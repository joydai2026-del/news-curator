import test from 'node:test';
import assert from 'node:assert/strict';

import { createAccessVerifier, readAccessToken } from './access.js';
import { AUD, ISSUER, KID, makeJwksFetch, makeKeyMaterial, signToken, tamperSignature, unsignedToken } from './_helpers.js';

const FIXED_NOW_MS = 1_756_000_000_000;
const nowSec = Math.floor(FIXED_NOW_MS / 1000);

const material = await makeKeyMaterial();

function verifierWith(fetchImpl, nowMs = FIXED_NOW_MS, extra = {}) {
  return createAccessVerifier({
    teamName: 'example-team',
    aud: AUD,
    fetchImpl,
    now: typeof nowMs === 'function' ? nowMs : () => nowMs,
    ...extra,
  });
}

test('a valid token is accepted and its claims are returned', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  const token = await signToken(material, { nowSec });
  const res = await v.verify(token);
  assert.equal(res.ok, true);
  assert.equal(res.claims.iss, ISSUER);
});

test('the verifier derives the documented issuer and key set URL', () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  assert.equal(v.issuer, 'https://example-team.cloudflareaccess.com');
  assert.equal(v.jwksUrl, 'https://example-team.cloudflareaccess.com/cdn-cgi/access/certs');
});

const rejections = [
  ['wrong aud', { claims: { aud: 'b'.repeat(64) } }, 'bad_aud'],
  ['wrong iss', { claims: { iss: 'https://attacker.cloudflareaccess.com' } }, 'bad_iss'],
  ['expired', { claims: { exp: nowSec - 3600, nbf: nowSec - 7200 } }, 'expired'],
  ['not yet valid', { claims: { nbf: nowSec + 3600 } }, 'not_yet_valid'],
  ['unknown kid', { header: { kid: 'rotated-away' } }, 'unknown_kid'],
  ['alg HS256', { header: { alg: 'HS256' } }, 'bad_alg'],
];

for (const [name, overrides, expectedReason] of rejections) {
  test(`rejects a token with ${name}`, async () => {
    const { fetchImpl } = makeJwksFetch([material.publicJwk]);
    const v = verifierWith(fetchImpl);
    const token = await signToken(material, { nowSec, ...overrides });
    const res = await v.verify(token);
    assert.equal(res.ok, false, `${name} must be rejected`);
    assert.equal(res.reason, expectedReason);
  });
}

test('rejects an alg:none token even when every claim is correct', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  const token = unsignedToken({ iss: ISSUER, aud: AUD, exp: nowSec + 600, nbf: nowSec - 10, sub: 's' });
  const res = await v.verify(token);
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'bad_alg');
});

test('rejects a tampered signature', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  const token = tamperSignature(await signToken(material, { nowSec }));
  const res = await v.verify(token);
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'bad_signature');
});

test('rejects a token whose payload was swapped under a valid signature', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  const good = await signToken(material, { nowSec });
  const forged = await signToken(material, { nowSec, claims: { aud: 'b'.repeat(64) } });
  const spliced = `${forged.split('.')[0]}.${forged.split('.')[1]}.${good.split('.')[2]}`;
  const res = await v.verify(spliced);
  assert.equal(res.ok, false);
  // aud is checked before the signature, so the coarse reason is bad_aud. The
  // point is that it is refused, not which gate caught it first.
  assert.ok(['bad_aud', 'bad_signature'].includes(res.reason));
});

test('rejects malformed and empty tokens', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  assert.equal((await v.verify('')).reason, 'missing_token');
  assert.equal((await v.verify('not-a-jwt')).reason, 'malformed_token');
  assert.equal((await v.verify('a.b.c')).reason, 'malformed_token');
});

test('the key set cache honors its TTL and refetches after it expires', async () => {
  const { fetchImpl, state } = makeJwksFetch([material.publicJwk]);
  let clock = FIXED_NOW_MS;
  const v = verifierWith(fetchImpl, () => clock, { jwksTtlMs: 60_000 });
  const token = await signToken(material, { nowSec });

  assert.equal((await v.verify(token)).ok, true);
  assert.equal(state.calls, 1, 'first verify fetches the key set');

  clock += 30_000;
  assert.equal((await v.verify(token)).ok, true);
  assert.equal(state.calls, 1, 'inside the TTL the cached key set is reused');

  clock += 40_000; // now 70s past the first fetch, TTL is 60s
  assert.equal((await v.verify(token)).ok, true);
  assert.equal(state.calls, 2, 'past the TTL the key set is refetched');
});

test('an unknown kid forces at most one extra refetch, then denies', async () => {
  const { fetchImpl, state } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, { jwksTtlMs: 600_000, jwksMinRefetchMs: 0 });
  const token = await signToken(material, { nowSec, header: { kid: 'never-issued' } });
  const res = await v.verify(token);
  assert.equal(res.reason, 'unknown_kid');
  assert.equal(state.calls, 2, 'one initial fetch plus one forced refetch, then it gives up');
});

