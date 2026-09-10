(() => {
  "use strict";

  const MAX_PAGE_SIZE = 100;
  const HISTORY_RANK_OFFSET = 1000000;
  const MAX_RESPONSE_BYTES = 256 * 1024;
  const MAX_DISCOVERY_BYTES = 1024 * 1024;
  const DISCOVERY_LANES = ["updates", "hot", "interested", "surprise"];
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
        !pageModes.includes(value.page_order_mode) ||
        (value.page_order_mode === "discovery" ? value.next_cursor !== null : !isObject(value.next_cursor)) ||
        encoder.encode(JSON.stringify(value.ordering_key)).length > 2048 || !isObject(value.score_components) ||
        encoder.encode(JSON.stringify(value.score_components)).length > 8192 ||
        !Array.isArray(value.topic_ids) || (!["discovery", "saved_at"].includes(value.page_order_mode) && value.topic_ids.length < 1) || value.topic_ids.length > 20 ||
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
    if (value.page_order_mode === "discovery") {
      if (value.publication_seq !== 0 || value.position !== 0 || !exactFields(value.topic_ranks, []) ||
          value.ordering_mode !== "weighted_total" || !exactFields(value.ordering_key, [])) fail("The discovery card was invalid.");
    } else if (value.page_order_mode === "edition_rank") {
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
  function validateDiscovery(value) {
    const error = "The private edition response was invalid.";
    if (!exactFields(value, ["schema_version", "status", "reason_code", "edition"]) || value.schema_version !== 1) fail(error);
    if (value.status === "unavailable") {
      if (value.edition !== null || !["no_private_edition", "edition_unavailable"].includes(value.reason_code)) fail(error);
      return value;
    }
    const edition = value.edition;
    const digest = (v) => typeof v === "string" && /^[0-9a-f]{64}$/.test(v);
    const integer = (v) => Number.isSafeInteger(v) && v >= 0;
    if (value.status !== "ready" || value.reason_code !== "" ||
        !exactFields(edition, ["edition_id", "generated_at", "code_revision", "policy_revision", "policy_digest", "snapshot_digest", "profile_revision", "receipt_digest", "stale", "disclosures", "shortfalls", "entries"]) ||
        !boundedString(edition.edition_id, 256) || !validTimestamp(edition.generated_at) ||
        typeof edition.code_revision !== "string" || !/^[0-9a-f]{40}$/.test(edition.code_revision) ||
        !integer(edition.policy_revision) || edition.policy_revision < 1 || !integer(edition.profile_revision) ||
        !digest(edition.policy_digest) || !digest(edition.snapshot_digest) || !digest(edition.receipt_digest) ||
        typeof edition.stale !== "boolean" || !Array.isArray(edition.disclosures) || edition.disclosures.length > 30 ||
        !edition.disclosures.every((v) => boundedString(v, 2000)) ||
        !exactFields(edition.shortfalls, DISCOVERY_LANES) || !Object.values(edition.shortfalls).every(integer) ||
        !Array.isArray(edition.entries) || edition.entries.length > MAX_PAGE_SIZE ||
        encoder.encode(JSON.stringify(value)).length > MAX_DISCOVERY_BYTES) fail(error);
    const seen = new Set();
    edition.entries.forEach((entry, index) => {
      if (!exactFields(entry, ["position", "primary_lane", "reason", "secondary_reasons", "card"]) ||
          entry.position !== index + 1 || !DISCOVERY_LANES.includes(entry.primary_lane) ||
          !boundedString(entry.reason, 2000) || !Array.isArray(entry.secondary_reasons) || entry.secondary_reasons.length > 3) fail(error);
      const lanes = new Set([entry.primary_lane]);
      entry.secondary_reasons.forEach((reason) => {
        if (!exactFields(reason, ["lane", "reason"]) || !DISCOVERY_LANES.includes(reason.lane) ||
            lanes.has(reason.lane) || !boundedString(reason.reason, 2000)) fail(error);
        lanes.add(reason.lane);
      });
      validateStory(entry.card, ["discovery"]);
      if (seen.has(entry.card.story_id)) fail(error);
      seen.add(entry.card.story_id);
      const components = ["relevance", "freshness", "trend", "editor_consensus", "deliberate_surprise", "diversity", "repetition_penalty", "source_fatigue_penalty", "final_score"];
      if (!exactFields(entry.card.score_components, components) ||
          !Object.values(entry.card.score_components).every((v) => typeof v === "number" && Number.isFinite(v))) fail(error);
    });
    return value;
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
  async function boundedJson(response, message, byteLimit = MAX_RESPONSE_BYTES) {
    const text = await response.text();
    if (encoder.encode(text).length > byteLimit) fail(message);
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
    async function rpc(name, body, validator, requiresAuth = false, byteLimit = MAX_RESPONSE_BYTES) {
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
        referrerPolicy: "no-referrer", redirect: "error", cache: "no-store",
        signal: AbortSignal.timeout(15000),
      });
      if (response.redirected !== false || response.url !== requestedUrl) fail("The reader endpoint redirected unexpectedly.");
      const payload = await boundedJson(response, "The reader response was invalid.", byteLimit);
      if (!response.ok) {
        if (session && [401, 403].includes(response.status) && typeof window !== "undefined") {
          window.NewsCuratorAuth?.rejectSession?.(session);
        }
        fail("The reader request failed.");
      }
      const result = validator(payload, Boolean(session));
      if (session && ["feed_page", "saved_page", "discovery_edition"].includes(name) && typeof window !== "undefined") {
        window.NewsCuratorAuth?.confirmSession?.(session);
      }
      return result;
    }
    return Object.freeze({
      latestPublication: () => rpc("latest_publication", {}, validateLatestPublication),
      discoveryEdition: (editionId = null) => {
        if (editionId !== null && !boundedString(editionId, 256)) fail("Invalid edition.");
        return rpc("discovery_edition", { p_edition_id: editionId }, validateDiscovery, true, MAX_DISCOVERY_BYTES);
      },
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
  function setReadPresentation(card, read) {
    card.classList.toggle("is-read", read);
    const readButton = card.querySelector(".read-action");
    if (readButton) {
      if (!read && typeof document !== "undefined" && document.activeElement === readButton) {
        card.querySelector(".accordion-toggle")?.focus({ preventScroll: true });
      }
      readButton.textContent = "Mark unread";
      readButton.hidden = !read;
    }
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
    if (hasRead) setReadPresentation(card, read);
    if (hasSaved) card.classList.toggle("is-saved", saved);
    if (Number.isSafeInteger(state.state_revision)) card.dataset.stateRevision = String(state.state_revision);
    const saveButton = card.querySelector(".save-action");
    if (saveButton && hasSaved) {
      saveButton.textContent = saved ? "Unsave" : "Save";
      saveButton.setAttribute("aria-pressed", String(saved));
    }
    if (interestButton && interestButton.dataset.topicId) {
      applyInterestTopic(card, interestButton.dataset.topicId);
    }
  }

  function setStoryStateControlsDisabled(card, disabled) {
    const readButton = card.querySelector(".read-action");
    const saveButton = card.querySelector(".save-action");
    // Read and unread are immediate local presentation actions. Only their
    // server synchronization is queued, so a visible Mark unread stays usable.
    if (readButton) readButton.disabled = false;
    if (saveButton) saveButton.disabled = disabled;
  }

  function beginStateMutation(card) {
    if (card.newsCuratorStateMutationToken) return null;
    const token = {};
    card.newsCuratorStateMutationToken = token;
    setStoryStateControlsDisabled(card, true);
    return token;
  }

  function finishStateMutation(card, token, ready) {
    if (card.newsCuratorStateMutationToken !== token) return false;
    delete card.newsCuratorStateMutationToken;
    setStoryStateControlsDisabled(card, !ready);
    return true;
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
    const read = element("button", "state-action read-action", "Mark unread");
    const save = element("button", "state-action save-action", "Save");
    const interest = element("button", "state-action interest-action", "More like this");
    [read, save, interest].forEach((button) => { button.type = "button"; });
    save.setAttribute("aria-pressed", "false");
    interest.setAttribute("aria-pressed", "false");
    interest.dataset.topicId = effectiveTopic(row.topic_ids, selectedTopicId);
    interest.dataset.fallbackTopicId = interest.dataset.topicId;
    const close = element("button", "shut", "Close");
    close.type = "button";
    actions.append(read, save);
    if (row.topic_ids.length) actions.append(interest);
    else {
      const addInterest = element("a", "add-interest", "Add an interest");
      addInterest.href = "/auth/callback/";
      actions.append(addInterest);
    }
    actions.append(close);
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
    actionTopic, applyInterestTopic, applyServerRank, applyServerState, beginStateMutation, createApi, createStoryCard, drainUpdates, effectiveTopic, finishStateMutation, loadedStatus, mergeTopicMembership, nextFeedCursor, nextSavedCursor,
    rankingReason, run, safeDestination,
    validateDiscovery, validateFeedPage, validateSavedPage, validateLatestPublication, validateUpdates,
  };
  const commonJs = typeof module !== "undefined" && module.exports;
  if (commonJs) {
    module.exports = contract;
  } else {
    window.NewsCuratorReaderApi = Object.freeze({
      create: () => {
        const api = createApi(
          window.NewsCuratorAuth.config(),
          () => window.NewsCuratorAuth.sessionForRequest(),
        );
        return Object.freeze({
          latestPublication: api.latestPublication,
          savedPage: api.savedPage,
          setStoryState: api.setStoryState,
        });
      },
    });
  }

  async function run() {
    const auth = window.NewsCuratorAuth;
    const view = window.NewsCuratorView;
    const status = document.getElementById("reader-status");
    const loadButton = document.getElementById("load-more");
    const updatesStatus = document.getElementById("updates-status");
    const updatesButton = document.getElementById("show-updates");
    if (!auth || !view || !status || !loadButton || !updatesStatus || !updatesButton) return;
    const savedTabs = document.querySelectorAll('.chip[data-filter="__saved__"]');
    let api;
    try { api = createApi(auth.config(), () => auth.sessionForRequest()); } catch (_) {
      loadButton.hidden = true;
      if (view.currentTab() === "__saved__") {
        document.querySelector('.chip[data-filter="__all__"]')?.click();
      }
      return;
    }
    savedTabs.forEach((tab) => {
      tab.hidden = false;
      tab.disabled = false;
    });
    document.querySelectorAll(".state-action:not(.read-action)").forEach((button) => { button.hidden = false; });
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
    const pageRequests = new Set();
    let publicationSeq = 0;
    let latest = null;
    let initializing = true;
    let pollTimer = null;
    let updateCursor = null;
    let nextHistoryAllRank = HISTORY_RANK_OFFSET;
    let authEpoch = 0;
    const authoritativeAllHistoryOrder = [];
    const authoritativeAllHistoryIds = new Set();
    const pendingUpdates = new Map();
    const discoveryControls = document.getElementById("discovery-controls");
    const discoveryStatus = document.getElementById("discovery-status");
    const discoveryNotice = document.getElementById("discovery-notice");
    const discoveryAccept = document.getElementById("discovery-accept");
    let discoveryEdition = null;
    let pendingDiscovery = null;
    let discoveryLane = "updates";
    let discoveryActive = false;
    let discoveryRequest = 0;
    let publicCards = [];
    let discoverySection = null;
    function discoveryMessage(message) { if (discoveryStatus) discoveryStatus.textContent = message; }
    function setDiscoveryLane(lane) {
      discoveryLane = lane;
      if (discoverySection) discoverySection.dataset.discoverySelectedLane = lane;
      discoveryControls?.querySelectorAll("[data-discovery-lane]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.discoveryLane === lane && discoveryActive));
      });
      view.apply();
      const count = discoveryEdition?.entries.filter((entry) => entry.primary_lane === lane).length || 0;
      const emptyReasons = { updates: "No verified publisher changes are available in this edition.", hot: "No stories met the recent independent-coverage threshold.", interested: "No fresh stories matched your settled interests.", surprise: "No unseen stories outside your interests met the quality and importance checks." };
      discoveryMessage(count ? `${count} ${count === 1 ? "story" : "stories"} in ${lane}.${discoveryEdition.stale ? " This edition is older than usual." : ""}` : emptyReasons[lane]);
    }
    function leaveDiscovery(clearEdition = false) {
      discoveryRequest += 1;
      if (discoveryActive) {
        cards.forEach((card) => {
          clearPrivateCardState(card);
          view.removeCard(card);
          card.replaceChildren();
          [...card.attributes].forEach((attribute) => card.removeAttribute(attribute.name));
          card.remove();
        });
        cards.clear();
        discoverySection?.remove();
        discoverySection = null;
        publicCards.forEach(({ card, parent }) => {
          parent.append(card); cards.set(card.dataset.storyId, card); view.addCard(card);
        });
        publicCards = [];
      }
      discoveryActive = false;
      if (clearEdition) {
        discoveryEdition = null; pendingDiscovery = null;
        if (discoveryNotice) discoveryNotice.hidden = true;
      }
      discoveryControls?.querySelectorAll("[data-discovery-lane]").forEach((button) => button.setAttribute("aria-pressed", "false"));
      view.apply(); refreshLoadButton();
    }
    function showDiscovery(edition) {
      if (!signedIn() || !discoveryControls) return;
      leaveDiscovery();
      discoveryEdition = edition;
      pendingDiscovery = null;
      if (discoveryNotice) discoveryNotice.hidden = true;
      publicCards = [...cards.values()].map((card) => ({ card, parent: card.parentNode }));
      publicCards.forEach(({ card }) => { view.removeCard(card); card.remove(); });
      cards.clear();
      discoveryActive = true;
      discoverySection = element("section", "topic-section discovery-section");
      discoverySection.dataset.section = "__discovery__";
      discoverySection.dataset.discoverySelectedLane = discoveryLane;
      const grid = element("div", "grid"); discoverySection.append(grid);
      document.getElementById("sections").append(discoverySection);
      edition.entries.forEach((entry) => {
        const card = createStoryCard(entry.card, selectedTopic(), topicSlugForId, topicIdForSlug(selectedTopic()));
        card.dataset.discoveryLane = entry.primary_lane;
        card.dataset.discoveryPosition = String(entry.position);
        const signals = card.querySelector(".signal");
        signals.replaceChildren(element("b", "", "Why this story"), element("span", "", entry.reason));
        entry.secondary_reasons.forEach((reason) => signals.append(element("p", "secondary-reason", `${reason.lane}: ${reason.reason}`)));
        cards.set(entry.card.story_id, card); grid.append(card); view.addCard(card);
      });
      discoveryControls.hidden = false;
      if (selectedTopic() === "__saved__") view.setTab?.("__all__");
      setDiscoveryLane(discoveryLane); refreshStateControls(); refreshInterestControls(); refreshLoadButton();
    }
    async function fetchDiscovery(initial = false) {
      if (!discoveryControls || !signedIn()) return;
      const epoch = authEpoch, request = ++discoveryRequest;
      try {
        const response = await api.discoveryEdition();
        if (epoch !== authEpoch || request !== discoveryRequest || !signedIn()) return;
        discoveryControls.hidden = false;
        if (response.status !== "ready") {
          leaveDiscovery(true);
          discoveryMessage("Your private discovery edition is not ready yet. Public stories are available.");
          return;
        }
        if (!discoveryEdition && initial) {
          discoveryLane = "updates"; view.setTab?.("__all__"); showDiscovery(response.edition);
        } else if (discoveryEdition?.edition_id !== response.edition.edition_id) {
          pendingDiscovery = response.edition;
          if (discoveryNotice) discoveryNotice.hidden = false;
        }
      } catch (_) {
        if (epoch === authEpoch && request === discoveryRequest && signedIn()) {
          discoveryControls.hidden = false;
          discoveryMessage("Your private edition could not be checked. Public stories remain available.");
        }
      }
    }
    async function openStoredDiscovery(editionId, lane) {
      const epoch = authEpoch, request = ++discoveryRequest;
      try {
        const response = await api.discoveryEdition(editionId);
        if (epoch !== authEpoch || request !== discoveryRequest || !signedIn()) return;
        if (response.status !== "ready") {
          leaveDiscovery(true);
          discoveryMessage("That private edition is no longer available. Public stories are available.");
          return;
        }
        if (response.edition.edition_id !== editionId) fail("The requested edition changed.");
        discoveryLane = lane; showDiscovery(response.edition);
      } catch (_) {
        if (epoch === authEpoch && request === discoveryRequest) discoveryMessage("That private edition could not be loaded. Try again.");
      }
    }
    discoveryControls?.addEventListener("click", (event) => {
      const lane = event.target.closest?.("[data-discovery-lane]")?.dataset.discoveryLane;
      if (lane && DISCOVERY_LANES.includes(lane)) {
        if (!discoveryEdition) { void fetchDiscovery(true); return; }
        discoveryLane = lane;
        if (!discoveryActive) void openStoredDiscovery(discoveryEdition.edition_id, lane); else setDiscoveryLane(lane);
      }
      if (event.target.closest?.("[data-discovery-public]")) {
        leaveDiscovery(); discoveryMessage("Showing public stories.");
        void hydrate(true).catch(() => announce("Public stories could not be synced."));
      }
    });
    discoveryAccept?.addEventListener("click", () => { if (pendingDiscovery) void openStoredDiscovery(pendingDiscovery.edition_id, discoveryLane); });

    function announce(message) { status.textContent = message; }
    function signedIn() { try { return auth.hasSessionCandidate(); } catch (_) { return false; } }
    let sessionWasPresent = signedIn();
    function requireSignIn() {
      if (signedIn() && (!auth.isConfirmed || auth.isConfirmed())) return true;
      announce("Sign in to sync reading controls.");
      const link = document.querySelector(".profile-link");
      if (link) link.focus();
      return false;
    }
    function selectedTopic() { return view.currentTab(); }
    function refreshLoadButton() {
      const topic = selectedTopic();
      loadButton.hidden = discoveryActive || initializing || !latest || exhausted.has(topic) ||
        (topic === "__saved__" && !signedIn());
      loadButton.disabled = [...pageRequests].some((request) =>
        request.topic === topic && request.epoch === authEpoch);
    }
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
    function stateReady(card) { return (discoveryActive && Boolean(card.dataset.discoveryLane) && signedIn()) || hydratedTopics(card).has(selectedTopic()); }
    function refreshStateControls() {
      cards.forEach((card) => {
        const ready = stateReady(card);
        card.querySelectorAll(".state-action").forEach((button) => {
          if (button.classList.contains("read-action")) {
            button.disabled = false;
            return;
          }
          const stateWrite = button.classList.contains("read-action") || button.classList.contains("save-action");
          button.disabled = !ready || (stateWrite && Boolean(card.newsCuratorStateMutationToken));
        });
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
      delete card.newsCuratorStateMutationToken;
      delete card.newsCuratorStateMutationPresentation;
      delete card.newsCuratorStateMutationBaseline;
      card.classList.remove("is-read", "is-saved", "is-more-like", "is-less-like");
      interestStates(card).clear();
      hydratedTopics(card).clear();
      card.dataset.stateRevision = "0";
      card.dataset.interestRevision = "0";
      const readButton = card.querySelector(".read-action");
      const saveButton = card.querySelector(".save-action");
      const interestButton = card.querySelector(".interest-action");
      if (readButton) {
        readButton.textContent = "Mark unread";
        readButton.hidden = true;
      }
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
      delete card.newsCuratorReadIntent;
      delete card.newsCuratorPendingReadIntent;
      delete card.newsCuratorLocalRead;
    }
    async function handleLogout() {
      sessionWasPresent = false;
      authEpoch += 1;
      leaveDiscovery(true);
      if (discoveryControls) discoveryControls.hidden = true;
      discoveryMessage("");
      auth.clearSession();
      document.querySelectorAll(".state-action").forEach((button) => { button.disabled = true; });
      cards.forEach((card, storyId) => {
        if (!card.newsCuratorStaticCard) {
          cards.delete(storyId);
          view.removeCard(card);
          card.replaceChildren();
          [...card.attributes].forEach((attribute) => card.removeAttribute(attribute.name));
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
      leaveDiscovery(true);
      if (discoveryControls) discoveryControls.hidden = true;
      discoveryMessage("");
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
          const pendingRead = existing.newsCuratorPendingReadIntent;
          const priorRead = existing.classList.contains("is-read");
          mergeTopicMembership(existing, row.topic_ids.map(topicSlugForId));
          existing.dataset.topicApiIds = [...row.topic_ids].sort().join(" ");
          applyServerRank(existing, row, selectedTopic(), topicSlugForId, historyAllRank);
          const currentRevision = Number(existing.dataset.stateRevision || 0);
          const incomingStale = row.state_revision < currentRevision;
          if (incomingStale) {
            // A slower page read may have started before a successful write.
            // Keep its topic and interest data, but never roll back newer CAS state.
            applyServerState(existing, { interests: row.interests });
          } else {
            applyServerState(existing, row);
          }
          const mutationBaseline = existing.newsCuratorStateMutationBaseline;
          if (mutationBaseline && mutationBaseline.token === existing.newsCuratorStateMutationToken &&
              row.state_revision >= mutationBaseline.state_revision) {
            mutationBaseline.read_at = row.read_at;
            mutationBaseline.saved_at = row.saved_at;
            mutationBaseline.state_revision = row.state_revision;
          }
          const mutationPresentation = existing.newsCuratorStateMutationPresentation;
          if (mutationPresentation) {
            applyServerState(existing, {
              read_at: mutationPresentation.read ? "local" : null,
              saved_at: mutationPresentation.saved ? "local" : null,
            });
          }
          if (!signedIn() && typeof existing.newsCuratorLocalRead === "boolean") {
            applyServerState(existing, {
              read_at: existing.newsCuratorLocalRead ? "local" : null,
            });
          }
          if (pendingRead) {
            pendingRead.previousRead = incomingStale ? priorRead : Boolean(row.read_at);
            applyServerState(existing, { read_at: pendingRead.read ? "local" : null });
          }
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
    function syncReadIntent(card, intent) {
      if (!intent || typeof intent.read !== "boolean") return;
      if (!signedIn()) {
        delete card.newsCuratorPendingReadIntent;
        card.newsCuratorLocalRead = intent.read;
        return;
      }
      if (card.newsCuratorStateMutationToken) {
        card.newsCuratorPendingReadIntent = intent;
        return;
      }
      if (!stateReady(card)) {
        card.newsCuratorPendingReadIntent = intent;
        setStoryStateControlsDisabled(card, true);
        return;
      }
      delete card.newsCuratorPendingReadIntent;
      void mutateState(
        card,
        intent.read,
        card.classList.contains("is-saved"),
        intent.previousRead,
      );
    }
    function flushPendingReadIntents() {
      cards.forEach((card) => {
        const intent = card.newsCuratorPendingReadIntent;
        if (intent && stateReady(card)) syncReadIntent(card, intent);
      });
    }
    async function hydrate(force = false) {
      if (discoveryActive) return;
      const topic = selectedTopic();
      if (!force && hydrated.has(topic)) return;
      if (topic === "__saved__" && !signedIn()) {
        requireSignIn();
        return;
      }
      const wasHydrated = hydrated.has(topic);
      const requestEpoch = authEpoch;
      const request = { topic, epoch: requestEpoch };
      pageRequests.add(request);
      refreshLoadButton();
      try {
        const initialCursor = topic === "__all__" ? { order_mode: "history_freshness" } : null;
        const rows = topic === "__saved__"
          ? await api.savedPage(null, latest.page_size)
          : await api.feedPage(topicIdForSlug(topic), initialCursor, latest.page_size);
        if (requestEpoch !== authEpoch || discoveryActive) return;
        mergeRows(rows, true, topic);
        const cursor = topic === "__saved__"
          ? nextSavedCursor(rows, latest.page_size)
          : nextFeedCursor(rows, latest.initial_history_cursor, initialCursor, latest.page_size);
        if (!wasHydrated) {
          if (cursor) cursors.set(topic, cursor); else exhausted.add(topic);
          hydrated.add(topic);
        }
        flushPendingReadIntents();
      } finally {
        pageRequests.delete(request);
        refreshLoadButton();
      }
    }
    async function loadMore() {
      const topic = selectedTopic();
      if (!latest || initializing || loadButton.disabled || exhausted.has(topic)) return;
      if (topic === "__saved__" && !requireSignIn()) return;
      const requestEpoch = authEpoch;
      const request = { topic, epoch: requestEpoch };
      pageRequests.add(request);
      refreshLoadButton();
      try {
        if (!hydrated.has(topic)) {
          await hydrate();
          if (requestEpoch === authEpoch && topic === selectedTopic()) announce("This section is ready.");
          return;
        }
        let rows;
        const currentCursor = cursors.get(topic) || null;
        if (topic === "__saved__") {
          rows = await api.savedPage(currentCursor, latest.page_size);
          if (requestEpoch !== authEpoch || discoveryActive) return;
          const cursor = nextSavedCursor(rows, latest.page_size);
          if (cursor) cursors.set(topic, cursor);
        } else {
          rows = await api.feedPage(topicIdForSlug(topic), currentCursor, latest.page_size);
          if (requestEpoch !== authEpoch || discoveryActive) return;
          const cursor = nextFeedCursor(rows, latest.initial_history_cursor, currentCursor, latest.page_size);
          if (cursor) cursors.set(topic, cursor); else exhausted.add(topic);
        }
        mergeRows(rows, true, topic);
        if (topic === "__saved__" && rows.length < latest.page_size) exhausted.add(topic);
        if (topic === selectedTopic()) {
          announce(rows.length ? loadedStatus(rows.length) : exhausted.has(topic)
            ? "No older stories remain in this section." : "Load more to check older stories.");
        }
      } catch (_) {
        if (requestEpoch === authEpoch && topic === selectedTopic()) announce("Older stories could not be loaded. Try again.");
      } finally {
        pageRequests.delete(request);
        refreshLoadButton();
      }
    }
    function reapplyCurrentMembership(card, priorFocusedAction = null) {
      const focused = priorFocusedAction || document.activeElement;
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
    function reconcilePendingRead(card, confirmedRead) {
      const pending = card.newsCuratorPendingReadIntent;
      if (!pending) return;
      if (pending.read === confirmedRead) {
        delete card.newsCuratorPendingReadIntent;
        return;
      }
      pending.previousRead = confirmedRead;
      applyServerState(card, { read_at: pending.read ? "local" : null });
    }
    async function mutateState(card, read, saved, previousRead = card.classList.contains("is-read")) {
      const focusedAction = document.activeElement;
      const restoreFocusOnRollback = Boolean(focusedAction && card.contains(focusedAction));
      const mutationToken = beginStateMutation(card);
      if (!mutationToken) return;
      const requestEpoch = authEpoch;
      let rolledBack = false;
      card.newsCuratorStateMutationPresentation = { token: mutationToken, read, saved };
      const previous = {
        read_at: previousRead ? "local" : null,
        saved_at: card.classList.contains("is-saved") ? "local" : null,
        state_revision: Number(card.dataset.stateRevision || 0),
      };
      card.newsCuratorStateMutationBaseline = { token: mutationToken, ...previous };
      applyServerState(card, { ...previous, read_at: read ? "local" : null, saved_at: saved ? "local" : null });
      try {
        const result = await api.setStoryState(card.dataset.storyId, read, saved, previous.state_revision, idempotencyKey());
        if (requestEpoch !== authEpoch || card.newsCuratorStateMutationToken !== mutationToken) return;
        if (result.status === "conflict") fail("Story state changed in another session.");
        const baseline = card.newsCuratorStateMutationBaseline || previous;
        const confirmed = Number(result.state_revision) >= Number(baseline.state_revision)
          ? { ...baseline, ...result }
          : baseline;
        applyServerState(card, confirmed);
        reconcilePendingRead(card, Boolean(confirmed.read_at));
        reapplyCurrentMembership(card, restoreFocusOnRollback ? focusedAction : null);
        announce("Reading state saved.");
      } catch (_) {
        if (requestEpoch !== authEpoch || card.newsCuratorStateMutationToken !== mutationToken) return;
        const baseline = card.newsCuratorStateMutationBaseline || previous;
        applyServerState(card, baseline);
        reconcilePendingRead(card, Boolean(baseline.read_at));
        reapplyCurrentMembership(card);
        rolledBack = true;
        announce("Reading state could not be saved. Try again.");
      } finally {
        if (card.newsCuratorStateMutationPresentation?.token === mutationToken) {
          delete card.newsCuratorStateMutationPresentation;
        }
        if (card.newsCuratorStateMutationBaseline?.token === mutationToken) {
          delete card.newsCuratorStateMutationBaseline;
        }
        const unlocked = finishStateMutation(card, mutationToken, stateReady(card));
        if (unlocked && card.newsCuratorPendingReadIntent) {
          syncReadIntent(card, card.newsCuratorPendingReadIntent);
        }
        if (unlocked && rolledBack && restoreFocusOnRollback && focusedAction.isConnected && !card.hidden) {
          focusedAction.focus({ preventScroll: true });
        }
      }
    }
    document.getElementById("sections").addEventListener("click", (event) => {
      const target = event.target.closest && event.target.closest("button");
      const card = event.target.closest && event.target.closest(".card[data-story-id]");
      if (!target || !card) return;
      let readIntent = card.newsCuratorReadIntent;
      if (readIntent) delete card.newsCuratorReadIntent;
      if (!readIntent && target.classList.contains("accordion-toggle") &&
          target.getAttribute("aria-expanded") === "true" && !card.classList.contains("is-read")) {
        readIntent = { read: true, previousRead: false };
        applyServerState(card, { read_at: "local" });
      } else if (!readIntent && target.classList.contains("read-action") &&
          card.classList.contains("is-read")) {
        readIntent = { read: false, previousRead: true };
        applyServerState(card, { read_at: null });
      }
      if (readIntent && (target.classList.contains("accordion-toggle") ||
          target.classList.contains("read-action"))) {
        syncReadIntent(card, readIntent);
        return;
      }
      const stateAction = target.classList.contains("state-action");
      if (stateAction && !stateReady(card)) return;
      if (target.classList.contains("save-action") && requireSignIn()) {
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
        if (selectedTopic() === "__saved__" && discoveryActive) leaveDiscovery();
        announce("");
        refreshLoadButton();
        refreshStateControls();
        refreshInterestControls();
        void hydrate().catch(() => { announce("This section could not be synced. Try again."); });
      });
    });
    loadButton.addEventListener("click", () => { void loadMore(); });
    updatesButton.addEventListener("click", () => { window.location.reload(); });
    window.addEventListener("news-curator:auth-changed", () => {
      if (!signedIn()) {
        // An unsigned pageshow is not a logout. Keep this tab's anonymous
        // read intent while still clearing private state after a real session.
        if (sessionWasPresent) void handleLogout();
        return;
      }
      sessionWasPresent = true;
      invalidateHydrationForSession();
      void fetchDiscovery(true);
      if (!latest) {
        if (!initializing) window.location.reload();
        return;
      }
      void hydrate(true).catch(() => {
        auth.accountUnavailable?.();
        announce("Sign-in could not be checked. Use Check sign-in again to retry.");
      });
    });
    if (typeof BroadcastChannel !== "undefined") {
      const channel = new BroadcastChannel(auth.channelName);
      channel.addEventListener("message", (event) => {
        if (exactFields(event.data, ["session", "type"]) && event.data.type === "session") {
          try {
            auth.acceptSession(event.data.session);
            sessionWasPresent = true;
            invalidateHydrationForSession();
            void fetchDiscovery(true);
            void hydrate(true).catch(() => {
              auth.accountUnavailable?.();
              announce("Sign-in could not be checked. Use Check sign-in again to retry.");
            });
            announce("Checking sign-in. Reading state is syncing.");
          } catch (_) {}
          return;
        }
        if (exactFields(event.data, ["type"]) && event.data.type === "logout") {
          void handleLogout();
        }
      });
    }
    // The inline accordion remains usable while this deferred controller loads.
    // Adopt any open/unread intent that occurred before our delegated listener.
    cards.forEach((card) => {
      const intent = card.newsCuratorReadIntent;
      if (!intent) return;
      delete card.newsCuratorReadIntent;
      syncReadIntent(card, intent);
    });
    try {
      latest = await api.latestPublication();
      void fetchDiscovery(true);
      if (!latest) {
        loadButton.hidden = true;
        auth.accountUnavailable?.();
        announce("No published edition is available yet.");
        return;
      }
      loadButton.textContent = `Load ${latest.page_size} more`;
      publicationSeq = latest.publication_seq;
      const poll = async () => {
        void fetchDiscovery(false);
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
    } catch (_) {
      auth.accountUnavailable?.();
      announce("Synced reading features are temporarily unavailable. Try checking sign-in again.");
    } finally {
      initializing = false;
      refreshLoadButton();
    }
  }
  if (!commonJs) void run();
})();
