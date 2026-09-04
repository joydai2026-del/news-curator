# News Curator v3 backend final local QA

Date: 2026-08-30  
Branch: `feat/ui-v3-mockups`  
Base: `3adefcae37794e40a4acc053d1e424791711678a`

## Result

The backend implementation is locally clean and the design remains untouched. The release ladder is still PARTIAL because local database, cloud, AI calibration, protected workflow, and deployment gates are open.

## Proof

- Deterministic suite: `1110 passed, 8 skipped, 6 deselected` in 3.30s.
- Ruff, compileall, JavaScript syntax, browser personalization contract, QA YAML parse, `git diff --check`, and the exact design/render diff against the base passed.
- Source snapshot: 3,741 items collected; validation passed with digest prefix `3b87917a7d91`.
- Offline build from that snapshot: 1,112 EN and 697 ZH items within 48 hours; 1,022 EN and 635 ZH after dedupe; 270 rows across 10 topics; localized JSON projections written.
- Fresh targeted checks: Canary and Hacker News RSS 2/2 fresh; parser salvage, transport fallback, and ultimate backend fallback reviews were LOCAL-CLEAN. The last fallback review was same-family only.

## Live non-Reddit source probe

At `2026-08-30T05:10:20.398464+00:00`, the production adapters attempted all 75 configured sources: 58 fresh, 15 stale, 1 empty, and 17 degraded. There were zero unavailable sources. Exit 1 is expected because stale and empty rows are health failures. Stale IDs were `cnbeta`, `ieee-comp`, `physorg-q`, `googleai`, `mittr-ai`, `vb-ai`, `deepmind`, `blockworks`, `dlnews`, `neimag`, `powermag`, `spacenews`, `dw-zh`, `google-zaobao`, and `cnbc`; empty was `udn-zh`; link-resolution degradation was `google-36kr`.

## Still open

- Local Supabase/Postgres matrix, Google provider call, cloud links/deployment, AI human calibration, and protected workflow run are not proven.
- Workflow source blockers remain in NCV3-15.
- External Codex review was unavailable without authorization to transmit private uncommitted source.
- No commit, push, deploy, or publish was performed. Reddit is paused. Design work was not started.
