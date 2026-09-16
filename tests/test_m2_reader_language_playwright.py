"""W10a/W10b: the language toggle, translated cards and the other-language section.

Rendered reader -> ASGI -> RankingService, with a local store. No provider, no
network beyond the routed test origins.
"""
from __future__ import annotations
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from curator.models import Item
from curator.render import render_site
from curator.recommendation.service import RankingService, ServicePolicy
from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
from curator.recommendation.asgi import RankingASGI
from scripts.build_auth_callback import activate_personalization_link

playwright = pytest.importorskip('playwright.sync_api')
from tests.test_m2_reader_playwright import LocalAuth, NoProvider, asgi_request  # noqa: E402

READER = 'https://reader.example'
RANKER = 'https://ranker.example'
DATABASE = 'https://project-ref.supabase.co'
OWNER = '00000000-0000-0000-0000-000000000001'
EXCLUSIVE = 'only-other-language-press'
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
LANGUAGE_POLICY = {'default_display': 'en', 'other_lane_enabled': True, 'exclusive_category_id': EXCLUSIVE}
# A hidden card still answers innerText, so every count and every headline read
# in this test goes through offsetParent, which only a painted node has.
VISIBLE_CARDS = ("() => [...document.querySelectorAll('[data-m2-card=true]')]"
                 ".filter((card) => card.offsetParent !== null).length")


def visible_headlines(page):
    return page.evaluate("() => [...document.querySelectorAll('[data-m2-card=true]')]"
                         ".filter((card) => card.offsetParent !== null)"
                         ".map((card) => card.querySelector('.headline').textContent)")


def row(index, language, title, summary, *, translations=None, group=None):
    return {'schema_version': 1, 'story_id': f'story:{index:064x}', 'title': title, 'summary': summary,
            'language': language, 'canonical_url': f'https://example.test/{index}',
            'source_id': 'fixture', 'source_name': 'Fixture Wire',
            'published_at': (NOW - timedelta(minutes=index)).isoformat().replace('+00:00', 'Z'),
            'source_observed_at': NOW.isoformat().replace('+00:00', 'Z'),
            'first_ingested_at': NOW.isoformat().replace('+00:00', 'Z'),
            'last_ingested_at': NOW.isoformat().replace('+00:00', 'Z'),
            'first_ready_at': NOW.isoformat().replace('+00:00', 'Z'),
            'last_ready_at': NOW.isoformat().replace('+00:00', 'Z'),
            'category_ids': ['world'], 'event_group_id': group,
            'title_translations': (translations or {}).get('title', {}),
            'summary_translations': (translations or {}).get('summary', {})}


FIXTURE_ROWS = [
    row(1, 'zh', '中文独家报道：某部门发布七项新规', '中文摘要内容。',
        translations={'title': {'en': 'Chinese exclusive: seven new rules published'},
                      'summary': {'en': 'An English summary of the Chinese exclusive.'}}),
    row(2, 'zh', '中文独家报道：未翻译的第二条', '第二条中文摘要。'),
    row(3, 'en', 'An English wire story every reader can already read', 'English summary text.'),
]


