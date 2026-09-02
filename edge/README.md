# `edge/`: the thin private-access layer

A single Cloudflare Worker that stands in front of the private half of the
product. It does one job: prove the caller has a valid Cloudflare Access
session, then hand back a private record with headers that keep it out of every
cache. Everything expensive stays in the scheduled Python job.

## Why it is thin

Workers Free enforces a hard 10 ms CPU ceiling per invocation and a 128 MB
memory ceiling. Those are runtime limits, not budgets: you cannot shed load to
get under them, so any design that builds a slate inside a request is one that
either fits or fails. This layer therefore never builds a slate, never ranks,
and never normalizes a feed. It verifies, reads, and returns. A model call, if
one is ever added, spends MOST of its cost waiting on the network rather than on
Worker CPU, but not none: parsing the response and reshaping it are CPU, and
they count against the same 10 ms ceiling.

## Files

| File | What it does |
|---|---|
| `worker.js` | Router. Explicit public allowlist, everything else needs a verified token. |
| `access.js` | RS256 Access JWT verification with WebCrypto. Injected clock and fetch. |
| `headers.js` | The private header set applied to every authenticated response and every denial. |
| `meter.js` | Builds a `LimitReceipt`-shaped record. Missing or stale reads are `null` plus a non-fresh verdict, never `0`. |
| `synthetic.js` | An obviously fake private projection, so the shape is testable in a public repo. |
| `fixtures/limit-receipt.sample.json` | The pinned receipt serialization. A shape change has to change this file too. |
| `wrangler.toml.example` | Placeholders only. No account, zone, team, or application identifiers. |

## Route policy

Deny by default, in this order: method, then path shape, then the token, then
the route. The token is checked before the route is resolved, so an unlisted
path cannot be probed without a session.

**Method.** `GET` only. Every other method (including `HEAD` and `OPTIONS`) is
refused with 405 before routing, so no route can be reached with a verb its
handler was never written for.

**Path shape.** A path is refused with 400 when it contains an encoded
separator (`%2F`, `%2f`, `%5C`, `%5c`), a backslash, or anything else that
decodes to a path different from the one that arrived. A path with two readings
is a path the router must not pick one reading of.

| Path | Class | Signed out | Valid session |
|---|---|---|---|
| `/healthz` | public | 200 `ok` | 200 `ok` |
| `/`, `/app`, `/app/*` | HTML app | 401 | 200 |
| `/api/*` | private record | 401 | 200 |
| `*.map` | source map | 401 | 404, always |
| `/projection/synthetic` | synthetic projection | 401 | 200 |
| `/receipt/limits` | limit receipt | 401 | 200 |
| `/application`, anything else | unknown | 401 | 404 |

`/app` matches only at a path boundary. `/application` is a different path and
does not inherit the app route.

**Percent-encoding.** Encoded bytes are allowed when the path has exactly ONE
reading, which is what the Chinese lane needs: `/app/%E4%B8%AD%E6%96%87` decodes
to `/app/中文`, introduces no separator and no dot segment, and re-encodes to
exactly what arrived, so it routes. `%2F`, `%5C`, `%252F`, `%2e%2e` and `%41`
(an alternate encoding of `A`) all fail one of those three checks and are
refused with 400.

**Dot segments resolve before the Worker runs.** The runtime normalizes the path
first, so `/api/%2e%2e/x` arrives as `/x` (an unlisted path, 404 with a session)
and `/app/../healthz` arrives as `/healthz`, which answers publicly with the
fixed string `ok`. Both are safe for the same reason: the router is an
allowlist, and the one public route is a constant with no reader data in it. It
is written down here because "a path with `/app/` in it answered without a
token" looks alarming until you know it was never that path by the time the
Worker saw it.

## Configuration

Two values are required. Everything else is optional with a documented default.
Placeholders and defaults live in `wrangler.toml.example`.

| Variable | Required | Default | What it controls |
|---|---|---|---|
| `ACCESS_TEAM_NAME` | yes | none | The Access team name. The issuer and key set URL are derived from it, so it is the trust root: it must be exactly one DNS label (lowercase letters, digits, hyphens, no leading or trailing hyphen, at most 63 characters). |
| `ACCESS_AUD` | yes | none | The application audience tag the token's `aud` must match. Non-blank, and not the shipped `REPLACE_WITH_...` placeholder. |
| `METER_POLICY_JSON` | no | `DEFAULT_METER_POLICY` | Meter thresholds, as JSON. Policy, not code. |
| `ACCESS_CLOCK_SKEW_SEC` | no | `30` | Skew allowed on `exp` and `nbf`, in seconds. **Maximum `300`** (see below). |
| `JWKS_CACHE_TTL_MS` | no | `600000` | How long a fetched key set is reused. **Must be `>= JWKS_MIN_REFETCH_MS`**, maximum `86400000` (24 h). |
| `JWKS_MIN_REFETCH_MS` | no | `60000` | Floor between forced refetches: what stops an unknown `kid` amplifying into one outbound fetch per request. **Must be `<= JWKS_CACHE_TTL_MS`**, maximum `86400000` (24 h). |
| `JWKS_STALE_GRACE_MS` | no | `0` | Serve-stale window past the TTL during a key set OUTAGE. `0` fails closed. Maximum `86400000` (24 h). |
| `JWKS_FETCH_TIMEOUT_MS` | no | `5000` | Upper bound on ONE key set fetch. A hung upstream becomes a denial instead of an unbounded wait. Must be `> 0`, maximum `30000` (30 s). |

