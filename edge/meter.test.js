import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildLimitReceipt,
  CUMULATIVE,
  DEFAULT_METER_POLICY,
  PER_INVOCATION,
  PER_ISOLATE,
  ReceiptInputError,
} from './meter.js';

const NOW = 1_756_000_000_000;

const POLICY = {
  policy_revision: 7,
  staleness_ms: 900_000,
  meters: {
    requests_per_day: { meter_kind: CUMULATIVE, unit: 'requests', warning_threshold: 70000, hard_stop_threshold: 90000 },
    cpu_per_invocation: { meter_kind: PER_INVOCATION, unit: 'ms', warning_threshold: 8, hard_stop_threshold: 10 },
  },
};

function build(readings, extra = {}) {
  return buildLimitReceipt({
    policy: POLICY,
    readings,
    meterSource: 'host_analytics_api',
    now: NOW,
    receiptId: 'lrec-test-1',
    attributedOperationClass: 'slate_build',
    ...extra,
  });
}

function reading(receipt, name) {
  return receipt.readings.find((r) => r.meter === name);
}

// The frozen MeterReading contract carries no diagnostic `warning` boolean
// (fix round 7): a consumer derives the same between-thresholds condition from
// the fields the reading already has. This is that derivation, used only in
// tests to state the condition the removed flag used to name.
function isWarnState(row) {
  return (
    row.value !== null &&
    row.warning_threshold !== null &&
    row.value >= row.warning_threshold &&
    row.breached === false
  );
}

test('a missing reading is unknown with a null value, never zero', () => {
  const r = build({});
  for (const row of r.readings) {
    assert.equal(row.value, null, `${row.meter} must not report a number it never read`);
    assert.equal(row.freshness_verdict, 'unknown');
    assert.equal(row.sampled_at, null);
  }
  assert.equal(r.final_state, 'unknown');
  assert.equal(r.envelope.state, 'unknown');
  assert.equal(r.envelope.reason_code, 'meter_stale:requests_per_day,cpu_per_invocation', 'the code names WHICH claims are missing, not just that one is');
});

test('a stale reading drops its value rather than reporting an old number', () => {
  const r = build({ requests_per_day: { value: 88000, sampled_at: NOW - 3_600_000 } });
  const row = reading(r, 'requests_per_day');
  assert.equal(row.value, null);
  assert.equal(row.freshness_verdict, 'stale');
  assert.notEqual(row.freshness_verdict, 'fresh');
  assert.equal(row.sampled_at, new Date(NOW - 3_600_000).toISOString());
  assert.equal(r.final_state, 'unknown');
  assert.deepEqual(r.shed_actions, [], 'a value we do not trust cannot trigger a shed');
});

test('a fresh reading over the hard stop sheds and settles as hard_stop', () => {
  const r = build({
    requests_per_day: { value: 95000, sampled_at: NOW - 1000 },
    cpu_per_invocation: { value: 3, sampled_at: NOW - 1000 },
  });
  assert.equal(reading(r, 'requests_per_day').value, 95000);
  assert.equal(reading(r, 'requests_per_day').breached, true);
  assert.deepEqual(r.shed_actions, ['hard_stop:requests_per_day']);
  assert.equal(r.final_state, 'hard_stop');
  assert.equal(r.envelope.reason_code, 'budget_hard_stop');
});

test('a fresh reading over the warning threshold warns but does not hard stop', () => {
  const r = build({
    requests_per_day: { value: 71000, sampled_at: NOW - 1000 },
    cpu_per_invocation: { value: 3, sampled_at: NOW - 1000 },
  });
  assert.deepEqual(r.shed_actions, ['warn:requests_per_day']);
  assert.equal(r.final_state, 'ok');
  assert.equal(r.envelope.state, 'settled');
});

test('a breached per-invocation ceiling emits no shed action and never settles green', () => {
  const r = build({
    requests_per_day: { value: 10, sampled_at: NOW - 1000 },
    cpu_per_invocation: { value: 11, sampled_at: NOW - 1000 },
  });
  assert.equal(reading(r, 'cpu_per_invocation').breached, true);
  assert.deepEqual(r.shed_actions, [], 'shedding cannot rescue a runtime hard limit, so it must not claim to');
  assert.equal(r.final_state, 'ceiling_breached', 'a blown ceiling is not a green receipt');
  assert.equal(r.envelope.state, 'failed');
  assert.equal(r.envelope.reason_code, 'ceiling_breached:cpu_per_invocation', 'the reason code names the meter that blew');
  assert.equal(r.envelope.settled_at, null);
});

