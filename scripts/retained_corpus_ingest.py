#!/usr/bin/env python3
"""Build or service-ingest a public-only retained-corpus artifact."""
from __future__ import annotations
import argparse, json, os, sys, time, urllib.error, urllib.request
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curator.config import load_config
from curator.pipeline import configured_source_specs
from curator.grouping import GroupingCandidate, GroupingPolicy, exact_matches
from curator.retained_corpus import apply_translations, public_ingest_rows, retain
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest
from curator.recommendation.supabase_http import _NoRedirect, validate_https_origin
from curator.sources import SafeHttpPolicy, SafeHttpTransport
from curator.translation import (
    ModelPairingAdapter,
    ModelTranslationAdapter,
    ModelTranslationConfig,
    SupabaseTranslationConfig,
    SupabaseTranslationStore,
)
from curator.translation.base import TranslationPrivacyError, TranslationProviderError
from curator.translation.ingest import IngestTranslationPolicy, translate_exclusive_stories
from curator.translation.pairing import (ExclusivityDecision, PairingCost, PairingPolicy,
                                         decide_exclusivity)
from curator.translation.store import StoreErrorReason, TranslationStoreError


# Everything remote that translation depends on. An outage in any of them is a
# named skip with exit 0: the hourly corpus ingest must still complete, because
# every retained row for the run would otherwise be lost with it.
# WHY THIS TUPLE IS EXACTLY THIS WIDE, decided once so it stops oscillating.
#
# Round 2 said the bare `except Exception` hid code defects. Round 3 narrowed it
# to three typed errors. Round 4 widened it again, because a 503 from the spend
# RPC was escaping and killing the whole hourly ingest, losing every retained
# row for that run. Round 5 settles it on a second-order principle:
#
#   Recoverability beats strictness for the CORPUS, strictness beats
#   recoverability for OUR OWN BUGS.
#
# Translation is an enrichment; the corpus ingest is the product. So anything
# shaped like a remote failure (transport, timeout, malformed remote payload,
# typed provider/store errors) degrades to a named skip with exit 0. Anything
# shaped like a defect in this repository (TypeError, KeyError, AttributeError,
# and a bare ValueError from our own validation) stays loud, because a silent
# ValueError is how a bad int() or a dataclass guard turns into "translation
# unavailable" for ever with nobody looking.
#
# Before widening this again, answer: what does the new entry REFUSE when its
# assumption is wrong? If the answer is "a real bug, silently", do not add it.
TRANSLATION_TRANSPORT_ERRORS = (
    TranslationProviderError,
    TranslationStoreError,
    TranslationPrivacyError,
    urllib.error.URLError,        # HTTPError is a subclass
    TimeoutError,
    ConnectionError,
    json.JSONDecodeError,         # a remote payload we could not parse
)



def _ingest_translation_policy(cfg) -> IngestTranslationPolicy:
    """Build the validated policy from config. Boot fails on a bad value."""
    translation, language = cfg.translation or {}, cfg.language or {}
    return IngestTranslationPolicy(
        enabled=bool(translation.get('enabled', False)),
        provider=str(translation.get('provider') or 'google'),
        display_language=str(language.get('default_display') or 'en'),
        run_character_limit=int(translation.get('run_character_limit', 2000)),
        day_character_limit=int(translation.get('day_character_limit', 15000)),
        month_character_limit=int(translation.get('month_character_limit', 450000)),
        daily_cost_limit_usd=float(translation.get('daily_cost_limit_usd', 0.5)),
        input_cost_per_million_tokens_usd=float(translation.get('input_cost_per_million_tokens_usd', 0.25)),
        output_cost_per_million_tokens_usd=float(translation.get('output_cost_per_million_tokens_usd', 2.0)),
        characters_per_token=int(translation.get('characters_per_token', 4)),
        max_output_tokens_per_story=int(translation.get('max_output_tokens_per_story', 1000)),
        cache_ttl_days=int(translation.get('cache_ttl_days', 30)),
        on_failure=str(translation.get('on_failure') or 'show_original_marked'),
        max_items=int(translation.get('max_items_per_language', 25)),
        normalization_version=str(translation.get('normalization_version') or 'normalized-item-v1'),
        glossary_policy_version=str(translation.get('glossary_policy_version') or 'none-v1'),
        candidate_policy_version=str(translation.get('candidate_policy_version') or 'ranked-non-newsletter-v1'),
    )


