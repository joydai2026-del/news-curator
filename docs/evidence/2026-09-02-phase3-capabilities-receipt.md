# Phase 3, second slice: declared capabilities, cross-run health, route retyping

Date: 2026-09-02. Base commit: `d2a5dca`. Additive only: the hourly pipeline's
fetch behaviour is unchanged, and nothing new is wired into it yet.

## What landed

| Part | New code | New tests |
|---|---|---|
| 1. Declared capabilities | `curator/sources/capabilities.py`, plus a `capabilities()` method on each concrete adapter | `tests/test_source_capabilities.py` (32) |
| 2. Cross-run health record | `curator/sources/health_record.py` | `tests/test_source_health_record.py` (70) |
| 3. Route retyping | one `type: atom` declaration in `topics.yaml` | `tests/test_source_route_typing.py` (10) |

Counts above are MEASURED after review round 6 (one `pytest` run per file):
32 + 70 + 10 = 112. They were left at the round-3 numbers through rounds 4 and 5
and were corrected in round 5. Every round of both independent reviews is
answered in the "Review round N" sections at the end of this receipt.

`SourceAdapter` in `curator/sources/base.py` is untouched. `DeclaresCapabilities`
in the new module is a separate typing hook, so fetching and declaring stay
separate obligations.

## Part 1: declared capabilities

Six registry keys, four declarations (`FeedAdapter.capabilities()` reads
`self.type_key`, so `feed`, `rss` and `atom` each declare under their own id).
Every value below is derived from adapter code, not from a provider's marketing.

| Registry key | poll | push | full text | trend | social | deletion | incremental checkpoint | consumes search queries | languages |
|---|---|---|---|---|---|---|---|---|---|
| `feed` | yes | no | no | no | no | no | no | no | (none declared) |
| `rss` | yes | no | no | no | no | no | no | no | (none declared) |
| `atom` | yes | no | no | no | no | no | no | no | (none declared) |
| `news_sitemap` | yes | no | no | no | no | no | no | no | (none declared) |
| `json_feed` | yes | no | no | no | no | no | no | no | (none declared) |
| `hackernews` | yes | no | no | **yes** | **yes** | no | no | **yes** | (none declared) |

### The code line behind each non-obvious value

| Value | Justification, read from source |
|---|---|
| `supports_full_text=False` on the feed family | `feed.py` calls `enforce_xml_bounds(..., enforce_text_limit=False)` with a comment saying publisher feeds embed full article bodies "that this adapter never consumes"; `_entry_summary` truncates to `description_chars` (default 600). |
| `supports_full_text=False` on `json_feed` | `parse_json_feed` reads `summary` only. It never touches `content_html` or `content_text`, the JSON Feed body fields. |
| `supports_full_text=False` on `news_sitemap` | `parse_news_sitemap` builds `Item` with no `description` argument at all. A news sitemap carries title, URL, publication date and image, and nothing else the adapter reads. |
| `supports_full_text=False` on `hackernews` | `_to_item` sets no `description`. |
| `supports_trend_signal=False` on the feed, JSON Feed and sitemap adapters | Each sets `native_rank=native_rank if spec.category == "trending" else None`, where `native_rank` is the adapter's own `enumerate` index. That is an operator's label on a route, not a value the publisher sent. `feed.py` says so in place: a position in a publisher's general feed "is not comparable with HN Trending". |
| `supports_trend_signal=True` on `hackernews` | `fetch` requests `tags=front_page` and assigns `native_rank` from the source's own returned order. |
| `supports_social_signal=True` on `hackernews` | `_to_item` reads `points` off the wire into `Item.score`. Those are votes cast by people. No other adapter reads any interaction count. |
| `consumes_search_queries=True` on `hackernews` only | `_query_plans` reads `context.queries` and turns each term into its own request. The other five adapters are handed the same queries and never look. This is the Gate 0c split, now declared and behaviourally tested. |
| `supports_incremental_checkpoint=False` everywhere | No adapter sends a conditional GET or reads `SourceContext.durable_store`. Checkpointing is greenfield, as the frozen contract records. |
| `supports_push=False` everywhere | Every adapter only issues outbound GETs. There is no inbound path in the source layer. |
| `supports_deletion=False` everywhere | No adapter has any notion of a retracted or tombstoned item. |
| `languages=()` everywhere | The adapter imposes no language restriction: the route declares its language through `SourceSpec.language` and the adapter stamps that onto every item. Empty means "no adapter-side restriction", not "serves no languages". The reading is written into the module docstring so it cannot drift. |

### How the declaration is checked rather than trusted

`consumes_search_queries` is the one flag with a behavioural test. For each of
the six keys the adapter is run twice against the same recorded fixture, once
with no queries and once with two `SourceQuery` values. The declaration must
match TWO independent signals, because either one alone can be fooled:

| Signal | What it measures | What it would miss alone |
|---|---|---|
| Digest | The requests issued, the COMPLETE normalized item (`dataclasses.astuple`, all 22 `Item` fields) for every item, AND `SourceResult.health` plus `SourceResult.note` | Nothing observable, but only because the digest is total. A four-field projection missed query-derived `native_rank` and `native_categories`; an item-only digest missed queries folded into the note alone. Both proven red below. |
| Access | Whether the adapter read `SourceContext.queries` at all, measured by a transparent sequence proxy installed after context construction (`__post_init__` copies any sequence back to a plain tuple) | An adapter that reads the queries and happens to produce identical output |

**The proxy is a tuple subclass with every C-level slot overridden.** Two
failures had to be closed at once, and round 2 traded one for the other:

| Hazard | What breaks | How it is closed |
|---|---|---|
| A subclass inherits tuple's C slots, so `count`, `in`, `==`, `reversed`, `index`, `len` and indexing answer in C and leave the flag `False` | An adapter consumes the queries through any of those and passes the access signal | Every one of them is overridden explicitly as a recording Python method. `__hash__` stays tuple's, because a hash reveals nothing about the contents and defining `__eq__` would otherwise make the type unhashable |
| A proxy that is NOT a tuple changes the type the adapter sees | An adapter guarding its read with `isinstance(context.queries, tuple)` reads in production and skips the read under test, so both signals report nothing and the gate goes green on a lie | The proxy subclasses `tuple`. `_TypeSensitiveLiar` is that exact adapter, kept as a permanent red gate |

Round 2 made the proxy an ordinary object to close the first hazard and thereby
opened the second. Both are closed now; neither wording in this receipt's round
2 section describes the current proxy.

**Coverage, stated exactly.** Eight access paths record
(`test_the_tracking_proxy_records_every_way_a_sequence_can_be_read`, one
parametrized row each), which also guards the signal against reading `False`
for everyone and passing vacuously. `!=` is NOT one of them: tuple answers it in
C and the proxy does not override it. Nothing in the suite or in any adapter
reaches the queries that way, and naming the hole is more useful than the
earlier claim that "no read of the queries goes unrecorded", which was not true
of any version of this proxy.

**Both signals run through `registry.fetch` ON A REAL `SafeHttpTransport`
(round 4).** Two earlier accounts of this were wrong, and both corrections are
kept visible rather than overwritten:

- Round 2 SPLIT the legs, justified by "the access proxy cannot survive
  `registry.fetch`". FALSE; a round-3 probe falsified it.
- Round 3 shared one leg on a stub transport and wrote that if the stub ever
  became a real transport the access signal would "quietly read `False` for
  every adapter". FALSE AND BACKWARDS. Measured: `registry.fetch` scopes the
  policy with `dataclasses.replace` for a real transport; `replace` re-runs
  `SourceContext.__post_init__`; its `tuple(self.queries)` ITERATES the proxy,
  which both marks it read and hands the adapter a plain tuple. The signal
  reads `True` FOR EVERYONE, honest adapters included. Loud and wrong, not
  silent and wrong. Round 3's related claim that the proxy was installed after
  construction so "`__post_init__` cannot downcast it" is likewise withdrawn:
  `__post_init__` DOES downcast it on the production path; it did not here only
  because the branch that would downcast it never fired under the stub.

Round 4 closes it rather than restating it. The transport is now a genuine
`SafeHttpTransport` with an injected resolver and connector (the construction
`tests/test_safe_transport.py` already uses), so the stub sits at the SOCKET,
below every check the transport performs, and the policy-scoping branch
production always takes fires in both legs. The tracking proxy is installed by
`_TrackedAdapter` at the ADAPTER BOUNDARY, inside the wrapped adapter's own
`fetch`, so it is built from whatever the adapter actually received, after
every rebuild. The access signal therefore no longer depends on the transport
type at all.

