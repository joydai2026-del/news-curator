# Gate 0b receipt: review and commit of the v3 backend tree

Date: 2026-09-01
Branch: `feat/ui-v3-mockups`, base `3adefca`.
Scope: this receipt settles Gate 0b, the review-and-commit gate for the previously uncommitted v3 backend working tree. It authorizes nothing beyond the commit itself: no deploy, no merge to `main`, no cloud mutation, no data import.

## Tree inventory and dispositions

The full unscoped `git status --short` before this gate held 21 modified and 61 untracked paths (82 total).

| Disposition | Count | What |
|---|---|---|
| Committed | 64 | All modified code, config, workflow, and test paths; all untracked backend code (`curator/health.py`, `curator/localization.py`, `curator/personalization/`, `curator/source_snapshot.py`, `curator/sources/`, `curator/translation/`), scripts, `static/auth/`, `supabase/`, `evals/`, all new tests and fixtures, and operational docs (`docs/contracts/`, `docs/qa-checklist.yaml`, `docs/ai-done-calibration-translation-bridge.md`, dated QA receipts under `docs/evidence/`). |
| Relocated to the private workspace before commit | 17 | Product planning, design mockups, research captures, handoff, and planning-review records. This repository is public; per the owner's 2026-09-01 ruling those documents never enter public git history. See `docs/README.md`. |
| Added by this gate | 2 | `docs/README.md` (doc-placement pointer) and this receipt. |
| Discarded | 0 | Nothing was deleted. |

## Review panel

Two independent adversarial reviews of the exact tree, different model vendors, run in parallel and reconciled:

| Leg | Verdict | Must-fixes | Outcome |
|---|---|---|---|
| Fresh-context reviewer A | FIX-FIRST | 3, all document-privacy items (private identifiers in planning docs headed for a public repo) | All 3 resolved by relocating those documents out of the repository before commit; post-move scan of `docs/` for the flagged strings returns clean. |
| Cross-vendor reviewer B | SAFE-TO-COMMIT | 0 | Focus areas all PASS: secrets, workflow security, newsletter privacy boundary, commit hygiene, new-code sweep. |

Both legs confirm: no secret or credential value anywhere in the committed set, workflows are SHA-pinned with main-only environment-gated secret jobs, and the newsletter privacy boundary is strengthened rather than weakened by this tree.

## Deterministic checks on this exact tree

| Check | Result |
|---|---|
| `python -m pytest -p no:cacheprovider -q -m "not allow_socket"` (the exact CI gate) | 1150 passed, 8 skipped, 6 deselected, 0 failed |
| `python -m compileall -q curator tests scripts` | exit 0 |
| `ruff check curator scripts` | all checks passed |
| `git diff --check` | clean |
| Large-file scan over untracked paths | no file over 5 MB in the committed set |

Note: an earlier quoted result of "1150 passed, 14 skipped" used `-m "not network"`, which matches no registered marker and therefore ran the full suite. The CI-gate command above is the authoritative receipt.

## Should-fix-later ledger (tracked, none block the commit)

| ID | Where | Issue |
|---|---|---|
| G1 | `curator/personalization/auth.py:170`, `static/auth/client.js:173` | Refreshed access tokens are not checked for a `sub` claim matching the saved user ID. Path not activated. Add the check plus forged-subject tests before personalization activation. |
| G2 | `.github/workflows/curate.yml` persist-state job | Omits `persist-credentials: false`, exposing the `contents: write` token to later steps in that job. Scope authentication to the push step. |
| G3 | `curator/sources/transport.py:220` | Outer sanitizer converts only expected transport errors; add a final catch-all conversion at the boundary. |
| G4 | `curator/sources/transport.py:170` | Per-host lease table never prunes idle hosts; unbounded growth in a long-lived process. |
| G5 | `curator/sources/transport.py:664` | URL gate rejects whitespace and DEL but not other C0 controls; currently unexploitable, misleading as written. |
| G6 | `.github/workflows/curate.yml` | `cp static/privacy.html` is unconditional; build hard-fails if the file is ever removed. |
| G7 | `.github/workflows/curate.yml` | `actions/configure-pages` was removed from the build job; expected to work with Pages source set to GitHub Actions, unexercised. Watch the first `main` run. |
| G8 | `curator/health.py:70-77` | Markdown table interpolation without `\|` escaping; cosmetic, trusted input. |

## Gate state

GATE 0B SETTLED at the commit this receipt ships in. Next gates per the plan: 0a (private-workspace restore and source inventory) and 0c (collector compatibility matrix) in parallel, then contract freeze. Merging to `main` remains blocked until the public-projection guard decision is settled, because a `main` push on these paths triggers the public hourly deploy.