test('a proven hard stop outranks an unproven reading', () => {
  const r = build({ requests_per_day: { value: 95000, sampled_at: NOW - 1000 } });
  assert.equal(reading(r, 'cpu_per_invocation').freshness_verdict, 'unknown');
  assert.equal(r.final_state, 'hard_stop');
});

test('the receipt carries the envelope fields the contract requires', () => {
  const r = build({});
  assert.deepEqual(Object.keys(r.envelope).sort(), [
    'actor_id', 'actor_kind', 'created_at', 'kind', 'policy_revision',
    'reason_code', 'receipt_id', 'settled_at', 'state', 'tenant_id', 'user_id',
  ]);
  assert.equal(r.envelope.policy_revision, 7, 'thresholds and revision come from policy, not from source');
  assert.equal(r.attributed_operation_class, 'slate_build');
});

test('an unreadable meter source is recorded as unavailable, not omitted', () => {
  const r = build({}, { meterSource: 'unavailable' });
  assert.equal(r.meter_source, 'unavailable');
});

test('the default policy names every meter the criteria require', () => {
  assert.deepEqual(Object.keys(DEFAULT_METER_POLICY.meters).sort(), [
    'cpu_per_invocation', 'cron_triggers_used', 'memory_failures',
    'requests_per_day', 'subrequests_per_request',
  ]);
});

test('a reading stamped in the future is not treated as fresh', () => {
  const r = build({ requests_per_day: { value: 5, sampled_at: NOW + 60_000 } });
  assert.equal(reading(r, 'requests_per_day').value, null);
  assert.notEqual(reading(r, 'requests_per_day').freshness_verdict, 'fresh');
});

test('staleness_ms: 0 is honored, not swallowed by a falsy default', () => {
  const r = buildLimitReceipt({
    policy: { policy_revision: 7, staleness_ms: 0, meters: { requests_per_day: POLICY.meters.requests_per_day } },
    readings: { requests_per_day: { value: 5, sampled_at: NOW - 1 } },
    meterSource: 'host_analytics_api',
    now: NOW,
    receiptId: 'lrec-test-zero',
    attributedOperationClass: 'slate_build',
  });
  const row = reading(r, 'requests_per_day');
  assert.equal(row.freshness_verdict, 'stale', 'a 1 ms old read is stale when the window is 0');
  assert.equal(row.value, null, 'a policy that says only an instantaneous read counts must not accept a 1 ms old number');
});

test('an out-of-range or non-numeric sampled_at is unknown, not a RangeError', () => {
  for (const stamp of [9e15, '1756000000000', Infinity, -1]) {
    const r = build({ requests_per_day: { value: 5, sampled_at: stamp } });
    const row = reading(r, 'requests_per_day');
    assert.equal(row.freshness_verdict, 'unknown', `sampled_at ${String(stamp)} must not be treated as a clock reading`);
    assert.equal(row.value, null);
    assert.equal(row.sampled_at, null);
  }
});

test('a missing policy_revision throws a typed error rather than dropping the key', () => {
  assert.throws(
    () => buildLimitReceipt({
      policy: { staleness_ms: 900_000, meters: {} },
      readings: {},
      meterSource: 'host_analytics_api',
      now: NOW,
      receiptId: 'lrec-test-2',
      attributedOperationClass: 'slate_build',
    }),
    ReceiptInputError,
  );
});

test('a missing attributed_operation_class throws a typed error', () => {
  assert.throws(
    () => buildLimitReceipt({
      policy: POLICY,
      readings: {},
      meterSource: 'host_analytics_api',
      now: NOW,
      receiptId: 'lrec-test-3',
    }),
    ReceiptInputError,
  );
});

test('memory is metered as a per-isolate ceiling, not a per-invocation one', () => {
  assert.equal(DEFAULT_METER_POLICY.meters.memory_failures.meter_kind, PER_ISOLATE);
  assert.equal(DEFAULT_METER_POLICY.meters.cpu_per_invocation.meter_kind, PER_INVOCATION);
  assert.equal(DEFAULT_METER_POLICY.meters.subrequests_per_request.meter_kind, PER_INVOCATION);
  assert.equal(DEFAULT_METER_POLICY.meters.requests_per_day.meter_kind, CUMULATIVE);
});

