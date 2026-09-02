# Learning ledger storage (phase 4, first slice)

## What the migration implements

`supabase/migrations/202609020001_learning_ledger.sql` creates the storage
for the frozen event, evidence, artifact, and receipt contracts
(`curator/contracts/{event,evidence,artifact,receipt,mirror,tenant}.py`):
`learning_events`, `correction_events`, `evidence_items`, `raw_imports`,
`knowledge_artifacts`, `artifact_versions`, `artifact_relations`,
`deletion_receipts`, `mirror_receipts`, plus a minimal `tenant_members` table
that every row-level security policy is keyed on. Columns match the frozen
dataclass field names, required fields are `NOT NULL`, and every closed enum
(`ActorKind`, `EventType`, `EvidenceClass`, `EvidenceOrigin`, `ConfidenceBand`,
`CorrectionAction`, `RetentionState`, `ArtifactType`, `ArtifactStatus`,
`PublicationClass`, `ReceiptState`, `MirrorState`) is a `CHECK` constraint,
not a free-text column. Every table has RLS enabled and forced, default
grants revoked from `public` and `anon`, and owner-scoped `SELECT`/`INSERT`
policies (plus `UPDATE` where the row's own state legitimately changes)
keyed on `tenant_members`. Nothing in this migration grants `DELETE` to
`authenticated` on any table: the ledger's own rows are never physically
removed, only superseded or marked by a later row.

## Append-only guarantee and how retraction works

`learning_events`, `correction_events`, `evidence_items`, and
`artifact_versions` are the actual history. Each carries a trigger
(`reject_ledger_mutation`) that raises on `UPDATE` or `DELETE`, so even a
service-role connection cannot edit history in place, plus an explicit
`REVOKE UPDATE, DELETE ... FROM authenticated` as a second, independent
barrier. A mistake or a user's "I didn't mean that" is never fixed by editing
the original row. It is fixed by appending a `CorrectionEvent` (or, in the
evidence graph, setting a later row's `retracted_by_event_id` to point back)
that names the row it corrects. `LedgerStore.effective_events(tenant_id,
as_of)` is the read-side of this: it returns every event recorded at or
before the watermark, minus any event whose id is the `target_id` of a
`retract` correction that occurred at or before the same watermark. The
retracted row itself is still readable by id; it is only excluded from this
projection. `DeletionReceipt` settlement is guarded the same way: a receipt
cannot claim `state = settled` while any of its listed projections is
unresolved, enforced both in SQL (`deletion_receipt_may_settle`) and in the
in-memory store, mirroring the invariant in
`tests/test_contract_freeze.py::_invariant_deletion_receipt`.

## What is NOT yet built

- Profile snapshot builder (reading `evidence_items` and `learning_events`
  into a `ProfileSnapshot`).
- Rebuild / replay pipeline that recomputes a snapshot from the effective
  event set after a correction invalidates one.
- Search index over artifacts or evidence.
- Mirror write path (the `mirror_receipts` table exists; nothing writes to
  an external target yet).
- A Postgres-backed `LedgerStore` implementation; only `InMemoryLedgerStore`
  exists today, for tests and local dry runs.

## Test command

```
.venv/bin/python -m pytest tests/test_learning_ledger.py -q
.venv/bin/python -m pytest -m "not allow_socket" -q
ruff check curator/ledger tests/test_learning_ledger.py
```