test('the refetch floor stops an unknown kid from hammering the key set', async () => {
  const { fetchImpl, state } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, { jwksTtlMs: 600_000, jwksMinRefetchMs: 60_000 });
  const token = await signToken(material, { nowSec, header: { kid: 'never-issued' } });
  for (let i = 0; i < 5; i += 1) {
    assert.equal((await v.verify(token)).reason, 'unknown_kid');
  }
  assert.equal(state.calls, 1, 'inside the refetch floor the key set is fetched once, and the token is still denied');
});

test('readAccessToken prefers the header and falls back to the cookie', () => {
  const withHeader = new Request('https://x.invalid/', {
    headers: { 'Cf-Access-Jwt-Assertion': 'from-header', Cookie: 'CF_Authorization=from-cookie' },
  });
  assert.equal(readAccessToken(withHeader), 'from-header');
  const withCookie = new Request('https://x.invalid/', { headers: { Cookie: 'other=1; CF_Authorization=from-cookie' } });
  assert.equal(readAccessToken(withCookie), 'from-cookie');
  assert.equal(readAccessToken(new Request('https://x.invalid/')), null);
});

test('a misconfigured verifier refuses to construct', () => {
  assert.throws(() => createAccessVerifier({ teamName: '', aud: AUD, fetchImpl: async () => {} }));
  assert.throws(() => createAccessVerifier({ teamName: 'x', aud: '', fetchImpl: async () => {} }));
});

test('KID constant is what the helper signs with', async () => {
  assert.equal(material.kid, KID);
});

test('a non-finite exp is refused: 1e999 parses to Infinity and is not an expiry', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  // Hand-built: JSON.stringify would turn Infinity into null, so the raw literal
  // has to be spliced into the payload segment the way an attacker would.
  const good = await signToken(material, { nowSec });
  const [h, , sig] = good.split('.');
  const payload = JSON.stringify({ iss: ISSUER, aud: AUD, nbf: nowSec - 10, sub: 's', exp: 0 }).replace('"exp":0', '"exp":1e999');
  const b64 = Buffer.from(payload, 'utf8').toString('base64url');
  const res = await v.verify(`${h}.${b64}.${sig}`);
  assert.equal(res.ok, false, 'exp: 1e999 must not read as a token that never expires');
  assert.equal(res.reason, 'bad_exp');
});

test('a string exp is refused rather than coerced', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  const token = await signToken(material, { nowSec, claims: { exp: '9999999999' } });
  const res = await v.verify(token);
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'missing_exp');
});

test('a present but non-finite nbf is refused', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  const token = await signToken(material, { nowSec, claims: { nbf: '0' } });
  const res = await v.verify(token);
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'bad_nbf');
});

test('an array aud containing the configured tag is accepted', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  const token = await signToken(material, { nowSec, claims: { aud: ['b'.repeat(64), AUD] } });
  assert.equal((await v.verify(token)).ok, true);
});

test('an array aud without the configured tag is denied', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl);
  const token = await signToken(material, { nowSec, claims: { aud: ['b'.repeat(64), 'c'.repeat(64)] } });
  const res = await v.verify(token);
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'bad_aud');
});

test('a key set that answers 503 denies, and never throws out of verify', async () => {
  const v = verifierWith(async () => ({ ok: false, status: 503, json: async () => ({}) }));
  const token = await signToken(material, { nowSec });
  const res = await v.verify(token);
  assert.equal(res.ok, false, 'an unreachable key set fails closed');
  assert.equal(res.reason, 'key_set_unavailable');
});

test('a malformed key set body denies rather than caching an empty key set', async () => {
  const v = verifierWith(async () => ({
    ok: true,
    status: 200,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON');
    },
  }));
  const token = await signToken(material, { nowSec });
  const res = await v.verify(token);
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'key_set_unavailable');
});

test('a key set without a keys array denies', async () => {
  const v = verifierWith(async () => ({ ok: true, status: 200, json: async () => ({ public_cert: 'x' }) }));
  const res = await v.verify(await signToken(material, { nowSec }));
  assert.equal(res.reason, 'key_set_unavailable');
});

test('a refresh failure on an unknown kid denies instead of throwing', async () => {
  // The first fetch succeeds, the forced refetch for the unknown kid fails.
  let calls = 0;
  const fetchImpl = async () => {
    calls += 1;
    if (calls === 1) return { ok: true, status: 200, json: async () => ({ keys: [material.publicJwk] }) };
    throw new Error('network down on refresh');
  };
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, { jwksTtlMs: 600_000, jwksMinRefetchMs: 0 });
  const token = await signToken(material, { nowSec, header: { kid: 'never-issued' } });
  const res = await v.verify(token);
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'key_set_unavailable');
  assert.equal(calls, 2);
});

