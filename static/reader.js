(() => {
  "use strict";

  const MAX_PAGE_SIZE = 100;
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
    if (!exactFields(value, ["finalized_at", "initial_history_cursor", "page_size", "poll_seconds", "publication_seq", "topics"]) ||
        !Number.isSafeInteger(value.publication_seq) || value.publication_seq < 0 ||
        !validTimestamp(value.finalized_at) || !Number.isInteger(value.poll_seconds) ||
        value.poll_seconds < 15 || value.poll_seconds > 86400 ||
        !Number.isInteger(value.page_size) || value.page_size < 1 || value.page_size > MAX_PAGE_SIZE ||
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
      page_size: value.page_size,
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
        (value.canonical_url === ""
          ? value.source_kind !== "newsletter"
          : !safeDestination(value.canonical_url)) ||
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
          !["more_like", "less_like"].includes(interest.signal) ||
          !Number.isSafeInteger(interest.revision) || interest.revision < 0) {
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
  function validatePageSize(value) {
    if (!Number.isInteger(value) || value < 1 || value > MAX_PAGE_SIZE) fail("The page size was invalid.");
    return value;
  }
  function validateCardPage(value, pageModes, pageSize = MAX_PAGE_SIZE) {
    if (!Array.isArray(value) || value.length > validatePageSize(pageSize)) fail("The feed response was invalid.");
    const seen = new Set();
    return value.map((row) => {
      const checked = validateStory(row, pageModes);
      if (seen.has(checked.story_id)) fail("The feed response was invalid.");
      seen.add(checked.story_id);
      return checked;
    });
  }
  function validateFeedPage(value, pageSize = MAX_PAGE_SIZE) {
    return validateCardPage(value, ["edition_rank", "history_freshness"], pageSize);
  }
  function validateSavedPage(value, pageSize = MAX_PAGE_SIZE) {
    return validateCardPage(value, ["saved_at"], pageSize);
  }
  function validateUpdates(value, pageSize = MAX_PAGE_SIZE) {
    if (!Array.isArray(value) || value.length > validatePageSize(pageSize)) fail("The updates response was invalid.");
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
      feedPage: (topicId, cursor, pageSize) => rpc("feed_page", {
        p_topic_id: topicId === "__all__" ? null : topicId,
        p_order_mode: cursor ? cursor.order_mode : (topicId === "__all__" ? "history_freshness" : "edition_rank"),
        p_after_position: cursor && cursor.order_mode === "edition_rank" ? cursor.after_position : null,
        p_after_story_id: cursor && cursor.order_mode === "edition_rank" ? cursor.after_story_id : null,
        p_before_published_at: cursor && cursor.order_mode === "history_freshness" ? cursor.before_published_at : null,
        p_before_story_id: cursor && cursor.order_mode === "history_freshness" ? cursor.before_story_id : null,
        p_limit: validatePageSize(pageSize),
      }, (payload) => validateFeedPage(payload, pageSize)),
      savedPage: (cursor, pageSize) => rpc("saved_page", {
        p_before_saved_at: cursor ? cursor.before_saved_at : null,
        p_before_story_id: cursor ? cursor.before_story_id : null,
        p_limit: validatePageSize(pageSize),
      }, (payload) => validateSavedPage(payload, pageSize), true),
      updatesSince: (publicationSeq, cursor, pageSize) => rpc("updates_since", {
        p_since_publication_seq: publicationSeq,
        p_after_publication_seq: cursor ? cursor.after_publication_seq : null,
        p_after_published_at: cursor ? cursor.after_published_at : null,
        p_after_story_id: cursor ? cursor.after_story_id : null,
        p_limit: validatePageSize(pageSize),
      }, (payload) => validateUpdates(payload, pageSize)),
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
  function applyServerRank(
    card, row, _selectedTopic, topicSlug = (value) => value, historyAllRank = null,
  ) {
    Object.entries(row.topic_ranks || {}).forEach(([topic, position]) => {
      card.setAttribute(`data-rank-${topicSlug(topic)}`, String(position));
    });
    if (Number.isSafeInteger(historyAllRank) && historyAllRank > HISTORY_RANK_OFFSET &&
        (!card.hasAttribute || !card.hasAttribute("data-rank-all"))) {
      card.setAttribute("data-rank-all", String(historyAllRank));
    }
  }
  function effectiveTopic(topicIds, selectedTopic) {
    if (TOPIC_ID.test(selectedTopic || "") && topicIds.includes(selectedTopic)) return selectedTopic;
    return [...topicIds].sort()[0];
  }
  function actionTopic(topicIds, selectedTopic, selectedTopicId, fallbackTopicId) {
    if (!["__all__", "__saved__"].includes(selectedTopic) &&
        TOPIC_ID.test(selectedTopicId || "") && topicIds.includes(selectedTopicId)) return selectedTopicId;
    if (TOPIC_ID.test(fallbackTopicId || "") && topicIds.includes(fallbackTopicId)) return fallbackTopicId;
    return effectiveTopic(topicIds, "__all__");
  }
  function nextFeedCursor(rows, initialCursor = null, currentCursor = null, pageSize = MAX_PAGE_SIZE) {
    validatePageSize(pageSize);
    const initialHistoryProbe = currentCursor && currentCursor.order_mode === "history_freshness" &&
      !currentCursor.before_published_at;
    if (!rows.length) {
      if (initialHistoryProbe && initialCursor) {
        return { order_mode: "history_freshness", ...initialCursor };
      }
      return currentCursor && currentCursor.order_mode === "history_freshness"
        ? null
        : (initialCursor ? { order_mode: "history_freshness", ...initialCursor } : null);
    }
    const last = rows.at(-1);
    if (last.page_order_mode === "edition_rank" && rows.length < pageSize) {
      return { order_mode: "history_freshness", ...(initialCursor || {}) };
    }
    if (last.page_order_mode === "history_freshness" && rows.length < pageSize) {
      return initialHistoryProbe && initialCursor
        ? { order_mode: "history_freshness", ...initialCursor }
        : null;
    }
    return { order_mode: last.page_order_mode, ...last.next_cursor };
  }
  function nextSavedCursor(rows, pageSize = MAX_PAGE_SIZE) {
    validatePageSize(pageSize);
    if (!rows.length || rows.length < pageSize) return null;
    return { ...rows.at(-1).next_cursor };
  }
  function loadedStatus(count) {
    return count === 1 ? "1 older story loaded." : `${count} older stories loaded.`;
  }
  function interestStates(card) {
    if (!(card.newsCuratorInterestStates instanceof Map)) {
      card.newsCuratorInterestStates = new Map();
    }
    return card.newsCuratorInterestStates;
  }
  function applyInterestTopic(card, topicId) {
    const interestButton = card.querySelector(".interest-action");
    if (!interestButton || !TOPIC_ID.test(topicId || "")) return;
    const state = interestStates(card).get(topicId) || { signal: null, revision: 0 };
    const interested = state.signal === "more_like";
    interestButton.dataset.topicId = topicId;
    interestButton.textContent = interested ? "More like this added" : "More like this";
    interestButton.setAttribute("aria-pressed", String(interested));
    card.classList.toggle("is-more-like", interested);
    card.dataset.interestRevision = String(state.revision);
  }
  function applyServerState(card, state, interestTopicId = null) {
    const hasRead = Object.prototype.hasOwnProperty.call(state, "read_at");
    const hasSaved = Object.prototype.hasOwnProperty.call(state, "saved_at");
    const interestButton = card.querySelector(".interest-action");
    const states = interestStates(card);
    if (Array.isArray(state.interests)) {
      states.clear();
      state.interests.forEach((interest) => {
        states.set(interest.topic_id, { signal: interest.signal, revision: interest.revision });
      });
    }
    if (Object.prototype.hasOwnProperty.call(state, "interest_signal") &&
        TOPIC_ID.test(interestTopicId || "") && Number.isSafeInteger(state.interest_revision)) {
      states.set(interestTopicId, {
        signal: state.interest_signal,
        revision: state.interest_revision,
      });
    }
    const read = hasRead && Boolean(state.read_at);
    const saved = hasSaved && Boolean(state.saved_at);
    if (hasRead) card.classList.toggle("is-read", read);
    if (hasSaved) card.classList.toggle("is-saved", saved);
    if (Number.isSafeInteger(state.state_revision)) card.dataset.stateRevision = String(state.state_revision);
    const readButton = card.querySelector(".read-action");
    const saveButton = card.querySelector(".save-action");
    if (readButton && hasRead) readButton.textContent = read ? "Mark unread" : "Mark read";
    if (saveButton && hasSaved) {
      saveButton.textContent = saved ? "Unsave" : "Save";
      saveButton.setAttribute("aria-pressed", String(saved));
    }
    if (interestButton && interestButton.dataset.topicId) {
      applyInterestTopic(card, interestButton.dataset.topicId);
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
  function sourceIdentity(sourceKind, sourceName) {
    return `${sourceKind}:${sourceName.trim().replace(/\s+/g, " ").toLowerCase()}`;
  }
  function createStoryCard(
    row, selectedTopic, topicSlug = (value) => value, selectedTopicId = selectedTopic,
    historyAllRank = null,
  ) {
    const card = element("article", "card");
    card.dataset.storyId = row.story_id;
    mergeTopicMembership(card, row.topic_ids.map(topicSlug));
    card.dataset.topicApiIds = [...row.topic_ids].sort().join(" ");
    applyServerRank(card, row, selectedTopic, topicSlug, historyAllRank);
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
    const primarySource = sourceIdentity(row.source_kind, row.source_name);
    const additionalMentions = row.coverage_mentions.filter((mention) =>
      sourceIdentity(mention.source_kind, mention.source_name) !== primarySource);
    if (additionalMentions.length) {
      const coverage = element("div", "row");
      coverage.append(element("b", "", "Also covered by"));
      const mentions = element("span");
      additionalMentions.forEach((mention, index) => {
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
    interest.dataset.fallbackTopicId = interest.dataset.topicId;
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

  async function drainUpdates(api, publicationSeq, cursor, pageSize, maxPages = 10) {
    validatePageSize(pageSize);
    const collected = [];
    let nextCursor = cursor;
    for (let page = 0; page < maxPages; page += 1) {
      const rows = await api.updatesSince(publicationSeq, nextCursor, pageSize);
      collected.push(...rows);
      if (rows.length < pageSize) return { rows: collected, cursor: null, drained: true };
      nextCursor = rows.at(-1).next_cursor;
    }
    return { rows: collected, cursor: nextCursor, drained: false };
  }

  const contract = {
    actionTopic, applyInterestTopic, applyServerRank, applyServerState, createApi, createStoryCard, drainUpdates, effectiveTopic, loadedStatus, mergeTopicMembership, nextFeedCursor, nextSavedCursor,
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
    document.querySelectorAll(".state-action").forEach((button) => { button.hidden = false; });
    const cards = new Map();
    document.querySelectorAll(".card[data-story-id]").forEach((card) => {
      card.newsCuratorStaticCard = true;
      card.newsCuratorPublicAttributes = {
        topicIds: card.dataset.topicIds || "",
        topicApiIds: card.dataset.topicApiIds || "",
        topics: card.dataset.topics || "",
        ranks: [...card.attributes]
          .filter((attribute) => attribute.name.startsWith("data-rank-"))
          .map((attribute) => [attribute.name, attribute.value]),
      };
      const interestButton = card.querySelector(".interest-action");
      if (interestButton && !interestButton.dataset.fallbackTopicId) {
        interestButton.dataset.fallbackTopicId = interestButton.dataset.topicId;
      }
      cards.set(card.dataset.storyId, card);
    });
    const cursors = new Map();
    const exhausted = new Set();
    const hydrated = new Set();
    let publicationSeq = 0;
    let latest = null;
    let pollTimer = null;
    let updateCursor = null;
    let nextHistoryAllRank = HISTORY_RANK_OFFSET;
    let authEpoch = 0;
    const authoritativeAllHistoryOrder = [];
    const authoritativeAllHistoryIds = new Set();
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
    function hydratedTopics(card) {
      if (!(card.newsCuratorHydratedTopics instanceof Set)) {
        card.newsCuratorHydratedTopics = new Set();
      }
      return card.newsCuratorHydratedTopics;
    }
    function stateReady(card) { return hydratedTopics(card).has(selectedTopic()); }
    function refreshStateControls() {
      cards.forEach((card) => {
        const disabled = !stateReady(card);
        card.querySelectorAll(".state-action").forEach((button) => { button.disabled = disabled; });
      });
    }
    function refreshInterestControls() {
      const topic = selectedTopic();
      cards.forEach((card) => {
        const button = card.querySelector(".interest-action");
        if (!button) return;
        const topicIds = (card.dataset.topicApiIds || "").split(/\s+/).filter(Boolean);
        const topicId = actionTopic(
          topicIds,
          topic,
          topicIdForSlug(topic),
          button.dataset.fallbackTopicId || button.dataset.topicId,
        );
        applyInterestTopic(card, topicId);
      });
    }
    function clearPrivateCardState(card) {
      card.classList.remove("is-read", "is-saved", "is-more-like", "is-less-like");
      interestStates(card).clear();
      hydratedTopics(card).clear();
      card.dataset.stateRevision = "0";
      card.dataset.interestRevision = "0";
      const readButton = card.querySelector(".read-action");
      const saveButton = card.querySelector(".save-action");
      const interestButton = card.querySelector(".interest-action");
      if (readButton) readButton.textContent = "Mark read";
      if (saveButton) {
        saveButton.textContent = "Save";
        saveButton.setAttribute("aria-pressed", "false");
      }
      if (interestButton) {
        interestButton.textContent = "More like this";
        interestButton.setAttribute("aria-pressed", "false");
      }
      const snapshot = card.newsCuratorPublicAttributes;
      if (snapshot) {
        card.dataset.topicIds = snapshot.topicIds;
        card.dataset.topicApiIds = snapshot.topicApiIds;
        card.dataset.topics = snapshot.topics;
        [...card.attributes]
          .filter((attribute) => attribute.name.startsWith("data-rank-"))
          .forEach((attribute) => card.removeAttribute(attribute.name));
        snapshot.ranks.forEach(([name, value]) => card.setAttribute(name, value));
      }
    }
    async function handleLogout() {
      authEpoch += 1;
      auth.clearSession();
      document.querySelectorAll(".state-action").forEach((button) => { button.disabled = true; });
      cards.forEach((card, storyId) => {
        if (!card.newsCuratorStaticCard) {
          cards.delete(storyId);
          card.replaceChildren();
          [...card.attributes].forEach((attribute) => card.removeAttribute(attribute.name));
          view.addCard(card);
          card.remove();
          return;
        }
        clearPrivateCardState(card);
      });
      cursors.clear();
      exhausted.clear();
      hydrated.clear();
      authoritativeAllHistoryOrder.length = 0;
      authoritativeAllHistoryIds.clear();
      nextHistoryAllRank = HISTORY_RANK_OFFSET;
      if (selectedTopic() === "__saved__") {
        const publicTab = document.querySelector('.chip[data-filter="__all__"]');
        if (publicTab) {
          hydrated.add("__all__");
          publicTab.click();
          hydrated.delete("__all__");
        }
      }
      refreshInterestControls();
      view.apply();
      announce("Signed out. Public stories are syncing.");
      try {
        await hydrate(true);
        announce("Signed out. Public stories are ready.");
      } catch (_) {
        announce("Signed out. Public stories could not be synced. Try again.");
      }
    }
    function invalidateHydrationForSession() {
      authEpoch += 1;
      document.querySelectorAll(".state-action").forEach((button) => { button.disabled = true; });
      cursors.clear();
      exhausted.clear();
      hydrated.clear();
      cards.forEach(clearPrivateCardState);
      refreshInterestControls();
      view.apply();
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
    function historySection() {
      let section = document.querySelector('.topic-section[data-section="__history__"]');
      if (section) return section;
      section = element("section", "topic-section");
      section.dataset.section = "__history__";
      section.append(element("div", "grid"));
      document.getElementById("sections").append(section);
      return section;
    }
    function reconcileAllHistoryRanks(rows) {
      rows.forEach((row) => {
        if (row.page_order_mode !== "history_freshness" ||
            authoritativeAllHistoryIds.has(row.story_id)) return;
        const card = cards.get(row.story_id);
        const currentRank = Number(card && card.getAttribute("data-rank-all"));
        // A current-edition card can also appear in history. Its static edition
        // rank remains authoritative and must never be moved behind the fold.
        if (Number.isSafeInteger(currentRank) && currentRank <= HISTORY_RANK_OFFSET) return;
        authoritativeAllHistoryIds.add(row.story_id);
        authoritativeAllHistoryOrder.push(row.story_id);
      });
      const provisional = [...cards.values()]
        .filter((card) => {
          const rank = Number(card.getAttribute("data-rank-all"));
          return Number.isSafeInteger(rank) && rank > HISTORY_RANK_OFFSET &&
            !authoritativeAllHistoryIds.has(card.dataset.storyId);
        })
        .sort((left, right) => {
          const rankDelta = Number(left.getAttribute("data-rank-all")) -
            Number(right.getAttribute("data-rank-all"));
          return rankDelta || left.dataset.storyId.localeCompare(right.dataset.storyId);
        });
      let rank = HISTORY_RANK_OFFSET;
      authoritativeAllHistoryOrder.forEach((storyId) => {
        const card = cards.get(storyId);
        if (card) card.setAttribute("data-rank-all", String(++rank));
      });
      provisional.forEach((card) => {
        card.setAttribute("data-rank-all", String(++rank));
      });
      nextHistoryAllRank = rank;
    }
    function mergeRows(rows, appendNew, hydratedTopic = selectedTopic()) {
      rows.forEach((row) => {
        const existing = cards.get(row.story_id);
        const historyAllRank = (!existing || !existing.hasAttribute("data-rank-all"))
          ? ++nextHistoryAllRank
          : null;
        if (existing) {
          mergeTopicMembership(existing, row.topic_ids.map(topicSlugForId));
          existing.dataset.topicApiIds = [...row.topic_ids].sort().join(" ");
          applyServerRank(existing, row, selectedTopic(), topicSlugForId, historyAllRank);
          applyServerState(existing, row);
          hydratedTopics(existing).add(hydratedTopic);
          view.addCard(existing);
          return;
        }
        if (!appendNew) return;
        const selected = selectedTopic();
        const card = createStoryCard(
          row, selected, topicSlugForId, topicIdForSlug(selected), historyAllRank,
        );
        cards.set(row.story_id, card);
        hydratedTopics(card).add(hydratedTopic);
        const section = row.page_order_mode === "edition_rank"
          ? sectionFor(row.topic_ids[0])
          : historySection();
        section.querySelector(".grid").append(card);
        view.addCard(card);
      });
      if (hydratedTopic === "__all__") reconcileAllHistoryRanks(rows);
      view.apply();
      refreshStateControls();
    }
    async function hydrate(force = false) {
      const topic = selectedTopic();
      if (!force && hydrated.has(topic)) return;
      if (topic === "__saved__" && !signedIn()) {
        requireSignIn();
        return;
      }
      const wasHydrated = hydrated.has(topic);
      const requestEpoch = authEpoch;
      const initialCursor = topic === "__all__" ? { order_mode: "history_freshness" } : null;
      const rows = topic === "__saved__"
        ? await api.savedPage(null, latest.page_size)
        : await api.feedPage(topicIdForSlug(topic), initialCursor, latest.page_size);
      if (requestEpoch !== authEpoch) return;
      mergeRows(rows, true, topic);
      const cursor = topic === "__saved__"
        ? nextSavedCursor(rows, latest.page_size)
        : nextFeedCursor(rows, latest.initial_history_cursor, initialCursor, latest.page_size);
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
      const requestEpoch = authEpoch;
      try {
        let rows;
        const currentCursor = cursors.get(topic) || null;
        if (topic === "__saved__") {
          rows = await api.savedPage(currentCursor, latest.page_size);
          if (requestEpoch !== authEpoch) return;
          const cursor = nextSavedCursor(rows, latest.page_size);
          if (cursor) cursors.set(topic, cursor);
        } else {
          rows = await api.feedPage(topicIdForSlug(topic), currentCursor, latest.page_size);
          if (requestEpoch !== authEpoch) return;
          const cursor = nextFeedCursor(rows, latest.initial_history_cursor, currentCursor, latest.page_size);
          if (cursor) cursors.set(topic, cursor); else exhausted.add(topic);
        }
        mergeRows(rows, true, topic);
        if (topic === "__saved__" && rows.length < latest.page_size) exhausted.add(topic);
        announce(rows.length ? loadedStatus(rows.length) : "No older stories remain in this section.");
      } catch (_) {
        if (requestEpoch === authEpoch) announce("Older stories could not be loaded. Try again.");
      } finally { loadButton.disabled = false; }
    }
    function reapplyCurrentMembership(card) {
      const focused = document.activeElement;
      const cardHadFocus = Boolean(focused && card.contains(focused));
      const scrollX = window.scrollX;
      const scrollY = window.scrollY;
      view.apply();
      if (cardHadFocus && card.hidden) {
        const nextAction = document.querySelector(".card:not([hidden]) .save-action");
        const savedTab = document.querySelector('.chip[data-filter="__saved__"]');
        const target = nextAction || savedTab;
        if (target) target.focus({ preventScroll: true });
      }
      window.scrollTo(scrollX, scrollY);
    }
    async function mutateState(card, read, saved) {
      const requestEpoch = authEpoch;
      const previous = {
        read_at: card.classList.contains("is-read") ? "local" : null,
        saved_at: card.classList.contains("is-saved") ? "local" : null,
        state_revision: Number(card.dataset.stateRevision || 0),
      };
      applyServerState(card, { ...previous, read_at: read ? "local" : null, saved_at: saved ? "local" : null });
      try {
        const result = await api.setStoryState(card.dataset.storyId, read, saved, previous.state_revision, idempotencyKey());
        if (requestEpoch !== authEpoch) return;
        if (result.status === "conflict") fail("Story state changed in another session.");
        applyServerState(card, { ...previous, ...result });
        reapplyCurrentMembership(card);
        announce("Reading state saved.");
      } catch (_) {
        if (requestEpoch !== authEpoch) return;
        applyServerState(card, previous);
        reapplyCurrentMembership(card);
        announce("Reading state could not be saved. Try again.");
      }
    }
    document.getElementById("sections").addEventListener("click", (event) => {
      const target = event.target.closest && event.target.closest("button");
      const card = event.target.closest && event.target.closest(".card[data-story-id]");
      if (!target || !card) return;
      const stateAction = target.classList.contains("state-action");
      if ((stateAction || target.classList.contains("accordion-toggle")) && !stateReady(card)) return;
      if (target.classList.contains("accordion-toggle") && target.getAttribute("aria-expanded") === "true" &&
          !card.classList.contains("is-read") && requireSignIn()) {
        void mutateState(card, true, card.classList.contains("is-saved"));
      } else if (target.classList.contains("read-action") && requireSignIn()) {
        void mutateState(card, !card.classList.contains("is-read"), card.classList.contains("is-saved"));
      } else if (target.classList.contains("save-action") && requireSignIn()) {
        void mutateState(card, card.classList.contains("is-read"), !card.classList.contains("is-saved"));
      } else if (target.classList.contains("interest-action") && requireSignIn()) {
        const topicIds = (card.dataset.topicApiIds || "").split(/\s+/).filter(Boolean);
        const topic = selectedTopic();
        const topicId = actionTopic(
          topicIds,
          topic,
          topicIdForSlug(topic),
          target.dataset.fallbackTopicId || target.dataset.topicId,
        );
        applyInterestTopic(card, topicId);
        if (card.classList.contains("is-more-like")) return;
        const revision = Number(card.dataset.interestRevision || 0);
        const requestEpoch = authEpoch;
        target.disabled = true;
        api.setStoryInterest(card.dataset.storyId, topicId, revision, idempotencyKey())
          .then((result) => {
            if (requestEpoch !== authEpoch) return;
            if (result.status === "conflict") fail("Story interest changed in another session.");
            applyServerState(card, result, topicId);
            announce("More like this was saved for future rankings.");
          })
          .catch(() => {
            if (requestEpoch === authEpoch) announce("More like this could not be saved. Try again.");
          })
          .finally(() => { target.disabled = !stateReady(card); });
      }
    });
    document.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        refreshStateControls();
        refreshInterestControls();
        void hydrate().catch(() => { announce("This section could not be synced. Try again."); });
      });
    });
    loadButton.addEventListener("click", () => { void loadMore(); });
    updatesButton.addEventListener("click", () => { window.location.reload(); });
    if (typeof BroadcastChannel !== "undefined") {
      const channel = new BroadcastChannel(auth.channelName);
      channel.addEventListener("message", (event) => {
        if (exactFields(event.data, ["session", "type"]) && event.data.type === "session") {
          try {
            auth.acceptSession(event.data.session);
            invalidateHydrationForSession();
            void hydrate(true).catch(() => { announce("Signed in, but reading state could not be synced."); });
            announce("Signed in. Reading state is syncing.");
          } catch (_) {}
          return;
        }
        if (exactFields(event.data, ["type"]) && event.data.type === "logout") {
          void handleLogout();
        }
      });
    }
    try {
      latest = await api.latestPublication();
      if (!latest) {
        loadButton.hidden = true;
        announce("No published edition is available yet.");
        return;
      }
      loadButton.textContent = `Load ${latest.page_size} more`;
      publicationSeq = latest.publication_seq;
      const poll = async () => {
        try {
          const current = await api.latestPublication();
          if (!current || current.publication_seq <= publicationSeq) return;
          const updatePage = await drainUpdates(api, publicationSeq, updateCursor, current.page_size);
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
