"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const client = require(path.join(__dirname, "..", "static", "auth", "client.js"));

function jwt(payload) {
  return `x.${Buffer.from(JSON.stringify(payload)).toString("base64url")}.x`;
}

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

function rawSession(overrides = {}) {
  return {
    access_token: jwt({ sub: "user-a" }),
    refresh_token: "refresh-token",
    expires_in: 3600,
    user: { id: "user-a" },
    provider_token: "must-not-persist",
    provider_refresh_token: "must-not-persist",
    unexpected: { raw: true },
    ...overrides,
  };
}

function refreshedSession(overrides = {}) {
  return {
    access_token: jwt({ sub: "user-a", jti: "rotated" }),
    refresh_token: "rotated-refresh-token",
    expires_in: 3600,
    user: { id: "user-a" },
    provider_token: "must-not-persist",
    unexpected: { raw: true },
    ...overrides,
  };
}

function preference(revision = 0) {
  return {
    user_id: "user-a",
    revision,
    locale: "en",
    interests: ["agents"],
    saved_searches: [{ id: "daily", query: "agent news", enabled: true }],
    created_at: "2026-08-29T12:00:00Z",
    updated_at: "2026-08-29T12:00:00Z",
  };
}

function installBrowserMocks() {
  const storage = new Map();
  const metas = {
    'meta[name="supabase-url"]': "https://example.supabase.co",
    'meta[name="supabase-publishable-key"]': "sb_publishable_test",
  };
  global.sessionStorage = {
    getItem: (key) => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  };
  global.document = { querySelector: (selector) => ({ content: metas[selector] }) };
  const assigned = [];
  global.window = {
    location: {
      origin: "https://news.example",
      href: "https://news.example/auth/callback/",
      assign: (url) => assigned.push(url),
    },
  };
  const historyCalls = [];
  global.history = { replaceState: (...args) => historyCalls.push(args) };
  return { assigned, historyCalls, metas, storage };
}

function assertFailClosedFetch(call) {
  assert.equal(call.options.credentials, "omit");
  assert.equal(call.options.referrerPolicy, "no-referrer");
  assert.equal(call.options.redirect, "error");
}