// ---------------------------------------------------------------------------
// Round 2, item 6: the key set outage window is configuration, and it fails
// closed by default.
//
// A key set the verifier can no longer confirm is a key set it must stop
// trusting. The grace exists so an operator can trade that for availability
// ON PURPOSE, bounded and written down, rather than by discovering the default.
// ---------------------------------------------------------------------------

/** A key set fetch that serves the real keys until it is switched to failing. */
function makeFlakyJwksFetch(keys) {
  const state = { calls: 0, fail: false };
  const fetchImpl = async () => {
    state.calls += 1;
    if (state.fail) throw new Error('key set unreachable');
    return { ok: true, status: 200, json: async () => ({ keys }) };
  };
  return { fetchImpl, state };
}

test('the default stale grace is zero: an outage past the TTL denies rather than trusting an unconfirmable key set', async () => {
  const { fetchImpl, state } = makeFlakyJwksFetch([material.publicJwk]);
  let clock = FIXED_NOW_MS;
  const v = verifierWith(fetchImpl, () => clock, { jwksTtlMs: 60_000 });
  const token = await signToken(material, { nowSec, claims: { exp: nowSec + 86_400 } });

  assert.equal((await v.verify(token)).ok, true, 'warm the cache while the key set is reachable');
  assert.equal(state.calls, 1);

  state.fail = true;
  clock += 61_000; // past the TTL, so the cache can no longer answer on its own
  const denied = await v.verify(token);
  assert.equal(denied.ok, false);
  assert.equal(denied.reason, 'key_set_unavailable', 'grace 0 denies on an outage, it does not fail open');
});

test('a positive stale grace serves the last confirmed key set inside the window, and denies after it', async () => {
  const { fetchImpl, state } = makeFlakyJwksFetch([material.publicJwk]);
  let clock = FIXED_NOW_MS;
  const v = verifierWith(fetchImpl, () => clock, { jwksTtlMs: 60_000, jwksStaleGraceMs: 120_000 });
  const token = await signToken(material, { nowSec, claims: { exp: nowSec + 86_400 } });

  assert.equal((await v.verify(token)).ok, true, 'warm the cache while the key set is reachable');
  state.fail = true;

  clock += 90_000; // 30s past the TTL, well inside the 120s grace
  const inside = await v.verify(token);
  assert.equal(inside.ok, true, 'inside the configured grace the last confirmed key set still verifies');

  clock += 120_000; // now 210s past the fetch: TTL 60s + grace 120s = 180s
  const outside = await v.verify(token);
  assert.equal(outside.ok, false, 'past the grace the verifier stops trusting the stale key set');
  assert.equal(outside.reason, 'key_set_unavailable');
});

// ---------------------------------------------------------------------------
// Round 3, item 1: the refetch floor must hold DURING an outage.
//
// The floor used to be measured from the last SUCCESS. During an outage inside
// the stale grace there is no success to measure from, so every request tried
// again, and an unknown kid forced a second try in the same request: an outage
// turned each inbound request into two outbound ones, against the 50-subrequest
// per-invocation ceiling this whole architecture exists to respect. It is now
// measured from the last ATTEMPT.
// ---------------------------------------------------------------------------

/** A key set fetch that can be switched between two key sets and to failing. */
function makeRotatingJwksFetch(keys) {
  const state = { calls: 0, fail: false, keys };
  const fetchImpl = async () => {
    state.calls += 1;
    if (state.fail) throw new Error('key set unreachable');
    return { ok: true, status: 200, json: async () => ({ keys: state.keys }) };
  };
  return { fetchImpl, state };
}

test('during an outage inside the stale grace, the refetch floor bounds attempts to one per window', async () => {
  const other = await makeKeyMaterial('rotated-in-kid');
  const { fetchImpl, state } = makeRotatingJwksFetch([material.publicJwk]);
  let clock = FIXED_NOW_MS;
  const v = verifierWith(fetchImpl, () => clock, {
    jwksTtlMs: 60_000,
    jwksMinRefetchMs: 60_000,
    jwksStaleGraceMs: 120_000,
  });
  const oldKeyToken = await signToken(material, { nowSec, claims: { exp: nowSec + 86_400 } });
  const newKeyToken = await signToken(other, { nowSec, claims: { exp: nowSec + 86_400 } });

  assert.equal((await v.verify(oldKeyToken)).ok, true, 'warm the cache while the key set is reachable');
  assert.equal(state.calls, 1);

  state.fail = true;
  clock += 61_000; // past the 60s TTL, inside the 120s grace

  // Two requests on the cached key, then two on a kid the cache does not carry.
  // The unknown kid is the amplification path: it forces a refetch, and that
  // forced refetch must respect the floor too.
  assert.equal((await v.verify(oldKeyToken)).ok, true, 'inside the grace the last confirmed key set still verifies');
  assert.equal((await v.verify(oldKeyToken)).ok, true);
  assert.equal((await v.verify(newKeyToken)).reason, 'unknown_kid');
  assert.equal((await v.verify(newKeyToken)).reason, 'unknown_kid');

  assert.equal(
    state.calls,
    2,
    'one warm-up plus at most ONE failed attempt per floor window: four requests must not become seven fetches',
  );

  // Past the floor, exactly one more attempt is allowed.
  clock += 61_000; // 122s past the warm-up: still inside TTL 60s + grace 120s = 180s
  assert.equal((await v.verify(oldKeyToken)).ok, true);
  assert.equal((await v.verify(oldKeyToken)).ok, true);
  assert.equal(state.calls, 3, 'the floor opens once per window, it does not close permanently');
});