test('a breached per-isolate ceiling behaves like a per-invocation one: no shed, not green', () => {
  const r = buildLimitReceipt({
    policy: {
      policy_revision: 7,
      staleness_ms: 900_000,
      meters: { memory_failures: DEFAULT_METER_POLICY.meters.memory_failures },
    },
    readings: { memory_failures: { value: 1, sampled_at: NOW - 1000 } },
    meterSource: 'host_analytics_api',
    now: NOW,
    receiptId: 'lrec-test-4',
    attributedOperationClass: 'slate_build',
  });
  assert.deepEqual(r.shed_actions, []);
  assert.equal(r.final_state, 'ceiling_breached');
  assert.equal(r.envelope.reason_code, 'ceiling_breached:memory_failures');
});

// --- round 2: breach precedence, diagnostic warnings, and the pinned shape ---

test('a breached ceiling dominates a co-occurring budget hard stop', () => {
  const r = build({
    requests_per_day: { value: 95000, sampled_at: NOW - 1000 },
    cpu_per_invocation: { value: 9999, sampled_at: NOW - 1000 },
  });
  assert.equal(r.final_state, 'ceiling_breached', 'a runtime failure nothing can shed outranks a recoverable budget stop');
  assert.equal(r.envelope.state, 'failed');
  assert.equal(r.envelope.settled_at, null, 'a blown ceiling must never carry a settled timestamp');
  assert.match(r.envelope.reason_code, /^ceiling_breached:cpu_per_invocation/, 'the reason names the ceiling that blew');
  assert.match(r.envelope.reason_code, /budget_hard_stop/, 'the budget stop is still recorded, not erased');
  assert.deepEqual(r.shed_actions, ['hard_stop:requests_per_day'], 'the shed action for the budget is still listed');
});

test('the same two breaches read in the opposite order settle the same way', () => {
  // Object key order is the iteration order of the readings map, so this is the
  // reverse of the test above and must not change the verdict.
  const r = build({
    cpu_per_invocation: { value: 9999, sampled_at: NOW - 1000 },
    requests_per_day: { value: 95000, sampled_at: NOW - 1000 },
  });
  assert.equal(r.final_state, 'ceiling_breached');
  assert.equal(r.envelope.state, 'failed');
  assert.equal(r.envelope.settled_at, null);
  assert.deepEqual(r.shed_actions, ['hard_stop:requests_per_day']);
});

test('a ceiling between its warning and its hard stop is derivable as a warning with no shed action', () => {
  const r = build({ cpu_per_invocation: { value: 9, sampled_at: NOW - 1000 } });
  const cpu = reading(r, 'cpu_per_invocation');
  assert.equal(cpu.breached, false, '9 ms is under the 10 ms hard stop');
  assert.equal(isWarnState(cpu), true, 'a published warning_threshold must be able to fire on a ceiling');
  assert.equal(cpu.warning_threshold, 8);
  assert.ok(!('warning' in cpu), 'the frozen contract carries no warning key; a consumer derives it');
  assert.deepEqual(r.shed_actions, [], 'shedding cannot rescue a ceiling, so no action may be named');
  assert.equal(r.final_state, 'unknown', 'the other meter is unread, so the receipt is not green either way');
});

test('a warning on a budget still emits its shed action, and a breach is not also a warning', () => {
  const warned = build({ requests_per_day: { value: 70000, sampled_at: NOW - 1000 } });
  assert.equal(isWarnState(reading(warned, 'requests_per_day')), true);
  assert.deepEqual(warned.shed_actions, ['warn:requests_per_day']);
  const breached = build({ requests_per_day: { value: 95000, sampled_at: NOW - 1000 } });
  assert.equal(reading(breached, 'requests_per_day').breached, true);
  assert.equal(isWarnState(reading(breached, 'requests_per_day')), false, 'past the hard stop it is a breach, not a warning');
});

test('an unread meter is neither breached nor in the derivable warning state', () => {
  const r = build({});
  assert.equal(isWarnState(reading(r, 'cpu_per_invocation')), false);
  assert.equal(reading(r, 'cpu_per_invocation').breached, false);
});

