"use strict";

const assert = require("assert");
const dashboard = require("../static/dashboard/dashboard.js");

const summary = {
  schema_version: 1,
  scope: "current_retained_state",
  snapshot_at: "2026-09-08T12:00:00Z",
  saved_count: 4,
  saved_unread_count: 2,
  read_count: 7,
  active_interest_signal_count: 3,
  topic_signals: [
    { topic_id: "quantum", more_like_count: 2, less_like_count: 1 },
  ],
};

assert.deepStrictEqual(dashboard.validateSummary(summary), summary);
assert.throws(() => dashboard.validateSummary({ ...summary, user_id: "private" }));
assert.throws(() => dashboard.validateSummary({ ...summary, saved_count: -1 }));
assert.throws(() => dashboard.validateSummary({ ...summary, saved_count: 1, saved_unread_count: 2 }));
assert.throws(() => dashboard.validateSummary({
  ...summary,
  topic_signals: [{ topic_id: 1, more_like_count: 2, less_like_count: 1 }],
}));
assert.throws(() => dashboard.validateSummary({ ...summary, scope: "lifetime" }));
assert.throws(() => dashboard.validateSummary({
  ...summary,
  topic_signals: [
    { topic_id: "quantum", more_like_count: 1, less_like_count: 0 },
    { topic_id: "quantum", more_like_count: 0, less_like_count: 1 },
  ],
}));
assert.throws(() => dashboard.validateSummary({
  ...summary,
  active_interest_signal_count: 5,
  topic_signals: [
    { topic_id: "quantum", more_like_count: 1, less_like_count: 0 },
    { topic_id: "ai", more_like_count: 2, less_like_count: 0 },
  ],
}));
assert.throws(() => dashboard.validateSummary({
  ...summary,
  active_interest_signal_count: 1,
  topic_signals: [{ topic_id: "quantum", more_like_count: 1, less_like_count: 1 }],
}));

const preference = {
  user_id: "private",
  revision: 3,
  locale: "en",
  interests: ["quantum computing"],
  saved_searches: [{ id: "q", query: "quantum", enabled: true }],
  created_at: "2026-09-01T12:00:00Z",
  updated_at: "2026-09-08T12:00:00Z",
};
assert.deepStrictEqual(Object.keys(dashboard.publicPreference(preference)).sort(), [
  "created_at", "interests", "locale", "revision", "saved_searches", "updated_at",
].sort());

const cursor = { before_saved_at: "2026-09-08T11:00:00Z", before_story_id: `story:${"a".repeat(64)}` };
const snapshot = dashboard.buildSnapshot({
  summary,
  preference,
  loadedItems: [{ story_id: `story:${"a".repeat(64)}`, title: "Saved story" }],
  displayedItems: [{ story_id: `story:${"a".repeat(64)}`, title: "Saved story" }],
  pageSize: 7,
  exhausted: false,
  nextCursor: cursor,
  now: () => "2026-09-08T12:30:00.000Z",
});
assert.deepStrictEqual(Object.keys(snapshot), ["schema_version", "kind", "snapshot_at", "summary", "preferences", "saved"]);
assert.strictEqual(snapshot.kind, "loaded_dashboard_snapshot");
assert.strictEqual(snapshot.saved.loaded_count, 1);
assert.strictEqual(snapshot.saved.displayed_count, 1);
assert.strictEqual(snapshot.saved.page_size, 7);
assert.strictEqual(snapshot.saved.all_saved_loaded, false);
assert.deepStrictEqual(snapshot.saved.next_cursor, cursor);
assert.ok(!JSON.stringify(snapshot).includes("private"));
assert.throws(() => dashboard.buildSnapshot({
  summary, preference, loadedItems: [], displayedItems: [], pageSize: 7,
  exhausted: true, nextCursor: cursor, now: () => "2026-09-08T12:30:00.000Z",
}));
const projected = dashboard.buildSnapshot({
  summary, preference, loadedItems: [{ story_id: `story:${"a".repeat(64)}`, user_id: "private" }],
  displayedItems: [{ story_id: `story:${"a".repeat(64)}`, user_id: "private" }], pageSize: 7,
  exhausted: false, nextCursor: cursor, now: () => "2026-09-08T12:30:00.000Z",
});
assert.ok(!JSON.stringify(projected).includes("private"));

console.log("dashboard contract runner: PASS");