async function main() {
  const browser = installBrowserMocks();
  const now = Math.floor(Date.now() / 1000);

  assert.equal(client.isPublishableKey("sb_publishable_test"), true);
  assert.equal(client.isPublishableKey(jwt({ role: "anon" })), true);
  assert.equal(client.isPublishableKey(jwt({ role: "service_role" })), false);
  assert.equal(client.isPublishableKey("sb_secret_test"), false);
  assert.equal(client.isPublishableKey("sb_other_test"), false);
  browser.metas['meta[name="supabase-publishable-key"]'] = jwt({ role: "service_role" });
  assert.throws(() => client.config(), /configuration/);
  browser.metas['meta[name="supabase-publishable-key"]'] = "sb_publishable_test";

  const projected = client.projectSession(rawSession(), now);
  assert.deepEqual(Object.keys(projected).sort(), ["access_token", "expires_at", "refresh_token", "user_id"]);
  assert.equal(projected.expires_at, now + 3600);
  assert.equal(JSON.stringify(projected).includes("must-not-persist"), false);
  assert.throws(() => client.projectSession(rawSession({ access_token: "not-a-jwt" }), now), /authentication response/);
  assert.throws(() => client.projectSession(rawSession({ access_token: jwt({ sub: "user-b" }) }), now), /authentication response/);
  assert.throws(() => client.projectSession(rawSession({ access_token: "x".repeat(16385) }), now), /authentication response/);
  assert.throws(() => client.validateStoredSession({ ...projected, provider_token: "x" }, now), /saved session/);
  const refreshedProjection = client.projectRefreshedSession(refreshedSession(), projected, now);
  assert.deepEqual(Object.keys(refreshedProjection).sort(), ["access_token", "expires_at", "refresh_token", "user_id"]);
  assert.equal(refreshedProjection.refresh_token, "rotated-refresh-token");
  assert.throws(
    () => client.projectRefreshedSession(refreshedSession({ user: { id: "user-b" } }), projected, now),
    /refreshed session/
  );
  assert.throws(
    () => client.projectRefreshedSession(refreshedSession({ refresh_token: projected.refresh_token }), projected, now),
    /refreshed session/
  );

  await client.beginSignIn();
  assert.equal(browser.assigned.length, 1);
  const authorize = new URL(browser.assigned[0]);
  const state = browser.storage.get("news-curator.auth.state");
  const verifier = browser.storage.get("news-curator.auth.verifier");
  assert.equal(authorize.origin, "https://example.supabase.co");
  assert.equal(authorize.pathname, "/auth/v1/authorize");
  assert.equal(authorize.searchParams.get("redirect_to"), `https://news.example/auth/callback/?client_state=${state}`);
  assert.equal(authorize.searchParams.get("code_challenge_method"), "S256");

  const lifecycleCalls = [];
  const lifecycleFetch = async (url, options) => {
    lifecycleCalls.push({ url, options });
    return response(200, rawSession(), url);
  };
  const callback = new URL(`https://news.example/auth/callback/?code=auth-code&client_state=${state}`);
  assert.equal(await client.finishCallback(callback, lifecycleFetch), true);
  assert.deepEqual(browser.historyCalls[0], [null, "", "/auth/callback/"]);
  assert.equal(browser.storage.has("news-curator.auth.state"), false);
  assert.equal(browser.storage.has("news-curator.auth.verifier"), false);

  await client.beginSignIn();
  assert.equal(
    await client.finishCallback(new URL("https://news.example/auth/callback/?client_state=missing-code"), async () => {
      throw new Error("fetch must not run");
    }),
    false
  );
  assert.equal(browser.storage.has("news-curator.auth.state"), false);
  assert.equal(browser.storage.has("news-curator.auth.verifier"), false);

  await client.beginSignIn();
  await assert.rejects(
    client.finishCallback(new URL("https://news.example/auth/callback/?error=access_denied&error_description=secret"), async () => {
      throw new Error("fetch must not run");
    }),
    /^Error: Sign in failed\.$/
  );
  assert.equal(browser.storage.has("news-curator.auth.state"), false);
  assert.equal(browser.storage.has("news-curator.auth.verifier"), false);

  await client.beginSignIn();
  const failedExchangeState = browser.storage.get("news-curator.auth.state");
  await assert.rejects(
    client.finishCallback(
      new URL(`https://news.example/auth/callback/?code=x&client_state=${failedExchangeState}`),
      async (url) => response(400, { error: "invalid_grant" }, url)
    ),
    /Sign in failed/
  );
  assert.equal(browser.storage.has("news-curator.auth.state"), false);
  assert.equal(browser.storage.has("news-curator.auth.verifier"), false);
  assert.equal(client.loadSession(now).user_id, "user-a");
  assert.equal(lifecycleCalls[0].options.body, JSON.stringify({ auth_code: "auth-code", code_verifier: verifier }));
  assertFailClosedFetch(lifecycleCalls[0]);

  await client.beginSignIn();
  const badState = browser.storage.get("news-curator.auth.state");
  await assert.rejects(
    client.finishCallback(new URL(`https://news.example/auth/callback/?code=x&client_state=${badState}-wrong`), async () => {
      throw new Error("fetch must not run");
    }),
    /could not be verified/
  );
  assert.equal(browser.storage.has("news-curator.auth.state"), false);
  assert.equal(browser.storage.has("news-curator.auth.verifier"), false);

  const authConfig = { url: "https://example.supabase.co", key: "sb_publishable_test" };
  const calls = [];
  const getFetch = async (url, options) => {
    calls.push({ url, options });
    return response(200, [preference()], url);
  };
  const got = await client.getPreferences(authConfig, projected, getFetch);
  assert.equal(got.user_id, "user-a");
  assert.equal(calls[0].options.headers.authorization, `Bearer ${projected.access_token}`);
  assertFailClosedFetch(calls[0]);

  const expired = { ...projected, expires_at: now - 1 };
  browser.storage.set("news-curator.auth.session", JSON.stringify(expired));
  const refreshGetCalls = [];
  const refreshGet = async (url, options) => {
    refreshGetCalls.push({ url, options });
    if (refreshGetCalls.length === 1) return response(200, refreshedSession(), url);
    return response(200, [preference()], url);
  };
  const refreshedGet = await client.getPreferences(authConfig, expired, refreshGet);
  assert.equal(refreshedGet.user_id, "user-a");
  assert.equal(refreshGetCalls[0].url, "https://example.supabase.co/auth/v1/token?grant_type=refresh_token");
  assert.deepEqual(JSON.parse(refreshGetCalls[0].options.body), { refresh_token: "refresh-token" });
  assert.equal(refreshGetCalls[0].options.headers.apikey, "sb_publishable_test");
  assert.equal(refreshGetCalls[1].options.headers.authorization, `Bearer ${refreshedProjection.access_token}`);
  refreshGetCalls.forEach(assertFailClosedFetch);
  assert.deepEqual(
    JSON.parse(browser.storage.get("news-curator.auth.session")),
    refreshedProjection
  );

  browser.storage.set("news-curator.auth.session", JSON.stringify(expired));
  await assert.rejects(
    client.getPreferences(authConfig, expired, async (url) => response(401, { error: "invalid_grant" }, url)),
    /Session refresh failed/
  );
  assert.equal(browser.storage.has("news-curator.auth.session"), false);

  let parsedRefreshRedirectBody = false;
  browser.storage.set("news-curator.auth.session", JSON.stringify(expired));
  await assert.rejects(
    client.getPreferences(authConfig, expired, async (url) => response(200, refreshedSession(), `${url}/moved`, {
      redirected: true,
      text: async () => { parsedRefreshRedirectBody = true; return JSON.stringify(refreshedSession()); },
    })),
    /Session refresh failed/
  );
  assert.equal(parsedRefreshRedirectBody, false);
  assert.equal(browser.storage.has("news-curator.auth.session"), false);

  browser.storage.set("news-curator.auth.session", JSON.stringify(expired));
  await assert.rejects(
    client.getPreferences(
      authConfig,
      expired,
      async (url) => response(200, refreshedSession(), `${url}#mismatch`)
    ),
    /Session refresh failed/
  );
  assert.equal(browser.storage.has("news-curator.auth.session"), false);

  browser.storage.set("news-curator.auth.session", JSON.stringify(expired));
  await assert.rejects(
    client.refreshSession(
      { url: "https://example.supabase.co/path", key: "sb_publishable_test" },
      expired,
      async () => { throw new Error("fetch must not run"); }
    ),
    /Session refresh failed/
  );
  assert.equal(browser.storage.has("news-curator.auth.session"), false);

  const update = {
    expected_revision: 0,
    locale: "en",
    interests: ["agents"],
    saved_searches: [{ id: "daily", query: "agent news", enabled: true }],
  };
  const setCalls = [];
  const setFetch = async (url, options) => {
    setCalls.push({ url, options });
    const payload = setCalls.length === 1 ? { status: "updated", revision: 1 } : [preference(1)];
    return response(200, payload, url);
  };
  const setResult = await client.setPreferences(authConfig, projected, update, setFetch);
  assert.equal(setResult.status, "updated");
  assert.equal(setResult.preference.revision, 1);
  assert.equal(setCalls[0].url.endsWith("/rest/v1/rpc/compare_and_swap_user_preferences"), true);
  assert.deepEqual(JSON.parse(setCalls[0].options.body), {
    expected_revision: 0,
    new_locale: "en",
    new_interests: ["agents"],
    new_saved_searches: [{ id: "daily", query: "agent news", enabled: true }],
  });
  setCalls.forEach(assertFailClosedFetch);

  browser.storage.set("news-curator.auth.session", JSON.stringify(expired));
  const refreshSetCalls = [];
  const refreshSetFetch = async (url, options) => {
    refreshSetCalls.push({ url, options });
    if (refreshSetCalls.length === 1) return response(200, refreshedSession(), url);
    if (refreshSetCalls.length === 2) return response(200, { status: "updated", revision: 1 }, url);
    return response(200, [preference(1)], url);
  };
  const refreshedSet = await client.setPreferences(authConfig, expired, update, refreshSetFetch);
  assert.equal(refreshedSet.status, "updated");
  assert.equal(refreshedSet.preference.revision, 1);
  assert.equal(refreshSetCalls[1].options.headers.authorization, `Bearer ${refreshedProjection.access_token}`);
  assert.equal(refreshSetCalls[2].options.headers.authorization, `Bearer ${refreshedProjection.access_token}`);
  refreshSetCalls.forEach(assertFailClosedFetch);

  const createCalls = [];
  const createFetch = async (url, options) => {
    createCalls.push({ url, options });
    const payload = createCalls.length === 1 ? { status: "not_found" } : [preference()];
    return response(createCalls.length === 1 ? 200 : 201, payload, url);
  };
  assert.equal((await client.setPreferences(authConfig, projected, update, createFetch)).status, "created");
  createCalls.forEach(assertFailClosedFetch);

  let parsedRedirectBody = false;
  await assert.rejects(
    client.getPreferences(authConfig, projected, async (url) => response(200, [preference()], `${url}/moved`, {
      redirected: true,
      text: async () => { parsedRedirectBody = true; return JSON.stringify([preference()]); },
    })),
    /redirected unexpectedly/
  );
  assert.equal(parsedRedirectBody, false);
  await assert.rejects(
    client.getPreferences(authConfig, projected, async (url) => response(200, [preference()], `${url}#mismatch`)),
    /redirected unexpectedly/
  );

  browser.storage.set("news-curator.auth.session", JSON.stringify({ ...projected, expires_at: now - 1 }));
  assert.throws(() => client.loadSession(now), /saved session/);
  assert.equal(browser.storage.has("news-curator.auth.session"), false);

  browser.storage.set("news-curator.auth.session", JSON.stringify(projected));
  const logoutCalls = [];
  await client.signOut(async (url, options) => {
    logoutCalls.push({ url, options });
    return response(204, null, url);
  });
  assert.equal(browser.storage.has("news-curator.auth.session"), false);
  assert.equal(logoutCalls[0].options.headers.authorization, `Bearer ${projected.access_token}`);
  assertFailClosedFetch(logoutCalls[0]);

  browser.storage.set("news-curator.auth.session", JSON.stringify(projected));
  await assert.rejects(
    client.signOut(async (url) => response(204, null, `${url}/moved`, { redirected: true })),
    /redirected unexpectedly/
  );
  assert.equal(browser.storage.has("news-curator.auth.session"), false);

  assert.throws(
    () => client.validatePreferenceInput({ ...update, saved_searches: [{ id: "x", query: "q", enabled: true, extra: 1 }] }),
    /preference input/
  );
  console.log("browser personalization contract: PASS");
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : "browser personalization contract failed");
  process.exitCode = 1;
});
