# Private discovery (M2)

M2 adds four private lanes to the existing signed-in reader. `Updates > Hot > Interested > Surprise` determines each story's one primary lane. Secondary reasons remain visible; topic chips independently filter the selected lane. Public stories and M1 reading actions remain available.

## Build and inspect without account access

```sh
python scripts/discovery_cli.py build --root . \
  --snapshot /path/to/current-source-snapshot.json \
  --previous-snapshot /path/to/previous-source-snapshot.json \
  --output /path/to/new-private-receipt.json
python scripts/discovery_cli.py verify --receipt /path/to/new-private-receipt.json
```

These are local historical source replays. Missing history is unknown, not empty. To declare an explicitly empty history for this local privacy scope, add `--first-local-edition`; this does not claim an account's or owner's history. To supply local history, add `--history /path/to/history.json`, a bounded JSON list whose rows contain `story_id`, `source_id`, and `shown_at`. Both controls are local inputs only and are mutually exclusive. The CLI cannot settle an owner edition and its verification proves internal consistency, not source authenticity. Output is created with mode600 and never overwrites an existing file. The committed fixture is captured public news; controlled negative test mutations must not become a news demonstration.

## Private serving path

The existing protected personalization job runs `scripts/materialize_discovery.py` with its configured owner and Supabase service credentials. It fetches the actual owner profile and complete retained edition history, then selects a source-only prior capture from a bounded, complete window of successful main-workflow runs. The first edition uses the oldest valid capture in that window; later editions use the newest valid capture at or before the latest stored edition. Selection shares the scoring policy and evaluation clock, and never chooses a baseline by edition verdict. It rebuilds the receipt, verifies all input bindings and finalizes only PASS. It exports no private artifact to Actions or Pages. Unknown or failed inputs preserve the previous private edition.

M2 interprets a saved configured category name or id as interest in the category's full existing matcher, including native source membership, language terms, aliases, and exclusions. An ordinary saved phrase that is not a category remains a literal headline interest. A category name and id saved together count as one matching concept, while the stored interest count remains the number the user actually saved. This `category-v1` interpretation and its canonical category name/id metadata are bound into the ranking digest and receipt identity. M1 retains its existing literal ranking and digest behavior.

Retries use a stable identity derived from owner, current/prior captures, profile revision and full profile fingerprint, policy, language, ranking/dedup configuration, git revision and actual engine fingerprint. A service-only identity lookup reconciles an uncertain commit. Automatic retries also look up the existing input tuple before baseline retrieval, so an expired artifact or changed latest-edition anchor cannot create a duplicate. Ambiguous stored matches fail closed. SQL checks the same identity and atomically stores the edition with its source facts.

The authenticated `discovery_edition(p_edition_id)` RPC derives its owner from `auth.uid()`. It returns one bounded edition. Both the browser and `scripts/read_discovery.py` use this contract. No authenticated caller chooses another owner. The default agent read prints aggregate counts; `--output` explicitly exports private content with mode600.

```sh
python scripts/read_discovery.py read --lane updates
python scripts/read_discovery.py read --lane surprise --output /path/to/private-edition.json
```

This CLI uses the existing AgentAuth and macOS Keychain session. Topic-free Surprise stories support Save/read and offer the existing Add an interest flow.

## Configuration and release

- Production ranking and lanes: `config/discovery-policy-r3.yaml`. R1 and strict R2 remain unchanged. R3 explicitly permits below-target Hot and Surprise shares when the corresponding primary lane has a recorded shortage; the receipt says `QUALIFIED_SHORTFALL` and retains its achieved share and original target. Upper caps, all other bands, and per-story evidence rules remain strict. The reader shows the selected stories with shortage notices. The offline CLI retains strict R2 by default; pass `--policy config/discovery-policy-r3.yaml` to replay R3.
- Cold-start affinity contract: configured non-subject topic IDs, initially `trending`, do not grant interest affinity. Their topic matches remain available for topic chips and evidence, so this rule does not remove or rewrite topic labels.
- Source/topic share limits use the actual selected edition as their denominator. Deterministic same-lane backfill preserves quality constraints and records honest shortages; eligible same-lane replacements repair minimum source/topic variety.
- Database storage: `discovery_storage_policy`. Normal quotas and limits remain configurable within the common 100-entry, 1 MiB read safety envelope. The larger private evidence payload has its own limit.
- Deployment migrations: `supabase/migrations/202609090001_discovery_lanes.sql`, then `202609100001_discovery_retry_identity.sql` and `202609100002_discovery_qualified_shortfalls.sql`, after the existing M1 migrations.
- Feature switch: repository variable `NEWS_CURATOR_DISCOVERY_ENABLED=true`, together with existing `NEWS_CURATOR_PERSONALIZATION_ENABLED=true`. Defaults off. Browser rendering can be tested locally with `--discovery-enabled`.
- Owner routing: the protected M2 step uses `NEWS_CURATOR_DISCOVERY_OWNER_USER_ID` when configured and otherwise falls back to the existing M1 owner. The M1 ranking step continues to use only its original owner setting.
- Baseline recovery controls: the materializer CLI accepts `--baseline-attempts` and `--baseline-timeout`; incomplete or inconsistent run listings fail closed. Failed editions report only band names and verdicts, with an explicit workflow warning.
- Rollback: disable the discovery feature switch and rebuild the existing public M1 page. Keep private tables and receipts for recovery. Do not delete records as a rollback shortcut.

Release is separate from build completion. Require reviewed code, PostgreSQL17.11 CI, a genuine owner-bound passing edition and live owner read/save verification before acceptance. Historical source-only replays establish engine behavior, not current owner settlement or release acceptance. Production evidence is recorded in `docs/evidence/2026-09-10-m2-production-release.md`.

Baseline retrieval uses GitHub's documented [workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow) and [artifact download command](https://cli.github.com/manual/gh_run_download).
