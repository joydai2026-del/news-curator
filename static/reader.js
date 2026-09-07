(() => {
  "use strict";

  const PAGE_SIZE = 20;
  const UPDATE_LIMIT = 20;
  const HISTORY_RANK_OFFSET = 1000000;
  const MAX_RESPONSE_BYTES = 256 * 1024;
  const STORY_ID = /^story:[0-9a-f]{64}$/;
  const TOPIC_ID = /^[a-z0-9][a-z0-9-]{0,79}$/;
  const ORDERING_MODES = new Set([
    "weighted_total", "preference_then_freshness", "native_rank_then_freshness",
  ]);
  const encoder = new TextEncoder();

  function fail(message) { throw new Error(message); }
  function isObject(value) { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }
  function exactFields(value, fields) {
    if (!isObject(value)) return false;
    const actual = Object.keys(value).sort();
    const expected = [...fields].sort();
    return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
  }
  function boundedString(value, max) {
    return typeof value === "string" && value.length > 0 && value.length <= max;
  }
  function validTimestamp(value) {
    return boundedString(value, 64) && Number.isFinite(Date.parse(value));
  }
  function validNullableTimestamp(value) { return value === null || validTimestamp(value); }
  function safeDestination(value) {
    if (!boundedString(value, 2048)) return null;
    try {
      const url = new URL(value);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
      return url.toString();
    } catch (_) {
      return null;
    }
  }
  function validateCoverageMention(value) {
    if (!exactFields(value, ["headline", "mentioned_at", "source_id", "source_kind", "source_name", "url"]) ||
        !["outlet", "newsletter"].includes(value.source_kind) ||
        !boundedString(value.source_id, 160) || !boundedString(value.source_name, 200) ||
        !boundedString(value.headline, 2000) || !validTimestamp(value.mentioned_at)) {
      fail("The feed response was invalid.");
    }
    const url = safeDestination(value.url);
    if (!url) fail("The feed response was invalid.");
    return { ...value, url };
  }
  function validateCursor(value, message = "The cursor response was invalid.", allowEmptyStory = false) {
    if (value === null) return null;
    if (!exactFields(value, ["before_published_at", "before_story_id"]) ||
        !validTimestamp(value.before_published_at) ||
        ((!allowEmptyStory || value.before_story_id !== "") && !STORY_ID.test(value.before_story_id))) fail(message);
    return { before_published_at: value.before_published_at, before_story_id: value.before_story_id };
  }
  function validateLatestPublication(value) {
    if (exactFields(value, [])) return null;
    if (!exactFields(value, ["finalized_at", "initial_history_cursor", "poll_seconds", "publication_seq", "topics"]) ||
        !Number.isSafeInteger(value.publication_seq) || value.publication_seq < 0 ||
        !validTimestamp(value.finalized_at) || !Number.isInteger(value.poll_seconds) ||
        value.poll_seconds < 15 || value.poll_seconds > 86400 ||
        !Array.isArray(value.topics) || value.topics.length > 100) {
      fail("The publication response was invalid.");
    }
    const topics = value.topics.map((topic) => {
      if (!exactFields(topic, ["name", "topic_id"]) || !TOPIC_ID.test(topic.topic_id) ||
          !boundedString(topic.name, 120)) fail("The publication response was invalid.");
      return { topic_id: topic.topic_id, name: topic.name };
    });
    return {
      publication_seq: value.publication_seq,
      finalized_at: value.finalized_at,
      topics,
      initial_history_cursor: validateCursor(
        value.initial_history_cursor,
        "The publication response was invalid.",
        true,
      ),
      poll_seconds: value.poll_seconds,
    };
  }
  const CARD_FIELDS = [
    "canonical_url", "coverage_mentions", "language", "next_cursor", "ordering_key", "ordering_mode",
    "page_order_mode", "position", "publication_seq", "published_at", "score_components", "story_id",
    "summary", "title", "topic_ids", "topic_ranks", "source_kind", "source_name",
    "ranking_explanation", "read_at", "saved_at", "state_revision", "interests",
  ];
  function validateStory(value, pageModes) {
    if (!isObject(value) || !exactFields(value, CARD_FIELDS) ||
        !STORY_ID.test(value.story_id) ||
        (value.canonical_url !== "" && !safeDestination(value.canonical_url)) ||
        !boundedString(value.title, 2000) || typeof value.summary !== "string" || value.summary.length > 8000 ||
        !["en", "zh"].includes(value.language) || !validTimestamp(value.published_at) ||
        !Number.isSafeInteger(value.publication_seq) || value.publication_seq < 0 ||
        !Number.isSafeInteger(value.position) || value.position < 0 ||
        !ORDERING_MODES.has(value.ordering_mode) || !isObject(value.ordering_key) ||
        !pageModes.includes(value.page_order_mode) || !isObject(value.next_cursor) ||
        encoder.encode(JSON.stringify(value.ordering_key)).length > 2048 || !isObject(value.score_components) ||
        encoder.encode(JSON.stringify(value.score_components)).length > 8192 ||
        !Array.isArray(value.topic_ids) || value.topic_ids.length < 1 || value.topic_ids.length > 20 ||
        !value.topic_ids.every((topic) => TOPIC_ID.test(topic)) ||
        !isObject(value.topic_ranks) || Object.keys(value.topic_ranks).length > 100 ||
        !Object.entries(value.topic_ranks).every(([topic, position]) =>
          TOPIC_ID.test(topic) && Number.isSafeInteger(position) && position > 0) ||
        !["outlet", "newsletter"].includes(value.source_kind) ||
        !boundedString(value.source_name, 200) || !boundedString(value.ranking_explanation, 2000) ||
        !Array.isArray(value.coverage_mentions) || value.coverage_mentions.length > 20 ||
        !validNullableTimestamp(value.read_at) || !validNullableTimestamp(value.saved_at) ||
        !Number.isSafeInteger(value.state_revision) || value.state_revision < 0 ||
        !Array.isArray(value.interests) || value.interests.length > 20 ||
        encoder.encode(JSON.stringify(value.coverage_mentions)).length > 32768) {
      fail("The feed response was invalid.");
    }
    value.coverage_mentions.forEach(validateCoverageMention);
    value.interests.forEach((interest) => {
      if (!exactFields(interest, ["revision", "signal", "topic_id"]) || !TOPIC_ID.test(interest.topic_id) ||
          interest.signal !== "more_like" || !Number.isSafeInteger(interest.revision) || interest.revision < 0) {
        fail("The feed response was invalid.");
      }
    });
    if (value.page_order_mode === "edition_rank") {
      if (!exactFields(value.next_cursor, ["after_position", "after_story_id"]) ||
          !Number.isSafeInteger(value.next_cursor.after_position) || value.next_cursor.after_position < 0 ||
          !STORY_ID.test(value.next_cursor.after_story_id)) fail("The feed response was invalid.");
    } else if (value.page_order_mode === "history_freshness") {
      validateCursor(value.next_cursor, "The feed response was invalid.");
    } else if (!exactFields(value.next_cursor, ["before_saved_at", "before_story_id"]) ||
               !validTimestamp(value.next_cursor.before_saved_at) ||
               !STORY_ID.test(value.next_cursor.before_story_id)) {
      fail("The feed response was invalid.");
    }
    return value;
  }
  function validateCardPage(value, pageModes) {
    if (!Array.isArray(value) || value.length > PAGE_SIZE) fail("The feed response was invalid.");
    const seen = new Set();
    return value.map((row) => {
      const checked = validateStory(row, pageModes);
      if (seen.has(checked.story_id)) fail("The feed response was invalid.");
      seen.add(checked.story_id);
      return checked;
    });
  }
  function validateFeedPage(value) {
    return validateCardPage(value, ["edition_rank", "history_freshness"]);
  }
  function validateSavedPage(value) {
    return validateCardPage(value, ["saved_at"]);
  }
  function validateUpdates(value) {
    if (!Array.isArray(value) || value.length > UPDATE_LIMIT) fail("The updates response was invalid.");
    return value.map((row) => {
      if (!exactFields(row, ["next_cursor", "publication_seq", "published_at", "story_id", "title", "topic_ids"]) ||
          !STORY_ID.test(row.story_id) || !boundedString(row.title, 2000) ||
          !validTimestamp(row.published_at) || !Number.isSafeInteger(row.publication_seq) ||
          row.publication_seq < 0 || !Array.isArray(row.topic_ids) || row.topic_ids.length > 20 ||
          !row.topic_ids.every((topic) => TOPIC_ID.test(topic)) ||
          !exactFields(row.next_cursor, ["after_publication_seq", "after_published_at", "after_story_id"]) ||
          !Number.isSafeInteger(row.next_cursor.after_publication_seq) || row.next_cursor.after_publication_seq < 0 ||
          !validTimestamp(row.next_cursor.after_published_at) || !STORY_ID.test(row.next_cursor.after_story_id)) {
        fail("The updates response was invalid.");
      }
      return row;
    });
  }
  function validateStoryState(value) {
    if (!isObject(value) || !["updated", "conflict"].includes(value.status) ||
        !Number.isSafeInteger(value.revision) || value.revision < 0) fail("The story state response was invalid.");
    if (value.status === "conflict") {
      if (!exactFields(value, ["revision", "status"])) fail("The story state response was invalid.");
      return value;
    }
    if (!exactFields(value, ["read_at", "revision", "saved_at", "status"]) ||
        !validNullableTimestamp(value.read_at) || !validNullableTimestamp(value.saved_at)) {
      fail("The story state response was invalid.");
    }
    return { status: "updated", read_at: value.read_at, saved_at: value.saved_at, state_revision: value.revision };
  }
  function validateInterest(value) {
    if (!isObject(value) || !["updated", "conflict"].includes(value.status) ||
        !Number.isSafeInteger(value.revision) || value.revision < 0) {
      fail("The story interest response was invalid.");
    }
    if (value.status === "conflict") {
      if (!exactFields(value, ["revision", "status"])) fail("The story interest response was invalid.");
      return value;
    }
    if (!exactFields(value, ["revision", "signal", "status"]) || value.signal !== "more_like") {
      fail("The story interest response was invalid.");
    }
    return { status: "updated", interest_signal: value.signal, interest_revision: value.revision };
  }
  async function boundedJson(response, message) {
    const text = await response.text();
    if (encoder.encode(text).length > MAX_RESPONSE_BYTES) fail(message);
    try { return JSON.parse(text); } catch (_) { fail(message); }
  }
  function validateApiConfig(config) {
    if (!exactFields(config, ["key", "url"]) || !boundedString(config.key, 8192)) fail("Reader configuration is invalid.");
    let url;
    try { url = new URL(config.url); } catch (_) { fail("Reader configuration is invalid."); }
    if (url.protocol !== "https:" || url.username || url.password || url.origin !== config.url ||
        url.pathname !== "/" || url.search || url.hash) fail("Reader configuration is invalid.");
    return { url: url.origin, key: config.key };
  }
  function createApi(rawConfig, sessionProvider, fetchImpl = fetch) {
    const config = validateApiConfig(rawConfig);
    async function rpc(name, body, validator, requiresAuth = false) {
      let session = null;
      try {
        session = await sessionProvider();
      } catch (_) {
        if (requiresAuth) fail("Sign in to continue.");
      }
      if (requiresAuth && (!session || !boundedString(session.access_token, 16384))) fail("Sign in to continue.");
      const requestedUrl = `${config.url}/rest/v1/rpc/${name}`;
      const headers = { apikey: config.key, accept: "application/json", "content-type": "application/json" };
      if (session && boundedString(session.access_token, 16384)) headers.authorization = `Bearer ${session.access_token}`;
      const response = await fetchImpl(requestedUrl, {
        method: "POST", headers, body: JSON.stringify(body), credentials: "omit",
        referrerPolicy: "no-referrer", redirect: "error",
      });
      if (response.redirected !== false || response.url !== requestedUrl) fail("The reader endpoint redirected unexpectedly.");
      const payload = await boundedJson(response, "The reader response was invalid.");
      if (!response.ok) fail("The reader request failed.");
      return validator(payload, Boolean(session));
    }
    return Object.freeze({
      latestPublication: () => rpc("latest_publication", {}, validateLatestPublication),
      feedPage: (topicId, cursor) => rpc("feed_page", {
        p_topic_id: topicId === "__all__" ? null : topicId,
        p_order_mode: cursor ? cursor.order_mode : (topicId === "__all__" ? "history_freshness" : "edition_rank"),
        p_after_position: cursor && cursor.order_mode === "edition_rank" ? cursor.after_position : null,
        p_after_story_id: cursor && cursor.order_mode === "edition_rank" ? cursor.after_story_id : null,
        p_before_published_at: cursor && cursor.order_mode === "history_freshness" ? cursor.before_published_at : null,
        p_before_story_id: cursor && cursor.order_mode === "history_freshness" ? cursor.before_story_id : null,
        p_limit: PAGE_SIZE,
      }, validateFeedPage),
      savedPage: (cursor) => rpc("saved_page", {
        p_before_saved_at: cursor ? cursor.before_saved_at : null,
        p_before_story_id: cursor ? cursor.before_story_id : null,
        p_limit: PAGE_SIZE,
      }, validateSavedPage, true),
      updatesSince: (publicationSeq, cursor = null) => rpc("updates_since", {
        p_since_publication_seq: publicationSeq,
        p_after_publication_seq: cursor ? cursor.after_publication_seq : null,
        p_after_published_at: cursor ? cursor.after_published_at : null,
        p_after_story_id: cursor ? cursor.after_story_id : null,
        p_limit: UPDATE_LIMIT,
      }, validateUpdates),
      setStoryState: (storyId, read, saved, revision, idempotencyKey) => rpc("set_story_state", {
        p_story_id: storyId, p_read: read, p_saved: saved,
        p_expected_revision: revision, p_idempotency_key: idempotencyKey,
      }, validateStoryState, true),
      setStoryInterest: (storyId, topicId, revision, idempotencyKey) => rpc("set_story_interest", {
        p_story_id: storyId, p_topic_id: topicId, p_signal: "more_like",
        p_expected_revision: revision, p_idempotency_key: idempotencyKey,
      }, validateInterest, true),
    });
  }
  function mergeTopicMembership(card, topicIds) {
    const merged = new Set((card.dataset.topicIds || "").split(/\s+/).filter(Boolean));
    topicIds.forEach((topic) => merged.add(topic));
    const value = [...merged].sort().join(" ");
    card.dataset.topicIds = value;
    card.dataset.topics = value;
  }
  function applyServerRank(card, row, selectedTopic, topicSlug = (value) => value) {
    Object.entries(row.topic_ranks || {}).forEach(([topic, position]) => {
      card.setAttribute(`data-rank-${topicSlug(topic)}`, String(position));
    });
    if (row.page_order_mode === "history_freshness" &&
               (!card.hasAttribute || !card.hasAttribute("data-rank-all"))) {
      card.setAttribute("data-rank-all", String(HISTORY_RANK_OFFSET + row.position));
    }
  }
  function effectiveTopic(topicIds, selectedTopic) {
    if (TOPIC_ID.test(selectedTopic || "") && topicIds.includes(selectedTopic)) return selectedTopic;
    return [...topicIds].sort()[0];
  }
  function nextFeedCursor(rows, initialCursor = null, currentCursor = null) {
    if (!rows.length) {
      return currentCursor && currentCursor.order_mode === "history_freshness"
        ? null
        : (initialCursor ? { order_mode: "history_freshness", ...initialCursor } : null);
    }
    const last = rows.at(-1);
    if (last.page_order_mode === "edition_rank" && rows.length < PAGE_SIZE) {
      return { order_mode: "history_freshness", ...(initialCursor || {}) };
    }
    return { order_mode: last.page_order_mode, ...last.next_cursor };
  }
  function nextSavedCursor(rows) {
    if (!rows.length) return null;
    return { ...rows.at(-1).next_cursor };
  }
  function loadedStatus(count) {
    return count === 1 ? "1 older story loaded." : `${count} older stories loaded.`;
  }
  function applyServerState(card, state) {
    const hasRead = Object.prototype.hasOwnProperty.call(state, "read_at");
    const hasSaved = Object.prototype.hasOwnProperty.call(state, "saved_at");
    const interestButton = card.querySelector(".interest-action");
    const topicId = interestButton && interestButton.dataset && interestButton.dataset.topicId;
    const matchingInterest = Array.isArray(state.interests)
      ? state.interests.find((interest) => interest.topic_id === topicId)
      : null;
    const hasInterest = Array.isArray(state.interests) ||
      Object.prototype.hasOwnProperty.call(state, "interest_signal");
    const interestSignal = matchingInterest ? matchingInterest.signal : state.interest_signal;
    const interestRevision = matchingInterest ? matchingInterest.revision : state.interest_revision;
    const read = hasRead && Boolean(state.read_at);
    const saved = hasSaved && Boolean(state.saved_at);
    const interested = hasInterest && interestSignal === "more_like";
    if (hasRead) card.classList.toggle("is-read", read);
    if (hasSaved) card.classList.toggle("is-saved", saved);
    if (hasInterest) card.classList.toggle("is-more-like", interested);
    if (Number.isSafeInteger(state.state_revision)) card.dataset.stateRevision = String(state.state_revision);
    if (Number.isSafeInteger(interestRevision)) card.dataset.interestRevision = String(interestRevision);
    else if (Array.isArray(state.interests)) card.dataset.interestRevision = "0";
    const readButton = card.querySelector(".read-action");
    const saveButton = card.querySelector(".save-action");
    if (readButton && hasRead) readButton.textContent = read ? "Mark unread" : "Mark read";
    if (saveButton && hasSaved) {
      saveButton.textContent = saved ? "Unsave" : "Save";
      saveButton.setAttribute("aria-pressed", String(saved));
    }
    if (interestButton && hasInterest) {
      interestButton.textContent = interested ? "More like this added" : "More like this";
      interestButton.setAttribute("aria-pressed", String(interested));
    }
  }
  function idempotencyKey() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  function element(name, className, text) {
    const node = document.createElement(name);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function createStoryCard(
    row, selectedTopic, topicSlug = (value) => value, selectedTopicId = selectedTopic,
  ) {
    const card = element("article", "card");
    card.dataset.storyId = row.story_id;
    mergeTopicMembership(card, row.topic_ids.map(topicSlug));
    card.dataset.topicApiIds = [...row.topic_ids].sort().join(" ");
    applyServerRank(card, row, selectedTopic, topicSlug);
    const heading = element("h2", "story-heading");
    const toggle = element("button", "accordion-toggle");
    const suffix = row.story_id.slice(-12);
    toggle.type = "button";
    toggle.id = `reader-toggle-${suffix}`;
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", `reader-detail-${suffix}`);
    toggle.append(element("span", "headline", row.title), element("span", "chev", "⌄"));
    toggle.lastChild.setAttribute("aria-hidden", "true");
    heading.append(toggle);
    const detail = element("div", "panel detail");
    detail.id = `reader-detail-${suffix}`;
    detail.hidden = true;
    detail.setAttribute("role", "region");
    detail.setAttribute("aria-labelledby", toggle.id);
    const panel = element("div", "panelin");
    const summary = element("div", "summary");
    summary.append(element("p", "full", row.summary));
    const details = element("div", "details");
    const source = element("div", "row");
    source.append(
      element("b", "", row.source_kind === "newsletter" ? "Newsletter" : "Source"),
      element("span", "", row.source_name),
    );
    const published = element("div", "row");
    published.append(
      element("b", "", "Published"),
      element("span", "", new Date(row.published_at).toLocaleString()),
    );
    details.append(source, published);
    if (row.coverage_mentions.length) {
      const coverage = element("div", "row");
      coverage.append(element("b", "", "Also covered by"));
      const mentions = element("span");
      row.coverage_mentions.forEach((mention, index) => {
        if (index) mentions.append(document.createTextNode(", "));
        const link = element("a", "", mention.source_name);
        link.href = mention.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer nofollow";
        link.title = mention.headline;
        mentions.append(link);
      });
      coverage.append(mentions);
      details.append(coverage);
    }
    summary.append(details);
    const actions = element("div", "acts");
    const destination = safeDestination(row.canonical_url);
    if (destination) {
      const link = element("a", "", "Read original");
      link.href = destination;
      link.target = "_blank";
      link.rel = "noopener noreferrer nofollow";
      actions.append(link);
    }
    const read = element("button", "state-action read-action", "Mark read");
    const save = element("button", "state-action save-action", "Save");
    const interest = element("button", "state-action interest-action", "More like this");
    [read, save, interest].forEach((button) => { button.type = "button"; });
    save.setAttribute("aria-pressed", "false");
    interest.setAttribute("aria-pressed", "false");
    interest.dataset.topicId = effectiveTopic(row.topic_ids, selectedTopicId);
    const close = element("button", "shut", "Close");
    close.type = "button";
    actions.append(read, save, interest, close);
    summary.append(actions);
    const reason = element("aside", "signal");
    reason.append(
      element("b", "", "Ranking signals"),
      element("span", "", row.ranking_explanation),
    );
    panel.append(summary, reason);
    detail.append(panel);
    card.append(heading, detail);
    applyServerState(card, row);
    return card;
  }
  function rankingReason(orderingMode, orderingKey, components) {
    if (orderingMode === "preference_then_freshness") {
      return Object.keys(orderingKey).length
        ? "Your saved interests are considered first, then freshness."
        : "The preference-first ordering key was empty.";
    }
    if (orderingMode === "native_rank_then_freshness") {
      return Object.keys(orderingKey).length
        ? "The source's captured rank is considered first, then freshness."
        : "The source-rank ordering key was empty.";
    }
    const names = Object.entries(components)
      .filter((entry) => typeof entry[1] === "number" && Number.isFinite(entry[1]) && entry[1] !== 0)
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
      .slice(0, 3)
      .map((entry) => entry[0].replace(/_/g, " "));
    return names.length ? `Weighted using ${names.join(", ")}.` : "No non-zero weighted signal was returned.";
  }

  async function drainUpdates(api, publicationSeq, cursor, maxPages = 10) {
    const collected = [];
    let nextCursor = cursor;
    for (let page = 0; page < maxPages; page += 1) {
      const rows = await api.updatesSince(publicationSeq, nextCursor);
      collected.push(...rows);
      if (rows.length < UPDATE_LIMIT) return { rows: collected, cursor: null, drained: true };
      nextCursor = rows.at(-1).next_cursor;
    }
    return { rows: collected, cursor: nextCursor, drained: false };
  }

  const contract = {
    applyServerRank, applyServerState, createApi, createStoryCard, drainUpdates, effectiveTopic, loadedStatus, mergeTopicMembership, nextFeedCursor, nextSavedCursor,
    rankingReason, run, safeDestination,
    validateFeedPage, validateLatestPublication, validateUpdates,
  };
  const commonJs = typeof module !== "undefined" && module.exports;
  if (commonJs) {
    module.exports = contract;
  }

  async function run() {
    const auth = window.NewsCuratorAuth;
    const view = window.NewsCuratorView;
    const status = document.getElementById("reader-status");
    const loadButton = document.getElementById("load-more");
    const updatesStatus = document.getElementById("updates-status");
    const updatesButton = document.getElementById("show-updates");
    if (!auth || !view || !status || !loadButton || !updatesStatus || !updatesButton) return;
    let api;
    try { api = createApi(auth.config(), () => auth.sessionForRequest()); } catch (_) {
      loadButton.hidden = true;
      return;
    }
    const cards = new Map();
    document.querySelectorAll(".card[data-story-id]").forEach((card) => cards.set(card.dataset.storyId, card));
    const cursors = new Map();
    const exhausted = new Set();
    const hydrated = new Set();
    let publicationSeq = 0;
    let latest = null;
    let pollTimer = null;
    let updateCursor = null;
    const pendingUpdates = new Map();

    function announce(message) { status.textContent = message; }
    function signedIn() { try { return auth.hasSessionCandidate(); } catch (_) { return false; } }
    function requireSignIn() {
      if (signedIn()) return true;
      announce("Sign in to sync reading controls.");
      const link = document.querySelector(".profile-link");
      if (link) link.focus();
      return false;
    }
    function selectedTopic() { return view.currentTab(); }
    function topicIdForSlug(slug) {
      if (slug === "__all__" || slug === "__saved__") return slug;
      const chip = document.querySelector(`.chip[data-filter="${CSS.escape(slug)}"]`);
      return chip && chip.dataset.topicId ? chip.dataset.topicId : slug;
    }
    function topicSlugForId(topicId) {
      const chip = document.querySelector(`.chip[data-topic-id="${CSS.escape(topicId)}"]`);
      return chip && chip.dataset.filter ? chip.dataset.filter : topicId;
    }
    function sectionFor(topicId) {
      const slug = topicSlugForId(topicId);
      let section = document.querySelector(`.topic-section[data-section="${CSS.escape(slug)}"]`);
      if (section) return section;
      const topic = latest && latest.topics.find((entry) => entry.topic_id === topicId);
      section = element("section", "topic-section");
      section.dataset.section = slug;
      section.dataset.topicId = topicId;
      section.append(element("h2", "section-title", topic ? topic.name : topicId), element("div", "grid"));
      document.getElementById("sections").append(section);
      return section;
    }
    function mergeRows(rows, appendNew) {
      rows.forEach((row) => {
        const existing = cards.get(row.story_id);
        if (existing) {
          mergeTopicMembership(existing, row.topic_ids.map(topicSlugForId));
          existing.dataset.topicApiIds = [...row.topic_ids].sort().join(" ");
          applyServerRank(existing, row, selectedTopic(), topicSlugForId);
          applyServerState(existing, row);
          view.addCard(existing);
          return;
        }
        if (!appendNew) return;
        const selected = selectedTopic();
        const card = createStoryCard(
          row, selected, topicSlugForId, topicIdForSlug(selected),
        );
        cards.set(row.story_id, card);
        sectionFor(row.topic_ids[0]).querySelector(".grid").append(card);
        view.addCard(card);
      });
      view.apply();
    }
    async function hydrate(force = false) {
      const topic = selectedTopic();
      if (!force && hydrated.has(topic)) return;
      if (topic === "__saved__" && !signedIn()) {
        requireSignIn();
        return;
      }
      const wasHydrated = hydrated.has(topic);
      const initialCursor = topic === "__all__" ? { order_mode: "history_freshness" } : null;
      const rows = topic === "__saved__"
        ? await api.savedPage(null)
        : await api.feedPage(topicIdForSlug(topic), initialCursor);
      mergeRows(rows, true);
      const cursor = topic === "__saved__"
        ? nextSavedCursor(rows)
        : nextFeedCursor(rows, latest && latest.initial_history_cursor, initialCursor);
      if (!wasHydrated) {
        if (cursor) cursors.set(topic, cursor); else exhausted.add(topic);
        hydrated.add(topic);
      }
    }
    async function loadMore() {
      const topic = selectedTopic();
      if (exhausted.has(topic)) return;
      if (topic === "__saved__" && !requireSignIn()) return;
      loadButton.disabled = true;
      try {
        let rows;
        const currentCursor = cursors.get(topic) || null;
        if (topic === "__saved__") {
          rows = await api.savedPage(currentCursor);
          const cursor = nextSavedCursor(rows);
          if (cursor) cursors.set(topic, cursor);
        } else {
          rows = await api.feedPage(topicIdForSlug(topic), currentCursor);
          const cursor = nextFeedCursor(rows, latest && latest.initial_history_cursor, currentCursor);
          if (cursor) cursors.set(topic, cursor); else exhausted.add(topic);
        }
        mergeRows(rows, true);
        if (topic === "__saved__" && rows.length < PAGE_SIZE) exhausted.add(topic);
        announce(rows.length ? loadedStatus(rows.length) : "No older stories remain in this section.");
      } catch (_) {
        announce("Older stories could not be loaded. Try again.");
      } finally { loadButton.disabled = false; }
    }
    async function mutateState(card, read, saved) {
      const previous = {
        read_at: card.classList.contains("is-read") ? "local" : null,
        saved_at: card.classList.contains("is-saved") ? "local" : null,
        state_revision: Number(card.dataset.stateRevision || 0),
        interest_revision: Number(card.dataset.interestRevision || 0),
        interest_signal: card.classList.contains("is-more-like") ? "more_like" : null,
      };
      applyServerState(card, { ...previous, read_at: read ? "local" : null, saved_at: saved ? "local" : null });
      try {
        const result = await api.setStoryState(card.dataset.storyId, read, saved, previous.state_revision, idempotencyKey());
        if (result.status === "conflict") fail("Story state changed in another session.");
        applyServerState(card, { ...previous, ...result });
        announce("Reading state saved.");
      } catch (_) {
        applyServerState(card, previous);
        announce("Reading state could not be saved. Try again.");
      }
    }
    document.getElementById("sections").addEventListener("click", (event) => {
      const target = event.target.closest && event.target.closest("button");
      const card = event.target.closest && event.target.closest(".card[data-story-id]");
      if (!target || !card) return;
      if (target.classList.contains("accordion-toggle") && target.getAttribute("aria-expanded") === "true" &&
          !card.classList.contains("is-read") && requireSignIn()) {
        void mutateState(card, true, card.classList.contains("is-saved"));
      } else if (target.classList.contains("read-action") && requireSignIn()) {
        void mutateState(card, !card.classList.contains("is-read"), card.classList.contains("is-saved"));
      } else if (target.classList.contains("save-action") && requireSignIn()) {
        void mutateState(card, card.classList.contains("is-read"), !card.classList.contains("is-saved"));
      } else if (target.classList.contains("interest-action") && requireSignIn() &&
                 !card.classList.contains("is-more-like")) {
        const revision = Number(card.dataset.interestRevision || 0);
        target.disabled = true;
        api.setStoryInterest(card.dataset.storyId, target.dataset.topicId, revision, idempotencyKey())
          .then((result) => {
            if (result.status === "conflict") fail("Story interest changed in another session.");
            applyServerState(card, result);
            announce("More like this was saved for future rankings.");
          })
          .catch(() => { announce("More like this could not be saved. Try again."); })
          .finally(() => { target.disabled = false; });
      }
    });
    document.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        void hydrate().catch(() => { announce("This section could not be synced. Try again."); });
      });
    });
    loadButton.addEventListener("click", () => { void loadMore(); });
    updatesButton.addEventListener("click", () => { window.location.reload(); });
    if (typeof BroadcastChannel !== "undefined") {
      const channel = new BroadcastChannel(auth.channelName);
      channel.addEventListener("message", (event) => {
        if (!exactFields(event.data, ["session", "type"]) || event.data.type !== "session") return;
        try {
          auth.acceptSession(event.data.session);
          void hydrate(true).catch(() => { announce("Signed in, but reading state could not be synced."); });
          announce("Signed in. Reading state is syncing.");
        } catch (_) {}
      });
    }
    try {
      latest = await api.latestPublication();
      if (!latest) {
        loadButton.hidden = true;
        announce("No published edition is available yet.");
        return;
      }
      publicationSeq = latest.publication_seq;
      const poll = async () => {
        try {
          const current = await api.latestPublication();
          if (!current || current.publication_seq <= publicationSeq) return;
          const updatePage = await drainUpdates(api, publicationSeq, updateCursor);
          const rows = updatePage.rows;
          updateCursor = updatePage.cursor;
          if (!rows.length && updatePage.drained) {
            publicationSeq = current.publication_seq;
            return;
          }
          rows.forEach((row) => pendingUpdates.set(row.story_id, row));
          const target = Math.max(...rows.map((row) => row.publication_seq));
          const count = pendingUpdates.size;
          updatesButton.textContent = `${count} new ${count === 1 ? "story" : "stories"} available`;
          updatesButton.dataset.publicationSeq = String(target);
          updatesStatus.hidden = false;
          if (updatePage.drained) {
            publicationSeq = current.publication_seq;
          }
        } catch (_) {}
      };
      pollTimer = window.setInterval(poll, latest.poll_seconds * 1000);
      void pollTimer;
      await hydrate();
    } catch (_) { announce("Synced reading features are temporarily unavailable."); }
  }
  if (!commonJs) void run();
})();