test('a key rotation the verifier can actually reach costs exactly one refetch', async () => {
  const rotatedIn = await makeKeyMaterial('rotated-in-kid');
  const { fetchImpl, state } = makeRotatingJwksFetch([material.publicJwk]);
  let clock = FIXED_NOW_MS;
  const v = verifierWith(fetchImpl, () => clock, {
    jwksTtlMs: 60_000,
    jwksMinRefetchMs: 60_000,
    jwksStaleGraceMs: 120_000,
  });
  const tokenA = await signToken(material, { nowSec, claims: { exp: nowSec + 86_400 } });
  const tokenB = await signToken(rotatedIn, { nowSec, claims: { exp: nowSec + 86_400 } });

  assert.equal((await v.verify(tokenA)).ok, true, 'key A verifies before the rotation');
  assert.equal(state.calls, 1);

  state.keys = [rotatedIn.publicJwk]; // upstream rotated A out and B in
  clock += 61_000; // past the TTL, so the next verify refetches

  assert.equal((await v.verify(tokenB)).ok, true, 'the rotated-in key verifies after one refetch');
  assert.equal(state.calls, 2, 'a reachable rotation costs one refetch, not one per request');
  assert.equal((await v.verify(tokenA)).ok, false, 'the rotated-out key stops verifying once the new set is confirmed');
  assert.equal(state.calls, 2, 'and the denial does not amplify into more fetches');
});

test('the stale grace is a trade, not free availability: it keeps a RETIRED key and denies a rotated-in one', async () => {
  // The documented semantics, pinned. Inside the grace the verifier is serving
  // a key set it can no longer confirm, so it preserves exactly the sessions an
  // operator would want dropped (signed by a key already rotated out) and
  // denies the ones they would want kept (signed by the current key). This is
  // recorded in edge/README.md as a known trade-off of JWKS_STALE_GRACE_MS > 0.
  const rotatedIn = await makeKeyMaterial('rotated-in-kid');
  const { fetchImpl, state } = makeRotatingJwksFetch([material.publicJwk]);
  let clock = FIXED_NOW_MS;
  const v = verifierWith(fetchImpl, () => clock, {
    jwksTtlMs: 60_000,
    jwksMinRefetchMs: 60_000,
    jwksStaleGraceMs: 3_600_000,
  });
  const retiredKeyToken = await signToken(material, { nowSec, claims: { exp: nowSec + 86_400 } });
  const currentKeyToken = await signToken(rotatedIn, { nowSec, claims: { exp: nowSec + 86_400 } });

  assert.equal((await v.verify(retiredKeyToken)).ok, true);
  state.keys = [rotatedIn.publicJwk]; // upstream rotates
  state.fail = true; // and then goes unreachable
  clock += 61_000;

  assert.equal((await v.verify(retiredKeyToken)).ok, true, 'a RETIRED key is still accepted inside the grace');
  assert.equal((await v.verify(currentKeyToken)).reason, 'unknown_kid', 'the CURRENT key is denied until a refetch succeeds');

  state.fail = false;
  clock += 61_000; // past the floor, so one attempt is allowed and it succeeds
  assert.equal((await v.verify(currentKeyToken)).ok, true, 'after recovery the rotated-in key works');
  assert.equal((await v.verify(retiredKeyToken)).ok, false, 'and the retired key correctly stops working');
});

test('every tuning value is validated at construction: a non-numeric or negative one refuses to build', () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  for (const bad of [
    { jwksTtlMs: 'ten minutes' },
    { jwksTtlMs: -1 },
    { jwksMinRefetchMs: NaN },
    { clockSkewSec: '30' },
    { jwksStaleGraceMs: -0.5 },
    { jwksStaleGraceMs: Infinity },
  ]) {
    assert.throws(() => verifierWith(fetchImpl, FIXED_NOW_MS, bad), /misconfigured/, `${JSON.stringify(bad)} must refuse`);
  }
  // Zero is a real value on both of these and must survive, not be replaced by
  // a default: it is how an operator asks for fail-closed and no cache reuse.
  assert.doesNotThrow(() => verifierWith(fetchImpl, FIXED_NOW_MS, { jwksStaleGraceMs: 0, jwksMinRefetchMs: 0, clockSkewSec: 0 }));
});

