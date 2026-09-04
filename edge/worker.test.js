import test from 'node:test';
import assert from 'node:assert/strict';

import { createAccessVerifier } from './access.js';
import { hasRequiredPrivateHeaders } from './headers.js';
import workerEntry, { createWorker, classifyPath, isSafePath, numericSetting, rawPathOf, resolveMeterPolicy } from './worker.js';
import { DEFAULT_METER_POLICY } from './meter.js';
import { ISSUER } from './_helpers.js';
import {
  AUD,
  assertForgedTokensDenied,
  assertSignedOutDenied,
  makeJwksFetch,
  makeKeyMaterial,
  PRIVATE_ROUTES,
  requestFor,
  signToken,
  tamperSignature,
} from './_helpers.js';

const FIXED_NOW_MS = 1_756_000_000_000;
const nowSec = Math.floor(FIXED_NOW_MS / 1000);
const material = await makeKeyMaterial();

function buildWorker() {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const verifier = createAccessVerifier({
    teamName: 'example-team',
    aud: AUD,
    fetchImpl,
    now: () => FIXED_NOW_MS,
  });
  return createWorker({
    verifier,
    now: () => FIXED_NOW_MS,
    readMeters: async () => ({ requests_per_day: { value: 12, sampled_at: FIXED_NOW_MS } }),
  });
}

test('every private route class denies a signed-out request', async () => {
  await assertSignedOutDenied(buildWorker());
});

test('a signed-out denial leaks no body content from the private route', async () => {
  const worker = buildWorker();
  for (const [, path] of PRIVATE_ROUTES) {
    const res = await worker.fetch(requestFor(path, null));
    const body = await res.text();
    assert.ok(!body.includes('SYNTHETIC PLACEHOLDER'), `${path} denial must not contain projection content`);
    assert.ok(!body.includes('<h1>'), `${path} denial must not contain app markup`);
  }
});

test('a forged or broken token is denied on every private route', async () => {
  const worker = buildWorker();
  const good = await signToken(material, { nowSec });
  const bad = [
    ['tampered', tamperSignature(good)],
    ['expired', await signToken(material, { nowSec, claims: { exp: nowSec - 3600, nbf: nowSec - 7200 } })],
    ['wrong aud', await signToken(material, { nowSec, claims: { aud: 'z'.repeat(64) } })],
    ['garbage', 'not.a.jwt'],
  ];
  await assertForgedTokensDenied(worker, bad);
});

test('a valid token gets the HTML app with the full private header set', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/', token));
  assert.equal(res.status, 200);
  assert.ok(hasRequiredPrivateHeaders(res), 'private response must carry no-store, Vary and noindex and carry no ETag');
  assert.match(await res.text(), /Private projection/);
});

test('a valid token gets the synthetic projection, and it is obviously synthetic', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/projection/synthetic', token));
  assert.equal(res.status, 200);
  assert.ok(hasRequiredPrivateHeaders(res));
  const body = await res.json();
  assert.equal(body.synthetic, true);
  assert.equal(body.subject_present, true);
  assert.match(body.entries[0].headline, /SYNTHETIC/);
});

test('a source map is refused even with a valid token', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/static/bundle.js.map', token));
  assert.equal(res.status, 404);
  assert.ok(hasRequiredPrivateHeaders(res));
});

test('the limit receipt route returns a receipt-shaped body to a valid session', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/receipt/limits', token));
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.envelope.kind, 'host_limits');
  assert.equal(body.envelope.tenant_id, 'tenant-owner-private');
  assert.equal(body.envelope.actor_kind, 'system');
  assert.equal(body.envelope.user_id, null);
  assert.equal(body.meter_source, 'host_analytics_api');
});

test('the health route answers without a token and carries no reader data', async () => {
  const worker = buildWorker();
  const res = await worker.fetch(requestFor('/healthz', null));
  assert.equal(res.status, 200);
  assert.equal(await res.text(), 'ok');
});

test('path classification is an allowlist: anything unlisted is private', () => {
  assert.equal(classifyPath('/healthz'), 'public');
  assert.equal(classifyPath('/'), 'html_app');
  assert.equal(classifyPath('/api/x'), 'api_record');
  assert.equal(classifyPath('/a/b.js.map'), 'source_map');
  assert.equal(classifyPath('/some/new/thing'), 'unknown');
});

test('a verifier that throws is a denial, never an allow', async () => {
  const worker = createWorker({
    verifier: {
      verify: async () => {
        throw new Error('key set unreachable');
      },
    },
    now: () => FIXED_NOW_MS,
  });
  const res = await worker.fetch(requestFor('/', 'anything'));
  assert.equal(res.status, 401);
  assert.equal((await res.json()).reason, 'verifier_error');
});

// ---------------------------------------------------------------------------
// Method and path handling. Both run BEFORE routing, so neither can be used to
// probe a route with a verb or an encoding its handler never expected.
// ---------------------------------------------------------------------------

function requestWith(path, method, token) {
  const headers = token ? { 'Cf-Access-Jwt-Assertion': token } : {};
  return new Request(`https://private.example.invalid${path}`, { method, headers });
}

test('only GET is answered: POST, HEAD and OPTIONS are refused before routing', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const cases = [
    ['POST', '/'],
    ['HEAD', '/'],
    ['OPTIONS', '/api/r'],
    ['DELETE', '/api/r'],
    ['PUT', '/healthz'],
  ];
  for (const [method, path] of cases) {
    const res = await worker.fetch(requestWith(path, method, token));
    assert.equal(res.status, 405, `${method} ${path} must be refused even with a valid token`);
    assert.ok(hasRequiredPrivateHeaders(res), `${method} ${path} refusal must carry the private header set`);
    const body = await res.text();
    assert.ok(!body.includes('<h1>'), `${method} ${path} must not return app markup`);
  }
});

test('an encoded separator or a path that decodes to something else is refused', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  for (const path of ['/app%2Fprivate', '/app%2fprivate', '/app/..%2F', '/app%5Cx', '/app%5cx']) {
    const res = await worker.fetch(requestFor(path, token));
    assert.equal(res.status, 400, `${path} has two readings and must not be routed`);
    assert.ok(hasRequiredPrivateHeaders(res));
  }
});

test('dot-segment traversal is normalized away by the URL parser and lands on deny-by-default', async () => {
  // Documented, not assumed: the runtime resolves /api/%2e%2e/x to /x BEFORE the
  // Worker sees it, so this form cannot be used to reach a route. It becomes an
  // ordinary unlisted path, which the allowlist answers with 404.
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/api/%2e%2e/x', token));
  assert.equal(new URL('https://h.invalid/api/%2e%2e/x').pathname, '/x');
  assert.equal(res.status, 404);
  assert.ok(!(await res.text()).includes('<h1>'));
});

test('isSafePath and rawPathOf reject the backslash form the URL parser hides', () => {
  assert.equal(isSafePath('/app/today'), true);
  assert.equal(isSafePath('/app%2Fprivate'), false);
  assert.equal(isSafePath('/app\\private'), false);
  assert.equal(isSafePath('/bad%zz'), false);
  assert.equal(rawPathOf('https://h.invalid/a\\b'), '/a\\b');
  assert.equal(isSafePath(rawPathOf('https://h.invalid/a\\b')), false);
});

