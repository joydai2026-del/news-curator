# Translation Bridge: AI-Done Calibration and Activation Gate

Status: **RED. Backend design is planned, not AI-calibrated or live-ready.**

This document prevents deterministic implementation proof from being described as translation quality proof. Evidence is graded as follows: A is production evidence, B is source evidence, and C is unverified. Nothing below may be promoted without a matching receipt.

## Required state sequence

| State | Meaning | Current state |
|---|---|---|
| Implemented | Code and migration exist. | RED |
| Locally proven | Deterministic suites and local rehearsals have dated receipts. | RED |
| Cloud-linked | Real Supabase and Google configuration pass their live contracts. | RED |
| AI-calibrated | Labeled evaluation, tripwires, rollback rehearsal, and weekly sampling pass. | RED |
| Live-proven | Reviewed scheduled production path and public page succeed. | RED |

## Deterministic floor

All must be green before any provider evaluation. These are local-proof requirements, not a quality percentage.

| Gate | Planned evidence | Pass condition | Grade until receipt |
|---|---|---|---|
| Input boundary | `uv run pytest -q tests/test_translation_contracts.py tests/test_translation_privacy.py` | Only immutable title and optional description cross the provider boundary. Newsletter and preference data are rejected before cache, budget, provider, and logs. | C |
| Adapter boundary | `uv run pytest -q tests/test_google_translation_adapter.py tests/test_secret_leaks.py` | Bounded id-bearing batch, validated response, origin-bound bearer token, and no secret/source-text leakage. | C |
| Cost state machine | `supabase db reset && uv run pytest -q tests/test_translation_store_local_db.py` | Atomic reservation, committed `sent` before write, conservative `charge_unknown`, and no automatic retry after ambiguity. | C |
| Projection integrity | `uv run pytest -q tests/test_localized_pipeline.py tests/test_translation_fail_soft.py` | Original identity, rank, category evidence, URL, attribution, dates, numbers, and native-language precedence remain intact. Failures serve originals. | C |
| Kill switch | `uv run pytest -q tests/test_translation_rollback.py tests/test_translation_state_machine.py` | `translation.enabled: false` completes the build with zero provider calls. | C |
| Workflow isolation | `uv run pytest -q tests/test_workflow_contract.py` | Translation secret job is protected-main-only, environment-bound, SHA-pinned, has no deploy/write permissions, and emits only a sanitized artifact. | C |

Current deterministic blockers: the translation package, migrations, fake-provider tests, local Supabase harness, and protected workflow are not yet implemented or freshly receipted.

## 20-case coverage smoke

Use 20 real current-news title and optional-description pairs captured into the versioned local evaluation fixture. They must be sanitized of any credential or user data, include both directions, and be human-labeled before use as a quality signal.

| Requirement | Minimum |
|---|---:|
| EN to ZH cases | 10 |
| ZH to EN cases | 10 |
| Politics cases | 6 total |
| Technology cases | 6 total |
| Business cases | 6 total |
| Remaining cases | 2 from any of the three sections, preserving direction balance |
| Hard terminology/name violations | 0 |

The smoke is diagnostic only. It may expose a bad direction or regression. It must not be used to report a quality percentage, activate translation, or call the system AI-done.

## 100-case labeled activation gate

Before reporting a percentage or enabling translation for routine production use, evaluate at least 100 real current-news cases with human labels.

Each record must retain: capture date, source locale, target locale, topic slice, title/description field selection, approved input digest, provider/model version, glossary-policy version, candidate-policy version, output, labeler id or pseudonym, and adjudication result. Do not store account preferences, newsletter content, credentials, or article bodies.

### Labels

1. Target language is correct.
2. Meaning is adequate.
3. Proper names and required terminology are preserved.
4. Headline is clear enough to display.
5. No safety or political-name corruption appears.

An item is acceptable only if all five labels pass. Any critical terminology, name, safety, or political-name violation is unacceptable regardless of the aggregate rate.

### Required reporting

Report the following, always with raw numerator and denominator:

| Measure | Pass rule |
|---|---|
| Overall acceptability | At least 90% acceptable across at least 100 labeled cases. |
| Confidence | Report the two-sided 95% Wilson interval for the acceptable proportion. |
| Critical violations | Exactly zero in the labeled set. |
| Worst slice | Report the smallest slice rate and its N. Do not hide small N. |
| Required slices | EN to ZH, ZH to EN, politics, technology, business, title-only, and title-plus-description when present. |

