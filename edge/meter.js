// Builds a LimitReceipt-shaped record from an injected meter reader.
//
// The rule this file exists to enforce: a meter we could not read is unknown,
// never zero. A zero would read as "we used nothing" and would silently unlock
// spending we never measured.
//
// Three meter kinds, and they are not interchangeable:
//   cumulative_budget      a running total over a window. Can be shed: stop
//                          doing optional work before the budget is spent.
//   per_invocation_ceiling a runtime hard limit on a single invocation (CPU
//                          milliseconds, subrequests per request). Shedding
//                          cannot rescue it. The work fits or the fallback runs
//                          elsewhere. No shed action is emitted, and a breach
//                          is never a green receipt.
//   per_isolate_ceiling    a runtime hard limit on the ISOLATE, not on one
//                          request. Memory is documented as per isolate, so an
//                          out-of-memory event is a property of the isolate that
//                          several invocations share. Same shed rule as a
//                          per-invocation ceiling; the kind is separate because
//                          the blast radius and the owner are different.
//
// Field names match curator/contracts/receipt.py (MeterReading, LimitReceipt,
// ReceiptEnvelope). This module emits plain JSON only, it imports nothing.
//
// Envelope note: ReceiptEnvelope inherits the required Ownership shape
// (tenant_id, actor_id, actor_kind, user_id) that lands in the same branch set
// as this file, so actor_kind and user_id are required envelope keys here, not
// extras. user_id is null only because the actor kind is `system`.
//
// Two keys this module emits are AHEAD of the frozen Python contract today and
// need it to land before the merge-time contract test can pass:
//   ReceiptEnvelope: actor_kind, user_id   (the Ownership change, adjudicated)
// A MeterReading carries no diagnostic `warning` flag: the frozen contract has
// no such field, and a consumer can derive the same condition from `value`
// sitting between `warning_threshold` and `hard_stop_threshold` with
// `breached: false`. Fix round 7 dropped the flag from the wire shape rather
// than widening the contract.
// The exact emitted shape is pinned by edge/fixtures/limit-receipt.sample.json.

export const FRESH = 'fresh';
export const STALE = 'stale';
export const UNKNOWN = 'unknown';

export const CUMULATIVE = 'cumulative_budget';
export const PER_INVOCATION = 'per_invocation_ceiling';
export const PER_ISOLATE = 'per_isolate_ceiling';

const DEFAULT_STALENESS_MS = 15 * 60 * 1000;

// A stamp outside this range is not a clock reading, it is a bug or an attack.
// Date.prototype.toISOString throws a RangeError past +-8.64e15, and a real
// sample can never predate the epoch or land in the next century.
const MIN_SAMPLE_MS = 0;
const MAX_SAMPLE_MS = Date.UTC(2100, 0, 1);

// Upper sanity bound on the freshness window. `900000000` (three extra zeros on
// 15 minutes) is 10.4 days, and at that width a reading sampled at the UNIX
// epoch verdicts `fresh` and settles the receipt green. A day is far past any
// real sampling interval, so refusing above it costs nothing an operator meant
// to do. Enforced at the configuration door in worker.js AND in the builder
// below, for the same reason METER_ID_RE is: the door is not the only way into
// the room.
export const MAX_STALENESS_MS = 24 * 60 * 60 * 1000;

/**
 * Freeze a validated policy all the way down.
 *
 * A policy object is handed to EVERY request in an isolate, so one stray write
 * anywhere downstream would change the thresholds for every later request that
 * isolate serves, and nothing would record it. This lives in meter.js, the
 * module that owns DEFAULT_METER_POLICY, and worker.js imports it for the
 * env-derived policy, so the two paths cannot drift. Nothing mutates a policy
 * today: this is defense in depth, and in strict mode (every module here is
 * one) a write now throws instead of silently landing.
 */