// ---------------------------------------------------------------------------
// Round 2, item 4: the envelope shape is pinned to a committed sample.
//
// Every other test here asserts a field it happens to care about, which means a
// key could be added, renamed or dropped and the suite would stay green while
// the Python contract on the other side stopped matching. This test builds a
// receipt from fixed inputs and compares the WHOLE serialization, byte for
// byte, against edge/fixtures/limit-receipt.sample.json.
//
// The sample is NEVER written by the suite. Deleting it makes this test red
// (ENOENT), not green. Regenerating it is an explicit, separate act:
//   node edge/scripts/regenerate-fixtures.mjs
// and CI runs `git diff --exit-code -- edge/fixtures` after the suite, so a run
// that changes a fixture by any route cannot exit 0.
//
// NOTE FOR THE MERGE: the Python-side contract validation of this same sample
// (feeding it to curator/contracts/receipt.py and asserting it validates) is
// added in the main checkout at merge time by the lead. It is not here because
// this worktree carries no Python, and the Ownership fields (actor_kind,
// user_id) are ahead of the frozen contract until that same branch set lands.
// The MeterReading shape itself carries no such gap as of fix round 7: the
// diagnostic `warning` flag was removed from the wire rather than added to the
// contract, so every reading key here already matches curator/contracts/receipt.py.
// ---------------------------------------------------------------------------

import { readFileSync } from 'node:fs';

import { buildSampleReceipt, SAMPLE_FIXTURE_PATH as SAMPLE_PATH } from './_helpers.js';

test('the receipt envelope matches the committed sample byte for byte', () => {
  const serialized = `${JSON.stringify(buildSampleReceipt(), null, 2)}\n`;
  // readFileSync with NO existence check on purpose. Writing the file when it
  // was absent turned a deleted fixture into a green run plus a silently
  // rewritten sample, which is exactly the failure a pinning test exists to
  // prevent: the guard evaporated the moment its own authority disappeared.
  // A missing fixture is now ENOENT, which is red. Regeneration is a deliberate
  // act: node edge/scripts/regenerate-fixtures.mjs.
  assert.equal(
    serialized,
    readFileSync(SAMPLE_PATH, 'utf8'),
    'the emitted receipt drifted from edge/fixtures/limit-receipt.sample.json: reconcile with the Python contract before changing the sample, then run node edge/scripts/regenerate-fixtures.mjs',
  );
});

test('the committed sample carries the four ownership keys the envelope contract requires', () => {
  const sample = JSON.parse(readFileSync(SAMPLE_PATH, 'utf8'));
  for (const key of ['tenant_id', 'actor_id', 'actor_kind', 'user_id']) {
    assert.ok(key in sample.envelope, `the sample envelope must carry ${key}`);
  }
  assert.equal(sample.envelope.actor_kind, 'system');
  assert.equal(sample.envelope.user_id, null, 'user_id is null precisely because the actor kind is system');
  // The sample is only worth pinning if it exercises the states that matter.
  const byName = Object.fromEntries(sample.readings.map((r) => [r.meter, r]));
  assert.equal(isWarnState(byName.requests_per_day), true, 'the sample pins a reading between its warning and hard-stop thresholds');
  assert.ok(!('warning' in byName.requests_per_day), 'the frozen contract carries no warning key on the wire');
  assert.equal(byName.memory_failures.value, null, 'the sample pins an unread meter as null, never 0');
  assert.equal(byName.memory_failures.freshness_verdict, 'unknown');
});

// ---------------------------------------------------------------------------
// Round 3, items 4 and 5: a receipt that measured nothing, and a reading that
// cannot carry its required fields.
// ---------------------------------------------------------------------------

test('a policy with zero meters yields unknown, never a settled green receipt', () => {
  // The exact previously-green input: the commented example that shipped in
  // edge/wrangler.toml.example. It used to return final_state ok, state
  // settled, and a settled_at timestamp, from a receipt with no readings at all.
  const r = buildLimitReceipt({
    policy: JSON.parse('{"policy_revision":1,"staleness_ms":900000,"meters":{}}'),
    readings: {},
    meterSource: 'unavailable',
    now: NOW,
    receiptId: 'lrec-empty-1',
    attributedOperationClass: 'edge_request',
  });
  assert.deepEqual(r.readings, []);
  assert.equal(r.final_state, 'unknown', 'a receipt that measured nothing cannot claim ok');
  assert.equal(r.envelope.state, 'unknown');
  assert.equal(r.envelope.settled_at, null, 'nothing was settled, so there is no settled timestamp');
  assert.equal(r.envelope.reason_code, 'no_meters_configured');
});