**`JWKS_CACHE_TTL_MS` must be `>= JWKS_MIN_REFETCH_MS`, and the Worker refuses
to start otherwise.** The two knobs are not independent. The refetch floor
applies to attempts even when no cache is left to serve (see *The refetch floor*
below), so if the cache expires BEFORE the floor permits a refill, the layer
throws its key set away and then forbids fetching a replacement. A perfectly
healthy key set answering `200` on every call then produces a permanent duty
cycle of `key_set_unavailable` denials: measured at **50% of authenticated
requests with a 30 s TTL against the default 60 s floor, 83% at a 10 s TTL**.
Shortening the TTL to pick up a key rotation faster is the natural instinct and
is exactly how a deployment lands there, so the combination is refused at
construction rather than left to be discovered in production. Lower the floor
alongside the TTL, or leave both alone.

**Every time knob has a MAXIMUM as well as a floor, and the Worker refuses to
start above it.**

| Variable | Maximum | What the maximum prevents |
|---|---|---|
| `ACCESS_CLOCK_SKEW_SEC` | `300` (5 min) | The accepted-session window past a token's own `exp`. `"30000"`, three extra zeros on the default `30`, is 8.33 hours of expired-token acceptance, measured as HTTP `200` on a private route. |
| `JWKS_CACHE_TTL_MS` | `86400000` (24 h) | A key retired or revoked upstream keeps verifying sessions in that isolate for the whole window. |
| `JWKS_MIN_REFETCH_MS` | `86400000` (24 h) | The floor is bounded by the TTL, which is bounded here. |
| `JWKS_STALE_GRACE_MS` | `86400000` (24 h) | The `0` default is the load-bearing fail-closed one; an unbounded grace defeats it permanently rather than for a bounded window. |
| `JWKS_FETCH_TIMEOUT_MS` | `30000` (30 s) | How long a hung upstream holds every request that joined the in-flight fetch before the hang becomes a denial. |

A floor alone is half a bound. `METER_POLICY_JSON staleness_ms` already carries
a 24-hour ceiling on the argument that three extra zeros on a default is a typo,
not a tuning choice, and the control it disables is the one the module exists to
provide. The five knobs above are the same defect class with a worse failure
direction: a too-wide freshness window fails GREEN, these fail OPEN. What the
ceilings refuse when the assumption is wrong: a deployment whose clocks are more
than five minutes apart (a machine to fix, not a session window to widen), or an
operator who deliberately wants expired Access tokens honored for hours, which
is not tuning but disabling the `exp` claim that Cloudflare Access owns. The
refusal is a deploy-time 503 recoverable in seconds by editing one variable; the
alternative is a silently extended authentication window that no log, no receipt
and no reason code records.

**`ACCESS_TEAM_NAME` is the trust root, so it is validated as one DNS label.**
Both the issuer a token's `iss` must equal and the host the key set is fetched
from are built by interpolating it, so an unvalidated value redirects trust
rather than misconfiguring a window: `attacker.example/path` yields a key set URL
on host `attacker.example`, and a token signed by that host's key opens a private
route. Accepted: lowercase letters, digits and hyphens, no leading or trailing
hyphen, at most 63 characters. Blank values and the shipped `REPLACE_WITH_...`
placeholders are refused for `ACCESS_TEAM_NAME` and `ACCESS_AUD` alike, so a
copied-but-unfilled config reads as "you did not fill in the file" instead of
"every token is denied".

**A numeric setting is PARSED, never coerced.** A value is read only from a real
number or a non-blank plain decimal string (`"60000"`, `"1.5"`). Whitespace,
booleans, arrays, hexadecimal (`"0x10"`), exponent notation (`"1e3"`), signs and
`"Infinity"` are all configuration errors. `Number(" ")` is `0`, and a floor
silently set to zero is the control disabled rather than tuned.

**A configuration the Worker cannot be built from is a refusal, not a crash.** A
malformed `METER_POLICY_JSON`, a missing `ACCESS_TEAM_NAME` or `ACCESS_AUD`, or
a tuning value that is not a finite number `>= 0` all produce:

- `503` with the private header set on every route, including every private one.
  Deny by default holds: a private route under an invalid configuration is
  refused, never served.
- a constant body, `{"error":"unavailable","reason":"worker_unavailable"}`. No
  exception text, no offending value, no key set URL: the caller who trips this
  is unauthenticated by definition.
- `/healthz` answers `{"status":"degraded","config":"invalid"}` with 503, because
  reporting the outage is what a health check is for. It still says nothing
  about which key or what value.

**The method gate is decided before the configuration is read.** Anything other
than `GET` is `405` on every path, whether the Worker built or not. It used to
sit inside the built Worker only, so `HEAD /healthz` answered `405` under a
valid configuration and `503` under a broken one, and an uptime monitor that
HEADs the health route saw the gate flip. One shape, always.

`METER_POLICY_JSON` is validated STRICTLY and as a WHOLE, not merely parsed:

| Rule | Why |
|---|---|
| Top-level keys are exactly `policy_revision`, `staleness_ms`, `meters` | `staleness_mss` used to fall back to the default in silence |
| Meter-spec keys are exactly `meter_kind`, `unit`, `warning_threshold`, `hard_stop_threshold` | `hard_stop` instead of `hard_stop_threshold` used to be indistinguishable from no threshold at all, so a one-word typo settled a receipt green at 55x the intended stop |
| `policy_revision` is a non-negative safe integer | It is the audit join key, and the frozen contract types it as an integer |
| `meters` is a plain object (`null` and `[]` refused) | `typeof null === 'object'` |
| Meter ids match `^[a-z][a-z0-9_]{0,63}$`, enforced at the config door AND inside `buildLimitReceipt` | Ids are interpolated into reason codes and shed actions, so `partner_acquisition_cost;budget_hard_stop` could forge a verdict. The grammar lives in `meter.js`, the module that does the interpolating; `worker.js` imports it. Ids are non-sensitive by VALIDATION at both layers, not by construction |
| `staleness_ms` is `<= 86400000` (24 hours) | The value half of the misspelled-key defect: three extra zeros turns 15 minutes into 10.4 days, wide enough for a reading sampled at the UNIX epoch to verdict `fresh` |
| Every spec carries a known `meter_kind` and a non-empty `unit` | A meter we cannot describe is not a meter |
| Thresholds are finite and `>= 0`, and `warning_threshold <= hard_stop_threshold` | A negative threshold is met by every reading; a warning above its stop can never fire |

An unknown key is a REFUSAL, which does refuse a forward-compatible policy that
adds a field this build has not learned. That is the right trade: the policy
carries `policy_revision`, so adding a field is a deliberate versioned change,
and the refusal is a deploy-time 503 recoverable by editing an env var, while a
silently disabled limit is not recoverable because nobody learns it happened.

A policy that parses but cannot describe its meters is a configuration error, so
it lands on the 503 path before a request is served rather than throwing one
route deep on `/receipt/limits`. An EMPTY `meters` map is legal: see the next
section.

The `JWKS_STALE_GRACE_MS` default of `0` is the load-bearing one. During a key
set outage past the TTL, `0` denies every session rather than trusting keys we
can no longer confirm. Raising it is a deliberate availability trade, bounded by
the window you set.

**Known trade-off: a positive grace does not carry sessions across a rotation.**
Inside the grace the verifier is serving a key set it can no longer confirm, so
its behavior during a rotation-plus-outage is the opposite of what "availability"
suggests:

| Token signed by | During an outage inside the grace | After the key set is reachable again |
|---|---|---|
| a key the upstream has already RETIRED | **accepted** (it is in the last set we confirmed) | denied |
| a key the upstream has just rotated IN | **denied, `unknown_kid`** (we have never seen it) | accepted |

So `JWKS_STALE_GRACE_MS > 0` extends trust in a possibly-retired key set by
`TTL + grace`, and preserves exactly the sessions an operator would want dropped
while denying the ones they would want kept. It buys availability against an
outage with NO rotation. Set it only if that is the trade you want. Pinned by
`the stale grace is a trade, not free availability...` in `access.test.js`.

**The refetch floor applies to ATTEMPTS, not only to successes, and it is
honored even with no cache to fall back on.** During an outage there is no
success to measure from, so `JWKS_MIN_REFETCH_MS` is counted from the last
attempt: at most one outbound key set fetch per floor window, no matter how many
requests arrive or how many of them carry an unknown `kid`. Inside the window,
with no servable cache, the verifier DENIES WITHOUT FETCHING.

