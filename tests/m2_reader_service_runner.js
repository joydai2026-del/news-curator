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
  assert.equal(reader.validateM2Config({ ...config, request_timeout_ms: 8000 }).transport_timeout_ms, 8000);
  assert.equal(reader.validateM2Config({ ...config, request_timeout_ms: 8000, transport_timeout_ms: 20000 }).transport_timeout_ms, 20000);
  assert.throws(() => reader.validateM2Config({ ...config, request_timeout_ms: 8001 }), /configuration/);
  assert.throws(() => reader.validateM2Config({ ...config, request_timeout_ms: 30000 }), /configuration/);
  assert.throws(() => reader.validateM2Config({ ...config, request_timeout_ms: 8000, transport_timeout_ms: 7999 }), /configuration/);
  assert.throws(() => reader.validateM2Config({ ...config, request_timeout_ms: 8000, transport_timeout_ms: 20001 }), /configuration/);
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
  assert.equal(JSON.stringify(calls[0]).includes("user_id"), false);

  const changed = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => { token = "token-b"; return response(payload(), url); });
  await assert.rejects(() => changed.rank(history, { as_of: "2026-09-14T16:00:00Z" }), /account changed/);
  token = "token-a";
  const stale = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => response(payload({ history_generation: 1 }), url));
  await assert.rejects(() => stale.rank(history, { as_of: "2026-09-14T16:00:00Z" }), /feed response/);
  assert.throws(() => reader.validateM2Config({ ...config, provider_retention_url: "javascript:bad" }), /configuration/);

  // Load more after a read or a save: /page returns the FROZEN order's binding,
  // and the reader must validate against THAT, not against the live revision.
  // Validating against the live one made a valid frozen page look invalid and
  // dropped the reader to the captured-edition fallback.
  const frozenBinding = { ...history, policy_version: config.policy_version,
    model_version: config.model_version, page_size: config.page_size,
    history_revision: history.included_history_revision,
    server_commit_revision: history.history_revision };
  const pager = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => response(payload(), url));
  // The behavior revision has since moved (she read a card and saved one), but
  // the frozen order still carries the revisions it was computed against.
  const pagedAfterReads = await pager.page("cursor-token", frozenBinding);
  assert.equal(pagedAfterReads.server_commit_revision, history.history_revision,
    "a page must keep answering with the frozen order's own binding");
  assert.equal(pagedAfterReads.cards.length > 0, true, "load more fell back after a read or save");

  // end_of_run is optional on the wire, so reader and ranker deploy in either
  // order: an older ranker never sends it, and the reader defaults it to false.
  assert.equal(reader.validateM2Response(payload(), frozenBinding).end_of_run, false);
  assert.equal(reader.validateM2Response({ ...payload(), end_of_run: true }, frozenBinding).end_of_run, true);
  assert.throws(() => reader.validateM2Response({ ...payload(), end_of_run: "yes" }, frozenBinding),
    /feed response/);

  // A prompt revision bump is a QUESTION the reader can answer in one tap, so
  // it must not collapse into the generic failure that shows a dead feed.
  const consentService = reader.createM2Service(config, async () => ({ access_token: token }),
    async (url) => ({ ok: false, redirected: false, url, text: async () => JSON.stringify(
      { error: "provider_consent_required", provider_policy_id: "m2-rankllm-predictions-r1" }) }));
  await assert.rejects(() => consentService.rank(history, { as_of: "2026-09-14T16:00:00Z" }),
    (error) => error.consentRequired === true &&
      error.providerPolicyId === "m2-rankllm-predictions-r1" &&
      !/The M2 reader request failed/.test(error.message));

  // Deploy-order safety: one release accepts a version-1 card (an older ranker),
  // a version-2 card, and a version-3 card (this one), in either direction. Every
  // one normalizes to the rendered shape, which is version 3.
  const expectation = { ...history, policy_version: config.policy_version, model_version: config.model_version,
    history_revision: history.included_history_revision, server_commit_revision: history.history_revision,
    page_size: config.page_size };
  const legacyCard = { ...payload().cards[0], card_schema_version: 1 };
  ["title_en", "title_zh", "summary_en", "summary_zh", "translation_status"].forEach((field) => { delete legacyCard[field]; });
  const legacy = reader.validateM2Response(payload({ cards: [legacyCard] }), expectation);
  assert.equal(legacy.cards[0].card_schema_version, 3, "a version-1 card normalizes to the rendered shape");
  assert.equal(legacy.cards[0].lane, null, "an unlabelled card renders without inventing a label");
  assert.deepEqual(legacy.cards[0].also_covered_by, []);
  assert.equal(legacy.cards[0].title_en, legacyCard.title);
  assert.equal(legacy.cards[0].title_zh, "");
  assert.deepEqual(legacy.cards[0].translation_status, { en: "original", zh: "untranslated" });
  const current = reader.validateM2Response(payload(), expectation);
  assert.equal(current.cards[0].card_schema_version, 3);
  // A version-3 card carries the element labels through untouched.
  const labelled = { ...payload().cards[0], card_schema_version: 3, lane: "surprise",
    lane_label: "surprise", surprise_label: "you might not have looked for this",
    exclusive_label: null, also_covered_by: ["Reuters"] };
  const withLabels = reader.validateM2Response(payload({ cards: [labelled] }), expectation);
  assert.equal(withLabels.cards[0].lane_label, "surprise");
  assert.equal(withLabels.cards[0].surprise_label, "you might not have looked for this");
  assert.deepEqual(withLabels.cards[0].also_covered_by, ["Reuters"]);
  // An unknown pool name is refused, so a renamed lane cannot render unchecked.
  assert.throws(() => reader.validateM2Response(
    payload({ cards: [{ ...labelled, lane: "trending" }] }), expectation), /feed response/);
  // A version-1 card carrying translation fields is still rejected, and so is
  // an oversized translated summary.
  assert.throws(() => reader.validateM2Response(payload({ cards: [{ ...payload().cards[0], card_schema_version: 1 }] }), expectation), /feed response/);
  assert.throws(() => reader.validateM2Response(payload({ cards: [{ ...payload().cards[0], summary_zh: "x".repeat(32001) }] }), expectation), /feed response/);
  assert.throws(() => reader.validateM2Response(payload({ cards: [{ ...payload().cards[0], card_schema_version: 3 }] }), expectation), /feed response/);
})().catch((error) => { console.error(error); process.exitCode = 1; });
