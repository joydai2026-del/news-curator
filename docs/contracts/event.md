# Event contract

Typed definition: `curator/contracts/event.py`
Fixtures: `tests/fixtures/contracts/event/`
Freezes: plan section "Event semantics", records `learning_events` and
`correction_events`. Criteria: SC-11, SC-11A, SC-11B, SC-18.

## Purpose

An append-only ledger of what the reader actually did, with each event's
strength decided by policy rather than by the surface that emitted it. The
contract's real job is to stop weak signals from being promoted by accident:
dwell is not a read, an unread label is not a non-open, and one visit is not a
save.

## Records

### `LearningEvent`

| Field | Type | Constraint |
|---|---|---|
| `event_id` | str | Required. |
| `tenant_id`, `actor_id`, `actor_kind`, `user_id` | inherited from `Ownership` | Required, all four. SUBJECT-BOUND: `user_id` required, non-blank, regardless of writer. See [tenant.md](tenant.md#ownership). |
| `event_type` | `EventType` | Required. Closed vocabulary of 16 members. |
| `occurred_at`, `recorded_at` | datetime | Required. |
| `surface` | str | Required. Which surface emitted it. |
| `idempotency_key` | str | **Required, not optional.** |
| `evidence_class` | `EvidenceClass` | Required. |
| `origin` | `EvidenceOrigin` | Required. |
| `confidence` | `ConfidenceBand` | Required. |
| `policy_revision` | int | Required. |
| `story_id`, `story_cluster_id`, `artifact_id`, `conversation_id` | str or null | As applicable. |
| `session_id` | str | Default empty. Needed for sessionization and repeat controls. |
| `duration_ms` | int or null | Dwell and scroll only. |
| `retracted_by_event_id` | str or null | Set by a correction. |

`idempotency_key` is required rather than optional because SC-18 requires the
human control and the agent API to produce IDENTICAL records. A retried agent
call with no key produces a second row, and the two paths stop matching.

**Derivation rule (decided 2026-09-02, round 6): the identity is
`(tenant_id, user_id, idempotency_key)`, scoped PER USER.** Before this the
identity was `(tenant_id, idempotency_key)` alone, so two different users in
the same tenant reusing the same key TEXT collided: whichever user's event
landed first silently absorbed the second user's retry, returning the FIRST
user's row to the second user and dropping the second user's event with no
way to find or delete it later. Scoping the key by user closes that: **a
client may reuse the same key text across different users safely**, because
identity is not shared across the user boundary. `curator/ledger/memory.py`
keys `InMemoryLedgerStore`'s idempotency index on the same triple, and
`supabase/migrations/202609020001_learning_ledger.sql` carries
`unique (tenant_id, user_id, idempotency_key)`, so the two surfaces cannot
drift. See `ledger-storage.md` for the storage-side detail.

### `EventSemantics`

The frozen table each policy row is validated against. An event type with no
semantics row is rejected rather than defaulted.

| Field | Type | Constraint |
|---|---|---|
| `event_type` | `EventType` | Required. |
| `default_evidence_class` | `EvidenceClass` | Required. |
| `default_confidence` | `ConfidenceBand` | Required. |
| `profile_effect` | str | Required, plain language. |
| `creates_global_source_block` | bool | Default `false`. |
| `can_mark_read` | bool | Default `false`. |
| `promotable_by_corroboration` | bool | Default `false`. |

### `CorrectionEvent`

| Field | Type | Constraint |
|---|---|---|
| `event_id` | str | Required. |
| `tenant_id`, `actor_id`, `actor_kind`, `user_id` | inherited from `Ownership` | Required, all four. SUBJECT-BOUND: `user_id` required, non-blank, regardless of writer. See [tenant.md](tenant.md#ownership). |
| `action` | `CorrectionAction` | Required. `correct`, `retract`, `delete_request`. |
| `target_kind`, `target_id` | str | Required. |
| `reason_code` | str | Required. Stable code. |
| `occurred_at` | datetime | Required. |
| `invalidated_snapshot_ids` | tuple of str | Every snapshot whose watermark covers the target. |

## The frozen event table

Initial weights are in `config/ranking-policy-r1.yaml` under `event_weights`.

| Event | Class | Origin | Confidence | Profile effect |
|---|---|---|---|---|
| More like this | explicit | live | strong | Raise the story's explainable topic, entity, source, and format features. |
| Less like this | explicit | live | strong | Lower MATCHED features only. Never a global source block. |
| Already knew this | explicit | live | strong | Lower novelty and knowledge-gap value. Not a statement that the topic is unwanted. |
| Surprise me | explicit | live | strong | Raise eligible out-of-profile allocation within the quality gates. |
| Save / Save answer | explicit | live | strong | Create a knowledge artifact and raise matched features. |
| Ask AI question | explicit | live | strong | Raise question-topic and knowledge-gap evidence. |
| Ask AI follow-up | explicit | live | strong | Depth is its own feature, weighted lower than the first question so it is not double-counted as breadth. |
| Create report | explicit | live | strong | Create a report artifact linked to conversation and story evidence. |
| Read More | explicit | live | medium | Raise interest in the cluster and long-form intent. |
| Accordion expand | observed | live | medium | Curiosity, deduplicated within a session window. |
| Return to a story | observed | live | medium | Durable relevance, only after accidental-repeat controls. |
| Dwell / Scroll | passive | live | weak | Supporting evidence only. **Cannot mark a story read.** |
| Mailbox unread label state | observed | imported | weak | Exposure and cadence only. Cannot create an open timestamp. |
| Single browser visit | observed | imported | weak | Exposure only. Promotable only through corroboration. |

## Invariants

0. **Enforced on the DATA, not only the reference table.** `EventSemantics`
   freezes the shape of one row; `LearningEvent`'s own `evidence_class`,
   `origin`, and `confidence` are checked against policy revision 1's
   `event_weights` row for that `event_type`, so a weak imported signal cannot
   be recorded as a live explicit strong event. The one legal departure is the
   event's own recorded corroboration promotion (`promotable_to`); an
   arbitrary confidence bump is rejected. A fixture asserts both the rejection
   and the legitimate promoted case.
1. **A passive or weak event may never set `can_mark_read`.** Dwell alone never
   writes a completed-read event. A fixture asserts the rejection.
2. **Less like this never creates a global source block.** It lowers the matched
   features and nothing else.
3. Imported mailbox state cannot create an `opened_at` or a historical open. A
   schema carrying one is rejected outright.
4. Imported events stay DISABLED until their inventory receipt is complete. In
   policy revision 1 both imported event types ship `enabled: false`. Absence of
   a receipt is a disable, not a default-on.
5. Corroboration is fail-closed. A single browser visit becomes medium only
   through an independent save, question, or explicit feedback, or through the
   configured minimum intentional returns across sessionized visits. If either
   policy field is absent, or timestamps cannot support sessionization, repeated
   visits stay weak.
6. Human and agent paths create identical validated records for feedback, save,
   question, report, and mirror actions.
7. A correction appends. The original event row is never edited or deleted, and
   the effective-event projection excludes retracted contributions.

## Freeze notes

- **2026-09-02, ownership.** `LearningEvent` and `CorrectionEvent` inherit the
  four `Ownership` fields. The visible change is `user_id`: a human or agent
  event must now name the human it belongs to, so an event can no longer name
  an actor and no person. A permanent seeded fixture
  (`invalid-learning-event-human-actor-without-user.json`) proves it is
  rejected.

- **2026-09-02, subject attribution.** Both are SUBJECT-BOUND: `user_id` is
  required non-blank whatever the `actor_kind`, so a system-written event about
  a person cannot name nobody. Blank (`""` or whitespace) is rejected everywhere
  the shape is checked, not only null.

- `EventType` has 16 members: the plan's table rows, with Ask AI question and
  follow-up separated (the plan says follow-up depth is a distinct feature) and
  dwell separated from scroll (they have different duration semantics and can
  fire independently).
- `duration_ms` is nullable and meaningful only for passive events. It is
  deliberately NOT on the explicit events, so nobody can weight a click by how
  long it took.
- Session boundaries live in policy (`browser_session_gap_minutes`), not in this
  contract. The contract only requires that `session_id` exists to sessionize
  against.
- Grade: C. No event ledger exists in the repository today; the plan itself
  grades product-captured events as zero rows.