class LanguageStore:
    def __init__(self, rows):
        self.rows = rows
        self.frozen = {}
        self.events = []
        self.exclusive_calls = []

    def history_snapshot(self, token):
        revision = len(self.events)
        return {'history_revision': revision, 'server_commit_revision': revision,
                'included_history_revision': revision, 'history_generation': 1, 'consent_revision': 1,
                'learning_enabled': True, 'provider_processing_enabled': False,
                'provider_policy_id': None, 'newest_event_id': None, 'events': []}

    def retained_candidates(self, *, category_id, query, limit, **kwargs):
        rows = [r for r in self.rows if not category_id or category_id in r['category_ids']]
        return rows[:limit]

    def retained_candidates_language_exclusive(self, *, display_language, query, limit, **kwargs):
        self.exclusive_calls.append(display_language)
        covered = {r['event_group_id'] for r in self.rows
                   if r['event_group_id'] and r['language'] == display_language}
        rows = [r for r in self.rows if r['language'] != display_language
                and (r['event_group_id'] is None or r['event_group_id'] not in covered)]
        return rows[:limit]

    def owner_states(self, token, story_ids):
        return {sid: {'read_at': None, 'saved_at': None, 'state_revision': 0, 'interests': []} for sid in story_ids}

    def reserve_budget(self, **kwargs):
        raise AssertionError('Unpriced local fallback must not reserve provider spend')

    def settle_budget(self, **kwargs):
        raise AssertionError('Provider must not run')

    def save_frozen_order(self, **kwargs):
        key = str(len(self.frozen) + 1)
        self.frozen[key] = copy.deepcopy(kwargs)
        return key

    def load_frozen_order(self, *, user_id, frozen_order_id):
        return copy.deepcopy(self.frozen.get(frozen_order_id))


def build_site(tmp_path):
    ranked = {'world': [Item(title=FIXTURE_ROWS[2]['title'], url=FIXTURE_ROWS[2]['canonical_url'],
                             canonical_url=FIXTURE_ROWS[2]['canonical_url'],
                             published_at=datetime.fromisoformat(FIXTURE_ROWS[2]['published_at'].replace('Z', '+00:00')),
                             source_id='fixture', source_name='Fixture Wire', language='en',
                             description=FIXTURE_ROWS[2]['summary'])]}
    site = tmp_path / 'site'
    render_site(ranked, [], NOW, site, topic_ids_by_name={'world': 'world'}, require_summaries=False,
                discovery_enabled=True, language_policy=LANGUAGE_POLICY)
    activate_personalization_link(site / 'index.html', supabase_url=DATABASE,
        publishable_key='sb_publishable_localtest',
        m2_config={'enabled': True, 'url': RANKER, 'policy_version': 'test-policy',
                   'model_version': 'test-model', 'provider_policy_id': 'test-policy',
                   'provider_retention_url': 'https://policy.example', 'page_size': 25,
                   'request_timeout_ms': 8000, 'transport_timeout_ms': 20000})
    return site


