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
    card_schema_version: 1,
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
})().catch((error) => { console.error(error); process.exitCode = 1; });