// ---------------------------------------------------------------------------
// Round 2, item 7: the skew window has an exact edge, and the edge is tested.
// ---------------------------------------------------------------------------

test('the expiry grace is exactly the configured skew: 29s stale still verifies, 30s does not', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, { clockSkewSec: 30 });
  // nowSec >= exp + skew denies, so exp = nowSec - 30 is the first denied value
  // and exp = nowSec - 29 is the last accepted one.
  assert.equal((await v.verify(await signToken(material, { nowSec, claims: { exp: nowSec - 29 } }))).ok, true, '29s past expiry is inside the 30s skew');
  const atEdge = await v.verify(await signToken(material, { nowSec, claims: { exp: nowSec - 30 } }));
  assert.equal(atEdge.ok, false, 'exactly 30s past expiry is outside the window');
  assert.equal(atEdge.reason, 'expired');
  const past = await v.verify(await signToken(material, { nowSec, claims: { exp: nowSec - 31 } }));
  assert.equal(past.reason, 'expired');
});

test('the not-before grace is exactly the configured skew: 30s early still verifies, 31s does not', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, { clockSkewSec: 30 });
  // nowSec + skew < nbf denies, so nbf = nowSec + 30 is the last accepted value.
  assert.equal((await v.verify(await signToken(material, { nowSec, claims: { nbf: nowSec + 30 } }))).ok, true, 'exactly 30s early is inside the window');
  const early = await v.verify(await signToken(material, { nowSec, claims: { nbf: nowSec + 31 } }));
  assert.equal(early.ok, false, '31s early is outside the window');
  assert.equal(early.reason, 'not_yet_valid');
});

test('a configured skew of zero removes the grace entirely', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, { clockSkewSec: 0 });
  assert.equal((await v.verify(await signToken(material, { nowSec, claims: { exp: nowSec + 1 } }))).ok, true);
  assert.equal((await v.verify(await signToken(material, { nowSec, claims: { exp: nowSec } }))).reason, 'expired');
});

test('a whitespace-only assertion header is no token, and must not shadow a valid cookie', async () => {
  const token = await signToken(material, { nowSec });
  const req = new Request('https://private.example.invalid/app', {
    headers: { 'Cf-Access-Jwt-Assertion': '   ', Cookie: `CF_Authorization=${token}` },
  });
  assert.equal(readAccessToken(req), token, 'the cookie fallback must run when the header trims to nothing');

  const padded = new Request('https://private.example.invalid/app', {
    headers: { 'Cf-Access-Jwt-Assertion': `  ${token}  ` },
  });
  assert.equal(readAccessToken(padded), token, 'a padded header is trimmed rather than refused as malformed');
});

// ---------------------------------------------------------------------------
// Round 4, item 1: the refetch floor is honored at the DEFAULT grace of 0, i.e.
// when there is no usable cache to fall back on. The floor used to be gated on
// a servable stale cache, which made it inert on every default deployment: an
// outage turned each inbound request into an outbound fetch, and a forced
// unknown-kid lookup into a second one.
// ---------------------------------------------------------------------------

test('at the default zero grace, a failed forced refresh does not refetch again inside the floor', async () => {
  const other = await makeKeyMaterial('rotated-in-kid');
  const { fetchImpl, state } = makeRotatingJwksFetch([material.publicJwk]);
  let clock = FIXED_NOW_MS;
  const v = verifierWith(fetchImpl, () => clock, {
    jwksTtlMs: 600_000,
    jwksMinRefetchMs: 60_000,
    jwksStaleGraceMs: 0,
  });
  const oldKeyToken = await signToken(material, { nowSec, claims: { exp: nowSec + 86_400 } });
  const newKeyToken = await signToken(other, { nowSec, claims: { exp: nowSec + 86_400 } });

  assert.equal((await v.verify(oldKeyToken)).ok, true, 'warm the cache while the key set is reachable');
  assert.equal(state.calls, 1);

  state.fail = true;
  clock += 61_000; // inside the 600s TTL, past the 60s floor

  for (let i = 0; i < 3; i += 1) {
    const res = await v.verify(newKeyToken);
    assert.equal(res.ok, false, 'an unknown kid we cannot confirm is denied');
    assert.equal(res.reason, 'key_set_unavailable');
  }
  assert.equal(
    state.calls,
    2,
    'one warm-up plus ONE failed attempt: three unknown-kid verifications must not become three more fetches',
  );

  // The floor bounds outbound calls. It does not break the requests the cached
  // key set can still answer.
  assert.equal((await v.verify(oldKeyToken)).ok, true, 'the still-valid cache keeps verifying during the floor');
  assert.equal(state.calls, 2);

  clock += 61_000; // the floor window reopens
  assert.equal((await v.verify(newKeyToken)).reason, 'key_set_unavailable');
  assert.equal(state.calls, 3, 'the floor opens once per window; it does not stop trying forever');
});

