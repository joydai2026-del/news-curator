// The positive control.
//
// A passing suite proves nothing unless it is capable of failing. Each test
// here breaks the edge layer in one specific way and asserts that the SAME
// assertion functions the real tests use go red. If any test in this file
// starts failing, the denial tests above it have stopped testing anything.

import test from 'node:test';
import assert from 'node:assert/strict';

import { createWorker } from './worker.js';
import { applyPrivateHeaders } from './headers.js';
import {
  assertForgedTokensDenied,
  assertSignedOutDenied,
  makeKeyMaterial,
  signToken,
  tamperSignature,
} from './_helpers.js';

const FIXED_NOW_MS = 1_756_000_000_000;
const nowSec = Math.floor(FIXED_NOW_MS / 1000);
const material = await makeKeyMaterial();

/** An edge layer with the auth check removed: the regression we most fear. */
const wideOpenWorker = {
  async fetch() {
    const res = new Response('SYNTHETIC PLACEHOLDER ALPHA', { status: 200 });
    applyPrivateHeaders(res.headers);
    return res;
  },
};

/** Denies, but lets the denial be cached and replayed. */
const cacheableDenialWorker = {
  async fetch() {
    return new Response('no', { status: 401, headers: { 'Cache-Control': 'public, max-age=600' } });
  },
};

test('CONTROL: a worker with no auth check makes the signed-out denial test fail', async () => {
  await assert.rejects(
    () => assertSignedOutDenied(wideOpenWorker),
    /must deny a signed-out request, got 200/,
    'the signed-out assertion must be able to go red',
  );
});

test('CONTROL: a cacheable denial makes the signed-out denial test fail', async () => {
  await assert.rejects(
    () => assertSignedOutDenied(cacheableDenialWorker),
    /must not be cacheable/,
    'the cache-header half of the assertion must be able to go red',
  );
});

test('CONTROL: a permissive verifier stub makes the forged-token test fail', async () => {
  // The stub says yes to everything, exactly as a broken verify() would.
  const permissiveWorker = createWorker({
    verifier: { verify: async () => ({ ok: true, claims: { sub: 'anyone' } }) },
    now: () => FIXED_NOW_MS,
  });
  const good = await signToken(material, { nowSec });
  const forged = [
    ['tampered', tamperSignature(good)],
    ['expired', await signToken(material, { nowSec, claims: { exp: nowSec - 3600, nbf: nowSec - 7200 } })],
    ['garbage', 'not.a.jwt'],
  ];
  await assert.rejects(
    () => assertForgedTokensDenied(permissiveWorker, forged),
    /must be denied, got 200/,
    'the forged-token assertion must be able to go red',
  );
});

test('CONTROL: the real worker under the same stub is the only difference', async () => {
  // Sanity: the permissive stub is what breaks it, not the route table. With
  // an always-deny verifier the same worker passes the forged-token assertion.
  const strictWorker = createWorker({
    verifier: { verify: async () => ({ ok: false, reason: 'bad_signature' }) },
    now: () => FIXED_NOW_MS,
  });
  await assertForgedTokensDenied(strictWorker, [['anything', 'a.b.c']]);
});
