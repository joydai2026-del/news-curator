"""W9: translation at retained-corpus ingest. No network, stub provider only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from curator.models import Item
from curator.translation import InMemoryTranslationStore, TranslationErrorReason, TranslationProviderError
from curator.translation.base import TranslationProviderResult, TranslationResultItem
from curator.translation.ingest import (
    IngestTranslationPolicy,
    TRANSLATED,
    UNTRANSLATED,
    translate_exclusive_stories,
)
from curator.translation.model_provider import ModelTranslationAdapter, ModelTranslationConfig

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def zh_item(title="英伟达发布 3 款芯片", summary="英伟达周二公布财报。"):
    return Item(title=title, url="https://example.cn/a", canonical_url="https://example.cn/a",
                source_id="cnbeta", source_name="cnBeta", published_at=NOW, language="zh", description=summary)


class StubProvider:
    provider_id = "openai"
    model_version = "gpt-5-mini:translation-json-v1"

    def __init__(self, behavior="ok"):
        self.behavior, self.calls = behavior, 0
        self.input_tokens = self.output_tokens = 0

    def translate(self, request):
        self.calls += 1
        if self.behavior == "error":
            raise TranslationProviderError(self.provider_id, TranslationErrorReason.PROVIDER_REJECTED)
        item = request.items[0]
        return TranslationProviderResult(
            items=(TranslationResultItem(request_id=item.request_id, title="Nvidia ships 3 chips",
                                         description="Nvidia reported earnings on Tuesday."),),
            source_language=request.source_language, target_language=request.target_language,
            provider=self.provider_id, model_version=self.model_version,
            input_tokens=self.input_tokens, output_tokens=self.output_tokens)


def policy(**kwargs):
    base = dict(enabled=True, provider="openai", display_language="en")
    base.update(kwargs)
    return IngestTranslationPolicy(**base)


def store():
    return InMemoryTranslationStore(clock=lambda: NOW)


def test_a_zh_exclusive_story_is_translated_into_the_display_language():
    provider = StubProvider()
    result = translate_exclusive_stories([("story:a", zh_item())], policy=policy(), store=store(),
                                         provider=provider, run_id="run-1", now=NOW)
    overlay = result.overlays["story:a"]
    assert overlay.status == TRANSLATED
    assert overlay.title_translations == {"en": "Nvidia ships 3 chips"}
    assert overlay.summary_translations == {"en": "Nvidia reported earnings on Tuesday."}
    assert result.counters["translated"] == 1


def test_the_same_content_is_never_translated_twice():
    provider, shared = StubProvider(), store()
    first = translate_exclusive_stories([("story:a", zh_item())], policy=policy(), store=shared,
                                        provider=provider, run_id="run-1", now=NOW)
    second = translate_exclusive_stories([("story:a", zh_item())], policy=policy(), store=shared,
                                         provider=provider, run_id="run-2", now=NOW)
    assert provider.calls == 1
    assert second.counters.get("cache_hit") == 1
    assert second.overlays["story:a"].title_translations == first.overlays["story:a"].title_translations


def test_a_provider_failure_still_returns_the_story_marked_untranslated():
    result = translate_exclusive_stories([("story:a", zh_item())], policy=policy(), store=store(),
                                         provider=StubProvider("error"), run_id="run-1", now=NOW)
    overlay = result.overlays["story:a"]
    assert overlay.status == UNTRANSLATED and overlay.title_translations == {}
    assert overlay.reason == "provider_rejected"
    assert result.untranslated_shown == 1


def test_the_dollar_cap_refuses_the_send_and_still_returns_every_story():
    """The cap is checked BEFORE the provider is entered, and it binds."""
    provider = StubProvider()
    tiny = policy(daily_cost_limit_usd=0.0)
    result = translate_exclusive_stories([("story:a", zh_item())], policy=tiny, store=store(),
                                         provider=provider, run_id="run-1", now=NOW)
    assert provider.calls == 0
    assert result.overlays["story:a"].status == UNTRANSLATED
    assert result.overlays["story:a"].reason == "cost_limit_reached"


def test_a_failure_after_the_send_still_consumes_budget():
    """A provider that fails after mark_sent may still have charged. The
    reservation is retained, so a broken provider cannot spend without limit."""
    one_attempt = policy(daily_cost_limit_usd=policy().reservation_usd(len("英伟达发布 3 款芯片") + len("英伟达周二公布财报。")) * 1.5)
    broken = StubProvider("error")
    result = translate_exclusive_stories(
        [("story:a", zh_item()), ("story:b", zh_item(title="独家：第二条", summary="第二条摘要。"))],
        policy=one_attempt, store=store(), provider=broken, run_id="run-1", now=NOW)
    assert broken.calls == 1, "the second send is refused by the retained reservation"
    assert result.counters["retained_usd_millionths"] > 0
    assert result.counters["settled_usd_millionths"] == 0
    assert result.overlays["story:a"].status == UNTRANSLATED
    assert result.overlays["story:b"].reason == "cost_limit_reached"
    assert len(result.overlays) == 2, "no story is dropped for a budget or provider problem"


def test_spend_is_settled_from_the_provider_reported_usage():
    provider = StubProvider()
    provider.input_tokens, provider.output_tokens = 1_000, 500
    active = policy(input_cost_per_million_tokens_usd=0.25, output_cost_per_million_tokens_usd=2.0)
    result = translate_exclusive_stories([("story:a", zh_item())], policy=active, store=store(),
                                         provider=provider, run_id="run-1", now=NOW)
    expected = round((1_000 * 0.25 + 500 * 2.0) / 1_000_000 * 1_000_000)
    assert result.counters["settled_usd_millionths"] == expected
    assert result.counters["retained_usd_millionths"] == 0


def test_a_provider_that_reports_no_usage_settles_at_the_reservation_not_at_zero():
    result = translate_exclusive_stories([("story:a", zh_item())], policy=policy(), store=store(),
                                         provider=StubProvider(), run_id="run-1", now=NOW)
    assert result.counters["settled_usd_millionths"] > 0


def test_the_same_content_hash_costs_once():
    provider, shared = StubProvider(), store()
    first = translate_exclusive_stories([("story:a", zh_item())], policy=policy(), store=shared,
                                        provider=provider, run_id="run-1", now=NOW)
    second = translate_exclusive_stories([("story:a", zh_item())], policy=policy(), store=shared,
                                         provider=provider, run_id="run-2", now=NOW)
    assert provider.calls == 1
    assert first.counters["settled_usd_millionths"] > 0
    assert second.counters["settled_usd_millionths"] == 0, "a cache hit is free"


def test_an_expired_cache_entry_is_refetched_at_ingest():
    provider, shared = StubProvider(), store()
    translate_exclusive_stories([("story:a", zh_item())], policy=policy(), store=shared,
                                provider=provider, run_id="run-1", now=NOW)
    later = NOW + timedelta(days=31)
    result = translate_exclusive_stories([("story:a", zh_item())], policy=policy(cache_ttl_days=30), store=shared,
                                         provider=provider, run_id="run-2", now=later)
    assert result.counters.get("cache_expired") == 1
    assert result.overlays["story:a"].status == TRANSLATED


def test_a_story_already_in_the_display_language_is_not_translated():
    english = Item(title="Nvidia ships 3 chips", url="https://example.com/a", canonical_url="https://example.com/a",
                   source_id="reuters", source_name="Reuters", published_at=NOW, language="en", description="Text.")
    provider = StubProvider()
    result = translate_exclusive_stories([("story:a", english)], policy=policy(), store=store(),
                                         provider=provider, run_id="run-1", now=NOW)
    assert result.overlays == {} and provider.calls == 0


def test_translation_is_a_no_op_while_the_feature_is_switched_off():
    provider = StubProvider()
    result = translate_exclusive_stories([("story:a", zh_item())], policy=policy(enabled=False), store=store(),
                                         provider=provider, run_id="run-1", now=NOW)
    assert result.overlays == {} and provider.calls == 0 and result.counters == {"disabled": 1}


@pytest.mark.parametrize("kwargs", [
    {"on_failure": "drop"},
    {"display_language": "fr"},
    {"cache_ttl_days": 0},
    {"cache_ttl_days": 366},
    {"daily_cost_limit_usd": 25.1},
    {"daily_cost_limit_usd": -0.1},
    {"input_cost_per_million_tokens_usd": -1},
    {"output_cost_per_million_tokens_usd": 1001},
    {"characters_per_token": 0},
    {"max_output_tokens_per_story": 0},
    {"enabled": "true"},
])
def test_policy_refuses_out_of_range_values(kwargs):
    with pytest.raises(ValueError):
        policy(**kwargs)


class StubTransport:
    def __init__(self, body, status=200):
        self.body, self.status = body, status
        self.requests = []

    def request(self, source_id, method, url, *, headers=None, body=None, credential=None, allowed_mime_types=()):
        self.requests.append((url, body, credential))

        class Response:
            status_code = self.status
            body = self.body
        return Response()


def adapter(body, status=200):
    transport = StubTransport(body if isinstance(body, bytes) else json.dumps(body).encode(), status)
    return ModelTranslationAdapter(
        config=ModelTranslationConfig(provider_id="openai", model="gpt-5-mini"),
        transport=transport, api_key=lambda: "test-key"), transport


def chat_reply(content):
    return {"choices": [{"message": {"content": content}}]}


def request_for(item):
    from curator.translation.base import TranslationInput, TranslationProviderRequest, TranslationRequestItem
    content = TranslationInput.from_item(item)
    return TranslationProviderRequest(items=(TranslationRequestItem(request_id="t-" + content.digest[:32], content=content),),
                                      source_language="zh", target_language="en")


def test_model_adapter_returns_the_json_fields_and_never_hardcodes_the_model():
    provider, transport = adapter(chat_reply(json.dumps({"title": "Nvidia ships 3 chips", "summary": "Earnings."})))
    result = provider.translate(request_for(zh_item(summary="财报。")))
    assert result.items[0].title == "Nvidia ships 3 chips"
    assert result.provider == "openai" and result.model_version.startswith("gpt-5-mini:")
    sent = json.loads(transport.requests[0][1])
    assert sent["model"] == "gpt-5-mini" and sent["response_format"] == {"type": "json_object"}
    assert "preserve" in sent["messages"][0]["content"].lower() or "Preserve" in sent["messages"][0]["content"]


@pytest.mark.parametrize("content", [
    "not json at all",
    json.dumps({"title": "only a title"}),
    json.dumps({"title": "t", "summary": "s", "extra": "x"}),
    json.dumps({"title": "", "summary": "s"}),
])
def test_model_adapter_rejects_any_reply_that_is_not_the_exact_json_contract(content):
    provider, _ = adapter(chat_reply(content))
    with pytest.raises(TranslationProviderError):
        provider.translate(request_for(zh_item(summary="财报。")))


def test_model_adapter_rejects_an_over_length_translation():
    huge = json.dumps({"title": "x" * 3000, "summary": "s"})
    provider, _ = adapter(chat_reply(huge))
    with pytest.raises(TranslationProviderError) as error:
        provider.translate(request_for(zh_item(summary="财报。")))
    assert error.value.reason is TranslationErrorReason.RESPONSE_TOO_LARGE


class FakePersistedLedger:
    """Stands in for the SQL day counter shared by every run on one UTC day."""

    def __init__(self, limit_usd):
        self.limit, self.reserved, self.settled, self.calls = limit_usd, 0.0, 0.0, 0

    def reserve(self, amount_usd):
        self.calls += 1
        if self.settled + self.reserved + amount_usd > self.limit:
            return False
        self.reserved += amount_usd
        return True

    def settle(self, reserved_usd, settled_usd):
        self.reserved = max(0.0, self.reserved - reserved_usd)
        self.settled += settled_usd


def test_two_runs_on_one_utc_day_share_the_persisted_cap():
    """An in-memory ledger resets every run; the shared one does not."""
    shared = FakePersistedLedger(limit_usd=policy().reservation_usd(40) * 1.5)
    first = translate_exclusive_stories([("story:a", zh_item())], policy=policy(), store=store(),
                                        provider=StubProvider(), run_id="run-1", now=NOW,
                                        spend_ledger=shared)
    assert first.overlays["story:a"].status == TRANSLATED
    # Run two, same day, different store so the cache cannot mask the cap.
    second = translate_exclusive_stories([("story:b", zh_item(title="独家：第二条", summary="第二条摘要。"))],
                                         policy=policy(), store=store(), provider=StubProvider(),
                                         run_id="run-2", now=NOW, spend_ledger=shared)
    assert second.overlays["story:b"].status == UNTRANSLATED
    assert second.overlays["story:b"].reason == "cost_limit_reached"
    assert shared.calls == 2


def test_the_persisted_ledger_is_the_authority_over_the_run_local_numbers():
    shared = FakePersistedLedger(limit_usd=0.0)
    provider = StubProvider()
    result = translate_exclusive_stories([("story:a", zh_item())], policy=policy(daily_cost_limit_usd=25.0),
                                         store=store(), provider=provider, run_id="run-1", now=NOW,
                                         spend_ledger=shared)
    assert provider.calls == 0, "a generous run-local limit cannot override the day's ledger"
    assert result.overlays["story:a"].reason == "cost_limit_reached"