test('/app matches only at a path boundary, so /application is not the app', async () => {
  assert.equal(classifyPath('/application'), 'unknown');
  assert.equal(classifyPath('/app'), 'html_app');
  assert.equal(classifyPath('/app/today'), 'html_app');
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/application', token));
  assert.equal(res.status, 404, '/application must not inherit the app route');
  assert.ok(!(await res.text()).includes('<h1>'));
});

// ---------------------------------------------------------------------------
// Valid-session route behavior that the evidence doc claims.
// ---------------------------------------------------------------------------

test('a valid token gets the API record with the full private header set', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/api/records/1', token));
  assert.equal(res.status, 200);
  assert.ok(hasRequiredPrivateHeaders(res), 'the API record must carry no-store, Vary, noindex and no validator');
  assert.equal(res.headers.get('referrer-policy'), 'no-referrer');
  assert.equal(res.headers.get('x-content-type-options'), 'nosniff');
  const body = await res.json();
  assert.equal(body.record, 'private');
  assert.equal(body.path, '/api/records/1');
});

test('an unlisted path is a 404 with the private headers even with a valid token', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/whatever/else', token));
  assert.equal(res.status, 404);
  assert.ok(hasRequiredPrivateHeaders(res));
  assert.equal((await res.json()).reason, 'not_found');
});

// ---------------------------------------------------------------------------
// Meter source failure. Losing the receipt would lose the only record that we
// could not read the meter.
// ---------------------------------------------------------------------------

test('a throwing meter reader still emits a receipt, marked unavailable and all unknown', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const worker = createWorker({
    verifier: createAccessVerifier({ teamName: 'example-team', aud: AUD, fetchImpl, now: () => FIXED_NOW_MS }),
    now: () => FIXED_NOW_MS,
    readMeters: async () => {
      throw new Error('graphql 500');
    },
  });
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/receipt/limits', token));
  assert.equal(res.status, 200, 'the handler must not throw the platform 500 that loses every private header');
  assert.ok(hasRequiredPrivateHeaders(res));
  const body = await res.json();
  assert.equal(body.meter_source, 'unavailable');
  assert.equal(body.final_state, 'unknown');
  for (const row of body.readings) {
    assert.equal(row.freshness_verdict, 'unknown', `${row.meter} must be unknown, never zero`);
    assert.equal(row.value, null);
  }
});

test('the limit receipt carries the Ownership-extended envelope keys', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec });
  const body = await (await worker.fetch(requestFor('/receipt/limits', token))).json();
  assert.equal(body.envelope.actor_id, 'actor-system');
  assert.equal(body.envelope.actor_kind, 'system');
  assert.equal(body.envelope.user_id, null, 'user_id is null only because the actor kind is system');
  assert.equal(typeof body.envelope.policy_revision, 'number');
  assert.equal(body.attributed_operation_class, 'edge_request');
});

// ---------------------------------------------------------------------------
// The DEPLOYED entry point. A verifier built per request throws away the JWKS
// cache and the refetch floor, turning every inbound request into an outbound
// subrequest against the 50-per-request limit.
// ---------------------------------------------------------------------------

function stubGlobalFetch(keys) {
  const state = { calls: 0 };
  const original = globalThis.fetch;
  globalThis.fetch = async () => {
    state.calls += 1;
    return { ok: true, status: 200, json: async () => ({ keys }) };
  };
  return { state, restore: () => { globalThis.fetch = original; } };
}

test('the exported worker keeps one verifier per isolate: two requests, one JWKS fetch', async () => {
  const env = { ACCESS_TEAM_NAME: 'isolate-team-a', ACCESS_AUD: AUD };
  const { state, restore } = stubGlobalFetch([material.publicJwk]);
  try {
    const token = await signToken(material, {
      nowSec: Math.floor(Date.now() / 1000),
      claims: { iss: `https://${env.ACCESS_TEAM_NAME}.cloudflareaccess.com` },
    });
    const a = await workerEntry.fetch(requestFor('/', token), env);
    const b = await workerEntry.fetch(requestFor('/', token), env);
    assert.equal(a.status, 200);
    assert.equal(b.status, 200);
    assert.equal(state.calls, 1, 'inside the TTL the second request must reuse the cached key set');
  } finally {
    restore();
  }
});

test('the exported worker holds the refetch floor: five unknown-kid requests, one JWKS fetch', async () => {
  const env = { ACCESS_TEAM_NAME: 'isolate-team-b', ACCESS_AUD: AUD };
  const { state, restore } = stubGlobalFetch([material.publicJwk]);
  try {
    const token = await signToken(material, {
      nowSec: Math.floor(Date.now() / 1000),
      header: { kid: 'never-issued' },
      claims: { iss: `https://${env.ACCESS_TEAM_NAME}.cloudflareaccess.com` },
    });
    for (let i = 0; i < 5; i += 1) {
      const res = await workerEntry.fetch(requestFor('/', token), env);
      assert.equal(res.status, 401);
    }
    assert.equal(state.calls, 1, 'an unauthenticated caller must not amplify into one outbound fetch per request');
  } finally {
    restore();
  }
});

test('meter thresholds are programmable policy: METER_POLICY_JSON is read, and a broken one is refused', () => {
  assert.equal(resolveMeterPolicy({}).policy_revision, 1);
  const overridden = resolveMeterPolicy({ METER_POLICY_JSON: '{"policy_revision":42,"staleness_ms":0,"meters":{}}' });
  assert.equal(overridden.policy_revision, 42);
  assert.throws(() => resolveMeterPolicy({ METER_POLICY_JSON: 'not json' }));
  assert.throws(() => resolveMeterPolicy({ METER_POLICY_JSON: '{"policy_revision":1}' }));
});

test('the issuer helper constant is the one the fixtures sign with', () => {
  assert.match(ISSUER, /^https:\/\/example-team\.cloudflareaccess\.com$/);
});

// ---------------------------------------------------------------------------
// Round 2, item 1: a Worker that cannot be BUILT must refuse, not throw.
//
// Construction reads configuration. A malformed METER_POLICY_JSON, a missing
// ACCESS_TEAM_NAME or ACCESS_AUD, or a non-numeric tuning value used to throw
// out of fetch() and surface as a platform exception. Every one of them is now
// a 503 that carries the private header set and says nothing about the cause.
// ---------------------------------------------------------------------------