def _service_headers(key):
    headers = {'apikey': key, 'content-type': 'application/json'}
    if not key.startswith('sb_secret_'):
        headers['authorization'] = 'Bearer ' + key
    return headers


def _rpc(url, key, name, body, *, timeout=30):
    request = urllib.request.Request(url + '/rest/v1/rpc/' + name,
                                     data=json.dumps(body).encode(),
                                     headers=_service_headers(key), method='POST')
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        if response.status != 200:
            # A typed store error, not a bare ValueError: the ingest boundary
            # deliberately does NOT catch ValueError (that is how our own bugs
            # stay loud), so a non-200 here would otherwise kill the whole run.
            raise TranslationStoreError(StoreErrorReason.UNAVAILABLE)
        return json.loads(response.read() or b'null')


class PersistedPairingLedger:
    """The pairing call budget: dollars AND a per-UTC-day call count, in SQL."""

    def __init__(self, url, key, *, daily_limit_usd, daily_call_limit, overrun_tolerance_usd=0.05):
        self._url, self._key = url, key
        self._limit, self._calls = float(daily_limit_usd), int(daily_call_limit)
        self._tolerance = float(overrun_tolerance_usd)
        self._scope_key = None

    def reserve_call(self, amount_usd: float) -> bool:
        result = _rpc(self._url, self._key, 'm2_reserve_pairing_call',
                      {'p_amount_usd': round(float(amount_usd), 6),
                       'p_daily_limit_usd': self._limit, 'p_daily_call_limit': self._calls})
        if not result or result.get('status') != 'reserved':
            return False
        # Remember the day this reservation belongs to: a run that crosses
        # midnight must settle where it reserved.
        self._scope_key = result.get('scope_key')
        return True

    def settle_call(self, reserved_usd: float, settled_usd: float) -> None:
        outcome = _rpc(self._url, self._key, 'm2_settle_translation_spend',
                       {'p_reserved_usd': round(float(reserved_usd), 6),
                        'p_settled_usd': round(float(settled_usd), 6),
                        'p_day_key': self._scope_key,
                        'p_overrun_tolerance_usd': self._tolerance})
        if isinstance(outcome, dict) and outcome.get('overrun'):
            print('::warning::pairing settlement exceeded its reservation and was clamped', file=sys.stderr)


class PersistedSpendLedger:
    """The daily dollar cap, held in SQL so it survives twelve runs an hour."""

    def __init__(self, url, key, *, daily_limit_usd, overrun_tolerance_usd=0.05):
        self._url, self._key, self._limit = url, key, float(daily_limit_usd)
        self._tolerance = float(overrun_tolerance_usd)
        self._scope_key = None

    def reserve(self, amount_usd: float) -> bool:
        result = _rpc(self._url, self._key, 'm2_reserve_translation_spend',
                      {'p_amount_usd': round(float(amount_usd), 6), 'p_daily_limit_usd': self._limit})
        if not result or result.get('status') != 'reserved':
            return False
        self._scope_key = result.get('scope_key')
        return True

    def settle(self, reserved_usd: float, settled_usd: float) -> None:
        outcome = _rpc(self._url, self._key, 'm2_settle_translation_spend',
                       {'p_reserved_usd': round(float(reserved_usd), 6),
                        'p_settled_usd': round(float(settled_usd), 6),
                        'p_day_key': self._scope_key,
                        'p_overrun_tolerance_usd': self._tolerance})
        if isinstance(outcome, dict) and outcome.get('overrun'):
            print('::warning::translation settlement exceeded its reservation and was clamped', file=sys.stderr)

    def retain(self, reserved_usd: float) -> None:
        # A retained reservation stays reserved in SQL: nothing to do, and that
        # is the point. It keeps protecting the cap until the day rolls over.
        return None

    def release(self, reserved_usd: float) -> None:
        """A pre-send abort cost nothing, so the day gets its money back.

        Released against the day it was RESERVED on, or a reservation made
        before midnight stays held for ever.
        """
        _rpc(self._url, self._key, 'm2_release_translation_spend',
             {'p_reserved_usd': round(float(reserved_usd), 6), 'p_day_key': self._scope_key})