test('during an outage with no usable cache, five requests cost one outbound fetch', async () => {
  const { fetchImpl, state } = makeRotatingJwksFetch([material.publicJwk]);
  state.fail = true; // the key set is unreachable from the very first request
  let clock = FIXED_NOW_MS;
  const v = verifierWith(fetchImpl, () => clock, {
    jwksTtlMs: 60_000,
    jwksMinRefetchMs: 60_000,
    jwksStaleGraceMs: 0,
  });
  const token = await signToken(material, { nowSec, claims: { exp: nowSec + 86_400 } });

  for (let i = 0; i < 5; i += 1) {
    const res = await v.verify(token);
    assert.equal(res.ok, false, 'no confirmed key set means deny: that is unchanged');
    assert.equal(res.reason, 'key_set_unavailable');
    clock += 1_000;
  }
  assert.equal(
    state.calls,
    1,
    'the denial is identical either way, so five requests inside one floor window cost ONE outbound fetch',
  );

  clock += 60_000;
  assert.equal((await v.verify(token)).reason, 'key_set_unavailable');
  assert.equal(state.calls, 2, 'recovery costs at most one floor window of delay, never a permanent stop');
});

test('concurrent requests still JOIN one in-flight fetch rather than being denied by the floor', async () => {
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const state = { calls: 0 };
  const fetchImpl = async () => {
    state.calls += 1;
    await gate;
    return { ok: true, status: 200, json: async () => ({ keys: [material.publicJwk] }) };
  };
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, { jwksMinRefetchMs: 60_000, jwksStaleGraceMs: 0 });
  const token = await signToken(material, { nowSec });

  const inFlight = [v.verify(token), v.verify(token), v.verify(token)];
  release();
  const results = await Promise.all(inFlight);
  for (const res of results) assert.equal(res.ok, true, 'a joined fetch must not be turned into a denial');
  assert.equal(state.calls, 1, 'three concurrent requests collapse into the single fetch the floor allows');
});

// ---------------------------------------------------------------------------
// Round 5, MF-1: the PRECONDITION the unconditional refetch floor depends on.
//
// This does NOT reverse round 4. The floor still applies to attempts with no
// usable cache, and the outbound-amplification argument stands. What it adds is
// that an unconditional floor is only sound when the cache OUTLIVES the floor
// window. With jwksTtlMs < jwksMinRefetchMs the cache expires before a refill
// is permitted, so a HEALTHY key set answering 200 every time still produced a
// permanent duty cycle of key_set_unavailable denials (measured at 50% with
// ttl 30 s / floor 60 s). That combination is now refused at construction.
// ---------------------------------------------------------------------------

test('a cache that expires before the refetch floor reopens is refused at construction, not at runtime', async () => {
  const { fetchImpl, state } = makeJwksFetch([material.publicJwk]);
  // The exact reported scenario: an operator shortens the TTL to pick up a key
  // rotation faster and leaves the floor at its default.
  assert.throws(
    () => verifierWith(fetchImpl, FIXED_NOW_MS, { jwksTtlMs: 30_000, jwksMinRefetchMs: 60_000 }),
    /jwksTtlMs must be >= jwksMinRefetchMs/,
    'ttl 30s against a 60s floor must refuse at construction',
  );
  assert.equal(state.calls, 0, 'a refused configuration must not have reached the key set at all');

  for (const [ttl, floor] of [[10_000, 60_000], [0, 1], [59_999, 60_000]]) {
    assert.throws(
      () => verifierWith(fetchImpl, FIXED_NOW_MS, { jwksTtlMs: ttl, jwksMinRefetchMs: floor }),
      /jwksTtlMs must be >= jwksMinRefetchMs/,
      `ttl ${ttl} against floor ${floor} must be refused`,
    );
  }
});

test('the pathological configuration can never produce a run of 401s, because it never verifies anything', async () => {
  // The failure this replaces: 120 requests against a key set answering 200 on
  // every call, 60 of them denied key_set_unavailable. The layer now has no
  // verifier to deny with. A deploy-time refusal is recoverable by editing one
  // variable; a silent 50% denial of authenticated traffic is not.
  const { fetchImpl, state } = makeJwksFetch([material.publicJwk]);
  let built = null;
  try {
    built = verifierWith(fetchImpl, FIXED_NOW_MS, { jwksTtlMs: 30_000, jwksMinRefetchMs: 60_000 });
  } catch {
    built = null;
  }
  assert.equal(built, null, 'no verifier exists under the pathological configuration');
  assert.equal(state.calls, 0, 'and no request was ever served, denied or otherwise');
});

