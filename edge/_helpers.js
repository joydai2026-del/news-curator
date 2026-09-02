// Test helpers. Not a test file: the filename is deliberately outside every
// pattern node --test collects.

import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

export const TEAM_NAME = 'example-team';
export const ISSUER = `https://${TEAM_NAME}.cloudflareaccess.com`;
export const JWKS_URL = `${ISSUER}/cdn-cgi/access/certs`;
export const AUD = 'a'.repeat(64);
export const KID = 'test-kid-1';

function b64url(bytes) {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function seg(obj) {
  return b64url(new TextEncoder().encode(JSON.stringify(obj)));
}

/** Generate a throwaway RSA key pair and the JWKS a team domain would serve. */
export async function makeKeyMaterial(kid = KID) {
  const pair = await crypto.subtle.generateKey(
    { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
    true,
    ['sign', 'verify'],
  );
  const jwk = await crypto.subtle.exportKey('jwk', pair.publicKey);
  const publicJwk = { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: 'RS256', use: 'sig', kid };
  return { privateKey: pair.privateKey, publicJwk, kid };
}

/** An injected fetch that serves one key set and counts how often it was called. */
export function makeJwksFetch(keys) {
  const state = { calls: 0, keys };
  const fetchImpl = async (url) => {
    assert.equal(url, JWKS_URL, 'verifier must fetch the team key set URL');
    state.calls += 1;
    return {
      ok: true,
      status: 200,
      json: async () => ({ keys: state.keys }),
    };
  };
  return { fetchImpl, state };
}

/**
 * Sign a token. Every field is overridable so a test can break exactly one
 * thing and leave the rest valid.
 */
export async function signToken(material, overrides = {}) {
  const nowSec = overrides.nowSec === undefined ? Math.floor(Date.now() / 1000) : overrides.nowSec;
  const header = { alg: 'RS256', kid: material.kid, typ: 'JWT', ...(overrides.header || {}) };
  const claims = {
    iss: ISSUER,
    aud: AUD,
    exp: nowSec + 600,
    nbf: nowSec - 10,
    iat: nowSec,
    sub: 'synthetic-subject',
    email: 'synthetic.user@synthetic.invalid',
    ...(overrides.claims || {}),
  };
  const h = seg(header);
  const p = seg(claims);
  const signing = new TextEncoder().encode(`${h}.${p}`);
  const sig = new Uint8Array(await crypto.subtle.sign({ name: 'RSASSA-PKCS1-v1_5' }, material.privateKey, signing));
  return `${h}.${p}.${b64url(sig)}`;
}

/** An unsigned token, the classic alg:none forgery. */
export function unsignedToken(claims) {
  return `${seg({ alg: 'none', kid: KID, typ: 'JWT' })}.${seg(claims)}.`;
}

export function tamperSignature(token) {
  // Flip a leading character, not a trailing one: the last base64url character
  // of a 256-byte signature carries padding bits, so changing it can decode to
  // the identical byte string and quietly not tamper with anything.
  const parts = token.split('.');
  const sig = parts[2];
  const first = sig.slice(0, 1);
  parts[2] = (first === 'A' ? 'B' : 'A') + sig.slice(1);
  return parts.join('.');
}

export function requestFor(path, token) {
  const headers = token ? { 'Cf-Access-Jwt-Assertion': token } : {};
  return new Request(`https://private.example.invalid${path}`, { headers });
}

/** Route classes a signed-out caller must never get content from. */
export const PRIVATE_ROUTES = Object.freeze([
  ['html_app', '/'],
  ['html_app_sub', '/app/today'],
  ['api_record', '/api/records/1'],
  ['source_map', '/static/bundle.js.map'],
  ['projection', '/projection/synthetic'],
  ['limit_receipt', '/receipt/limits'],
  ['unlisted', '/whatever/else'],
]);

/**
 * THE core assertion of this whole slice, factored out so the positive control
 * can run the very same code against a deliberately broken verifier and prove
 * the suite is capable of going red.
 */
export async function assertSignedOutDenied(worker) {
  for (const [name, path] of PRIVATE_ROUTES) {
    const res = await worker.fetch(requestFor(path, null));
    assert.ok(
      res.status === 401 || res.status === 403,
      `${name} (${path}) must deny a signed-out request, got ${res.status}`,
    );
    assert.equal(res.headers.get('cache-control'), 'private, no-store', `${name} denial must not be cacheable`);
  }
}

/**
 * The second core assertion: a token the verifier should refuse must not open
 * any private route. Shared with the positive control for the same reason.
 */
export async function assertForgedTokensDenied(worker, tokens) {
  for (const [name, token] of tokens) {
    for (const [, path] of PRIVATE_ROUTES) {
      const res = await worker.fetch(requestFor(path, token));
      assert.ok(res.status === 401 || res.status === 403, `${name} on ${path} must be denied, got ${res.status}`);
    }
  }
}

// ---------------------------------------------------------------------------
// The committed receipt fixture's INPUTS.
//
// It lives here, not in meter.test.js, so the test and the regeneration script
// (edge/scripts/regenerate-fixtures.mjs) build the sample from ONE definition.
// The test never writes the file: a fixture the suite can rewrite is a fixture
// that stops guarding the moment it goes missing.
// ---------------------------------------------------------------------------

import { buildLimitReceipt, CUMULATIVE, PER_INVOCATION, PER_ISOLATE } from './meter.js';

export const SAMPLE_NOW = 1_756_000_000_000;

export const SAMPLE_FIXTURE_PATH = fileURLToPath(new URL('./fixtures/limit-receipt.sample.json', import.meta.url));

/** Fixed inputs. Nothing here reads the clock, a random source, or the network. */
export function buildSampleReceipt() {
  return buildLimitReceipt({
    policy: {
      policy_revision: 7,
      staleness_ms: 900_000,
      meters: {
        requests_per_day: { meter_kind: CUMULATIVE, unit: 'requests', warning_threshold: 70000, hard_stop_threshold: 90000 },
        cpu_per_invocation: { meter_kind: PER_INVOCATION, unit: 'ms', warning_threshold: 8, hard_stop_threshold: 10 },
        memory_failures: { meter_kind: PER_ISOLATE, unit: 'events', warning_threshold: 1, hard_stop_threshold: 1 },
      },
    },
    readings: {
      requests_per_day: { value: 71000, sampled_at: SAMPLE_NOW - 60_000 },
      cpu_per_invocation: { value: 3, sampled_at: SAMPLE_NOW - 1000 },
      // memory_failures is deliberately absent: the sample has to pin the
      // unknown shape too, because null-not-zero is the whole point of the file.
    },
    meterSource: 'host_analytics_api',
    now: SAMPLE_NOW,
    receiptId: 'lrec-fixture-0001',
    attributedOperationClass: 'edge_request',
    tenantId: 'tenant-fixture',
    actorId: 'actor-fixture',
  });
}