export function deepFreeze(value) {
  if (value === null || typeof value !== 'object') return value;
  for (const key of Object.keys(value)) deepFreeze(value[key]);
  return Object.freeze(value);
}

/** Thrown when a caller omits an input the frozen receipt contract requires. */
export class ReceiptInputError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ReceiptInputError';
  }
}

export const METER_KINDS = Object.freeze([CUMULATIVE, PER_INVOCATION, PER_ISOLATE]);

// Meter ids are AUDIT VOCABULARY, not free text: this module interpolates them
// verbatim into reason codes (`meter_stale:<a>,<b>`,
// `ceiling_breached:<a>;budget_hard_stop`) and into shed actions
// (`hard_stop:<name>`). An unconstrained id forges that grammar:
// `partner_acquisition_cost;budget_hard_stop`, never read, emits a reason code
// that reads as a budget hard stop with no hard stop and no shed action behind
// it. The grammar lives HERE, in the module that owns the invariant, and
// worker.js imports it for the configuration door. Enforcing it at the door
// only was defense at one entrance to a room with more than one: the moment a
// policy arrives from anywhere but METER_POLICY_JSON, the builder is the last
// check standing.
export const METER_ID_RE = /^[a-z][a-z0-9_]{0,63}$/;

// The exact key set a MeterReading must carry on the wire, with the type each
// one is allowed to be. JSON.stringify DROPS an undefined value silently, so a
// spec that omits `unit` used to publish a reading with no `unit` key at all
// and the Python contract on the other side would reject it at merge time,
// long after the receipt was written. A reading that cannot be built correctly
// is a configuration error, never a quietly shortened row.
const READING_FIELDS = Object.freeze({
  meter: (v) => typeof v === 'string' && v.length > 0,
  meter_kind: (v) => METER_KINDS.includes(v),
  value: (v) => v === null || (typeof v === 'number' && Number.isFinite(v) && v >= 0),
  unit: (v) => typeof v === 'string' && v.length > 0,
  freshness_verdict: (v) => v === FRESH || v === STALE || v === UNKNOWN,
  sampled_at: (v) => v === null || typeof v === 'string',
  warning_threshold: (v) => v === null || (typeof v === 'number' && Number.isFinite(v)),
  hard_stop_threshold: (v) => v === null || (typeof v === 'number' && Number.isFinite(v)),
  breached: (v) => typeof v === 'boolean',
});

/** Every required field present with the right type, checked before serialization. */
function assertReadingShape(row) {
  for (const [field, ok] of Object.entries(READING_FIELDS)) {
    if (!(field in row) || !ok(row[field])) {
      throw new ReceiptInputError(
        `meter "${row.meter}" cannot be published: ${field} is missing or the wrong type`,
      );
    }
  }
}

function isCeiling(kind) {
  return kind === PER_INVOCATION || kind === PER_ISOLATE;
}

/**
 * @param {object} args
 * @param {object} args.policy      { policy_revision, staleness_ms, meters: {name: spec} }
 * @param {object} args.readings    {name: {value, sampled_at}} from the meter source
 * @param {string} args.meterSource where the readings came from, or 'unavailable'
 * @param {number} args.now         epoch milliseconds
 * @throws {ReceiptInputError} when policy_revision or attributedOperationClass is missing
 */