test('an unread required meter is named in the reason code, not just counted', () => {
  const r = build({ requests_per_day: { value: 5, sampled_at: NOW - 1000 } });
  assert.equal(r.final_state, 'unknown');
  assert.equal(r.envelope.state, 'unknown');
  assert.equal(r.envelope.settled_at, null);
  assert.equal(r.envelope.reason_code, 'meter_stale:cpu_per_invocation', 'every configured meter is required');
});

test('a meter spec missing its unit is a refusal, never a reading with the key dropped', () => {
  // JSON.stringify drops an undefined value, so this used to publish a reading
  // with no `unit` key at all: the exact artifact the fixture and the merge-time
  // Python contract test exist to pin.
  assert.throws(
    () => buildLimitReceipt({
      policy: { policy_revision: 1, meters: { a: { meter_kind: CUMULATIVE, hard_stop_threshold: 10 } } },
      readings: {},
      meterSource: 'unavailable',
      now: NOW,
      receiptId: 'lrec-noshape-1',
      attributedOperationClass: 'edge_request',
    }),
    ReceiptInputError,
  );
});

test('a meter spec that is not an object at all is a refusal, not a TypeError', () => {
  for (const spec of [null, 'nope', 42, []]) {
    assert.throws(
      () => buildLimitReceipt({
        policy: { policy_revision: 1, meters: { x: spec } },
        readings: {},
        meterSource: 'unavailable',
        now: NOW,
        receiptId: 'lrec-noshape-2',
        attributedOperationClass: 'edge_request',
      }),
      ReceiptInputError,
      `spec ${JSON.stringify(spec)} must be a typed refusal`,
    );
  }
});

test('a well-formed reading carries all nine required fields, every one the right type', () => {
  const r = build({ requests_per_day: { value: 71000, sampled_at: NOW - 1000 } });
  assert.deepEqual(Object.keys(reading(r, 'requests_per_day')).sort(), [
    'breached', 'freshness_verdict', 'hard_stop_threshold', 'meter', 'meter_kind',
    'sampled_at', 'unit', 'value', 'warning_threshold',
  ]);
});

// Fix round 7: the frozen curator/contracts/receipt.py MeterReading has these
// nine fields and no others. This test fails the moment ANY reading, in ANY
// state (fresh, stale, unknown, breached, warned), publishes a tenth key: the
// contract test in the main checkout would reject it as `unknown field`, and
// that failure is cheaper to catch here, in the module that builds the wire
// shape, than after a merge.
const FROZEN_READING_KEYS = Object.freeze([
  'meter', 'meter_kind', 'value', 'unit', 'freshness_verdict',
  'sampled_at', 'warning_threshold', 'hard_stop_threshold', 'breached',
].sort());

test('every emitted reading has exactly the nine frozen MeterReading keys and no others', () => {
  const r = buildLimitReceipt({
    policy: DEFAULT_METER_POLICY,
    readings: {
      requests_per_day: { value: 71000, sampled_at: NOW - 1000 }, // warn state
      cpu_per_invocation: { value: 11, sampled_at: NOW - 1000 }, // breached ceiling
      memory_failures: { value: -1, sampled_at: NOW - 1000 }, // impossible -> unread
      // subrequests_per_request, cron_triggers_used left unread entirely
    },
    meterSource: 'host_analytics_api',
    now: NOW,
    receiptId: 'lrec-frozen-keys',
    attributedOperationClass: 'edge_request',
  });
  assert.ok(r.readings.length > 0, 'the fixture must actually exercise readings');
  for (const row of r.readings) {
    assert.deepEqual(
      Object.keys(row).sort(),
      FROZEN_READING_KEYS,
      `meter "${row.meter}" must carry exactly the frozen MeterReading keys`,
    );
  }
});