const BAD_ENVS = Object.freeze([
  ['malformed METER_POLICY_JSON', { ACCESS_TEAM_NAME: 'cfg-team', ACCESS_AUD: AUD, METER_POLICY_JSON: '{oops' }],
  ['policy without a meters map', { ACCESS_TEAM_NAME: 'cfg-team', ACCESS_AUD: AUD, METER_POLICY_JSON: '{"policy_revision":1}' }],
  ['empty env', {}],
  ['missing ACCESS_TEAM_NAME', { ACCESS_AUD: AUD }],
  ['missing ACCESS_AUD', { ACCESS_TEAM_NAME: 'cfg-team' }],
  ['non-numeric ACCESS_CLOCK_SKEW_SEC', { ACCESS_TEAM_NAME: 'cfg-team', ACCESS_AUD: AUD, ACCESS_CLOCK_SKEW_SEC: 'soon' }],
  ['negative JWKS_CACHE_TTL_MS', { ACCESS_TEAM_NAME: 'cfg-team', ACCESS_AUD: AUD, JWKS_CACHE_TTL_MS: '-1' }],
  ['non-numeric JWKS_MIN_REFETCH_MS', { ACCESS_TEAM_NAME: 'cfg-team', ACCESS_AUD: AUD, JWKS_MIN_REFETCH_MS: 'often' }],
  ['non-numeric JWKS_STALE_GRACE_MS', { ACCESS_TEAM_NAME: 'cfg-team', ACCESS_AUD: AUD, JWKS_STALE_GRACE_MS: 'a while' }],
]);

test('an invalid configuration is a 503 on every private route, never an exception', async () => {
  for (const [label, env] of BAD_ENVS) {
    for (const [routeName, path] of PRIVATE_ROUTES) {
      const res = await workerEntry.fetch(requestFor(path, null), env);
      assert.equal(res.status, 503, `${label} on ${routeName} must refuse with 503, got ${res.status}`);
      assert.ok(hasRequiredPrivateHeaders(res), `${label} on ${routeName} must carry the private header set`);
    }
  }
});

test('an invalid configuration denies a private route even with a token that would otherwise verify', async () => {
  const env = { ACCESS_TEAM_NAME: 'cfg-team', ACCESS_AUD: AUD, METER_POLICY_JSON: '{oops' };
  const { restore } = stubGlobalFetch([material.publicJwk]);
  try {
    const token = await signToken(material, {
      nowSec: Math.floor(Date.now() / 1000),
      claims: { iss: `https://${env.ACCESS_TEAM_NAME}.cloudflareaccess.com` },
    });
    const res = await workerEntry.fetch(requestFor('/app/today', token), env);
    assert.equal(res.status, 503, 'a private route under an invalid configuration is refused, never served');
    const body = await res.text();
    assert.ok(!body.includes('Private projection'), 'the app body must not be served under an invalid configuration');
  } finally {
    restore();
  }
});

test('a construction failure leaks no exception text, no configuration value and no key set URL', async () => {
  for (const [label, env] of BAD_ENVS) {
    const res = await workerEntry.fetch(requestFor('/api/records/1', null), env);
    const body = await res.text();
    assert.equal(body, JSON.stringify({ error: 'unavailable', reason: 'worker_unavailable' }), `${label} body must be the constant refusal`);
    for (const needle of ['oops', 'cloudflareaccess.com', 'cdn-cgi', 'METER_POLICY_JSON', 'ACCESS_TEAM_NAME', 'must be']) {
      assert.ok(!body.includes(needle), `${label} body must not mention ${needle}`);
    }
  }
});

test('the health route under an invalid configuration reports degraded without saying what is wrong', async () => {
  for (const [label, env] of BAD_ENVS) {
    const res = await workerEntry.fetch(requestFor('/healthz', null), env);
    assert.equal(res.status, 503, `${label}: health must report the outage`);
    assert.ok(hasRequiredPrivateHeaders(res), `${label}: health refusal still carries the private header set`);
    const payload = JSON.parse(await res.text());
    assert.deepEqual(payload, { status: 'degraded', config: 'invalid' }, `${label}: health says invalid and nothing else`);
  }
});

test('a valid configuration still serves the public route: the 503 is the failure path, not the default', async () => {
  const res = await workerEntry.fetch(requestFor('/healthz', null), { ACCESS_TEAM_NAME: 'cfg-team-ok', ACCESS_AUD: AUD });
  assert.equal(res.status, 200);
  assert.equal(await res.text(), 'ok');
});

// ---------------------------------------------------------------------------
// Round 2, item 5: percent-encoding is allowed when it has ONE reading.
//
// A product with a Chinese lane needs non-ASCII slugs, and every one of those
// arrives percent-encoded. Refusing all percent-encoding would break that lane.
// What stays refused is a path that means two different things to two parsers.
// ---------------------------------------------------------------------------

test('a UTF-8 percent-encoded Chinese slug is safe and reaches the router', async () => {
  // %E4%B8%AD%E6%96%87 is the UTF-8 encoding of 中文.
  const encoded = '/app/%E4%B8%AD%E6%96%87';
  assert.equal(decodeURIComponent(encoded), '/app/中文', 'fixture sanity: this is the Chinese slug');
  assert.equal(isSafePath(encoded), true, 'a single-reading encoded path must be routable');

  const worker = buildWorker();
  const denied = await worker.fetch(requestFor(encoded, null));
  assert.equal(denied.status, 401, 'it reaches the router, so it is denied by auth rather than by path shape');

  const token = await signToken(material, { nowSec, claims: { iss: ISSUER } });
  const served = await worker.fetch(requestFor(encoded, token));
  assert.equal(served.status, 200, 'a verified session gets the app route for a Chinese slug');
});

test('an encoded path that decodes to a second reading is still refused, one form at a time', () => {
  const refused = [
    ['%2F encoded slash', '/app/a%2Fb'],
    ['%2f lowercase slash', '/app/a%2fb'],
    ['%5C encoded backslash', '/app/a%5Cb'],
    ['%252F double-encoded slash', '/app/a%252Fb'],
    ['%2e%2e encoded dot segment', '/app/%2e%2e/secret'],
    ['%2E%2E uppercase dot segment', '/app/%2E%2E/secret'],
    ['%00 encoded control character', '/app/a%00b'],
    ['%41 alternate encoding of an ASCII character', '/app/%41'],
  ];
  for (const [label, path] of refused) {
    assert.equal(isSafePath(path), false, `${label} must be refused: ${path}`);
  }
});

test('the refused encodings are 400 at the edge, not a route decision', async () => {
  const worker = buildWorker();
  const token = await signToken(material, { nowSec, claims: { iss: ISSUER } });
  for (const path of ['/app/a%2Fb', '/app/a%5Cb', '/app/a%252Fb']) {
    const res = await worker.fetch(requestFor(path, token));
    assert.equal(res.status, 400, `${path} must be refused on shape even with a valid token`);
    assert.ok(hasRequiredPrivateHeaders(res), `${path} refusal carries the private header set`);
  }
  // %2e%2e is the one case the shape check never sees: the URL parser decodes a
  // dot triplet and resolves the segment away before request.url exists, so
  // `/app/%2e%2e/secret` arrives as `/secret`. isSafePath refuses that form on
  // its own (asserted above); at the router it lands on deny-by-default, which
  // is the same outcome by a different door. What must never happen is the app
  // body coming back from a traversal.
  const traversed = await worker.fetch(requestFor('/app/%2e%2e/secret', token));
  assert.equal(traversed.status, 404, 'a traversal normalizes to an unlisted path and is refused');
  assert.ok(!(await traversed.text()).includes('Private projection'), 'a traversal never yields the app body');
});