test('a valid ordering still constructs and still honors the round-4 floor', async () => {
  const token = await signToken(material, { nowSec });
  // Equal is legal: the cache is refillable exactly as it expires.
  for (const [ttl, floor] of [[60_000, 60_000], [600_000, 60_000], [120_000, 60_000], [1, 0]]) {
    const { fetchImpl } = makeJwksFetch([material.publicJwk]);
    const v = verifierWith(fetchImpl, FIXED_NOW_MS, { jwksTtlMs: ttl, jwksMinRefetchMs: floor });
    const res = await v.verify(token);
    assert.equal(res.ok, true, `ttl ${ttl} against floor ${floor} must remain a working configuration`);
  }

  // And the round-4 floor behavior itself is untouched under a valid ordering:
  // an outage with no usable cache still costs ONE outbound fetch per window.
  const { fetchImpl, state } = makeRotatingJwksFetch([material.publicJwk]);
  state.fail = true;
  let clock = FIXED_NOW_MS;
  const v = verifierWith(fetchImpl, () => clock, {
    jwksTtlMs: 600_000,
    jwksMinRefetchMs: 60_000,
    jwksStaleGraceMs: 0,
  });
  for (let i = 0; i < 5; i += 1) {
    assert.equal((await v.verify(token)).reason, 'key_set_unavailable');
  }
  assert.equal(state.calls, 1, 'the round-4 floor still bounds five denials to one outbound fetch');
});

// ---------------------------------------------------------------------------
// Round 5, S4: a HUNG key set upstream must deny, not stall.
//
// getKeys() deliberately JOINS an in-flight attempt rather than denying, so an
// attempt that never settles held every concurrent request open with no
// ceiling. A refusing upstream was already a denial; a silent one was not.
// ---------------------------------------------------------------------------

test('a key set fetch that never answers becomes a denial, not an unbounded wait', async () => {
  let started = 0;
  const hangingFetch = async () => {
    started += 1;
    return new Promise(() => {}); // accepted, never answered
  };
  const v = verifierWith(hangingFetch, FIXED_NOW_MS, { jwksFetchTimeoutMs: 25 });
  const token = await signToken(material, { nowSec });

  const began = Date.now();
  const results = await Promise.all([v.verify(token), v.verify(token), v.verify(token)]);
  const elapsed = Date.now() - began;

  for (const res of results) {
    assert.equal(res.ok, false, 'a hung key set denies');
    assert.equal(res.reason, 'key_set_unavailable', 'and it denies with the key-set reason, not by hanging');
  }
  assert.equal(started, 1, 'the three concurrent verifications still collapse into one outbound attempt');
  assert.ok(elapsed < 2000, `three concurrent verifications must not stall: took ${elapsed}ms`);
});

test('a fetch that answers inside the timeout is unaffected by the bound', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, { jwksFetchTimeoutMs: 5_000 });
  const token = await signToken(material, { nowSec });
  assert.equal((await v.verify(token)).ok, true, 'the timeout must not break a healthy fetch');
});

test('the fetch timeout is a validated tuning value like every other one', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  for (const bad of [-1, NaN, Number.POSITIVE_INFINITY, '5000', null]) {
    assert.throws(
      () => verifierWith(fetchImpl, FIXED_NOW_MS, { jwksFetchTimeoutMs: bad }),
      /jwksFetchTimeoutMs/,
      `jwksFetchTimeoutMs ${String(bad)} must be a configuration error`,
    );
  }
  // Zero is refused separately: it aborts before the request is sent, which is
  // not a bound, it is an outage this file would configure for itself.
  assert.throws(
    () => verifierWith(fetchImpl, FIXED_NOW_MS, { jwksFetchTimeoutMs: 0 }),
    /jwksFetchTimeoutMs must be > 0/,
    'a zero timeout is a self-inflicted outage, not a tuning choice',
  );
});

// ---------------------------------------------------------------------------
// Round 6, MF-1: every time knob has a documented MAXIMUM, refused at
// construction. A floor alone is half a bound: `ACCESS_CLOCK_SKEW_SEC="30000"`
// (three extra zeros on the default 30) is 8.33 hours of accepted expiry, and
// every knob below fails OPEN rather than green when it is made large enough.
// ---------------------------------------------------------------------------

test('a clock skew past the documented maximum is refused at construction', async () => {
  const { fetchImpl, state } = makeJwksFetch([material.publicJwk]);
  // The exact reported input: three extra zeros on the shipped default of 30.
  assert.throws(
    () => verifierWith(fetchImpl, FIXED_NOW_MS, { clockSkewSec: 30_000 }),
    /clockSkewSec must be <= 300/,
    'skew 30000 (8.33 hours of expired-token acceptance) must refuse at construction',
  );
  assert.equal(state.calls, 0, 'a refused configuration must not have reached the key set at all');
  // A year, the other shape of the same typo.
  assert.throws(() => verifierWith(fetchImpl, FIXED_NOW_MS, { clockSkewSec: 31_536_000 }), /clockSkewSec must be <= 300/);
});

