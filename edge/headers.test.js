import test from 'node:test';
import assert from 'node:assert/strict';

import {
  applyPrivateHeaders,
  denialResponse,
  hasRequiredPrivateHeaders,
  privateJson,
  privateResponse,
} from './headers.js';

test('a private response is uncacheable, unindexed and carries no validator', () => {
  const res = privateResponse('body', { headers: { ETag: '"abc"', 'Last-Modified': 'x' } });
  assert.equal(res.headers.get('cache-control'), 'private, no-store');
  assert.equal(res.headers.get('vary'), 'Cookie, Cf-Access-Jwt-Assertion');
  assert.equal(res.headers.get('x-robots-tag'), 'noindex, nofollow');
  assert.equal(res.headers.get('etag'), null, 'a validator on a private body invites a replay');
  assert.equal(res.headers.get('last-modified'), null);
  assert.ok(hasRequiredPrivateHeaders(res));
});

test('applyPrivateHeaders overwrites a permissive Cache-Control already set', () => {
  const headers = new Headers({ 'Cache-Control': 'public, max-age=31536000' });
  applyPrivateHeaders(headers);
  assert.equal(headers.get('cache-control'), 'private, no-store');
});

test('a denial carries the same private headers as a success', () => {
  const res = denialResponse('bad_aud');
  assert.equal(res.status, 401);
  assert.ok(hasRequiredPrivateHeaders(res));
});

test('a denial body names a coarse reason and no claim value', async () => {
  const body = await denialResponse('bad_aud').json();
  assert.equal(body.error, 'unauthorized');
  assert.equal(body.reason, 'bad_aud');
  assert.deepEqual(Object.keys(body).sort(), ['error', 'reason']);
});

test('hasRequiredPrivateHeaders rejects a response missing any one header', () => {
  const res = privateJson({ a: 1 });
  assert.ok(hasRequiredPrivateHeaders(res));
  res.headers.set('Cache-Control', 'public');
  assert.equal(hasRequiredPrivateHeaders(res), false);
});

test('hasRequiredPrivateHeaders rejects a response carrying a Last-Modified validator', () => {
  const res = privateJson({ a: 1 });
  assert.ok(hasRequiredPrivateHeaders(res));
  res.headers.set('Last-Modified', 'Tue, 02 Sep 2026 00:00:00 GMT');
  assert.equal(
    hasRequiredPrivateHeaders(res),
    false,
    'a Last-Modified validator on a private body invites a revalidated replay',
  );
});

test('hasRequiredPrivateHeaders rejects a response carrying an ETag validator', () => {
  const res = privateJson({ a: 1 });
  res.headers.set('ETag', '"abc"');
  assert.equal(hasRequiredPrivateHeaders(res), false);
});