test('a dot segment that resolves ONTO the public route is the public route, and gives nothing private', async () => {
  // The counterpart of the traversal test above, and the one a reviewer asks
  // about: `/app/../healthz` normalizes to `/healthz` before the Worker runs,
  // so a signed-out caller gets the public answer. That is correct, and it is
  // only correct because the public route is a fixed string with no reader
  // data in it. Documented in edge/README.md so nobody has to rediscover it.
  assert.equal(new URL('https://h.invalid/app/../healthz').pathname, '/healthz');
  const worker = buildWorker();
  const res = await worker.fetch(requestFor('/app/../healthz', null));
  assert.equal(res.status, 200);
  assert.equal(await res.text(), 'ok', 'normalizing onto the public route yields the public constant, nothing else');
});

// ---------------------------------------------------------------------------
// Round 3, item 3: GET /receipt/limits NEVER throws out of fetch().
//
// Round 2 closed the CONSTRUCTION crash. These configurations all PARSE, so
// they build a Worker successfully, pass the method gate, the path gate and the
// token check, and used to throw one route deep, producing a platform exception
// carrying none of the private headers this layer guarantees on every response.
// Every one of them is now a controlled refusal.
// ---------------------------------------------------------------------------

const INCOMPLETE_POLICIES = Object.freeze([
  ['no policy_revision', '{"meters":{}}'],
  ['null policy_revision', '{"policy_revision":null,"meters":{}}'],
  ['a null meter spec', '{"policy_revision":9,"meters":{"x":null}}'],
  ['a spec that is not an object', '{"policy_revision":9,"meters":{"x":"nope"}}'],
  ['a spec missing its unit', '{"policy_revision":9,"meters":{"a":{"meter_kind":"cumulative_budget","hard_stop_threshold":10}}}'],
  ['a spec with an unknown meter_kind', '{"policy_revision":9,"meters":{"a":{"meter_kind":"vibes","unit":"ms"}}}'],
  ['meters as null', '{"policy_revision":9,"meters":null}'],
  ['meters as an array', '{"policy_revision":9,"meters":[]}'],
]);

test('a policy that parses but is incomplete is a 503, never an uncaught exception on the receipt route', async () => {
  const { restore } = stubGlobalFetch([material.publicJwk]);
  try {
    for (const [label, policy] of INCOMPLETE_POLICIES) {
      const env = { ACCESS_TEAM_NAME: 'cfg-team', ACCESS_AUD: AUD, METER_POLICY_JSON: policy };
      const token = await signToken(material, {
        nowSec: Math.floor(Date.now() / 1000),
        claims: { iss: `https://${env.ACCESS_TEAM_NAME}.cloudflareaccess.com` },
      });
      const res = await workerEntry.fetch(requestFor('/receipt/limits', token), env);
      assert.equal(res.status, 503, `${label} must be a refusal, got ${res.status}`);
      assert.ok(hasRequiredPrivateHeaders(res), `${label} refusal must carry the private header set`);
      const body = await res.text();
      assert.equal(body, JSON.stringify({ error: 'unavailable', reason: 'worker_unavailable' }), `${label} body is the constant refusal`);
    }
  } finally {
    restore();
  }
});

test('resolveMeterPolicy refuses every incomplete shape at configuration time', () => {
  for (const [label, policy] of INCOMPLETE_POLICIES) {
    assert.throws(() => resolveMeterPolicy({ METER_POLICY_JSON: policy }), `${label} must be a configuration error`);
  }
});

test('a meter reader that throws still yields a receipt, and it is all unknown', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const worker = createWorker({
    verifier: createAccessVerifier({ teamName: 'example-team', aud: AUD, fetchImpl, now: () => FIXED_NOW_MS }),
    now: () => FIXED_NOW_MS,
    readMeters: async () => { throw new Error('analytics unreachable'); },
  });
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/receipt/limits', token));
  assert.equal(res.status, 200, 'losing the receipt would lose the only record that we could not read');
  const body = await res.json();
  assert.equal(body.meter_source, 'unavailable');
  assert.ok(body.readings.length > 0);
  for (const row of body.readings) {
    assert.equal(row.value, null, `${row.meter} must be unknown, never zero`);
    assert.equal(row.freshness_verdict, 'unknown');
  }
  assert.equal(body.final_state, 'unknown');
  assert.equal(body.envelope.state, 'unknown');
  assert.equal(body.envelope.settled_at, null);
});

test('a receipt that cannot be built at the route is a 503, not a platform exception', async () => {
  // A policy handed straight to createWorker, bypassing resolveMeterPolicy: the
  // route's own catch is what is under test here, independently of the config
  // validation in front of it.
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const worker = createWorker({
    verifier: createAccessVerifier({ teamName: 'example-team', aud: AUD, fetchImpl, now: () => FIXED_NOW_MS }),
    now: () => FIXED_NOW_MS,
    meterPolicy: { meters: { a: { meter_kind: 'cumulative_budget', unit: 'requests' } } },
  });
  const token = await signToken(material, { nowSec });
  const res = await worker.fetch(requestFor('/receipt/limits', token));
  assert.equal(res.status, 503, 'a receipt with no policy_revision is unauditable, so it is refused');
  assert.ok(hasRequiredPrivateHeaders(res));
  assert.equal(await res.text(), JSON.stringify({ error: 'unavailable', reason: 'worker_unavailable' }));
});

// ---------------------------------------------------------------------------
// Round 3, item 4: an empty policy is legal to configure and impossible to
// mistake for a pass.
// ---------------------------------------------------------------------------

test('the shipped example policy is a complete receipt, and an empty one settles nothing', async () => {
  const { restore } = stubGlobalFetch([material.publicJwk]);
  try {
    const env = {
      ACCESS_TEAM_NAME: 'cfg-team',
      ACCESS_AUD: AUD,
      METER_POLICY_JSON: '{"policy_revision":1,"staleness_ms":900000,"meters":{}}',
    };
    const token = await signToken(material, {
      nowSec: Math.floor(Date.now() / 1000),
      claims: { iss: `https://${env.ACCESS_TEAM_NAME}.cloudflareaccess.com` },
    });
    const res = await workerEntry.fetch(requestFor('/receipt/limits', token), env);
    assert.equal(res.status, 200, 'an empty meters map is a legal configuration');
    const body = await res.json();
    assert.deepEqual(body.readings, []);
    assert.equal(body.final_state, 'unknown', 'green on nothing is the false verdict this rule exists to stop');
    assert.equal(body.envelope.state, 'unknown');
    assert.equal(body.envelope.settled_at, null);
    assert.equal(body.envelope.reason_code, 'no_meters_configured');
  } finally {
    restore();
  }
});

// ---------------------------------------------------------------------------
// Round 3, item 6: the method gate is decided BEFORE configuration, so it
// cannot flip between 405 and 503; and the config-failure response is compared
// as a WHOLE header set, not only as a body.
// ---------------------------------------------------------------------------