For `x` acceptable outcomes from `n` cases, use a two-sided 95% Wilson interval with `z = 1.96`:

```text
center = (p + z²/(2n)) / (1 + z²/n)
margin = z * sqrt((p(1-p) + z²/(4n)) / n) / (1 + z²/n)
p = x/n
interval = [center - margin, center + margin]
```

No 100-case result exists today. Current evaluation result: **RED, C unverified.**

## Privacy and cost tripwires

| Tripwire | Required action | Current state |
|---|---|---|
| Any newsletter, preference, credential, or unauthorized text reaches a translation boundary | Disable translation immediately. Preserve originals. Open an incident, add a regression case, and do not re-enable until deterministic privacy tests pass. | RED, no implementation receipt |
| Credential sentinel appears in stdout, stderr, health, Actions summary, cache, artifact, or render | Revoke or rotate affected credential, disable translation, remove exposure, and rerun leak tests before re-enable. | RED, no implementation receipt |
| Run, day, or month character budget would be exceeded | Block new provider calls. Serve cache or originals. Record budget class only. | RED, no implementation receipt |
| `sent` request has timeout, broken response, process termination, or another ambiguous charge outcome | Mark `charge_unknown`, keep full reservation counted, and block automatic retry. | RED, no implementation receipt |
| Provider, database, artifact, or validation failure | Fail soft to originals. Surface a safe warning without raw source text or secrets. | RED, no implementation receipt |
| Any critical quality violation | Disable the affected language direction, add case to regression set, and require a clean evaluation before re-enable. | RED, no implementation receipt |

Configured initial limits from the approved plan are 2,000 characters per run, 15,000 per day, and 450,000 per month. They are policy defaults, not current usage, and must be runtime configuration rather than source constants.

## Named rollback actions

| Trigger | Owner/action | Evidence needed to re-enable |
|---|---|---|
| Quality or safety defect | On-call operator sets `translation.enabled: false` and disables the affected direction if supported. | Regression case, deterministic suite, labeled review of remediation. |
| Cost threshold | Translation bridge blocks new calls and serves cache/originals. | Budget configuration and controlled reservation test. |
| Ambiguous provider charge | Translation store marks `charge_unknown`; no automatic retry. | Manual reconciliation receipt or explicitly approved retry policy. |
| Privacy or secret leak | Operator disables translation and rotates/revokes the affected credential. | Sentinel tests, leak scan, and protected workflow review. |
| Cloud service outage | Bridge fails soft to originals; operator records safe failure category. | Controlled recovery run with originals preserved. |

The exact alert destination is **BLOCKED** until JJ selects an existing channel. The implementation may expose a generic webhook setting, but must not assume a channel or claim an alarm is live.

## Weekly post-activation sampling

After activation, sample 20 newly translated items each week plus every user- or operator-flagged output. Maintain direction and topic coverage when the week contains sufficient eligible items. Human-label with the five criteria above, record provider and policy versions, and add every failed sample to the permanent regression fixture before any related re-enable.

Track: provider error rate, cache-hit rate, characters per run/day/month, translation coverage, critical violations, disable events, and sampled acceptability by required slice. Weekly sampling is not a substitute for the 100-case activation gate.

## Current RED blockers and evidence ledger

| Requirement | Required evidence | Current grade/state |
|---|---|---|
| Deterministic floor | Dated local command receipts under `docs/evidence/` | C, RED |
| 20-case smoke | Sanitized real-current-news fixture and labels | C, RED |
| 100-case gate | Labeled provider output, Wilson interval, slice table, zero critical violations | C, RED |
| Supabase cloud contract | Approved project configuration and live RLS receipt | C, BLOCKED |
| Google Cloud provider contract | Approved credential, billing, protected-job receipt, and controlled request | C, BLOCKED |
| Alert destination | JJ-selected existing notification channel and verified delivery | C, BLOCKED |
| Rollback rehearsal | Main-only protected workflow or locally equivalent tested receipt | C, RED |
| Weekly sampling | At least one post-activation sample receipt | C, RED |
| Live public path | Reviewed scheduled production run and public anonymous page check | C, BLOCKED |

Source evidence supporting the plan's proposed Google and Supabase approach is B-grade only. It does not prove this repository's configuration, behavior, quality, cost, or production readiness.
