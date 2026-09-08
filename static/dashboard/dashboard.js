(() => {
  "use strict";

  const SUMMARY_FIELDS = ["active_interest_signal_count", "read_count", "saved_count", "saved_unread_count", "schema_version", "scope", "snapshot_at", "topic_signals"];
  const PREFERENCE_FIELDS = ["created_at", "interests", "locale", "revision", "saved_searches", "updated_at"];
  const TOPIC = /^[a-z0-9][a-z0-9-]{0,79}$/;
  const STORY = /^story:[0-9a-f]{64}$/;
  const ITEM_FIELDS = ["canonical_url", "coverage_mentions", "interests", "language", "next_cursor", "ordering_key", "ordering_mode", "page_order_mode", "position", "publication_seq", "published_at", "ranking_explanation", "read_at", "saved_at", "score_components", "source_kind", "source_name", "state_revision", "story_id", "summary", "title", "topic_ids", "topic_ranks"];
  const encoder = new TextEncoder();

  function fail(message) { throw new Error(message); }
  function object(value) { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }
  function exact(value, fields) {
    if (!object(value)) return false;
    const keys = Object.keys(value).sort();
    const expected = [...fields].sort();
    return keys.length === expected.length && keys.every((key, index) => key === expected[index]);
  }
  function timestamp(value) { return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value)); }
  function count(value) { return Number.isSafeInteger(value) && value >= 0; }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function validateSummary(value) {
    if (!exact(value, SUMMARY_FIELDS) || value.schema_version !== 1 || value.scope !== "current_retained_state" ||
        !timestamp(value.snapshot_at) || !count(value.saved_count) || !count(value.saved_unread_count) ||
        !count(value.read_count) || !count(value.active_interest_signal_count) ||
        !Array.isArray(value.topic_signals) || value.topic_signals.length > 100) fail("The dashboard response was invalid.");
    if (value.saved_unread_count > value.saved_count) fail("The dashboard response was invalid.");
    const seen = new Set();
    let displayedSignals = 0;
    let previous = null;
    value.topic_signals.forEach((row) => {
      if (!exact(row, ["less_like_count", "more_like_count", "topic_id"]) || typeof row.topic_id !== "string" || !TOPIC.test(row.topic_id) ||
          seen.has(row.topic_id) || !count(row.more_like_count) || !count(row.less_like_count)) fail("The dashboard response was invalid.");
      const total = row.more_like_count + row.less_like_count;
      if (!Number.isSafeInteger(total) ||
          (previous && (total > previous.total || (total === previous.total && row.topic_id <= previous.topicId)))) {
        fail("The dashboard response was invalid.");
      }
      displayedSignals += total;
      if (!Number.isSafeInteger(displayedSignals)) fail("The dashboard response was invalid.");
      previous = { total, topicId: row.topic_id };
      seen.add(row.topic_id);
    });
    if (displayedSignals > value.active_interest_signal_count) fail("The dashboard response was invalid.");
    return clone(value);
  }
  function publicPreference(value) {
    const allowedShape = exact(value, PREFERENCE_FIELDS) || exact(value, [...PREFERENCE_FIELDS, "user_id"]);
    if (!allowedShape || !Number.isSafeInteger(value.revision) ||
        value.revision < 0 || !["en", "zh"].includes(value.locale) || !Array.isArray(value.interests) ||
        !Array.isArray(value.saved_searches)) fail("The preference response was invalid.");
    return Object.fromEntries(PREFERENCE_FIELDS.map((key) => [key, clone(value[key])]));
  }
  function validCursor(value) {
    return value === null || (exact(value, ["before_saved_at", "before_story_id"]) && timestamp(value.before_saved_at) && STORY.test(value.before_story_id));
  }
  function buildSnapshot({ summary, preference, loadedItems, displayedItems, pageSize, exhausted, nextCursor, now = () => new Date().toISOString() }) {
    if (!Array.isArray(loadedItems) || !Array.isArray(displayedItems) || !Number.isInteger(pageSize) || pageSize < 1 || pageSize > 100 ||
        typeof exhausted !== "boolean" || !validCursor(nextCursor) || (exhausted && nextCursor !== null) || (!exhausted && nextCursor === null) ||
        loadedItems.some((item) => !object(item) || !STORY.test(item.story_id)) ||
        displayedItems.some((item) => !object(item) || !loadedItems.some((loaded) => loaded.story_id === item.story_id))) fail("The dashboard snapshot was invalid.");
    const snapshotAt = now();
    if (!timestamp(snapshotAt)) fail("The dashboard snapshot was invalid.");
    const projectedItems = displayedItems.map((item) => Object.fromEntries(
      ITEM_FIELDS.filter((field) => field in item).map((field) => [field, clone(item[field])]),
    ));
    return {
      schema_version: 1, kind: "loaded_dashboard_snapshot", snapshot_at: snapshotAt,
      summary: validateSummary(summary), preferences: publicPreference(preference),
      saved: {
        loaded_count: loadedItems.length, displayed_count: displayedItems.length, page_size: pageSize,
        all_saved_loaded: exhausted, next_cursor: exhausted ? null : clone(nextCursor), items: projectedItems,
      },
    };
  }
  async function summaryRequest(auth, fetchImpl = fetch) {
    const config = auth.config();
    const session = await auth.sessionForRequest();
    if (!session || typeof session.access_token !== "string") fail("Sign in to continue.");
    const url = `${config.url}/rest/v1/rpc/dashboard_summary`;
    const response = await fetchImpl(url, { method: "POST", headers: { apikey: config.key, authorization: `Bearer ${session.access_token}`, accept: "application/json", "content-type": "application/json" }, body: "{}", cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer", redirect: "error", signal: AbortSignal.timeout(15000) });
    if (response.redirected !== false || response.url !== url || !response.ok) fail("The dashboard could not be read.");
    const text = await response.text();
    if (encoder.encode(text).length > 65536) fail("The dashboard response was invalid.");
    try { return validateSummary(JSON.parse(text)); } catch (_) { fail("The dashboard response was invalid."); }
  }

  const contract = { buildSnapshot, publicPreference, summaryRequest, validateSummary };
  if (typeof module !== "undefined" && module.exports) { module.exports = contract; return; }

  async function run() {
    const auth = window.NewsCuratorAuth;
    const personalization = window.NewsCuratorPersonalization;
    const readerFactory = window.NewsCuratorReaderApi;
    if (!auth || !personalization || !readerFactory) return;
    const signedOut = document.getElementById("signed-out");
    const privateRoot = document.getElementById("private-dashboard");
    const status = document.getElementById("dashboard-status");
    const savedList = document.getElementById("saved-list");
    const savedEmpty = document.getElementById("saved-empty");
    const search = document.getElementById("saved-search");
    const load = document.getElementById("load-saved");
    let api, summary, preference, pageSize = 20, cursor = null, exhausted = false;
    let saved = [];
    let epoch = 0;
    let formDirty = false;
    let preferenceBusy = false;
    let savedLoading = false;
    let summaryFresh = false;
    let summaryGeneration = 0;
    const expanded = new Set();
    const confirmed = new Map();
    const desired = new Map();
    const active = new Set();

    function announce(message) { status.textContent = message; }
    function updateDownloadState() {
      document.getElementById("download-view").disabled = formDirty || preferenceBusy || savedLoading || !summaryFresh || active.size > 0 || !summary || !preference;
    }
    function setPreferenceBusy(busy) {
      preferenceBusy = busy;
      ["interest-list", "add-search", "save-preferences", "reload-preferences"].forEach((id) => { document.getElementById(id).disabled = busy; });
      document.querySelectorAll("#saved-searches input,#saved-searches button").forEach((control) => { control.disabled = busy; });
      updateDownloadState();
    }
    function clearPrivate() {
      epoch += 1; summaryGeneration += 1; summary = null; summaryFresh = false; preference = null; cursor = null; exhausted = false; saved = [];
      expanded.clear(); confirmed.clear(); desired.clear(); active.clear();
      savedList.replaceChildren(); document.getElementById("interest-list").value = "";
      document.getElementById("saved-searches").replaceChildren(); document.getElementById("topic-signals").replaceChildren();
      ["metric-saved", "metric-unread", "metric-read", "metric-signals", "insights-time"].forEach((id) => { document.getElementById(id).textContent = ""; });
      search.value = ""; formDirty = false; preferenceBusy = false; savedLoading = false; privateRoot.hidden = true; signedOut.hidden = false; updateDownloadState();
    }
    function shownRows() {
      const query = search.value.trim().toLocaleLowerCase();
      return query ? saved.filter((row) => `${row.title} ${row.summary} ${row.source_name}`.toLocaleLowerCase().includes(query)) : [...saved];
    }
    function makeButton(label, className) { const button = document.createElement("button"); button.type = "button"; button.className = className; button.textContent = label; return button; }
    function renderSaved() {
      const rows = shownRows(); savedList.replaceChildren();
      rows.forEach((row) => {
        const card = document.createElement("article"); card.className = `saved-card${row.read_at ? " is-read" : ""}`; card.dataset.storyId = row.story_id;
        const heading = document.createElement("h3"), toggle = makeButton(row.title, "saved-toggle");
        toggle.setAttribute("aria-expanded", "false"); heading.append(toggle);
        const detail = document.createElement("div"); detail.className = "saved-detail"; detail.hidden = !expanded.has(row.story_id);
        toggle.setAttribute("aria-expanded", String(!detail.hidden));
        const source = document.createElement("p"); source.textContent = row.source_name;
        const text = document.createElement("p"); text.textContent = row.summary;
        const actions = document.createElement("div"); actions.className = "saved-actions";
        if (row.canonical_url) {
          const original = document.createElement("a"); original.href = row.canonical_url;
          original.target = "_blank"; original.rel = "noopener noreferrer";
          original.referrerPolicy = "no-referrer"; original.textContent = "Read original";
          actions.append(original);
        }
        const read = makeButton("Mark unread", "read-action"); read.hidden = !row.read_at;
        const unsave = makeButton("Unsave", "save-action"); actions.append(read, unsave); detail.append(source, text, actions); card.append(heading, detail);
        async function drainMutations() {
          if (active.has(row.story_id)) return;
          const requestEpoch = epoch;
          active.add(row.story_id);
          summaryFresh = false;
          updateDownloadState();
          while (desired.has(row.story_id)) {
            const wanted = desired.get(row.story_id); desired.delete(row.story_id);
            const baseline = confirmed.get(row.story_id);
            try {
              const result = await api.setStoryState(row.story_id, wanted.read, wanted.saved, baseline.state_revision, crypto.randomUUID());
              if (requestEpoch !== epoch) return;
              if (result.status === "conflict") { announce("This story changed elsewhere. Reload the dashboard."); Object.assign(row, baseline, { state_revision: result.revision }); desired.delete(row.story_id); break; }
              const next = { read_at: result.read_at, saved_at: result.saved_at, state_revision: result.state_revision };
              confirmed.set(row.story_id, next); Object.assign(row, next);
              if (!wanted.saved) { saved = saved.filter((item) => item.story_id !== row.story_id); expanded.delete(row.story_id); desired.delete(row.story_id); }
              announce("Saved story updated.");
            } catch (_) { if (requestEpoch !== epoch) return; Object.assign(row, baseline); desired.delete(row.story_id); announce("The story could not be updated. Try again."); break; }
            const queued = desired.get(row.story_id);
            if (queued) { row.read_at = queued.read ? (row.read_at || "local") : null; row.saved_at = queued.saved ? (row.saved_at || "local") : null; }
          }
          active.delete(row.story_id);
          if (requestEpoch !== epoch) return;
          const generation = ++summaryGeneration;
          try {
            const nextSummary = await summaryRequest(auth);
            if (requestEpoch !== epoch || generation !== summaryGeneration) return;
            summary = nextSummary; summaryFresh = true;
          } catch (_) {
            if (requestEpoch !== epoch || generation !== summaryGeneration) return;
            summaryFresh = false; announce("Story updated. Reload the dashboard before downloading current totals.");
          }
          if (summary) renderSummary(); renderSaved(); updateDownloadState();
        }
        function mutate(nextRead, nextSaved) {
          desired.set(row.story_id, { read: nextRead, saved: nextSaved });
          row.read_at = nextRead ? (row.read_at || "local") : null; row.saved_at = nextSaved ? (row.saved_at || "local") : null;
          renderSaved(); void drainMutations();
        }
        toggle.addEventListener("click", () => {
          if (detail.hidden) expanded.add(row.story_id); else expanded.delete(row.story_id);
          detail.hidden = !detail.hidden; toggle.setAttribute("aria-expanded", String(!detail.hidden));
          if (!detail.hidden && !row.read_at) mutate(true, true);
        });
        read.addEventListener("click", () => { mutate(false, true); });
        unsave.addEventListener("click", () => { mutate(Boolean(row.read_at), false); });
        savedList.append(card);
      });
      savedEmpty.hidden = rows.length > 0; load.hidden = exhausted; load.textContent = `Load ${pageSize} more`;
    }
    function appendSearchRow(item) {
      const container = document.getElementById("saved-searches");
      const row = document.createElement("div"); row.className = "search-row"; row.dataset.id = item.id;
      const enabled = document.createElement("input"); enabled.type = "checkbox"; enabled.checked = item.enabled; enabled.setAttribute("aria-label", "Use this saved search");
      const query = document.createElement("input"); query.value = item.query; query.maxLength = 300; query.setAttribute("aria-label", "Saved search query");
      const remove = makeButton("Remove", "remove-search text-button"); remove.addEventListener("click", () => { row.remove(); formDirty = true; updateDownloadState(); });
      [enabled, query].forEach((control) => control.addEventListener("input", () => { formDirty = true; updateDownloadState(); }));
      row.append(enabled, query, remove); container.append(row); return row;
    }
    function renderPreferences() {
      document.getElementById("interest-list").value = preference.interests.join("\n");
      const container = document.getElementById("saved-searches"); container.replaceChildren();
      preference.saved_searches.forEach(appendSearchRow); formDirty = false; updateDownloadState();
    }
    function renderSummary() {
      document.getElementById("metric-saved").textContent = String(summary.saved_count);
      document.getElementById("metric-unread").textContent = String(summary.saved_unread_count);
      document.getElementById("metric-read").textContent = String(summary.read_count);
      document.getElementById("metric-signals").textContent = String(summary.active_interest_signal_count);
      document.getElementById("insights-time").textContent = `Current records as of ${new Date(summary.snapshot_at).toLocaleString()}`;
      const signals = document.getElementById("topic-signals"); signals.replaceChildren();
      summary.topic_signals.forEach((item) => { const row = document.createElement("div"); row.className = "signal-row"; const name = document.createElement("span"); name.textContent = item.topic_id; const values = document.createElement("span"); values.textContent = `${item.more_like_count} More like, ${item.less_like_count} Less like`; row.append(name, values); signals.append(row); });
    }
    async function loadSaved(first) {
      const requestEpoch = epoch;
      savedLoading = true; updateDownloadState();
      try {
        const rows = await api.savedPage(first ? null : cursor, pageSize);
        if (requestEpoch !== epoch) return;
        const known = new Set(saved.map((row) => row.story_id)); rows.forEach((row) => { if (!known.has(row.story_id)) { saved.push(row); confirmed.set(row.story_id, { read_at: row.read_at, saved_at: row.saved_at, state_revision: row.state_revision }); } });
        exhausted = rows.length < pageSize; cursor = exhausted || !rows.length ? null : clone(rows.at(-1).next_cursor); renderSaved();
      } finally {
        if (requestEpoch === epoch) { savedLoading = false; updateDownloadState(); }
      }
    }
    async function loadDashboard() {
      const requestEpoch = epoch;
      const session = await auth.sessionForRequest(); if (requestEpoch !== epoch) return;
      if (!session) { clearPrivate(); announce("Sign in to open your private dashboard."); return; }
      const nextApi = readerFactory.create(); const latest = await nextApi.latestPublication(); if (!latest) fail("The current edition is unavailable.");
      if (requestEpoch !== epoch) return;
      const [nextSummary, nextPreference] = await Promise.all([summaryRequest(auth), personalization.get()]);
      if (requestEpoch !== epoch) return;
      api = nextApi; pageSize = latest.page_size; summary = nextSummary; summaryFresh = true; savedLoading = true;
      preference = nextPreference || { revision: 0, locale: "en", interests: [], saved_searches: [], created_at: null, updated_at: null };
      signedOut.hidden = true; privateRoot.hidden = false; renderSummary(); renderPreferences(); await loadSaved(true);
      if (requestEpoch === epoch) announce("Your dashboard is up to date.");
    }
    async function attemptLoad() {
      const requestEpoch = epoch;
      try { await loadDashboard(); } catch (_) {
        if (requestEpoch !== epoch) return;
        clearPrivate(); auth.accountUnavailable?.(); announce("Your dashboard could not be loaded. Check sign-in and try again.");
      }
    }
    search.addEventListener("input", renderSaved);
    document.querySelector(".rail nav").addEventListener("focusin", (event) => {
      if (event.target instanceof HTMLElement) {
        event.target.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    });
    load.addEventListener("click", () => { const requestEpoch = epoch; load.disabled = true; void loadSaved(false).catch(() => { if (requestEpoch === epoch) announce("More Saved stories could not be loaded."); }).finally(() => { if (requestEpoch === epoch) load.disabled = false; }); });
    document.getElementById("interest-list").addEventListener("input", () => { formDirty = true; updateDownloadState(); });
    document.getElementById("add-search").addEventListener("click", () => { const row = appendSearchRow({ id: `search-${crypto.randomUUID()}`, query: "", enabled: true }); formDirty = true; updateDownloadState(); row.querySelector('input[aria-label="Saved search query"]').focus(); });
    document.getElementById("reload-preferences").addEventListener("click", () => { const requestEpoch = epoch; setPreferenceBusy(true); void personalization.get().then((value) => { if (requestEpoch !== epoch) return; preference = value || preference; renderPreferences(); announce("Latest interests loaded."); }).catch(() => { if (requestEpoch === epoch) announce("Interests could not be loaded."); }).finally(() => { if (requestEpoch === epoch) setPreferenceBusy(false); }); });
    document.getElementById("save-preferences").addEventListener("click", async () => {
      const requestEpoch = epoch;
      const interests = document.getElementById("interest-list").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
      const searches = [...document.querySelectorAll(".search-row")].map((row) => ({ id: row.dataset.id, query: row.querySelector('input[aria-label="Saved search query"]').value.trim(), enabled: row.querySelector('input[type="checkbox"]').checked })).filter((item) => item.query);
      setPreferenceBusy(true);
      try { const result = await personalization.set({ expected_revision: preference.revision, locale: preference.locale, interests, saved_searches: searches }); if (requestEpoch !== epoch) return; if (result.status === "conflict" || result.status === "not_found") { announce("Your interests changed elsewhere. Reload before saving."); return; } preference = result.preference; renderPreferences(); announce("Your interests were saved."); } catch (_) { if (requestEpoch === epoch) announce("Your interests could not be saved. Try again."); } finally { if (requestEpoch === epoch) setPreferenceBusy(false); }
    });
    document.getElementById("download-view").addEventListener("click", () => {
      const snapshot = buildSnapshot({ summary, preference, loadedItems: saved, displayedItems: shownRows(), pageSize, exhausted, nextCursor: cursor });
      const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = "news-curator-loaded-view.json"; link.click(); URL.revokeObjectURL(url);
    });
    window.addEventListener("news-curator:auth-changed", () => { clearPrivate(); void attemptLoad(); });
    await attemptLoad();
  }
  void run();
})();