test('an unsupported method is 405 under a valid configuration AND under a broken one', async () => {
  const goodEnv = { ACCESS_TEAM_NAME: 'cfg-team-ok', ACCESS_AUD: AUD };
  for (const [label, badEnv] of BAD_ENVS) {
    for (const [method, path] of [['HEAD', '/healthz'], ['OPTIONS', '/healthz'], ['POST', '/'], ['HEAD', '/receipt/limits']]) {
      const good = await workerEntry.fetch(requestWith(path, method, null), goodEnv);
      const bad = await workerEntry.fetch(requestWith(path, method, null), badEnv);
      assert.equal(good.status, 405, `${method} ${path} under a valid config must be 405`);
      assert.equal(bad.status, 405, `${method} ${path} under ${label} must be 405 too, not 503`);
      assert.ok(hasRequiredPrivateHeaders(bad), `${method} ${path} under ${label} carries the private header set`);
    }
  }
});

test('a construction failure returns the identical header set on every bad configuration, and leaks no value into it', async () => {
  const seen = new Set();
  const forbidden = ['oops', 'soon', 'often', 'a while', 'cfg-team', 'cloudflareaccess.com', 'cdn-cgi', 'METER_POLICY_JSON', 'ACCESS_TEAM_NAME', 'ACCESS_AUD', AUD, '-1'];
  for (const [label, env] of BAD_ENVS) {
    const res = await workerEntry.fetch(requestFor('/api/records/1', null), env);
    const headers = [...res.headers].map(([k, v]) => `${k}: ${v}`).sort();
    for (const line of headers) {
      for (const needle of forbidden) {
        assert.ok(!line.includes(needle), `${label}: header ${line} must not carry a configuration value`);
      }
    }
    seen.add(JSON.stringify([res.status, headers]));
  }
  assert.equal(seen.size, 1, 'every bad configuration must be indistinguishable from the outside, headers included');
});

// ---------------------------------------------------------------------------
// Round 3, item 6: the percent-decoding probes from the review, pinned so the
// behavior cannot regress silently.
// ---------------------------------------------------------------------------

test('mixed-case percent triplets round-trip, and overlong UTF-8 and %00 stay refused', async () => {
  // Percent triplets are case-insensitive on the wire, so the lowercase form of
  // the Chinese slug is the SAME path and must be routable.
  const mixedCase = '/app/%e4%b8%ad%e6%96%87';
  assert.equal(decodeURIComponent(mixedCase), '/app/中文', 'fixture sanity');
  assert.equal(isSafePath(mixedCase), true, 'a lowercase triplet is the same path as its uppercase form');
  assert.equal(isSafePath('/app/%E4%b8%AD%e6%96%87'), true, 'and so is a mixture of the two');

  // Overlong UTF-8: %C0%AF is a non-shortest encoding of '/'. decodeURIComponent
  // refuses it outright, and so must the path gate: a decoder that accepted it
  // would see a separator the prefix check never saw.
  for (const path of ['/app/%C0%AF', '/app/%C0%AE%C0%AE', '/app/%c0%af', '/app/a%00b', '/app/%00']) {
    assert.equal(isSafePath(path), false, `${path} must be refused`);
  }

  const worker = buildWorker();
  const token = await signToken(material, { nowSec, claims: { iss: ISSUER } });
  assert.equal((await worker.fetch(requestFor(mixedCase, token))).status, 200, 'the mixed-case slug reaches the app route');
  for (const path of ['/app/%C0%AF', '/app/a%00b']) {
    const res = await worker.fetch(requestFor(path, token));
    assert.equal(res.status, 400, `${path} is refused on shape even with a valid token`);
    assert.ok(hasRequiredPrivateHeaders(res));
  }
});

// ---------------------------------------------------------------------------
// Round 4, items 2 to 4 and the threshold should-fixes: the policy validator
// now checks the KEYS, not only the values it happens to recognize.
//
// The defect this closes: `hard_stop` instead of `hard_stop_threshold` was
// indistinguishable from an absent threshold, so a meter reading 5,000,000
// against an intended stop of 90,000 settled GREEN with a settled timestamp.
// The same hole swallowed `staleness_mss` (silently falling back to the
// 15-minute default) and arbitrary junk keys.
// ---------------------------------------------------------------------------

const STRICT_POLICY_REJECTS = new Map([
  // Unknown keys, the round-4 finding itself.
  ['a misspelled staleness_ms', '{"policy_revision":1,"staleness_mss":0,"meters":{}}'],
  ['a junk top-level key', '{"policy_revision":1,"bogus_key":"whatever","meters":{}}'],
  [
    'a misspelled hard_stop_threshold',
    '{"policy_revision":1,"meters":{"requests_per_day":{"meter_kind":"cumulative_budget","unit":"requests","hard_stop_treshold":1}}}',
  ],
  [
    'the shipped meter written from memory (hard_stop / warn_threshold)',
    '{"policy_revision":1,"meters":{"requests_per_day":{"meter_kind":"cumulative_budget","unit":"requests","warn_threshold":70000,"hard_stop":90000}}}',
  ],
  // policy_revision must be a non-negative safe integer.
  ['policy_revision as a string', '{"policy_revision":"one","meters":{}}'],
  ['policy_revision as a fraction', '{"policy_revision":1.5,"meters":{}}'],
  ['policy_revision as a boolean', '{"policy_revision":true,"meters":{}}'],
  ['policy_revision as an object', '{"policy_revision":{},"meters":{}}'],
  ['policy_revision as a negative number', '{"policy_revision":-1,"meters":{}}'],
  ['policy_revision past the safe integer range', '{"policy_revision":9007199254740993,"meters":{}}'],
  // Meter id grammar: ids are interpolated into reason codes and shed actions.
  [
    'a meter id that forges the reason-code grammar',
    '{"policy_revision":1,"meters":{"partner_acquisition_cost;budget_hard_stop":{"meter_kind":"cumulative_budget","unit":"usd"}}}',
  ],
  [
    'a meter id carrying a comma',
    '{"policy_revision":1,"meters":{"a,b":{"meter_kind":"cumulative_budget","unit":"usd"}}}',
  ],
  [
    'a meter id carrying a colon',
    '{"policy_revision":1,"meters":{"hard_stop:x":{"meter_kind":"cumulative_budget","unit":"usd"}}}',
  ],
  [
    'a meter id carrying a control character',
    '{"policy_revision":1,"meters":{"a\\nb":{"meter_kind":"cumulative_budget","unit":"usd"}}}',
  ],
  [
    'a meter id that is not lowercase snake case',
    '{"policy_revision":1,"meters":{"RequestsPerDay":{"meter_kind":"cumulative_budget","unit":"usd"}}}',
  ],
  [
    'an oversized meter id',
    `{"policy_revision":1,"meters":{"${'a'.repeat(65)}":{"meter_kind":"cumulative_budget","unit":"usd"}}}`,
  ],
  // Threshold sanity.
  [
    'a negative hard_stop_threshold',
    '{"policy_revision":1,"meters":{"a":{"meter_kind":"cumulative_budget","unit":"usd","hard_stop_threshold":-1}}}',
  ],
  [
    'a warning above its hard stop',
    '{"policy_revision":1,"meters":{"a":{"meter_kind":"cumulative_budget","unit":"usd","warning_threshold":100,"hard_stop_threshold":10}}}',
  ],
]);