def _run_reader(tmp_path, rows, steps):
    """Boot the rendered reader against a local store and run `steps(page, ctx)`."""
    site = build_site(tmp_path)
    store = LanguageStore(rows)
    service = RankingService(auth=LocalAuth(), store=store, adapter=RankLLMAdapter(
        policy=RankerPolicy('test-provider', 'test-model', 'https://provider.example', 'test-prompt'),
        engine=NoProvider()),
        policy=ServicePolicy('test-policy', 'test-model', 'test-policy', 'test-tenant', enabled=True,
                             display_language='en', exclusive_category_id=EXCLUSIVE, other_lane_enabled=True),
        cursor_key=b'k' * 32)
    app = RankingASGI(service=service, reader_origin=READER)
    ranker_requests = []
    page_errors = []

    def route_handler(route):
        request = route.request
        parsed = urlsplit(request.url)
        body = request.post_data_json if request.post_data else {}
        if request.url.startswith(READER):
            if parsed.path == '/auth/client.js':
                script = ('window.__localSession={access_token:"local-auth-token",user_id:"' + OWNER + '"};'
                          'window.NewsCuratorAuth={config:()=>({url:"' + DATABASE + '",key:"sb_publishable_localtest"}),'
                          'sessionForRequest:async()=>window.__localSession,hasSessionCandidate:()=>!!window.__localSession,'
                          'isConfirmed:()=>true,confirmSession:()=>{},clearSession:()=>{window.__localSession=null;},'
                          'channelName:"m2-local-test"};')
                return route.fulfill(status=200, content_type='text/javascript', body=script)
            file = site / (parsed.path.lstrip('/') or 'index.html')
            return route.fulfill(status=200,
                content_type='text/javascript' if file.suffix == '.js' else 'text/html', body=file.read_bytes())
        if request.url.startswith(RANKER):
            ranker_requests.append(parsed.path)
            status, payload = asgi_request(app, request)
            return route.fulfill(status=status, content_type='application/json', body=payload)
        if request.url.startswith(DATABASE):
            name = parsed.path.rsplit('/', 1)[-1]
            if name == 'latest_publication':
                payload = {'publication_seq': 1, 'finalized_at': NOW.isoformat().replace('+00:00', 'Z'),
                           'initial_history_cursor': None, 'page_size': 20, 'poll_seconds': 60,
                           'topics': [{'topic_id': 'world', 'name': 'world'}]}
            elif name == 'm2_history_snapshot':
                payload = store.history_snapshot('local-auth-token')
            elif name in ('feed_page', 'saved_page'):
                payload = []
            elif name == 'discovery_edition':
                payload = {'schema_version': 1, 'status': 'unavailable', 'reason_code': 'no_private_edition', 'edition': None}
            elif name == 'append_behavior_event':
                payload = {'status': 'recorded', 'event_id': body.get('p_event_id'), 'event_revision': 1}
            else:
                raise AssertionError(name)
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
        route.abort()

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, args=['--mute-audio'])
        context = browser.new_context(viewport={'width': 390, 'height': 844})
        context.route('**/*', route_handler)
        page = context.new_page()
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        try:
            page.goto(READER, wait_until='networkidle')
            page.wait_for_function(VISIBLE_CARDS + " === 3")

            # (a) The toggle exists without being told where to look.
            toggle = page.locator('#m2-language-toggle')
            assert toggle.is_visible() and toggle.inner_text() == '中文'

            # (b) A zh-exclusive story reads in English while the site is English.
            # innerText on a display:none node still returns its text, so every
            # assertion here is about what is actually PAINTED.
            texts = visible_headlines(page)
            assert len(texts) == 3, texts
            assert 'Chinese exclusive: seven new rules published' in texts

            # (d) The untranslated zh story is still shown, and says so.
            assert '中文独家报道：未翻译的第二条' in texts
            marks = page.locator('.translation-mark')
            assert marks.count() == 1 and marks.first.is_visible()
            assert 'Not translated' in marks.first.inner_text()

            # (c) The section exists in the rail and the phone strip, derived.
            chips = page.locator('.chip[data-language-exclusive=true]')
            assert chips.count() == 2
            assert chips.first.inner_text() == 'Only in Chinese press'

            before_requests = len(ranker_requests)
            toggle.click()
            page.wait_for_function("() => document.getElementById('m2-language-toggle').innerText === 'EN'")
            # Switching is instant and free: no new ranking request.
            assert len(ranker_requests) == before_requests
            # The toggle must not blank the page. Every card stays PAINTED, not
            # merely present in the DOM with display:none.
            page.wait_for_function(VISIBLE_CARDS + " === 3")
            assert page.locator('[data-m2-card=true]').first.is_visible()
            assert page.locator('[data-m2-card=true]').first.bounding_box() is not None
            # The title is derived: reading in Chinese renames it to the mirror.
            assert page.locator('.chip[data-language-exclusive=true]').first.inner_text() == '只有英文媒体报道'
            after_texts = visible_headlines(page)
            assert len(after_texts) == 3, after_texts
            # Chinese display: Chinese cards read as written, English is untranslated.
            assert '中文独家报道：某部门发布七项新规' in after_texts
            assert 'An English wire story every reader can already read' in after_texts

            toggle.click()
            page.wait_for_function("() => document.getElementById('m2-language-toggle').innerText === '中文'")
            # Toggling twice returns to the start with the same visible count.
            page.wait_for_function(VISIBLE_CARDS + " === 3")
            assert visible_headlines(page) == texts

            # Opening the section serves only language-exclusive stories.
            page.locator('.chip[data-language-exclusive=true]').nth(1).click()
            page.wait_for_function(VISIBLE_CARDS + " === 2")
            assert store.exclusive_calls == ['en']
            section_title = page.locator('#sections [data-section="__m2__"] .section-title').first
            assert section_title.inner_text() == 'Only in Chinese press'
            assert page.locator('.m2-empty').count() == 0
        finally:
            context.close()
            browser.close()
    assert page_errors == []


