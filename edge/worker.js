// The thin edge layer.
//
// What it does: check the Access token, fail closed, hand back a private
// projection with headers that keep it out of every cache.
// What it deliberately does NOT do: build a slate, rank stories, normalize
// feeds, or call a model inline. That work stays in the scheduled job, where a
// 10 ms per-invocation CPU ceiling does not apply.
//
// Route policy is an explicit allowlist. A path nobody listed is private, and
// a private path with no verified token is a 401 before anything else runs.

import {
  createAccessVerifier,
  DEFAULT_CLOCK_SKEW_SEC,
  DEFAULT_JWKS_FETCH_TIMEOUT_MS,
  DEFAULT_JWKS_STALE_GRACE_MS,
  DEFAULT_JWKS_TTL_MS,
  DEFAULT_MIN_REFETCH_MS,
  readAccessToken,
} from './access.js';
import {
  degradedHealthResponse,
  denialResponse,
  privateJson,
  privateResponse,
  unavailableResponse,
} from './headers.js';
import {
  buildLimitReceipt,
  deepFreeze,
  DEFAULT_METER_POLICY,
  MAX_STALENESS_MS,
  METER_ID_RE,
  METER_KINDS,
} from './meter.js';
import { syntheticProjectionFor } from './synthetic.js';

/** The only paths that answer without a verified token. Everything else is private. */
export const PUBLIC_PATHS = Object.freeze(['/healthz']);

/** The only methods this layer answers at all. Everything else is refused before routing. */
export const ALLOWED_METHODS = Object.freeze(['GET']);

// Encoded separators. A path carrying one of these means two different things
// to two different parsers, which is how a prefix check gets walked past.
const ENCODED_SEPARATORS = /%2f|%5c|\\/i;
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

/**
 * True when the request path is safe to route on.
 *
 * Percent-encoding is allowed, because a product with a Chinese lane needs
 * non-ASCII slugs and every one of those arrives percent-encoded. What is NOT
 * allowed is a path with two readings. A percent-encoded path passes only when
 * a single round of decoding introduces no separator, no dot segment and no
 * control character, and re-encoding the decoded form reproduces exactly what
 * arrived. That last check is what rejects an alternate encoding of an ASCII
 * character (`%41` for `A`) and, together with the leftover-percent rule,
 * double encoding (`%252F`).
 */
export function isSafePath(rawPath) {
  if (typeof rawPath !== 'string' || rawPath.length === 0) return false;
  if (ENCODED_SEPARATORS.test(rawPath)) return false;
  if (CONTROL_CHARS.test(rawPath)) return false;
  let decoded;
  try {
    decoded = decodeURIComponent(rawPath);
  } catch {
    return false;
  }
  if (decoded === rawPath) return true;
  return isSafeEncodedPath(rawPath);
}

/** The single-decode round trip, applied per segment. */
function isSafeEncodedPath(rawPath) {
  for (const segment of rawPath.split('/')) {
    if (!segment.includes('%')) continue;
    let decodedSegment;
    try {
      decodedSegment = decodeURIComponent(segment);
    } catch {
      return false;
    }
    if (decodedSegment === segment) continue;
    // A percent left over after one decode means the caller encoded an encoding.
    // One more decode downstream and it becomes something else again.
    if (decodedSegment.includes('%')) return false;
    if (CONTROL_CHARS.test(decodedSegment)) return false;
    if (decodedSegment.includes('/') || decodedSegment.includes('\\')) return false;
    if (decodedSegment === '.' || decodedSegment === '..') return false;
    // Percent triplets are case-insensitive on the wire, so compare with the
    // hex normalized. Everything else must match byte for byte.
    const normalized = segment.replace(/%[0-9a-fA-F]{2}/g, (triplet) => triplet.toUpperCase());
    if (encodeURIComponent(decodedSegment) !== normalized) return false;
  }
  return true;
}