def read_exclusivity_decisions(url, key, story_ids, *, display_language, policy_id):
    """Decisions already on record. A decided story is never re-asked."""
    if not story_ids:
        return {}
    rows = _rpc(url, key, 'm2_read_exclusivity_decisions',
                {'p_story_ids': list(story_ids), 'p_display_language': display_language,
                 'p_policy_id': policy_id})
    decisions = {}
    for row in rows or ():
        decisions[str(row['story_id'])] = ExclusivityDecision(
            story_id=str(row['story_id']),
            decided_at=datetime.fromisoformat(str(row['decided_at']).replace('Z', '+00:00')),
            model=str(row.get('model') or ''), policy_id=str(row.get('policy_id') or ''),
            match_story_id=row.get('match_story_id'),
            outcome=str(row.get('outcome') or 'exclusive'),
            display_language=str(row.get('display_language') or display_language),
            attempts=int(row.get('attempts') or 1),
            retry_after=(datetime.fromisoformat(str(row['retry_after']).replace('Z', '+00:00'))
                         if row.get('retry_after') else None),
            rechecked_at=(datetime.fromisoformat(str(row['rechecked_at']).replace('Z', '+00:00'))
                          if row.get('rechecked_at') else None))
    return decisions


def recheck_exclusivity_decision(url, key, decision):
    """Persist a re-check and return what the STORE now holds, never our attempt."""
    row = _rpc(url, key, 'm2_recheck_exclusivity_decision', {
        'p_story_id': decision.story_id, 'p_display_language': decision.display_language,
        'p_policy_id': decision.policy_id, 'p_outcome': decision.outcome,
        'p_match_story_id': decision.match_story_id})
    return _decision_from_row(row, decision.display_language) if row and row.get('story_id') else None


def _decision_from_row(row, display_language):
    return ExclusivityDecision(
        story_id=str(row['story_id']),
        decided_at=datetime.fromisoformat(str(row['decided_at']).replace('Z', '+00:00')),
        model=str(row.get('model') or ''), policy_id=str(row.get('policy_id') or ''),
        match_story_id=row.get('match_story_id'),
        outcome=str(row.get('outcome') or 'exclusive'),
        display_language=str(row.get('display_language') or display_language),
        attempts=int(row.get('attempts') or 1),
        retry_after=(datetime.fromisoformat(str(row['retry_after']).replace('Z', '+00:00'))
                     if row.get('retry_after') else None),
        rechecked_at=(datetime.fromisoformat(str(row['rechecked_at']).replace('Z', '+00:00'))
                      if row.get('rechecked_at') else None))


def record_exclusivity_decision(url, key, decision):
    """Write BEFORE the decision is allowed to matter. A settled answer is
    written once; an undecided one carries its attempt count and retry time."""
    _rpc(url, key, 'm2_record_exclusivity_decision', {
        'p_story_id': decision.story_id, 'p_display_language': decision.display_language,
        'p_policy_id': decision.policy_id, 'p_model': decision.model,
        'p_outcome': decision.outcome, 'p_match_story_id': decision.match_story_id,
        'p_retry_after': decision.retry_after.isoformat() if decision.retry_after else None})