test('an unknown or malformed policy key is a configuration error, never a silently disabled control', () => {
  for (const [label, policy] of STRICT_POLICY_REJECTS) {
    assert.throws(() => resolveMeterPolicy({ METER_POLICY_JSON: policy }), `${label} must be a configuration error`);
  }
  // The legal shapes must keep working: strictness that breaks the shipped
  // policy would be a worse bug than the one it closes.
  assert.equal(resolveMeterPolicy({}).policy_revision, 1);
  assert.equal(
    resolveMeterPolicy({ METER_POLICY_JSON: '{"policy_revision":0,"staleness_ms":0,"meters":{}}' }).policy_revision,
    0,
    'revision zero is a legal non-negative integer',
  );
  const full = resolveMeterPolicy({
    METER_POLICY_JSON:
      '{"policy_revision":7,"staleness_ms":900000,"meters":{"requests_per_day":{"meter_kind":"cumulative_budget","unit":"requests","warning_threshold":70000,"hard_stop_threshold":90000}}}',
  });
  assert.equal(full.meters.requests_per_day.hard_stop_threshold, 90000);
});

test('the misspelled-threshold policy is a 503 on the real dispatch path, not a green receipt at 55x the limit', async () => {
  const { restore } = stubGlobalFetch([material.publicJwk]);
  try {
    for (const [label, policy] of STRICT_POLICY_REJECTS) {
      const env = { ACCESS_TEAM_NAME: 'cfg-team', ACCESS_AUD: AUD, METER_POLICY_JSON: policy };
      const token = await signToken(material, {
        nowSec: Math.floor(Date.now() / 1000),
        claims: { iss: `https://${env.ACCESS_TEAM_NAME}.cloudflareaccess.com` },
      });
      const res = await workerEntry.fetch(requestFor('/receipt/limits', token), env);
      assert.equal(res.status, 503, `${label} must be refused before any receipt is settled, got ${res.status}`);
      assert.ok(hasRequiredPrivateHeaders(res), `${label} refusal carries the private header set`);
      assert.equal(
        await res.text(),
        JSON.stringify({ error: 'unavailable', reason: 'worker_unavailable' }),
        `${label} body is the constant refusal`,
      );
    }
  } finally {
    restore();
  }
});

// ---------------------------------------------------------------------------
// Round 5, MF-1 on the REAL dispatch path: the pathological TTL/floor pairing
// is a construction-time refusal, so a private route is a 503 and /healthz says
// invalid. It is never a run of 401s against a key set that answered 200.
// ---------------------------------------------------------------------------

const TTL_FLOOR_ENV = Object.freeze({
  ACCESS_TEAM_NAME: 'ttl-floor-team',
  ACCESS_AUD: AUD,
  JWKS_CACHE_TTL_MS: '30000',
  JWKS_MIN_REFETCH_MS: '60000',
});

test('a TTL shorter than the refetch floor refuses at deploy time instead of denying live traffic', async () => {
  const { state, restore } = stubGlobalFetch([material.publicJwk]);
  try {
    const token = await signToken(material, {
      nowSec: Math.floor(Date.now() / 1000),
      claims: { iss: `https://${TTL_FLOOR_ENV.ACCESS_TEAM_NAME}.cloudflareaccess.com` },
    });
    for (const [routeName, path] of PRIVATE_ROUTES) {
      const res = await workerEntry.fetch(requestFor(path, token), TTL_FLOOR_ENV);
      assert.equal(res.status, 503, `${routeName} must be a configuration refusal, got ${res.status}`);
      assert.notEqual(res.status, 401, `${routeName} must NOT be an authentication denial against a healthy key set`);
      assert.ok(hasRequiredPrivateHeaders(res), `${routeName} refusal carries the private header set`);
      assert.equal(
        await res.text(),
        JSON.stringify({ error: 'unavailable', reason: 'worker_unavailable' }),
        `${routeName} body is the constant refusal, naming no variable`,
      );
    }
    assert.equal(state.calls, 0, 'a refused configuration never reaches the key set');

    const health = await workerEntry.fetch(requestFor('/healthz', null), TTL_FLOOR_ENV);
    assert.equal(health.status, 503);
    assert.deepEqual(JSON.parse(await health.text()), { status: 'degraded', config: 'invalid' });
  } finally {
    restore();
  }
});

test('a valid TTL and floor ordering still serves, so the refusal is the failure path and not the default', async () => {
  const { restore } = stubGlobalFetch([material.publicJwk]);
  try {
    for (const [ttl, floor] of [['600000', '60000'], ['60000', '60000']]) {
      const env = { ACCESS_TEAM_NAME: 'ttl-ok-team', ACCESS_AUD: AUD, JWKS_CACHE_TTL_MS: ttl, JWKS_MIN_REFETCH_MS: floor };
      const res = await workerEntry.fetch(requestFor('/healthz', null), env);
      assert.equal(res.status, 200, `ttl ${ttl} against floor ${floor} must remain a working configuration`);
      assert.equal(await res.text(), 'ok');
    }
  } finally {
    restore();
  }
});

// ---------------------------------------------------------------------------
// Round 5, Codex 2: numeric configuration is PARSED, not coerced. Number(" ")
// is 0, which silently set the refetch floor to zero and disabled the control
// that bounds outbound amplification. Booleans, arrays and alternate numeric
// grammars were accepted the same way.
// ---------------------------------------------------------------------------

const BAD_NUMERIC_SETTINGS = Object.freeze([
  ['a whitespace-only string', ' '],
  ['a tab', '\t'],
  ['a boolean', true],
  ['a boolean false', false],
  ['an array', [1]],
  ['an empty array', []],
  ['a hexadecimal string', '0x10'],
  ['an exponent string', '1e3'],
  ['a signed string', '+60000'],
  ['a negative string', '-60000'],
  ['a leading-dot string', '.5'],
  ['a trailing-space string', '60000 '],
  ['the string Infinity', 'Infinity'],
  ['an object', {}],
  ['a word', 'often'],
]);

