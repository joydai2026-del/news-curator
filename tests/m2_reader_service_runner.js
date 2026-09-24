"use strict";
const assert = require("node:assert/strict");
const reader = require("../static/reader.js");

const storyId = `story:${"a".repeat(64)}`;
const history = { history_revision: 8, included_history_revision: 6,
  history_generation: 2, consent_revision: 3 };
const config = { enabled: true, url: "https://rank.example",
  policy_version: "policy-2", model_version: "model-config-4", page_size: 20,
  provider_policy_id: "provider-policy-3",
  provider_retention_url: "https://policy.example/retention" };
function payload(overrides = {}) { return { schema_version: 1, request_id: "request-1",
  policy_version: config.policy_version, model_version: config.model_version,
  history_revision: 6, server_commit_revision: 8, history_generation: 2, consent_revision: 3,
  result_mode: "fallback", fallback_reason: "model_timeout", cards: [{ story_id: storyId,
    card_schema_version: 2,
    source_id: "ars", language: "en", category_ids: ["ai"], read_at: null,
    saved_at: null, state_revision: 0, interests: [],
    title: "Six Chinese AI firms accused of aggressively copying US frontier models",
    summary: "US urges AI firms to ID, then secretly switch, Chinese users to less-capable models.",
    source_name: "Ars Technica", published_at: "2026-09-09T20:06:28Z",
    title_en: "Six Chinese AI firms accused of aggressively copying US frontier models",
    summary_en: "US urges AI firms to ID, then secretly switch, Chinese users to less-capable models.",
    title_zh: "", summary_zh: "",
    translation_status: { en: "original", zh: "untranslated" },
    url: "https://arstechnica.com/tech-policy/2026/09/example" }], next_cursor: "opaque", ...overrides }; }
function response(value, url) { return { ok: true, redirected: false, url,
  text: async () => JSON.stringify(value) }; }

