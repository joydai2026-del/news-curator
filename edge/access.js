// Cloudflare Access JWT verification for the edge layer.
//
// Deny by default. Every failure path returns { ok: false, reason } and the
// caller turns that into a 401. Nothing here trusts a header it did not verify.
//
// No runtime dependencies: WebCrypto (crypto.subtle) does RS256, and both the
// clock and the fetch used for the key set are injected so tests stay hermetic.
//
// Reference (read 2026-09-02):
// https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/

export const ACCESS_JWT_HEADER = 'Cf-Access-Jwt-Assertion';
export const ACCESS_JWT_COOKIE = 'CF_Authorization';

// Tuning defaults. Every one of these is overridable from configuration (see
// edge/README.md); the values here are what applies when nothing is set.
export const DEFAULT_JWKS_TTL_MS = 10 * 60 * 1000; // 10 minutes
export const DEFAULT_MIN_REFETCH_MS = 60 * 1000; // floor between forced refetches
export const DEFAULT_CLOCK_SKEW_SEC = 30;
// Serve-stale on a key set outage. Zero means fail closed: an outage past the
// TTL denies every session rather than trusting a key set we can no longer
// confirm. A positive value trades that for availability, deliberately.
export const DEFAULT_JWKS_STALE_GRACE_MS = 0;
// Upper bound on ONE key set fetch, in milliseconds. A hung upstream is not the
// same failure as a refusing one: a refusal denies immediately, a hang holds the
// in-flight promise open and every request that joins it waits with no ceiling.
// Bounding the fetch turns a hang into a denial, which is what the rest of this
// file already does with every other key set failure.
export const DEFAULT_JWKS_FETCH_TIMEOUT_MS = 5 * 1000;

// Documented MAXIMA on every time knob above. A tuning value has a floor
// (`>= 0`, already enforced) and it also needs a CEILING, for the same reason
// METER_POLICY_JSON staleness_ms got one: three extra zeros on a default is not
// a tuning choice, it is a typo, and each of these windows DISABLES the control
// it names once it is wide enough. The difference from staleness_ms is the
// direction of failure. A too-wide freshness window fails GREEN (a receipt
// reads settled on a stale number). These fail OPEN:
//   clockSkewSec       the accepted-session window past a token's own `exp`.
//                      "30000" (three extra zeros on 30) is 8.33 hours of
//                      expired-token acceptance, measured on the real dispatch
//                      path as HTTP 200 on a private route.
//   jwksTtlMs          how long a retired or revoked key keeps verifying
//                      sessions in this isolate.
//   jwksStaleGraceMs   the same, past the TTL, during an outage. The `0`
//                      default is the load-bearing fail-closed one.
//   jwksFetchTimeoutMs how long a hung upstream holds requests before the hang
//                      becomes a denial.
//
// What the ceilings refuse when the assumption is wrong: a deployment whose
// clocks are more than five minutes apart (a machine to fix, not a session
// window to widen), or an operator who deliberately wants expired Access
// tokens honored for hours (not tuning: that is disabling the `exp` claim, and
// Access owns session lifetime). The refusal is a deploy-time 503 recoverable
// in seconds by editing one variable; the current alternative is a silently
// extended authentication window that no log, no receipt and no reason code
// records. Blast radius favors refusing at the door, the same trade the TTL
// floor precondition and the policy-key allowlist already make.
export const MAX_CLOCK_SKEW_SEC = 300; // 5 minutes, 10x the default
export const MAX_JWKS_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours
export const MAX_JWKS_MIN_REFETCH_MS = 24 * 60 * 60 * 1000; // 24 hours
export const MAX_JWKS_STALE_GRACE_MS = 24 * 60 * 60 * 1000; // 24 hours
export const MAX_JWKS_FETCH_TIMEOUT_MS = 30 * 1000; // 30 seconds

// The team name is the TRUST ROOT of this whole layer: the issuer a token's
// `iss` must equal and the host the key set is fetched from are both built by
// interpolating it. An unvalidated value therefore redirects trust rather than
// misconfiguring a window: `attacker.example/path` yields a key set URL on host
// `attacker.example`, and a token signed by that host's key opens a private
// route. Cloudflare team names are one DNS label, so that is what is accepted:
// lowercase letters, digits and hyphens, no leading or trailing hyphen, at most
// 63 characters. Everything else is a configuration error caught before any URL
// is built.
export const ACCESS_TEAM_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

// The sentinels wrangler.toml.example ships. A copied-but-unfilled config is a
// configuration error, not an identity: without this check ACCESS_AUD keeps its
// placeholder and the aud comparison simply never matches, which reads as
// "every token is denied" instead of "you did not fill in the file".
const PLACEHOLDER_PREFIX = 'REPLACE_WITH_';

