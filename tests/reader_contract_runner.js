"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const reader = require(path.join(__dirname, "..", "static", "reader.js"));

function response(status, payload, url, overrides = {}) {
  return {
    ok: status >= 200 && status < 300,
    redirected: false,
    status,
    url,
    text: async () => JSON.stringify(payload),
    ...overrides,
  };
}

function story(overrides = {}) {
  return {
    story_id: "story:" + "a".repeat(64),
    canonical_url: "https://publisher.example/story",
    title: "A real story",
    summary: "A publisher supplied summary.",
    language: "en",
    published_at: "2026-09-07T12:00:00Z",
    publication_seq: 7,
    position: 1,
    page_order_mode: "edition_rank",
    next_cursor: { after_position: 1, after_story_id: "story:" + "a".repeat(64) },
    ordering_mode: "weighted_total",
    ordering_key: { score: 1, story_id: "story:" + "a".repeat(64) },
    score_components: { freshness: 1 },
    topic_ids: ["ai"],
    topic_ranks: { ai: 1 },
    source_kind: "outlet",
    source_name: "Publisher",
    ranking_explanation: "Weighted using freshness.",
    coverage_mentions: [],
    read_at: null,
    saved_at: null,
    state_revision: 0,
    interests: [],
    ...overrides,
  };
}

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.className = "";
    this.textContent = "";
    this.hidden = false;
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, on) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (on) names.add(name); else names.delete(name);
        this.className = [...names].join(" ");
      },
    };
  }
  append(...children) { this.children.push(...children); this.lastChild = children.at(-1); }
  addEventListener() {}
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  querySelector(selector) {
    if (selector.startsWith(".")) {
      const name = selector.slice(1);
      return this.children.find((child) => child instanceof FakeElement && child.classList.contains(name)) || null;
    }
    return null;
  }
}

function textOf(node) {
  if (!(node instanceof FakeElement)) return String(node.textContent || "");
  return node.textContent + node.children.map(textOf).join("");
}

