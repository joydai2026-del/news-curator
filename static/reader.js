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
  function validateHistorySnapshot(value) {
    const fields = ["consent_revision", "events", "history_generation", "history_revision",
      "included_history_revision", "learning_enabled", "newest_event_id",
      "provider_policy_id", "provider_processing_enabled", "server_commit_revision"];
    if (!exactFields(value, fields) || !Number.isSafeInteger(value.history_generation) ||
        value.history_generation < 1 || !Number.isSafeInteger(value.history_revision) ||
        !Number.isSafeInteger(value.included_history_revision) ||
        value.server_commit_revision !== value.history_revision ||
        !Number.isSafeInteger(value.consent_revision) || typeof value.learning_enabled !== "boolean" ||
        typeof value.provider_processing_enabled !== "boolean" || !Array.isArray(value.events) ||
        !(value.newest_event_id === null || boundedString(value.newest_event_id, 128)) ||
        !(value.provider_policy_id === null || boundedString(value.provider_policy_id, 512))) {
      fail("The behavior history response was invalid.");
    }
    return value;
  }
  function validateBehaviorReceipt(value) {
    if (!isObject(value) || !["recorded", "replayed", "learning_disabled"].includes(value.status)) fail("The learning response was invalid.");
    if (value.status !== "learning_disabled" && (!/^event:[0-9a-f]{64}$/.test(value.event_id) ||
        !Number.isSafeInteger(value.event_revision) || value.event_revision < 1)) fail("The learning response was invalid.");
    return value;
  }
  function validateOwnerExportPage(value, _signedIn, session) {
    const fields = ["fence", "max_download_bytes", "next_cursor", "offset", "owner_id",
      "rows", "schema_version", "total_rows"];
    if (!exactFields(value, fields) || value.schema_version !== 1 ||
        !session || !boundedString(session.user_id, 128) || value.owner_id !== session.user_id ||
        !/^[0-9a-f]{64}$/.test(value.fence) || !Number.isSafeInteger(value.offset) || value.offset < 0 ||
        !Number.isSafeInteger(value.total_rows) || value.total_rows < 0 ||
        !Number.isSafeInteger(value.max_download_bytes) || value.max_download_bytes < 1048576 ||
        value.max_download_bytes > 134217728 || value.total_rows > value.max_download_bytes ||
        !Array.isArray(value.rows) || value.rows.length > 500 ||
        !(value.next_cursor === null || boundedString(value.next_cursor, 2048))) {
      fail("The data export response was invalid.");
    }
    const rows = value.rows.map((row) => {
      if (!exactFields(row, ["key", "section", "value"]) || !boundedString(row.section, 80) ||
          !boundedString(row.key, 512) || !isObject(row.value)) fail("The data export response was invalid.");
      return row;
    });
    if (value.offset + rows.length > value.total_rows || (value.next_cursor && !rows.length)) {
      fail("The data export response was invalid.");
    }
    return { ...value, rows };
  }
  function validateAtomic(value, validator) {
    if (!isObject(value)) fail("The reading response was invalid.");
    const { behavior_event: event, ...state } = value;
    const result = validator(state);
    if (result.status !== "conflict") validateBehaviorReceipt(event);
    return result;
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
      const result = validator(payload, Boolean(session), session);
      if (session && ["feed_page", "saved_page", "discovery_edition", "m2_history_snapshot"].includes(name) && typeof window !== "undefined") {
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
      historySnapshot: (limit = null) => rpc("m2_history_snapshot", { p_limit: limit }, validateHistorySnapshot, true),
      setBehaviorConsent: (learning, providerProcessing, providerPolicyId) => rpc("set_behavior_consent", {
        p_learning_enabled: learning, p_provider_processing_enabled: providerProcessing,
        p_provider_policy_id: providerPolicyId,
      }, (value) => value, true),
      clearBehaviorHistory: () => rpc("clear_behavior_history", {}, (value) => value, true),
      ownerExportPage: (cursor = null, expectedFence = null) => rpc("m2_owner_export_page", {
        p_cursor: cursor, p_expected_fence: expectedFence,
      }, validateOwnerExportPage, true, MAX_DISCOVERY_BYTES),
      appendBehaviorEvent: (event) => rpc("append_behavior_event", event, validateBehaviorReceipt, true),
      setStoryStateWithEvent: (storyId, read, saved, revision, key, event) => rpc("set_story_state_with_event", {
        p_story_id: storyId, p_read: read, p_saved: saved, p_expected_revision: revision,
        p_idempotency_key: key, ...event,
      }, (value) => validateAtomic(value, validateStoryState), true),
      setStoryInterestWithEvent: (storyId, topicId, signal, revision, key, event) => rpc("set_story_interest_with_event", {
        p_story_id: storyId, p_topic_id: topicId, p_signal: signal, p_expected_revision: revision,
        p_idempotency_key: key, ...event,
      }, (value) => validateAtomic(value, (state) => {
        if (state.signal === "less_like") return { ...validateInterest({ ...state, signal: "more_like" }), interest_signal: "less_like" };
        return validateInterest(state);
      }), true),
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
    const less = card.querySelector(".less-interest-action");
    if (less) {
      const reduced = state.signal === "less_like";
      less.textContent = reduced ? "Less like this added" : "Less like this";
      less.setAttribute("aria-pressed", String(reduced));
      card.classList.toggle("is-less-like", reduced);
    }
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

  function mergeM2CardState(incoming, current, sameHistoryContext) {
    if (!isObject(current) || current.story_id !== incoming.story_id) return incoming;
    // Local card state is safe to carry across a frozen refresh only while the
    // history generation and consent revision are unchanged. A clear-history
    // or consent change makes the incoming server state authoritative.
    if (!sameHistoryContext) return incoming;
    const merged = { ...incoming };
    if (Number.isSafeInteger(current.state_revision) &&
        current.state_revision > incoming.state_revision) {
      ["read_at", "saved_at", "state_revision"].forEach((field) => { merged[field] = current[field]; });
    }
    const interests = new Map((incoming.interests || []).map((interest) => [interest.topic_id, interest]));
    (current.interests || []).forEach((interest) => {
      const existing = interests.get(interest.topic_id);
      if (!existing || interest.revision > existing.revision) interests.set(interest.topic_id, interest);
    });
    merged.interests = [...interests.values()].sort((left, right) => left.topic_id.localeCompare(right.topic_id));
    return merged;
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

  const M2_CARD_FIELDS_V1 = ["card_schema_version", "published_at", "source_name", "story_id", "summary", "title", "url",
    "source_id", "language", "category_ids", "read_at", "saved_at", "state_revision", "interests"];
  const M2_TRANSLATION_FIELDS = ["title_en", "title_zh", "summary_en", "summary_zh", "translation_status"];
  const M2_CARD_FIELDS = [...M2_CARD_FIELDS_V1, ...M2_TRANSLATION_FIELDS];
  // Version 3 adds the element labels: why this story is in front of her.
  const M2_LABEL_FIELDS = ["lane", "lane_label", "surprise_label", "exclusive_label", "also_covered_by"];
  const M2_CARD_FIELDS_V3 = [...M2_CARD_FIELDS, ...M2_LABEL_FIELDS];
  // Version 4 carries the independently measured number of outlets. Names are
  // optional because the retained corpus can prove the count without exposing
  // every member of the source cluster.
  const M2_CARD_FIELDS_V4 = [...M2_CARD_FIELDS_V3, "coverage_count"];
  const M2_LANES = ["updates", "hot", "interested", "surprise", "more"];
  // Same bound the database column carries, so an oversized translated summary
  // is rejected here rather than rendered.
  const MAX_TRANSLATED_SUMMARY = 32000;
  const DISPLAY_LANGUAGES = ["en", "zh"];
  const TRANSLATION_STATUS = ["original", "translated", "untranslated"];
  // The other language's name, in the language currently being read. The
  // section title and the untranslated mark are derived from this, never
  // written out in English, so the site flip renames them for free.
  const OTHER_LANGUAGE_NAME = { en: { zh: "Chinese", en: "English" }, zh: { en: "English", zh: "Chinese" } };
  const LANGUAGE_STRINGS = {
    en: {
      toggleLabel: "中文", exclusiveSection: (other) => `Only in ${other} press`,
      emptyExclusive: (other) => `No stories that only the ${other} press carried today`,
      untranslated: (other) => `Not translated. Shown in ${other}.`,
      endOfRun: "You have read everything in this run. Come back later for more.",
      stillPreparing: "Still preparing your page, try again.",
      alsoCovered: (count) => `Also in ${count} other ${count === 1 ? "source" : "sources"}`,
      search: "Search all retained stories",
    },
    zh: {
      toggleLabel: "EN", exclusiveSection: (other) => `只有${other === "English" ? "英文" : "中文"}媒体报道`,
      emptyExclusive: (other) => `今天没有只有${other === "English" ? "英文" : "中文"}媒体报道的新闻`,
      untranslated: (other) => `未翻译，按原文显示。`,
      endOfRun: "这一轮的报道你都读完了，稍后再来看看。",
      stillPreparing: "页面还在准备，请稍后再试。",
      alsoCovered: (count) => `另有 ${count} 家媒体报道`,
      search: "搜索全部保留的报道",
    },
  };
  const M2_RESPONSE_FIELDS = ["cards", "consent_revision", "fallback_reason", "history_generation",
    "history_revision", "model_version", "next_cursor", "policy_version", "request_id",
    "result_mode", "schema_version", "server_commit_revision"];
  function validateM2Config(value) {
    if (!isObject(value) || typeof value.enabled !== "boolean") fail("M2 reader configuration is invalid.");
    if (!value.enabled) return Object.freeze({ enabled: false });
    // How long to wait before asking again when another request is already
    // buying this view's ranking, and how many times. Config, not a constant.
    value = { in_progress_retry_ms: 2000, in_progress_max_attempts: 3, ...value };
    value = { request_timeout_ms: 8000, ...value };
    // The backend's validated function timeout can be as high as 300 seconds.
    // Keep the transport alive beyond that; the 8s timer only paints the
    // baseline and is not permission to abort a healthy ranking.
    value = { transport_timeout_ms: 310000, ...value };
    if (!exactFields(value, ["enabled", "model_version", "page_size", "policy_version",
      "provider_policy_id", "provider_retention_url", "url", "request_timeout_ms",
      "transport_timeout_ms", "in_progress_retry_ms", "in_progress_max_attempts"]) || !boundedString(value.policy_version, 256) ||
      !boundedString(value.provider_policy_id, 256) ||
      !boundedString(value.model_version, 256) || !safeDestination(value.provider_retention_url) ||
      !Number.isInteger(value.page_size) || value.page_size < 1 || value.page_size > MAX_PAGE_SIZE ||
      !Number.isInteger(value.request_timeout_ms) || value.request_timeout_ms < 1 || value.request_timeout_ms > 8000 ||
      !Number.isInteger(value.transport_timeout_ms) || value.transport_timeout_ms < 310000 || value.transport_timeout_ms > 600000 ||
      !Number.isInteger(value.in_progress_retry_ms) || value.in_progress_retry_ms < 100 || value.in_progress_retry_ms > 10000 ||
      !Number.isInteger(value.in_progress_max_attempts) || value.in_progress_max_attempts < 1 || value.in_progress_max_attempts > 10) {
      fail("M2 reader configuration is invalid.");
    }
    const endpoint = safeDestination(value.url);
    if (!endpoint) fail("M2 reader configuration is invalid.");
    return Object.freeze({ ...value, url: endpoint.replace(/\/$/, "") });
  }
  function validateM2Response(value, expected) {
    // `end_of_run` is OPTIONAL on the wire, so a reader and a ranker can deploy
    // in either order: an older ranker simply never sends it. It is lifted out
    // before the exact-field check and put back after.
    let endOfRun = false;
    if (isObject(value) && "end_of_run" in value) {
      if (typeof value.end_of_run !== "boolean") fail("The M2 feed response was invalid.");
      endOfRun = value.end_of_run;
      delete value.end_of_run;
    }
    const revisionsMatch = expected.allow_frozen_revisions === true
      ? Number.isSafeInteger(value.history_revision) && value.history_revision >= 0 &&
        Number.isSafeInteger(value.server_commit_revision) &&
        value.server_commit_revision >= value.history_revision &&
        value.history_revision <= expected.history_revision
      : value.history_revision === expected.history_revision &&
        value.server_commit_revision === expected.server_commit_revision;
    if (!exactFields(value, M2_RESPONSE_FIELDS) || value.schema_version !== 1 ||
        value.policy_version !== expected.policy_version || value.model_version !== expected.model_version ||
        !revisionsMatch ||
        value.history_generation !== expected.history_generation ||
        value.consent_revision !== expected.consent_revision ||
        !["model", "fallback"].includes(value.result_mode) || typeof value.fallback_reason !== "string" ||
        (value.result_mode === "model" && value.fallback_reason !== "") ||
        (value.result_mode === "fallback" && !boundedString(value.fallback_reason, 256)) ||
        !Array.isArray(value.cards) || value.cards.length > expected.page_size ||
        !(value.next_cursor === null || boundedString(value.next_cursor, 4096))) fail("The M2 feed response was invalid.");
    const seen = new Set();
    value.cards.forEach((card) => {
      // One release accepts both card schemas, so the reader and the ranker can
      // deploy in either order without every card failing validation.
      const labelled = card.card_schema_version >= 3;
      const countedCoverage = card.card_schema_version === 4;
      const translated = card.card_schema_version >= 2;
      if (![1, 2, 3, 4].includes(card.card_schema_version) ||
          !exactFields(card, countedCoverage ? M2_CARD_FIELDS_V4 : labelled ? M2_CARD_FIELDS_V3 : translated ? M2_CARD_FIELDS : M2_CARD_FIELDS_V1) ||
          !STORY_ID.test(card.story_id) ||
          (labelled && (!M2_LANES.includes(card.lane) || !boundedString(card.lane_label, 40) ||
            !(card.surprise_label === null || boundedString(card.surprise_label, 80)) ||
            !(card.exclusive_label === null || boundedString(card.exclusive_label, 80)) ||
            !Array.isArray(card.also_covered_by) ||
            !card.also_covered_by.every((name) => boundedString(name, 200)) ||
            (countedCoverage && (!Number.isSafeInteger(card.coverage_count) || card.coverage_count < 0)))) ||
          (translated && (
            !DISPLAY_LANGUAGES.every((code) => typeof card[`title_${code}`] === "string" &&
              card[`title_${code}`].length <= 2000 && typeof card[`summary_${code}`] === "string" &&
              card[`summary_${code}`].length <= MAX_TRANSLATED_SUMMARY) ||
            !isObject(card.translation_status) || !exactFields(card.translation_status, DISPLAY_LANGUAGES) ||
            !DISPLAY_LANGUAGES.every((code) => TRANSLATION_STATUS.includes(card.translation_status[code])))) ||
          !boundedString(card.title, 2000) || typeof card.summary !== "string" ||
          !boundedString(card.source_name, 200) || !validTimestamp(card.published_at) ||
          !safeDestination(card.url) || !boundedString(card.source_id, 512) || !["en", "zh"].includes(card.language) ||
          !Array.isArray(card.category_ids) || !card.category_ids.every((id) => TOPIC_ID.test(id)) ||
          !validNullableTimestamp(card.read_at) || !validNullableTimestamp(card.saved_at) ||
          !Number.isSafeInteger(card.state_revision) || card.state_revision < 0 || !Array.isArray(card.interests) ||
          !card.interests.every((interest) => exactFields(interest, ["topic_id", "signal", "revision"]) &&
            TOPIC_ID.test(interest.topic_id) && ["more_like", "less_like"].includes(interest.signal) &&
            Number.isSafeInteger(interest.revision) && interest.revision >= 0) ||
          seen.has(card.story_id)) fail("The M2 feed response was invalid.");
      if (!labelled) {
        // A ranker that has not shipped the recipe yet sends no labels. The
        // reader still renders the card; it just has nothing to say about why.
        card.lane = null; card.lane_label = null; card.surprise_label = null;
        card.exclusive_label = null; card.also_covered_by = [];
      }
      if (!countedCoverage) card.coverage_count = Math.max(1, card.also_covered_by.length + 1);
      if (!translated) {
        const other = card.language === "en" ? "zh" : "en";
        card[`title_${card.language}`] = card.title;
        card[`summary_${card.language}`] = card.summary;
        card[`title_${other}`] = "";
        card[`summary_${other}`] = "";
        card.translation_status = { [card.language]: "original", [other]: "untranslated" };
      }
      card.card_schema_version = 4;
      seen.add(card.story_id);
    });
    value.end_of_run = endOfRun;
    return value;
  }
  function createM2Service(rawConfig, sessionProvider, fetchImpl = fetch) {
    const config = validateM2Config(rawConfig);
    if (!config.enabled) return Object.freeze({ enabled: false });
    async function request(path, method, body, expected) {
      // Refresh before a request when the bearer cannot outlive the longest
      // accepted transport window.  The extra 30 seconds covers client/server
      // clock skew and the final response validation round trip.
      const minimumValiditySeconds = Math.ceil(config.transport_timeout_ms / 1000) + 30;
      const before = await sessionProvider(minimumValiditySeconds);
      if (!before || !boundedString(before.access_token, 16384)) fail("Sign in to continue.");
      const url = `${config.url}${path}`;
      const response = await fetchImpl(url, { method, headers: {
        accept: "application/json", "content-type": "application/json",
        authorization: `Bearer ${before.access_token}`,
      }, body: body === null ? undefined : JSON.stringify(body), credentials: "omit",
      redirect: "error", cache: "no-store", referrerPolicy: "no-referrer", signal: AbortSignal.timeout(config.transport_timeout_ms) });
      const payload = await boundedJson(response, "The M2 reader response was invalid.");
      if (response.redirected !== false || response.url !== url) fail("The M2 endpoint redirected unexpectedly.");
      const after = await sessionProvider(0);
      const beforeOwner = boundedString(before.user_id, 256) ? before.user_id : null;
      const afterOwner = after && boundedString(after.user_id, 256) ? after.user_id : null;
      // Production sessions carry user_id, so an ordinary token rotation for
      // the same owner is accepted while an account switch is still refused.
      // The token comparison remains only for small contract doubles that do
      // not model identity.
      const sameOwner = beforeOwner && afterOwner
        ? beforeOwner === afterOwner
        : after && after.access_token === before.access_token;
      if (!after || !sameOwner) fail("The signed-in account changed.");
      if (!response.ok) {
        // A ranking prompt revision is a QUESTION, not an outage: the owner
        // agreed to a different provider policy than the one now running, and
        // one tap on the existing consent control fixes it. Collapsing this
        // into the generic failure is how a deploy looks like a dead feed.
        // The ranking for this view is being bought right now by another
        // request. Retryable, and NOT a reason to fall back to the captured
        // edition: the answer exists in a moment.
        if (isObject(payload) && payload.error === "ranking_in_progress") {
          const error = new Error("Still preparing your page.");
          error.rankingInProgress = true;
          throw error;
        }
        if (isObject(payload) && payload.error === "cursor_version") {
          const error = new Error("Refreshing this page onto the current reader version.");
          error.staleCursor = true;
          throw error;
        }
        if (isObject(payload) && payload.error === "provider_consent_required") {
          const error = new Error("Personalized ranking needs your permission again.");
          error.consentRequired = true;
          error.providerPolicyId = boundedString(payload.provider_policy_id, 256)
            ? payload.provider_policy_id : "";
          throw error;
        }
        fail("The M2 reader request failed.");
      }
      return validateM2Response(payload, expected);
    }
    return Object.freeze({
      enabled: true,
      retentionUrl: config.provider_retention_url,
      rank: (history, eligibility, excludeStoryIds = []) => request("/rank", "POST", {
        schema_version: 1, policy_version: config.policy_version, model_version: config.model_version,
        history_revision: history.included_history_revision,
        server_commit_revision: history.history_revision,
        history_generation: history.history_generation, consent_revision: history.consent_revision,
        page_size: config.page_size, eligibility, exclude_story_ids: excludeStoryIds,
      }, { ...history, policy_version: config.policy_version, model_version: config.model_version,
        page_size: config.page_size, server_commit_revision: history.history_revision,
        history_revision: history.included_history_revision, allow_frozen_revisions: true }),
      page: (cursor, binding) => request(`/page?cursor=${encodeURIComponent(cursor)}`, "GET", null,
        { ...binding, policy_version: config.policy_version, model_version: config.model_version,
          page_size: config.page_size }),
    });
  }

  const contract = {
    actionTopic, applyInterestTopic, applyServerRank, applyServerState, beginStateMutation, createApi, createM2Service, createStoryCard, drainUpdates, effectiveTopic, finishStateMutation, loadedStatus, mergeM2CardState, mergeTopicMembership, nextFeedCursor, nextSavedCursor,
    rankingReason, run, safeDestination,
    validateDiscovery, validateFeedPage, validateM2Config, validateM2Response, validateSavedPage, validateLatestPublication, validateUpdates,
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
    let authEpoch = 0;
    let ownerExportEpoch = 0;
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
    const m2Controls = document.getElementById("m2-controls");
    const meta = (name) => document.querySelector(`meta[name="news-curator-m2-${name}"]`)?.content || "";
    let m2 = null;
    let m2Config = null;
    try {
      m2Config = validateM2Config(meta("enabled") === "true" ? {
        enabled: true, url: meta("endpoint"), policy_version: meta("policy-version"),
        model_version: meta("model-version"), provider_policy_id: meta("provider-policy-id"),
        provider_retention_url: meta("provider-retention-url"), page_size: Number(meta("page-size")),
        request_timeout_ms: Number(meta("request-timeout-ms") || 8000),
        transport_timeout_ms: Number(meta("transport-timeout-ms") || meta("request-timeout-ms") || 8000),
      } : { enabled: false });
      m2 = createM2Service(m2Config, (minimumValiditySeconds = 0) =>
        auth.sessionForRequest(undefined, undefined, minimumValiditySeconds));
    } catch (_) { announce("Personalized feed configuration is unavailable. Public stories remain available."); }
    let m2Active = false, m2Sequence = 0, m2InteractionEpoch = 0, m2Cursor = null, m2Binding = null, m2Key = null, m2Topic = null;
    let m2Section = null, m2PublicCards = [], m2Position = 0, m2Entries = [];
    const LANGUAGE_STORAGE_KEY = "news-curator-display-language";
    const configuredLanguage = document.querySelector('meta[name="news-curator-display-language"]')?.content;
    // The reader's own choice wins over the site default; neither is hardcoded.
    let displayLanguage = DISPLAY_LANGUAGES.includes(configuredLanguage) ? configuredLanguage : "en";
    try {
      const stored = localStorage.getItem(LANGUAGE_STORAGE_KEY);
      if (DISPLAY_LANGUAGES.includes(stored)) displayLanguage = stored;
    } catch (_) { /* a browser with storage denied still reads the site default */ }
    const strings = () => LANGUAGE_STRINGS[displayLanguage];
    const otherLanguageName = () =>
      OTHER_LANGUAGE_NAME[displayLanguage][displayLanguage === "en" ? "zh" : "en"];
    let behaviorWrites = Promise.resolve();
    let m2SearchTimer = null;
    const searchBox = document.getElementById("q");
    document.querySelectorAll(".state-action:not(.read-action)").forEach((button) => { button.hidden = false; });
    const cards = new Map();
    const pendingStateMutations = new Map();
    const pendingInterestMutations = new Map();
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
    const editionMeta = document.querySelector('.edition-meta');
    const editionLabels = [...document.querySelectorAll('.crumb, .eyebrow, .railnote, .brand small')]
      .map((node) => ({ node, text: node.textContent }));
    const editionMetaSpans = editionMeta ? [...editionMeta.querySelectorAll('span')] : [];
    const publicEditionMeta = editionMetaSpans.map((span) => span.textContent);
    const publicStoryCount = editionMetaSpans.find((span) => / stories?$/.test(span.textContent.trim()));
    const staleMeta = document.getElementById('stale');
    const publicStale = staleMeta ? {
      built: staleMeta.dataset.built, text: staleMeta.textContent, hidden: staleMeta.hidden,
      previousHidden: staleMeta.previousElementSibling?.hidden,
    } : null;
    function editionTime(iso) {
      const formatted = new Intl.DateTimeFormat('en-US', {
        month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric',
        minute: '2-digit', timeZoneName: 'short',
        timeZone: editionMeta?.dataset.timezone || 'America/New_York',
      }).format(new Date(iso));
      return formatted.replace(/^([^,]+), (\d{4}), (.+)$/, '$1, $2 at $3');
    }
    function showPrivateEditionMeta(edition) {
      if (editionMetaSpans[0]) editionMetaSpans[0].textContent = `Built ${editionTime(edition.generated_at)}`;
      if (publicStoryCount) {
        const count = edition.entries.length;
        publicStoryCount.textContent = `${count} ${count === 1 ? 'story' : 'stories'}`;
      }
      if (staleMeta) {
        staleMeta.dataset.built = edition.generated_at;
        staleMeta.textContent = '';
        staleMeta.hidden = true;
        if (staleMeta.previousElementSibling) staleMeta.previousElementSibling.hidden = true;
      }
    }
    function restorePublicEditionMeta() {
      editionMetaSpans.forEach((span, index) => { span.textContent = publicEditionMeta[index]; });
      if (staleMeta && publicStale) {
        staleMeta.dataset.built = publicStale.built;
        staleMeta.textContent = publicStale.text;
        staleMeta.hidden = publicStale.hidden;
        if (staleMeta.previousElementSibling) staleMeta.previousElementSibling.hidden = publicStale.previousHidden;
      }
    }
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
      const shortageNotice = ["hot", "surprise"].includes(lane) && discoveryEdition?.shortfalls[lane] > 0
        ? ` Fewer qualified ${lane === "hot" ? "Hot" : "Surprise"} stories were selected for this edition.`
        : "";
      discoveryMessage(count
        ? `${count} ${count === 1 ? "story" : "stories"} in ${lane}.${shortageNotice}${discoveryEdition.stale ? " This edition is older than usual." : ""}`
        : `${shortageNotice.trim() || emptyReasons[lane]}`);
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
      restorePublicEditionMeta();
      if (clearEdition) {
        discoveryEdition = null; pendingDiscovery = null;
        if (discoveryNotice) discoveryNotice.hidden = true;
      }
      discoveryControls?.querySelectorAll("[data-discovery-lane]").forEach((button) => button.setAttribute("aria-pressed", "false"));
      view.apply(); refreshLoadButton();
    }
    function showDiscovery(edition) {
      if (!signedIn() || !discoveryControls || m2?.enabled) return;
      leaveDiscovery();
      discoveryEdition = edition;
      showPrivateEditionMeta(edition);
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
      if (!discoveryControls || !signedIn() || m2Active || m2?.enabled) return;
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
      if (m2?.enabled) return;
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

    function usesM2() { return Boolean(m2?.enabled && signedIn() && selectedTopic() !== "__saved__"); }
    function showM2Policy() {
      const retention = document.getElementById("m2-provider-retention");
      if (retention && m2Config?.enabled) {
        retention.href = m2Config.provider_retention_url;
        retention.hidden = false;
      }
    }
    function unknownM2Consent() {
      const local = document.getElementById("m2-local-learning"), provider = document.getElementById("m2-provider-processing");
      if (local) { local.indeterminate = true; local.disabled = true; }
      if (provider) { provider.indeterminate = true; provider.disabled = true; }
    }
    function syncM2Consent(history) {
      const local = document.getElementById("m2-local-learning"), provider = document.getElementById("m2-provider-processing");
      if (local) { local.indeterminate = false; local.checked = history.learning_enabled; local.disabled = false; }
      if (provider) {
        provider.indeterminate = false;
        provider.checked = history.provider_processing_enabled;
        provider.disabled = !history.learning_enabled;
      }
    }
    function m2Eligibility() {
      const topic = selectedTopic();
      return { category: topic === "__all__" ? null : topicIdForSlug(topic), query: searchBox?.value.trim() || null };
    }
    function enqueueBehavior(operation) {
      const epoch = authEpoch;
      const pending = behaviorWrites.catch(() => {}).then(async () => {
        if (epoch !== authEpoch || !signedIn()) fail("The signed-in account changed.");
        const snapshot = await api.historySnapshot();
        if (epoch !== authEpoch) fail("The signed-in account changed.");
        const value = await operation(snapshot);
        if (epoch !== authEpoch) fail("The signed-in account changed.");
        return value;
      });
      behaviorWrites = pending;
      return pending;
    }
    async function drainBehaviorWrites() {
      while (true) {
        const pending = behaviorWrites;
        await pending.catch(() => {});
        if (behaviorWrites === pending) return;
      }
    }
    async function behaviorIdentity(snapshot) {
      const bytes = await crypto.subtle.digest("SHA-256", encoder.encode(idempotencyKey()));
      return { p_event_id: "event:" + [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join(""),
        p_occurred_at: new Date().toISOString(), p_expected_history_generation: snapshot.history_generation };
    }
    function recordBehavior(type, payload) {
      if (!m2?.enabled || !signedIn()) return Promise.resolve();
      return enqueueBehavior(async (snapshot) => {
        if (!snapshot.learning_enabled) return;
        return api.appendBehaviorEvent({ ...await behaviorIdentity(snapshot), p_event_type: type,
          p_payload: { ...payload, surface: "reader" }, p_schema_version: 1 });
      });
    }
    function clearM2Cards() {
      cards.forEach((card, id) => {
        if (card.dataset.m2Card !== "true") return;
        clearPrivateCardState(card); view.removeCard(card); card.replaceChildren(); card.remove(); cards.delete(id);
      });
      m2Section?.remove(); m2Section = null; m2Position = 0; m2Entries = [];
    }
    function leaveM2(invalidate = true) {
      if (invalidate) m2Sequence += 1;
      m2Cursor = null; m2Binding = null; m2Key = null; m2Topic = null;
      clearTimeout(m2SearchTimer);
      if (m2Active) {
        clearM2Cards();
        m2PublicCards.forEach(({ card, parent }) => { parent.append(card); cards.set(card.dataset.storyId, card); view.addCard(card); });
        m2PublicCards = [];
      }
      m2Active = false;
      if (editionMeta) editionMeta.style.display = "";
      editionLabels.forEach(({ node, text }) => { node.textContent = text; });
      if (m2Controls) m2Controls.hidden = true;
      if (searchBox) searchBox.placeholder = "Search this edition";
      restorePublicEditionMeta();
      view.apply();
    }
    // Switching the language must never cost a request: every card already
    // carries both language slots, so the toggle only chooses which to read.
    function displayRow(entry, reason) {
      const status = entry.translation_status[displayLanguage];
      const translated = status !== "untranslated";
      const title = translated ? entry[`title_${displayLanguage}`] : entry.title;
      const summary = translated ? entry[`summary_${displayLanguage}`] : entry.summary;
      // The language-exclusive section is a SERVER-side selection, so no story
      // carries it in its own category_ids. Without this the client-side
      // membership filter hides every card the section just fetched.
      const selectedId = topicIdForSlug(selectedTopic());
      const topicIds = exclusiveSelected() && !entry.category_ids.includes(selectedId)
        ? [...entry.category_ids, selectedId] : entry.category_ids;
      return { ...entry, title: title || entry.title, summary: summary || entry.summary,
        canonical_url: entry.url, topic_ids: topicIds, source_kind: "outlet",
        coverage_mentions: [], topic_ranks: {}, ranking_explanation: reason,
        translation_mark: status === "untranslated"
          ? strings().untranslated(OTHER_LANGUAGE_NAME[displayLanguage][entry.language]) : "",
        element_labels: [entry.lane_label, entry.surprise_label, entry.exclusive_label]
          .filter((label) => typeof label === "string" && label !== ""),
        also_covered_by: Array.isArray(entry.also_covered_by) ? entry.also_covered_by : [],
        coverage_count: Number.isSafeInteger(entry.coverage_count) ? entry.coverage_count : 1 };
    }
    // Every card says why it is on the page. The wording comes from the server,
    // which reads it from config, so renaming a pool never means editing the
    // reader.
    function markElementLabels(card, row) {
      if (!row.element_labels || !row.element_labels.length) return;
      const strip = element("p", "element-labels");
      row.element_labels.forEach((text) => strip.append(element("span", "element-label", text)));
      strip.dataset.lane = row.lane || "";
      card.querySelector(".story-heading")?.after(strip);
    }
    // The outlets whose duplicate rows collapsed into this one. It was computed,
    // persisted and validated, and then never shown: the reader could not tell a
    // story three outlets carried from one nobody else did.
    function markAlsoCovered(card, row) {
      const names = Array.isArray(row.also_covered_by) ? row.also_covered_by : [];
      const otherCount = Math.max(names.length, (row.coverage_count || 1) - 1);
      if (otherCount < 1) return;
      const line = element("p", "also-covered", strings().alsoCovered(otherCount));
      if (names.length) line.title = names.join(", ");
      card.querySelector(".story-heading")?.after(line);
    }
    function markUntranslated(card, row) {
      if (!row.translation_mark) return;
      const mark = element("p", "translation-mark", row.translation_mark);
      mark.dataset.translationStatus = "untranslated";
      card.querySelector(".story-heading")?.after(mark);
    }
    function refreshLanguageLabels() {
      const toggle = document.getElementById("m2-language-toggle");
      if (toggle) {
        toggle.textContent = strings().toggleLabel;
        toggle.dataset.displayLanguage = displayLanguage;
      }
      document.querySelectorAll('.chip[data-language-exclusive="true"]').forEach((chip) => {
        chip.textContent = strings().exclusiveSection(otherLanguageName());
        // The ONE empty element on the page renders this wording when the
        // section is selected and nothing came back.
        chip.dataset.emptyText = strings().emptyExclusive(otherLanguageName());
      });
      const title = m2Section?.querySelector(".section-title");
      if (title && exclusiveSelected()) title.textContent = strings().exclusiveSection(otherLanguageName());
      if (searchBox && m2Active) searchBox.placeholder = strings().search;
    }
    function exclusiveSelected() {
      const chip = document.querySelector('.chip[data-language-exclusive="true"]');
      return Boolean(chip && selectedTopic() === chip.dataset.filter);
    }
    // The rerender rebuilds every card from m2Entries, which is the SERVER
    // snapshot. Without this, saving a story and then switching language shows
    // the card unsaved again, because the local mutation lived only in the DOM.
    function rememberM2State(card, state) {
      const storyId = card?.dataset?.storyId;
      if (!storyId || !m2Active) return;
      const entry = m2Entries.find((candidate) => candidate.story_id === storyId);
      if (!entry) return;
      ["read_at", "saved_at", "state_revision", "interests"].forEach((field) => {
        if (Object.prototype.hasOwnProperty.call(state, field)) entry[field] = state[field];
      });
    }
    function restorePendingMutations(card) {
      const storyId = card.dataset.storyId;
      const state = pendingStateMutations.get(storyId);
      if (state) {
        const renderedRevision = Number(card.dataset.stateRevision || 0);
        if (Number.isSafeInteger(renderedRevision) && renderedRevision > Number(state.baseline?.state_revision || 0)) {
          state.baseline = {
            token: state.token,
            read_at: card.classList.contains("is-read") ? "local" : null,
            saved_at: card.classList.contains("is-saved") ? "local" : null,
            state_revision: renderedRevision,
          };
        }
        card.newsCuratorStateMutationToken = state.token;
        card.newsCuratorStateMutationBaseline = state.baseline;
        card.newsCuratorStateMutationPresentation = state.presentation;
        if (state.pendingRead) card.newsCuratorPendingReadIntent = state.pendingRead;
        applyServerState(card, {
          read_at: (state.pendingRead?.read ?? state.presentation.read) ? "local" : null,
          saved_at: state.presentation.saved ? "local" : null,
        });
      }
      const interest = pendingInterestMutations.get(storyId);
      if (interest) card.newsCuratorInterestMutation = interest;
    }
    function rerenderM2Cards() {
      if (!m2Active || !m2Binding) return;
      const entries = m2Entries.slice();
      const pendingMutations = new Map();
      cards.forEach((card, storyId) => {
        if (card.dataset.m2Card !== "true") return;
        const pending = {
          stateToken: card.newsCuratorStateMutationToken,
          stateBaseline: card.newsCuratorStateMutationBaseline,
          statePresentation: card.newsCuratorStateMutationPresentation,
          pendingRead: card.newsCuratorPendingReadIntent,
          interest: card.newsCuratorInterestMutation,
        };
        if (pending.stateToken || pending.pendingRead || pending.interest) pendingMutations.set(storyId, pending);
      });
      const reason = m2Binding.result_mode === "model"
        ? "Ranked using your current query and permitted reading history."
        : "Freshness order. Model ranking was not used.";
      clearM2Cards();
      m2Entries = entries;
      if (!m2Section) {
        m2Section = element("section", "topic-section"); m2Section.dataset.section = "__m2__";
        m2Section.append(element("div", "grid")); document.getElementById("sections").append(m2Section);
      }
      entries.forEach((entry) => {
        const row = displayRow(entry, reason);
        const card = createStoryCard(row, selectedTopic(), topicSlugForId, topicIdForSlug(selectedTopic()));
        card.dataset.m2Card = "true"; card.dataset.m2Position = String(++m2Position);
        card.dataset.m2Topic = m2Topic;
        // The view filter hides any remote card whose m2Query differs from the
        // live search box. Omitting it here blanked the page on every toggle.
        card.dataset.m2Query = (searchBox?.value.trim() || "").toLowerCase();
        markUntranslated(card, row);
        markElementLabels(card, row);
        markAlsoCovered(card, row);
        const interest = card.querySelector(".interest-action");
        if (interest) {
          const less = element("button", "state-action less-interest-action", "Less like this"); less.type = "button";
          interest.after(less);
        }
        const pending = pendingMutations.get(entry.story_id);
        if (pending?.stateToken) {
          card.newsCuratorStateMutationToken = pending.stateToken;
          card.newsCuratorStateMutationBaseline = pending.stateBaseline;
          card.newsCuratorStateMutationPresentation = pending.statePresentation;
        }
        if (pending?.pendingRead) card.newsCuratorPendingReadIntent = pending.pendingRead;
        if (pending?.interest) card.newsCuratorInterestMutation = pending.interest;
        restorePendingMutations(card);
        cards.set(entry.story_id, card); hydratedTopics(card).add(m2Topic);
        m2Section.querySelector(".grid").append(card); view.addCard(card);
      });
      applyExclusiveSectionTitle();
      view.apply(); refreshStateControls(); refreshInterestControls();
    }
    function applyExclusiveSectionTitle() {
      if (!m2Section) return;
      let title = m2Section.querySelector(".section-title");
      if (!exclusiveSelected()) { title?.remove(); return; }
      if (!title) {
        title = element("h2", "section-title");
        m2Section.prepend(title);
      }
      title.textContent = strings().exclusiveSection(otherLanguageName());
      // ONE empty element on the page. The section supplies its wording through
      // the chip, and the view renders it; a second node beside the generic one
      // showed two contradictory messages at once.
      document.querySelectorAll('.chip[data-language-exclusive="true"]').forEach((chip) => {
        chip.dataset.emptyText = strings().emptyExclusive(otherLanguageName());
      });
    }
    function switchDisplayLanguage() {
      displayLanguage = displayLanguage === "en" ? "zh" : "en";
      try { localStorage.setItem(LANGUAGE_STORAGE_KEY, displayLanguage); } catch (_) { /* ignore */ }
      refreshLanguageLabels();
      rerenderM2Cards();
      announce(strings().toggleLabel);
    }
    function applyM2Page(response, append, eligibility, responseTopic) {
      const currentEntries = new Map(m2Entries.map((entry) => [entry.story_id, entry]));
      const sameHistoryContext = Boolean(m2Binding &&
        m2Binding.history_generation === response.history_generation &&
        m2Binding.consent_revision === response.consent_revision);
      if (!m2Active) {
        leaveDiscovery(true);
        m2PublicCards = [...cards.values()].map((card) => ({ card, parent: card.parentNode }));
        m2PublicCards.forEach(({ card }) => { clearPrivateCardState(card); view.removeCard(card); card.remove(); });
        cards.clear(); m2Active = true;
        if (editionMeta) editionMeta.style.display = "none";
        editionLabels.forEach(({ node, text }) => {
          node.textContent = node.matches('.crumb') ? text.replace(/Today's edition/i, "Reading feed")
            : node.matches('.eyebrow') ? "Reading feed"
            : node.matches('.railnote') ? "Refresh the feed for the latest available stories."
            : "Reading Companion";
        });
      }
      if (!append) clearM2Cards();
      m2Topic = responseTopic;
      if (!m2Section) {
        m2Section = element("section", "topic-section"); m2Section.dataset.section = "__m2__";
        m2Section.append(element("div", "grid")); document.getElementById("sections").append(m2Section);
      }
      const reason = response.result_mode === "model" ? "Ranked using your current query and permitted reading history." : "Freshness order. Model ranking was not used.";
      response.cards.forEach((incoming) => {
        const entry = mergeM2CardState(incoming, currentEntries.get(incoming.story_id), sameHistoryContext);
        if (cards.has(entry.story_id)) return;
        m2Entries.push(entry);
        const row = displayRow(entry, reason);
        const card = createStoryCard(row, selectedTopic(), topicSlugForId, topicIdForSlug(selectedTopic()));
        card.dataset.m2Card = "true"; card.dataset.m2Position = String(++m2Position);
        card.dataset.m2Topic = m2Topic;
        card.dataset.m2Query = (eligibility.query || "").toLowerCase();
        const interest = card.querySelector(".interest-action");
        if (interest) {
          const less = element("button", "state-action less-interest-action", "Less like this"); less.type = "button";
          interest.after(less);
        }
        restorePendingMutations(card);
        markUntranslated(card, row);
        markElementLabels(card, row);
        markAlsoCovered(card, row);
        cards.set(entry.story_id, card); hydratedTopics(card).add(m2Topic);
        m2Section.querySelector(".grid").append(card); view.addCard(card);
      });
      m2Cursor = response.next_cursor; m2Binding = response;
      // The section's own empty state must exist BEFORE the view decides
      // whether to show the generic one, or both render together.
      applyExclusiveSectionTitle(); refreshLanguageLabels();
      document.getElementById("discovery-controls")?.setAttribute("hidden", "");
      if (m2Controls) m2Controls.hidden = false;
      if (searchBox) searchBox.placeholder = strings().search;
      const mode = document.getElementById("m2-mode");
      if (mode) mode.textContent = reason;
      if (publicStoryCount) publicStoryCount.textContent = `${cards.size} stories loaded`;
      view.apply(); refreshStateControls(); refreshInterestControls();
    }
    async function loadM2(append = false, searchEvent = false, attempt = 1) {
      if (!usesM2()) return;
      unknownM2Consent(); showM2Policy();
      const epoch = authEpoch, request = ++m2Sequence, interactionEpoch = m2InteractionEpoch, eligibility = m2Eligibility();
      const key = JSON.stringify(eligibility);
      const pageRequest = { topic: selectedTopic(), epoch };
      pageRequests.add(pageRequest); refreshLoadButton();
      let baselineShown = false;
      const showBaseline = () => {
        if (epoch !== authEpoch || request !== m2Sequence || !usesM2()) return;
        baselineShown = true;
        if (!append || !m2Active) leaveM2(false);
        if (m2Controls) m2Controls.hidden = false;
        const mode = document.getElementById("m2-mode");
        if (mode) mode.textContent = "Personalized feed is still loading.";
        announce("Personalized feed is still loading.");
      };
      // A re-consent prompt is its own state. It says what happened, in one
      // line, and leaves the consent control on screen so the fix is one tap.
      // Keep the loading state and ask again. Bounded: after the configured
      // attempts she gets a plain sentence, never the captured-edition fallback,
      // because nothing is wrong with her feed.
      const stillPreparing = () => {
        if (epoch !== authEpoch || request !== m2Sequence || !usesM2()) return;
        const mode = document.getElementById("m2-mode");
        if (mode) mode.textContent = strings().stillPreparing;
        announce(strings().stillPreparing);
      };
      const consentRequired = () => {
        if (epoch !== authEpoch || request !== m2Sequence || !usesM2()) return;
        showBaseline();
        if (request !== m2Sequence) return;
        m2Sequence += 1;
        if (m2Controls) m2Controls.hidden = false;
        const mode = document.getElementById("m2-mode");
        const message = "Personalized ranking needs your permission again. Turn it back on to resume.";
        if (mode) {
          mode.textContent = message;
          mode.dataset.consentRequired = "true";
        }
        announce(message);
      };
      const terminalFallback = () => {
        if (epoch !== authEpoch || request !== m2Sequence || !usesM2()) return;
        const retainedPage = append && m2Active;
        showBaseline();
        if (request !== m2Sequence) return;
        m2Sequence += 1;
        const mode = document.getElementById("m2-mode");
        if (retainedPage) {
          if (mode) mode.textContent = "Could not load more. Your current stories are still available.";
          announce("Could not load more. Your current stories are still available.");
        } else {
          if (mode) mode.textContent = "Captured edition fallback. Personalized ranking did not finish.";
          announce("Showing the captured edition. Personalized ranking did not finish.");
        }
      };
      const deadline = setTimeout(showBaseline, m2Config.request_timeout_ms);
      // This deadline starts before queued behavior writes and history retrieval.
      // It bounds the reader request, while an aborted browser request cannot prove
      // that upstream work stopped.
      const transportDeadline = setTimeout(terminalFallback, m2Config.transport_timeout_ms);
      try {
        if (searchEvent && eligibility.query) await recordBehavior("search_query", { query: eligibility.query });
        await drainBehaviorWrites();
        const history = await api.historySnapshot();
        if (epoch !== authEpoch || request !== m2Sequence || !usesM2()) return;
        syncM2Consent(history);
        const canContinue = append && key === m2Key && m2Cursor && m2Binding &&
          history.history_generation === m2Binding.history_generation && history.consent_revision === m2Binding.consent_revision;
        // A new eligible request always carries the committed history. The
        // server freezes existing pages and re-ranks continuation windows.
        const response = canContinue
          // THE CONTRACT, stated once: /page returns the FROZEN order's binding,
          // so the reader compares against the frozen binding and not against
          // the live revision. Comparing against the live one meant that reading
          // or saving a story made a perfectly valid frozen page look invalid
          // here, and the reader dropped to the captured-edition fallback right
          // after the server had stopped re-ranking for exactly that reason.
          ? await m2.page(m2Cursor, { ...m2Binding })
          : await m2.rank(history, eligibility);
        if (epoch !== authEpoch || request !== m2Sequence || !usesM2()) return;
        // A read, save or interest click may have started after this request
        // took its server snapshot. Let that write finish before replacing the
        // old card, so its confirmed state reaches m2Entries and can be merged
        // into this frozen response instead of being lost with a detached node.
        await drainBehaviorWrites();
        if (epoch !== authEpoch || request !== m2Sequence || !usesM2() ||
            JSON.stringify(m2Eligibility()) !== key) return;
        if (baselineShown) {
          await drainBehaviorWrites();
          const latest = await api.historySnapshot();
          // Scoped the same way the server scopes its own staleness check. The
          // behavior revisions move on every read and every save, and treating
          // that as a reason to throw away a slow-but-valid response is the
          // client-side half of the bug the server just stopped having. What
          // still invalidates a response: a different owner, a different
          // eligibility, a history reset, a consent change.
          if (epoch !== authEpoch || request !== m2Sequence || !usesM2() || m2InteractionEpoch !== interactionEpoch ||
              JSON.stringify(m2Eligibility()) !== key || latest.history_generation !== history.history_generation ||
              latest.consent_revision !== history.consent_revision || latest.learning_enabled !== history.learning_enabled ||
              latest.provider_processing_enabled !== history.provider_processing_enabled ||
              latest.provider_policy_id !== history.provider_policy_id) {
            terminalFallback(); return;
          }
        }
        applyM2Page(response, Boolean(canContinue), eligibility, pageRequest.topic); m2Key = key;
        if (response.end_of_run) {
          // Not a failure and not an empty page: she has read the whole run.
          const mode = document.getElementById("m2-mode");
          if (mode) mode.textContent = strings().endOfRun;
          announce(strings().endOfRun);
        }
        announce(response.cards.length ? `${cards.size} stories loaded.` : "No matching stories found in the retained corpus.");
        if (!append && eligibility.query && !response.cards.length) {
          await recordBehavior("search_zero_results", { query: eligibility.query, result_count: 0 });
        }
      } catch (error) {
        if (error && error.rankingInProgress) {
          if (attempt < m2Config.in_progress_max_attempts) {
            clearTimeout(deadline); clearTimeout(transportDeadline);
            pageRequests.delete(pageRequest); refreshLoadButton();
            await new Promise((resolve) => setTimeout(resolve, m2Config.in_progress_retry_ms));
            return loadM2(append, searchEvent, attempt + 1);
          }
          stillPreparing();
        } else if (error && error.staleCursor && append) {
          // A cursor from the pre-atomic release cannot safely share the new
          // response budget. Start one current-contract rank instead of
          // showing a dead feed or retrying the stale page cursor.
          clearTimeout(deadline); clearTimeout(transportDeadline);
          m2Cursor = null; m2Binding = null;
          return loadM2(false, false);
        } else if (error && error.consentRequired) consentRequired(); else terminalFallback();
      } finally {
        clearTimeout(deadline); clearTimeout(transportDeadline); pageRequests.delete(pageRequest); refreshLoadButton();
      }
    }
    const saveM2Consent = async () => {
      const local = document.getElementById("m2-local-learning"), provider = document.getElementById("m2-provider-processing");
      const learning = local.checked, processing = learning && provider.checked;
      // Invalidate visible history-derived order immediately, before the write.
      m2Sequence += 1; clearM2Cards(); m2Cursor = null; m2Binding = null;
      await enqueueBehavior(() => api.setBehaviorConsent(learning, processing, processing ? m2Config.provider_policy_id : null));
      await loadM2();
    };
    ["m2-local-learning", "m2-provider-processing"].forEach((id) => document.getElementById(id)?.addEventListener("change", () => {
      void saveM2Consent().catch(() => announce("Consent could not be updated. Try again."));
    }));
    document.getElementById("m2-language-toggle")?.addEventListener("click", () => { switchDisplayLanguage(); });
    refreshLanguageLabels();
    document.getElementById("m2-refresh")?.addEventListener("click", () => { void loadM2(); });
    document.getElementById("m2-clear-history")?.addEventListener("click", () => {
      abortOwnerExport();
      m2Sequence += 1; clearM2Cards(); m2Cursor = null; m2Binding = null;
      void enqueueBehavior(() => api.clearBehaviorHistory()).then(() => loadM2())
        .catch(() => announce("Learning history could not be cleared. Try again."));
    });
    document.getElementById("m2-download-data")?.addEventListener("click", () => { void downloadOwnerData(); });
    document.addEventListener("pointerdown", () => { m2InteractionEpoch += 1; }, true);
    document.addEventListener("keydown", () => { m2InteractionEpoch += 1; }, true);
    searchBox?.addEventListener("input", () => {
      if (!usesM2()) return;
      m2Sequence += 1; clearTimeout(m2SearchTimer);
      m2SearchTimer = setTimeout(() => { void loadM2(false, true); }, 300);
    });

    function announce(message) { status.textContent = message; }
    function abortOwnerExport() {
      ownerExportEpoch += 1;
      const button = document.getElementById("m2-download-data");
      if (button) button.disabled = false;
    }
    async function downloadOwnerData() {
      const button = document.getElementById("m2-download-data");
      if (!button || button.disabled || !requireSignIn()) return;
      const runEpoch = ++ownerExportEpoch, accountEpoch = authEpoch;
      button.disabled = true;
      let rows = [];
      let rowBytes = 0;
      try {
        const initialSession = await auth.sessionForRequest();
        if (!initialSession || !boundedString(initialSession.user_id, 128)) fail("Sign in to continue.");
        let cursor = null, fence = null, ownerId = null, totalRows = null, maximumBytes = null;
        const seen = new Set();
        do {
          if (runEpoch !== ownerExportEpoch || accountEpoch !== authEpoch) fail("The data export was canceled.");
          const page = await api.ownerExportPage(cursor, fence);
          if (runEpoch !== ownerExportEpoch || accountEpoch !== authEpoch) fail("The data export was canceled.");
          if (ownerId === null) {
            ownerId = page.owner_id; fence = page.fence; totalRows = page.total_rows;
            maximumBytes = page.max_download_bytes;
            if (ownerId !== initialSession.user_id || page.offset !== 0) fail("The signed-in account changed.");
          } else if (page.owner_id !== ownerId || page.fence !== fence || page.total_rows !== totalRows ||
              page.max_download_bytes !== maximumBytes || page.offset !== rows.length) {
            fail("The data export changed. Try again.");
          }
          for (const row of page.rows) {
            const identity = `${row.section}\u0000${row.key}`;
            if (seen.has(identity)) fail("The data export response was invalid.");
            const encodedBytes = encoder.encode(JSON.stringify(row)).length + 2;
            if (rowBytes + encodedBytes > maximumBytes) fail("The data export is too large.");
            rowBytes += encodedBytes; seen.add(identity); rows.push(row);
          }
          if (rows.length > totalRows || rows.length > maximumBytes) fail("The data export is too large.");
          cursor = page.next_cursor;
        } while (cursor !== null);
        if (rows.length !== totalRows) fail("The data export was incomplete.");
        const finalFence = await api.ownerExportPage(null, fence);
        const currentSession = await auth.sessionForRequest();
        if (runEpoch !== ownerExportEpoch || accountEpoch !== authEpoch || !currentSession ||
            currentSession.user_id !== ownerId || finalFence.owner_id !== ownerId || finalFence.fence !== fence ||
            finalFence.total_rows !== totalRows) fail("The data export changed. Try again.");
        const content = JSON.stringify({ schema_version: 1, kind: "news_curator_owner_export",
          owner_id: ownerId, fence, total_rows: totalRows, rows });
        if (encoder.encode(content).length > maximumBytes) fail("The data export is too large.");
        const url = URL.createObjectURL(new Blob([content], { type: "application/json" }));
        const link = document.createElement("a");
        link.href = url; link.download = "news-curator-my-data.json"; link.click(); URL.revokeObjectURL(url);
        announce("Your data download is ready.");
      } catch (_) {
        rows = [];
        if (runEpoch === ownerExportEpoch && accountEpoch === authEpoch) announce("Your data could not be downloaded. Try again.");
      } finally {
        rows = [];
        if (runEpoch === ownerExportEpoch) button.disabled = false;
      }
    }
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
      if (usesM2()) {
        loadButton.textContent = `Load ${m2Config.page_size} more`;
        loadButton.hidden = initializing || !m2Active || !m2Cursor;
        loadButton.disabled = [...pageRequests].some((request) => request.epoch === authEpoch);
        return;
      }
      if (latest) loadButton.textContent = `Load ${latest.page_size} more`;
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
    function stateReady(card) { return (card.dataset.m2Card === "true" && signedIn()) || (discoveryActive && Boolean(card.dataset.discoveryLane) && signedIn()) || hydratedTopics(card).has(selectedTopic()); }
    function refreshStateControls() {
      cards.forEach((card) => {
        const ready = stateReady(card);
        card.querySelectorAll(".state-action").forEach((button) => {
          if (button.classList.contains("read-action")) {
            button.disabled = false;
            return;
          }
          const stateWrite = button.classList.contains("read-action") || button.classList.contains("save-action");
          const interestWrite = button.classList.contains("interest-action") ||
            button.classList.contains("less-interest-action");
          button.disabled = !ready || (stateWrite && Boolean(card.newsCuratorStateMutationToken)) ||
            (interestWrite && Boolean(card.newsCuratorInterestMutation));
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
      delete card.newsCuratorInterestMutation;
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
      abortOwnerExport();
      authEpoch += 1;
      pendingStateMutations.clear(); pendingInterestMutations.clear();
      leaveM2();
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
      abortOwnerExport();
      authEpoch += 1;
      pendingStateMutations.clear(); pendingInterestMutations.clear();
      leaveM2();
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
        const pending = pendingStateMutations.get(card.dataset.storyId);
        if (pending?.token === card.newsCuratorStateMutationToken) pending.pendingRead = intent;
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
      if (usesM2()) { await loadM2(); return; }
      if (m2Active) leaveM2();
      if (discoveryActive) return;
      const topic = selectedTopic();
      if (!latest) {
        latest = await api.latestPublication();
        if (!latest) { announce("No published edition is available yet."); return; }
      }
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
      if (usesM2()) { if (!loadButton.disabled) await loadM2(true); return; }
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
      rememberM2State(card, { read_at: pending.read ? "local" : null });
    }
    async function mutateState(card, read, saved, previousRead = card.classList.contains("is-read"), eventType = "read_more") {
      const focusedAction = document.activeElement;
      const restoreFocusOnRollback = Boolean(focusedAction && card.contains(focusedAction));
      const storyId = card.dataset.storyId;
      if (pendingStateMutations.has(storyId)) return;
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
      pendingStateMutations.set(storyId, {
        token: mutationToken,
        baseline: card.newsCuratorStateMutationBaseline,
        presentation: card.newsCuratorStateMutationPresentation,
        pendingRead: null,
      });
      applyServerState(card, { ...previous, read_at: read ? "local" : null, saved_at: saved ? "local" : null });
      rememberM2State(card, { read_at: read ? "local" : null, saved_at: saved ? "local" : null });
      try {
        const key = idempotencyKey();
        const result = m2?.enabled && signedIn() && (eventType !== "read_more" || read)
          ? await enqueueBehavior(async (snapshot) => api.setStoryStateWithEvent(card.dataset.storyId, read, saved,
              previous.state_revision, key, { ...await behaviorIdentity(snapshot), p_event_type: eventType, p_surface: "reader" }))
          : await api.setStoryState(card.dataset.storyId, read, saved, previous.state_revision, key);
        if (requestEpoch !== authEpoch) return;
        if (result.status === "conflict") fail("Story state changed in another session.");
        const pending = pendingStateMutations.get(storyId);
        if (pending?.token !== mutationToken) return;
        const baseline = pending.baseline || previous;
        const confirmed = Number(result.state_revision) >= Number(baseline.state_revision)
          ? { ...baseline, ...result }
          : baseline;
        const entry = m2Entries.find((candidate) => candidate.story_id === storyId);
        if (entry) ["read_at", "saved_at", "state_revision"].forEach((field) => { entry[field] = confirmed[field]; });
        const currentCard = cards.get(storyId);
        if (currentCard) restorePendingMutations(currentCard);
        const presentationCard = card.isConnected && card.newsCuratorStateMutationToken === mutationToken
          ? card : currentCard?.newsCuratorStateMutationToken === mutationToken ? currentCard : null;
        if (!presentationCard) return;
        applyServerState(presentationCard, confirmed);
        rememberM2State(presentationCard, confirmed);
        reconcilePendingRead(presentationCard, Boolean(confirmed.read_at));
        reapplyCurrentMembership(presentationCard,
          presentationCard === card && restoreFocusOnRollback ? focusedAction : null);
        announce("Reading state saved.");
      } catch (_) {
        const pending = pendingStateMutations.get(storyId);
        if (requestEpoch !== authEpoch || pending?.token !== mutationToken) return;
        const baseline = pending.baseline || previous;
        const entry = m2Entries.find((candidate) => candidate.story_id === storyId);
        if (entry) ["read_at", "saved_at", "state_revision"].forEach((field) => { entry[field] = baseline[field]; });
        const currentCard = cards.get(storyId);
        if (currentCard) restorePendingMutations(currentCard);
        const presentationCard = card.isConnected && card.newsCuratorStateMutationToken === mutationToken
          ? card : currentCard?.newsCuratorStateMutationToken === mutationToken ? currentCard : null;
        if (!presentationCard) return;
        applyServerState(presentationCard, baseline);
        reconcilePendingRead(presentationCard, Boolean(baseline.read_at));
        reapplyCurrentMembership(presentationCard);
        rolledBack = true;
        announce("Reading state could not be saved. Try again.");
      } finally {
        const pending = pendingStateMutations.get(storyId);
        if (pending?.token === mutationToken) pendingStateMutations.delete(storyId);
        const currentCard = cards.get(storyId);
        const presentationCard = card.isConnected && card.newsCuratorStateMutationToken === mutationToken
          ? card : currentCard?.newsCuratorStateMutationToken === mutationToken ? currentCard : null;
        if (presentationCard?.newsCuratorStateMutationPresentation?.token === mutationToken) {
          delete presentationCard.newsCuratorStateMutationPresentation;
        }
        if (presentationCard?.newsCuratorStateMutationBaseline?.token === mutationToken) {
          delete presentationCard.newsCuratorStateMutationBaseline;
        }
        const unlocked = Boolean(presentationCard &&
          finishStateMutation(presentationCard, mutationToken, stateReady(presentationCard)));
        if (unlocked && presentationCard.newsCuratorPendingReadIntent) {
          syncReadIntent(presentationCard, presentationCard.newsCuratorPendingReadIntent);
        }
        if (unlocked && rolledBack && restoreFocusOnRollback && focusedAction.isConnected && !presentationCard.hidden) {
          focusedAction.focus({ preventScroll: true });
        }
      }
    }
    document.getElementById("sections").addEventListener("click", (event) => {
      const target = event.target.closest && event.target.closest("button");
      const card = event.target.closest && event.target.closest(".card[data-story-id]");
      if (!card) return;
      const query = m2Active && card.dataset.m2Card === "true" ? (searchBox?.value.trim() || "") : "";
      const original = event.target.closest?.(".acts a");
      if (original && card.dataset.m2Card === "true") {
        void recordBehavior("open_original", { story_id: card.dataset.storyId }).catch(() => announce("Original opened. Learning could not be saved."));
      }
      if (query && (original || target?.classList.contains("accordion-toggle"))) {
        void recordBehavior("search_result_click", { query, story_id: card.dataset.storyId,
          result_position: Number(card.dataset.m2Position) }).catch(() => announce("Search learning could not be saved."));
      }
      if (!target) return;
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
        void mutateState(card, card.classList.contains("is-read"), !card.classList.contains("is-saved"), card.classList.contains("is-read"), "save");
      } else if ((target.classList.contains("interest-action") || target.classList.contains("less-interest-action")) && requireSignIn()) {
        const topicIds = (card.dataset.topicApiIds || "").split(/\s+/).filter(Boolean);
        const topic = selectedTopic();
        const topicId = actionTopic(
          topicIds,
          topic,
          topicIdForSlug(topic),
          target.dataset.fallbackTopicId || target.dataset.topicId,
        );
        applyInterestTopic(card, topicId);
        const signal = target.classList.contains("less-interest-action") ? "less_like" : "more_like";
        if (signal === "more_like" && card.classList.contains("is-more-like")) return;
        const revision = Number(card.dataset.interestRevision || 0);
        const storyId = card.dataset.storyId;
        if (pendingInterestMutations.has(storyId)) return;
        const requestEpoch = authEpoch;
        const interestToken = {};
        card.newsCuratorInterestMutation = { token: interestToken };
        pendingInterestMutations.set(storyId, card.newsCuratorInterestMutation);
        refreshStateControls();
        const key = idempotencyKey();
        const operation = m2?.enabled
          ? enqueueBehavior(async (snapshot) => api.setStoryInterestWithEvent(card.dataset.storyId, topicId,
              signal, revision, key, { ...await behaviorIdentity(snapshot), p_surface: "reader" }))
          : api.setStoryInterest(card.dataset.storyId, topicId, revision, key);
        operation
          .then((result) => {
            if (requestEpoch !== authEpoch) return;
            if (result.status === "conflict") fail("Story interest changed in another session.");
            const presentationCard = card.isConnected ? card : cards.get(storyId);
            if (presentationCard) restorePendingMutations(presentationCard);
            if (!presentationCard || presentationCard.newsCuratorInterestMutation?.token !== interestToken) return;
            applyServerState(presentationCard, result, topicId);
            rememberM2State(presentationCard, { interests: [...interestStates(presentationCard)].map(([savedTopicId, state]) => ({
              topic_id: savedTopicId, signal: state.signal, revision: state.revision,
            })) });
            announce(`${signal === "less_like" ? "Less" : "More"} like this was saved for future rankings.`);
          })
          .catch(() => {
            if (requestEpoch === authEpoch) announce("More like this could not be saved. Try again.");
          })
          .finally(() => {
            const pending = pendingInterestMutations.get(storyId);
            if (pending?.token === interestToken) pendingInterestMutations.delete(storyId);
            const presentationCard = card.isConnected ? card : cards.get(storyId);
            if (presentationCard?.newsCuratorInterestMutation?.token === interestToken) {
              delete presentationCard.newsCuratorInterestMutation;
              refreshStateControls();
            }
          });
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
      if (!usesM2()) void fetchDiscovery(true);
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
            if (!usesM2()) void fetchDiscovery(true);
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
      latest = usesM2() ? await api.latestPublication().catch(() => null) : await api.latestPublication();
      if (!usesM2()) void fetchDiscovery(true);
      if (usesM2()) {
        loadButton.textContent = `Load ${m2Config.page_size} more`;
        await loadM2();
        return;
      }
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
