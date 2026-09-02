"""Produce a fail-soft, schema-validated translation overlay.

The job ranks original items first. It never translates newsletters, never
feeds translated text back into ranking, and never emits an unconfirmed paid
response. Missing configuration or any provider/store failure yields a valid
empty artifact so the ordinary source build remains usable.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

# GitHub Actions invokes this file directly from the repository root. Python
# otherwise puts only ``scripts/`` on sys.path, so the local package would not
# be importable on the exact production command path.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curator.config import Config, load_config  # noqa: E402
from curator.localization import story_id_for_item, write_translation_artifact  # noqa: E402
from curator.models import Item, TranslationRecord  # noqa: E402
from curator.normalize import clean_title  # noqa: E402
from curator.pipeline import build_ranked_language, collect  # noqa: E402
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest  # noqa: E402
from curator.sources import SafeHttpPolicy, SafeHttpTransport  # noqa: E402
from curator.translation import (  # noqa: E402
    DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS,
    DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS,
    AcquireRequest,
    AcquireStatus,
    BudgetLimits,
    GoogleTranslationAdapter,
    GoogleTranslationConfig,
    ReservationState,
    SupabaseTranslationConfig,
    SupabaseTranslationStore,
    TranslationCacheKey,
    TranslationCacheRecord,
    TranslationCandidatePolicy,
    TranslationInput,
    TranslationOutputLimits,
    TranslationProviderRegistry,
    TranslationProviderRequest,
    TranslationRequestItem,
    select_translation_candidates,
)


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_HEALTH_KEYS = (
    "translated",
    "cache_hit",
    "budget_exhausted",
    "blocked",
    "quarantined",
    "existing",
    "recover_failed",
    "acquire_failed",
    "cache_invalid",
    "mark_sent_failed",
    "provider_failed",
    "provider_contract_failed",
    "settlement_failed",
    "charge_unknown",
    "failed_before_send",
    "persistence_unknown",
)
_DEGRADED_HEALTH_KEYS = tuple(
    key for key in _HEALTH_KEYS if key not in {"translated", "cache_hit"}
)


@dataclass(frozen=True)
class TranslationProductionResult:
    records: tuple[TranslationRecord, ...]
    counters: Mapping[str, int]
    fatal_persistence_failure: bool = False

    def warning_summary(self) -> str:
        pairs = [f"{key}={self.counters.get(key, 0)}" for key in _HEALTH_KEYS if self.counters.get(key, 0)]
        return ",".join(pairs) or "no_candidates=1"

    def degraded_summary(self) -> str:
        return ",".join(
            f"{key}={self.counters.get(key, 0)}"
            for key in _DEGRADED_HEALTH_KEYS
            if self.counters.get(key, 0)
        )


def produce_translation_records(
    *,
    cfg: Config,
    ranked_by_language: Mapping[str, Mapping[str, list[Item]]],
    store,
    provider,
    now: datetime,
    run_id: str,
) -> TranslationProductionResult:
    """Run conservative paid transitions over already-ranked originals."""

    policy = cfg.translation
    provider_id = _text(policy, "provider")
    model_version = _provider_model_identity(provider)
    normalization_version = _text(policy, "normalization_version")
    glossary_version = _text(policy, "glossary_policy_version")
    candidate_version = _text(policy, "candidate_policy_version")
    targets = tuple(policy.get("targets") or ("en", "zh"))
    limits = BudgetLimits(
        _positive_int(policy, "run_character_limit"),
        _positive_int(policy, "day_character_limit"),
        _positive_int(policy, "month_character_limit"),
    )
    candidate_policy = TranslationCandidatePolicy(
        max_items=_positive_int(policy, "max_items_per_language"),
        max_characters=_positive_int(policy, "max_characters_per_language"),
    )
    safe_run_id = _safe_identifier(run_id)
    records: list[TranslationRecord] = []
    counters: Counter[str] = Counter()
    fatal_persistence_failure = False
    output_limits = TranslationOutputLimits(
        title=_positive_int_with_default(
            policy,
            "max_output_title_characters",
            DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS,
        ),
        description=_positive_int_with_default(
            policy,
            "max_output_description_characters",
            DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS,
        ),
    )
    lease_timeout_seconds = _positive_int_with_default(policy, "lease_timeout_seconds", 900)
    sent_timeout_seconds = _positive_int_with_default(policy, "sent_timeout_seconds", 900)

    candidate_streams = _translation_candidate_streams(
        targets=targets,
        ranked_by_language=ranked_by_language,
        candidate_policy=candidate_policy,
    )
    candidate_rounds = max(
        (len(candidates) for _, _, _, candidates in candidate_streams),
        default=0,
    )
    # One candidate per configured direction per round prevents a stable target
    # order from consuming the shared run budget before another direction gets
    # a chance. Candidate order within each direction remains rank-stable.
    for candidate_index in range(candidate_rounds):
        for target, source, by_digest, candidates in candidate_streams:
            if candidate_index >= len(candidates):
                continue
            candidate = candidates[candidate_index]
            item = by_digest.get(candidate.content.digest)
            if item is None:
                continue
            story_id = story_id_for_item(item)
            key = TranslationCacheKey.from_input(
                story_id=story_id,
                content=candidate.content,
                target_locale=target,
                normalization_version=normalization_version,
                provider=provider_id,
                model_version=model_version,
                glossary_policy_version=glossary_version,
                candidate_policy_version=candidate_version,
            )
            idempotency_key = _safe_identifier(
                f"{safe_run_id}:{key.digest[:40]}"
            )
            request = AcquireRequest(
                key=key,
                idempotency_key=idempotency_key,
                run_id=safe_run_id,
                reserved_characters=candidate.content.character_count,
                limits=limits,
            )
            try:
                store.recover_stale(
                    key,
                    lease_timeout_seconds=lease_timeout_seconds,
                    sent_timeout_seconds=sent_timeout_seconds,
                )
            except Exception:
                counters["recover_failed"] += 1
            try:
                acquired = store.acquire(request)
            except Exception:
                counters["acquire_failed"] += 1
                continue
            if acquired.status == AcquireStatus.CACHE_HIT:
                if acquired.cache is None or acquired.cache.key != key:
                    counters["cache_invalid"] += 1
                    try:
                        store.quarantine(key, reason_code="response_correlation")
                    except Exception:
                        counters["persistence_unknown"] += 1
                    continue
                try:
                    output_limits.validate(
                        clean_title(acquired.cache.translated_title),
                        clean_title(acquired.cache.translated_description),
                    )
                    records.append(_record_from_cache(acquired.cache))
                    counters["cache_hit"] += 1
                except (TypeError, ValueError):
                    counters["cache_invalid"] += 1
                    try:
                        store.quarantine(key, reason_code="output_contract")
                    except Exception:
                        counters["persistence_unknown"] += 1
                continue
            if not _reservation_response_correlates(acquired, request):
                counters["acquire_failed"] += 1
                continue
            if acquired.status != AcquireStatus.LEASED:
                if acquired.status.value in _HEALTH_KEYS:
                    counters[acquired.status.value] += 1
                continue
            try:
                sent = store.mark_sent(idempotency_key)
            except Exception:
                counters["mark_sent_failed"] += 1
                safe_state = _recover_uncertain_mark_sent(store, idempotency_key, counters)
                if not safe_state:
                    fatal_persistence_failure = True
                continue
            if sent.state != ReservationState.SENT:
                if sent.state == ReservationState.SETTLED:
                    cached = store.lookup(key)
                    if cached is not None:
                        try:
                            output_limits.validate(
                                clean_title(cached.translated_title),
                                clean_title(cached.translated_description),
                            )
                            records.append(_record_from_cache(cached))
                            counters["cache_hit"] += 1
                        except (TypeError, ValueError):
                            counters["cache_invalid"] += 1
                continue

            provider_request = TranslationProviderRequest(
                items=(candidate,),
                source_language=source,
                target_language=target,
            )
            try:
                response = provider.translate(provider_request)
                if (
                    response.provider != provider_id
                    or response.model_version != model_version
                    or response.source_language != provider_request.source_language
                    or response.target_language != provider_request.target_language
                    or len(response.items) != 1
                    or response.items[0].request_id != candidate.request_id
                ):
                    raise ValueError("provider contract mismatch")
                translated = response.items[0]
                title = clean_title(translated.title)
                description = clean_title(translated.description)
                output_limits.validate(title, description)
                cache = TranslationCacheRecord(
                    key=key,
                    translated_title=title,
                    translated_description=description,
                    actual_characters=candidate.content.character_count,
                    max_title_characters=output_limits.title,
                    max_description_characters=output_limits.description,
                )
            except Exception as exc:
                # mark_sent committed before the provider was entered. Any
                # provider/contract failure may therefore have incurred a charge.
                if isinstance(exc, ValueError):
                    counters["provider_contract_failed"] += 1
                else:
                    counters["provider_failed"] += 1
                if not _record_charge_unknown(store, idempotency_key, counters):
                    fatal_persistence_failure = True
                continue
            try:
                settled = store.settle(
                    idempotency_key,
                    actual_characters=candidate.content.character_count,
                    record=cache,
                )
            except Exception:
                counters["settlement_failed"] += 1
                if not _record_charge_unknown(store, idempotency_key, counters):
                    fatal_persistence_failure = True
                continue
            if settled.state != ReservationState.SETTLED:
                counters["charge_unknown"] += 1
                continue
            records.append(_record_from_cache(cache))
            counters["translated"] += 1
    bounded = {key: min(counters.get(key, 0), 10_000) for key in _HEALTH_KEYS}
    return TranslationProductionResult(tuple(records), bounded, fatal_persistence_failure)


def _reservation_response_correlates(acquired, request: AcquireRequest) -> bool:
    reservation = acquired.reservation
    if acquired.status in (AcquireStatus.LEASED, AcquireStatus.EXISTING):
        if reservation is None or reservation.request != request:
            return False
        return acquired.status is not AcquireStatus.LEASED or reservation.state is ReservationState.LEASED
    if acquired.status is AcquireStatus.BLOCKED:
        return bool(
            reservation is not None
            and reservation.request.key == request.key
            and reservation.request.idempotency_key != request.idempotency_key
            and reservation.state
            in (
                ReservationState.LEASED,
                ReservationState.SENT,
                ReservationState.CHARGE_UNKNOWN,
                ReservationState.CHARGED_WITHOUT_CACHE,
            )
        )
    return reservation is None


def _recover_uncertain_mark_sent(store, idempotency_key: str, counters: Counter[str]) -> bool:
    """Prove one conservative durable outcome after a lost mark-sent response."""

    try:
        current = store.mark_charge_unknown(idempotency_key)
    except Exception:
        try:
            current = store.mark_failed_before_send(idempotency_key)
        except Exception:
            counters["persistence_unknown"] += 1
            return False
        counters["failed_before_send"] += 1
        return current.state == ReservationState.FAILED_BEFORE_SEND
    if current.state == ReservationState.CHARGE_UNKNOWN:
        counters["charge_unknown"] += 1
        return True
    if current.state in (ReservationState.SETTLED, ReservationState.CHARGED_WITHOUT_CACHE):
        return True
    counters["persistence_unknown"] += 1
    return False


def _record_charge_unknown(store, idempotency_key: str, counters: Counter[str]) -> bool:
    try:
        conservative = store.mark_charge_unknown(idempotency_key)
    except Exception:
        counters["persistence_unknown"] += 1
        return False
    if conservative.state == ReservationState.CHARGE_UNKNOWN:
        counters["charge_unknown"] += 1
        return True
    if conservative.state in (ReservationState.SETTLED, ReservationState.CHARGED_WITHOUT_CACHE):
        return True
    counters["persistence_unknown"] += 1
    return False


def _record_from_cache(cache: TranslationCacheRecord) -> TranslationRecord:
    return TranslationRecord(
        story_id=cache.key.story_id,
        input_digest=cache.key.input_digest,
        source_language=cache.key.source_locale,
        target_language=cache.key.target_locale,
        title=clean_title(cache.translated_title),
        description=clean_title(cache.translated_description),
        provider=cache.key.provider,
        model_version=cache.key.model_version,
    )


def _unique_ranked(ranked: Mapping[str, list[Item]]) -> list[Item]:
    output: list[Item] = []
    seen: set[str] = set()
    rows_by_category = tuple(ranked.values())
    for rank_index in range(max((len(rows) for rows in rows_by_category), default=0)):
        for rows in rows_by_category:
            if rank_index >= len(rows):
                continue
            item = rows[rank_index]
            identity = story_id_for_item(item)
            if identity in seen:
                continue
            seen.add(identity)
            output.append(item)
    return output


def _translation_candidate_streams(
    *,
    targets: tuple[str, ...],
    ranked_by_language: Mapping[str, Mapping[str, list[Item]]],
    candidate_policy: TranslationCandidatePolicy,
) -> tuple[
    tuple[str, str, Mapping[str, Item], tuple[TranslationRequestItem, ...]], ...
]:
    """Build stable per-direction queues before shared-budget acquisition."""

    streams = []
    for target in targets:
        source = "zh" if target == "en" else "en"
        native_ids = {
            story_id_for_item(item)
            for item in _unique_ranked(ranked_by_language.get(target, {}))
        }
        originals = [
            item
            for item in _unique_ranked(ranked_by_language.get(source, {}))
            if not item.is_newsletter and story_id_for_item(item) not in native_ids
        ]
        candidates = select_translation_candidates(
            originals,
            target_language=target,
            policy=candidate_policy,
        )
        by_digest: dict[str, Item] = {}
        for item in originals:
            by_digest.setdefault(TranslationInput.from_item(item).digest, item)
        streams.append((target, source, by_digest, candidates))
    return tuple(streams)


def _text(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"translation {key} is required")
    return value.strip()


def _positive_int(values: Mapping[str, object], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"translation {key} must be a positive integer")
    return value


def _positive_int_with_default(values: Mapping[str, object], key: str, default: int) -> int:
    if key not in values:
        return default
    return _positive_int(values, key)


def _safe_identifier(value: str) -> str:
    if _ID.fullmatch(value):
        return value
    import hashlib

    return "run:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_token_file(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("token file must not be a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
        raise ValueError("token file is invalid")
    token = path.read_text(encoding="ascii")
    if not token or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in token):
        raise ValueError("token file is invalid")
    return token


def _empty(path: Path, now: datetime) -> int:
    write_translation_artifact((), path, generated_at=now)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--google-access-token-file", type=Path, required=True)
    parser.add_argument(
        "--source-snapshot",
        type=Path,
        help="validated authoritative originals from the no-secret collection job",
    )
    parser.add_argument("--run-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        cfg = load_config(args.root)
        if not cfg.translation.get("enabled", False):
            return _empty(args.output, now)
        authoritative_results = None
        if args.source_snapshot is not None:
            snapshot = load_source_snapshot(
                args.source_snapshot,
                expected_configuration_digest=snapshot_config_digest(cfg),
                current_time=now,
                max_age_seconds=cfg.source_snapshot_max_age_seconds,
            )
            authoritative_results = list(snapshot.results)
        policy = cfg.translation
        url = os.environ.get(_text(policy, "supabase_url_env"), "")
        service_key = os.environ.get(
            _text(policy, "supabase_service_role_key_env"), ""
        )
        project_id = os.environ.get(_text(policy, "project_id_env"), "")
        token = _read_token_file(args.google_access_token_file)
        transport = SafeHttpTransport(
            policy=SafeHttpPolicy(
                total_timeout_seconds=float(
                    _positive_int(policy, "request_timeout_seconds")
                ),
                max_wire_bytes=_positive_int(policy, "max_response_bytes"),
                max_decoded_bytes=_positive_int(policy, "max_response_bytes"),
                per_host_concurrency=_positive_int(
                    policy, "per_host_concurrency"
                ),
            )
        )
        store = SupabaseTranslationStore(
            SupabaseTranslationConfig(url, service_key), transport=transport
        )
        google = GoogleTranslationAdapter(
            config=GoogleTranslationConfig(
                project_id=project_id,
                location=_text(policy, "location"),
                model_version=_google_model_resource(policy, project_id),
                max_characters=_positive_int(
                    policy, "max_characters_per_language"
                ),
                max_response_bytes=_positive_int(policy, "max_response_bytes"),
                max_output_title_chars=_positive_int_with_default(
                    policy,
                    "max_output_title_characters",
                    DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS,
                ),
                max_output_description_chars=_positive_int_with_default(
                    policy,
                    "max_output_description_characters",
                    DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS,
                ),
            ),
            transport=transport,
            access_token=lambda: token,
        )
        registry = TranslationProviderRegistry({google.provider_id: google})
        provider = registry.get(_text(policy, "provider"))
        results = authoritative_results if authoritative_results is not None else collect(cfg)
        ranked = {
            language: build_ranked_language(
                cfg, results, now, language=language
            )
            for language in ("en", "zh")
        }
        run_id = args.run_id or _default_run_id(now, os.environ)
        result = produce_translation_records(
            cfg=cfg,
            ranked_by_language=ranked,
            store=store,
            provider=provider,
            now=now,
            run_id=run_id,
        )
        write_translation_artifact(result.records, args.output, generated_at=now)
        print("translation health: " + result.warning_summary())
        if result.degraded_summary():
            print(
                "::warning title=Translation lane degraded::" + result.degraded_summary(),
                file=sys.stderr,
            )
        if result.fatal_persistence_failure:
            print(
                "translation lane failed: ambiguous paid state was not durably recorded",
                file=sys.stderr,
            )
            return 1
        return 0
    except Exception:
        print("translation lane unavailable; wrote originals-only artifact", file=sys.stderr)
        return _empty(args.output, now)


def _timestamp_run(value: datetime) -> str:
    return "local:" + value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _default_run_id(value: datetime, environment: Mapping[str, str]) -> str:
    """Give each GitHub attempt its own retry-safe reservation namespace."""

    github_run_id = environment.get("GITHUB_RUN_ID")
    if not github_run_id:
        return _timestamp_run(value)
    github_attempt = environment.get("GITHUB_RUN_ATTEMPT") or "1"
    return f"github:{github_run_id}:{github_attempt}"


def _provider_model_identity(provider: object) -> str:
    """Return the exact model value the provider adapter sends on the wire."""

    value = getattr(provider, "model_version", None)
    if not isinstance(value, str) or not value:
        raise ValueError("translation provider model identity is unavailable")
    return value


def _google_model_resource(policy: Mapping[str, object], project_id: str) -> str:
    """Resolve the checked-in template without allowing arbitrary formatting."""

    location = _text(policy, "location")
    template = _text(policy, "model_version")
    pattern = re.compile(
        r"^projects/\{project_id\}/locations/\{location\}/models/"
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
        r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,63}){0,3}$"
    )
    if pattern.fullmatch(template) is None:
        raise ValueError("translation Google model resource template is invalid")
    return template.format(project_id=project_id, location=location)


if __name__ == "__main__":
    raise SystemExit(main())