test('a numeric setting that is not a number is a configuration error, never a silent coercion', () => {
  for (const [label, raw] of BAD_NUMERIC_SETTINGS) {
    assert.throws(
      () => numericSetting({ JWKS_MIN_REFETCH_MS: raw }, 'JWKS_MIN_REFETCH_MS', 60_000),
      /JWKS_MIN_REFETCH_MS must be a finite number >= 0/,
      `${label} must be refused, not coerced`,
    );
  }
  // Absent still means the default, and the legal shapes still parse.
  assert.equal(numericSetting({}, 'JWKS_MIN_REFETCH_MS', 60_000), 60_000, 'absent means the default');
  assert.equal(numericSetting({ JWKS_MIN_REFETCH_MS: '' }, 'JWKS_MIN_REFETCH_MS', 60_000), 60_000, 'empty means the default');
  assert.equal(numericSetting({ JWKS_MIN_REFETCH_MS: null }, 'JWKS_MIN_REFETCH_MS', 60_000), 60_000);
  assert.equal(numericSetting({ JWKS_MIN_REFETCH_MS: '60000' }, 'JWKS_MIN_REFETCH_MS', 1), 60_000, 'a plain decimal string parses');
  assert.equal(numericSetting({ JWKS_MIN_REFETCH_MS: '0' }, 'JWKS_MIN_REFETCH_MS', 1), 0, 'an explicit zero is honored');
  assert.equal(numericSetting({ JWKS_MIN_REFETCH_MS: '1.5' }, 'JWKS_MIN_REFETCH_MS', 1), 1.5, 'a decimal fraction parses');
  assert.equal(numericSetting({ JWKS_MIN_REFETCH_MS: 60_000 }, 'JWKS_MIN_REFETCH_MS', 1), 60_000, 'a real number parses');
});

test('a whitespace or non-numeric tuning value reports invalid on /healthz, it does not disable the control', async () => {
  const cases = [
    ['whitespace JWKS_MIN_REFETCH_MS', { JWKS_MIN_REFETCH_MS: ' ' }],
    ['boolean JWKS_CACHE_TTL_MS', { JWKS_CACHE_TTL_MS: true }],
    ['array JWKS_STALE_GRACE_MS', { JWKS_STALE_GRACE_MS: [1] }],
    ['hexadecimal ACCESS_CLOCK_SKEW_SEC', { ACCESS_CLOCK_SKEW_SEC: '0x10' }],
    ['whitespace JWKS_FETCH_TIMEOUT_MS', { JWKS_FETCH_TIMEOUT_MS: ' ' }],
  ];
  for (const [label, extra] of cases) {
    const env = { ACCESS_TEAM_NAME: 'numeric-team', ACCESS_AUD: AUD, ...extra };
    const health = await workerEntry.fetch(requestFor('/healthz', null), env);
    assert.equal(health.status, 503, `${label}: health must report the outage`);
    assert.deepEqual(JSON.parse(await health.text()), { status: 'degraded', config: 'invalid' }, `${label}: health says invalid`);
    const priv = await workerEntry.fetch(requestFor('/app/today', null), env);
    assert.equal(priv.status, 503, `${label}: a private route is refused, never served`);
    assert.ok(hasRequiredPrivateHeaders(priv), `${label}: the refusal carries the private header set`);
  }
});

// ---------------------------------------------------------------------------
// Round 5, S2: the memoized env policy is shared by every request in the
// isolate, so it is frozen all the way down before it is cached.
// ---------------------------------------------------------------------------

test('the memoized policy is deep-frozen, so one stray write cannot re-tune every later request', () => {
  const json = '{"policy_revision":9,"meters":{"requests_per_day":{"meter_kind":"cumulative_budget","unit":"requests","hard_stop_threshold":90000}}}';
  const p = resolveMeterPolicy({ METER_POLICY_JSON: json });
  assert.ok(Object.isFrozen(p), 'the policy object is frozen');
  assert.ok(Object.isFrozen(p.meters), 'the meters map is frozen');
  assert.ok(Object.isFrozen(p.meters.requests_per_day), 'each meter spec is frozen');

  assert.throws(() => { p.meters.requests_per_day.hard_stop_threshold = 999999; }, TypeError, 'raising a hard stop must throw');
  assert.throws(() => { p.policy_revision = 0; }, TypeError, 'rewriting the audit join key must throw');
  assert.throws(() => { p.meters.smuggled = { meter_kind: 'cumulative_budget', unit: 'u' }; }, TypeError, 'adding a meter must throw');

  const again = resolveMeterPolicy({ METER_POLICY_JSON: json });
  assert.equal(again.meters.requests_per_day.hard_stop_threshold, 90000, 'the memoized policy is unchanged');
});

// ---------------------------------------------------------------------------
// Round 5, S3: staleness_ms has an upper sanity bound. Round 4 refused a
// misspelled threshold KEY; the same defect class lived on in the VALUE, where
// three extra zeros turned 15 minutes into 10.4 days.
// ---------------------------------------------------------------------------

test('a freshness window wider than a day is a configuration error, not a tuning choice', () => {
  for (const bad of [86_400_001, 900_000_000, 1e15, Number.MAX_SAFE_INTEGER]) {
    assert.throws(
      () => resolveMeterPolicy({ METER_POLICY_JSON: `{"policy_revision":1,"staleness_ms":${bad},"meters":{}}` }),
      /staleness_ms must be <= 86400000/,
      `staleness_ms ${bad} must be refused`,
    );
  }
  // The bound is generous on purpose: everything a real deployment would set
  // still parses, including the exact cap.
  for (const ok of [0, 900_000, 3_600_000, 86_400_000]) {
    assert.equal(
      resolveMeterPolicy({ METER_POLICY_JSON: `{"policy_revision":1,"staleness_ms":${ok},"meters":{}}` }).staleness_ms,
      ok,
      `staleness_ms ${ok} must remain legal`,
    );
  }
});

// ---------------------------------------------------------------------------
// Round 5, Codex 1 on the REAL dispatch path: -1 on every meter.
// ---------------------------------------------------------------------------

test('a receipt built from impossible readings is unknown on the real route, never a settled green', async () => {
  const worker = createWorker({
    verifier: { verify: async () => ({ ok: true, claims: { sub: 'synthetic-subject', email: 'synthetic.user@synthetic.invalid' } }) },
    now: () => FIXED_NOW_MS,
    // Every meter in the default policy reads -1: the shape a broken meter
    // source produces, and the one that used to settle green because -1 is
    // below every threshold.
    readMeters: async () => {
      const out = {};
      for (const name of Object.keys(DEFAULT_METER_POLICY.meters)) {
        out[name] = { value: -1, sampled_at: FIXED_NOW_MS };
      }
      return out;
    },
  });
  const res = await worker.fetch(requestFor('/receipt/limits', 'any-token'));
  assert.equal(res.status, 200, 'the receipt is still published: losing it would lose the record that we could not read');
  const receipt = JSON.parse(await res.text());
  assert.equal(receipt.final_state, 'unknown', 'an impossible reading can never settle the receipt');
  assert.equal(receipt.envelope.state, 'unknown', 'the envelope is not settled either');
  assert.equal(receipt.envelope.settled_at, null, 'no settled timestamp on a receipt that measured nothing');
  assert.ok(receipt.envelope.reason_code.startsWith('meter_stale:'), 'the reason code names the unread meters');
  for (const row of receipt.readings) {
    assert.equal(row.value, null, `${row.meter} must not publish -1`);
    assert.equal(row.freshness_verdict, 'unknown', `${row.meter} is unread, not fresh`);
    assert.equal(row.breached, false);
  }
  assert.equal(receipt.readings.length, Object.keys(DEFAULT_METER_POLICY.meters).length, 'every meter is still named');
});