def test_the_empty_exclusive_section_shows_exactly_one_message(tmp_path):
    """Two empty states on one screen contradict each other. Only one may paint."""
    site = build_site(tmp_path)
    # A corpus with no language-exclusive story at all.
    store = LanguageStore([FIXTURE_ROWS[2]])
    service = RankingService(auth=LocalAuth(), store=store, adapter=RankLLMAdapter(
        policy=RankerPolicy('test-provider', 'test-model', 'https://provider.example', 'test-prompt'),
        engine=NoProvider()),
        policy=ServicePolicy('test-policy', 'test-model', 'test-policy', 'test-tenant', enabled=True,
                             display_language='en', exclusive_category_id=EXCLUSIVE, other_lane_enabled=True),
        cursor_key=b'k' * 32)
    app = RankingASGI(service=service, reader_origin=READER)
    page_errors = []

    def route_handler(route):
        request = route.request
        parsed = urlsplit(request.url)
        body = request.post_data_json if request.post_data else {}
        if request.url.startswith(READER):
            if parsed.path == '/auth/client.js':
                script = ('window.__localSession={access_token:"local-auth-token",user_id:"' + OWNER + '"};'
                          'window.NewsCuratorAuth={config:()=>({url:"' + DATABASE + '",key:"sb_publishable_localtest"}),'
                          'sessionForRequest:async()=>window.__localSession,hasSessionCandidate:()=>!!window.__localSession,'
                          'isConfirmed:()=>true,confirmSession:()=>{},clearSession:()=>{window.__localSession=null;},'
                          'channelName:"m2-local-test"};')
                return route.fulfill(status=200, content_type='text/javascript', body=script)
            file = site / (parsed.path.lstrip('/') or 'index.html')
            return route.fulfill(status=200,
                content_type='text/javascript' if file.suffix == '.js' else 'text/html', body=file.read_bytes())
        if request.url.startswith(RANKER):
            status, payload = asgi_request(app, request)
            return route.fulfill(status=status, content_type='application/json', body=payload)
        if request.url.startswith(DATABASE):
            name = parsed.path.rsplit('/', 1)[-1]
            if name == 'latest_publication':
                payload = {'publication_seq': 1, 'finalized_at': NOW.isoformat().replace('+00:00', 'Z'),
                           'initial_history_cursor': None, 'page_size': 20, 'poll_seconds': 60,
                           'topics': [{'topic_id': 'world', 'name': 'world'}]}
            elif name == 'm2_history_snapshot':
                payload = store.history_snapshot('local-auth-token')
            elif name in ('feed_page', 'saved_page'):
                payload = []
            elif name == 'discovery_edition':
                payload = {'schema_version': 1, 'status': 'unavailable', 'reason_code': 'no_private_edition', 'edition': None}
            elif name == 'append_behavior_event':
                payload = {'status': 'recorded', 'event_id': body.get('p_event_id'), 'event_revision': 1}
            else:
                raise AssertionError(name)
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
        route.abort()

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, args=['--mute-audio'])
        context = browser.new_context(viewport={'width': 390, 'height': 844})
        context.route('**/*', route_handler)
        page = context.new_page()
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        try:
            page.goto(READER, wait_until='networkidle')
            page.wait_for_function(VISIBLE_CARDS + " === 1")
            page.locator('.chip[data-language-exclusive=true]').nth(1).click()
            page.wait_for_function(VISIBLE_CARDS + " === 0")
            painted = page.evaluate(
                "() => [...document.querySelectorAll('.m2-empty, #empty')]"
                ".filter((node) => node.offsetParent !== null).map((node) => node.textContent.trim())")
            assert len(painted) == 1, painted
            assert 'No stories that only the Chinese press carried today' in painted[0]
        finally:
            context.close()
            browser.close()
    assert page_errors == []