(async () => {
  assert.deepEqual(reader.validateM2Config({ enabled: false }), { enabled: false });
  assert.equal(reader.validateM2Config({ ...config, request_timeout_ms: 8000 }).request_timeout_ms, 8000);
  assert.equal(reader.validateM2Config({ ...config, request_timeout_ms: 8000 }).transport_timeout_ms, 310000);
  assert.equal(reader.validateM2Config({ ...config, request_timeout_ms: 8000, transport_timeout_ms: 310000 }).transport_timeout_ms, 310000);
  assert.throws(() => reader.validateM2Config({ ...config, request_timeout_ms: 8001 }), /configuration/);
  assert.throws(() => reader.validateM2Config({ ...config, request_timeout_ms: 310000 }), /configuration/);
  assert.throws(() => reader.validateM2Config({ ...config, request_timeout_ms: 8000, transport_timeout_ms: 309999 }), /configuration/);
  assert.throws(() => reader.validateM2Config({ ...config, request_timeout_ms: 8000, transport_timeout_ms: 600001 }), /configuration/);
  let token = "token-a";
  const calls = [];
  const service = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url, options) => { calls.push({ url, options }); return response(payload(), url); });
  const ranked = await service.rank(history, { as_of: "2026-09-14T16:00:00Z", language: "en" });
  assert.equal(ranked.result_mode, "fallback");
  assert.equal(ranked.fallback_reason, "model_timeout");
  assert.equal(JSON.parse(calls[0].options.body).server_commit_revision, 8);
  assert.equal(calls[0].options.signal instanceof AbortSignal, true);
  assert.equal(calls[0].options.headers.authorization, "Bearer token-a");
  assert.equal(calls[0].options.headers["x-news-curator-order-origin"], "1");
  assert.equal(JSON.stringify(calls[0]).includes("user_id"), false);

  const changed = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => { token = "token-b"; return response(payload(), url); });
  await assert.rejects(() => changed.rank(history, { as_of: "2026-09-14T16:00:00Z" }), /account changed/);
  token = "token-a";
  const validityRequests = [];
  let rotatingToken = "token-a";
  const rotatedSameOwner = reader.createM2Service(config, async (minimumValiditySeconds) => {
    validityRequests.push(minimumValiditySeconds);
    return { access_token: rotatingToken, user_id: "owner-a" };
  }, async (url) => { rotatingToken = "token-b"; return response(payload(), url); });
  await rotatedSameOwner.rank(history, { as_of: "2026-09-14T16:00:00Z" });
  assert.deepEqual(validityRequests, [340, 0]);
  let activeOwner = "owner-a";
  const switchedOwner = reader.createM2Service(config,
    async () => ({ access_token: "token-a", user_id: activeOwner }),
    async (url) => { activeOwner = "owner-b"; return response(payload(), url); });
  await assert.rejects(() => switchedOwner.rank(history, { as_of: "2026-09-14T16:00:00Z" }),
    /account changed/);
  const stale = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => response(payload({ history_generation: 1 }), url));
  await assert.rejects(() => stale.rank(history, { as_of: "2026-09-14T16:00:00Z" }), /feed response/);
  // A rank request may honestly return the view's existing frozen order after
  // a read or save advances live behavior revisions. Accept older revisions,
  // but never a response claiming knowledge of a future revision.
  const frozenRefresh = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => response(payload({ history_revision: 5, server_commit_revision: 7 }), url));
  const reused = await frozenRefresh.rank(history, { as_of: "2026-09-14T16:00:00Z" });
  assert.equal(reused.server_commit_revision, 7);
  const concurrentCommit = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => response(payload({ server_commit_revision: 9 }), url));
  const rebound = await concurrentCommit.rank(history, { as_of: "2026-09-14T16:00:00Z" });
  assert.equal(rebound.server_commit_revision, 9,
    "a concurrent behavior commit may legitimately advance beyond the request revision");
  const futureRefresh = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => response(payload({ history_revision: 7, server_commit_revision: 9 }), url));
  await assert.rejects(() => futureRefresh.rank(history, { as_of: "2026-09-14T16:00:00Z" }), /feed response/);
  const currentCard = payload().cards[0];
  const refreshedCard = reader.mergeM2CardState(
    { ...currentCard, state_revision: 2, read_at: null, saved_at: null,
      interests: [{ topic_id: "ai", signal: "more_like", revision: 1 }] },
    { ...currentCard, state_revision: 4, read_at: "2026-09-14T15:00:00Z",
      saved_at: "2026-09-14T15:01:00Z", interests: [
        { topic_id: "ai", signal: "less_like", revision: 3 },
        { topic_id: "policy", signal: "more_like", revision: 2 },
      ] }, true);
  assert.equal(refreshedCard.state_revision, 4);
  assert.equal(refreshedCard.saved_at, "2026-09-14T15:01:00Z");
  assert.deepEqual(refreshedCard.interests, [
    { topic_id: "ai", signal: "less_like", revision: 3 },
    { topic_id: "policy", signal: "more_like", revision: 2 },
  ], "a frozen refresh must not roll back newer card or interest state");
  const clearedCard = reader.mergeM2CardState(
    { ...currentCard, state_revision: 0, read_at: null, saved_at: null, interests: [] },
    { ...currentCard, state_revision: 4, read_at: "2026-09-14T15:00:00Z",
      saved_at: "2026-09-14T15:01:00Z",
      interests: [{ topic_id: "ai", signal: "more_like", revision: 3 }] }, false);
  assert.equal(clearedCard.state_revision, 0,
    "a new history generation must treat the server card state as authoritative");
  assert.deepEqual(clearedCard.interests, [],
    "a history reset or consent change must not resurrect deleted interests");
  assert.throws(() => reader.validateM2Config({ ...config, provider_retention_url: "javascript:bad" }), /configuration/);

  // Load more after a read or a save: /page returns the FROZEN order's binding,
  // and the reader must validate against THAT, not against the live revision.
  // Validating against the live one made a valid frozen page look invalid and
  // dropped the reader to the captured-edition fallback.
  const frozenBinding = { ...history, policy_version: config.policy_version,
    model_version: config.model_version, page_size: config.page_size,
    history_revision: history.included_history_revision,
    server_commit_revision: history.history_revision };
  const pagerCalls = [];
  const pager = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url, options) => { pagerCalls.push({ url, options }); return response(payload(), url); });
  // The behavior revision has since moved (she read a card and saved one), but
  // the frozen order still carries the revisions it was computed against.
  const pagedAfterReads = await pager.page("cursor-token", frozenBinding);
  assert.equal(pagedAfterReads.server_commit_revision, history.history_revision,
    "a page must keep answering with the frozen order's own binding");
  assert.equal(pagedAfterReads.cards.length > 0, true, "load more fell back after a read or save");
  assert.equal(pagerCalls[0].options.headers["x-news-curator-order-origin"], "1");

  // end_of_run is optional on the wire, so reader and ranker deploy in either
  // order: an older ranker never sends it, and the reader defaults it to false.
  assert.equal(reader.validateM2Response(payload(), frozenBinding).end_of_run, false);
  // Provenance is optional for frozen orders from older releases. New orders
  // name whether the current view used feed rules or which model timing path.
  assert.equal(reader.validateM2Response(payload(), frozenBinding).order_origin, null);
  for (const origin of ["recipe", "freshness"]) {
    assert.equal(reader.validateM2Response({ ...payload(), order_origin: origin }, frozenBinding).order_origin, origin);
  }
  for (const origin of ["prepared_model", "direct_model"]) {
    assert.equal(reader.validateM2Response({ ...payload(), result_mode: "model", fallback_reason: "",
      order_origin: origin }, frozenBinding).order_origin, origin);
  }
  assert.throws(() => reader.validateM2Response({ ...payload(), order_origin: "prepared_model" }, frozenBinding),
    /feed response/);
  for (const origin of ["recipe", "freshness"]) {
    assert.throws(() => reader.validateM2Response({ ...payload(), result_mode: "model",
      fallback_reason: "", order_origin: origin }, frozenBinding), /feed response/);
  }
  assert.throws(() => reader.validateM2Response({ ...payload(), order_origin: "unknown" }, frozenBinding),
    /feed response/);
  assert.equal(reader.validateM2Response({ ...payload(), end_of_run: true }, frozenBinding).end_of_run, true);
  assert.throws(() => reader.validateM2Response({ ...payload(), end_of_run: "yes" }, frozenBinding),
    /feed response/);

  // Another request is already buying this view's ranking. Retryable, and NOT a
  // reason to show the captured edition: the answer exists in a moment.
  const busyService = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => ({ ok: false, redirected: false, url, text: async () => JSON.stringify(
      { error: "ranking_in_progress" }) }));
  await assert.rejects(() => busyService.rank(history, { as_of: "2026-09-14T16:00:00Z" }),
    (error) => error.rankingInProgress === true &&
      !/The M2 reader request failed/.test(error.message));
  const oldCursorService = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => ({ ok: false, redirected: false, url, text: async () => JSON.stringify(
      { error: "cursor_version" }) }));
  await assert.rejects(() => oldCursorService.page("old-cursor", frozenBinding),
    (error) => error.staleCursor === true && /current reader version/.test(error.message));
  // The retry budget is config, with safe defaults.
  assert.equal(reader.validateM2Config(config).in_progress_retry_ms, 2000);
  assert.equal(reader.validateM2Config(config).in_progress_max_attempts, 3);
  assert.equal(reader.validateM2Config(config).empty_page_max_attempts, 3);
  assert.throws(() => reader.validateM2Config({ ...config, in_progress_retry_ms: 99 }), /configuration/);
  assert.throws(() => reader.validateM2Config({ ...config, in_progress_max_attempts: 0 }), /configuration/);
  assert.throws(() => reader.validateM2Config({ ...config, empty_page_max_attempts: 0 }), /configuration/);
  assert.throws(() => reader.validateM2Config({ ...config, empty_page_max_attempts: 6 }), /configuration/);

  // A prompt revision bump is a QUESTION the reader can answer in one tap, so
  // it must not collapse into the generic failure that shows a dead feed.
  const consentService = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => ({ ok: false, redirected: false, url, text: async () => JSON.stringify(
      { error: "provider_consent_required", provider_policy_id: "m2-rankllm-predictions-r1" }) }));
  await assert.rejects(() => consentService.rank(history, { as_of: "2026-09-14T16:00:00Z" }),
    (error) => error.consentRequired === true &&
      error.providerPolicyId === "m2-rankllm-predictions-r1" &&
      !/The M2 reader request failed/.test(error.message));

  // Deploy-order safety: one release accepts the three older card versions and
  // version 4, which adds the independently measured coverage count.
  const expectation = { ...history, policy_version: config.policy_version, model_version: config.model_version,
    history_revision: history.included_history_revision, server_commit_revision: history.history_revision,
    page_size: config.page_size };
  const legacyCard = { ...payload().cards[0], card_schema_version: 1 };
  ["title_en", "title_zh", "summary_en", "summary_zh", "translation_status"].forEach((field) => { delete legacyCard[field]; });
  const legacy = reader.validateM2Response(payload({ cards: [legacyCard] }), expectation);
  assert.equal(legacy.cards[0].card_schema_version, 4, "a version-1 card normalizes to the rendered shape");
  assert.equal(legacy.cards[0].lane, null, "an unlabelled card renders without inventing a label");
  assert.deepEqual(legacy.cards[0].also_covered_by, []);
  assert.equal(legacy.cards[0].title_en, legacyCard.title);
  assert.equal(legacy.cards[0].title_zh, "");
  assert.deepEqual(legacy.cards[0].translation_status, { en: "original", zh: "untranslated" });
  const current = reader.validateM2Response(payload(), expectation);
  assert.equal(current.cards[0].card_schema_version, 4);
  // A version-3 card carries the element labels through untouched.
  const labelled = { ...payload().cards[0], card_schema_version: 3, lane: "surprise",
    lane_label: "surprise", surprise_label: "you might not have looked for this",
    exclusive_label: null, also_covered_by: ["Reuters"] };
  const withLabels = reader.validateM2Response(payload({ cards: [labelled] }), expectation);
  assert.equal(withLabels.cards[0].lane_label, "surprise");
  assert.equal(withLabels.cards[0].surprise_label, "you might not have looked for this");
  assert.deepEqual(withLabels.cards[0].also_covered_by, ["Reuters"]);
  const covered = { ...labelled, card_schema_version: 4, coverage_count: 4 };
  const withCoverage = reader.validateM2Response(payload({ cards: [covered] }), expectation);
  assert.equal(withCoverage.cards[0].coverage_count, 4);
  // The honest fifth chip is a real lane the reader accepts.
  const backfilled = reader.validateM2Response(
    payload({ cards: [{ ...labelled, lane: "more", lane_label: "More", surprise_label: null }] }),
    expectation);
  assert.equal(backfilled.cards[0].lane, "more");
  assert.equal(backfilled.cards[0].lane_label, "More");

  // An unknown pool name is refused, so a renamed lane cannot render unchecked.
  assert.throws(() => reader.validateM2Response(
    payload({ cards: [{ ...labelled, lane: "trending" }] }), expectation), /feed response/);
  // A version-1 card carrying translation fields is still rejected, and so is
  // an oversized translated summary.
  assert.throws(() => reader.validateM2Response(payload({ cards: [{ ...payload().cards[0], card_schema_version: 1 }] }), expectation), /feed response/);
  assert.throws(() => reader.validateM2Response(payload({ cards: [{ ...payload().cards[0], summary_zh: "x".repeat(32001) }] }), expectation), /feed response/);
  assert.throws(() => reader.validateM2Response(payload({ cards: [{ ...payload().cards[0], card_schema_version: 3 }] }), expectation), /feed response/);
})().catch((error) => { console.error(error); process.exitCode = 1; });
