"""The two B8 corpus bugs, proven on PostgreSQL 17.11 with every migration applied.

A string-matched migration test proves nothing, so these APPLY the migrations
and call the real RPCs. Same container shape as
tests/test_m2_translation_postgres_runtime.py.

Covers:
  * duplicate stories in one result (identical title, same language, different
    canonical URL, so two durable corpus rows), and
  * the category floor: no retained row may reach the corpus with no category.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from curator.config import load_config
from curator.models import Item
from curator.retained_corpus import public_ingest_rows, retain

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'postgres:17.11'
MIGRATIONS = (
    'supabase/migrations/202609070001_reading_history.sql',
    'supabase/migrations/202609140002_m2_retained_corpus.sql',
    'supabase/migrations/202609160001_m2_translation_columns.sql',
    'supabase/migrations/202609170001_m2_retained_overlay.sql',
    'supabase/migrations/202608290002_translation_store.sql',
    'supabase/migrations/202609160002_m2_translation_spend_and_decisions.sql',
    'supabase/migrations/202609160003_m2_exclusive_lane_from_decisions.sql',
    'supabase/migrations/202609180101_m2_retained_candidates_dedupe.sql',
)
POLICY = 'pairing-json-v1'
NOW = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def _run(*args, input_text=None, check=True):
    return subprocess.run(args, input=input_text, capture_output=True, text=True,
                          timeout=120, check=check)


def _sql(container, sql, check=True):
    return _run('docker', 'exec', '-i', container, 'psql', '-X', '-At', '-U', 'postgres',
                '-v', 'ON_ERROR_STOP=1', input_text=sql, check=check)


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


@pytest.fixture(scope='module')
def db():
    if not shutil.which('docker'):
        pytest.skip('Docker unavailable; PostgreSQL 17.11 runtime not verified')
    if _run('docker', 'image', 'inspect', IMAGE, check=False).returncode:
        pytest.skip('Installed PostgreSQL 17.11 image or Docker daemon unavailable; no pull attempted')
    container = 'news-curator-dedupe-' + uuid.uuid4().hex[:10]
    _run('docker', 'run', '--rm', '-d', '--name', container, '-e', 'POSTGRES_PASSWORD=review-only', IMAGE)
    try:
        deadline = time.monotonic() + 30
        ready = 0
        while time.monotonic() < deadline:
            result = _sql(container, 'select 1;', check=False)
            ready = ready + 1 if result.returncode == 0 and result.stdout.strip() == '1' else 0
            if ready >= 2:
                break
            time.sleep(.25)
        else:
            pytest.fail('PostgreSQL 17.11 readiness timeout')
        _sql(container, """
          create role anon nologin;
          create role authenticated nologin;
          create role service_role nologin bypassrls;
          create schema extensions;
          create extension pgcrypto with schema extensions;
          create schema auth;
          create table auth.users(id uuid primary key);
          create function auth.uid() returns uuid language sql stable as $$
            select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
          create function auth.jwt() returns jsonb language sql stable as $$
            select coalesce(nullif(current_setting('request.jwt.claims',true),''),'{}')::jsonb $$;
          grant usage on schema public,auth to anon,authenticated,service_role;
          grant execute on function auth.uid(), auth.jwt() to anon,authenticated,service_role;
        """)
        for migration in MIGRATIONS:
            _sql(container, (ROOT / migration).read_text())
        yield container
    finally:
        _run('docker', 'stop', container, check=False)


@pytest.fixture(autouse=True)
def clean_corpus(db):
    """Every test owns the whole corpus.

    Order-coupled database tests hide regressions: a test can pass only because
    an earlier one left the right rows behind, and it then fails when run alone
    or reordered. The delete order follows the foreign keys, which are all
    `on delete restrict` (202609140002:6,33,43), so the children go first.
    """
    # One statement per call: ON_ERROR_STOP aborts the whole batch on the first
    # failure, and a cleanup that silently no-ops is precisely the bug that
    # order-coupling hides. The assertion below makes a failed clean LOUD.
    for statement in ('delete from translation_private.exclusivity_decisions;',
                      'delete from public.retained_corpus_categories;',
                      'delete from public.retained_corpus_source_categories;',
                      'delete from public.retained_corpus_observations;'):
        result = _sql(db, statement, check=False)
        if result.returncode:
            pytest.fail(f'corpus reset failed on {statement!r}: {result.stderr.strip()[:300]}')
    remaining = _sql(db, 'select count(*) from public.retained_corpus_observations;')
    assert remaining.stdout.strip() == '0', remaining.stdout
    return db


def _story_id(url):
    return 'story:' + hashlib.sha256(url.encode('utf-8')).hexdigest()


def _row(url, *, language='zh', title='中国出入境新规9.15上路 台湾陆委会示警5类人有风险',
         published_at='2026-09-16T10:00:00Z', source_id='rfi-zh', categories=('world',)):
    return {
        'story_id': _story_id(url), 'origin_class': 'public_outlet', 'source_kind': 'outlet',
        'canonical_url': url, 'title': title, 'summary': '摘要内容。', 'language': language,
        'source_id': source_id, 'source_name': 'RFI Chinese', 'source_is_aggregator': False,
        'published_at': published_at, 'source_observed_at': published_at,
        'category_ids': list(categories),
    }


def _ingest(container, rows, *, check=True):
    return _sql(container, "set role service_role;"
                "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);", check=check)


def _candidates(container, expression):
    result = _sql(container, "set role service_role;"
                  "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                  f"select coalesce(jsonb_agg(value), '[]'::jsonb) from {expression} as rows(value);")
    return json.loads(result.stdout.splitlines()[-1])


def test_two_addresses_for_one_headline_return_one_row(db):
    # The reported shape: rfi-zh served one story at two URLs, so the corpus
    # holds two rows with identical titles and the feed showed the story twice.
    first, second = 'https://www.rfi.fr/cn/a-1?x=1', 'https://www.rfi.fr/cn/a-1'
    _ingest(db, [_row(first, published_at='2026-09-16T09:00:00Z'),
                 _row(second, published_at='2026-09-16T10:00:00Z')])
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    titles = [row['title'] for row in rows]
    assert len(titles) == len(set(titles)), titles
    kept = [row['story_id'] for row in rows if row['title'].startswith('中国出入境新规')]
    # The newer observation is the representative, chosen by the function's own
    # order, never by which page the caller asked for.
    assert kept == [_story_id(second)]


def test_the_collapse_is_case_and_whitespace_folded_but_never_fuzzy(db):
    _ingest(db, [
        _row('https://example.test/fold-a', title='Apple releases iOS 18.6.1',
             language='en', published_at='2026-09-16T08:00:00Z'),
        _row('https://example.test/fold-b', title='APPLE   releases  iOS 18.6.1',
             language='en', published_at='2026-09-16T08:30:00Z'),
        # One digit apart is a DIFFERENT release. Exact-only collapsing keeps it.
        _row('https://example.test/fold-c', title='Apple releases iOS 18.6.2',
             language='en', published_at='2026-09-16T08:45:00Z'),
    ])
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    ids = {row['story_id'] for row in rows}
    assert _story_id('https://example.test/fold-b') in ids
    assert _story_id('https://example.test/fold-a') not in ids
    assert _story_id('https://example.test/fold-c') in ids


def test_the_same_headline_in_two_languages_is_never_collapsed(db):
    shared = 'Same headline, two languages'
    _ingest(db, [
        _row('https://example.test/lang-en', title=shared, language='en',
             published_at='2026-09-16T07:00:00Z'),
        _row('https://example.test/lang-zh', title=shared, language='zh',
             published_at='2026-09-16T07:30:00Z'),
    ])
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    ids = {row['story_id'] for row in rows}
    assert _story_id('https://example.test/lang-en') in ids
    assert _story_id('https://example.test/lang-zh') in ids


def test_a_repeat_outside_the_window_is_kept(db):
    # A recurring headline (a daily briefing) is two real stories, not one.
    title = 'Morning briefing: what happened overnight'
    _ingest(db, [
        _row('https://example.test/repeat-day-1', title=title, language='en',
             published_at='2026-09-10T06:00:00Z'),
        _row('https://example.test/repeat-day-3', title=title, language='en',
             published_at='2026-09-13T06:00:00Z'),
    ])
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    ids = {row['story_id'] for row in rows}
    assert _story_id('https://example.test/repeat-day-1') in ids
    assert _story_id('https://example.test/repeat-day-3') in ids


def _page_boundary_corpus(db):
    _ingest(db, [
        _row('https://www.rfi.fr/cn/a-1?x=1', published_at='2026-09-16T09:00:00Z'),
        _row('https://www.rfi.fr/cn/a-1', published_at='2026-09-16T10:00:00Z'),
        _row('https://example.test/fold-a', title='Apple releases iOS 18.6.1',
             language='en', published_at='2026-09-16T08:00:00Z'),
        _row('https://example.test/fold-b', title='APPLE   releases  iOS 18.6.1',
             language='en', published_at='2026-09-16T08:30:00Z'),
        _row('https://example.test/fold-c', title='Apple releases iOS 18.6.2',
             language='en', published_at='2026-09-16T08:45:00Z'),
        _row('https://example.test/repeat-day-1', title='Morning briefing',
             language='en', published_at='2026-09-10T06:00:00Z'),
        _row('https://example.test/repeat-day-3', title='Morning briefing',
             language='en', published_at='2026-09-13T06:00:00Z'),
    ])


def test_the_representative_does_not_change_with_the_page_boundary(db):
    _page_boundary_corpus(db)
    # A per-page rule would collapse a duplicate on page 1 and then show the
    # loser again on page 2, because page 2's window no longer contains the
    # winner. The rule here is decided against the whole table, so it cannot.
    # The property is about the COLLAPSED rows: a row hidden on page 1 must stay
    # hidden on page 2, where the winner is no longer inside the window. Titles
    # that legitimately repeat (two languages, or a recurring headline outside
    # the window) are two stories and must keep appearing.
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,3)")
    assert len(rows) == 3
    last = rows[-1]
    more = _candidates(db, "public.m2_retained_candidates(null,null,"
                           f"{_quote(last['published_at'])}::timestamptz,{_quote(last['story_id'])},100)")
    assert more
    ids = [row['story_id'] for row in rows + more]
    assert len(ids) == len(set(ids)), ids
    collapsed = {_story_id('https://example.test/fold-a'),
                 _story_id('https://www.rfi.fr/cn/a-1?x=1')}
    assert not (collapsed & set(ids)), sorted(collapsed & set(ids))


def test_a_zero_window_restores_the_uncollapsed_projection(db):
    _ingest(db, [_row('https://www.rfi.fr/cn/a-1?x=1', published_at='2026-09-16T09:00:00Z'),
                 _row('https://www.rfi.fr/cn/a-1', published_at='2026-09-16T10:00:00Z')])
    collapsed = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    assert len(collapsed) == 1
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100,0)")
    assert len(rows) == 2
    titles = [row['title'] for row in rows]
    assert len(titles) != len(set(titles))


def test_the_window_argument_is_validated(db):
    for bad in ('-1', '721'):
        denied = _sql(db, "set role service_role;"
                          f"select public.m2_retained_candidates(null,null,null,null,100,{bad});", check=False)
        assert denied.returncode != 0 and 'invalid dedupe window' in denied.stderr


def test_the_dedupe_key_helper_is_service_role_only(db):
    for role in ('anon', 'authenticated'):
        denied = _sql(db, f"set role {role}; select public.m2_story_dedupe_key('x');", check=False)
        assert denied.returncode != 0 and 'permission denied' in denied.stderr.lower()


def _decide(container, story_id, outcome='exclusive', *, display_language='en'):
    """Record a pairing decision, which is what the exclusive lane reads."""
    return _sql(container, "set role service_role;"
                "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                f"select public.m2_record_exclusivity_decision({_quote(story_id)}, "
                f"{_quote(display_language)}, {_quote(POLICY)}, 'gpt-5-mini', {_quote(outcome)}, null);",
                check=False)


def _exclusive(container, *, limit=100, before=None):
    cursor = (f"{_quote(before['published_at'])}::timestamptz,{_quote(before['story_id'])}"
              if before else "null,null")
    return _candidates(container, "public.m2_retained_candidates_language_exclusive("
                                  f"'en',null,{cursor},{limit},{_quote(POLICY)})")


DUP_NEW = 'https://www.rfi.fr/cn/lane-dup'
DUP_OLD = 'https://www.rfi.fr/cn/lane-dup?v=1'


def _exclusive_lane_corpus(db, *, decide_newer=True):
    """Four zh stories, one of them a duplicated pair, plus two singles so the
    lane has something to page through. `decide_newer=False` gives the NEWER
    twin no exclusivity decision, which is the must-fix-A case: it is not part
    of this lane and must not be able to suppress the twin that is."""
    rows = [
        (DUP_OLD, '仅中文报道的独家新闻', '2026-09-16T09:00:00Z'),
        (DUP_NEW, '仅中文报道的独家新闻', '2026-09-16T10:00:00Z'),
        ('https://www.rfi.fr/cn/lane-single-a', '第二条仅中文报道', '2026-09-16T08:00:00Z'),
        ('https://www.rfi.fr/cn/lane-single-b', '第三条仅中文报道', '2026-09-16T07:00:00Z'),
    ]
    _ingest(db, [_row(url, title=title, published_at=when) for url, title, when in rows])
    for url, _, _ in rows:
        if url == DUP_NEW and not decide_newer:
            continue
        decided = _decide(db, _story_id(url))
        if decided.returncode:
            pytest.skip('exclusivity decision RPC unavailable: ' + decided.stderr.strip()[:200])


def test_the_exclusive_lane_collapses_a_duplicate_to_the_newer_row(db):
    # The reported symptom lives on this surface too: without the same rule the
    # "Only in Chinese press" section shows one story twice.
    _exclusive_lane_corpus(db)
    rows = _exclusive(db)
    ids = [row['story_id'] for row in rows]
    assert _story_id(DUP_NEW) in ids
    assert _story_id(DUP_OLD) not in ids
    titles = [(row['language'], ' '.join(row['title'].lower().split())) for row in rows]
    assert len(titles) == len(set(titles)), titles


def test_a_twin_outside_the_lane_never_suppresses_the_one_inside_it(db):
    # MUST-FIX A, exclusive-lane half. The newer twin has no exclusivity
    # decision, so it is not in this lane at all. When the representative was
    # picked from the whole table it won anyway and deleted the only visible
    # copy, and the story vanished from "Only in Chinese press" entirely.
    _exclusive_lane_corpus(db, decide_newer=False)
    ids = [row['story_id'] for row in _exclusive(db)]
    assert _story_id(DUP_OLD) in ids, 'the decided twin must still be served'
    assert _story_id(DUP_NEW) not in ids, 'the undecided twin is not in this lane'


def test_the_exclusive_lane_representative_survives_the_page_boundary(db):
    _exclusive_lane_corpus(db)
    first = _exclusive(db, limit=2)
    assert len(first) == 2
    rest = _exclusive(db, before=first[-1])
    assert rest
    ids = [row['story_id'] for row in first + rest]
    assert len(ids) == len(set(ids)), ids
    assert _story_id(DUP_OLD) not in ids


def test_a_zero_window_restores_duplicates_in_the_exclusive_lane(db):
    _exclusive_lane_corpus(db)
    rows = _candidates(db, "public.m2_retained_candidates_language_exclusive("
                           f"'en',null,null,null,100,{_quote(POLICY)},0)")
    ids = [row['story_id'] for row in rows]
    assert _story_id(DUP_OLD) in ids
    assert _story_id(DUP_NEW) in ids


def test_the_exclusive_lane_validates_its_window(db):
    denied = _sql(db, "set role service_role;"
                  "select public.m2_retained_candidates_language_exclusive("
                  f"'en',null,null,null,100,{_quote(POLICY)},-1);", check=False)
    assert denied.returncode != 0 and 'invalid dedupe window' in denied.stderr


def test_a_twin_in_another_category_never_suppresses_the_one_being_filtered(db):
    # MUST-FIX A, category half. Two observations of one headline, each filed
    # under a different category. Filtering to the loser's category used to
    # return NOTHING, because the winner was chosen against the whole table and
    # is not in the filtered set.
    older, newer = 'https://example.test/cat-old', 'https://example.test/cat-new'
    _ingest(db, [
        _row(older, title='One headline, two sections', language='en',
             published_at='2026-09-16T09:00:00Z', categories=('world',)),
        _row(newer, title='One headline, two sections', language='en',
             published_at='2026-09-16T10:00:00Z', categories=('business',)),
    ])
    in_world = [row['story_id'] for row in
                _candidates(db, f"public.m2_retained_candidates({_quote('world')},null,null,null,100)")]
    in_business = [row['story_id'] for row in
                   _candidates(db, f"public.m2_retained_candidates({_quote('business')},null,null,null,100)")]
    assert in_world == [_story_id(older)], in_world
    assert in_business == [_story_id(newer)], in_business
    # Unfiltered, they are still one story: the newer one.
    unfiltered = [row['story_id'] for row in
                  _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")]
    assert unfiltered == [_story_id(newer)], unfiltered


def test_a_twin_outside_the_search_result_never_suppresses_the_one_inside_it(db):
    # Same defect through the query filter: the summaries differ, so only one
    # twin matches the search, and it must still be returned.
    older, newer = 'https://example.test/q-old', 'https://example.test/q-new'
    _ingest(db, [
        {**_row(older, title='One headline, two summaries', language='en',
                published_at='2026-09-16T09:00:00Z'), 'summary': 'mentions peregrine falcons'},
        {**_row(newer, title='One headline, two summaries', language='en',
                published_at='2026-09-16T10:00:00Z'), 'summary': 'mentions nothing of the sort'},
    ])
    hits = [row['story_id'] for row in
            _candidates(db, f"public.m2_retained_candidates(null,{_quote('peregrine')},null,null,100)")]
    assert hits == [_story_id(older)], hits


def test_the_real_capture_ingests_with_no_uncategorised_row(db):
    # B8 bug 1, end to end: the shipped config, the real retain() floor, the
    # real ingest RPC, and the invariant asserted in SQL rather than in Python.
    capture = json.loads((ROOT / 'tests/fixtures/m2-retained-public.json').read_text())
    config = load_config(ROOT)
    allowed = {row['source_id'] for row in capture['rows']}
    items = []
    for raw in capture['rows'][:120]:
        items.append(Item(title=raw['title'], description=raw['summary'], url=raw['canonical_url'],
                          canonical_url=raw['canonical_url'], source_id=raw['source_id'],
                          source_name=raw['source_name'], language=raw['language'],
                          published_at=datetime.fromisoformat(raw['published_at'].replace('Z', '+00:00'))))
    retained = retain(items, categories=config.categories, observed_at=NOW,
                      fallback_category_for=config.fallback_category_for)
    payload = public_ingest_rows(retained, allowed_source_ids=allowed)
    assert payload
    assert _ingest(db, payload).returncode == 0
    ingested = ','.join(_quote(row['story_id']) for row in payload)
    orphans = _sql(db, "select count(*) from public.retained_corpus_observations o "
                       f"where o.story_id in ({ingested}) "
                       "and not exists (select 1 from public.retained_corpus_categories c "
                       "where c.story_id = o.story_id);")
    assert orphans.stdout.strip() == '0', orphans.stdout
    # The measurement this fixes: the SAME rows without the floor leave orphans.
    unfloored = public_ingest_rows(
        retain(items, categories=config.categories, observed_at=NOW), allowed_source_ids=allowed)
    assert [row for row in unfloored if not row['category_ids']]