// ---------------------------------------------------------------------------
// Round 4, item 5: a breach must not ERASE an unread meter.
//
// The hard-stop branch ran before the unread-meter branch, so a receipt claimed
// `settled` with a settled timestamp while 3 of 4 required meters were never
// read, and the reason code named none of them. The verdict stays authoritative
// (a consumer acting only on settled receipts must never miss a real hard stop);
// the missing claims are now recorded alongside it.
// ---------------------------------------------------------------------------

test('a budget hard stop keeps its verdict AND still names the meters that were never read', () => {
  const policy = {
    policy_revision: 4,
    staleness_ms: 900_000,
    meters: {
      budget: { meter_kind: CUMULATIVE, unit: 'requests', warning_threshold: 70000, hard_stop_threshold: 90000 },
      cpu: { meter_kind: PER_INVOCATION, unit: 'ms', hard_stop_threshold: 10 },
      memory: { meter_kind: PER_ISOLATE, unit: 'events', hard_stop_threshold: 1 },
      subrequests: { meter_kind: PER_INVOCATION, unit: 'subrequests', hard_stop_threshold: 50 },
    },
  };
  const receipt = buildLimitReceipt({
    policy,
    readings: { budget: { value: 95000, sampled_at: NOW - 1000 } },
    meterSource: 'host_analytics_api',
    now: NOW,
    receiptId: 'lrec-r4-hardstop',
    attributedOperationClass: 'edge_request',
  });

  // The stop is not downgraded: shedding still has to happen, and a downstream
  // consumer that filters on `settled` must still see it.
  assert.equal(receipt.final_state, 'hard_stop');
  assert.equal(receipt.envelope.state, 'settled');
  assert.ok(receipt.envelope.settled_at, 'a real hard stop keeps its settled timestamp');
  assert.deepEqual(receipt.shed_actions, ['hard_stop:budget']);

  // And the hole in the claim is named, not swallowed.
  assert.equal(receipt.envelope.reason_code, 'budget_hard_stop;meter_stale:cpu,memory,subrequests');
  const unread = receipt.readings.filter((r) => r.freshness_verdict === 'unknown').map((r) => r.meter);
  assert.deepEqual(unread, ['cpu', 'memory', 'subrequests'], '3 of 4 required meters were never read');
});

test('a breached ceiling names the unread meters too, after the budget hard stop it already names', () => {
  const policy = {
    policy_revision: 4,
    staleness_ms: 900_000,
    meters: {
      budget: { meter_kind: CUMULATIVE, unit: 'requests', hard_stop_threshold: 90000 },
      cpu: { meter_kind: PER_INVOCATION, unit: 'ms', hard_stop_threshold: 10 },
      memory: { meter_kind: PER_ISOLATE, unit: 'events', hard_stop_threshold: 1 },
    },
  };
  const receipt = buildLimitReceipt({
    policy,
    readings: {
      budget: { value: 95000, sampled_at: NOW - 1000 },
      cpu: { value: 4000, sampled_at: NOW - 1000 },
    },
    meterSource: 'host_analytics_api',
    now: NOW,
    receiptId: 'lrec-r4-ceiling',
    attributedOperationClass: 'edge_request',
  });
  assert.equal(receipt.final_state, 'ceiling_breached');
  assert.equal(receipt.envelope.state, 'failed');
  assert.equal(receipt.envelope.settled_at, null);
  assert.equal(receipt.envelope.reason_code, 'ceiling_breached:cpu;budget_hard_stop;meter_stale:memory');
});

test('a breach with every meter read fresh carries no meter_stale suffix', () => {
  const policy = {
    policy_revision: 4,
    staleness_ms: 900_000,
    meters: {
      budget: { meter_kind: CUMULATIVE, unit: 'requests', hard_stop_threshold: 90000 },
      cpu: { meter_kind: PER_INVOCATION, unit: 'ms', hard_stop_threshold: 10 },
    },
  };
  const receipt = buildLimitReceipt({
    policy,
    readings: {
      budget: { value: 95000, sampled_at: NOW - 1000 },
      cpu: { value: 2, sampled_at: NOW - 1000 },
    },
    meterSource: 'host_analytics_api',
    now: NOW,
    receiptId: 'lrec-r4-clean',
    attributedOperationClass: 'edge_request',
  });
  assert.equal(receipt.envelope.reason_code, 'budget_hard_stop', 'no hole in the claim, no suffix');
});