def ingest_corpus_rows(url, key, rows):
    """STEP ONE of the hourly run: the corpus itself, before any paid work.

    Translation is an enrichment; the corpus is the product. Writing it first is
    what makes a cancelled run survivable: the rows are already in, and only the
    overlay is missing until the next run fills it.
    """
    body = json.dumps({'p_rows': rows}).encode()
    request = urllib.request.Request(url + '/rest/v1/rpc/m2_ingest_retained_corpus',
                                     data=body, headers=_service_headers(key), method='POST')
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError('retained corpus ingest failed')


def overlay_rows(rows):
    """STEP TWO's payload: only what pairing and translation decided."""
    payload = []
    for row in rows:
        entry = {'story_id': row.story_id}
        if row.title_translations:
            entry['title_translations'] = dict(row.title_translations)
        if row.summary_translations:
            entry['summary_translations'] = dict(row.summary_translations)
        if row.event_group_id:
            entry['event_group_id'] = row.event_group_id
        if len(entry) > 1:
            payload.append(entry)
    return payload


def apply_overlay_rows(url, key, rows):
    """Write the overlay onto rows the corpus write already put in.

    NOT a second m2_ingest_retained_corpus call: that upsert only fires when the
    incoming source_observed_at is NEWER, so re-sending the same rows would be a
    no-op and the overlay would never land. This RPC touches the three overlay
    columns only, merging translations and never replacing an existing group id.
    """
    if not rows:
        return 0
    return _rpc(url, key, 'm2_apply_retained_overlay', {'p_rows': rows})


def _overlay_failure_reason(error):
    """Name the cause, and name the DEPLOY ORDER when that is what it is.

    PostgREST answers 404 for a function it has never seen, which is exactly the
    state between this code reaching main and the migration being applied. An
    operator reading the log must not have to guess that.
    """
    if error.code == 404:
        return ('HTTP 404, m2_apply_retained_overlay is not deployed yet: apply '
                'supabase/migrations/202609170001_m2_retained_overlay.sql, then reload '
                'the PostgREST schema cache. The corpus write for this run is already done.')
    return f'HTTP {error.code}'


def read_corpus_window(url, key, *, policy, now, pages: int = 40):
    """Read every corpus row inside the PAIRING window, newest first.

    Pairing must see stories ingested by EARLIER runs, not only this batch. The
    read RPC caps at 100 rows per call, so this pages until it leaves the window
    or hits the page cap. Hitting the cap means the window was NOT fully read,
    which the caller treats as a reason to skip paid work, not to guess.
    """
    headers = _service_headers(key)
    horizon = now - timedelta(hours=policy.window_hours)
    collected, before_published, before_story, truncated = [], None, None, False
    for page in range(pages):
        body = json.dumps({'p_category_id': None, 'p_query': None,
                           'p_before_published_at': before_published,
                           'p_before_story_id': before_story, 'p_limit': 100}).encode()
        request = urllib.request.Request(url + '/rest/v1/rpc/m2_retained_candidates',
                                         data=body, headers=headers, method='POST')
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError('retained corpus read failed')
            rows = json.loads(response.read())
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            published = datetime.fromisoformat(str(row['published_at']).replace('Z', '+00:00'))
            if published < horizon:
                return tuple(collected), False
            collected.append(GroupingCandidate(
                story_id=str(row['story_id']), language=str(row['language']),
                title=str(row['title']), summary=str(row.get('summary') or ''),
                published_at=published, canonical_url=str(row.get('canonical_url') or ''),
                category_ids=tuple(row.get('category_ids') or ()),
                event_group_id=row.get('event_group_id')))
        before_published, before_story = rows[-1]['published_at'], rows[-1]['story_id']
        if len(rows) < 100:
            # The corpus itself ran out inside the window: fully read.
            return tuple(collected), False
        truncated = page == pages - 1
    return tuple(collected), truncated


