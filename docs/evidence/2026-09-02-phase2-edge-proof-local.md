# Phase 2, edge privacy proof: the local half

Date: 2026-09-02. Scope: everything provable without a hosting account.
Covers SC-12 (unauthenticated requests retrieve nothing private), SC-13 (no
service-role credential in browser assets), SC-28 (meter source proof).

Evidence grades used throughout: **A** proven in production, **B** proven in
source or stated by the vendor's own documentation, **C** not verified.

The live half (a real deployment, a real signed-out denial against a real
Access-protected hostname, real meter numbers) is **not** in this receipt. It
needs the account. The owner actions at the bottom are the whole gap.

---

## 1. Facts checked against vendor documentation

All pages read 2026-09-02.

| # | Fact | Value as documented | Grade | Source |
|---|---|---|---|---|
| 1 | Free plan requests | 100,000/day, reset at midnight UTC | B | [workers/platform/limits](https://developers.cloudflare.com/workers/platform/limits/) |
| 2 | Free plan CPU per invocation | 10 ms for HTTP requests | B | same |
| 3 | Memory per isolate | 128 MB (Free and Paid alike) | B | same |
| 4 | Subrequests per request (Free) | 50/request | B | same |
| 5 | Cron Triggers | 5 per account | B | same |
| 6 | Behavior past the daily request limit | Error 1027. Depending on route configuration the request either bypasses the Worker (fail open) or gets a Cloudflare 1027 error page (fail closed) | B | same |
| 7 | Header carrying the Access JWT | `Cf-Access-Jwt-Assertion`. `CF_Authorization` cookie is a fallback; the docs recommend validating the header because the cookie "is not guaranteed to be passed" | B | [cloudflare-one/.../validating-json](https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/) |
| 8 | Key set URL | `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/certs`. Keys rotate every 6 weeks by default; match the token's `kid` against the `public_certs` array rather than the single `public_cert` | B | same |
| 9 | Required claims and algorithm | `iss` = `https://<team-name>.cloudflareaccess.com`; `aud` = the Application Audience tag, unique per application; signing algorithm RS256. `email` appears in the payload examples | B | same |
| 10 | A Worker route can sit behind Access | Yes. Three ways: account-wide, per-Worker (covers routes, Custom Domains, the workers.dev hostname and previews), or a hostname/path self-hosted application | B | [workers/configuration/cloudflare-access](https://developers.cloudflare.com/workers/configuration/cloudflare-access/) |
| 11 | Access-on-Worker caveats | Worker-level Access policies do not support WebSocket connections (upgrade requests fail with 403); `ctx.access` does not propagate through Service Bindings or RPC to downstream Workers | B | same |
| 12 | Cache-Control for authenticated responses | `private` = intended for a single user, must not be stored by a shared cache; `no-store` = no cache may store any part of request or response. With an Authorization header and Origin Cache Control (default on Free), a response is cacheable only if Cache-Control also has `public`, `s-maxage`, or `must-revalidate`; `Set-Cookie` and `Authorization` also trigger an automatic bypass | B | [cache/concepts/cache-control](https://developers.cloudflare.com/cache/concepts/cache-control/) |
| 13 | Authoritative meter source | GraphQL dataset `workersInvocationsAdaptive`. The documented query pulls `sum { subrequests requests errors }` and `quantiles { cpuTimeP50 cpuTimeP99 }` | B | [analytics/.../querying-workers-metrics](https://developers.cloudflare.com/analytics/graphql-api/tutorials/querying-workers-metrics/) |
| 14 | What the meter source gives you | Worker metrics retain up to three months, in increments of at most one week. Wall time and CPU time use reservoir sampling. Memory appears as P50/P90/P99/P999 percentiles. Invocation statuses include `success`, `clientDisconnected`, `scriptThrewException`, `exceededResources`, `internalError` | B | [workers/observability/metrics-and-analytics](https://developers.cloudflare.com/workers/observability/metrics-and-analytics/) |
| 15 | What the meter source does NOT give you | No exact per-request CPU number (percentiles from a sample, not a total), and no memory field in the documented `workersInvocationsAdaptive` query. Treated as C because the pages read did not publish a complete field list for the dataset | C | inference from #13 and #14 |
| 16 | Python Workers status | Open beta, not GA. Requires the `python_workers` compatibility flag | B | [workers/languages/python](https://developers.cloudflare.com/workers/languages/python/) |
| 17 | Zero Trust / Access free seat count | Commonly stated as 50 users. **Not confirmed.** Three vendor pages were fetched (Cloudflare One docs index, the Zero Trust plans page, the Cloudflare One account-limits page) and none of them stated a number. Only search-result snippets did | C | to confirm in the account dashboard |

Consequence of #15 for SC-28: the meter source is authoritative for **requests,
errors and subrequests**, and is a **sampled estimate** for CPU. A run receipt
must record which of the two it is holding, and a CPU figure from this source
can never be reported as an exact per-request measurement.

Consequence of #6: past 100,000 requests/day the platform's own behavior
depends on route configuration, and one of the two options (fail open) means
requests bypass the Worker. A private surface must be configured fail closed,
because a bypassed Worker is a bypassed access check.

---

## 2. Architecture decision: the Worker is thin

**HOW decision:** where the heavy compute lives, given a 10 ms per-invocation
CPU ceiling that cannot be shed.

| Option | What it is | Pros | Cons |
|---|---|---|---|
| A. Thin edge, heavy compute in the scheduled job | The Worker verifies the Access JWT, fails closed, and serves a pre-built private projection. Slate build, normalization and ranking stay in the existing Python job. An Ask AI turn is an outbound subrequest | Nothing in the request path is CPU-bound, so the 10 ms ceiling stops being a design risk. Reuses the pipeline that already exists. Zero new runtime dependencies | Projections must be built ahead of the request, so a reader can see a projection that is minutes old. Two places now hold logic |
| B. Full application in the Worker | Build the slate per request at the edge | One deployment surface. Always current | Ranking a real story volume inside 10 ms of CPU is unproven and probably false. Shedding cannot rescue a single over-ceiling invocation, so the failure mode is a hard error, not a slow response. Would mean porting the pipeline |
| C. Python Worker running the existing pipeline at the edge | Reuse the Python code as-is at the edge | No port | Python Workers are open beta (fact #16), and it does not solve the CPU ceiling anyway. Beta plus an unproven ceiling on a private surface is two risks stacked |

**Preference: A.** The deciding fact is that a per-invocation ceiling is a
runtime hard limit, not a budget. Every other limit in this system can be
managed by shedding optional work. This one cannot: an invocation that needs 12
ms of CPU fails, and no policy can save it. So the correct response is to make
sure the request path never needs that CPU, rather than to measure how close we
get. Option B makes the ceiling load-bearing on story volume, which is exactly
the variable we do not control.

**This is the SC-28 named fallback, and it is named, not proven.** SC-28
requires that per-invocation ceilings either be proven to fit or have a named
fallback. The named fallback is: the slate build does not run inside a Worker
invocation at all. It runs in the scheduled Python job, and the request path
serves a projection that job already built.

Three corrections to how that fallback was described, from the vendor limits
page (fact #2, #3, #4):

| Claim | Correct statement |
|---|---|
| An Ask AI `fetch()` is "outside the invocation" | **Wrong.** An outbound `fetch()` IS a subrequest and it counts against the 50-per-request limit. It happens inside the invocation, not beside it |
| The model turn costs no Worker CPU | **Partly right.** CPU time excludes time spent WAITING on a subrequest, so the wait is free. Parsing and reshaping the response inside the Worker is not. The turn is only CPU-free if the Worker relays the body rather than parsing it, and no such rule is written into any code today |
| Memory is a per-invocation ceiling | **Wrong.** Memory is documented per ISOLATE, and an isolate serves more than one invocation. `meter.js` now meters it as `per_isolate_ceiling`, a third meter kind, rather than filing it under `per_invocation_ceiling` |

**What that leaves unproven.** The fallback is a real engineering position and
it removes the slate build from the request path, which is the part that would
have made the CPU ceiling load-bearing on story volume. But whether the
remaining request path fits inside 10 ms of CPU on real volumes is **grade C
until the live half runs**: no deployment exists and no CPU number has been
measured. And the Ask AI turn at the edge is a **design position, not a proven
path**: no Ask AI code exists in `edge/`, so the relay-never-parse rule that
would make the CPU claim true is unwritten and unenforced. Neither should be
read as an SC-28 compliance proof.

**Worker language: JavaScript ES modules on the native runtime, zero runtime
dependencies.** RS256 verification uses WebCrypto (`crypto.subtle`), which the
runtime provides. Tests use Node's built-in runner. Nothing was installed for
this slice, and nothing needs to be.

**What remains unmeasured (grade C):** actual CPU milliseconds per request on
real story volumes, actual memory headroom against 128 MB, and whether a real
Ask AI subrequest completes inside the platform's wall-clock behavior. All three
need a deployment. Until then this decision rests on the argument above and on
the documented limits, not on a measurement.

---

## 3. Proven locally (grade B: proven in source, against injected doubles)

Command, run in the worktree:

```
node --test "edge/*.test.js"
```

Node v22.22.3. **140 tests, 140 pass, 0 fail** (measured 2026-09-02, after fix
round 5). The tally by round: 47/47 pre-review, 77/77 after round 1, 82/82
mid-round-2 (the partial pass), 100/100 after round 2, 116/116 after round 3,
124/124 after round 4, 140/140 after round 5, 158/158 after round 6 (current, both runner modes). `edge/package.json` declares
`"type": "module"`, so the suite no longer depends on Node's ESM auto-detection:
`node --no-experimental-detect-module --test "edge/*.test.js"` also passes
158/158 in both modes as of round 6 (it was 0 pass / 5 fail before round 4; 140/140 at round 5).
Note the glob must be quoted:
passing the bare directory `edge/` is treated as a module entry point on this
Node build and errors out. CI runs the same command in the `edge-tests` job of
`.github/workflows/ci.yml`, so a regression turns a pull request red instead of
waiting for someone to type it.

CI pins `node-version: '22'`, which floats across 22.x patch releases **by
design**: this layer has no dependencies and no build step, so a runtime patch
release is precisely the change CI should catch rather than one it should hide.
The local runs recorded in this receipt were on **Node v22.22.3** exactly.

| Area | What the tests establish |
|---|---|
| Signed-out denial | All seven private route classes (HTML app, app sub-path, API record, `.map`, synthetic projection, limit receipt, unlisted path) return 401 with `Cache-Control: private, no-store`, and the denial body contains neither projection content nor app markup |
| Token rejection | Wrong `aud`, wrong `iss`, expired, not-yet-valid, unknown `kid`, `alg: HS256`, `alg: none`, tampered signature, spliced payload, and malformed tokens are all refused, each with its own reason code |
| Valid session | A correctly signed token opens the app (`/`), the API record (`GET /api/records/1`) and the synthetic projection, and every response carries `private, no-store`, `Vary: Cookie, Cf-Access-Jwt-Assertion`, `X-Robots-Tag: noindex, nofollow`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, and neither an `ETag` nor a `Last-Modified` |
| Unlisted path with a session | `/whatever/else` and `/application` return 404 with the full private header set and no app markup, so a valid session cannot enumerate routes either |
| Method and path shape | Only `GET` is answered: `POST /`, `HEAD /`, `OPTIONS /api/r`, `DELETE /api/r` and `PUT /healthz` are refused with 405 before routing, each carrying the private headers. A path containing `%2F`, `%2f`, `%5C`, `%5c`, a backslash, or anything that decodes to a different path is refused with 400. `/app` matches only at a path boundary |
| Claim types | `exp: 1e999` (which `JSON.parse` turns into `Infinity`) and a string `exp` are both refused, as is a present but non-finite `nbf`. An array `aud` containing the configured tag is accepted; an array `aud` without it is denied |
| Key set failures | A 503, an unparseable body, a body with no `keys` array, and a failure on the forced refresh each return `key_set_unavailable` rather than throwing out of `verify()` |
| Source maps | Refused with 404 even with a valid session |
| Fail closed | A verifier that throws produces a 401, never an allow |
| Key set cache | Honors its TTL, refetches once past it, forces at most one extra fetch on an unknown `kid`, and a refetch floor stops an unknown `kid` from hammering the key set. Proven on **the exported Worker dispatch shape too** (no deployment exists): the exported `fetch` handler keeps one verifier per isolate, keyed by team and audience, so two valid requests inside the TTL cause one JWKS fetch and five unknown-`kid` requests inside the refetch floor also cause one |
| Meter receipt | A missing read is `value: null` with verdict `unknown`; a stale read drops its value rather than reporting an old number; a fresh read over the hard stop emits `hard_stop:<meter>` and settles `hard_stop`; a future-stamped read is not fresh; a proven hard stop outranks an unproven read |
| Meter receipt, corrected in fix round 1 | `staleness_ms: 0` is honored (a 1 ms old read is `stale`, not `fresh`) instead of being swallowed by a falsy default; an out-of-range or non-numeric `sampled_at` (`9e15`, a string, `Infinity`, a negative) is `unknown` with `value: null` rather than a `RangeError`; a breached ceiling of either kind emits **no** shed action but reports `final_state: ceiling_breached`, envelope `state: failed`, and a `reason_code` naming the meter, so a blown ceiling can never settle green; a missing `policy_revision` or `attributed_operation_class` throws a typed `ReceiptInputError` instead of silently dropping a required key; memory is metered as `per_isolate_ceiling` |
| Meter source failure | A meter reader that throws still produces a receipt, marked `meter_source: "unavailable"` with every reading `unknown`, rather than losing the record that we could not read the meter |
| Positive control | Three deliberately broken edge layers (auth check removed, denial made cacheable, verifier stubbed permissive) each make the same assertion helpers the real tests use go red. The suite is proven capable of failing |

### Fix round 2: what changed, and the test that proves each one

Every row is grade **B** (proven in source against injected doubles). The test
column is the exact test title, so a claim here can be located by name.

| Round-2 item | What changed | Proving test |
|---|---|---|
| Config failures are a refusal, not a crash | Worker construction (`verifierForEnv`, `resolveMeterPolicy`, every numeric setting) moved inside a `try` in the exported `fetch`. A malformed `METER_POLICY_JSON`, a missing `ACCESS_TEAM_NAME` or `ACCESS_AUD`, or a non-numeric tuning value now returns 503 with the private header set on every route | `an invalid configuration is a 503 on every private route, never an exception` |
| A private route under an invalid config is denied, never served | The 503 fires before any route handler, so a token that would otherwise verify still gets no app body | `an invalid configuration denies a private route even with a token that would otherwise verify` |
| The refusal body leaks nothing | Constant body `{"error":"unavailable","reason":"worker_unavailable"}`. Asserted to contain no exception text, no configuration value, no key set URL, no variable name | `a construction failure leaks no exception text, no configuration value and no key set URL` |
| `/healthz` may say `config: invalid`, and nothing more | Health reports the outage (that is what a health check is for) with a body asserted equal to `{"status":"degraded","config":"invalid"}` | `the health route under an invalid configuration reports degraded without saying what is wrong` |
| The 503 is the failure path, not the new default | A valid configuration still serves `/healthz` 200 `ok` | `a valid configuration still serves the public route: the 503 is the failure path, not the default` |
| A breached ceiling dominates a budget hard stop | Precedence reordered in `meter.js`: `final_state: ceiling_breached`, envelope `state: failed`, `reason_code` naming the ceiling meter and appending `;budget_hard_stop`. The budget's shed actions are still listed. Proven in both reading orders | `a breached ceiling dominates a co-occurring budget hard stop`, `the same two breaches read in the opposite order settle the same way` |
| Warning thresholds apply to every meter kind | The warn branch now sets `warning: true` on ceilings too, diagnostic only: no shed action, `breached: false` | `a ceiling between its warning and its hard stop warns without a shed action`, `a warning on a budget still emits its shed action, and a breach is not also a warning` |
| Envelope shape is pinned byte for byte | `edge/fixtures/limit-receipt.sample.json` holds the full serialization from fixed inputs, compared as a string; the test FAILS with `ENOENT` when the committed sample is missing rather than writing one. It pins a warning, an unread meter as `null`, and the four ownership keys | `the receipt envelope matches the committed sample byte for byte`, `the committed sample carries the four ownership keys the envelope contract requires` |
| Percent-encoded paths with ONE reading are routable | `isSafePath` now allows encoding whose single decode introduces no separator, no dot segment and no control character AND whose re-encoding round-trips. A UTF-8 Chinese slug reaches the router; `%2F`, `%5C`, `%252F`, `%2e%2e`, `%41` stay refused | `a UTF-8 percent-encoded Chinese slug is safe and reaches the router`, `an encoded path that decodes to a second reading is still refused, one form at a time`, `the refused encodings are 400 at the edge, not a route decision` |
| Session-window tuning is programmable policy | `ACCESS_CLOCK_SKEW_SEC`, `JWKS_CACHE_TTL_MS`, `JWKS_MIN_REFETCH_MS`, `JWKS_STALE_GRACE_MS` are env-configured with validated defaults; a non-numeric or negative value is a config error on the 503 path | `every tuning value is validated at construction: a non-numeric or negative one refuses to build`, plus the `BAD_ENVS` rows above |
| A key set outage fails closed by default | New serve-stale grace, default `0`. Grace 0 denies past the TTL during an outage; a positive grace serves the last confirmed key set inside the window and denies after it | `the default stale grace is zero: an outage past the TTL denies rather than trusting an unconfirmable key set`, `a positive stale grace serves the last confirmed key set inside the window, and denies after it` |
| The skew boundary is exact | 29s past expiry verifies, exactly 30s does not; exactly 30s early verifies, 31s does not; skew 0 removes the grace | `the expiry grace is exactly the configured skew: 29s stale still verifies, 30s does not`, `the not-before grace is exactly the configured skew: 30s early still verifies, 31s does not`, `a configured skew of zero removes the grace entirely` |
| Every time knob has a MAXIMUM, not just a floor (round 6) | `ACCESS_CLOCK_SKEW_SEC <= 300`, `JWKS_CACHE_TTL_MS`, `JWKS_MIN_REFETCH_MS` and `JWKS_STALE_GRACE_MS <= 86400000`, `JWKS_FETCH_TIMEOUT_MS <= 30000`, all refused at construction. The reported input (`ACCESS_CLOCK_SKEW_SEC="30000"` plus a token that expired an hour ago) returned HTTP 200 on `/app/today` before the ceiling and is a 503 on every private route after it | `a clock skew past the documented maximum is refused at construction`, `each time knob refuses its maximum plus one and accepts the maximum itself`, `a verifier at every maximum still verifies a valid token`, `an over-wide clock skew refuses at deploy time instead of serving a token that expired an hour ago` |
| The team name is validated as one DNS label (round 6) | `ACCESS_TEAM_NAME` builds both the issuer and the key set host, so `attacker.example/path` redirected the trust root. It is now one DNS label or a construction refusal, and blank values plus the shipped `REPLACE_WITH_...` placeholders are refused for `ACCESS_TEAM_NAME` and `ACCESS_AUD` alike | `a team name that is not one DNS label is refused before any URL is built`, `the shipped REPLACE_WITH placeholders are refused for both required identifiers`, `a valid DNS label builds exactly the documented key set URL and issuer`, `a team name that redirects the trust root refuses at deploy time` |
| The SHIPPED default policy is frozen all the way down (round 6) | `Object.freeze` reached the policy and its `meters` map but not the individual specs, so `DEFAULT_METER_POLICY.meters.requests_per_day.hard_stop_threshold = 99999999` landed silently on the path that runs when nothing is configured. Both policies now go through the same `deepFreeze`, which lives in `meter.js` | `the default policy is frozen all the way down, specs included`, `mutating a default meter spec throws instead of re-tuning every later request` |
| The meter provenance guard accepts only a plain readings map (round 7) | `isPlainObject` accepted any non-array object, so a reader that returned a raw fetch `Response`, a `Map`, or a `Date` was labeled with the host provenance and published a receipt in state `unknown` under the reader's name. The guard now requires `Object.prototype` or a null prototype; every other object is `meter_source: unavailable`. Reverting the check fails the named test (measured: 54 pass, 1 fail on `worker.test.js`) | `a meter reader that returns a non-object is unavailable, never labeled with the reader name` (now also `Response`, `Map`, `Date`) |
| The tally history in this document is current (round 7) | Lines in section 3 quoted the round-5 tally (140) as current after round 6 had reached 158. Both lines now carry the round history and end at 158/158 in both runner modes | Not a code claim; verified by reading this file against `node --test` output |
| Staleness and threshold sanity hold in the BUILDER, not only at the door (round 6) | The door refused `staleness_ms: 900000000`, a negative threshold and `warn > hard`; the builder accepted all three, and `staleness_ms: 1e15` settled a reading sampled at the UNIX epoch as `fresh`, `final_state: ok`. All three are now `ReceiptInputError` in `buildLimitReceipt`, which the route turns into a 503 | `a freshness window wider than a day is refused by the builder, not settled green`, `a negative threshold is refused by the builder, so a legitimate zero is never a breach`, `a warning above its hard stop is refused by the builder, because it could never fire` |
| `meter_source` is a provenance claim, not a label (round 6) | A reader returning `null`, an array or a string produced zero usable readings and was still labeled `host_analytics_api`. A non-object return is now `unavailable`, the same rule a throw already had | `a meter reader that returns a non-object is unavailable, never labeled with the reader name`, `a meter reader that returns a real readings object still names its provenance` |
| CI runs the suite in BOTH module-resolution modes (round 6) | `edge/package.json` is what makes these files ES modules; Node's auto-detection papered over its absence, so deleting the file left CI green at 140/140 while `--no-experimental-detect-module` collapsed to 5 tests / 5 failures. CI now runs both commands and asserts the manifest exists | `.github/workflows/ci.yml` steps `Edge layer tests without ESM auto-detection` and `The edge package manifest exists` |
| The assertion header is trimmed before the cookie fallback | A whitespace-only header no longer shadows a valid cookie; a padded header is trimmed rather than treated as malformed | `a whitespace-only assertion header is no token, and must not shadow a valid cookie` |
| Dot-segment normalization is documented and proven both ways | `/api/%2e%2e/x` lands on deny-by-default; `/app/../healthz` normalizes to the public route and returns the public constant `ok`, which is safe only because that route is a fixed string. Written up in `edge/README.md` | `a dot segment that resolves ONTO the public route is the public route, and gives nothing private` |
| Per-isolate memoization of the policy | `resolveMeterPolicy` memoizes successful parses per raw string, so the 10 ms ceiling is not spent re-parsing on every request. A failure is never memoized | `meter thresholds are programmable policy: METER_POLICY_JSON is read, and a broken one is refused` |
| Verifier cache key is unambiguous | `VERIFIERS` keys on `JSON.stringify(settings)` rather than a delimiter that can appear inside a value | `the exported worker keeps one verifier per isolate: two requests, one JWKS fetch` |
| `.gitignore` is scoped | `/edge/wrangler.toml` and `/edge/.dev.vars`, anchored to the repository root rather than matching any `wrangler.toml` or `.dev.vars` anywhere in the tree | Read of `.gitignore`; no test (a gitignore rule is not executable) |

**Merge-time follow-up, owned by the lead.** `edge/fixtures/limit-receipt.sample.json`
is pinned on the JavaScript side only. The Python-side contract validation of
that same sample (feeding it to `curator/contracts/receipt.py` and asserting it
validates) is added **in the main checkout at merge time by the lead**, not
here: this worktree carries no Python, and two keys the sample emits are ahead
of the frozen contract by design (`ReceiptEnvelope.actor_kind` and
`user_id` from the adjudicated Ownership change, and `MeterReading.warning`).
The same note is written next to the fixture path in `edge/meter.test.js` so it
is found by whoever touches the shape, not only by whoever reads this receipt.

**Positive control, re-proven after every round-2 change** (2026-09-02, Node
v22.22.3). The whole auth check in `worker.js` was replaced with a hardcoded
`{ ok: true }` and the suite run: **8 of 100 tests failed**, including
`every private route class denies a signed-out request`,
`a forged or broken token is denied on every private route`,
`a verifier that throws is a denial, never an allow`, and the new
`a UTF-8 percent-encoded Chinese slug is safe and reaches the router`. The
mutation was reverted and the suite returned to 100/100. The suite is proven
capable of failing on the property it exists to defend, and the round-2 tests
are inside that proof rather than beside it.

**One honest finding from the control run.** Removing only the narrow line
`if (!token) return denialResponse('missing_token', 401)` does **not** turn the
suite red, because `verifier.verify(null)` independently denies with
`missing_token`. That line is defense in depth, not the load-bearing check; the
load-bearing check is the `result.ok` test. Recorded because a control that
passes needs an explanation, not a shrug.

### Fix round 3: what changed, and the test that proves each one

Every row is grade **B** (proven in source against injected doubles) unless the
grade column says otherwise. The test column is the exact test title.

| Finding | What changed | Test that proves it | Grade |
|---|---|---|---|
| The JWKS refetch floor was measured from the last SUCCESS, so during an outage inside the stale grace every request attempted a fetch, twice when the `kid` was unknown | `access.js` tracks the last ATTEMPT separately from the last success. While a stale cache is still servable, a failed attempt starts the `jwksMinRefetchMs` floor, and a forced unknown-`kid` lookup cannot repeat a fetch that just failed inside it | `during an outage inside the stale grace, the refetch floor bounds attempts to one per window` (four requests, two on the cached key and two on an unknown one, add exactly ONE fetch attempt; the floor then reopens once per window) | B |
| A reachable key rotation was untested for cost | Unchanged behavior, now pinned | `a key rotation the verifier can actually reach costs exactly one refetch` (asserts the fetch count, and that the rotated-out key stops verifying without further fetches) | B |
| The fixture pinning test WROTE the sample when it was absent, so deleting the file turned a contract break into a green run plus a silently rewritten fixture | The write-if-absent branch is gone. `meter.test.js` reads the fixture unconditionally, so absence is `ENOENT`. Regeneration moved to `edge/scripts/regenerate-fixtures.mjs` (Node standard library only), documented in `edge/README.md`, and the `edge-tests` CI job runs `git diff --exit-code -- edge/fixtures` after the suite | `the receipt envelope matches the committed sample byte for byte`. Proven red twice: renaming `unit` to `units` in the builder gives 92/116, and deleting the fixture gives 114/116 with an `ENOENT` error rather than a rewrite | B |
| `GET /receipt/limits` threw out of `fetch()` on three parseable `METER_POLICY_JSON` values, producing a platform exception carrying none of the private headers | `resolveMeterPolicy` now validates the WHOLE shape (`policy_revision` present, `meters` a plain object so `null` and `[]` are refused, every spec a plain object with a known `meter_kind`, a non-empty `unit`, and finite thresholds), so a defect is a configuration error on the already-tested 503 path. The receipt route body is additionally wrapped in try/catch returning `unavailableResponse()` | `a policy that parses but is incomplete is a 503, never an uncaught exception on the receipt route` (8 configurations including `{"meters":{}}`, `{"policy_revision":null,...}`, `{"meters":{"x":null}}` and a spec missing `unit`, each asserted 503 + `hasRequiredPrivateHeaders` + the constant body); `resolveMeterPolicy refuses every incomplete shape at configuration time`; `a receipt that cannot be built at the route is a 503, not a platform exception` (the route's own catch, tested independently of the validation in front of it); `a meter reader that throws still yields a receipt, and it is all unknown` | B |
| **SC-28.** An empty meter policy, including the project's own commented `wrangler.toml.example` line, produced `final_state: ok`, envelope `state: settled` and a settled timestamp from a receipt with ZERO readings | Every configured meter is a REQUIRED meter. Zero readings is `final_state: unknown`, `state: unknown`, `settled_at: null`, `reason_code: no_meters_configured`. A configured meter not read fresh keeps `unknown` and now NAMES the meters: `meter_stale:<names>`, the same suffix style as the existing `ceiling_breached:<name>`, so no new vocabulary term is introduced ahead of the Python contract. The example in `wrangler.toml.example` is replaced with a complete policy, and the empty case is documented there as measuring nothing | `a policy with zero meters yields unknown, never a settled green receipt` (the exact previously-green input); `the shipped example policy is a complete receipt, and an empty one settles nothing` (through the real `export default`); `an unread required meter is named in the reason code, not just counted` | B |
| A reading silently dropped `meter_kind` and `unit` when the spec omitted them, because `JSON.stringify` drops an `undefined` value | `meter.js` validates all ten `MeterReading` fields present with the right type before serialization; a defect is a typed `ReceiptInputError`, which the route turns into a 503. A spec that is not an object at all is the same refusal instead of a `TypeError` | `a meter spec missing its unit is a refusal, never a reading with the key dropped`; `a meter spec that is not an object at all is a refusal, not a TypeError`; `a well-formed reading carries all ten required fields, every one the right type` | B |
| `HEAD`/`OPTIONS` on `/healthz` returned 405 under a valid configuration and 503 under a broken one, so the gate flipped | Decided: the method gate is **405 always, before the configuration is read**. It moved to the top of the exported `fetch`, ahead of construction | `an unsupported method is 405 under a valid configuration AND under a broken one` (four method/path pairs across all nine `BAD_ENVS`) | B |
| The config-failure test compared bodies only | It now compares the COMPLETE header set across every `BAD_ENVS` case, asserts the set is identical for all nine, and asserts no configuration value appears in any header | `a construction failure returns the identical header set on every bad configuration, and leaks no value into it` | B |
| Mixed-case UTF-8 and overlong UTF-8 probes were correct but unpinned | Pinned as permanent tests alongside `%00` | `mixed-case percent triplets round-trip, and overlong UTF-8 and %00 stay refused` (`/app/%e4%b8%ad%e6%96%87` routes to 200 with a session; `%C0%AF`, `%C0%AE%C0%AE`, `%c0%af`, `%00` and `a%00b` all refused, and 400 at the edge with a valid token) | B |
| The stale-grace rotation semantics were described as buying availability, which is not what they buy | Documented as a known trade-off in `edge/README.md` (a table: a RETIRED key is accepted inside the grace, a rotated-IN key is denied until a refetch succeeds) and in the module comment in `access.js`. No behavior change: the trade is the point of the setting | `the stale grace is a trade, not free availability: it keeps a RETIRED key and denies a rotated-in one` | B |

**Positive control, re-run after every change above.** Removing the whole auth
check from `worker.js` (replacing the token read, the verify call and the
`result.ok` test with `{ ok: true }`) turns **9 of 116** tests red, including the
control's own `CONTROL: the real worker under the same stub is the only
difference`, both signed-out-denial helpers, the forged-token helper, and the
Chinese-slug test. Restored, the suite is 116/116 again. The suite is still
proven capable of failing on the property it exists to defend.

**Fixture red proofs, run this round.** (1) Renaming `unit` to `units` in
`meter.js`: 92/116, and the byte-comparison test is among the failures. (2)
Deleting `edge/fixtures/limit-receipt.sample.json`: 114/116 with
`ENOENT: no such file or directory` and no file rewritten. Both restored before
the final run.

### Fix round 4: what changed, and the test that proves each one

Round 4 raised four must-fixes from the cross-model reviewer and two from the
Claude reviewer (one shared). Every row is grade **B** (proven in source against
injected doubles). The test column is the exact test title.

| Finding | What changed | Test that proves it | Grade |
|---|---|---|---|
| **SC-12.** The round-3 refetch floor was gated on a servable stale cache, and the default `JWKS_STALE_GRACE_MS` is `0`, so the floor was INERT on every default deployment. Warm cache, TTL 600 s, floor 60 s, grace 0, one failed refresh, three verifications: fetch count rose 1 to 4 | The floor is honored unconditionally. Inside `jwksMinRefetchMs` of the last attempt, with no servable cache, `access.js` DENIES WITHOUT FETCHING. In-flight joining is preserved, so concurrent requests still collapse into the one fetch the floor allows | `at the default zero grace, a failed forced refresh does not refetch again inside the floor` (Codex's exact scenario: fetch count stops at 2, the still-valid cache keeps verifying, the floor reopens once per window); `during an outage with no usable cache, five requests cost one outbound fetch`; `concurrent requests still JOIN one in-flight fetch rather than being denied by the floor` | B |
| **SC-28.** `validateMeterPolicy` checked the VALUE of every key it recognized and ignored every key it did not, so `hard_stop` instead of `hard_stop_threshold` was indistinguishable from an absent threshold. A meter reading 5,000,000 against an intended stop of 90,000 settled GREEN with a settled timestamp. `staleness_mss` silently fell back to the 15-minute default | Exact key allowlists at both levels. Top level: `policy_revision`, `staleness_ms`, `meters`. Spec: `meter_kind`, `unit`, `warning_threshold`, `hard_stop_threshold`. An unknown key throws, landing on the already-tested deploy-time 503 path before any request is served | `an unknown or malformed policy key is a configuration error, never a silently disabled control` (18 rejected shapes, plus the shipped policy and revision 0 still accepted); `the misspelled-threshold policy is a 503 on the real dispatch path, not a green receipt at 55x the limit` (each of the 18 through the real `export default`, asserting 503 + `hasRequiredPrivateHeaders` + the constant body) | B |
| `policy_revision` accepted contract-invalid types: `"one"` was emitted unchanged although the frozen contract types it as an integer | Non-negative safe integer only. String, fraction, boolean, object, negative and past-`MAX_SAFE_INTEGER` are configuration errors | Both tests above (the 18 shapes include all six invalid `policy_revision` cases; revision `0` is asserted legal) | B |
| Unvalidated meter ids could forge reason-code semantics: `partner_acquisition_cost;budget_hard_stop`, never read, emitted a reason code that reads as a budget hard stop with no hard stop and no shed action behind it | Meter ids must match `^[a-z][a-z0-9_]{0,63}$`: lowercase snake case, bounded length, no `,` `:` `;`, no control characters. Rejected at policy validation. Ids are therefore non-sensitive by construction, and `edge/README.md` records that, because they are quoted verbatim inside the receipt | Both tests above (the 18 shapes include the forged id, a comma, a colon, a control character, a non-snake-case id and a 65-character id) | B |
| A budget hard stop erased the unread-meter hole: a receipt claimed `settled` with a settled timestamp while 3 of 4 required meters were never read, and named none of them. The evidence doc's own verdict table said otherwise | Both breach branches append `;meter_stale:<names>`. The envelope state is deliberately NOT downgraded: a consumer that filters on `settled` must not be able to miss a real hard stop, so the verdict stays authoritative and the hole is recorded beside it. The verdict table in section 3 is corrected | `a budget hard stop keeps its verdict AND still names the meters that were never read` (3 of 4 unread plus a hard stop; asserts `hard_stop`, `settled`, a settled timestamp, `shed_actions: ["hard_stop:budget"]` AND `budget_hard_stop;meter_stale:cpu,memory,subrequests`); `a breached ceiling names the unread meters too, after the budget hard stop it already names`; `a breach with every meter read fresh carries no meter_stale suffix` | B |
| Should-fix: a negative threshold, and a `warning_threshold` above its `hard_stop_threshold` | Both are configuration errors. A negative threshold is met by every reading including a legitimate zero, so it is not a limit; a warning above its stop can never fire, because the stop is checked first | Both strict-validation tests (two of the 18 shapes) | B |
| Should-fix: fixture regeneration was a direct write, so a crash midway left a truncated fixture that still looked committed | The generator writes a sibling temp file and renames it into place, cleaning up the temp file on failure | Determinism re-verified: two consecutive runs produce 1,541 bytes with sha256 `e668190c18cbd32a9b9738a154734a28a3e75b69f1eb2d3681adf41e82b2d3e4`, identical to the committed fixture | B |
| Should-fix: CI ran `git diff --exit-code -- edge/fixtures` but never ran the regeneration command, so a drifted generator was never exercised | The `edge-tests` job now runs `node edge/scripts/regenerate-fixtures.mjs` between the suite and the diff gate, so a generator that no longer matches the committed contract evidence fails in CI | Not independently testable in this worktree (CI is not run locally). Grade **C** for the CI behavior; the script itself is B | C |
| Should-fix: the whole suite relied on Node's ESM auto-detection, with no `package.json` anywhere in the repo | `edge/package.json` added: `"type": "module"`, `"private": true`, a `test` script, and no dependencies | `node --no-experimental-detect-module --test "edge/*.test.js"` passes 124/124 (it was 0 pass / 5 fail). The CI command is unchanged | B |

**The SC-12 floor decision, recorded with its reason.** Honoring the floor when
no cache is usable means denying without attempting a fetch. That was a real
choice, so the reasoning is written into `access.js` next to the code and
repeated here:

- The denial is **identical either way** until a fetch succeeds. With no
  confirmed key set, every token is refused whether or not another attempt is
  made, so an inbound caller can observe no difference during the outage.
- What changes is **outbound amplification**. At the default grace of `0` the
  old gate was inert: each request cost one outbound fetch and a forced
  unknown-`kid` lookup cost a second, unbounded, and paceable 1:1 by an
  unauthenticated caller sending forged kids, against the 50-subrequest
  per-invocation ceiling.
- The cost, stated plainly: after the upstream recovers, **at most one floor
  window of extra denial** that an immediate retry would have avoided. A bounded
  delay on recovery is recoverable; an unbounded fan-out into a failing
  dependency is not.
- **Correction, round 5.** That cost statement held only under an unstated
  precondition: the cache has to OUTLIVE the floor window. With
  `JWKS_CACHE_TTL_MS < JWKS_MIN_REFETCH_MS` the floor also fires with no outage
  at all, and the denial is not one window on recovery, it is permanent and
  recurring: **50% of authenticated requests denied `key_set_unavailable` at a
  30 s TTL against the default 60 s floor, 83% at a 10 s TTL, against a key set
  answering 200 on every call.** Round 5 enforces the precondition rather than
  reversing the round-4 decision: `createAccessVerifier` now refuses
  `ttlMs < minRefetchMs` at construction, which lands on the already-tested
  deploy-time 503. The round-4 outbound-amplification argument above is
  untouched and remains correct.

**Round-4 mutation checks: the new guards are load-bearing, not decorative.**
Each fix was reverted on a byte copy and the named test went red:

| Mutation (on a copy at `scratchpad/r4fixproof/edge`) | Result |
|---|---|
| Restore the old `&& staleCacheServable(at)` gate on the floor | 122/124, red: both new floor tests |
| Delete both `assertOnlyKeys` calls | 122/124, red: both strict-validation tests |
| Delete the `METER_ID_RE` check | 122/124, red: both strict-validation tests |
| Delete the `policy_revision` safe-integer check | 122/124, red: both strict-validation tests |
| Delete the `staleSuffix` append in the breach branches | 122/124, red: both unread-meter tests |
| Delete the threshold `>= 0` and `warn <= hard` checks | 122/124, red: both strict-validation tests |

**Round-4 red proofs, re-run and restored.** (1) Fixture drift (`unit` to
`units` in `meter.js`): **97/124, 27 fail**, the byte-comparison test among
them. (2) Fixture deleted: **122/124, 2 fail**, both `ENOENT`, and
`edge/fixtures/` still empty afterwards, so nothing self-healed. (3) Positive
control (`verify` replaced with `{ ok: true, claims: {} }`): **87/124, 37 fail**,
including the forged-token helper and every new round-4 floor test. Each was
restored and the suite re-run to 124/124 before the next.

**Correction, round 5.** The tally above is right, but the round-4 sentence
originally also named both signed-out helpers and the Chinese-slug test as
failing under this control, and they do not: measured, all three PASS. The
mutation replaces `verify` inside `access.js`, and a signed-out request never
reaches the verifier (`worker.js` returns 401 on a missing token first), while
the Chinese-slug test asserts a 200 under a valid session and is unaffected by a
verifier that says yes. That narrative belonged to the round-3 mutation, which
removed the auth check from `worker.js` itself. The signed-out denial property
is proven capable of going red separately, by the two CONTROL tests in
`edge/positive-control.test.js` (`a worker with no auth check makes the
signed-out denial test fail` and `a cacheable denial makes the signed-out denial
test fail`), which is what the sentence should have pointed at.

### Fix round 5: what changed, and the test that proves each one

Two must-fixes (one from each reviewer) plus four should-fixes. Both reviewers
confirmed every round-4 item closed on the real code path before finding these.

| Round-5 finding | What changed | Test that proves it | Grade |
|---|---|---|---|
| **Must-fix (Claude MF-1)**: with `JWKS_CACHE_TTL_MS < JWKS_MIN_REFETCH_MS` the round-4 unconditional floor denies authenticated users against a HEALTHY key set. Measured 50% of 120 requests at a 30 s TTL / 60 s floor, 83% at 10 s, with the misleading reason `key_set_unavailable` | `createAccessVerifier` refuses `ttlMs < minRefetchMs` at construction, landing on the already-tested deploy-time 503. The reasoning is written into `access.js` so a later round cannot read it as a reversal of round 4 | `a cache that expires before the refetch floor reopens is refused at construction, not at runtime`, `the pathological configuration can never produce a run of 401s, because it never verifies anything`, `a valid ordering still constructs and still honors the round-4 floor`, and on the real dispatch path `a TTL shorter than the refetch floor refuses at deploy time instead of denying live traffic` | B |
| **Must-fix (Codex 1)**: a negative or non-finite reading settled a receipt green. `value: -1` on all five default meters returned HTTP 200, `final_state: "ok"`, `state: "settled"`, a settled timestamp, and kept every `-1` | A reading must be a finite number `>= 0`. Anything else is an UNREAD meter: `value: null`, `freshness_verdict: "unknown"`, named in the reason code, and it can never settle. The published-value contract tightened the same way | `a negative reading is an unread meter, never a fresh one that settles green`, `every non-finite and negative value shape is refused as a reading` (which also asserts a real `0` still verdicts fresh and settles), and through the route `a receipt built from impossible readings is unknown on the real route, never a settled green` | B |
| **Must-fix (Codex 2)**: numeric configuration was coerced, not parsed. `JWKS_MIN_REFETCH_MS=" "` became `0`, silently disabling the refetch floor; booleans, arrays and hexadecimal were accepted too, and `/healthz` returned 200 | `numericSetting` accepts only a real number or a non-blank plain decimal string. Whitespace, booleans, arrays, hexadecimal, exponent notation, signs and `"Infinity"` are configuration errors on the 503 path | `a numeric setting that is not a number is a configuration error, never a silent coercion` (15 refused shapes, 7 legal ones still parsing) and `a whitespace or non-numeric tuning value reports invalid on /healthz, it does not disable the control` | B |
| **Must-fix (Claude MF-2)**: the round-4 positive-control narrative named three tests as failing that actually pass | Corrected in this document (see the round-4 red-proof paragraph) and re-measured this round | Measured: under that control tests 92, 93 and 120 pass; 94 fails. The signed-out property is covered by the two CONTROL tests in `edge/positive-control.test.js` | B |
| **Should-fix (Claude S1)**: the meter-id grammar was enforced at the configuration door only. `buildLimitReceipt` interpolated any id verbatim, so a forged id emitted `meter_stale:partner_acquisition_cost;budget_hard_stop` | `METER_ID_RE` moved into `meter.js`, the module that does the interpolating, exported, imported by `worker.js`, and asserted inside the `buildLimitReceipt` meter loop as a `ReceiptInputError` (already a 503 at the route) | `a forged meter id is refused by the receipt builder, not just by the config door` (8 forged shapes plus the maximum legal id still building) | B |
| **Should-fix (Claude S2)**: the memoized env policy is shared by every request in the isolate and was not frozen | `validateMeterPolicy` deep-freezes before `POLICIES.set` | `the memoized policy is deep-frozen, so one stray write cannot re-tune every later request` (three mutation attempts all throw `TypeError`) | B |
| **Should-fix (Claude S3)**: `staleness_ms` had no upper bound, so `900000000` (three extra zeros) silently widened 15 minutes to 10.4 days | Refused above `86400000` (24 hours) | `a freshness window wider than a day is a configuration error, not a tuning choice` (4 refused, 4 legal including the exact cap) | B |
| **Should-fix (Claude S4)**: no timeout on the key set fetch, so a hung upstream stalled every request that joined the in-flight attempt instead of denying | New `JWKS_FETCH_TIMEOUT_MS` (default 5000, validated like every other tuning value, must be `> 0`). The fetch is passed an abort signal AND raced against a timer, because a signal alone does not bound an implementation that ignores it | `a key set fetch that never answers becomes a denial, not an unbounded wait` (an injected never-resolving fetch: three concurrent verifications all deny `key_set_unavailable`, still on one outbound attempt, in well under 2 s), plus `a fetch that answers inside the timeout is unaffected by the bound` and `the fetch timeout is a validated tuning value like every other one` | B |

**Round-5 red proofs, re-run and restored** (on a byte copy outside the
worktree, each restored and re-verified to 140/140 before the next). (1) Fixture
drift (`unit` to `units` in `meter.js`): **109/140, 31 fail**, the
byte-comparison test among them. (2) Fixture deleted: **138/140, 2 fail**, both
`ENOENT`, and `edge/fixtures/` still empty afterwards, so nothing self-healed.
(3) Positive control (`verify` replaced with `{ ok: true, claims: {} }`):
**101/140, 39 fail**. The failing set is the auth core (every `access.test.js`
verification and key-set test, the four new round-5 access tests among them),
the forged-token helper, the synthetic-projection test and both exported-worker
isolate/floor tests. It does NOT include the signed-out helpers or the
Chinese-slug test, for the reason recorded in the round-4 correction above: a
signed-out request is refused before the verifier runs.

**Round-5 deferrals, restated unchanged.** All five items still open after round
4 were judged correctly deferred by the round-5 review, and none blocks the
local proof: (1) the CI fixture gate stays vacuous until `edge/` is committed,
and self-closes on that commit; (2) `meter_stale` still collapses never-read and
too-old, pending a `meter_unread` term in the Python contract; (3) the 405 body
wording and the missing `Allow` header remain a WHAT for the owner; (4) whether
a meter with no thresholds should be legal remains a WHAT for the owner; (5) the
entire live Cloudflare half stays grade C by decision, so fail-open-on-exception,
real CPU headroom and the real Access application are still unproven in
production.

Two round-5 should-fixes were NOT taken and are recorded here rather than
silently dropped: `git status --porcelain` instead of `git diff --exit-code` in
the CI fixture gate (Claude S5, which only bites once `edge/` is committed and
only for a NEWLY created fixture file, and the generator writes one known path),
and own-property lookup for readings (Claude S6, probed as not exploitable:
`constructor` is a legal id under the grammar but yields `value: null` and
`final_state: unknown`). Both become worth taking when phase 3 wires an external
meter source; neither is reachable through the shipped configuration today.

### Round-2 should-fixes NOT taken, and why

| Item | What it is | Why it was not taken this round |
|---|---|---|
| S4 | Denial reason codes (`bad_iss` before `bad_aud`, both before any signature check) are an unauthenticated team-name oracle | The Access login redirect already exposes the team domain, so this is confirmation rather than disclosure. Collapsing every wire reason to `denied` and moving the specific reason to a log field is the right fix, and it needs a logging surface this layer does not have yet |
| S5 | `HEAD`/`OPTIONS` get 405 with no `Allow` header, and the 405 body says `"error":"unauthorized"` | **Half closed in round 3.** The lead decided the gate: 405 always, before the configuration is read, so it can no longer flip to 503 on a broken config. The `Allow` header and the body wording remain open, and remain a WHAT |
| S6 | `readAccessToken` returns `''` rather than `null` for an empty cookie, violating its own JSDoc | No live impact: `if (!token)` absorbs it and the verifier denies an empty string anyway. Round-2 work went to the whitespace-header case, which DID have a live consequence (shadowing a valid cookie) |
| S8 | `breached: false` on a never-read meter | Cannot be fixed at the edge alone. `curator/contracts/receipt.py` declares `breached: bool = False`, so emitting `null` breaks the frozen contract. This is a contract WHAT for the lead (`bool \| None`), not an edge defect |
| S9 | No `Content-Security-Policy: frame-ancestors 'none'` and no `Strict-Transport-Security` on the private HTML app | One line each, but both are policy decisions about a surface that does not exist yet (the real app is not written). Adding a CSP now would pin a policy for a page nobody has designed |
| S10 | `reason_code: "meter_stale"` collapses never-read and too-old into one code | **Still open, narrowed in round 3.** The code now NAMES the meters (`meter_stale:<names>`), a suffix in the same style as the existing `ceiling_breached:<name>`, so the reader learns which claim is missing without introducing a new vocabulary term. Splitting never-read from too-old still needs a `meter_unread` term, which is a Python-contract change to make once at merge |

SC-13 note: no credential of any kind is read, stored, or emitted by this layer.
Configuration is two non-secret identifiers supplied as Worker vars, and
`wrangler.toml.example` carries placeholders only. The filled-in copy
(`edge/wrangler.toml`), which does hold the team name, the audience tag, the
hostname and the zone name, is listed in `.gitignore` as of fix round 1 and
anchored to the repository root (`/edge/wrangler.toml`, `/edge/.dev.vars`) in
round 2, so the rule cannot silently swallow an unrelated `wrangler.toml`
elsewhere in the tree. Before round 1 the example file told the reader it was
git-ignored and it was not, in a public repository. Meter thresholds are read from `METER_POLICY_JSON`, so
changing one never needs a source edit, and an unparseable override is refused
rather than silently replaced by the defaults.

Freshness handling, stated precisely because SC-28 turns on it: a reading is
`fresh` only when a value and a stamp inside the staleness window are both
present AND the value is a finite number `>= 0`. Anything else yields
`value: null` and a non-fresh verdict, `unknown` when nothing was read and
`stale` when the read was too old. In no case is an unread meter reported as
`0`. The `>= 0` half was added in round 5: every meter kind here counts
something, so a negative reading is a broken meter, not a low one, and `-1`
compares below every threshold. Five meters reading `-1` used to produce
`final_state: "ok"` with a settled timestamp.

**The empty-receipt rule (SC-28), added in round 3.** The rule above is enforced
row by row, and with zero rows there is no row to enforce it on. So the verdict
is decided for the receipt as a whole too. Every meter the policy configures is
a REQUIRED meter, and:

| Receipt | `final_state` | envelope `state` | `settled_at` | `reason_code` |
|---|---|---|---|---|
| no meters configured (`"meters":{}`) | `unknown` | `unknown` | `null` | `no_meters_configured` |
| a configured meter not read fresh, **and no breach** | `unknown` | `unknown` | `null` | `meter_stale:<the meters>` |
| a budget hard stop **with** meters unread (round 4) | `hard_stop` | `settled` | the timestamp | `budget_hard_stop;meter_stale:<the meters>` |
| a breached ceiling **with** meters unread (round 4) | `ceiling_breached` | `failed` | `null` | `ceiling_breached:<name>[;budget_hard_stop];meter_stale:<the meters>` |

**Precedence, corrected in round 4.** A breach outranks an unproven reading for
the VERDICT, and that stays: a hard stop is authoritative and settled, because a
consumer that acts only on `settled` receipts must never be able to miss a real
stop. What round 4 changed is that a breach no longer ERASES the hole. The
breach branches used to short-circuit the unread-meter branch, so a receipt read
`settled` with a settled timestamp while 3 of 4 required meters were never read
and the reason code named none of them. Both signals are now present.

**The strict-validation rule (SC-28), added in round 4.** A receipt is only an
audit artifact if the policy behind it means what the operator wrote. So
`METER_POLICY_JSON` is validated against EXACT key allowlists, not just checked
for the keys this code happens to recognize:

| Level | Keys accepted | Everything else |
|---|---|---|
| Top level | `policy_revision`, `staleness_ms`, `meters` | configuration error, deploy-time 503 |
| Meter spec | `meter_kind`, `unit`, `warning_threshold`, `hard_stop_threshold` | configuration error, deploy-time 503 |

with `policy_revision` a non-negative safe integer, meter ids matching
`^[a-z][a-z0-9_]{0,63}$`, thresholds finite and `>= 0`, and
`warning_threshold <= hard_stop_threshold`. Before this, `hard_stop` instead of
`hard_stop_threshold` was indistinguishable from an absent threshold, so one
misspelled word silently disabled a spend limit and settled a receipt GREEN at
55x the intended stop, and a meter id could forge the reason-code grammar.

What strictness refuses when its assumption is wrong: a forward-compatible
policy carrying a field this build has not learned yet. That trade is taken
deliberately. The policy already carries `policy_revision`, so adding a field is
a versioned change, and the refusal is a deploy-time 503 before any traffic,
recoverable in seconds by editing an env var. A silently disabled limit is not
recoverable, because nobody learns it was disabled.

An empty policy used to settle GREEN with a timestamp: the strongest verdict
this system can issue, from a receipt that could not reach a meter source. The
input was the project's own commented `wrangler.toml.example` line, which is now
a complete policy. SC-28 audits this artifact, so a false green here is the one
that matters. Reading shape is enforced for the same reason: all ten
`MeterReading` fields must be present with the right type before serialization,
because `JSON.stringify` drops an `undefined` value and a spec missing its
`unit` would otherwise publish a row with no `unit` key at all.

---

## 4. Not proven, and why (grade C)

| Claim | Why it is still C |
|---|---|
| A real signed-out request to a real Access-protected hostname is denied | No deployment exists. The tests exercise this layer's own verifier, not the platform's |
| Access in front of the Worker actually redirects rather than passing through | Needs a real Access application |
| Real CPU per request fits inside 10 ms on real story volumes | Needs a deployment and real traffic. This is the SC-28 measurement the named fallback exists to avoid depending on |
| Real memory headroom against 128 MB | Same |
| The GraphQL meter returns the fields we expect for this account | Needs an API token |
| Free tier includes 50 Access seats | Fact #17: no vendor page read on 2026-09-02 stated a number |
| Cached private responses cannot be retrieved by an unauthenticated caller | The headers are set and asserted here. Whether the edge cache honors them for this configuration is a live test |
| The named SC-28 fallback fits on real volumes | The fallback removes the slate build from the request path, which is the load-bearing part. Whether what remains fits inside 10 ms of CPU on real story volumes is unmeasured until the live half runs |
| An Ask AI turn at the edge stays inside the ceilings | Design position only. No Ask AI code exists in `edge/`. It would be a subrequest against the 50-per-request limit, and it is CPU-free only if the Worker relays the response rather than parsing it, a rule nothing enforces today |

---

## 5. Owner actions, in order

1. **Create the Cloudflare Access application** for the private hostname. Record
   the team name and the application audience tag. Neither goes in this
   repository: they go into the deploy environment.
2. **Confirm the free seat count** in the Zero Trust dashboard (fact #17 is
   unverified) and confirm the seat count needed for the pilot fits it.
3. **Configure the route fail closed**, so that exceeding the daily request
   limit produces the 1027 error page rather than bypassing the Worker
   (fact #6). A bypassed Worker is an unauthenticated Worker, which makes this
   the single most severe SC-12 failure mode in the design. Treat it as a
   deploy-checklist step with a recorded verification in the live-half receipt,
   not a bullet someone is asked to remember: nothing in this repository can
   enforce or observe a route setting.
4. **Decide the deploy path.** Two options, and this is a real decision, not a
   formality:
   - Install the vendor CLI locally. That is an external package install, so it
     needs the two-round security scan first. Nothing was installed for this
     slice.
   - Or deploy from a scheduled job using the vendor's HTTP API with a scoped
     token held as a repository secret. No local install, and the deploy path is
     visible in the job log. This is the lighter of the two and avoids adding a
     local dependency to a public repository's toolchain.
5. **Mint a read-only analytics token** for the GraphQL meter, separate from the
   deploy token, so a meter read can never deploy anything.
6. **Then run the live half**: deploy, curl the hostname signed out and confirm
   the denial, sign in and confirm the private projection, pull one real
   `workersInvocationsAdaptive` reading, and record actual CPU percentiles
   against the 10 ms ceiling. That is what turns rows in section 4 from C to A.