export function buildLimitReceipt(args) {
  const policy = args.policy || {};
  const meters = policy.meters || {};
  // Explicit undefined check: 0 is a meaningful staleness window ("only an
  // instantaneous read counts") and must not be swallowed by a falsy default.
  const stalenessMs = policy.staleness_ms === undefined ? DEFAULT_STALENESS_MS : policy.staleness_ms;
  const raw = args.readings || {};
  const now = args.now;
  const createdAt = new Date(now).toISOString();

  // Defense in depth, the same argument METER_ID_RE just settled: worker.js
  // validates the policy at the CONFIGURATION DOOR, but the door is not the
  // only way into the room, and the builder is what actually acts on these two
  // numbers. Measured before this check existed: a policy carrying
  // staleness_ms 1e15 reached the builder and a reading sampled at the UNIX
  // epoch verdicted `fresh`, `final_state: ok`, envelope `settled` (the exact
  // FALSE GREEN this module exists to prevent), and a hard_stop_threshold of
  // -1 turned a legitimate reading of 0 into `breached: true`,
  // `final_state: hard_stop`. A ReceiptInputError here becomes the same 503 the
  // route already returns for a receipt it cannot make auditable.
  if (typeof stalenessMs !== 'number' || !Number.isFinite(stalenessMs) || stalenessMs < 0) {
    throw new ReceiptInputError('policy.staleness_ms must be a finite number >= 0');
  }
  if (stalenessMs > MAX_STALENESS_MS) {
    throw new ReceiptInputError(
      `policy.staleness_ms must be <= ${MAX_STALENESS_MS} (24 hours): a wider freshness window settles a receipt on a reading no one can act on`,
    );
  }

  // Both of these are required with no default in the frozen contract, and
  // JSON.stringify drops an undefined value silently. A receipt that cannot be
  // built is strictly better than one that quietly lacks its policy revision.
  if (policy.policy_revision === undefined || policy.policy_revision === null) {
    throw new ReceiptInputError('policy.policy_revision is required: a receipt without one is unauditable');
  }
  if (args.attributedOperationClass === undefined || args.attributedOperationClass === null) {
    throw new ReceiptInputError('attributedOperationClass is required by the receipt contract');
  }

  const readings = [];
  const shedActions = [];
  const breachedCeilings = [];
  // Every configured meter is a REQUIRED meter. Anything not read fresh is a
  // hole in the claim, and the reason code names it.
  const unknownMeters = [];
  let anyHardStop = false;
  let anyNotFresh = false;

  for (const [name, spec] of Object.entries(meters)) {
    // Defense in depth, not a duplicate of the configuration check: this is the
    // layer that interpolates the id into the reason code, so this is where the
    // invariant has to hold. A refusal here becomes the same 503 the route
    // already returns for a receipt it cannot make auditable.
    if (!METER_ID_RE.test(name)) {
      throw new ReceiptInputError(
        `meter id "${name}" is not audit vocabulary: lowercase snake case, at most 64 characters, no separators`,
      );
    }
    if (!spec || typeof spec !== 'object' || Array.isArray(spec)) {
      throw new ReceiptInputError(`meter "${name}" has no specification: a meter we cannot describe is not a meter`);
    }
    // Same defense-in-depth argument as staleness_ms above. A negative threshold
    // is met by every reading including a legitimate zero, so it is not a limit
    // but an always-on breach; a warning above its hard stop can never fire,
    // because the hard stop is checked first and wins.
    for (const field of ['warning_threshold', 'hard_stop_threshold']) {
      const v = spec[field];
      if (v === undefined || v === null) continue;
      if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) {
        throw new ReceiptInputError(`meter "${name}" ${field} must be a finite number >= 0`);
      }
    }
    if (
      typeof spec.warning_threshold === 'number' &&
      typeof spec.hard_stop_threshold === 'number' &&
      spec.warning_threshold > spec.hard_stop_threshold
    ) {
      throw new ReceiptInputError(
        `meter "${name}" warning_threshold must be <= hard_stop_threshold, or the warning can never fire`,
      );
    }
    const kind = spec.meter_kind;
    const observed = raw[name];
    let value = null;
    let freshness = UNKNOWN;
    let sampledAt = null;

    // A negative or non-finite reading is IMPOSSIBLE for every meter kind here
    // (a budget spent, CPU milliseconds, subrequests, memory failures: all
    // counts). Accepting one settles a receipt green on a number no meter could
    // have produced, and -1 compares below every threshold, so it reads as
    // "used less than nothing". An impossible reading is an UNREAD meter: value
    // null, verdict unknown, named in the reason code, and it can never settle.
    const hasValue =
      observed &&
      typeof observed.value === 'number' &&
      Number.isFinite(observed.value) &&
      observed.value >= 0;
    // A stamp must be a finite number inside a sane range BEFORE it is handed to
    // Date: 9e15 is finite, is typeof number, and throws a RangeError on
    // toISOString. An unusable stamp is an unread meter, not a fresh one.
    const hasStamp =
      observed &&
      typeof observed.sampled_at === 'number' &&
      Number.isFinite(observed.sampled_at) &&
      observed.sampled_at >= MIN_SAMPLE_MS &&
      observed.sampled_at <= MAX_SAMPLE_MS;

    if (hasValue && hasStamp) {
      const age = now - observed.sampled_at;
      if (age >= 0 && age <= stalenessMs) {
        value = observed.value;
        freshness = FRESH;
        sampledAt = new Date(observed.sampled_at).toISOString();
      } else {
        // Present but too old to act on. The value is dropped rather than
        // reported, so nothing downstream can treat a stale number as current.
        freshness = STALE;
        sampledAt = new Date(observed.sampled_at).toISOString();
      }
    }

    if (freshness !== FRESH) {
      anyNotFresh = true;
      unknownMeters.push(name);
    }

    let breached = false;
    if (freshness === FRESH) {
      const hard = spec.hard_stop_threshold;
      const warn = spec.warning_threshold;
      if (typeof hard === 'number' && value >= hard) {
        breached = true;
        if (kind === CUMULATIVE) {
          shedActions.push(`hard_stop:${name}`);
          anyHardStop = true;
        } else if (isCeiling(kind)) {
          // A ceiling gets no shed action on purpose. Recording one would imply
          // the breach was recoverable at runtime. It is not. It still has to
          // change the verdict: a blown ceiling is not a green receipt.
          breachedCeilings.push(name);
        }
      } else if (typeof warn === 'number' && value >= warn) {
        // The between-thresholds state applies to every meter kind, and the
        // reading carries everything a consumer needs to derive it (`value`,
        // `warning_threshold`, `breached: false`), so no separate diagnostic
        // flag is published on the wire: the frozen MeterReading contract has
        // no such field (fix round 7). The shed action stays a budget thing:
        // shedding cannot rescue a ceiling, so naming an action there would be
        // a lie about what the operator can do.
        if (kind === CUMULATIVE) shedActions.push(`warn:${name}`);
      }
    }

    const row = {
      meter: name,
      meter_kind: kind,
      value,
      unit: spec.unit,
      freshness_verdict: freshness,
      sampled_at: sampledAt,
      warning_threshold: spec.warning_threshold === undefined ? null : spec.warning_threshold,
      hard_stop_threshold: spec.hard_stop_threshold === undefined ? null : spec.hard_stop_threshold,
      breached,
    };
    assertReadingShape(row);
    readings.push(row);
  }

  // Precedence: a proven breach outranks an unproven reading, because it is the
  // only one of the two we are certain about. A BREACHED CEILING OUTRANKS A
  // BUDGET HARD STOP, and this order is load-bearing: a budget hard stop is a
  // recoverable state that names an action, while a blown ceiling is a runtime
  // failure nothing at runtime can rescue. Ordering it the other way let a CPU
  // ceiling exceeded 1000x settle green as long as a budget also tripped, which
  // is the round-2 finding this comment exists to stop coming back. The shed
  // actions for the budget are still listed: the ceiling changes the verdict,
  // it does not erase the advice.
  //
  // A BREACH DOES NOT ERASE AN UNREAD METER. The breach branches used to
  // short-circuit the unread-meter branch, so a receipt could claim `settled`
  // with a settled timestamp while 3 of 4 required meters were never read, and
  // name none of them. Both signals are now carried: the breach keeps the
  // verdict (a hard stop stays authoritative and settled, because a consumer
  // that acts only on settled receipts must never be able to MISS a real stop),
  // and `meter_stale:<names>` records which claims are missing.
  let finalState = 'ok';
  let reasonCode = '';
  let envelopeState = 'settled';
  const staleSuffix = unknownMeters.length > 0 ? `;meter_stale:${unknownMeters.join(',')}` : '';
  if (breachedCeilings.length > 0) {
    finalState = 'ceiling_breached';
    reasonCode = `ceiling_breached:${breachedCeilings.join(',')}`;
    if (anyHardStop) reasonCode += ';budget_hard_stop';
    reasonCode += staleSuffix;
    envelopeState = 'failed';
  } else if (anyHardStop) {
    finalState = 'hard_stop';
    reasonCode = `budget_hard_stop${staleSuffix}`;
  } else if (readings.length === 0) {
    // A receipt that measured NOTHING is the one shape that must never settle
    // green. The per-reading rule ("unread is unknown, never zero") is enforced
    // row by row, and with zero rows there is no row to enforce it on: an empty
    // meters map used to produce final_state ok, state settled, and a settled
    // timestamp, which is the strongest verdict this system can issue, from a
    // receipt that could not even reach a meter source. SC-28 audits this
    // artifact, so a false green here is the one that matters.
    finalState = UNKNOWN;
    reasonCode = 'no_meters_configured';
    envelopeState = UNKNOWN;
  } else if (anyNotFresh) {
    // Every configured meter is required: the policy is the definition of what
    // this receipt claims to cover. The code names the meters we could not
    // read, so the reader knows WHICH claim is missing, not just that one is.
    finalState = UNKNOWN;
    reasonCode = `meter_stale:${unknownMeters.join(',')}`;
    envelopeState = UNKNOWN;
  }

  return {
    envelope: {
      receipt_id: args.receiptId,
      tenant_id: args.tenantId || 'tenant-owner-private',
      kind: 'host_limits',
      state: envelopeState,
      created_at: createdAt,
      policy_revision: policy.policy_revision,
      // Ownership fields. Required by the Ownership-extended ReceiptEnvelope
      // contract landing in this same branch set; user_id is null because the
      // actor kind is `system`.
      actor_id: args.actorId || 'actor-system',
      actor_kind: 'system',
      user_id: null,
      reason_code: reasonCode,
      settled_at: envelopeState === 'settled' ? createdAt : null,
    },
    meter_source: args.meterSource || 'unavailable',
    attributed_operation_class: args.attributedOperationClass,
    readings,
    shed_actions: shedActions,
    final_state: finalState,
  };
}