def translate_rows(cfg, rows, *, env, now, store=None, provider=None, corpus=(),
                   pairing_provider=None, decisions=None, truncated=False, on_decision=None,
                   spend_ledger=None, pairing_ledger=None, persist_decision=None,
                   recheck_decision=None, truncation_reason='page_ceiling', run_started=None):
    """Decide exclusivity with the model, then translate what it ruled exclusive.

    Returns ``(rows, message)``. A missing credential, a switched-off feature, a
    provider failure or an unavailable cache is always a named SKIP with exit 0:
    the hourly ingest must keep running and no story is ever dropped for a
    translation problem. A programmer error is NOT swallowed.
    """
    try:
        policy = _ingest_translation_policy(cfg)
        pairing_policy = PairingPolicy.from_config(cfg.translation or {})
    except ValueError as error:
        return rows, f'translation unavailable: invalid policy ({error})'
    if not policy.enabled:
        return rows, 'translation skipped: sources.yaml translation.enabled is false'
    translation = cfg.translation or {}
    key_env = str(translation.get('api_key_env') or '')
    api_key = env.get(key_env, '') if key_env else ''
    if not api_key:
        # Exit 0, but make it impossible to miss in the Actions log.
        name = key_env or 'translation.api_key_env'
        print(f'::warning::translation skipped: {name} not configured', file=sys.stderr)
        return rows, f'translation skipped: {name} is not set'
    if truncated:
        # Exclusivity is unproven when the window was not fully read, and a
        # wrong exclusivity claim spends money on a story already covered.
        # Silent-off for ever if the corpus outgrows the ceiling, so this is a
        # warning, not a plain log line. Name the ACTUAL cause: telling an
        # operator to raise a page limit during a database outage wastes the
        # one signal this path has.
        if truncation_reason == 'page_ceiling':
            print('::warning::translation skipped: corpus read-back hit the page ceiling '
                  '(raise translation.corpus_readback_max_pages)', file=sys.stderr)
        else:
            print(f'::warning::translation skipped: corpus read-back failed ({truncation_reason})',
                  file=sys.stderr)
        return rows, 'translation skipped: corpus read-back truncated'

    batch = tuple(_pairing_candidate(row) for row in rows)
    # The work queue is the batch PLUS the other-language stories already in the
    # corpus window. A story this run's budget skipped falls out of the feed
    # within hours; if the queue were only the current fetch it would then be
    # asked by nobody, ever. decide_exclusivity filters by window and by
    # decision, so a settled story here costs nothing.
    queue = batch + tuple(row for row in corpus
                          if row.language != policy.display_language
                          and row.story_id not in {row.story_id for row in batch})
    try:
        if store is None or provider is None or pairing_provider is None:
            built = _build_translation_clients(translation, api_key, env)
            if built is None:
                return rows, 'translation skipped: store not configured'
            store = store or built[0]
            provider = provider or built[1]
            pairing_provider = pairing_provider or built[2]
    except TRANSLATION_TRANSPORT_ERRORS as error:
        return rows, f'translation unavailable: client not built ({type(error).__name__})'

    prefilter = exact_matches(batch + tuple(corpus), policy=GroupingPolicy.from_config(cfg.grouping or {}))
    try:
        decision = decide_exclusivity(
            queue, corpus, display_language=policy.display_language, policy=pairing_policy,
            started=run_started,
            provider=pairing_provider, now=now, already_decided=dict(decisions or {}),
            prefilter=prefilter, ledger=pairing_ledger, persist=persist_decision,
            recheck=recheck_decision,
            cost=PairingCost(
                input_cost_per_million_tokens_usd=policy.input_cost_per_million_tokens_usd,
                output_cost_per_million_tokens_usd=policy.output_cost_per_million_tokens_usd,
                characters_per_token=policy.characters_per_token,
                # The SAME key the request cap uses. Matching defaults are not
                # the same thing as one source of truth: raise the cap in config
                # and the reservation must move with it.
                output_allowance_tokens=int(translation.get('pairing_output_tokens', 64))))
    except TRANSLATION_TRANSPORT_ERRORS as error:
        print(f'::warning::pairing unavailable: {type(error).__name__}', file=sys.stderr)
        return rows, f'pairing unavailable: {type(error).__name__}'
    if decision.budget_stop == 'daily_cap':
        # The PERSISTED day budget, not this run's share: every run for the rest
        # of the UTC day gets the same answer, so it is a warning an operator can
        # see, and budget_refusals is the tripwire for a backlog that never drains.
        print(f'::warning::pairing budget reached: daily cap, calls={decision.calls} '
              f'remaining_undecided={decision.budget_skipped} '
              f'budget_refusals={decision.budget_refusals}', file=sys.stderr)
    elif decision.budget_stop is not None:
        # Not a failure: this run took its share, persisted every answer it got
        # and left the rest undecided for the next run. Exit stays 0.
        print(f'pairing budget reached: calls={decision.attempted_calls} '
              f'elapsed={decision.elapsed_seconds:.1f} '
              f'remaining_undecided={decision.budget_skipped} '
              f'budget_refusals={decision.budget_refusals}', file=sys.stderr)
    if on_decision is not None:
        for record in decision.pending:
            on_decision(record)
    rows = _apply_group_ids(rows, decision.group_ids)

    exclusive = set(decision.exclusive_story_ids)
    stories = [(row.story_id, row.item) for row in rows if row.story_id in exclusive]
    if not stories:
        return rows, (f'translation skipped: no language-exclusive stories '
                      f'(pairing calls={decision.calls} undecided={len(decision.undecided)} '
                      f'budget_refusals={decision.budget_refusals} '
                      f'persistence_failures={decision.persistence_failures})')
    try:
        result = translate_exclusive_stories(
            stories, policy=policy, store=store, provider=provider,
            run_id=f"retained-corpus-{now.strftime('%Y%m%dT%H%M%SZ')}", now=now,
            spend_ledger=spend_ledger)
    except TRANSLATION_TRANSPORT_ERRORS as error:
        # Provider and store degradation only. A TypeError here is a code defect
        # and must surface, not hide behind "original text retained".
        return rows, f'translation unavailable: {type(error).__name__}, original text retained'
    translated = result.counters.get('translated', 0) + result.counters.get('cache_hit', 0)
    return (apply_translations(rows, result.overlays),
            f'translated={translated} untranslated_shown={result.untranslated_shown} '
            f'pairing_calls={decision.calls} undecided={len(decision.undecided)} '
            f'budget_refusals={decision.budget_refusals} '
            f'persistence_failures={decision.persistence_failures}')


