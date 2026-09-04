(() => {
  "use strict";

  const CALLBACK_PATH = "/auth/callback/";
  const STATE_KEY = "news-curator.auth.state";
  const VERIFIER_KEY = "news-curator.auth.verifier";
  const SESSION_KEY = "news-curator.auth.session";
  const SESSION_FIELDS = ["access_token", "expires_at", "refresh_token", "user_id"];
  const MAX_TOKEN_CHARS = 16384;
  const MAX_RESPONSE_BYTES = 64 * 1024;
  const encoder = new TextEncoder();

  function fail(message) {
    throw new Error(message);
  }

  function isObject(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  function boundedString(value, maxChars) {
    return typeof value === "string" && value.length > 0 && value.length <= maxChars;
  }

  function exactFields(value, fields) {
    if (!isObject(value)) return false;
    const actual = Object.keys(value).sort();
    const expected = [...fields].sort();
    return actual.length === expected.length && actual.every((name, index) => name === expected[index]);
  }

  function isPublishableKey(value) {
    if (!boundedString(value, 8192)) return false;
    if (value.startsWith("sb_publishable_")) return value.length > "sb_publishable_".length;
    if (value.startsWith("sb_")) return false;
    const payload = decodePayload(value);
    return Boolean(payload) && payload.role === "anon";
  }

  function validateAuthConfig(value) {
    let parsed;
    try {
      if (!exactFields(value, ["key", "url"])) fail("Public auth configuration is missing or invalid.");
      parsed = new URL(value.url);
    } catch (_) {
      fail("Public auth configuration is missing or invalid.");
    }
    if (
      parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.origin !== value.url ||
      parsed.pathname !== "/" || parsed.search || parsed.hash || !isPublishableKey(value.key)
    ) {
      fail("Public auth configuration is missing or invalid.");
    }
    return { url: parsed.origin, key: value.key };
  }

  function config() {
    const configuredUrl = document.querySelector('meta[name="supabase-url"]')?.content;
    const key = document.querySelector('meta[name="supabase-publishable-key"]')?.content;
    let parsed;
    try {
      parsed = new URL(configuredUrl);
    } catch (_) {
      fail("Public auth configuration is missing or invalid.");
    }
    if (
      parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.pathname !== "/" ||
      parsed.search || parsed.hash || !isPublishableKey(key)
    ) {
      fail("Public auth configuration is missing or invalid.");
    }
    return validateAuthConfig({ url: parsed.origin, key });
  }

  function randomValue(bytes = 32) {
    const value = new Uint8Array(bytes);
    crypto.getRandomValues(value);
    return base64url(value);
  }

  function base64url(bytes) {
    let text = "";
    bytes.forEach((byte) => { text += String.fromCharCode(byte); });
    return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function challenge(verifier) {
    return base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(verifier))));
  }

  function callbackUrl(state) {
    const url = new URL(CALLBACK_PATH, window.location.origin);
    url.searchParams.set("client_state", state);
    return url.toString();
  }

  async function beginSignIn() {
    const { url } = config();
    const state = randomValue();
    const verifier = randomValue(48);
    sessionStorage.setItem(STATE_KEY, state);
    sessionStorage.setItem(VERIFIER_KEY, verifier);
    const authorize = new URL(`${url}/auth/v1/authorize`);
    authorize.searchParams.set("provider", "google");
    authorize.searchParams.set("redirect_to", callbackUrl(state));
    authorize.searchParams.set("code_challenge", await challenge(verifier));
    authorize.searchParams.set("code_challenge_method", "S256");
    window.location.assign(authorize.toString());
  }

  function decodePayload(jwt) {
    if (!boundedString(jwt, MAX_TOKEN_CHARS)) return null;
    const parts = jwt.split(".");
    if (parts.length !== 3 || !parts[1] || parts[1].length > 12000) return null;
    try {
      const padded = parts[1].replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(parts[1].length / 4) * 4, "=");
      const payload = JSON.parse(atob(padded));
      return isObject(payload) ? payload : null;
    } catch (_) {
      return null;
    }
  }

  function projectSession(raw, nowSeconds = Date.now() / 1000) {
    if (
      !isObject(raw) ||
      !boundedString(raw.access_token, MAX_TOKEN_CHARS) ||
      !boundedString(raw.refresh_token, MAX_TOKEN_CHARS) ||
      !isObject(raw.user) ||
      !boundedString(raw.user.id, 256)
    ) {
      fail("The authentication response was invalid.");
    }
    const identity = decodePayload(raw.access_token);
    if (!identity || !boundedString(identity.sub, 256) || identity.sub !== raw.user.id) {
      fail("The authentication response was invalid.");
    }
    let expiresAt;
    if (Number.isSafeInteger(raw.expires_at) && raw.expires_at > 0 && raw.expires_at <= nowSeconds + 86400) {
      expiresAt = Math.floor(raw.expires_at);
    } else if (Number.isFinite(raw.expires_in) && raw.expires_in > 0 && raw.expires_in <= 86400) {
      expiresAt = Math.floor(nowSeconds + raw.expires_in);
    } else {
      fail("The authentication response was invalid.");
    }
    return {
      access_token: raw.access_token,
      refresh_token: raw.refresh_token,
      expires_at: expiresAt,
      user_id: raw.user.id,
    };
  }

  function validateSessionShape(value) {
    if (
      !exactFields(value, SESSION_FIELDS) ||
      !boundedString(value.access_token, MAX_TOKEN_CHARS) ||
      !boundedString(value.refresh_token, MAX_TOKEN_CHARS) ||
      !boundedString(value.user_id, 256) ||
      !Number.isInteger(value.expires_at) || value.expires_at <= 0
    ) {
      fail("The saved session was invalid.");
    }
    return value;
  }

  function validateStoredSession(value, nowSeconds = Date.now() / 1000) {
    const session = validateSessionShape(value);
    if (session.expires_at <= nowSeconds) fail("The saved session was invalid.");
    return session;
  }

  function projectRefreshedSession(raw, currentSession, nowSeconds = Date.now() / 1000) {
    const previous = validateSessionShape(currentSession);
    if (
      !isObject(raw) ||
      !boundedString(raw.access_token, MAX_TOKEN_CHARS) ||
      !boundedString(raw.refresh_token, MAX_TOKEN_CHARS) ||
      raw.refresh_token === previous.refresh_token ||
      !isObject(raw.user) ||
      !boundedString(raw.user.id, 256) ||
      raw.user.id !== previous.user_id
    ) {
      fail("The refreshed session was invalid.");
    }
    let expiresAt;
    if (Number.isSafeInteger(raw.expires_at) && raw.expires_at > nowSeconds && raw.expires_at <= nowSeconds + 86400) {
      expiresAt = Math.floor(raw.expires_at);
    } else if (Number.isFinite(raw.expires_in) && raw.expires_in > 0 && raw.expires_in <= 86400) {
      expiresAt = Math.floor(nowSeconds + raw.expires_in);
    } else {
      fail("The refreshed session was invalid.");
    }
    return {
      access_token: raw.access_token,
      refresh_token: raw.refresh_token,
      expires_at: expiresAt,
      user_id: previous.user_id,
    };
  }

  function loadSessionCandidate() {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw || raw.length > MAX_TOKEN_CHARS * 3) fail("No saved session exists.");
    try {
      return validateSessionShape(JSON.parse(raw));
    } catch (_) {
      sessionStorage.removeItem(SESSION_KEY);
      fail("The saved session was invalid.");
    }
  }

  function loadSession(nowSeconds = Date.now() / 1000) {
    try {
      return validateStoredSession(loadSessionCandidate(), nowSeconds);
    } catch (_) {
      sessionStorage.removeItem(SESSION_KEY);
      fail("The saved session was invalid.");
    }
  }

  function boundedText(value, maxChars, maxBytes) {
    return typeof value === "string" && value === value.trim() && value.length >= 1 &&
      value.length <= maxChars && encoder.encode(value).length <= maxBytes;
  }

  function validatePreferenceInput(value) {
    if (!exactFields(value, ["expected_revision", "interests", "locale", "saved_searches"])) {
      fail("The preference input was invalid.");
    }
    if (!Number.isInteger(value.expected_revision) || value.expected_revision < 0 || !["en", "zh"].includes(value.locale)) {
      fail("The preference input was invalid.");
    }
    if (!Array.isArray(value.interests) || value.interests.length > 20 ||
        !value.interests.every((item) => boundedText(item, 80, 160))) {
      fail("The preference input was invalid.");
    }
    if (!Array.isArray(value.saved_searches) || value.saved_searches.length > 20) {
      fail("The preference input was invalid.");
    }
    const seen = new Set();
    const searches = value.saved_searches.map((item) => {
      if (!exactFields(item, ["enabled", "id", "query"]) ||
          !boundedText(item.id, 64, 128) || !boundedText(item.query, 300, 600) ||
          typeof item.enabled !== "boolean" || seen.has(item.id)) {
        fail("The preference input was invalid.");
      }
      seen.add(item.id);
      return { id: item.id, query: item.query, enabled: item.enabled };
    });
    if (encoder.encode(JSON.stringify(searches)).length > 8192) fail("The preference input was invalid.");
    return {
      expected_revision: value.expected_revision,
      locale: value.locale,
      interests: [...value.interests],
      saved_searches: searches,
    };
  }

  function validatePreferenceRecord(value, expectedUserId) {
    const allowed = ["created_at", "interests", "locale", "revision", "saved_searches", "updated_at", "user_id"];
    if (!isObject(value) || Object.keys(value).some((field) => !allowed.includes(field)) ||
        !["interests", "locale", "revision", "saved_searches", "user_id"].every((field) => field in value) ||
        value.user_id !== expectedUserId || !boundedString(value.user_id, 256)) {
      fail("The preference response was invalid.");
    }
    const checked = validatePreferenceInput({
      expected_revision: value.revision,
      locale: value.locale,
      interests: value.interests,
      saved_searches: value.saved_searches,
    });
    if ((value.created_at != null && !boundedString(value.created_at, 128)) ||
        (value.updated_at != null && !boundedString(value.updated_at, 128))) {
      fail("The preference response was invalid.");
    }
    return {
      user_id: value.user_id,
      revision: checked.expected_revision,
      locale: checked.locale,
      interests: checked.interests,
      saved_searches: checked.saved_searches,
      created_at: value.created_at ?? null,
      updated_at: value.updated_at ?? null,
    };
  }

  async function boundedJson(response, invalidMessage) {
    const text = await response.text();
    if (encoder.encode(text).length > MAX_RESPONSE_BYTES) fail(invalidMessage);
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (_) {
      fail(invalidMessage);
    }
  }

  async function responseJson(response) {
    return boundedJson(response, "The preference response was invalid.");
  }

  function requireExactResponse(response, requestedUrl, message) {
    if (response.redirected !== false || response.url !== requestedUrl) fail(message);
  }

  function validateEmail(value) {
    if (!boundedString(value, 254) || value !== value.trim() || /\s/.test(value)) {
      fail("Enter a valid email address.");
    }
    const parts = value.split("@");
    if (parts.length !== 2 || !parts[0] || !parts[1] || !parts[1].includes(".")) {
      fail("Enter a valid email address.");
    }
    return value;
  }

  function validateEmailCode(value) {
    if (!boundedString(value, 128) || value !== value.trim() || /\s/.test(value)) {
      fail("Enter the code from your email.");
    }
    return value;
  }

  async function requestEmailCode(email, fetchImpl = fetch) {
    const { url, key } = config();
    const otpUrl = `${url}/auth/v1/otp`;
    const response = await fetchImpl(otpUrl, {
      method: "POST",
      headers: { apikey: key, "content-type": "application/json" },
      body: JSON.stringify({ email: validateEmail(email), create_user: false, data: {} }),
      credentials: "omit",
      referrerPolicy: "no-referrer",
      redirect: "error",
    });
    requireExactResponse(response, otpUrl, "The authentication endpoint redirected unexpectedly.");
    const payload = await boundedJson(response, "The authentication response was invalid.");
    if (!response.ok) fail("Email code could not be sent.");
    const empty = exactFields(payload, []);
    const confirmation = exactFields(payload, ["message"]) && boundedString(payload.message, 256);
    if (!empty && !confirmation) fail("The authentication response was invalid.");
  }

  async function verifyEmailCode(email, token, fetchImpl = fetch) {
    const { url, key } = config();
    const verifyUrl = `${url}/auth/v1/verify`;
    const response = await fetchImpl(verifyUrl, {
      method: "POST",
      headers: { apikey: key, "content-type": "application/json" },
      body: JSON.stringify({ email: validateEmail(email), token: validateEmailCode(token), type: "email" }),
      credentials: "omit",
      referrerPolicy: "no-referrer",
      redirect: "error",
    });
    requireExactResponse(response, verifyUrl, "The authentication endpoint redirected unexpectedly.");
    if (!response.ok) fail("Sign in failed.");
    const rawSession = await boundedJson(response, "The authentication response was invalid.");
    const safeSession = projectSession(rawSession);
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(safeSession));
    return safeSession;
  }

  async function refreshSession(authConfig, currentSession, fetchImpl = fetch, nowSeconds = Date.now() / 1000) {
    try {
      const checkedConfig = validateAuthConfig(authConfig);
      const previous = validateSessionShape(currentSession);
      const tokenUrl = `${checkedConfig.url}/auth/v1/token?grant_type=refresh_token`;
      const response = await fetchImpl(tokenUrl, {
        method: "POST",
        headers: { apikey: checkedConfig.key, "content-type": "application/json" },
        body: JSON.stringify({ refresh_token: previous.refresh_token }),
        credentials: "omit",
        referrerPolicy: "no-referrer",
        redirect: "error",
      });
      requireExactResponse(response, tokenUrl, "The authentication endpoint redirected unexpectedly.");
      if (!response.ok) fail("Session refresh failed.");
      const rawSession = await boundedJson(response, "The refreshed session was invalid.");
      const safeSession = projectRefreshedSession(rawSession, previous, nowSeconds);
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(safeSession));
      return safeSession;
    } catch (_) {
      sessionStorage.removeItem(SESSION_KEY);
      fail("Session refresh failed.");
    }
  }

  async function sessionForRequest(authConfig, currentSession, fetchImpl, nowSeconds = Date.now() / 1000) {
    let session;
    try {
      session = validateSessionShape(currentSession);
    } catch (_) {
      sessionStorage.removeItem(SESSION_KEY);
      fail("The saved session was invalid.");
    }
    return session.expires_at > nowSeconds
      ? session
      : refreshSession(authConfig, session, fetchImpl, nowSeconds);
  }

  function preferenceHeaders(authConfig, session, representation = false) {
    const headers = {
      apikey: authConfig.key,
      authorization: `Bearer ${session.access_token}`,
      accept: "application/json",
      "content-type": "application/json",
    };
    if (representation) headers.prefer = "return=representation";
    return headers;
  }

  async function getPreferences(authConfig, currentSession, fetchImpl = fetch) {
    const checkedConfig = validateAuthConfig(authConfig);
    const session = await sessionForRequest(checkedConfig, currentSession, fetchImpl);
    const query = new URLSearchParams({
      select: "user_id,revision,locale,interests,saved_searches,created_at,updated_at",
      limit: "2",
    });
    const requestedUrl = `${checkedConfig.url}/rest/v1/user_preferences?${query}`;
    const response = await fetchImpl(requestedUrl, {
      method: "GET",
      headers: preferenceHeaders(checkedConfig, session),
      credentials: "omit",
      referrerPolicy: "no-referrer",
      redirect: "error",
    });
    requireExactResponse(response, requestedUrl, "The preference endpoint redirected unexpectedly.");
    const payload = await responseJson(response);
    if (!response.ok || !Array.isArray(payload) || payload.length > 1) fail("Preferences could not be read.");
    return payload.length === 0 ? null : validatePreferenceRecord(payload[0], session.user_id);
  }

  async function setPreferences(authConfig, currentSession, rawUpdate, fetchImpl = fetch) {
    const update = validatePreferenceInput(rawUpdate);
    const checkedConfig = validateAuthConfig(authConfig);
    const session = await sessionForRequest(checkedConfig, currentSession, fetchImpl);
    const rpcUrl = `${checkedConfig.url}/rest/v1/rpc/compare_and_swap_user_preferences`;
    const rpc = await fetchImpl(rpcUrl, {
      method: "POST",
      headers: preferenceHeaders(checkedConfig, session),
      body: JSON.stringify({
        expected_revision: update.expected_revision,
        new_locale: update.locale,
        new_interests: update.interests,
        new_saved_searches: update.saved_searches,
      }),
      credentials: "omit",
      referrerPolicy: "no-referrer",
      redirect: "error",
    });
    requireExactResponse(rpc, rpcUrl, "The preference endpoint redirected unexpectedly.");
    const outcome = await responseJson(rpc);
    if (!rpc.ok || !isObject(outcome) || !["updated", "conflict", "not_found"].includes(outcome.status)) {
      fail("Preferences could not be updated.");
    }
    if (outcome.status === "updated") {
      if (!Number.isInteger(outcome.revision) || outcome.revision < 1 ||
          !boundedString(outcome.updated_at, 128)) {
        fail("The preference response was invalid.");
      }
      return {
        status: "updated",
        preference: {
          user_id: session.user_id,
          revision: outcome.revision,
          locale: update.locale,
          interests: update.interests,
          saved_searches: update.saved_searches,
          created_at: null,
          updated_at: outcome.updated_at,
        },
      };
    }
    if (outcome.status === "conflict") {
      if (!Number.isInteger(outcome.revision) || outcome.revision < 0) fail("The preference response was invalid.");
      return { status: "conflict", revision: outcome.revision };
    }
    if (update.expected_revision !== 0) return { status: "not_found" };

    const createUrl = `${checkedConfig.url}/rest/v1/user_preferences`;
    const created = await fetchImpl(createUrl, {
      method: "POST",
      headers: preferenceHeaders(checkedConfig, session, true),
      body: JSON.stringify({
        user_id: session.user_id,
        locale: update.locale,
        interests: update.interests,
        saved_searches: update.saved_searches,
      }),
      credentials: "omit",
      referrerPolicy: "no-referrer",
      redirect: "error",
    });
    requireExactResponse(created, createUrl, "The preference endpoint redirected unexpectedly.");
    const inserted = await responseJson(created);
    if (created.status === 409) {
      const current = await getPreferences(checkedConfig, session, fetchImpl);
      return { status: "conflict", revision: current ? current.revision : null };
    }
    if (!created.ok || !Array.isArray(inserted) || inserted.length !== 1) fail("Preferences could not be created.");
    return { status: "created", preference: validatePreferenceRecord(inserted[0], session.user_id) };
  }

  async function finishCallback(callback, fetchImpl = fetch) {
    const code = callback.searchParams.get("code");
    const returnedState = callback.searchParams.get("client_state");
    const providerError = callback.searchParams.get("error");
    const expectedState = sessionStorage.getItem(STATE_KEY);
    const verifier = sessionStorage.getItem(VERIFIER_KEY);
    sessionStorage.removeItem(STATE_KEY);
    sessionStorage.removeItem(VERIFIER_KEY);
    history.replaceState(null, "", CALLBACK_PATH);
    if (providerError) fail("Sign in failed.");
    if (!code) return false;
    if (!expectedState || returnedState !== expectedState || !verifier) {
      fail("The sign-in response could not be verified.");
    }

    const { url, key } = config();
    const tokenUrl = `${url}/auth/v1/token?grant_type=pkce`;
    const response = await fetchImpl(tokenUrl, {
      method: "POST",
      headers: { apikey: key, "content-type": "application/json" },
      body: JSON.stringify({ auth_code: code, code_verifier: verifier }),
      credentials: "omit",
      referrerPolicy: "no-referrer",
      redirect: "error",
    });
    requireExactResponse(response, tokenUrl, "The authentication endpoint redirected unexpectedly.");
    if (!response.ok) fail("Sign in failed.");
    const rawSession = await boundedJson(response, "The authentication response was invalid.");
    const safeSession = projectSession(rawSession);
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(safeSession));
    return true;
  }

  async function signOut(fetchImpl = fetch) {
    let session;
    try {
      session = loadSessionCandidate();
    } finally {
      sessionStorage.removeItem(SESSION_KEY);
    }
    const { url, key } = config();
    const logoutUrl = `${url}/auth/v1/logout`;
    const response = await fetchImpl(logoutUrl, {
      method: "POST",
      headers: { apikey: key, authorization: `Bearer ${session.access_token}` },
      credentials: "omit",
      referrerPolicy: "no-referrer",
      redirect: "error",
    });
    requireExactResponse(response, logoutUrl, "The authentication endpoint redirected unexpectedly.");
  }

  const contract = {
    beginSignIn,
    config,
    finishCallback,
    getPreferences,
    isPublishableKey,
    loadSession,
    projectSession,
    projectRefreshedSession,
    refreshSession,
    requestEmailCode,
    setPreferences,
    signOut,
    validateEmail,
    validatePreferenceInput,
    validatePreferenceRecord,
    validateStoredSession,
    verifyEmailCode,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = contract;
    return;
  }

  window.NewsCuratorPersonalization = Object.freeze({
    get: () => getPreferences(config(), loadSessionCandidate()),
    set: (input) => setPreferences(config(), loadSessionCandidate(), input),
  });

  async function run() {
    const status = document.getElementById("status");
    const loginPanel = document.getElementById("login-panel");
    const codePanel = document.getElementById("code-panel");
    const preferencesPanel = document.getElementById("preferences-panel");
    const email = document.getElementById("email");
    const code = document.getElementById("code");
    const interests = document.getElementById("interests");
    const interestCount = document.getElementById("interest-count");
    const buttons = [...document.querySelectorAll("button")];
    let currentSession = null;
    let currentPreference = null;

    function announce(message) {
      status.textContent = message;
    }

    function setBusy(busy) {
      buttons.forEach((button) => { button.disabled = busy; });
    }

    function parseInterests() {
      const values = interests.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
      if (values.length > 20) fail("Keep the list to 20 interests or fewer.");
      return values;
    }

    function updateCount() {
      const count = interests.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean).length;
      interestCount.textContent = `${count} of 20 interests`;
    }

    function showSignedOut() {
      currentSession = null;
      currentPreference = null;
      loginPanel.hidden = false;
      codePanel.hidden = true;
      preferencesPanel.hidden = true;
      code.value = "";
    }

    function showPreferences(preference) {
      currentPreference = preference || { revision: 0, locale: "en", interests: [], saved_searches: [] };
      interests.value = currentPreference.interests.join("\n");
      updateCount();
      loginPanel.hidden = true;
      codePanel.hidden = true;
      preferencesPanel.hidden = false;
    }

    async function loadPreferences() {
      const preference = await getPreferences(config(), currentSession);
      currentSession = loadSessionCandidate();
      showPreferences(preference);
    }

    interests.addEventListener("input", updateCount);
    document.getElementById("send-code").addEventListener("click", async () => {
      setBusy(true);
      try {
        const address = email.value.trim();
        await requestEmailCode(address);
        email.value = address;
        codePanel.hidden = false;
        code.focus();
        announce("Check your email for the sign-in code.");
      } catch (_) {
        announce("We could not send a code. Check the email address and try again.");
      } finally {
        setBusy(false);
      }
    });
    document.getElementById("verify-code").addEventListener("click", async () => {
      setBusy(true);
      try {
        currentSession = await verifyEmailCode(email.value.trim(), code.value.trim());
      } catch (_) {
        announce("That code did not work. Request a new code and try again.");
        setBusy(false);
        return;
      }
      try {
        await loadPreferences();
        announce("Signed in. Your interests are ready.");
      } catch (_) {
        announce("Signed in, but your interests could not be loaded. Refresh the page to try again.");
      } finally {
        setBusy(false);
      }
    });
    document.getElementById("reload-interests").addEventListener("click", async () => {
      setBusy(true);
      try {
        await loadPreferences();
        announce("Your latest interests are loaded.");
      } catch (_) {
        announce("Your interests could not be loaded. Try again.");
      } finally {
        setBusy(false);
      }
    });
    document.getElementById("save-interests").addEventListener("click", async () => {
      setBusy(true);
      try {
        const outcome = await setPreferences(config(), currentSession, {
          expected_revision: currentPreference.revision,
          locale: currentPreference.locale,
          interests: parseInterests(),
          saved_searches: currentPreference.saved_searches,
        });
        currentSession = loadSessionCandidate();
        if (outcome.status === "conflict") {
          announce("Your interests changed in another session. Reload them before saving again.");
        } else if (outcome.status === "not_found") {
          announce("Your saved interests changed. Reload them before saving again.");
        } else {
          showPreferences(outcome.preference);
          announce("Your interests were saved.");
        }
      } catch (error) {
        announce(error && /20 interests/.test(error.message)
          ? error.message
          : "Your interests could not be saved. Try again.");
      } finally {
        setBusy(false);
      }
    });
    document.getElementById("sign-out").addEventListener("click", async () => {
      setBusy(true);
      try {
        await signOut();
        announce("Signed out.");
      } catch (_) {
        announce("Signed out on this device.");
      } finally {
        showSignedOut();
        setBusy(false);
      }
    });

    try {
      currentSession = loadSessionCandidate();
      await loadPreferences();
      announce("Your interests are ready.");
    } catch (_) {
      showSignedOut();
      announce("Sign in to personalize your feed.");
    }
  }

  run();
})();
