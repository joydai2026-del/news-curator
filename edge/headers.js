// Response headers for anything that is not a public asset.
//
// Every authenticated response and every denial carries the same set, so a
// private record can never land in a shared cache and a denial can never be
// replayed from one. Reference (read 2026-09-02):
// https://developers.cloudflare.com/cache/concepts/cache-control/

export const PRIVATE_CACHE_CONTROL = 'private, no-store';
export const PRIVATE_VARY = 'Cookie, Cf-Access-Jwt-Assertion';
export const NOINDEX = 'noindex, nofollow';

export const REQUIRED_PRIVATE_HEADERS = Object.freeze({
  'cache-control': PRIVATE_CACHE_CONTROL,
  vary: PRIVATE_VARY,
  'x-robots-tag': NOINDEX,
});

/**
 * Force the private header set onto a Headers object, in place.
 * ETag is removed: a validator on a private response invites a revalidated
 * replay, and there is nothing to revalidate against.
 */
export function applyPrivateHeaders(headers) {
  headers.set('Cache-Control', PRIVATE_CACHE_CONTROL);
  headers.set('Vary', PRIVATE_VARY);
  headers.set('X-Robots-Tag', NOINDEX);
  headers.set('Referrer-Policy', 'no-referrer');
  headers.set('X-Content-Type-Options', 'nosniff');
  headers.delete('ETag');
  headers.delete('Last-Modified');
  return headers;
}

export function privateResponse(body, init = {}) {
  const res = new Response(body, init);
  applyPrivateHeaders(res.headers);
  return res;
}

export function privateJson(payload, init = {}) {
  const res = privateResponse(JSON.stringify(payload), init);
  res.headers.set('Content-Type', 'application/json; charset=utf-8');
  return res;
}

/** A denial. Reason codes are coarse on purpose: they never name a key or a claim value. */
export function denialResponse(reason, status = 401) {
  return privateJson({ error: 'unauthorized', reason }, { status });
}

/**
 * The refusal used when the Worker cannot be built at all: a malformed or
 * missing configuration value. The body is a constant. An exception message,
 * the offending value, and the key set URL all stay out of it, because the
 * caller who triggers this is unauthenticated by definition.
 */
export function unavailableResponse(reason = 'worker_unavailable') {
  return privateJson({ error: 'unavailable', reason }, { status: 503 });
}

/**
 * The health answer when construction failed. Public by design, so it says
 * only that the configuration is invalid. It never says which key, what value,
 * or what the parser complained about.
 */
export function degradedHealthResponse() {
  return privateJson({ status: 'degraded', config: 'invalid' }, { status: 503 });
}

/**
 * True when a response carries every header a private response must carry.
 * Both validators are checked: an ETag or a Last-Modified on a private body is
 * an invitation to revalidate and replay it, so either one fails the check.
 */
export function hasRequiredPrivateHeaders(response) {
  for (const [name, expected] of Object.entries(REQUIRED_PRIVATE_HEADERS)) {
    if (response.headers.get(name) !== expected) return false;
  }
  if (response.headers.get('etag') !== null) return false;
  return response.headers.get('last-modified') === null;
}