test('each time knob refuses its maximum plus one and accepts the maximum itself', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const DAY = 24 * 60 * 60 * 1000;
  const knobs = [
    ['clockSkewSec', 300, {}],
    ['jwksTtlMs', DAY, {}],
    // The floor is bounded by the TTL, so its own maximum is exercised with a
    // TTL at the same value: this asserts the MAXIMUM, not the TTL/floor pairing.
    ['jwksMinRefetchMs', DAY, { jwksTtlMs: DAY }],
    ['jwksStaleGraceMs', DAY, {}],
    ['jwksFetchTimeoutMs', 30_000, {}],
  ];
  for (const [label, max, extra] of knobs) {
    assert.throws(
      () => verifierWith(fetchImpl, FIXED_NOW_MS, { ...extra, [label]: max + 1 }),
      new RegExp(`${label} must be <= ${max}`),
      `${label} must refuse ${max + 1}`,
    );
    const atMax = verifierWith(fetchImpl, FIXED_NOW_MS, { ...extra, [label]: max });
    assert.equal(typeof atMax.verify, 'function', `${label} at exactly ${max} must still construct`);
  }
});

test('a verifier at every maximum still verifies a valid token', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const DAY = 24 * 60 * 60 * 1000;
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, {
    clockSkewSec: 300,
    jwksTtlMs: DAY,
    jwksMinRefetchMs: DAY,
    jwksStaleGraceMs: DAY,
    jwksFetchTimeoutMs: 30_000,
  });
  const res = await v.verify(await signToken(material, { nowSec }));
  assert.equal(res.ok, true, 'the caps are generous enough that the boundary configuration still works');
});

test('a token expired past the maximum skew is still denied, so the cap is a real ceiling', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  const v = verifierWith(fetchImpl, FIXED_NOW_MS, { clockSkewSec: 300 });
  // Expired one hour ago: inside the 30000 s window an operator typo would have
  // opened, far outside the 300 s ceiling.
  const token = await signToken(material, { nowSec, claims: { exp: nowSec - 3600, nbf: nowSec - 7200 } });
  const res = await v.verify(token);
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'expired');
});

// ---------------------------------------------------------------------------
// Round 6, MF-2: the team name is the TRUST ROOT (issuer and key set host are
// both interpolated from it), so it must be exactly one DNS label before any
// URL is built.
// ---------------------------------------------------------------------------

test('a team name that is not one DNS label is refused before any URL is built', async () => {
  const { fetchImpl, state } = makeJwksFetch([material.publicJwk]);
  const bad = [
    'attacker.example/path', // the reported input: key set host becomes attacker.example
    'attacker.example/x?z=',
    'evil.com#',
    'a b',
    'UPPER',
    '-leading-hyphen',
    'trailing-hyphen-',
    'has_underscore',
    'x'.repeat(64),
    '   ',
  ];
  for (const teamName of bad) {
    assert.throws(
      () => createAccessVerifier({ teamName, aud: AUD, fetchImpl, now: () => FIXED_NOW_MS }),
      /misconfigured/,
      `teamName ${JSON.stringify(teamName)} must be refused at construction`,
    );
  }
  assert.equal(state.calls, 0, 'a refused team name must never have been fetched from');
});

test('the shipped REPLACE_WITH placeholders are refused for both required identifiers', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  assert.throws(
    () => createAccessVerifier({ teamName: 'REPLACE_WITH_TEAM_NAME', aud: AUD, fetchImpl, now: () => FIXED_NOW_MS }),
    /still the shipped placeholder/,
  );
  assert.throws(
    () =>
      createAccessVerifier({
        teamName: 'example-team',
        aud: 'REPLACE_WITH_APPLICATION_AUDIENCE_TAG',
        fetchImpl,
        now: () => FIXED_NOW_MS,
      }),
    /still the shipped placeholder/,
  );
  // Blank and whitespace-only are refused for both, not just falsy-empty.
  assert.throws(() => createAccessVerifier({ teamName: '  ', aud: AUD, fetchImpl, now: () => FIXED_NOW_MS }), /misconfigured/);
  assert.throws(() => createAccessVerifier({ teamName: 'example-team', aud: '  ', fetchImpl, now: () => FIXED_NOW_MS }), /non-blank/);
});

test('a valid DNS label builds exactly the documented key set URL and issuer', async () => {
  const { fetchImpl } = makeJwksFetch([material.publicJwk]);
  for (const label of ['example-team', 'a', 'team-1', 'x'.repeat(63)]) {
    const v = createAccessVerifier({ teamName: label, aud: AUD, fetchImpl, now: () => FIXED_NOW_MS });
    assert.equal(v.issuer, `https://${label}.cloudflareaccess.com`);
    assert.equal(v.jwksUrl, `https://${label}.cloudflareaccess.com/cdn-cgi/access/certs`);
  }
});