That denial is what it would have returned anyway: with no confirmed key set,
every token is refused whether or not another fetch is attempted. So honoring
the floor costs nothing an inbound caller can observe during the outage, and it
bounds outbound amplification (at the default `JWKS_STALE_GRACE_MS` of `0` the
floor used to be inert, so each request cost a fetch and an unknown `kid` cost a
second, paceable 1:1 by an unauthenticated caller). The one cost is on the other
side: after the key set is reachable again, up to one floor window of denial
that an immediate retry would have avoided.

That bound holds only while the cache OUTLIVES the floor window, which is why
`JWKS_CACHE_TTL_MS < JWKS_MIN_REFETCH_MS` is refused at construction. Without
that precondition the floor also fires with no outage at all, and the denial is
not one window on recovery, it is permanent and recurring.

Pinned by three tests in `access.test.js`: `during an outage inside the stale
grace, the refetch floor bounds attempts to one per window`, `at the default zero
grace, a failed forced refresh does not refetch again inside the floor`, and
`during an outage with no usable cache, five requests cost one outbound fetch`.
Concurrent requests still join the single in-flight fetch rather than being
denied by the floor (`concurrent requests still JOIN one in-flight fetch...`).

## Meter kinds

`meter.js` records three, and they are not interchangeable.

| Kind | What it limits | Can shedding help? | A breach means |
|---|---|---|---|
| `cumulative_budget` | a running total over a window (requests/day, cron triggers) | Yes: stop optional work before the budget is spent | `hard_stop:<meter>` shed action, `final_state: hard_stop` |
| `per_invocation_ceiling` | a runtime hard limit on ONE invocation (CPU ms, subrequests per request) | No: the work fits or the fallback runs elsewhere | no shed action, `final_state: ceiling_breached`, envelope `state: failed` |
| `per_isolate_ceiling` | a runtime hard limit on the ISOLATE, which several invocations share (memory) | No, same as above | same as above |

Memory is a per-isolate limit in the vendor's own limits page, not a
per-invocation one, which is why it has its own kind rather than being filed
under `per_invocation_ceiling`. A blown ceiling of either kind never settles
green: shedding cannot rescue it, but the receipt still has to say so.

**A receipt that measured nothing is never green.** The rule "an unread meter is
unknown, never zero" is enforced row by row, and with zero rows there is no row
to enforce it on. So the verdict is decided for the receipt as a whole too:
every meter the policy configures is a REQUIRED meter, and

| Receipt | `final_state` | envelope `state` | `settled_at` | `reason_code` |
|---|---|---|---|---|
| no meters configured (`"meters":{}`) | `unknown` | `unknown` | `null` | `no_meters_configured` |
| a configured meter not read fresh | `unknown` | `unknown` | `null` | `meter_stale:<the meters>` |

An empty policy used to produce `final_state: ok`, `state: settled` and a
settled timestamp: the strongest verdict this system can issue, from a receipt
that could not even reach a meter source. SC-28 audits this artifact, so a false
green here is the one that matters.

A reading is also validated before it is serialized. All ten fields must be
present with the right type; `JSON.stringify` drops an `undefined` value
silently, so a spec missing its `unit` used to publish a reading with no `unit`
key at all. A reading that cannot be built correctly is a refusal (a 503 on the
route), never a quietly shortened row.

## Running the tests

No dependencies and no build step. Node's built-in runner only:

```
node --test "edge/*.test.js"
```

Quote the glob. Passing the bare directory (`node --test edge/`) is treated as a
module entry point on Node 22.22.3 and fails. CI runs the same command in the
`edge-tests` job of `.github/workflows/ci.yml`.

CI pins `node-version: '22'`, which FLOATS across 22.x patch releases by design:
this code has no dependencies and no build step, so a patch release of the
runtime is exactly the change we want CI to catch. Local runs of this suite were
on Node 22.22.3.

`edge/fixtures/limit-receipt.sample.json` pins the receipt serialization byte
for byte. The suite READS it and never writes it: a missing fixture is `ENOENT`
(red), and a drifted one is a byte-comparison failure (red). Neither self-heals.

Regenerating it is a deliberate, separate act:

```
node edge/scripts/regenerate-fixtures.mjs
```

Run it only when you MEANT to change the emitted shape, and reconcile that
change with `curator/contracts/receipt.py` in the same commit. CI runs
`git diff --exit-code -- edge/fixtures` after the suite, so a fixture rewritten
by any route cannot reach `main` green.

`positive-control.test.js` is not optional. It breaks the edge layer three ways
(auth check removed, denial made cacheable, verifier stubbed permissive) and
asserts that the same assertion helpers the real tests use go red. If those
control tests ever pass silently, the denial tests have stopped testing
anything.

## What is not proven here

Everything in this directory is proven against injected doubles. Real CPU per
request on real story volumes, a real signed-out denial against a real
Access-protected hostname, and real meter readings all need the account. See
`docs/evidence/2026-09-02-phase2-edge-proof-local.md`.
