# Contract-freeze receipt (SC-41)

Date: 2026-09-02
Branch: `feat/ui-v3-mockups`, on top of `b61539a`.
Scope: settles contract freeze (phase 1). Authorizes no deploy, no merge to `main`, no cloud mutation, no data import.

## Frozen set

| Deliverable | Where | Count |
|---|---|---|
| Contract definitions | `docs/contracts/` | 12 contracts (tenant, authorization, source-plugin, evidence, event, search, candidate, artifact, mirror, output-adapter, publication, receipt) plus README and the personalization reconciliation with its recorded decision |
| Typed definitions (dataclasses, Protocols, Enums; no behavior, no I/O) | `curator/contracts/` | 14 modules |
| Fixtures | `tests/fixtures/contracts/` | 113 (60 valid, 53 invalid; every invalid names the rule it violates) |
| Policy revision 1 | `config/ranking-policy-r1.yaml` | all 7 SC-20 bands with values and rationale; the 4 SC-08A mandatory bands active; component weights; lane quotas as caps (8/6/6/4); protected exploration; cluster-level repetition; all 16 event weights |
| Freeze test | `tests/test_contract_freeze.py` | 265 passed in 0.42s |

Full suite on this exact tree: 1415 passed, 8 skipped, 6 deselected in 3.84s.

## Review record

Two independent adversarial legs (fresh-context reviewer and cross-vendor reviewer) both returned FIX-FIRST on the first freeze draft. Their strongest shared finding: validators trusted supplied values instead of recomputing them, so receipts could self-certify. Reviewer-seeded attacks that passed the draft validator (a settled deletion with empty projections or a false zero-contribution verdict, a ranking receipt naming the wrong primary lane, a settled slate with no bands, an import inventory enabled while unverified, a digest-embedded at-most-once key, an imported browser visit marked strong) are now permanent invalid fixtures that must fail. The merged fix round applied every must-fix and the cheap should-fixes; a second cleanup round implemented the vendor-name scan, removed owner identifiers from the public fixture set, and bound `final_score` to its weighted components.

Deferred by design (recorded, not silent): the account-deletion semantics for the preference row (cascade with mandatory deletion receipt, marked open to owner veto in the reconciliation), and the machine-readable source-rights field (declared, deferred until discover/poll is built).

## SC-41 checklist

| Requirement | State |
|---|---|
| Every frozen contract has a committed definition plus at least one fixture | Yes, 12 of 12 |
| Policy revision 1 with concrete initial values for every SC-20 band and every SC-08A active band | Yes |
| Shipped Supabase personalization schema inspected and explicitly adopted or superseded with a recorded decision | Yes: adopt with additive migration; two conflicts adjudicated and recorded |
| Context-free review of the frozen set until clean | Two legs plus two fix rounds; final direct verification on this tree; the cross-vendor re-review runs as a follow-up check and any finding lands as a follow-up commit |

CONTRACT FREEZE SETTLED. Next per the plan: phase 2 (host and privacy proof) and phase 3 (source adaptation) in parallel with phase 4 (ledger and artifact store).