// ---------------------------------------------------------------------------
// Round 5, Codex 1: an IMPOSSIBLE reading must never settle a receipt green.
// Every meter kind here counts something (a budget spent, CPU milliseconds,
// subrequests, memory failures), so a negative value is not a low number, it is
// a broken meter. It used to be kept verbatim and compared against the
// thresholds, where -1 is below every one of them, so meters reading -1
// produced final_state ok, state settled and a settled timestamp.
// ---------------------------------------------------------------------------

test('a negative reading is an unread meter, never a fresh one that settles green', () => {
  const r = build({
    requests_per_day: { value: -1, sampled_at: NOW },
    cpu_per_invocation: { value: -1, sampled_at: NOW },
  });
  for (const row of r.readings) {
    assert.equal(row.value, null, `${row.meter} must not publish a value no meter could produce`);
    assert.equal(row.freshness_verdict, 'unknown', `${row.meter} is unread, not fresh`);
    assert.equal(row.breached, false);
    assert.equal(isWarnState(row), false);
  }
  assert.equal(r.final_state, 'unknown', 'an impossible reading can never settle');
  assert.equal(r.envelope.settled_at, null, 'nothing was measured, so there is no settled timestamp');
  assert.equal(
    r.envelope.reason_code,
    'meter_stale:requests_per_day,cpu_per_invocation',
    'the reason code must name the meters an impossible value left unread',
  );
});

test('every non-finite and negative value shape is refused as a reading', () => {
  for (const bad of [-1, -0.0001, Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY, NaN]) {
    const r = build({ requests_per_day: { value: bad, sampled_at: NOW } });
    const row = reading(r, 'requests_per_day');
    assert.equal(row.value, null, `value ${String(bad)} must not be published`);
    assert.equal(row.freshness_verdict, 'unknown', `value ${String(bad)} must not verdict fresh`);
    assert.equal(r.final_state, 'unknown', `value ${String(bad)} must not settle the receipt`);
  }
  // Zero is a legitimate reading and must keep working: strictness that refused
  // a real "we used nothing yet" would be a worse bug than the one it closes.
  const zero = build({
    requests_per_day: { value: 0, sampled_at: NOW },
    cpu_per_invocation: { value: 0, sampled_at: NOW },
  });
  assert.equal(reading(zero, 'requests_per_day').value, 0, 'zero is measured, not impossible');
  assert.equal(reading(zero, 'requests_per_day').freshness_verdict, 'fresh');
  assert.equal(zero.final_state, 'ok');
  assert.equal(zero.envelope.state, 'settled');
});

// ---------------------------------------------------------------------------
// Round 5, S1: the meter-id grammar is enforced HERE, in the module that
// interpolates ids into reason codes, not only at the configuration door.
// ---------------------------------------------------------------------------

test('a forged meter id is refused by the receipt builder, not just by the config door', () => {
  const forged = {
    policy_revision: 7,
    meters: {
      'partner_acquisition_cost;budget_hard_stop': { meter_kind: CUMULATIVE, unit: 'usd', hard_stop_threshold: 100 },
    },
  };
  assert.throws(
    () => build({}, { policy: forged }),
    ReceiptInputError,
    'an id that can forge the reason-code grammar must never reach a receipt',
  );

  const bad = ['a,b', 'a:b', 'A_meter', '_leading', '1meter', 'a'.repeat(65), 'has space'];
  for (const name of bad) {
    assert.throws(
      () => build({}, { policy: { policy_revision: 7, meters: { [name]: { meter_kind: CUMULATIVE, unit: 'u' } } } }),
      ReceiptInputError,
      `meter id ${JSON.stringify(name)} must be refused by the builder`,
    );
  }
  // The legal grammar still builds, including the maximum legal length.
  const longest = 'a' + 'b'.repeat(63);
  const ok = build({}, { policy: { policy_revision: 7, meters: { [longest]: { meter_kind: CUMULATIVE, unit: 'u' } } } });
  assert.equal(ok.readings.length, 1, 'a 64-character lowercase snake-case id is legal');
});

// ---------------------------------------------------------------------------
// Round 6, MF-2: DEFAULT_METER_POLICY is the SHIPPED default, handed to every
// request in an isolate whenever nobody sets METER_POLICY_JSON. Object.freeze
// was applied to the policy object and to its meters map but NOT to the
// individual spec objects, so a hard_stop_threshold write silently landed on
// the path that runs when an operator configures nothing at all.
// ---------------------------------------------------------------------------