Two tests hold both halves of that. `test_instrumenting_on_the_callers_context_is_what_the_real_branch_defeats`
asserts the MEASURED failure (adapter gets a plain tuple, proxy reads `True`,
adapter read nothing), so the withdrawn claim cannot be re-asserted.
`test_the_tracking_proxy_is_the_object_the_adapter_receives` asserts the
transport IS a `SafeHttpTransport`, that the object the wrapped adapter
received IS the installed proxy, and that the read is observed on `hackernews`
and not on `rss`.

Fixtures used (all recorded, no network; the suite is socket-blocked):
`cnbeta.xml` for `feed` and `rss`, `buzzing.xml` for `atom`, `cnn-news.xml` for
`news_sitemap`, `daring-fireball.json` for `json_feed`, `hn-front-page.json`
for `hackernews`.

A second test names the difference rather than accepting any difference: with
queries, Hacker News issues extra requests carrying `query=Claude`; without
them it issues none. The `rss` adapter's two runs are identical.

## Part 2: cross-run health record

`fold_source_health(previous, health, observed_at, *, plugin_id="", note="")` is pure:
no I/O, no clock of its own, no persistence. Persistence lands with the
checkpoint wiring.

**THE FOLD MEASURES DELIVERY, NOT CLEANLINESS.** Decided 2026-09-02 and written
into the `health_record.py` module docstring as a dated design note. Rounds 1
and 2 reversed each other on whether a partial run should freeze the counters. A
reversal means the question was not decidable from the evidence either round
held, so it is settled on an explicit second-order principle and recorded in the
code, not in a reviewer's head.

| Field | Rule |
|---|---|
| `last_success_at` | Stamped with the observation time whenever the run delivered at least one usable item; carried otherwise. |
| `consecutive_failures` | Counts consecutive runs that delivered zero usable items or failed in transport. Resets to 0 on ANY delivery. |
| a PARTIAL run | Recorded ONLY as a reason-code signal, `partial:<reason>`. The partial-ness moves neither the counter nor the stamp; delivery decides both, exactly as for any other run. |
| `disabled` | The one non-delivery case: the route was not polled, so there was nothing to measure and both counters carry through untouched. |

**Why that way round.** A route delivering 30 items per run is not failing,
whatever hint it carries, and an alert built on a frozen `last_success_at` would
page on a healthy route. A route delivering nothing IS failing, whatever hint it
carries. Making "not clean" a third counter state puts both of those errors into
one field. The cleanliness signal is real and is kept, in the reason code.

**Dead-but-200 detection uses `newest_item_age_hours` and `status`, never the
counter.** A frozen archive delivers every run, so its counter sits at 0 and its
success stamp keeps advancing: correct, and the reason the age axis exists.

The rest of the record's fields:

| Field | Rule |
|---|---|
| `newest_item_age_hours` | Recomputed from the newest item's publication against the supplied observation time, clamped at 0. `SourceHealth.age_hours` is deliberately never read, so the age is always measured against the clock this fold was given. |
| `status` | Recomputed, not copied. The live status string is consulted only for the two conditions this layer cannot re-derive: transport failure and parse failure. |
| `observed_at` | Must not move backwards. A STRICTLY older observation raises `HealthFoldOrderError` rather than rewinding `observed_at` and `last_success_at`. Re-observing at the SAME moment is decided by CONTENT, not by the timestamp: if the status, usable-item count, newest-item age and composed reason code all match, it is a true replay and the PREVIOUS RECORD ITSELF comes back (`same is previous`), so a replayed run cannot advance a counter. If they differ it is a different observation sharing one stamp, and it raises `HealthFoldOrderError` naming both. A caller that replays history in order never reaches any of these branches. |
| `reason_code` | Bounded at 120 characters, the codebase's own precedent (`snapshot_health_reason`, `source_snapshot.py:363`). The composed reason stacks a `partial:` prefix, the upstream causes and the appended note; the measured worst realistic case was 150 characters before this bound. Anything longer is cut at a segment boundary and ends with `;trunc:<8 hex of the sha256 of the complete composed reason>` (the round-5 form; see the truncation rows below), so the field stays self-describing rather than being clipped later by whatever column persistence gives it. |
| naive datetimes | Refused as `ValueError` wherever they sit: `observed_at`, `health.newest_at`, `previous.observed_at`, `previous.last_success_at`. All four are validated BEFORE the equality short-circuit, so a naive value never surfaces as a `TypeError` from a comparison deeper in. |

**THE NOTE CHANNEL REACHES THIS FOLD AND NOTHING ELSE, TODAY.** The partial
marker is the mechanism the round-2 fix rides on, so where it stops matters:

| Question | Answer |
|---|---|
| How does a partial run reach the fold? | Only when a caller passes `note=` (it is `SourceResult.note`, the one field `base.py::_health` never rewrites) |
| Does any production caller pass it? | NO. Nothing in `curator/` calls `fold_source_health` at all |
| Would the note reach the fold if one did? | Only from inside `curator.pipeline.collect` (the name `_source_tier` used earlier in this file does not exist in the tree). `curator/pipeline.py` builds the sources tier from `result.items` and `result.health` and replaces every per-source note with one `"N source alerts"` summary, so `SourceResult.note` dies in that stack frame |
| Does the snapshot carry it? | NO. `_health_dict` in `source_snapshot.py` has ten keys and none is a note |
| So what does wiring cost? | Either calling the fold inside `curator.pipeline.collect` while the result is still in hand, or adding a per-source note to the snapshot health row. Either route is possible future implementation work. This receipt does not set its sequence or priority. |

This slice deliberately does not wire it: persistence is behind a hard boundary
here. `test_the_pipelines_health_row_carries_no_note_for_the_fold_to_read`
records the gap as an assertion rather than a sentence, and is written to be
changed deliberately if either wiring route is implemented rather than leaving
the gap to be discovered accidentally.