// ---------------------------------------------------------------------------
// Round 6, MF-1 on the REAL dispatch path. The exact reported scenario: one env
// var changed from its shipped default, plus a correctly signed token that
// expired an hour ago. Before the ceiling this returned HTTP 200 and served the
// private projection. It is now a construction-time 503 on every private route,
// and /healthz says the configuration is invalid.
// ---------------------------------------------------------------------------

const OVERWIDE_SKEW_ENV = Object.freeze({
  ACCESS_TEAM_NAME: 'skew-team',
  ACCESS_AUD: AUD,
  ACCESS_CLOCK_SKEW_SEC: '30000', // three extra zeros on the default 30
});

test('an over-wide clock skew refuses at deploy time instead of serving a token that expired an hour ago', async () => {
  const { state, restore } = stubGlobalFetch([material.publicJwk]);
  try {
    const realNowSec = Math.floor(Date.now() / 1000);
    const expiredToken = await signToken(material, {
      nowSec: realNowSec,
      claims: {
        iss: `https://${OVERWIDE_SKEW_ENV.ACCESS_TEAM_NAME}.cloudflareaccess.com`,
        exp: realNowSec - 3600,
        nbf: realNowSec - 7200,
      },
    });
    for (const [routeName, path] of PRIVATE_ROUTES) {
      const res = await workerEntry.fetch(requestFor(path, expiredToken), OVERWIDE_SKEW_ENV);
      assert.equal(res.status, 503, `${routeName} must be a configuration refusal, got ${res.status}`);
      assert.notEqual(res.status, 200, `${routeName} must NEVER serve a private response to an expired token`);
      assert.ok(hasRequiredPrivateHeaders(res), `${routeName} refusal carries the private header set`);
    }
    const health = await workerEntry.fetch(requestFor('/healthz', null), OVERWIDE_SKEW_ENV);
    assert.equal(health.status, 503);
    assert.equal(await health.text(), JSON.stringify({ status: 'degraded', config: 'invalid' }));
    assert.equal(state.calls, 0, 'a refused configuration never reached the key set');
  } finally {
    restore();
  }
});

test('the same route under the shipped default skew serves a valid token and denies the expired one', async () => {
  const { restore } = stubGlobalFetch([material.publicJwk]);
  try {
    const env = { ACCESS_TEAM_NAME: 'skew-team', ACCESS_AUD: AUD };
    const realNowSec = Math.floor(Date.now() / 1000);
    const iss = 'https://skew-team.cloudflareaccess.com';
    const good = await signToken(material, { nowSec: realNowSec, claims: { iss } });
    const expired = await signToken(material, {
      nowSec: realNowSec,
      claims: { iss, exp: realNowSec - 3600, nbf: realNowSec - 7200 },
    });
    const okRes = await workerEntry.fetch(requestFor('/app/today', good), env);
    assert.equal(okRes.status, 200, 'the default configuration is healthy: the ceiling refuses only the typo');
    const denied = await workerEntry.fetch(requestFor('/app/today', expired), env);
    assert.equal(denied.status, 401);
    assert.equal(await denied.text(), JSON.stringify({ error: 'unauthorized', reason: 'expired' }));
  } finally {
    restore();
  }
});

// ---------------------------------------------------------------------------
// Round 6, MF-2 on the REAL dispatch path: a team name that is not one DNS
// label redirects the trust root (key set host AND the iss a token must match),
// so it is refused before any URL is built. A shipped placeholder is refused
// for both required identifiers.
// ---------------------------------------------------------------------------

test('a team name that redirects the trust root refuses at deploy time', async () => {
  const { state, restore } = stubGlobalFetch([material.publicJwk]);
  try {
    for (const teamName of ['attacker.example/path', 'evil.com#', 'a b', 'REPLACE_WITH_TEAM_NAME']) {
      const env = { ACCESS_TEAM_NAME: teamName, ACCESS_AUD: AUD };
      const res = await workerEntry.fetch(requestFor('/app/today', 'any-token'), env);
      assert.equal(res.status, 503, `teamName ${JSON.stringify(teamName)} must refuse the private route`);
      assert.ok(hasRequiredPrivateHeaders(res));
      const health = await workerEntry.fetch(requestFor('/healthz', null), env);
      assert.equal(health.status, 503, `teamName ${JSON.stringify(teamName)} must report /healthz degraded`);
      assert.equal(await health.text(), JSON.stringify({ status: 'degraded', config: 'invalid' }));
    }
    assert.equal(state.calls, 0, 'no refused configuration reached any key set host');
  } finally {
    restore();
  }
});

test('an unfilled ACCESS_AUD placeholder is a configuration refusal, not a run of denials', async () => {
  const { restore } = stubGlobalFetch([material.publicJwk]);
  try {
    const env = { ACCESS_TEAM_NAME: 'example-team', ACCESS_AUD: 'REPLACE_WITH_APPLICATION_AUDIENCE_TAG' };
    const res = await workerEntry.fetch(requestFor('/app/today', 'any-token'), env);
    assert.equal(res.status, 503);
    const health = await workerEntry.fetch(requestFor('/healthz', null), env);
    assert.equal(health.status, 503);
    assert.equal(await health.text(), JSON.stringify({ status: 'degraded', config: 'invalid' }));
  } finally {
    restore();
  }
});

// ---------------------------------------------------------------------------
// Round 6, Codex round-5 should-fix taken: meter_source is a PROVENANCE claim.
// A reader that returned null, an array or a string produced no readings, and
// the receipt must not attest to a source that gave us nothing.
// ---------------------------------------------------------------------------

test('a meter reader that returns a non-object is unavailable, never labeled with the reader name', async () => {
  for (const bad of [null, undefined, [{ value: 1 }], 'nope', 42, true, new Response('{}'), new Map(), new Date(0)]) {
    const worker = createWorker({
      verifier: { verify: async () => ({ ok: true, claims: { sub: 'synthetic-subject', email: 'synthetic.user@synthetic.invalid' } }) },
      now: () => FIXED_NOW_MS,
      readMeters: async () => bad,
    });
    const res = await worker.fetch(requestFor('/receipt/limits', 'any-token'));
    assert.equal(res.status, 200, 'the receipt is still published: it is the record that we could not read');
    const receipt = JSON.parse(await res.text());
    assert.equal(
      receipt.meter_source,
      'unavailable',
      `readMeters returning ${JSON.stringify(bad) ?? 'undefined'} must not claim the reader as provenance`,
    );
    assert.equal(receipt.final_state, 'unknown', 'nothing was read, so nothing settles');
  }
});

test('a meter reader that returns a real readings object still names its provenance', async () => {
  const worker = createWorker({
    verifier: { verify: async () => ({ ok: true, claims: { sub: 'synthetic-subject', email: 'synthetic.user@synthetic.invalid' } }) },
    now: () => FIXED_NOW_MS,
    readMeters: async () => ({ requests_per_day: { value: 12, sampled_at: FIXED_NOW_MS } }),
  });
  const res = await worker.fetch(requestFor('/receipt/limits', 'any-token'));
  const receipt = JSON.parse(await res.text());
  assert.equal(receipt.meter_source, 'host_analytics_api');
});
