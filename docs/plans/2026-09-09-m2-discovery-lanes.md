# M2 implementation plan

Build in the prepared isolated worktree on `feat/m2-discovery-lanes` (base 4741a64744c35f98d9f14c1180470f9ccb766f95). Live Notion M2 is Planned and asks working Updates/newest, Hot/trendy, Interested/fresh, Surprise/out-of-scope, overlap and evidence. JJ authorizes proceeding alongside M1 learning. Do not mark learning complete.

Reuse frozen candidate records and ranking r1 as specification, existing source snapshots, story identity, exact-URL dedupe, interest-score materializer, render/reader and signed-in auth. No new dependencies. Preserve M1 as rollback. No account mutations/login demos; no owner identifiers/interests or matching positions in public artifacts.

Destination is resolved by the approved September1 reading-companion scope: M2 lives in the private signed-in hub. The configured owner receives their own materialized edition through an authenticated owner-only RPC. Other accounts receive no owner edition. Public Pages contains controls only, never personal lane assignments.

## Engine contract

New curator/discovery.py and config/discovery-policy-r2.yaml (separate version, do not edit frozen r1). Pure functions accept original Items, Config categories, fixed aware now, validated policy, explicit profile scores/revision/availability, optional prior observed evidence and edition history. Ownership required when creating frozen private Slate/RankingReceipt; no invented users. Public inputs/profile fixtures use no account data.

Generate evidence before per-topic caps. Partition languages. Canonical dedupe uses existing identity/dedupe; only exact canonical-URL provenance can substantiate independent sources. Reject future/estimated timestamps for time-sensitive evidence. Do not infer trend from native_rank, arbitrary point scales, fuzzy-title links, duplicate routes, or newsletter-only count.

Updates: changed publisher content digest for the SAME exact canonical URL, publisher, and source route against a supplied earlier observation. Compare cleaned publisher title/description, exclude aggregator paraphrases and estimated timestamps; require prior observation time strictly earlier than current and change observation inside configured freshness window. A newly syndicated mention or a later publication time without changed publisher content is not an Update. No prior observation means no verified Updates; record insufficient baseline evidence. Store before/after evidence digests and observation times. Never claim a semantic fact change from text change: reason says publisher text changed since previous observation.
Hot: >=2 distinct independent non-newsletter echo-eligible platforms with publication times inside declared recent window; store count and interval and explicitly describe measured coverage reach per window, not growth acceleration. A later evidence time is required for observed movement. These are coverage signals, not audience popularity.
Interested: positive score from provided settled profile artifact, freshness window. Cold start: public topic-config match if policy explicitly selects fallback, reason clearly says shared topics and no personal profile. Cold-start affinity uses subject topic IDs only. Policy-configured non-subject IDs, initially `trending`, remain available as topic matches and chips but do not grant interest affinity. Do not fabricate a profile revision.
Surprise: outside strong profile matches; gated source quality, freshness, real publisher evidence, importance evidenced by >=2 independent echo-eligible sources in the wider 48-hour Surprise window. Hot uses the narrower 24-hour window; a story meeting both remains Hot by frozen priority, and Surprise must legitimately ship short if no importance-qualified candidate remains, and novelty against supplied history. Missing profile/history means unavailable, not proof of novelty. A cold-start discovery view can explicitly use no prior local edition as a declared baseline, never pretend historical novelty. Without profile, only configured shared-topic complement may support a shared-topic reason; no personal-interest claim. Preserve uncategorized eligible originals to avoid losing Surprise upstream.

Merge candidate lane memberships once, primary uses configured strict priority Updates > Hot > Interested > Surprise. Persist secondary reasons and every score. Only primary consumes quota. Score eight components consistently with frozen weighted-contribution rule; penalties explicit and unknown history disclosed by versioned exception. Final source-share and topic-share limits use the actual selected edition as the denominator. If a final share limit rejects a candidate, deterministic backfill takes the next eligible candidate from that same candidate's primary lane, preserving edition length where the lane has eligible candidates. Record shortfall per lane. Recompute seven edition bands after final selection; active failure means unpublishable, not a claimed balanced edition. Receipt replay reruns all lane generators and final selection from bound current/prior observations, topic matches, profile, history, policy and evaluation clock. It compares the full candidate set, primary/secondary assignments, scores, explanations, ordering, quotas and bands against the stored receipt. Trusted consumers independently reconstruct every expected binding.

Operational thresholds remain versioned validated config: windows, quotas, weights, source/topic caps, freshness/interest thresholds, bands. Validate booleans vs numeric, NaN/inf, negative ranges, missing/unknown keys, lane permutation and size/quota consistency. No arbitrary fallback on malformed policy.

## Private integration

The existing protected personalization job runs the sole Python scorer. It fetches the configured owner's real profile and database-proven history, verifies the complete receipt, and atomically finalizes only PASS editions through a service-only RPC. No hosted Python service or edge scorer is added. Source-only prior snapshots come from validated successful main-workflow artifacts with matching configuration and earlier observation clocks.

Private storage is separate from public publication entries. Browser and agent CLI use the same bounded whole-edition authenticated RPC, which derives the owner from auth.uid(). Save/read eligibility admits public finalized stories, the caller's private edition, or the caller's retained saved/read state. Topic-free Surprise entries keep an empty topic list and offer the existing Add an interest path.

Normal limits are validated policy: edition quotas and scoring in config/discovery-policy-r2.yaml; database storage, response, staleness and retained-history limits in discovery_storage_policy. Existing public feed limits are unchanged. Private materializer outputs are aggregate-only; private exports require an explicit path with owner-only permissions.

The NEWS_CURATOR_DISCOVERY_ENABLED repository variable defaults off. Enabling it requires existing personalization, the database migration, passing edition evidence and the separate release gate. A refused candidate retains the previous settled edition and cannot block the M1 public build.

Lane controls separate from topic chips; new visits Updates; one card per story; secondary reasons visible; each lane empty state says why. Preserve read/save/mark unread, new-tab links, search, older-history and new-edition notice, responsive keyboard controls. Scope does not include M3 knowledge or M4 learning.

## Dependency map

1. Pure engine/evidence/policy implementation parallel with independent tests and reader integration investigation. Depends on this plan review.
2. Reader implementation parallel with engine once output shape fixed. Depends on the agreed private RPC contract.
3. Pipeline/CLI integration depends on engine plus reader output contract.
4. Deterministic tests and independent Claude/Codex reviews parallel after integration. Fix all material issues and rerun reviewers.
5. Real-source fixed snapshot replay, real local HTTP reader full path and screenshots depend on tests. No mock data in demo. Verify all four lanes if actual evidence exists; never fabricate to fill a lane. Nonempty all-four requirement remains open if real snapshot lacks evidence.
6. Commit exact files and push prepared branch/draft PR after reviews. Release/production acceptance separate; do not mark Notion Done from build alone.

Baseline: selected existing rank/pipeline/render/contract tests passed before edits. Full suite and meaningful new negative tests required. Avoid new installations; reuse existing Python venv and installed Playwright. Bound all browser and test processes; inspect disk/runtime availability before running. Local cloud-file eviction may require hydration without replacing tracked content.
