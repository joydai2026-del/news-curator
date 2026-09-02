# Artifact contract

Typed definition: `curator/contracts/artifact.py`
Fixtures: `tests/fixtures/contracts/artifact/`
Freezes: plan section "Core records" (`knowledge_artifacts`, `artifact_versions`,
`artifact_relations`) and the conversation-to-artifact graph. Criteria: SC-26.

## Purpose

Questions, answers, reports, insights, and saves become durable versioned
records in the canonical private store. A mirror is a copy on someone else's
system; this is the original.

## Records

### `KnowledgeArtifact`

| Field | Type | Constraint |
|---|---|---|
| `artifact_id` | str | Required. |
| `tenant_id`, `actor_id` | str | Required. |
| `artifact_type` | `ArtifactType` | Required. `question`, `answer`, `report`, `insight`, `save`. |
| `status` | `ArtifactStatus` | Required. `draft`, `settled`, `redacted`, `retracted`. |
| `publication_class` | `PublicationClass` | Required. Private by default. |
| `created_at` | datetime | Required. |
| `current_version` | int | Required. Points at the newest `ArtifactVersion`. |
| `title` | str | Default empty. |
| `conversation_id`, `story_id` | str or null | Link back to what produced it. |

### `ArtifactVersion`

| Field | Type | Constraint |
|---|---|---|
| `artifact_id` | str | Required. |
| `version` | int | Required, monotonic from 1. |
| `parent_version` | int or null | Null for version 1. Otherwise **strictly less than** `version`. |
| `checksum` | str | Required. The value a mirror compares against. |
| `content_reference` | str | Required. Restricted storage reference. |
| `actor_id` | str | Required. |
| `settled_at` | datetime | Required. |
| `citations` | tuple of str | Default empty. Story and document ids the content cites. |
| `redacted_by_event_id` | str or null | Set by a correction. The version row stays queryable. |

### `ArtifactRelation`

| Field | Type | Constraint |
|---|---|---|
| `relation_id`, `tenant_id`, `conversation_id`, `artifact_id` | str | Required. |
| `relation_type` | str | Required. |
| `requested_type` | str | Default empty. |
| `depth` | int | Default 0. How far into a follow-up chain the request came from. |

## Invariants

1. Versions are immutable. A revision appends a new version; it never rewrites
   an existing one.
2. `parent_version` is strictly less than `version`. A version that is its own
   parent breaks the chain that mirrors and deletions both walk.
3. `checksum` identifies content exactly. A mirror settles only when a target
   readback matches this value.
4. A redaction or retraction sets a field and leaves the row queryable. The
   immutable audit chain survives deletion of the content it describes.
5. Both directions of the conversation-to-artifact relation are retained, so an
   answer can be traced to its question and a conversation can enumerate what it
   produced.
6. An artifact is private unless an explicit public projection promotes it.
   Verbatim quoted commentary extracted from a newsletter is NOT public-eligible
   without separate licensing, whatever the artifact's own class says.
7. Ask AI answers cite available story evidence or state explicitly that
   evidence is insufficient. An answer that does neither is not settleable.

## Freeze notes

- `ArtifactType` has five members and deliberately excludes anything that is not
  reader-generated knowledge. A raw import is not an artifact, and a fixture
  asserts that an output adapter cannot declare it eligible.
- `relation_type` is a free string while `artifact_type` is closed. Relations
  describe the graph and will grow (requested_report, answers, cites, supersedes);
  artifact classes carry eligibility and mirror rules and must not grow silently.
- The plan borrows a verified external pattern (a conversation creating a
  requested report, infographic, or insight with both sides retaining the
  relation) but the schema here is provider-neutral and depends on nothing
  external.
- Grade: C. No artifact store exists in the repository today.
