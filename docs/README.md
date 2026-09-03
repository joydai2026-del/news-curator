# docs

This public repository keeps only operational documentation for the code in
this repo. The allowlist of what may live under `docs/` is:

- `docs/README.md` (this file)
- `docs/contracts/` (machine-facing interface contracts)
- `docs/qa-checklist.yaml` (a dormant, dated code-QA evidence snapshot; not a
  current definition of done)
- `docs/ai-done-calibration-translation-bridge.md`
- `docs/plans/` only for clearly marked historical implementation or design
  records; these files do not control current planning
- `docs/evidence/` dated CODE-QA and GATE receipts only: build, test, review,
  and gate receipts for changes in this repository (for example
  `2026-08-30-v3-backend-final-local-qa.md`, `2026-09-01-gate0b-receipt.md`)

Current product planning does not live in this repository. The
[Notion sprint page](https://app.notion.com/p/Fall-2026-AI-Sprint-3c6442f52cf7801db1c2fe2e54d777f2)
controls milestones, dates, order, done tests, explainer gates, priorities, and
decisions.

Phase labels in contracts and receipts are historical implementation labels,
not the milestone roadmap. Notion controls project order and done tests.

Design mockups, research captures, plan reviews, handoffs, and anything
describing the owner's private usage do not belong in this public repository.
Files of those classes published before 2026-09-01 remain in public git history.