async function main() {
  const latest = reader.validateLatestPublication({
    publication_seq: 7,
    finalized_at: "2026-09-07T12:00:00Z",
    topics: [{ topic_id: "ai", name: "AI" }],
    initial_history_cursor: {
      before_published_at: "2026-09-07T11:00:00Z",
      before_story_id: "",
    },
    poll_seconds: 60,
  });
  assert.equal(latest.publication_seq, 7);
  assert.equal(reader.validateLatestPublication({ ...latest, poll_seconds: 86400 }).poll_seconds, 86400);
  assert.throws(() => reader.validateLatestPublication({ ...latest, poll_seconds: 86401 }), /publication response/);
  assert.throws(() => reader.validateLatestPublication({ ...latest, extra: true }), /publication response/);
  assert.equal(reader.validateFeedPage([story()])[0].story_id, story().story_id);
  assert.throws(
    () => reader.validateFeedPage([story({ ordering_mode: "unknown" })]),
    /feed response/
  );
  assert.throws(
    () => reader.validateFeedPage([story({ coverage_mentions: [{
      source_kind: "outlet",
      source_id: "source-a",
      source_name: "Source A",
      url: "http://publisher.example/story",
      headline: "A real story",
      mentioned_at: "2026-09-07T12:00:00Z",
    }] })]),
    /feed response/
  );
  assert.equal(reader.validateFeedPage([story({ coverage_mentions: [{
    source_kind: "newsletter",
    source_id: "newsletter-a",
    source_name: "Newsletter A",
    url: "https://publisher.example/story",
    headline: "A real story",
    mentioned_at: "2026-09-07T12:00:00Z",
  }] })])[0].coverage_mentions.length, 1);
  assert.throws(() => {
    const malformed = story();
    delete malformed.state_revision;
    reader.validateFeedPage([malformed]);
  }, /feed response/);
  assert.deepEqual(reader.nextFeedCursor([], latest.initial_history_cursor), {
    order_mode: "history_freshness",
    ...latest.initial_history_cursor,
  });
  assert.deepEqual(reader.nextFeedCursor([story()], latest.initial_history_cursor), {
    order_mode: "history_freshness",
    ...latest.initial_history_cursor,
  });
  assert.throws(() => reader.validateFeedPage([story({ title: "x".repeat(2001) })]), /feed response/);
  assert.throws(
    () => reader.validateFeedPage([story({ state_revision: undefined })], true),
    /feed response/
  );

  const calls = [];
  const api = reader.createApi(
    { url: "https://project-ref.supabase.co", key: "sb_publishable_example" },
    () => ({ access_token: "header.payload.signature" }),
    async (url, options) => {
      calls.push({ url, options });
      return response(200, [story()], url);
    }
  );
  await api.feedPage("ai", {
    order_mode: "history_freshness",
    before_published_at: "2026-09-07T11:00:00Z",
    before_story_id: "story:" + "b".repeat(64),
  });
  assert.equal(calls[0].url.endsWith("/rest/v1/rpc/feed_page"), true);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    p_topic_id: "ai",
    p_order_mode: "history_freshness",
    p_after_position: null,
    p_after_story_id: null,
    p_before_published_at: "2026-09-07T11:00:00Z",
    p_before_story_id: "story:" + "b".repeat(64),
    p_limit: 20,
  });
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.redirect, "error");
  await api.feedPage("__all__", null);
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    p_topic_id: null,
    p_order_mode: "history_freshness",
    p_after_position: null,
    p_after_story_id: null,
    p_before_published_at: null,
    p_before_story_id: null,
    p_limit: 20,
  });
  await api.feedPage("ai", {
    order_mode: "edition_rank",
    after_position: 20,
    after_story_id: "story:" + "c".repeat(64),
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    p_topic_id: "ai",
    p_order_mode: "edition_rank",
    p_after_position: 20,
    p_after_story_id: "story:" + "c".repeat(64),
    p_before_published_at: null,
    p_before_story_id: null,
    p_limit: 20,
  });

  calls.length = 0;
  const stateApi = reader.createApi(
    { url: "https://project-ref.supabase.co", key: "sb_publishable_example" },
    () => ({ access_token: "header.payload.signature" }),
    async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("/saved_page")) {
        const row = story({
          saved_at: "2026-09-07T12:02:00Z",
          page_order_mode: "saved_at",
          next_cursor: {
            before_saved_at: "2026-09-07T12:02:00Z",
            before_story_id: story().story_id,
          },
        });
        return response(200, [row], url);
      }
      if (url.endsWith("/updates_since")) return response(200, [{
        publication_seq: 8,
        story_id: story().story_id,
        title: "A real story",
        published_at: "2026-09-07T12:00:00Z",
        topic_ids: ["ai"],
        next_cursor: {
          after_publication_seq: 8,
          after_published_at: "2026-09-07T12:00:00Z",
          after_story_id: story().story_id,
        },
      }], url);
      if (url.endsWith("/set_story_state")) return response(200, {
        status: "updated", read_at: "2026-09-07T12:03:00Z", saved_at: null, revision: 2,
      }, url);
      return response(200, {
        status: "updated", signal: "more_like", revision: 1,
      }, url);
    }
  );
  await stateApi.savedPage({ before_saved_at: "2026-09-07T12:02:00Z", before_story_id: story().story_id });
  await stateApi.updatesSince(7);
  await stateApi.setStoryState(story().story_id, true, false, 1, "idem-state");
  await stateApi.setStoryInterest(story().story_id, "ai", 0, "idem-interest");
  assert.deepEqual(calls.map((call) => JSON.parse(call.options.body)), [
    { p_before_saved_at: "2026-09-07T12:02:00Z", p_before_story_id: story().story_id, p_limit: 20 },
    {
      p_since_publication_seq: 7,
      p_after_publication_seq: null,
      p_after_published_at: null,
      p_after_story_id: null,
      p_limit: 20,
    },
    {
      p_story_id: story().story_id, p_read: true, p_saved: false,
      p_expected_revision: 1, p_idempotency_key: "idem-state",
    },
    {
      p_story_id: story().story_id, p_topic_id: "ai", p_signal: "more_like",
      p_expected_revision: 0, p_idempotency_key: "idem-interest",
    },
  ]);

  const existing = { dataset: { topicIds: "ai" } };
  reader.mergeTopicMembership(existing, ["ai", "crypto"]);
  assert.equal(existing.dataset.topicIds, "ai crypto");
  const rankedCard = {
    attrs: { "data-rank-all": "3" },
    setAttribute(name, value) { this.attrs[name] = value; },
    hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); },
  };
  reader.applyServerRank(rankedCard, story({
    page_order_mode: "edition_rank", position: 17, topic_ids: ["ai", "crypto"],
    topic_ranks: { ai: 4, crypto: 17 },
  }), "crypto");
  assert.equal(rankedCard.attrs["data-rank-crypto"], "17");
  assert.equal(rankedCard.attrs["data-rank-ai"], "4");
  reader.applyServerRank(rankedCard, story({
    page_order_mode: "history_freshness", position: 2,
  }), "__all__");
  assert.equal(rankedCard.attrs["data-rank-all"], "3");
  const historyCard = {
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = value; },
    hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); },
  };
  reader.applyServerRank(historyCard, story({
    page_order_mode: "history_freshness", position: 2, topic_ranks: { ai: 9, crypto: 2 },
  }), "__all__");
  assert.equal(historyCard.attrs["data-rank-all"], "1000002");
  assert.equal(historyCard.attrs["data-rank-ai"], "9");
  assert.equal(historyCard.attrs["data-rank-crypto"], "2");
  assert.equal(reader.effectiveTopic(["crypto", "ai"], "crypto"), "crypto");
  assert.equal(reader.effectiveTopic(["crypto", "ai"], "__all__"), "ai");

  const fakeCard = {
    dataset: {},
    classList: {
      values: new Set(),
      toggle(name, on) { if (on) this.values.add(name); else this.values.delete(name); },
    },
    controls: {
      ".read-action": { textContent: "", setAttribute() {} },
      ".save-action": { textContent: "", attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } },
      ".interest-action": {
        textContent: "", dataset: { topicId: "ai" }, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
      },
    },
    querySelector(selector) { return this.controls[selector] || null; },
  };
  reader.applyServerState(fakeCard, story({ read_at: "2026-09-07T12:01:00Z" }));
  assert.equal(fakeCard.classList.values.has("is-read"), true);
  assert.equal(fakeCard.controls[".read-action"].textContent, "Mark unread");
  assert.equal(fakeCard.controls[".save-action"].textContent, "Save");
  reader.applyServerState(fakeCard, {
    interests: [{ topic_id: "ai", signal: "more_like", revision: 1 }],
  });
  assert.equal(fakeCard.classList.values.has("is-read"), true);
  assert.equal(fakeCard.controls[".read-action"].textContent, "Mark unread");
  assert.equal(fakeCard.classList.values.has("is-more-like"), true);
  assert.equal(reader.safeDestination("https://publisher.example/a"), "https://publisher.example/a");
  assert.equal(reader.safeDestination("javascript:alert(1)"), null);
  assert.equal(
    reader.rankingReason("preference_then_freshness", { preference: 1, freshness: 2 }, {}),
    "Your saved interests are considered first, then freshness."
  );
  assert.equal(
    reader.rankingReason("native_rank_then_freshness", { native_rank: 1 }, {}),
    "The source's captured rank is considered first, then freshness."
  );
  assert.equal(
    reader.rankingReason("weighted_total", { score: 3 }, { freshness: 2, topic_fit: 1 }),
    "Weighted using freshness, topic fit."
  );

  let updateCall = 0;
  const updateRows = Array.from({ length: 21 }, (_, index) => story({
    story_id: "story:" + (index + 1).toString(16).padStart(64, "0"),
    publication_seq: 8,
  })).map((row) => ({
    publication_seq: row.publication_seq,
    story_id: row.story_id,
    title: row.title,
    published_at: row.published_at,
    topic_ids: row.topic_ids,
    next_cursor: {
      after_publication_seq: row.publication_seq,
      after_published_at: row.published_at,
      after_story_id: row.story_id,
    },
  }));
  const drained = await reader.drainUpdates({
    updatesSince: async () => updateCall++ === 0 ? updateRows.slice(0, 20) : updateRows.slice(20),
  }, 7, null);
  assert.equal(drained.rows.length, 21);
  assert.equal(drained.drained, true);
  assert.equal(drained.cursor, null);
  const newer = await reader.drainUpdates({
    updatesSince: async (baseline) => baseline === 8 ? [{ ...updateRows[0], publication_seq: 9 }] : [],
  }, 8, null);
  assert.equal(newer.rows[0].publication_seq, 9);

  const controls = new Map([
    ["reader-status", { textContent: "" }],
    ["load-more", { hidden: false, addEventListener() {} }],
    ["updates-status", { hidden: true }],
    ["show-updates", { dataset: {}, addEventListener() {} }],
    ["sections", { addEventListener() {}, append() {} }],
  ]);
  global.window = {
    NewsCuratorAuth: {
      config: () => ({ url: "https://reader.example", key: "public-key" }),
      hasSessionCandidate: () => false,
      sessionForRequest: async () => null,
    },
    NewsCuratorView: { currentTab: () => "__all__", addCard() {}, apply() {} },
    location: { reload() {} },
    setInterval() { throw new Error("polling should not start without a publication"); },
  };
  global.document = {
    getElementById: (id) => controls.get(id) || null,
    querySelectorAll: () => [],
    querySelector: () => null,
  };
  global.BroadcastChannel = undefined;
  global.fetch = async (url) => response(200, {}, url);
  await reader.run();
  assert.equal(controls.get("reader-status").textContent, "No published edition is available yet.");
  assert.equal(controls.get("load-more").hidden, true);
  delete global.fetch;
  delete global.document;
  delete global.window;
  delete global.BroadcastChannel;

  const sections = new FakeElement("div");
  const aiSection = new FakeElement("section");
  aiSection.dataset.section = "ai";
  const grid = new FakeElement("div");
  grid.className = "grid";
  aiSection.append(grid);
  sections.append(aiSection);
  const liveControls = new Map([
    ["reader-status", new FakeElement()], ["load-more", new FakeElement("button")],
    ["updates-status", new FakeElement()], ["show-updates", new FakeElement("button")],
    ["sections", sections],
  ]);
  const addedCards = [];
  global.BroadcastChannel = undefined;
  global.CSS = { escape: (value) => value };
  global.window = {
    NewsCuratorAuth: {
      config: () => ({ url: "https://reader.example", key: "public-key" }),
      hasSessionCandidate: () => true,
      sessionForRequest: async () => ({ access_token: "refreshed-reader-token" }),
    },
    NewsCuratorView: {
      currentTab: () => "__all__", addCard: (card) => addedCards.push(card), apply() {},
    },
    location: { reload() {} }, setInterval: () => 1,
  };
  global.document = {
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (value) => ({ textContent: value }),
    getElementById: (id) => liveControls.get(id) || null,
    querySelectorAll: () => [],
    querySelector: (selector) => selector.includes('data-section="ai"') ? aiSection : null,
  };
  let controllerCalls = 0;
  const controllerHeaders = [];
  global.fetch = async (url, options) => {
    controllerHeaders.push(options.headers);
    controllerCalls += 1;
    if (url.endsWith("/latest_publication")) return response(200, {
      publication_seq: 7, finalized_at: "2026-09-07T12:00:00Z",
      topics: [{ topic_id: "ai", name: "AI" }],
      initial_history_cursor: null, poll_seconds: 60,
    }, url);
    return response(200, [story({
      page_order_mode: "history_freshness",
      next_cursor: { before_published_at: "2026-09-07T12:00:00Z", before_story_id: story().story_id },
      topic_ids: ["ai", "crypto"], topic_ranks: { ai: 2, crypto: 1 },
      source_kind: "newsletter", source_name: "Daily Brief",
      ranking_explanation: "Saved interests were considered first, then freshness.",
    })], url);
  };
  await reader.run();
  assert.equal(controllerCalls, 2);
  assert.equal(controllerHeaders[1].authorization, "Bearer refreshed-reader-token");
  assert.equal(addedCards.length, 1);
  assert.equal(addedCards[0].attrs["data-rank-ai"], "2");
  assert.equal(addedCards[0].attrs["data-rank-crypto"], "1");
  const renderedText = textOf(addedCards[0]);
  assert.match(renderedText, /NewsletterDaily Brief/);
  assert.match(renderedText, /Published/);
  assert.match(renderedText, /Saved interests were considered first, then freshness\./);

  const refreshedRpcHeaders = [];
  const refreshedApi = reader.createApi(
    { url: "https://reader.example", key: "public-key" },
    async () => ({ access_token: "refreshed-reader-token" }),
    async (url, options) => {
      refreshedRpcHeaders.push(options.headers);
      if (url.endsWith("/feed_page")) return response(200, [], url);
      return response(200, {
        status: "updated", read_at: "2026-09-07T12:03:00Z", saved_at: null, revision: 2,
      }, url);
    },
  );
  await refreshedApi.feedPage("ai", null);
  await refreshedApi.setStoryState(story().story_id, true, false, 1, "refresh-write");
  assert.deepEqual(
    refreshedRpcHeaders.map((headers) => headers.authorization),
    ["Bearer refreshed-reader-token", "Bearer refreshed-reader-token"],
  );
  let failedRefreshReachedRpc = false;
  const failedRefreshApi = reader.createApi(
    { url: "https://reader.example", key: "public-key" },
    async () => { throw new Error("Session refresh failed."); },
    async () => { failedRefreshReachedRpc = true; throw new Error("RPC must not run"); },
  );
  await assert.rejects(failedRefreshApi.feedPage("ai", null), /Session refresh failed/);
  assert.equal(failedRefreshReachedRpc, false);
  let anonymousAuthorization = "not-called";
  const afterClearApi = reader.createApi(
    { url: "https://reader.example", key: "public-key" },
    async () => null,
    async (url, options) => {
      anonymousAuthorization = options.headers.authorization;
      return response(200, [], url);
    },
  );
  await afterClearApi.feedPage("ai", null);
  assert.equal(anonymousAuthorization, undefined);
  delete global.BroadcastChannel;
  delete global.CSS;
  delete global.fetch;
  delete global.document;
  delete global.window;
  console.log("reader contract: PASS");
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : "reader contract failed");
  process.exitCode = 1;
});