function b64urlToBytes(input) {
  if (typeof input !== 'string' || input.length === 0) throw new Error('empty segment');
  if (/[^A-Za-z0-9_-]/.test(input)) throw new Error('not base64url');
  const pad = input.length % 4 === 0 ? '' : '='.repeat(4 - (input.length % 4));
  const b64 = input.replace(/-/g, '+').replace(/_/g, '/') + pad;
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

function b64urlToJson(input) {
  return JSON.parse(new TextDecoder().decode(b64urlToBytes(input)));
}

function deny(reason) {
  return { ok: false, reason };
}

/**
 * Read the Access token off a request. The header is authoritative; the cookie
 * is a documented fallback that is not guaranteed to be present.
 */
export function readAccessToken(request) {
  const header = request.headers.get(ACCESS_JWT_HEADER);
  // Trim BEFORE deciding the header won. A whitespace-only assertion header is
  // no token at all, and it must not shadow a valid cookie.
  if (header && header.trim()) return header.trim();
  const cookie = request.headers.get('Cookie');
  if (!cookie) return null;
  for (const part of cookie.split(';')) {
    const [name, ...rest] = part.trim().split('=');
    if (name === ACCESS_JWT_COOKIE && rest.length > 0) return rest.join('=').trim();
  }
  return null;
}

/**
 * @param {object} config
 * @param {string} config.teamName    the Access team name (issuer is derived)
 * @param {string} config.aud         the application audience tag
 * @param {function} config.fetchImpl injected fetch, used only for the key set
 * @param {function} config.now       injected clock, returns epoch milliseconds
 */
export function createAccessVerifier(config) {
  const teamName = config.teamName;
  const aud = config.aud;
  const fetchImpl = config.fetchImpl;
  const now = config.now || (() => Date.now());
  // Explicit undefined checks: 0 is a meaningful value here and must not be
  // swallowed by a falsy default.
  const ttlMs = config.jwksTtlMs === undefined ? DEFAULT_JWKS_TTL_MS : config.jwksTtlMs;
  const minRefetchMs = config.jwksMinRefetchMs === undefined ? DEFAULT_MIN_REFETCH_MS : config.jwksMinRefetchMs;
  const skewSec = config.clockSkewSec === undefined ? DEFAULT_CLOCK_SKEW_SEC : config.clockSkewSec;
  const staleGraceMs =
    config.jwksStaleGraceMs === undefined ? DEFAULT_JWKS_STALE_GRACE_MS : config.jwksStaleGraceMs;
  const fetchTimeoutMs =
    config.jwksFetchTimeoutMs === undefined ? DEFAULT_JWKS_FETCH_TIMEOUT_MS : config.jwksFetchTimeoutMs;

  if (!teamName || !aud || typeof fetchImpl !== 'function') {
    throw new Error('access verifier misconfigured: teamName, aud and fetchImpl are required');
  }
  // Both required identifiers, checked before anything is interpolated.
  for (const [label, value] of [['teamName', teamName], ['aud', aud]]) {
    if (typeof value !== 'string' || value.trim() === '') {
      throw new Error(`access verifier misconfigured: ${label} must be a non-blank string`);
    }
    if (value.startsWith(PLACEHOLDER_PREFIX)) {
      throw new Error(`access verifier misconfigured: ${label} is still the shipped placeholder`);
    }
  }
  // The trust root. See ACCESS_TEAM_NAME_RE above.
  if (!ACCESS_TEAM_NAME_RE.test(teamName)) {
    throw new Error(
      'access verifier misconfigured: teamName must be one DNS label (lowercase letters, digits and hyphens, no leading or trailing hyphen, at most 63 characters)',
    );
  }
  // A tuning value that is not a finite, non-negative number is a
  // misconfiguration, not something to coerce. Coercing it would silently widen
  // a window (NaN >= anything is false, so every comparison would fall the
  // wrong way) and nobody would see it.
  for (const [label, value, max] of [
    ['jwksTtlMs', ttlMs, MAX_JWKS_TTL_MS],
    ['jwksMinRefetchMs', minRefetchMs, MAX_JWKS_MIN_REFETCH_MS],
    ['clockSkewSec', skewSec, MAX_CLOCK_SKEW_SEC],
    ['jwksStaleGraceMs', staleGraceMs, MAX_JWKS_STALE_GRACE_MS],
    ['jwksFetchTimeoutMs', fetchTimeoutMs, MAX_JWKS_FETCH_TIMEOUT_MS],
  ]) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
      throw new Error(`access verifier misconfigured: ${label} must be a finite number >= 0`);
    }
    // See MAX_* above for why each window has a ceiling and what refusing costs.
    if (value > max) {
      throw new Error(`access verifier misconfigured: ${label} must be <= ${max}`);
    }
  }
  // A timeout of zero aborts before the request is sent, which is not a bound,
  // it is an outage this file would have configured for itself.
  if (fetchTimeoutMs <= 0) {
    throw new Error('access verifier misconfigured: jwksFetchTimeoutMs must be > 0');
  }
  // PRECONDITION of the unconditional refetch floor (round 5). This is NOT a
  // reversal of the round-4 decision: the floor still applies to ATTEMPTS even
  // when no cache is left to serve, and the outbound-amplification argument at
  // getKeys() is untouched and correct. What round 4 left implicit is that an
  // unconditional floor is only sound when the cache OUTLIVES the floor window.
  // With ttlMs < minRefetchMs the cache expires before the floor permits a
  // refill, so a perfectly healthy key set answering 200 on every call still
  // produces a permanent duty cycle of `key_set_unavailable` denials (measured:
  // 50% of authenticated requests at ttl 30 s / floor 60 s, 83% at ttl 10 s),
  // with a reason code pointing an operator at the wrong system.
  //
  // What this refuses when the assumption is wrong: a deployment where an
  // operator genuinely wants a key set thrown away before it is allowed to be
  // fetched again. That configuration has no coherent meaning. The refusal is a
  // deploy-time 503 recoverable in seconds by editing one variable; the
  // alternative is a silent denial of live traffic. Same trade the policy-key
  // allowlist already makes: refuse the incoherent configuration at the door.
  if (ttlMs < minRefetchMs) {
    throw new Error(
      'access verifier misconfigured: jwksTtlMs must be >= jwksMinRefetchMs, or the key set cache expires before the refetch floor allows a refill',
    );
  }

  const issuer = `https://${teamName}.cloudflareaccess.com`;
  const jwksUrl = `${issuer}/cdn-cgi/access/certs`;

  let cache = { keys: null, fetchedAt: -Infinity };
  // The last ATTEMPT, successful or not. Tracked separately from the last
  // SUCCESS because the refetch floor has to bound outbound calls during an
  // outage, and during an outage there is no success to measure from. Without
  // this, a stale-but-in-grace cache made every request attempt a fetch (twice,
  // once for the normal read and once for the forced unknown-kid lookup).
  let lastAttemptAt = -Infinity;
  let inFlight = null;

  async function fetchKeys(signal) {
    const res = await fetchImpl(jwksUrl, { method: 'GET', signal });
    if (!res || !res.ok) throw new Error(`key set fetch failed: ${res ? res.status : 'no response'}`);
    const body = await res.json();
    if (!body || !Array.isArray(body.keys)) throw new Error('key set body is not a JWKS');
    return body.keys;
  }

  async function loadKeys() {
    // The whole read is bounded, not only the connect: an upstream that accepts
    // the connection and never answers, or answers headers and never sends a
    // body, hangs `inFlight` forever, and getKeys() deliberately JOINS
    // `inFlight` rather than denying, so every concurrent request hangs with it.
    // A denial is a bounded, observable outcome; an unbounded wait is not.
    //
    // The abort signal is passed to the fetch AND raced against a timer. The
    // signal alone is not enough: an implementation that ignores it (an
    // injected test double, or a runtime that does not honor it) would still
    // hang. The race is what guarantees the bound; the signal is what releases
    // the socket when the runtime does honor it.
    const controller = new AbortController();
    let timer = null;
    try {
      const keys = await Promise.race([
        fetchKeys(controller.signal),
        new Promise((_, reject) => {
          timer = setTimeout(() => {
            controller.abort();
            reject(new Error('key set fetch timed out'));
          }, fetchTimeoutMs);
        }),
      ]);
      cache = { keys, fetchedAt: now() };
      return keys;
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  /** True when the cached key set has aged out but is still inside its grace. */
  function staleCacheServable(at) {
    return staleGraceMs > 0 && Boolean(cache.keys) && at - cache.fetchedAt <= ttlMs + staleGraceMs;
  }

  async function getKeys(force) {
    const at = now();
    const age = at - cache.fetchedAt;
    if (!force && cache.keys && age < ttlMs) return cache.keys;
    if (force && cache.keys && age < minRefetchMs) return cache.keys;
    // The floor applies to ATTEMPTS, not only to successes, and it is honored
    // even when NO cache is left to serve. Inside `jwksMinRefetchMs` of a failed
    // attempt, with no usable cache, this denies WITHOUT FETCHING.
    //
    // Why this direction (round 4 decision, written here so a later round does
    // not silently flip it back): the denial is identical either way until a
    // fetch succeeds. With no usable key set every token is refused whether or
    // not we try again, so honoring the floor changes nothing an inbound caller
    // can observe during the outage. What it does change is outbound
    // amplification. At the default grace of 0 the previous gate was inert, so
    // every inbound request cost one outbound fetch, and a forced unknown-kid
    // lookup cost a second: unbounded, and paceable 1:1 by an unauthenticated
    // caller sending forged kids, against the 50-subrequest per-invocation
    // ceiling this architecture exists to respect.
    //
    // What it costs when the assumption is wrong: after the upstream recovers,
    // up to one floor window of denial that a retry would have avoided. A
    // bounded delay on recovery is recoverable. An unbounded fan-out into a
    // failing dependency is not.
    if (at - lastAttemptAt < minRefetchMs) {
      if (staleCacheServable(at)) return cache.keys;
      // An attempt is already running: JOIN it rather than deny, so concurrent
      // requests still collapse into the single outbound fetch the floor allows.
      if (!inFlight) throw new Error('key set refetch floor is closed and no usable cache remains');
    }
    if (!inFlight) {
      lastAttemptAt = at;
      inFlight = loadKeys().finally(() => {
        inFlight = null;
      });
    }
    try {
      return await inFlight;
    } catch (err) {
      // The key set is unreachable and the cached copy has aged out. Serving it
      // anyway is a deliberate, bounded, configured choice: the grace defaults
      // to 0, which denies. Above 0 we serve the last key set we did confirm,
      // and only inside the window. NOTE the trade this makes: the last key set
      // we confirmed may already have been RETIRED upstream, so a grace above 0
      // keeps retired-key sessions alive and still denies a key rotated in
      // during the outage. See edge/README.md.
      if (staleCacheServable(now())) return cache.keys;
      throw err;
    }
  }

  function audMatches(claim) {
    if (typeof claim === 'string') return claim === aud;
    if (Array.isArray(claim)) return claim.includes(aud);
    return false;
  }

  async function verify(token) {
    if (typeof token !== 'string' || token.length === 0) return deny('missing_token');
    const parts = token.split('.');
    if (parts.length !== 3) return deny('malformed_token');

    let header;
    let claims;
    try {
      header = b64urlToJson(parts[0]);
      claims = b64urlToJson(parts[1]);
    } catch {
      return deny('malformed_token');
    }
    if (!header || typeof header !== 'object' || !claims || typeof claims !== 'object') {
      return deny('malformed_token');
    }

    // Algorithm is pinned. "none" and every symmetric algorithm are refused
    // before any key lookup happens.
    if (header.alg !== 'RS256') return deny('bad_alg');
    if (typeof header.kid !== 'string' || header.kid.length === 0) return deny('missing_kid');

    if (claims.iss !== issuer) return deny('bad_iss');
    if (!audMatches(claims.aud)) return deny('bad_aud');

    const nowSec = Math.floor(now() / 1000);
    // exp must be a FINITE number. JSON.parse turns an overlarge literal such as
    // 1e999 into Infinity, which is typeof 'number' and would compare as a token
    // that never expires. A string exp is not a number and is refused too.
    if (typeof claims.exp !== 'number') return deny('missing_exp');
    if (!Number.isFinite(claims.exp)) return deny('bad_exp');
    if (nowSec >= claims.exp + skewSec) return deny('expired');
    if (claims.nbf !== undefined) {
      if (typeof claims.nbf !== 'number' || !Number.isFinite(claims.nbf)) return deny('bad_nbf');
      if (nowSec + skewSec < claims.nbf) return deny('not_yet_valid');
    }

    // Every key-set failure (transport error, non-200, unparseable body) is a
    // denial, not a throw. The module contract at the top of this file promises
    // that, and a caller that forgets a try/catch must not fail open.
    let jwk;
    try {
      let keys = await getKeys(false);
      jwk = keys.find((k) => k && k.kid === header.kid);
      if (!jwk) {
        keys = await getKeys(true);
        jwk = keys.find((k) => k && k.kid === header.kid);
      }
    } catch {
      return deny('key_set_unavailable');
    }
    if (!jwk) return deny('unknown_kid');

    let key;
    try {
      key = await crypto.subtle.importKey(
        'jwk',
        { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: 'RS256', ext: true },
        { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
        false,
        ['verify'],
      );
    } catch {
      return deny('bad_key');
    }

    let signature;
    try {
      signature = b64urlToBytes(parts[2]);
    } catch {
      return deny('malformed_token');
    }
    const signed = new TextEncoder().encode(`${parts[0]}.${parts[1]}`);
    const valid = await crypto.subtle.verify({ name: 'RSASSA-PKCS1-v1_5' }, key, signature, signed);
    if (!valid) return deny('bad_signature');

    return { ok: true, claims };
  }

  return { verify, issuer, jwksUrl };
}