**Status precedence**, fixed and documented in the module: disabled, then
unavailable, then malformed, then empty (zero usable items), then stale (newest
older than the route's `max_age_hours`), then link-resolution-degraded, then
fresh. Stale outranks degraded links on purpose: indirect links are an accepted
condition, a route that stopped publishing is the failure this record exists to
surface.

**Why two axes.** `consecutive_failures` catches an outage. It cannot catch a
frozen archive, because a frozen archive succeeds every run. The regression test
replays one real recorded feed across four observation times and shows exactly
that: status FRESH then STALE, STALE, STALE, with the age growing past ten times
the route's threshold, while `consecutive_failures` stays at 0 the whole way.
Reading only the counter would call that route healthy forever, which is the
miss these records were frozen to close.

### Finding: a live status outside the frozen vocabulary

`HackerNewsAdapter` emits `status_hint="degraded"` (`hackernews.py`), and
`_health` in `base.py` leaves a non-empty hint in place when items exist.
`degraded` is not one of the seven `HealthStatus` values the contract freezes,
and the contract states health status stays within that vocabulary.

**Neither the enum nor the adapter changed in this slice.** The enum is frozen,
and changing the emitted string would change live behaviour. What changed is
the fold: legacy hints are now normalized from ONE table, with a test per row.

| Hint | Treatment | Reason code recorded | Counters |
|---|---|---|---|
| `""` | No hint was given; classify by evidence | unchanged | normal (success if items, else failure) |
| `degraded` | PARTIAL: the run was not clean; status still comes from items and age | `partial:<original reason>` | follow DELIVERY: reset and stamp when items exist, a failure when it delivered nothing (design note above) |
| anything else | FAIL CLOSED | `unrecognized_status_hint:<hint>` | counted as a failure |

The bug this closes: before the fix an unknown hint fell through to `FRESH` and
RESET `consecutive_failures`, so a degraded Hacker News run cleared the route's
failure history.

**Escalated, not decided here.** The frozen `HealthStatus` vocabulary has no
value meaning "delivered items, run was not clean". `partial:` in the reason
code is a workaround inside this layer, not a status. Whether the enum gains a
partial-success value is a contract question for the next freeze revision.

### Finding: what the reason code can and cannot carry

`_health` in `base.py` OVERWRITES `reason_code` with `newest_item_too_old`, and
the status with `stale`, whenever items exist and the newest is older than the
route's threshold. A degraded run that is ALSO stale therefore reaches the fold
with neither its `degraded` status nor its original reason. `base.py` is a hard
boundary for this slice, so it was not changed.

What survives is `SourceResult.note`, which the adapter sets separately and
`_health` never touches. So the note is the channel a partial run signals
through:

| Mechanism | Detail |
|---|---|
| The marker | A `;`-separated note segment equal to `partial` or starting with `partial:`. Read BEFORE classifying, so a run relabelled `stale` upstream still records `partial:<reason>`. Segment-wise, never substring: `partially_degraded` is a cause, not a marker. |
| Who emits it | `HackerNewsAdapter` now prefixes its degraded-branch note with a `partial` segment (`note=";".join(("partial", *notes))`). That is the only source change; `reason_code` and `status_hint` are unchanged, and `base.py` is untouched. |
| What else the note carries | The rest of the note is appended as `<reason>;note:<note>`, skipped only when the note equals a COMPLETE existing segment. Substring comparison silently swallowed a real note: `query_failures:1` reads as contained in `query_failures:10` while meaning something else. |

Proven through the REAL path in
`test_the_partial_marker_in_the_note_survives_base_pys_stale_rewrite`, which
builds the line with `success_result(...)` against a previous FAILURE record,
and in
`test_a_degraded_run_that_is_also_stale_loses_its_reason_before_the_fold_sees_it`
for the no-marker case, rather than hand-constructing a `SourceHealth` the live
producer cannot emit.

### One known bypass of the staleness signal

Named in the module docstring so a later alert is not built on the wrong field.

| Bypass | Effect |
|---|---|
| `feed.py` stamps `published_at=min(published, now)` | A frozen archive whose entries are dated in the FUTURE reads FRESH with age 0 forever. The age axis cannot see that route. Pre-existing, not introduced here. |

The second item listed here in round 1 (`last_success_at` advances on every
dead-but-200 run) is NOT a bypass under the delivery rule: such a run really did
deliver, so the stamp is correct. It is restated above as the reason a
dead-but-200 alert must read `status` and `newest_item_age_hours`.

Also latent, not live: a run with `status="unavailable"` or `"malformed"` and
`usable_items > 0` records `consecutive_failures=0` and a success stamp. Today
no adapter can emit that shape (`hackernews.py` guards its unavailable branch
with `not items`), and the salvaged-malformed case genuinely did deliver items,
so the behaviour was left as is rather than changed on a hypothetical.

## Part 3: route retyping

**Retyped: one route.** `buzzing` (in `topics.yaml`) moves from the inherited
`rss` default to `type: atom`.

| Evidence | Result |
|---|---|
| Format proof | The recorded capture `tests/fixtures/feeds/buzzing.xml` was taken from the route's own URL and parses as `atom10`. Its root element is `<feed xmlns="http://www.w3.org/2005/Atom">`. |
| Equivalence proof | Both adapters are driven through `registry.fetch` (the live dispatch path: registry to `guarded_fetch` to the adapter's own `fetch`) on the SAME recorded payload with a stub transport, and the COMPLETE `Item` sequences are compared (`as_rss.items == as_atom.items`, every field of every item). A companion test sabotages `AtomAdapter.fetch` only and asserts the comparison separates them, so the proof cannot pass without actually exercising the atom adapter. |
| Behaviour risk | None found. `AtomAdapter` subclasses `FeedAdapter` and overrides only `type_key`, so the request (same allowed MIME types) and the parse are the same code. The legacy fetcher in `curator/fetchers/rss.py` branches on `news_sitemap` only, so it treats `atom` exactly as it treated `rss`. |
| Visible change | `SourceHealth.source_type` for this route now reads `atom` instead of `rss`. The existing fixture sweep in `tests/test_source_contracts.py` was updated to expect it. |

**Not retyped, with the reason.**

| Route or adapter | Why not |
|---|---|
| `cnbeta`, `solidot`, `google-36kr` | Captures parse as `rss20`. Already correctly typed. |
| `dw-zh` | Capture parses as `rss10` (RSS 1.0 / RDF). Correctly typed `rss`: it is served by the same adapter and there is no `rdf` discriminator to move it to. |
| `cnn-news`, `fox-news` | Already `news_sitemap`. |
| `json_feed` adapter | Still zero routes. The only recorded JSON Feed capture (`tests/fixtures/sources/daring-fireball.json`) belongs to NO configured route, so there is nothing to retype and no equivalence to prove. |
| `theregister`, `simonw` | Atom candidates, not JSON Feed: `sources.yaml` gives them `https://www.theregister.com/headlines.atom` and `https://simonwillison.net/atom/everything/`. Neither has a recorded capture under `tests/fixtures/feeds/`, and the bar is equality against a capture, so neither was retyped. |
| `feed` adapter | Still zero routes by design. It is the base class behind the `rss` and `atom` configuration aliases; a route typed `feed` would say less than either. |

A test asserts that the only registered adapters serving zero routes are exactly
`feed` and `json_feed`, so a future zero-route adapter cannot appear unnoticed.

## Verification

| Check | Result |
|---|---|
| `ruff check curator tests` | All checks passed. |
| Full suite, base `d2a5dca` | 1468 passed, 8 skipped, 6 deselected. |
| Full suite, after this slice (round 1 as built) | 1512 passed, 8 skipped, 6 deselected. |
| Full suite, after review round 1 fixes | 1526 passed, 8 skipped, 6 deselected. |
| Full suite, after review round 2 fixes | 1545 passed, 8 skipped, 6 deselected. +19 against round 1, +77 against base. |
| Full suite, after review round 3 fixes | 1563 passed, 8 skipped, 6 deselected. +18 against round 2, +95 against base. |
| Full suite, after review round 4 fixes | 1570 passed, 8 skipped, 6 deselected. +7 against round 3, +102 against base. |
| Full suite, after review round 5 fixes | **1575 passed, 8 skipped, 6 deselected.** +5 against round 4, +107 against base, 0 regressions, skip and deselect counts unchanged throughout. |
| Focused files, after review round 5 (collected) | capabilities 32, health record 65, route typing 10 = **107**, all passing in one run. |

### Red-gate proof for part 1

The capability gate was proven to fail when a declaration disagrees with
behaviour. Two declarations were temporarily flipped: `hackernews` to
`consumes_search_queries=False` and the feed family to `True`.

```
4 failed, 9 passed
AssertionError: hackernews declares consumes_search_queries=False but observed True
AssertionError: rss declares consumes_search_queries=True but observed False
AssertionError: atom declares consumes_search_queries=True but observed False
AssertionError: feed declares consumes_search_queries=True but observed False
```

Both declarations were restored and the file returns 14 passed. The gate fails
in both directions: claiming a capability the adapter lacks, and denying one it
has.

### Red-gate proof that the WIDENED digest is what has teeth

A throwaway probe (deleted after the run) monkeypatched `FeedAdapter.fetch` to
fold `context.queries` into `native_rank` and `native_categories` while leaving
the requests and the declaration (`consumes_search_queries=False`) untouched.
This is exactly the adapter shape the review said would slip through.

```
FAILED tests/test_zz_redgate_probe.py::test_probe_rss
AssertionError: rss declares consumes_search_queries=False but observed True
assert False is True
```

The same probe run also scored the SAME sabotage with the pre-fix four-field
digest `(title, canonical_url, published_at, description)`, and it PASSED
(`1 failed, 1 passed`): the old projection could not see the difference. So the
widening from four fields to `dataclasses.astuple` is what closed the hole, not
the sabotage being obvious.

### Red-gate proof for part 3 (the route-typing equivalence test)

A second throwaway probe (deleted after the run) replaced `AtomAdapter.fetch`
with a function that raises, patching the subclass only, and called the
equivalence test. Before the fix this test compared `parse_feed_document` with
itself and stayed GREEN under the same sabotage. Now:

```
FAILED tests/test_zz_route_probe.py::test_probe_equivalence_goes_red
>       assert as_rss.items == as_atom.items
E       AssertionError: assert (Item(title='...', ...),) == ()
E         Left contains one more item
tests/test_source_route_typing.py:115: AssertionError
```

`guarded_fetch` contains the adapter failure, so the sabotage surfaces as an
empty item tuple rather than an exception. The permanent test
`test_the_equivalence_proof_actually_exercises_the_atom_adapter` keeps that
check in the suite.

## Merge note: what a merge to `main` actually changes

`topics.yaml` changed, and the public hourly Curate workflow is triggered by
changes to it AND by changes under `curator/**` (five files there changed), so
merging this slice RETRIGGERS that workflow for two reasons, not one. The change
is still behaviour preserving for items, but it is not invisible:

| Surface | Before | After |
|---|---|---|
| Requests issued, and the parsed items | unchanged | unchanged (`AtomAdapter.fetch is FeedAdapter.fetch`; `Item` carries no source-type field) |
| `SourceHealth.source_type` for `buzzing`, which reaches the health JSON (`curator/health.py`) and the source snapshot (`curator/source_snapshot.py`) | `rss` | `atom` |
| The route's configuration digest in the source snapshot | computed over `rss` | computed over `atom` |
| `SourceResult.note` on the Hacker News degraded branch | `front_page_stale;...` | `partial;front_page_stale;...` (`hackernews.py:276`) |

Nothing else in `curator/` branches on `rss` versus `atom`: the legacy fetcher
in `curator/fetchers/rss.py` branches on `news_sitemap` only.

The Hacker News note prefix is the ONLY runtime-visible source change in this
slice, and it changes nothing downstream: `SourceResult.note` is read by no
production code (see the note-channel table above), and `TierResult.degraded`
tests a note for truthiness only, on a branch already guarded by `if notes:`.
It is observable in `tests/test_source_contracts.py:219` and nowhere else.

## Review round 1: every finding and what was done

Two independent reviews (one Claude adversarial, one Codex) both returned
FIX-FIRST, and both aimed at the EVIDENCE rather than the design.

| Finding | What was done | Proving test |
|---|---|---|
| Claude MF-1 / Codex 2: the query gate compared 4 of 22 `Item` fields, so query-derived ranking passed as "does not consume" | Digest widened to `dataclasses.astuple(item)` over every field, AND a second signal added: `SourceContext.queries` is wrapped in a tracking tuple that records whether it was read; the declaration must match both | `test_declared_search_query_consumption_matches_observed_behaviour`, `test_the_tracking_sequence_actually_records_a_read`; red-gate proof above |
| Claude MF-2 / Codex should-fix: the route-typing test called `parse_feed_document`, exercising neither adapter, while its docstring and receipt line claimed otherwise | Both parses now go through `registry.fetch` with a stub transport and full `Item` sequence equality; docstring and the receipt's equivalence row corrected | `test_retyping_buzzing_to_atom_produced_identical_normalized_items`, `test_the_equivalence_proof_actually_exercises_the_atom_adapter`; red-gate proof above |
| Codex 1: `status_hint="degraded"` is outside the frozen enum, and the fold read an unknown hint as FRESH and reset the failure counter | Table-driven normalization with a test per row; unknown fails closed; `degraded` is PARTIAL (status from evidence, counters untouched, reason `partial:<reason>`). **SUPERSEDED in round 2 by the delivery decision: "counters untouched" is no longer the behaviour, delivery decides them, and the two tests named in this row's last two slots no longer exist under those names.** Enum NOT changed, adapter string NOT changed, escalated as a contract question | `test_every_legacy_status_hint_row_is_normalized_as_documented`, `test_an_unrecognized_status_hint_fails_closed_rather_than_reading_fresh`, `test_a_partial_run_delivers_items_without_clearing_the_failure_history`, `test_a_partial_run_that_delivered_nothing_is_still_a_failure` |
| Claude MF-3: the receipt and docstring claimed a degraded run's `reason_code` survives; `base.py` `_health` overwrites it with `newest_item_too_old`, and the proving test hand-built a `SourceHealth` | The false claim is gone. `SourceResult.note` is the one field that survives, so the fold takes an optional `note` and records `<reason>;note:<note>`. Proven through `success_result`, the real path, not a hand-built line | `test_a_degraded_run_that_is_also_stale_loses_its_reason_before_the_fold_sees_it`, `test_a_note_is_not_repeated_when_the_reason_already_carries_it` |
| Claude MF-4: an out-of-order observation walked `observed_at` and `last_success_at` backwards | Refused with a typed `HealthFoldOrderError` rather than silently keeping the newer record. Same-moment re-observation stays idempotent | `test_an_observation_older_than_the_record_is_refused_not_silently_applied` |
| Claude SF-2, SF-3: two staleness bypasses (future-dated items clamp to age 0; `last_success_at` advances on a frozen archive) were named nowhere | Both written into the module docstring and into this receipt | documentation only; SF-3 is asserted by the existing regression test's `record.last_success_at == observations[-1]` |
| Claude SF-1: `unavailable` / `malformed` with items > 0 records a success | Left as is, deliberately. Not reachable through today's adapters, and the salvaged-malformed case really did deliver items. Recorded above as latent | none (documented, not changed) |
| Claude SF-8: nothing in production calls `declared_capabilities` or `fold_source_health` | Unchanged and still stated up front: this slice lands the functions and their tests, not a wired health path | none |
| Claude SF-9: the `theregister` / `simonw` note was filed under the `json_feed` row where it does not belong | Split into its own row | none (receipt fix) |
| Claude SF-4, SF-5, SF-6, SF-7 | Not changed in this round. SF-4 (`supports_trend_signal` on an aggregator that emits `native_rank`) is a definition question worth its own decision; SF-5 and SF-6 are correct readings of what those assertions can and cannot prove; SF-7 (one item in the capture) is answered by the widened comparison | none |

## Review round 2: every finding and what was done

Two independent reviews again (one Claude adversarial, one Codex), both
FIX-FIRST. Two round-1 findings were still open, three were new.

| Finding | What was done | Proving test |
|---|---|---|
| Codex still-open 1: a `degraded` HN run that is ALSO stale reaches the fold as `stale`, so the partial signal was lost and the counters reset | SETTLED BY PRINCIPLE, not by another reversal. The counter reset is now the DECIDED behaviour (the fold measures delivery, dated design note in the module docstring). The lost SIGNAL is fixed: the note carries a `partial` marker, read before classifying, and `HackerNewsAdapter` emits it | `test_the_partial_marker_in_the_note_survives_base_pys_stale_rewrite` (through `success_result` with a previous failure record), `test_a_partially_prefixed_word_is_not_read_as_the_partial_marker`, and the HN assertion in `tests/test_source_contracts.py` |
| Codex still-open 2 / Claude MF-A: folding the same observation twice at the same timestamp incremented `consecutive_failures` | `moment == previous.observed_at` returns `previous` itself, after source validation. `HealthFoldOrderError` is now reserved for strictly older moments. Every naive datetime raises the same `ValueError`, never a `TypeError` | `test_replaying_the_same_failed_observation_does_not_advance_the_counter`, `test_replaying_the_same_successful_observation_leaves_the_record_alone`, `test_a_naive_timestamp_on_the_previous_record_is_refused_not_a_type_error`, `test_a_naive_newest_item_timestamp_is_refused_before_anything_is_folded` |
| Claude: the idempotence assertion at `tests/test_source_health_record.py:464-468` could not fail (it asserted `observed_at` matched, which the fold stamps from the moment it was handed) | Replaced with `same is previous` plus a counter check, so it asserts the record really was returned unchanged | `test_an_observation_older_than_the_record_is_refused_not_silently_applied` |
| Codex 3 / Claude MF-B: `TrackingQueries` was a tuple subclass, so `count()`, `in`, `==` and `reversed` read the contents in C and left `read` False; and the digest covered items only, so queries folded into `SourceResult.note` were invisible | Proxy rewritten as a transparent non-tuple sequence marking eight access paths; digest widened to include `SourceResult.health` and `SourceResult.note`; the access leg moved onto the direct-adapter call because `registry.fetch` copies the proxy away | `test_the_tracking_proxy_records_every_way_a_sequence_can_be_read` (8 rows), the three red gates below, `test_the_note_liar_is_caught_by_the_digest_and_not_only_by_the_access_signal` |
| Codex should-fix: `_with_note` compared substrings, so `note="query_failures:1"` was swallowed by `reason_code="query_failures:10"` | Compares COMPLETE `;`-separated segments | `test_a_note_that_only_looks_contained_in_the_reason_is_still_recorded` |
| Both reviews: the missing partial-success enum value | STILL ESCALATED, deliberately not decided here. The frozen `HealthStatus` has no "delivered items, run was not clean" value; `partial:` in the reason code is a workaround inside this layer. A contract question for the next freeze revision |  none (escalation) |

### Red gates added this round (permanent tests, not throwaway probes)

Three adapters that declare `consumes_search_queries=False` and consume the
queries anyway, each through a path the round-1 harness could not see. If the
gate stops failing these, it has stopped being a gate.

| Fake adapter | How it consumes | Which round-1 hole it proves closed |
|---|---|---|
| `_CountingLiar` | `context.queries.count(...)`, output identical | A tuple subclass answers `count` in C; the old flag stayed `False` |
| `_ContainmentLiar` | `QUERIES[0] in context.queries`, output identical | Same C-slot hole, via `__contains__` |
| `_NoteLiar` | Reads the queries and folds them into `SourceResult.note` only: no item, request or health change | The old digest covered items only. NOTE: this adapter alone does not prove that hole closed, because it reads through `tuple(context.queries)` and the access signal catches it on `__iter__`. The dedicated `test_the_note_liar_is_caught_by_the_digest_and_not_only_by_the_access_signal` is what pins the digest widening |

`test_an_adapter_that_consumes_queries_while_declaring_otherwise_is_caught`
asserts all three produce a non-empty violation list, and
`test_an_honest_adapter_that_ignores_the_queries_passes_the_same_gate` asserts
the gate can still say yes, so the red gates are not passing on a gate that
always fails.

### Both round-1 sabotages re-run against the round-2 harness

Throwaway `tests/test_zz_redgate_probe.py`, created, run, and deleted.

Sabotage 1, `native_rank` probe: `FeedAdapter.fetch` monkeypatched to fold
`len(context.queries)` into `native_rank`, declaration untouched. STILL RED:

```
FAILED tests/test_zz_redgate_probe.py::test_probe_rss
type_key = 'rss'
AssertionError: rss declares consumes_search_queries=False but the queries
changed what it produced: True
```

Sabotage 2, `AtomAdapter.fetch` patched to raise (subclass only; asserted
`AtomAdapter.fetch is FeedAdapter.fetch` first). STILL RED:

```
FAILED tests/test_zz_redgate_probe.py::test_probe_equivalence
>       assert as_rss.items == as_atom.items
E       AssertionError: assert (Item(title='...', ...),) == ()
E         Left contains one more item
tests/test_source_route_typing.py:115: AssertionError
```

Both went red in the same run (`2 failed in 0.18s`), and the file was deleted
afterwards.

### Evidence grades for this round

| Claim | Grade | Basis |
|---|---|---|
| Full suite 1545 passed, 8 skipped, 6 deselected | B | Run here: `python -m pytest -p no:cacheprovider -o addopts="" -m "not allow_socket"` |
| `ruff check curator tests` clean | B | Run here: `All checks passed!` |
| Every behavioural claim in Parts 1 and 2 above | B | Each row names the test that proves it; all pass in the run above |
| The hourly pipeline is unaffected | B | Static: nothing calls `fold_source_health` or `declared_capabilities` in `curator/` outside tests; the only source change is the HN note prefix, which no pipeline code branches on |
| Anything about live production behaviour | C | Nothing here has run against a live route |

## Review round 3: every finding and what was done

Two independent reviews again (one Claude adversarial, one Codex), both
FIX-FIRST. Three must-fixes and six should-fixes across the two, applied as a
union. Both reviews independently reproduced 1545 passed and clean ruff before
this round; Codex's full-suite rerun was blocked by its read-only sandbox and it
graded that row B.

| Finding | What was done | Proving test |
|---|---|---|
| Claude MF-1: an equal-timestamp fold returned `previous` after comparing the TIMESTAMP ALONE, so a transport failure arriving at the same stamp as a success was silently discarded, and three docstrings plus a receipt line called that branch "a replay" | The equal moment is now decided by CONTENT. The observation is classified first, then compared on status, usable-item count, newest-item age and composed reason code. Equal means a true replay and `previous` comes back; different raises `HealthFoldOrderError` naming both sides. The reasoning is written into the code (why sharing a stamp is not evidence of being the same run) per the §3.5 stopping rule, and all three "replay" sentences plus the receipt row now say what is actually checked | `test_a_different_observation_at_the_same_moment_is_refused_not_dropped`, `test_a_replay_that_differs_only_in_its_note_is_refused_too`, `test_the_same_observation_at_the_same_moment_is_still_the_same_object`, and the two pre-existing replay tests |
| Codex 1: the tracking proxy was not the type production supplies, so an adapter guarding its read with `isinstance(context.queries, tuple)` read in production, skipped the read under test, produced identical output, and passed the gate | `TrackingQueries` is a `tuple` subclass again, with explicit recording overrides for `__iter__`, `__getitem__`, `__len__`, `__contains__`, `__eq__`, `__reversed__`, `count` and `index`, and `__hash__` kept as tuple's. It is still installed after `SourceContext` construction so `__post_init__` cannot downcast it. Codex's exact liar is a permanent red gate. The receipt's exhaustive-coverage wording is corrected: eight paths record, `!=` does not, and that is now stated rather than claimed away | `test_an_adapter_that_consumes_queries_while_declaring_otherwise_is_caught[type_sensitive]`, `test_the_tracking_proxy_is_a_tuple_so_a_type_check_cannot_route_around_it`, `test_the_tracking_proxy_records_every_way_a_sequence_can_be_read` (8 rows) |
| Claude MF-2: the stated reason for splitting the two signal legs was false. The proxy DOES survive `registry.fetch` under this file's transport, because the `dataclasses.replace` branch keys on `SafeHttpTransport` and `RecordingTransport` is not one. So the access leg ran one call SHALLOWER than production, not deeper, and no leg ran the production shape | The access leg runs through `registry.fetch` as well; no leg is direct. The false justification is deleted from both the module docstring and this receipt, and the real coupling is pinned by a test that asserts the stub is not a `SafeHttpTransport`, that the proxy is the object the adapter received, and that the read is observed on `hackernews` and not on `rss` | `test_the_tracking_proxy_is_the_object_the_adapter_receives` |
| Claude MF-3: `SourceResult.note` is dropped by `pipeline.py` before any persistence, so the partial-marker channel the whole round-2 fix rides on dead-ends before any durable boundary, and the receipt presented it as a working channel | Stated plainly, not wired. Persistence is behind this slice's hard boundary, so the gap is documented in `health_record.py`'s module docstring and in the note-channel table above (the marker reaches the fold only when a caller passes `note=`, no production caller does, and wiring could either call the fold inside `curator.pipeline.collect` or add a per-source note to the snapshot health row). The assertion is designed to change deliberately if that wiring is implemented. This receipt assigns no project order. | `test_the_pipelines_health_row_carries_no_note_for_the_fold_to_read` |
| Claude SF-1: round 2's request for a bound on `reason_code` was dropped, and this round's prefixes made the measured worst case 150 characters against the codebase's own 120 precedent | Bounded at 120 (`_MAX_REASON_CODE`), truncated with a stable `;truncated` marker, named in the module docstring and in the field table above, before persistence creates a column | `test_the_composed_reason_code_is_capped_at_the_codebases_own_120`, `test_a_reason_code_that_already_fits_is_left_exactly_as_composed` |
| Codex should-fix: no explicit note-parser tests for the edges | Eight parametrized rows covering no partial segment, multiple partial segments, `partial:cause:with:colons`, `cause:partial`, the empty note, the bare marker, a non-leading marker and the `partially_degraded` prefixed word | `test_which_notes_mark_a_run_partial` (8 rows), `test_multiple_partial_segments_mark_the_reason_once_not_twice` |
| Codex should-fix: the assertion after `assert same is previous` was redundant | Replaced with an independent expectation (`same.consecutive_failures == 0`), not a restatement of the identity | `test_an_observation_older_than_the_record_is_refused_not_silently_applied` |
| Codex should-fix: the merge section omitted the Hacker News note-prefix change | Added as its own row, with the reason it changes nothing downstream and the one test that observes it | `tests/test_source_contracts.py:219` |
| Codex should-fix: the round-2 wording claiming "counters untouched" was superseded and named tests that no longer exist | Marked SUPERSEDED in place, rather than rewritten, so the reversal stays visible | none (receipt fix) |
| Codex should-fix: a paragraph interrupted the field table, orphaning four rows | Table repaired with its own header | none (receipt fix) |
| Claude SF-3: the receipt credited `_NoteLiar` itself with proving the item-only-digest hole closed, and on its own it does not (it reads through `tuple(...)`, so the access signal catches it) | Credit moved to the dedicated test in the red-gate table | none (receipt fix) |
| Claude SF-2: the partial marker is recorded twice for the common case (`partial:` prefix plus the note's own `partial` segment) | NOT changed, and the round-3 justification for that is CORRECTED below in the round-4 table. Two of its three grounds hold (it is cosmetic; stripping segments out of the note is exactly the kind of quiet rewrite of an operator-visible field that should not ride along in a fix round). The third ("the 120-character bound removes the length argument") was FALSE and is withdrawn | none (deliberately unchanged) |
| Claude SF-4 / Codex: still no production caller | Unchanged and still stated at the top of this receipt. After three rounds the honest status is: **detectable, not yet detected, and the signal channel does not yet reach anything durable** | none |

### Red gates re-run this round

| Gate | Mutation applied | Result |
|---|---|---|
| Round-1 sabotage 1: `FeedAdapter.fetch` folds `len(context.queries)` into `native_rank`, declaration untouched | monkeypatch, throwaway probe file | RED on `rss`, `atom` and `feed`: `declares consumes_search_queries=False but the queries changed what it produced: True`. Now TWO violations per route, not one, because the access leg runs through the registry too |
| Round-1 sabotage 2: `AtomAdapter.fetch` patched on the subclass only (`AtomAdapter.fetch is FeedAdapter.fetch` asserted first) | monkeypatch, throwaway probe file | RED: `AssertionError` at `tests/test_source_route_typing.py:115`, left tuple has one more item |
| Codex's type-sensitive liar | reverted `TrackingQueries` to a non-tuple proxy | RED: `AssertionError: _TypeSensitiveLiar passed a gate it should fail`, plus `test_the_tracking_proxy_is_a_tuple_so_a_type_check_cannot_route_around_it`. 2 failed, 27 passed |
| Content-equal idempotence | equal-moment branch reverted to deciding on the timestamp alone | RED: `test_a_different_observation_at_the_same_moment_is_refused_not_dropped` and `test_a_replay_that_differs_only_in_its_note_is_refused_too`. 2 failed, 54 passed |
| Reason-code bound | `_bounded` removed from the composition | RED: `assert 150 == 120`, matching the reviewer's independently measured worst case exactly. 1 failed, 55 passed |

The throwaway probe file (`tests/test_zz_redgate_probe.py`) was created, run and
deleted; every mutation above was reverted and the full suite re-run green
afterwards.

### Evidence grades for this round

| Claim | Grade | Basis |
|---|---|---|
| Full suite 1563 passed, 8 skipped, 6 deselected | A | Run here: `python -m pytest -p no:cacheprovider -o addopts="" -m "not allow_socket"` |
| `ruff check curator tests` clean | A | Run here: `All checks passed!` |
| Every red gate in the table above | A | Each mutation applied, run, and reverted in this session |
| Every behavioural claim in Parts 1 and 2 | B | Each row names the test that proves it; all pass in the run above |
| The note channel reaches no production consumer | B | Read from source: `pipeline.py` `collect`, `source_snapshot.py` `_health_dict`; asserted by `test_the_pipelines_health_row_carries_no_note_for_the_fold_to_read` |
| The hourly pipeline is unaffected | B | Static: nothing in `curator/` calls `fold_source_health` or `declared_capabilities` outside tests; the one runtime-visible source change is the HN note prefix, which no production code reads |
| Anything about live production behaviour | C | Nothing here has run against a live route |


---

## Round 4 review fixes (union of both legs)

Both legs returned FIX-FIRST. Claude raised 3 must-fixes plus should-fixes,
Codex raised 4 must-fixes. The union is applied below. Every `B` names its
test.

| Finding (leg) | Fix | Test | Grade |
|---|---|---|---|
| Claude MF-1 / Codex 2: the 120-character bound ran BEFORE the equal-moment comparison, so everything past the cut was invisible to the replay check and a genuinely different observation sharing one stamp was returned as `previous`, silently | The fold now composes the COMPLETE reason (`composed`), compares on it, and applies `_bounded` only on the way into the record. `_same_observation` takes the complete value and bounds it itself, documenting that `previous.reason_code` is the stored stand-in. The ordering constraint is written into the code at the call site | `test_a_different_observation_past_the_bound_at_one_moment_is_refused` (differing tail past 120 characters at one timestamp, must raise `HealthFoldOrderError`), `test_a_true_replay_past_the_bound_is_still_the_same_object` | B |
| Which half of that fix is load-bearing (measured, not assumed) | Mutating the ORDERING back to bound-then-compare leaves the suite green: the digest below already makes the stored form injective. Mutating `_bounded` back to the round-3 fixed-offset marker turns 3 tests red. So the DIGEST is the mechanism and the ordering is the second line of defence. This is stated in the code comment rather than claimed the other way round | mutation runs recorded in the fix receipt | B |
| Codex 3 / Claude SF-1: truncation cut mid-segment (forging the cause `query_failur` out of `query_failures:99`) and the `;truncated` marker was an ordinary segment name, so two different oversized reasons stored identically and a reason genuinely ending in `truncated` was indistinguishable from a truncated one | `_bounded` now keeps only COMPLETE `;` segments and ends with `;trunc:<8 hex characters of the sha256 of the complete composed reason>`. The frozen record is unchanged: this is a representation rule inside the existing `reason_code` field. A segment of the form `trunc:<hex>` is documented as NOT a recognized cause | `test_the_composed_reason_code_is_capped_at_the_codebases_own_120` (asserts every kept segment is a complete segment of the composed value, and that `query_failur` no longer appears), `test_two_different_oversized_reasons_do_not_store_as_one_value` (Codex's exact collision pair), `test_the_truncation_marker_is_a_digest_of_the_complete_reason` | B |
| Claude SF-2 carried from round 3 (the doubled `partial` marker): the round-3 justification claimed the 120 bound removed its length argument | The deferral STANDS; the justification is corrected. That ground was false: the bound did not remove the length problem, it converted it into MF-1 and SF-1. It is withdrawn above, and the doubled-marker case is now MOOT as a length argument: truncation cuts on segment boundaries and identifies the complete value by digest, so eight characters of duplication no longer risk corrupting a trailing cause or colliding two observations. The remaining grounds (cosmetic; do not quietly rewrite an operator-visible field in a fix round) are the real ones | none (deliberately unchanged) | B |
| Codex 1 / Claude MF-2 / Claude MF-3: the access signal was instrumented on the CALLER's context, so under a real transport `dataclasses.replace` plus `__post_init__` both consumed the proxy and downcast it. An honest adapter read as a liar, and an adapter keying its read on `isinstance(context.transport, SafeHttpTransport)` passed the gate green | Instrumentation moved to the ADAPTER BOUNDARY (`_TrackedAdapter`), and BOTH legs now run through `registry.fetch` on a real `SafeHttpTransport` with an injected resolver and connector (the stub sits at the socket, not at the transport type). All six live adapters, the liars and the honest adapters run the policy-scoping `replace` branch production always takes | `test_declared_search_query_consumption_matches_observed_behaviour` (6 rows), `test_an_adapter_that_consumes_queries_while_declaring_otherwise_is_caught[transport_sensitive]`, `[type_sensitive]`, `test_an_honest_adapter_reads_as_unread_through_the_real_transport_branch`, `test_the_tracking_proxy_is_the_object_the_adapter_receives` | B |
| Claude MF-3: three places asserted the failure mode BACKWARDS ("the access signal would quietly read `False` for everyone") and two claimed `__post_init__` "cannot downcast" the proxy | Every one corrected to the measured behaviour in `tests/test_source_capabilities.py` (module docstring and test docstrings) and in this receipt. The falsified claim is kept EXECUTABLE so it cannot be re-asserted | `test_instrumenting_on_the_callers_context_is_what_the_real_branch_defeats` | B |
| Codex 4: the historical future-wiring test pinned only ONE of the two documented wiring routes, so wiring the other left it green | Rewritten as `test_the_fold_is_not_wired_into_the_production_collection_path_yet`. It now asserts BOTH facts: the snapshot health row carries no `note` key (route A) AND `fold_source_health` is not named anywhere in `curator/pipeline.py`, with `curator.pipeline.collect` reading no `result.note` (route B). Its docstring states which assertions change for either implementation route. The test describes a technical gap, not project order. | `test_the_fold_is_not_wired_into_the_production_collection_path_yet` | B |
| Round-4 side finding (§3.16): the module docstring and both reviews named the production collection function `_source_tier`, which does not exist anywhere in the tree | Corrected to `curator.pipeline.collect`, with the stale name called out in place so the correction is visible | `test_the_fold_is_not_wired_into_the_production_collection_path_yet` (it calls `inspect.getsource(pipeline.collect)`, so a rename turns it red) | B |
| Claude SF-2 (structural): the round-3 Hacker News merge-note row was orphaned by a blank line, so it rendered as literal pipe text | Blank line deleted; the row is inside the table again | none (document) | B |
| Claude SF-3: `assert not isinstance(transport, SafeHttpTransport)` could not fail | Gone. The transport IS a `SafeHttpTransport` now, and the assertion reads the other way | `test_the_tracking_proxy_is_the_object_the_adapter_receives` | B |
| Codex should-fix: "stop calling the stub route the full production dispatch path" | Moot: it is no longer a stub route. Both legs run the real transport and the real policy-scoping branch | as above | B |
| Codex should-fix: focused ruff was unavailable in the configured venv | `ruff check curator tests` was run from the shell ruff on PATH: **All checks passed.** The project venv still has no `ruff` module; that is a tooling gap, not a code finding | none | A |
| Claude SF-5 / Codex, carried unchanged and still true | `fold_source_health` and `declared_capabilities` still have NO production caller, and the note channel still reaches nothing durable. Re-grepped this round: the only `fold_source_health` reference in `curator/` is the `__init__.py` re-export. The honest status remains **detectable, not detected, signal channel not yet durable** | `test_the_fold_is_not_wired_into_the_production_collection_path_yet` | B |

### Round-4 red-gate and mutation runs

| Run | Result |
|---|---|
| Round-1 sabotage 1 (`native_rank` folded from `len(context.queries)` in `FeedAdapter.fetch`, declaration untouched) | RED. `rss`, `atom`, `feed` each report `declares consumes_search_queries=False but the queries changed what it produced: True` plus the access-leg violation. 5 failed, 27 passed |
| Round-1 sabotage 2 (`AtomAdapter.fetch` overridden on the subclass only) | RED. `AssertionError` at `tests/test_source_route_typing.py:115`. 1 failed, 9 passed |
| `_TypeSensitiveLiar` (Codex's round-3 liar), through the real transport branch | RED as required (caught by the gate) |
| `_TransportSensitiveLiar` (Claude's round-4 liar), through the real transport branch | RED as required (caught by the gate) |
| Honest adapter through the real transport branch | Reads as UNREAD, no violations |
| Mutation: revert the access leg to caller-side instrumentation | RED, and in exactly the direction the corrected documentation now states: 9 failed, including all five non-consuming live adapters and both honest-adapter tests. Every honest adapter reported as a liar |
| Mutation: `_bounded` reverted to the round-3 fixed-offset `;truncated` marker | RED. 3 failed, including `test_a_different_observation_past_the_bound_at_one_moment_is_refused` with `DID NOT RAISE HealthFoldOrderError`: the round-3 silent drop reproduced |
| Mutation: bound BEFORE the equal-moment comparison (round 3's ordering) | GREEN. Recorded, not hidden: the digest alone closes the bug, so the ordering is defence in depth, not the mechanism |

## Review round 5 (final pass): what was found, what changed

Both legs returned FIX-FIRST and both named this the last pass. Every row below
names the test that holds it. Grades: A = run in this session, B = read from
source in this worktree, C = not verified.

| Finding (leg) | Fix | Test that holds it | Grade |
|---|---|---|---|
| Claude MF-1 / Codex 1: the bounded reason is not a collision-free observation identity. `_bounded` returned any value of 120 characters or fewer VERBATIM, so a SHORT reason ending in a well-formed `;trunc:<8 hex>` segment stored identically to the truncated form of a long one, and the equal-moment check returned `previous` with no exception, no counter and no record. The note channel made the forgery constructible. Codex's own pair: `"a" * 121` versus the literal `";trunc:e9615320"` | ONE rule, closing both legs, in two halves that are independently pinned. (a) ESCAPE ON THE WAY IN: `_bounded` now escapes the caller's text before appending the marker. A `;` segment matching `trunc:<8 hex>` becomes `\trunc:<8 hex>`, a backslash becomes two, a control character becomes `\xNN`. The escape is injective, so it cannot merge two inputs, and after it the only bare `;trunc:<hex>` tail in a stored value is one this module wrote. ESCAPING was chosen over REJECTION because rejection throws away a real observation, and would need its own reason code, which is the very channel under repair. (b) A TRUNCATED PRIOR IS NON-REPLAYABLE: when the record already stored carries the reserved suffix, an equal-moment fold raises `HealthFoldOrderError` naming the truncation, instead of comparing through the stored form. The governing sentence is written into `fold_source_health` and into the module docstring: A SHORTENED REPRESENTATION CAN PROVE DIFFERENCE BUT NEVER SAMENESS | `test_a_short_reason_cannot_impersonate_a_truncated_long_one`, `test_two_long_reasons_sharing_a_digest_prefix_are_still_not_folded_as_one`, `test_a_note_cannot_forge_the_truncation_marker`, `test_a_true_replay_past_the_bound_is_refused_not_silently_accepted` | A |
| Claude MF-1, second half: the absolute claim that licensed the whole comparison. `health_record.py` asserted "THE STORED FORM IS A COLLISION-FREE STAND-IN FOR THE COMPLETE ONE" and `_same_observation`'s docstring generalized it to "two different complete reasons cannot share one stored form" | Both corrected in place. The module docstring now states the one-way property, names what is NOT true and why, and lists the two rules that make the frozen field safe. `_same_observation` now states the exact scope of what its comparison can conclude, and the two caller guarantees it rests on | `test_two_long_reasons_sharing_a_digest_prefix_are_still_not_folded_as_one` (the digest is pinned to a constant, so the two stored forms are byte-identical and the fold must still refuse: the refusal does not rest on the digest) | A |
| Claude MF-1, third half: Codex's round-4 prescription ("carry `reason_truncated` separately, do not encode this state solely through an allowed text segment") and its fallback were both declined without the declination being written down | Written into the module docstring: `SourceHealthRecord` is frozen and is a hard boundary this slice must not cross, the separate field is the right shape for the next contract revision, and the two rules above are what makes the frozen field safe until then. Codex's FALLBACK ("fail closed on equal timestamps whenever either reason required truncation") is now IMPLEMENTED, not declined; it costs the over-the-bound replay guarantee, and the cost is stated where the code makes it | as above | B |
| Claude MF-2: the conditional-liar class is unbounded and unnamed, and has cost three rounds (type-sensitive, transport-sensitive, and round 5 constructed spec-sensitive and count-sensitive) | DECIDED: no sixth liar. The boundary is written into three places instead: `capabilities.py`'s module docstring, `tests/test_source_capabilities.py`'s module docstring, and this receipt. The gate proves exactly two things: that the adapter's `fetch` READS the queries object, or that changing the queries changes the complete normalized result. An adapter consuming query semantics through another channel (spec options, transport-level parameters, counting via a different object) is outside what this gate can see, and its declaration is enforced by REVIEW, not by this test. The existing two liars are kept | the two existing liars stay as permanent red gates; the boundary is documentation by design | B |
| Claude SF-1: the fail-closed path could emit `;trunc:<hex>` with an EMPTY cause. When no complete segment fit the budget the loop broke immediately and returned 15 characters naming nothing, on the branch built from an unvalidated live status string | When nothing fits, a hard-cut head of the first NON-EMPTY segment is kept, prefixed `cut:` so it can never be read as a complete cause | `test_a_fail_closed_reason_never_stores_without_naming_its_cause`, and the marker assertion in `test_two_different_oversized_reasons_do_not_store_as_one_value` | A |
| Claude SF-2: the bound implemented only HALF the precedent it cites. `snapshot_health_reason` is `_text(..., 120, ...)` and `_text` enforces length AND no character below `ord` 32; `_bounded` enforced only the length, so this fold could produce a row that persistence would hard-reject | Folded into the same escape: a control character becomes `\xNN`. The test calls the REAL validator (`curator.source_snapshot._text`), not a restatement of it | `test_the_bound_enforces_the_whole_of_the_precedent_it_cites` | A |
| Claude SF-3: this receipt's own first table said 29 / 56 / 10 and went stale in round 4 | Refreshed to the MEASURED counts, 32 / 65 / 10 = 107, with the staleness named in place | measured by `pytest --collect-only`, one run per file | A |
| Claude SF-4: `_transport(registry, type_key)` took a `registry` it never used | Parameter removed; all four call sites updated | full suite green | A |
| Claude SF-5: round-4's tautology survived inverted. `assert isinstance(transport, SafeHttpTransport)` reads a local assigned two lines earlier from `_transport`, which constructs one | Both occurrences deleted, replaced by a comment naming why they were tautologies and which assertion in each test has teeth (`seen["queries"] is not tracked` for the rebuild, `seen["queries"] is wrapper.tracked` for the boundary delivery). Mutation A turns both of those red | `test_instrumenting_on_the_callers_context_is_what_the_real_branch_defeats`, `test_the_tracking_proxy_is_the_object_the_adapter_receives` | A |
| Codex should-fix: an adapter using `if type(context.queries) is tuple:` reads queries in production and skips under `TrackingQueries`, and the live probe returned no violations | Covered by the MF-2 boundary text, which names exact-type checks and every other harness-visible discriminator as outside the gate rather than implying the liar list is total | documentation by design (see MF-2) | B |

### Round-5 red-gate and mutation runs

Sabotages ran against a copy of the worktree (`scratchpad/r5fixmut`, `.git`
pointer removed); the worktree itself was never mutated.

| Run | Result |
|---|---|
| Round-1 sabotage 1 (`FeedAdapter.fetch` folds `len(context.queries)` into its output, declaration untouched) | RED. **5 failed, 27 passed**, on `atom`, `feed`, `rss`, plus `test_the_tracking_proxy_is_the_object_the_adapter_receives` and `test_search_query_consumption_is_visible_in_the_requests_themselves`. Same count as rounds 4 and 5's reviews |
| Round-1 sabotage 2 (`AtomAdapter.fetch` overridden on the subclass so it stops being a pure relabel) | RED. **1 failed, 9 passed**, `AssertionError` at `tests/test_source_route_typing.py:115`, "Left contains one more item" |
| `_TypeSensitiveLiar` and `_TransportSensitiveLiar`, through the real transport branch | Both still caught. `test_an_adapter_that_consumes_queries_while_declaring_otherwise_is_caught[type_sensitive]` and `[transport_sensitive]` pass |
| Over-120 replay pair, after the round-5 rule change | `test_a_different_observation_past_the_bound_at_one_moment_is_refused` still refuses; `test_a_true_replay_past_the_bound_is_still_the_same_object` is REPLACED by `test_a_true_replay_past_the_bound_is_refused_not_silently_accepted`, because the truncated record is no longer replayable. That is a deliberate behaviour change, not a regression |
| MUTATION C: `_escaped` reverted to the identity function | RED. **3 failed, 62 passed**: `test_a_short_reason_cannot_impersonate_a_truncated_long_one`, `test_a_note_cannot_forge_the_truncation_marker`, `test_the_bound_enforces_the_whole_of_the_precedent_it_cites` |
| MUTATION D: the truncated-prior refusal disabled | RED. **2 failed, 63 passed**: `test_a_true_replay_past_the_bound_is_refused_not_silently_accepted` (DID NOT RAISE) and `test_two_long_reasons_sharing_a_digest_prefix_are_still_not_folded_as_one` (a forced digest collision folds silently). The two halves of the fix are independently pinned: neither mutation kills the other's tests |
| MUTATION E: the SF-1 hard-cut head removed | RED. **2 failed, 63 passed**: `test_a_fail_closed_reason_never_stores_without_naming_its_cause` and `test_two_different_oversized_reasons_do_not_store_as_one_value` |
| `ruff check curator tests` (shell ruff on PATH; the project venv still has no `ruff` module) | All checks passed |
| Hard boundaries: `git diff --stat d2a5dca` over `curator/sources/base.py`, `curator/contracts`, `curator/sources/checkpoint.py`, `tests/test_source_checkpoint.py`, `tests/fixtures/checkpoints`, `curator/ledger`, `supabase`, `docs/contracts` | EMPTY |

### What round 5 changed about an earlier round's own claim

Round 4 recorded, correctly for its rules, that the DIGEST was the mechanism
closing the over-the-bound silent drop and that compare-first-bound-last was
defence in depth. Round 5's rule change supersedes that: a truncated record is
now refused rather than compared through, so NEITHER the digest NOR the
ordering is what closes it. Both are kept (the digest still distinguishes two
truncated values in storage and in the error message; composing before bounding
is what gives that message the complete incoming observation), and the comment
in `fold_source_health` says so in place rather than leaving the round-4
sentence standing. The round-4 rows above are left as written, with this
correction recorded, per this file's SUPERSEDED-in-place convention.

## Review round 6: what was found, what changed

Round 6 was the first round in which the Claude leg returned SHIP. The Codex leg
returned FIX-FIRST on one new must-fix, and both legs are answered here.

| Finding | Leg | Fix | Red gate |
|---|---|---|---|
| **MF-1. The composition namespace was forgeable.** `_with_note` joined the reason code and the note with a bare `;note:` separator and the escape ran on the JOINED string, so a caller segment reading `note:...` was indistinguishable from the separator. `("timeout;note:dns", "")` and `("timeout", "dns")` composed to one stored value, and an equal-moment fold of the second onto the first returned the first record instead of raising: an observation silently dropped | Codex | `_compose` escapes each caller field INDEPENDENTLY and then joins, and `_escaped` reserves the module-owned `note:` and `cut:` prefixes the same way it already reserved a whole `trunc:<8 hex>` segment. `_bounded` no longer escapes at all: it receives escaped text and only measures and cuts, so no structural marker is written twice | MUTATION F2 (round-5 composition restored exactly: escape the joined string, nothing reserved) is RED at **3 failed, 67 passed** |
| **SF-A.** One sentence and one test NAME asserted the opposite of the file's own boundary paragraph, on the exact-type shape | Claude | The module sentence now says the proxy defeats an `isinstance` guard and names the exact-type check as OUTSIDE the gate; the test is renamed `..._so_an_isinstance_check_cannot_route_around_it`; the boundary lists in `curator/sources/capabilities.py` and the test module docstring now both name `type(context.queries) is tuple` | Documentation. No behaviour change, no new liar |
| **SF-B.** The fail-closed hard cut was a fixed-offset slice over already-escaped text, so it could split a `\xNN` or leave a dangling backslash | Claude | The head is built unit by unit through `_escape_units`, so a cut lands only between escape units | MUTATION G (fixed-offset slice restored) is RED at **1 failed, 69 passed**, on the named pads 98, 99 and 100 |
| **SF-C.** `cut:` was declared a module marker but was not reserved out of caller text, unlike `trunc:<8 hex>` | Claude | `cut:` joined `note:` in `_RESERVED_PREFIXES`, so a caller segment beginning with it is escaped | MUTATION H (`cut:` unreserved) is RED at **1 failed, 69 passed** |
| **SF-D.** `_bounded` was recorded in round 5 as idempotent, and escaping made that false | Claude | Recorded in the module docstring and in `_bounded`'s docstring, accurately rather than loosely: after this round `_bounded` alone IS idempotent (it no longer escapes), and the non-idempotent step is `_escaped` inside `_compose`. The technical rule for any future caller is therefore "never send a stored reason back through `_compose`" | Documentation |
| **SF-E.** A real Hacker News reason composes past 120 characters, so refused over-the-bound replay is that route's common case, not an edge case | Claude | Recorded in the module docstring beside the refusal rule, with the operational consequence stated plainly: on that route an IDENTICAL replay of a long reason RAISES, a caller is expected to fold once per observation, and a persistence retry must key on the record it already wrote rather than on re-folding | Documentation |
| **SF-F.** `topics.yaml` carries an owner identifier and em dashes at four and three pre-existing lines | Claude | OUT OF SCOPE for this additive slice, and left alone deliberately. All seven lines are present at base `d2a5dca` and this slice touches only `topics.yaml:593-601`. It is a separate public-surface cleanup, not a change this slice should smuggle in | Not applicable |

Two Codex should-fixes on wording (`health_record.py:265` and `:353`, and the
receipt's stale field table) were NOT taken in this round, which was scoped to
the namespace must-fix and the six Claude should-fixes. They carry forward.

### Round 6 tallies

| Run | Result |
|---|---|
| Full suite | 1580 passed, 8 skipped, 6 deselected |
| `tests/test_source_capabilities.py` | 32 passed |
| `tests/test_source_health_record.py` | 70 passed |
| `tests/test_source_route_typing.py` | 10 passed |
| `ruff check curator tests` | All checks passed |
| `git diff --check` | clean |
| Hard boundaries, `git diff --stat d2a5dca` over all eight paths | EMPTY |

Five tests were added, all in `tests/test_source_health_record.py`:
`test_a_caller_note_segment_cannot_impersonate_the_module_note_separator` (the
exact Codex pair), `test_a_note_that_carries_its_own_note_segment_is_still_distinct`
(`note:` inside a semicolon-separated note),
`test_the_escape_round_trips_so_it_can_never_merge_two_observations` (a left
inverse for `_escaped`, which is what injectivity means, plus pairwise
distinctness across both fields), `test_a_hard_cut_never_lands_inside_an_escape_sequence`
(pads 98, 99, 100) and `test_a_caller_cannot_forge_the_hard_cut_marker`.

## Known boundaries: what this slice does NOT prove

Stated as the closing section because five review rounds each found one more
thing the evidence did not cover. These are the ones that remain, deliberately.

1. **The capability gate's reach.** It proves that an adapter's `fetch` READS
   the queries object it is handed, or that changing the queries changes the
   complete normalized result. It does NOT prove that an adapter cannot consume
   query semantics some other way: through spec options, through
   transport-level parameters, by counting through a different object, or by
   branching on anything else the harness does not reproduce (the spec id, the
   clock, the fixture bytes, an exact-type check on the queries). Any
   harness-visible difference is a discriminator, so a conditional reader can
   always evade both signals. The honest scope of a green result is "no live
   adapter reads the queries under production-shaped inputs", NOT "no adapter
   can read them". Declarations by adapters of those shapes are enforced by
   review.

2. **The note channel reaches nothing durable, and there is no production
   caller.** `fold_source_health` and `declared_capabilities` are called only
   from tests; the only reference in `curator/` is the `__init__.py` re-export.
   `curator.pipeline.collect` replaces every per-source note with one summary
   string, and the snapshot health row has ten keys, none of them a note. So
   the reason code this slice spent three rounds making safe is not yet
   persisted anywhere, and none of the representation rules have been exercised
   against a real column. The status is DETECTABLE, NOT DETECTED. A future
   implementation may wire it, and
   `test_the_fold_is_not_wired_into_the_production_collection_path_yet` pins
   both wiring routes so that change cannot land silently. This receipt does
   not determine when that work occurs.

3. **The partial-success enum question is escalated, not decided.** The frozen
   `HealthStatus` vocabulary has no value for "delivered items, run was not
   clean". This slice records that state in the reason code only, because
   widening the frozen enum is a contract question for the next freeze
   revision. `curator/contracts` is untouched.

4. **Over-the-bound replay is now refused, not supported.** An equal-moment
   fold onto a record whose reason was truncated raises. If a caller ever needs
   true replay for an over-long reason, that requires the separate
   `reason_truncated` (or a full stored digest) field in the next contract
   revision. Until then the fold prefers a loud, recoverable refusal to a
   comparison it cannot justify.

5. **Not verified at all (grade C):** live activation. Nothing in this slice
   has run against a real route, because nothing calls it. Redirect and
   content-encoding paths of `SafeHttpTransport` are also unexercised by the
   capability harness, which does not claim them.