def _pairing_candidate(row):
    return GroupingCandidate(
        story_id=row.story_id, language=row.item.language, title=row.item.title,
        summary=row.item.description or '', published_at=row.item.published_at,
        canonical_url=row.item.canonical_url, category_ids=tuple(sorted(row.category_ids)),
        event_group_id=row.event_group_id)


def _apply_group_ids(rows, group_ids):
    """An id a row already carries is authoritative and is never replaced."""
    from dataclasses import replace as _replace
    return tuple(_replace(row, event_group_id=row.event_group_id or group_ids[row.story_id])
                 if not row.event_group_id and row.story_id in group_ids else row
                 for row in rows)


def _build_translation_clients(translation, api_key, env):
    url = env.get(str(translation.get('supabase_url_env') or ''), '')
    service_key = env.get(str(translation.get('supabase_service_role_key_env') or ''), '')
    if not url or not service_key:
        return None
    validate_https_origin(url)
    transport = SafeHttpTransport(policy=SafeHttpPolicy(
        total_timeout_seconds=float(translation.get('request_timeout_seconds', 20)),
        max_wire_bytes=int(translation.get('max_response_bytes', 524288)),
        max_decoded_bytes=int(translation.get('max_response_bytes', 524288)),
        per_host_concurrency=int(translation.get('per_host_concurrency', 2))))
    store = SupabaseTranslationStore(SupabaseTranslationConfig(url, service_key), transport=transport)
    pairing = ModelPairingAdapter(
        config=ModelTranslationConfig(
            provider_id=str(translation.get('provider') or 'openai'),
            model=str(translation.get('model') or ''),
            api_origin=str(translation.get('api_origin') or 'https://api.openai.com'),
            max_response_bytes=int(translation.get('max_response_bytes', 524288)),
            max_output_tokens=int(translation.get('max_output_tokens_per_story', 1000)),
            pairing_output_tokens=int(translation.get('pairing_output_tokens', 64)),
            reasoning_effort=str(translation.get('reasoning_effort') or 'minimal')),
        transport=transport, api_key=lambda: api_key)
    provider = ModelTranslationAdapter(
        config=ModelTranslationConfig(
            provider_id=str(translation.get('provider') or 'openai'),
            model=str(translation.get('model') or ''),
            api_origin=str(translation.get('api_origin') or 'https://api.openai.com'),
            max_response_bytes=int(translation.get('max_response_bytes', 524288)),
            max_output_tokens=int(translation.get('max_output_tokens_per_story', 1000)),
            pairing_output_tokens=int(translation.get('pairing_output_tokens', 64)),
            reasoning_effort=str(translation.get('reasoning_effort') or 'minimal')),
        transport=transport, api_key=lambda: api_key)
    return store, provider, pairing