/**
 * The meters this project tracks. Thresholds are policy, not code: they are
 * read from configuration so changing one never needs a source edit.
 * Ceiling values here are the documented free-plan limits, recorded 2026-09-02.
 * memory_failures is per_isolate_ceiling, not per_invocation_ceiling: the
 * vendor limits page documents memory as a per-isolate limit, and an isolate
 * serves more than one invocation.
 */
export const DEFAULT_METER_POLICY = deepFreeze({
  policy_revision: 1,
  staleness_ms: 15 * 60 * 1000,
  meters: {
    requests_per_day: { meter_kind: CUMULATIVE, unit: 'requests', warning_threshold: 70000, hard_stop_threshold: 90000 },
    subrequests_per_request: { meter_kind: PER_INVOCATION, unit: 'subrequests', warning_threshold: 40, hard_stop_threshold: 50 },
    cpu_per_invocation: { meter_kind: PER_INVOCATION, unit: 'ms', warning_threshold: 8, hard_stop_threshold: 10 },
    memory_failures: { meter_kind: PER_ISOLATE, unit: 'events', warning_threshold: 1, hard_stop_threshold: 1 },
    cron_triggers_used: { meter_kind: CUMULATIVE, unit: 'triggers', warning_threshold: 4, hard_stop_threshold: 5 },
  },
});