/** The path exactly as it arrived, before URL parsing folds a backslash into a slash. */
export function rawPathOf(requestUrl) {
  const match = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/[^/?#\\]*([^?#]*)/.exec(requestUrl);
  return match ? match[1] : '';
}

export function classifyPath(pathname) {
  if (PUBLIC_PATHS.includes(pathname)) return 'public';
  if (pathname.endsWith('.map')) return 'source_map';
  if (pathname.startsWith('/api/')) return 'api_record';
  if (pathname === '/projection/synthetic') return 'projection';
  if (pathname === '/receipt/limits') return 'limit_receipt';
  // /app matches only at a path boundary: /application is a different path and
  // must not inherit the app route.
  if (pathname === '/' || pathname === '/app' || pathname.startsWith('/app/')) return 'html_app';
  return 'unknown';
}

const APP_HTML = `<!doctype html><meta charset="utf-8"><title>Private projection</title>
<h1>Private projection</h1><p>This page is served only to a verified session.</p>`;

/**
 * @param {object} deps
 * @param {{verify: function}} deps.verifier
 * @param {function} deps.now  epoch milliseconds
 * @param {object}  [deps.meterPolicy]
 * @param {function} [deps.readMeters] async () => {name: {value, sampled_at}}
 */
export function createWorker(deps) {
  const verifier = deps.verifier;
  const now = deps.now || (() => Date.now());
  const meterPolicy = deps.meterPolicy || DEFAULT_METER_POLICY;
  const readMeters = deps.readMeters || null;

  async function handle(request) {
    // Method first. An unsupported method never reaches the router, so no route
    // can be probed with a verb its handler was never written for.
    if (!ALLOWED_METHODS.includes(request.method)) {
      return denialResponse('method_not_allowed', 405);
    }

    const url = new URL(request.url);
    // Path safety second, on both the raw string and the parsed pathname. The
    // URL parser folds a backslash into a slash for http(s), so the raw form is
    // the only place that trick is still visible.
    if (!isSafePath(rawPathOf(request.url)) || !isSafePath(url.pathname)) {
      return denialResponse('bad_path', 400);
    }

    const kind = classifyPath(url.pathname);

    if (kind === 'public') {
      // Public means public: a fixed string, no reader data, nothing derived
      // from the request. It is here so "private by default" has a visible
      // exception rather than an implicit one.
      return privateResponse('ok', { status: 200, headers: { 'Content-Type': 'text/plain' } });
    }

    // Deny by default. The token check runs before routing so an unlisted or
    // non-existent private path cannot be probed without a session.
    const token = readAccessToken(request);
    if (!token) return denialResponse('missing_token', 401);

    let result;
    try {
      result = await verifier.verify(token);
    } catch {
      return denialResponse('verifier_error', 401);
    }
    if (!result || !result.ok) return denialResponse((result && result.reason) || 'denied', 401);

    switch (kind) {
      case 'source_map':
        // Never served, session or not. Build output that would map back to
        // source has no reader-facing purpose here.
        return denialResponse('not_available', 404);
      case 'html_app':
        return privateResponse(APP_HTML, { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
      case 'api_record':
        return privateJson({ record: 'private', path: url.pathname }, { status: 200 });
      case 'projection':
        return privateJson(syntheticProjectionFor(result.claims), { status: 200 });
      case 'limit_receipt': {
        // A meter source that throws is an UNREAD meter, not a missing receipt.
        // Losing the receipt would lose the only record that we could not read.
        let readings = {};
        let meterSource = 'unavailable';
        if (readMeters) {
          try {
            const result = await readMeters();
            // `meter_source` is a PROVENANCE claim on a record an auditor reads:
            // it names the reader that produced these readings. A reader that
            // returned null, an array or a string produced no readings at all,
            // and labeling that `host_analytics_api` would attest to a source
            // that gave us nothing. An unusable return is an unread meter, same
            // rule as a throw. (Codex round-5 should-fix, taken in round 6.)
            if (isPlainObject(result)) {
              readings = result;
              meterSource = 'host_analytics_api';
            } else {
              readings = {};
              meterSource = 'unavailable';
            }
          } catch {
            readings = {};
            meterSource = 'unavailable';
          }
        }
        // The receipt builder REFUSES rather than publishing a receipt it
        // cannot make auditable (no policy revision, a spec it cannot describe,
        // a reading whose required field is missing). Refusing is right; doing
        // it as an uncaught exception is not. An exception out of fetch() is
        // the one response shape that carries none of the private headers this
        // layer guarantees on every response, denials included, and on a route
        // configured fail-open it is the condition that hands the request to
        // origin unauthenticated. A receipt that cannot be built is a 503.
        try {
          const receipt = buildLimitReceipt({
            policy: meterPolicy,
            readings,
            meterSource,
            now: now(),
            receiptId: newReceiptId(now()),
            attributedOperationClass: 'edge_request',
          });
          return privateJson(receipt, { status: 200 });
        } catch {
          return unavailableResponse();
        }
      }
      default:
        return denialResponse('not_found', 404);
    }
  }

  return { fetch: handle };
}

/** Receipt ids are the audit join key, so two in the same millisecond must differ. */
function newReceiptId(nowMs) {
  const suffix =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
  return `lrec-${nowMs}-${suffix}`;
}

/**
 * Meter thresholds are policy, not code (wrangler.toml documents
 * METER_POLICY_JSON as the override). An unparseable override is refused rather
 * than silently replaced, and since round 4 an override with an UNKNOWN KEY is
 * refused too, so a typo can never quietly widen or disable a threshold.
 */
// Parsing the policy on every request costs CPU inside the 10 ms ceiling this
// whole architecture exists to respect, and the string almost never changes.
// Successful parses are memoized per isolate, keyed by the raw string. A
// failure is never memoized: it must throw every time it is asked.
const POLICIES = new Map();

function isPlainObject(value) {
  // A readings map is a plain object: prototype Object.prototype or null.
  // A Response, Map, Date, or class instance is an object too, but it is not
  // a readings map, and treating it as one let a reader that returned a raw
  // fetch Response claim host provenance (round-7 finding).
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

// Exact key allowlists. A key this code does not recognize is a CONFIGURATION
// ERROR, not something to ignore. `hard_stop` instead of `hard_stop_threshold`
// used to be indistinguishable from an absent threshold, so a one-word typo
// silently disabled a spend limit and settled the receipt green at 55x the
// intended stop; `staleness_mss` silently fell back to the 15-minute default.
//
// What strictness refuses when its assumption is wrong: a forward-compatible
// policy carrying a field this build has not learned yet. That is the right
// trade here, because the policy already carries policy_revision (so adding a
// field is a deliberate, versioned change) and the refusal is a DEPLOY-TIME 503
// before any request is served, recoverable in seconds by editing an env var. A
// silently disabled limit is not recoverable, because nobody learns it was
// disabled.
const POLICY_KEYS = Object.freeze(['policy_revision', 'staleness_ms', 'meters']);
// MAX_STALENESS_MS and deepFreeze are imported from meter.js, the module that
// owns the policy, rather than restated here, so the door and the builder
// cannot drift. Same consolidation METER_ID_RE already got.
const METER_SPEC_KEYS = Object.freeze(['meter_kind', 'unit', 'warning_threshold', 'hard_stop_threshold']);

// Meter ids are AUDIT VOCABULARY, not free text. The grammar and the reasoning
// live in meter.js, the module that interpolates ids into reason codes and shed
// actions; it is imported here rather than restated, so the two cannot drift.
// This is the configuration DOOR. meter.js enforces the same rule inside
// buildLimitReceipt, because the door is not the only way into the room: ids
// are non-sensitive by VALIDATION at both layers, not by construction.

function assertOnlyKeys(obj, allowed, label) {
  for (const key of Object.keys(obj)) {
    if (!allowed.includes(key)) {
      throw new Error(`${label} has an unknown key: a control this code does not read is a control that does not exist`);
    }
  }
}

/**
 * Validate the WHOLE policy, not just that it parsed.
 *
 * `typeof null === 'object'` and `typeof [] === 'object'`, so the old check
 * accepted `meters: null` and `meters: []`. It also never looked at
 * policy_revision or at an individual meter spec, so three parseable values
 * built a Worker successfully and then threw one route deep, on
 * /receipt/limits, out of fetch(). Every defect below is a CONFIGURATION error
 * and lands on the already-tested 503 path, before any request is served.
 *
 * An EMPTY meters map is deliberately legal: it is a policy that measures
 * nothing, and meter.js turns that into an unknown receipt rather than a green
 * one. Legal to configure, impossible to mistake for a pass.
 */
/**
 * `resolveMeterPolicy` caches one policy object per configuration string and
 * hands the SAME object to every request in the isolate, so it is deep-frozen
 * with meter.js's `deepFreeze` before it is memoized.
 *
 * CORRECTION (round 6). This comment previously read "`DEFAULT_METER_POLICY` is
 * already frozen; the env-derived one was not." That was FALSE and it made the
 * round-5 fix asymmetric in the wrong direction: `Object.freeze` had been
 * applied to the default policy object and to its `meters` map but NOT to the
 * individual spec objects inside it, so `DEFAULT_METER_POLICY.meters
 * .requests_per_day.hard_stop_threshold = 99999999` silently landed, on the
 * path that runs when an operator sets nothing at all. Both policies now go
 * through the same `deepFreeze`.
 */

function validateMeterPolicy(parsed) {
  if (!isPlainObject(parsed)) {
    throw new Error('METER_POLICY_JSON must be an object with a meters map');
  }
  assertOnlyKeys(parsed, POLICY_KEYS, 'METER_POLICY_JSON');
  // policy_revision is the audit join key between a receipt and the policy that
  // produced it. The frozen contract types it as an integer, so a string, a
  // fraction, a boolean, an object or a value past Number.MAX_SAFE_INTEGER (which
  // no longer round-trips) is a configuration error, not something to emit.
  const revision = parsed.policy_revision;
  if (revision === undefined || revision === null) {
    throw new Error('METER_POLICY_JSON must carry a policy_revision: a receipt without one is unauditable');
  }
  if (typeof revision !== 'number' || !Number.isSafeInteger(revision) || revision < 0) {
    throw new Error('METER_POLICY_JSON policy_revision must be a non-negative safe integer');
  }
  if (!isPlainObject(parsed.meters)) {
    throw new Error('METER_POLICY_JSON must be an object with a meters map');
  }
  if (parsed.staleness_ms !== undefined) {
    const s = parsed.staleness_ms;
    if (typeof s !== 'number' || !Number.isFinite(s) || s < 0) {
      throw new Error('METER_POLICY_JSON staleness_ms must be a finite number >= 0');
    }
    // Round 4 refused a misspelled threshold KEY. The same defect class survives
    // in the VALUE: `900000000` (three extra zeros on 15 minutes) is 10.4 days,
    // and at that width a reading sampled at the UNIX epoch verdicts `fresh` and
    // settles the receipt. A freshness window wider than a day is not a tuning
    // choice, it is a typo, and the control it disables is the one this whole
    // module exists to provide. The cap is deliberately generous: a day is far
    // past any real sampling interval, so refusing above it costs nothing an
    // operator meant to do.
    if (s > MAX_STALENESS_MS) {
      throw new Error('METER_POLICY_JSON staleness_ms must be <= 86400000 (24 hours)');
    }
  }
  for (const [name, spec] of Object.entries(parsed.meters)) {
    if (!METER_ID_RE.test(name)) {
      throw new Error('METER_POLICY_JSON meter ids must be lowercase snake case, at most 64 characters, with no separators');
    }
    if (!isPlainObject(spec)) {
      throw new Error(`METER_POLICY_JSON meter ${name} must be an object`);
    }
    assertOnlyKeys(spec, METER_SPEC_KEYS, `METER_POLICY_JSON meter ${name}`);
    if (!METER_KINDS.includes(spec.meter_kind)) {
      throw new Error(`METER_POLICY_JSON meter ${name} needs a known meter_kind`);
    }
    if (typeof spec.unit !== 'string' || spec.unit.length === 0) {
      throw new Error(`METER_POLICY_JSON meter ${name} needs a unit`);
    }
    for (const field of ['warning_threshold', 'hard_stop_threshold']) {
      const v = spec[field];
      if (v === undefined || v === null) continue;
      // A negative threshold is met by every reading, including a legitimate
      // zero, so it is not a limit: it is an always-on breach nobody meant.
      if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) {
        throw new Error(`METER_POLICY_JSON meter ${name} ${field} must be a finite number >= 0`);
      }
    }
    const warn = spec.warning_threshold;
    const hard = spec.hard_stop_threshold;
    // A warning above its hard stop can never fire: the hard stop is checked
    // first and wins. Advertising a warning that is unreachable by construction
    // is the same defect class as a threshold key that was never read.
    if (typeof warn === 'number' && typeof hard === 'number' && warn > hard) {
      throw new Error(`METER_POLICY_JSON meter ${name} warning_threshold must be <= hard_stop_threshold`);
    }
  }
  return deepFreeze(parsed);
}

export function resolveMeterPolicy(env) {
  const raw = env && env.METER_POLICY_JSON;
  if (raw === undefined || raw === null || raw === '') return DEFAULT_METER_POLICY;
  const cached = POLICIES.get(raw);
  if (cached) return cached;
  const parsed = validateMeterPolicy(JSON.parse(raw));
  POLICIES.set(raw, parsed);
  return parsed;
}

// The ONLY string shape a numeric setting is read from: plain decimal digits,
// optionally with a decimal fraction. Everything `Number()` would otherwise
// accept is refused on purpose, because each alternate grammar is a way for a
// value that is not a number to become one silently:
//   `" "`      -> Number(" ") is 0, which would set the refetch FLOOR to zero
//                 and disable the control that bounds outbound amplification
//   `true`     -> Number(true) is 1, a millisecond
//   `[1]`      -> Number([1]) is 1; `[]` is 0
//   `"0x10"`   -> 16, a base an operator writing a millisecond count never meant
//   `"1e3"`    -> 1000. Refused too: exponent notation is not how any of these
//                 knobs are written in wrangler.toml.example, and accepting one
//                 unusual grammar to reject another is arbitrary. If a future
//                 operator wants it, the fix is one character in this regex plus
//                 a documented example, not a silent coercion today.
//   `"Infinity"`, `"-1"`, `".5"`, `"1 "` -> all refused
const DECIMAL_SETTING_RE = /^\d+(\.\d+)?$/;

/**
 * Read one numeric tuning value from configuration. Absent (undefined, null or
 * the empty string) means the default. Anything present that is not a finite
 * non-negative number, or a non-blank plain decimal string, is a CONFIGURATION
 * ERROR, not something to coerce: a NaN threshold compares false against
 * everything and would silently disable the control it names, and a coerced 0
 * is worse still because it looks deliberate.
 *
 * The throw lands on the already-tested deploy-time 503 path, before any
 * request is served, and /healthz reports the configuration as invalid.
 */
export function numericSetting(env, name, fallback) {
  const raw = env && env[name];
  if (raw === undefined || raw === null || raw === '') return fallback;
  let value;
  if (typeof raw === 'number') {
    value = raw;
  } else if (typeof raw === 'string' && DECIMAL_SETTING_RE.test(raw)) {
    value = Number(raw);
  } else {
    throw new Error(`${name} must be a finite number >= 0`);
  }
  if (!Number.isFinite(value) || value < 0) {
    throw new Error(`${name} must be a finite number >= 0`);
  }
  return value;
}

// One verifier per isolate, keyed by the configuration it was built from. The
// JWKS cache and the refetch floor both live inside the verifier closure, so
// building one per request would throw both away and turn every inbound request
// into an outbound subrequest. A config change yields a different key and so a
// new verifier.
const VERIFIERS = new Map();

export function verifierForEnv(env, fetchImpl = (u, i) => fetch(u, i)) {
  const settings = {
    teamName: env.ACCESS_TEAM_NAME,
    aud: env.ACCESS_AUD,
    clockSkewSec: numericSetting(env, 'ACCESS_CLOCK_SKEW_SEC', DEFAULT_CLOCK_SKEW_SEC),
    jwksTtlMs: numericSetting(env, 'JWKS_CACHE_TTL_MS', DEFAULT_JWKS_TTL_MS),
    jwksMinRefetchMs: numericSetting(env, 'JWKS_MIN_REFETCH_MS', DEFAULT_MIN_REFETCH_MS),
    jwksStaleGraceMs: numericSetting(env, 'JWKS_STALE_GRACE_MS', DEFAULT_JWKS_STALE_GRACE_MS),
    jwksFetchTimeoutMs: numericSetting(env, 'JWKS_FETCH_TIMEOUT_MS', DEFAULT_JWKS_FETCH_TIMEOUT_MS),
  };
  // JSON, not a delimiter: a delimiter that can appear inside a value makes two
  // different configurations share one cache entry.
  const key = JSON.stringify(settings);
  let verifier = VERIFIERS.get(key);
  if (!verifier) {
    verifier = createAccessVerifier({ ...settings, fetchImpl, now: () => Date.now() });
    VERIFIERS.set(key, verifier);
  }
  return verifier;
}

export default {
  async fetch(request, env) {
    // The method gate runs BEFORE configuration, so the answer to an
    // unsupported verb is the same whether the Worker is healthy or not. It
    // used to sit inside createWorker only, so HEAD /healthz returned 405 under
    // a valid config and 503 under a broken one: an uptime monitor that HEADs
    // the health route saw the gate flip. One shape, always.
    if (!ALLOWED_METHODS.includes(request.method)) {
      return denialResponse('method_not_allowed', 405);
    }
    // Construction is inside the try. A malformed METER_POLICY_JSON or a
    // missing ACCESS_TEAM_NAME used to throw out of fetch() on every path,
    // including the only public one, which turns a typo into a platform
    // exception rather than a controlled refusal. It is now a 503 that carries
    // the private header set and says nothing about what was wrong. Deny by
    // default still holds: a private route under an invalid configuration is
    // refused, never served.
    let worker;
    try {
      worker = createWorker({
        verifier: verifierForEnv(env),
        now: () => Date.now(),
        meterPolicy: resolveMeterPolicy(env),
      });
    } catch {
      // The health route may say the configuration is invalid, because that is
      // the outage a health check exists to report. It says nothing else: no
      // key, no value, no parser message, no key set URL.
      if (isHealthPath(request)) return degradedHealthResponse();
      return unavailableResponse();
    }
    return worker.fetch(request);
  },
};

/** True when the request targets the public health route, judged safely. */
function isHealthPath(request) {
  try {
    return PUBLIC_PATHS.includes(new URL(request.url).pathname);
  } catch {
    return false;
  }
}