def main() -> int:
    # The run's clock starts HERE, before the corpus read-back, so the read-back
    # and the pairing loop share one `translation.run_time_budget_seconds`.
    run_started = time.monotonic()
    p=argparse.ArgumentParser(); p.add_argument('command', choices=('build','ingest')); p.add_argument('--root',type=Path,default=Path.cwd()); p.add_argument('--source-snapshot',type=Path,required=True); p.add_argument('--output',type=Path); a=p.parse_args()
    cfg=load_config(a.root); snap=load_source_snapshot(a.source_snapshot, expected_configuration_digest=snapshot_config_digest(cfg))
    # Use the same registry route enumeration as collection.  The global
    # Hacker News adapter is configured outside ``rss`` and category feeds.
    # Filtering before deduplication rejects an unexpected snapshot origin
    # before the expensive fuzzy pass, without changing eligible provenance.
    allowed = {spec.id for spec in configured_source_specs(cfg)}
    source_items = [
        item for result in snap.results for item in result.items
        if item.source_id in allowed
    ]
    unexpected = {
        item.source_id for result in snap.results for item in result.items
        if item.source_id not in allowed
    }
    if unexpected:
        raise ValueError("snapshot contains an unconfigured source")
    grouping_policy = GroupingPolicy.from_config(cfg.grouping or {})
    pairing_policy = PairingPolicy.from_config(cfg.translation or {})
    retained = retain(source_items, categories=cfg.categories, observed_at=snap.generated_at,
                      grouping=grouping_policy, fallback_category_for=cfg.fallback_category_for)
    corpus, truncated, decisions = (), False, {}
    truncation_reason = 'page_ceiling'
    url = os.environ.get('NEWS_CURATOR_SUPABASE_URL', '')
    key = os.environ.get('NEWS_CURATOR_SUPABASE_SECRET_KEY', '')
    if a.command == 'ingest':
        # STEP ONE, before the corpus read-back, the pairing loop and any paid
        # call: the retained rows go in. Everything after this point may be
        # cancelled (a job timeout, a stopped run) and the hour's corpus still
        # lands. Losing an overlay costs one run of translation; losing the
        # corpus write loses the stories.
        if not url or not key:
            raise ValueError('retained corpus ingest unavailable')
        validate_https_origin(url)
        ingest_corpus_rows(url, key, public_ingest_rows(retained, allowed_source_ids=allowed))
    if a.command == 'ingest' and url and key:
        try:
            corpus, truncated = read_corpus_window(
                url, key, policy=pairing_policy, now=snap.generated_at,
                pages=int((cfg.translation or {}).get('corpus_readback_max_pages', 40)))
            display_language = str((cfg.language or {}).get('default_display') or 'en')
            # The backlog is part of the work queue, so its decisions have to be
            # read too, or every corpus story looks undecided and is re-asked.
            lookup_ids = list(dict.fromkeys([row.story_id for row in retained]
                                            + [row.story_id for row in corpus
                                               if row.language != display_language]))
            decisions = read_exclusivity_decisions(
                url, key, lookup_ids,
                display_language=str((cfg.language or {}).get('default_display') or 'en'),
                policy_id=pairing_policy.policy_id)
            print(f'pairing corpus rows={len(corpus)} truncated={truncated} '
                  f'decisions={len(decisions)}', file=sys.stderr)
        except (urllib.error.URLError, TranslationStoreError, ValueError, json.JSONDecodeError) as error:
            # Without the window we cannot prove exclusivity, so this run does
            # not pay for translation. It is a skip, never a wrong claim.
            truncated = True
            truncation_reason = type(error).__name__
            print(f'pairing corpus unavailable: {truncation_reason}', file=sys.stderr)
    recorded = []
    live = a.command == 'ingest' and url and key
    translation_cfg = cfg.translation or {}
    tolerance = translation_cfg.get('settle_overrun_tolerance_usd', 0.05)
    spend_ledger = (PersistedSpendLedger(url, key,
                        daily_limit_usd=translation_cfg.get('daily_cost_limit_usd', 0.5),
                        overrun_tolerance_usd=tolerance)
                    if live else None)
    pairing_ledger = (PersistedPairingLedger(url, key,
                          daily_limit_usd=translation_cfg.get('daily_cost_limit_usd', 0.5),
                          daily_call_limit=translation_cfg.get('pairing_daily_call_limit', 600),
                          overrun_tolerance_usd=tolerance)
                      if live else None)
    # Persist BEFORE the answer is allowed to move money or change what is shown.
    persist = ((lambda decision: record_exclusivity_decision(url, key, decision)) if live else None)
    recheck = ((lambda decision: recheck_exclusivity_decision(url, key, decision)) if live else None)
    retained, translation_message = translate_rows(
        cfg, retained, env=os.environ, now=snap.generated_at, corpus=corpus,
        decisions=decisions, truncated=truncated, on_decision=recorded.append,
        spend_ledger=spend_ledger, pairing_ledger=pairing_ledger, persist_decision=persist,
        recheck_decision=recheck, truncation_reason=truncation_reason, run_started=run_started)
    print(translation_message, file=sys.stderr)
    if recorded:
        print(f'exclusivity decisions persisted={len(recorded)}', file=sys.stderr)
    if a.command == 'build':
        if not a.output: raise ValueError('output required')
        rows = public_ingest_rows(retained, allowed_source_ids=allowed)
        a.output.write_text(json.dumps({'schema_version':1,'generated_at':snap.generated_at.isoformat(),'rows':rows}, ensure_ascii=False), encoding='utf-8'); return 0
    # STEP TWO: only what pairing and translation decided, merged onto the rows
    # step one already wrote. This is an ENRICHMENT write, so it obeys the rule
    # stated at the top of this file: a remote failure is a named skip with exit
    # 0, and the corpus write that already happened stays done.
    overlay = overlay_rows(retained)
    try:
        applied = apply_overlay_rows(url, key, overlay)
    except urllib.error.HTTPError as error:
        print(f'::warning::overlay not applied: {_overlay_failure_reason(error)}', file=sys.stderr)
        return 0
    except TRANSLATION_TRANSPORT_ERRORS as error:
        print(f'::warning::overlay not applied: {type(error).__name__}', file=sys.stderr)
        return 0
    print(f'overlay rows={len(overlay)} applied={applied}', file=sys.stderr)
    return 0
if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception: print('retained corpus ingest unavailable', file=sys.stderr); raise SystemExit(2)
