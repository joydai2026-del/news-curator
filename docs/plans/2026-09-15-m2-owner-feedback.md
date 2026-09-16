# M2 owner feedback repair

Status: implementation in progress. Owner acceptance is reopened by the reported UX failures. This supplements the existing M2 plan; it does not mark quality qualification complete.

## User-visible outcome

1. English is the default display language. A persistent English/中文 switch controls reader labels and story titles/summaries across All, categories, search, pagination, Saved and fallback. Original stories retain their identities, original languages and source links. Translate once and cache; never mix untranslated prose into the chosen-language feed. A translation failure is visible in the selected language, never passed off as a translation.
2. Save immediately shows Saving…, then Saved after durable confirmation. Failed saves restore the previous state with an actionable error. Saved state persists across reload and is accessible as a toggle.
3. China News is a distinct topic. Working assumption, pending the optional clarification: China-focused reporting from both Chinese and international sources. Language is not a proxy for geography. Existing and newly verified public feeds populate it; later user-provided sources remain easy to add through configuration.
4. Load more immediately acknowledges the click, retains current cards and scroll position, prevents duplicate requests and ends in new content, a truthful fallback, or a retryable error. Improve actual request latency where evidence supports it, without extra model calls caused by speculative prefetch. Use restrained glass styling and short transitions with a reduced-motion path and readable contrast.
5. While the owner interacts, show whether the latest permitted activity was recorded and used by the latest model result. Backend monitoring reports real request outcomes, latency and safe failure categories, separately from usefulness. Existing positive/negative feedback remains input to the engine. No claim that a click or changed order proves improved relevance.

## Reuse and implementation choices

Reuse the existing translation provider interface/cache, locale preference and CAS API, canonical story identity, source registry, reader components, owner history, ranking receipts, cost ledger, Supabase and GitHub Actions. No new package, analytics vendor, social integration, server subscription, or training framework is planned. The inactive Google translation lane currently lacks its environment/configuration; evaluate the already authorized OpenAI connection behind the existing adapter instead of adding Google billing setup. All paid calls retain durable bounded accounting. Translate outside the interactive pagination path.

## Parallel execution map

| Work | Depends on | Parallel with | Owner |
|---|---|---|---|
| Save, loading and visual feedback fixes | Reproduce existing UI gaps | Translation design, monitoring | Sol |
| Translation/cache and locale contract | Existing adapter/budget inspection and plan review | UI, monitoring, China sources | Terra with root adjudication |
| China topic/source configuration | Verify public feeds and China relevance; scope assumption above | UI and monitoring | Root integration / delegated configuration |
| Operational health | Existing telemetry plan plus adversarial corrections | UI and translation | Sol |
| Language switch and learning activity UI integration | Locale/monitor contracts and UI fix | Backend verification | Root integration |
| Cross-model review, regression and live QA | Integrated implementation | Source probes and bounded real translation evals | Independent reviewers |
| Production release and owner handoff | Tests, independent review, rollback and live checks | Operational observation | Root |

## Binary acceptance checks

- Selected-language title/summary and labels remain consistent across both languages, every feed surface, Save/reload, pending translations, failures and signed-out fallback. Proper names/source attribution are preserved; no original foreign-language prose silently replaces a failed translation.
- A source-language story obtains a validated translation in the other language and appears under the same story ID and source URL. Updated source content cannot reuse a stale digest. Search can match original or translated content while rendering only the selected language.
- Locale changes invalidate incompatible in-flight responses/cursors using the reader request epoch. Each frozen ranking binds its explicit display language; changing display language starts a fresh request. The saved preference supplies the initial locale, while an explicit CLI locale remains available. No owner state is accepted from another session. UI and CLI use the same contract. Original-content language remains distinct from display language for coverage metrics.
- Save and Load more show visible busy state in the first event-loop update; Saved appears only on confirmed persistence; network failure/conflict and repeated-click tests pass.
- Frozen next pages make no extra model call. At a candidate boundary, any new ranking is explicit and bounded. Measure real warm/cold request and render time, not only animations.
- China News has real dated, relevant items from multiple independent source families. Disabled/unavailable feeds remain visible in source health. No promise of covering all internet news.
- Learning status says latest activity used only for a successful model result matching current history revision, generation and consent with no pending writes. It never claims all older events were included.
- Technical request counts include mapped failures that occur before frozen-ranking persistence. Telemetry uses fixed dimensions and no owner IDs, queries, prompts, titles or history. Recording failure cannot break or materially delay the reader; platform termination remains an explicit observability limit.
- Operational health treats idle/low-volume/missing evidence honestly. Seven-day quality insufficiency does not masquerade as a backend outage. Thresholds and retention are validated configuration.
- Static/unit/integration/PostgreSQL suites, independent review and the actual deployed human and agent paths pass. Phone widths, keyboard feedback and reduced motion are checked visually.

## Measurement boundary

Functional health proves action capture, latest-input use, model/fallback outcome and working controls. Relevance and coverage still use the existing real-case, human-judged M2 criteria. No new sample floor or automated quality pass is invented here. No feature is considered accepted just because an aggregate health job is green.

## Failure and projection boundaries

Feed/category/search candidates without a current matching translation are omitted BEFORE page assembly and cursor selection. Short pages are valid; no foreign original fills a gap. Saved preserves saved membership, with a selected-language translation-unavailable title/summary if no current translation exists. Source names and proper-name attribution are preserved.

The signed-out/static reader selects only `data/news-en.json` or `data/news-zh.json` for the chosen locale. It validates the projection language before rendering. Missing, malformed or unavailable projections produce a localized unavailable/retry state. The mixed-language HTML edition is never the fallback for a selected-language view; the story container stays hidden until the persisted locale has been read and its validated projection is ready, preventing an English flash for a Chinese reader. A no-script notice explains that JavaScript is required for the language-aware reader.

Operational telemetry's authoritative plan is `docs/plans/2026-09-15-m2-operational-health.md`. It stores fixed request-outcome/latency counts only. Provider attempts and cost remain in the existing frozen receipts and money ledgers; the older scratch monitoring proposal is superseded on these points.

The requested external Claude plan review was blocked by automatic approval review before transmission. Independent Codex review remains available. No external review PASS is claimed.