test('the default policy is frozen all the way down, specs included', () => {
  assert.ok(Object.isFrozen(DEFAULT_METER_POLICY), 'the policy object');
  assert.ok(Object.isFrozen(DEFAULT_METER_POLICY.meters), 'the meters map');
  for (const [name, spec] of Object.entries(DEFAULT_METER_POLICY.meters)) {
    assert.ok(Object.isFrozen(spec), `the ${name} spec must be frozen too`);
  }
});

test('mutating a default meter spec throws instead of re-tuning every later request', () => {
  const before = DEFAULT_METER_POLICY.meters.requests_per_day.hard_stop_threshold;
  assert.throws(
    () => {
      DEFAULT_METER_POLICY.meters.requests_per_day.hard_stop_threshold = 99_999_999;
    },
    TypeError,
    'a threshold write must throw in strict mode, not land silently',
  );
  assert.throws(() => {
    DEFAULT_METER_POLICY.meters.requests_per_day.unit = 'pwned';
  }, TypeError);
  assert.throws(() => {
    DEFAULT_METER_POLICY.meters.new_meter = { meter_kind: CUMULATIVE, unit: 'x' };
  }, TypeError);
  assert.equal(DEFAULT_METER_POLICY.meters.requests_per_day.hard_stop_threshold, before, 'nothing changed');
  assert.equal(DEFAULT_METER_POLICY.meters.requests_per_day.unit, 'requests');
});

// ---------------------------------------------------------------------------
// Round 6, NEW-4: the builder enforces staleness and threshold sanity too, not
// only the configuration door. Same argument METER_ID_RE already settled: the
// door is not the only way into the room, and the builder is what acts on these
// numbers. Each case below produced a FALSE GREEN before the check existed.
// ---------------------------------------------------------------------------

test('a freshness window wider than a day is refused by the builder, not settled green', () => {
  assert.throws(
    () => build({ requests_per_day: { value: 1, sampled_at: 0 } }, { policy: { ...POLICY, staleness_ms: 1e15 } }),
    ReceiptInputError,
    'staleness_ms 1e15 used to verdict a reading sampled at the UNIX epoch as fresh and settle the receipt',
  );
  assert.throws(
    () => build({}, { policy: { ...POLICY, staleness_ms: 86_400_001 } }),
    /staleness_ms must be <= 86400000/,
  );
  assert.throws(() => build({}, { policy: { ...POLICY, staleness_ms: -1 } }), /staleness_ms must be a finite number >= 0/);
  assert.throws(() => build({}, { policy: { ...POLICY, staleness_ms: Number.NaN } }), ReceiptInputError);
  // Exactly the cap still builds.
  const ok = build({}, { policy: { ...POLICY, staleness_ms: 86_400_000 } });
  assert.equal(ok.final_state, 'unknown', 'no readings, so still unknown, but the policy itself is legal');
});

test('a negative threshold is refused by the builder, so a legitimate zero is never a breach', () => {
  const policy = {
    ...POLICY,
    meters: { alpha: { meter_kind: CUMULATIVE, unit: 'requests', hard_stop_threshold: -1 } },
  };
  assert.throws(
    () => build({ alpha: { value: 0, sampled_at: NOW } }, { policy }),
    /hard_stop_threshold must be a finite number >= 0/,
    'a reading of 0 used to become breached: true, final_state hard_stop',
  );
  assert.throws(
    () =>
      build(
        {},
        { policy: { ...POLICY, meters: { alpha: { meter_kind: CUMULATIVE, unit: 'r', warning_threshold: -5 } } } },
      ),
    /warning_threshold must be a finite number >= 0/,
  );
});

test('a warning above its hard stop is refused by the builder, because it could never fire', () => {
  const policy = {
    ...POLICY,
    meters: { alpha: { meter_kind: CUMULATIVE, unit: 'requests', warning_threshold: 90, hard_stop_threshold: 10 } },
  };
  assert.throws(
    () => build({ alpha: { value: 5, sampled_at: NOW } }, { policy }),
    /warning_threshold must be <= hard_stop_threshold/,
  );
});
