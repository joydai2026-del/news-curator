begin;

-- Learning ledger: append-only behavioral history plus the durable records
-- that hang off it (evidence, artifacts, receipts). Modeled on the frozen
-- dataclasses in curator/contracts/{event,evidence,artifact,receipt,mirror,
-- tenant}.py. See docs/contracts/ledger-storage.md for the plain-English
-- summary of what this migration implements and what it deliberately does
-- not build yet.

-- ---------------------------------------------------------------------
-- Tenant membership: the predicate every RLS policy below is keyed on.
-- ---------------------------------------------------------------------

create table public.tenant_members (
  tenant_id text not null,
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default statement_timestamp(),
  primary key (tenant_id, user_id)
);

alter table public.tenant_members enable row level security;
alter table public.tenant_members force row level security;

create policy tenant_members_select_own
on public.tenant_members for select
to authenticated
using (auth.uid() is not null and auth.uid() = user_id);

revoke all on table public.tenant_members from public, anon, authenticated;
grant select on table public.tenant_members to authenticated;

create or replace function public.is_tenant_member(check_tenant_id text)
returns boolean
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  select exists (
    select 1
    from public.tenant_members m
    where m.tenant_id = check_tenant_id and m.user_id = auth.uid()
  )
$$;

revoke execute on function public.is_tenant_member(text) from public, anon;
grant execute on function public.is_tenant_member(text) to authenticated;

-- ---------------------------------------------------------------------
-- Append-only enforcement: shared trigger function. Attached below to
-- learning_events, correction_events, evidence_items, artifact_versions.
-- Blocks UPDATE and DELETE for every role, including service_role, since a
-- trigger fires regardless of RLS or grants. Retraction is a new row in
-- correction_events, never an edit to the row being retracted.
-- ---------------------------------------------------------------------

create or replace function public.reject_ledger_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception using
    errcode = '0A000',
    message = format(
      'append-only table %I: %s is not permitted; write a new row instead',
      TG_TABLE_NAME, TG_OP
    );
end;
$$;

revoke execute on function public.reject_ledger_mutation() from public, anon, authenticated;

-- ---------------------------------------------------------------------
-- learning_events (curator/contracts/event.py: LearningEvent)
-- ---------------------------------------------------------------------

create table public.learning_events (
  event_id text primary key,
  tenant_id text not null,
  actor_id text not null,
  actor_kind text not null check (actor_kind in ('human', 'agent', 'system')),
  event_type text not null check (event_type in (
    'more_like_this', 'less_like_this', 'already_knew_this', 'surprise_me',
    'save', 'save_answer', 'ask_ai_question', 'ask_ai_follow_up',
    'create_report', 'read_more', 'accordion_expand', 'return_to_story',
    'dwell', 'scroll', 'imported_mail_unread_state', 'imported_browser_visit'
  )),
  occurred_at timestamptz not null,
  recorded_at timestamptz not null,
  surface text not null,
  idempotency_key text not null,
  evidence_class text not null check (evidence_class in ('observed', 'inferred', 'explicit', 'passive')),
  origin text not null check (origin in ('live', 'imported')),
  confidence text not null check (confidence in ('strong', 'medium', 'weak')),
  policy_revision integer not null,
  story_id text,
  story_cluster_id text,
  artifact_id text,
  conversation_id text,
  session_id text not null default '',
  duration_ms integer,
  retracted_by_event_id text,
  unique (tenant_id, idempotency_key)
);

alter table public.learning_events enable row level security;
alter table public.learning_events force row level security;

create policy learning_events_select_own
on public.learning_events for select
to authenticated
using (public.is_tenant_member(tenant_id));

create policy learning_events_insert_own
on public.learning_events for insert
to authenticated
with check (public.is_tenant_member(tenant_id));

create trigger learning_events_append_only
before update or delete on public.learning_events
for each row execute function public.reject_ledger_mutation();

revoke all on table public.learning_events from public, anon, authenticated;
revoke update, delete on table public.learning_events from authenticated;
grant select, insert on table public.learning_events to authenticated;

-- ---------------------------------------------------------------------
-- correction_events (curator/contracts/event.py: CorrectionEvent)
-- ---------------------------------------------------------------------

create table public.correction_events (
  event_id text primary key,
  tenant_id text not null,
  actor_id text not null,
  action text not null check (action in ('correct', 'retract', 'delete_request')),
  target_kind text not null,
  target_id text not null,
  reason_code text not null,
  occurred_at timestamptz not null,
  invalidated_snapshot_ids text[] not null default '{}'::text[]
);

alter table public.correction_events enable row level security;
alter table public.correction_events force row level security;

create policy correction_events_select_own
on public.correction_events for select
to authenticated
using (public.is_tenant_member(tenant_id));

create policy correction_events_insert_own
on public.correction_events for insert
to authenticated
with check (public.is_tenant_member(tenant_id));

create trigger correction_events_append_only
before update or delete on public.correction_events
for each row execute function public.reject_ledger_mutation();

revoke all on table public.correction_events from public, anon, authenticated;
revoke update, delete on table public.correction_events from authenticated;
grant select, insert on table public.correction_events to authenticated;

-- ---------------------------------------------------------------------
-- raw_imports (curator/contracts/evidence.py: RawImport)
-- ---------------------------------------------------------------------

create table public.raw_imports (
  raw_import_id text primary key,
  tenant_id text not null,
  owner_actor_id text not null,
  source_kind text not null,
  checksum text not null,
  schema_version text not null,
  storage_reference text not null,
  imported_at timestamptz not null,
  consent_version text not null,
  retention_state text not null check (retention_state in ('active', 'retracted', 'purged')),
  exported_at timestamptz,
  byte_size integer not null default 0,
  unique (tenant_id, source_kind, checksum)
);

alter table public.raw_imports enable row level security;
alter table public.raw_imports force row level security;

create policy raw_imports_select_own
on public.raw_imports for select
to authenticated
using (public.is_tenant_member(tenant_id));

create policy raw_imports_insert_own
on public.raw_imports for insert
to authenticated
with check (public.is_tenant_member(tenant_id));

create policy raw_imports_update_own
on public.raw_imports for update
to authenticated
using (public.is_tenant_member(tenant_id))
with check (public.is_tenant_member(tenant_id));

revoke all on table public.raw_imports from public, anon, authenticated;
grant select, insert, update on table public.raw_imports to authenticated;

-- ---------------------------------------------------------------------
-- evidence_items (curator/contracts/evidence.py: EvidenceItem)
-- ---------------------------------------------------------------------

create table public.evidence_items (
  evidence_id text primary key,
  tenant_id text not null,
  raw_import_id text references public.raw_imports (raw_import_id),
  source_item_id text not null,
  occurred_at timestamptz not null,
  recorded_at timestamptz not null,
  evidence_class text not null check (evidence_class in ('observed', 'inferred', 'explicit', 'passive')),
  origin text not null check (origin in ('live', 'imported')),
  confidence text not null check (confidence in ('strong', 'medium', 'weak')),
  weight double precision not null,
  policy_revision integer not null,
  story_id text,
  canonical_url text not null default '',
  entity_ids text[] not null default '{}'::text[],
  topic_tags text[] not null default '{}'::text[],
  corroborated boolean not null default false,
  corroborating_evidence_ids text[] not null default '{}'::text[],
  retracted_by_event_id text
);

alter table public.evidence_items enable row level security;
alter table public.evidence_items force row level security;

create policy evidence_items_select_own
on public.evidence_items for select
to authenticated
using (public.is_tenant_member(tenant_id));

create policy evidence_items_insert_own
on public.evidence_items for insert
to authenticated
with check (public.is_tenant_member(tenant_id));

create trigger evidence_items_append_only
before update or delete on public.evidence_items
for each row execute function public.reject_ledger_mutation();

revoke all on table public.evidence_items from public, anon, authenticated;
revoke update, delete on table public.evidence_items from authenticated;
grant select, insert on table public.evidence_items to authenticated;

-- ---------------------------------------------------------------------
-- knowledge_artifacts (curator/contracts/artifact.py: KnowledgeArtifact)
-- ---------------------------------------------------------------------

create table public.knowledge_artifacts (
  artifact_id text primary key,
  tenant_id text not null,
  actor_id text not null,
  artifact_type text not null check (artifact_type in ('question', 'answer', 'report', 'insight', 'save')),
  status text not null check (status in ('draft', 'settled', 'redacted', 'retracted')),
  publication_class text not null check (publication_class in ('private', 'public')),
  created_at timestamptz not null,
  current_version integer not null,
  title text not null default '',
  conversation_id text,
  story_id text
);

alter table public.knowledge_artifacts enable row level security;
alter table public.knowledge_artifacts force row level security;

create policy knowledge_artifacts_select_own
on public.knowledge_artifacts for select
to authenticated
using (public.is_tenant_member(tenant_id));

create policy knowledge_artifacts_insert_own
on public.knowledge_artifacts for insert
to authenticated
with check (public.is_tenant_member(tenant_id));

create policy knowledge_artifacts_update_own
on public.knowledge_artifacts for update
to authenticated
using (public.is_tenant_member(tenant_id))
with check (public.is_tenant_member(tenant_id));

revoke all on table public.knowledge_artifacts from public, anon, authenticated;
grant select, insert, update on table public.knowledge_artifacts to authenticated;

-- ---------------------------------------------------------------------
-- artifact_versions (curator/contracts/artifact.py: ArtifactVersion)
-- No tenant_id column on the frozen dataclass; tenant membership is proven
-- through the parent knowledge_artifacts row.
-- ---------------------------------------------------------------------

create table public.artifact_versions (
  artifact_id text not null references public.knowledge_artifacts (artifact_id),
  version integer not null,
  parent_version integer,
  checksum text not null,
  content_reference text not null,
  actor_id text not null,
  settled_at timestamptz not null,
  citations text[] not null default '{}'::text[],
  redacted_by_event_id text,
  primary key (artifact_id, version)
);

alter table public.artifact_versions enable row level security;
alter table public.artifact_versions force row level security;

create policy artifact_versions_select_own
on public.artifact_versions for select
to authenticated
using (exists (
  select 1 from public.knowledge_artifacts ka
  where ka.artifact_id = artifact_versions.artifact_id
    and public.is_tenant_member(ka.tenant_id)
));

create policy artifact_versions_insert_own
on public.artifact_versions for insert
to authenticated
with check (exists (
  select 1 from public.knowledge_artifacts ka
  where ka.artifact_id = artifact_versions.artifact_id
    and public.is_tenant_member(ka.tenant_id)
));

create trigger artifact_versions_append_only
before update or delete on public.artifact_versions
for each row execute function public.reject_ledger_mutation();

revoke all on table public.artifact_versions from public, anon, authenticated;
revoke update, delete on table public.artifact_versions from authenticated;
grant select, insert on table public.artifact_versions to authenticated;

-- ---------------------------------------------------------------------
-- artifact_relations (curator/contracts/artifact.py: ArtifactRelation)
-- ---------------------------------------------------------------------

create table public.artifact_relations (
  relation_id text primary key,
  tenant_id text not null,
  conversation_id text not null,
  artifact_id text not null references public.knowledge_artifacts (artifact_id),
  relation_type text not null,
  requested_type text not null default '',
  depth integer not null default 0
);

alter table public.artifact_relations enable row level security;
alter table public.artifact_relations force row level security;

create policy artifact_relations_select_own
on public.artifact_relations for select
to authenticated
using (public.is_tenant_member(tenant_id));

create policy artifact_relations_insert_own
on public.artifact_relations for insert
to authenticated
with check (public.is_tenant_member(tenant_id));

revoke all on table public.artifact_relations from public, anon, authenticated;
grant select, insert on table public.artifact_relations to authenticated;

-- ---------------------------------------------------------------------
-- deletion_receipts (curator/contracts/receipt.py: ReceiptEnvelope + DeletionReceipt)
-- ---------------------------------------------------------------------

create or replace function public.deletion_receipt_may_settle(state text, projections jsonb)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select state <> 'settled'
    or not exists (
      select 1
      from jsonb_array_elements(projections) as p
      where (p ->> 'resolved') is distinct from 'true'
    )
$$;

revoke execute on function public.deletion_receipt_may_settle(text, jsonb) from public, anon, authenticated;

create table public.deletion_receipts (
  receipt_id text primary key,
  tenant_id text not null,
  kind text not null default 'deletion',
  state text not null check (state in ('settled', 'partial', 'failed', 'unknown')),
  created_at timestamptz not null,
  policy_revision integer not null,
  actor_id text not null default '',
  reason_code text not null default '',
  settled_at timestamptz,
  target_kind text not null,
  target_ids text[] not null default '{}'::text[],
  correction_watermark timestamptz not null,
  invalidated_snapshot_ids text[] not null default '{}'::text[],
  rebuild_id text not null,
  zero_contribution_verdict boolean not null,
  projections jsonb not null default '[]'::jsonb check (jsonb_typeof(projections) = 'array'),
  mirrored_targets text[] not null default '{}'::text[],
  audit_chain_queryable boolean not null default true,
  check (public.deletion_receipt_may_settle(state, projections))
);

alter table public.deletion_receipts enable row level security;
alter table public.deletion_receipts force row level security;

create policy deletion_receipts_select_own
on public.deletion_receipts for select
to authenticated
using (public.is_tenant_member(tenant_id));

create policy deletion_receipts_insert_own
on public.deletion_receipts for insert
to authenticated
with check (public.is_tenant_member(tenant_id));

create policy deletion_receipts_update_own
on public.deletion_receipts for update
to authenticated
using (public.is_tenant_member(tenant_id))
with check (public.is_tenant_member(tenant_id));

revoke all on table public.deletion_receipts from public, anon, authenticated;
grant select, insert, update on table public.deletion_receipts to authenticated;

-- ---------------------------------------------------------------------
-- mirror_receipts (curator/contracts/mirror.py: MirrorReceipt)
-- ---------------------------------------------------------------------

create table public.mirror_receipts (
  receipt_id text primary key,
  tenant_id text not null,
  artifact_id text not null references public.knowledge_artifacts (artifact_id),
  artifact_version integer not null,
  adapter_id text not null,
  target_id text not null,
  state text not null check (state in ('planned', 'writing', 'settled', 'conflict', 'unknown')),
  idempotency_key text not null,
  attempted_at timestamptz not null,
  expected_prior_checksum text not null,
  attempted_checksum text not null,
  readback_checksum text not null default '',
  settled_at timestamptz,
  reason_code text not null default '',
  created_revision_id text not null default '',
  prior_receipt_ids text[] not null default '{}'::text[],
  prior_attempt_state text not null default '',
  resolution_ref text not null default ''
);

alter table public.mirror_receipts enable row level security;
alter table public.mirror_receipts force row level security;

create policy mirror_receipts_select_own
on public.mirror_receipts for select
to authenticated
using (public.is_tenant_member(tenant_id));

create policy mirror_receipts_insert_own
on public.mirror_receipts for insert
to authenticated
with check (public.is_tenant_member(tenant_id));

create policy mirror_receipts_update_own
on public.mirror_receipts for update
to authenticated
using (public.is_tenant_member(tenant_id))
with check (public.is_tenant_member(tenant_id));

revoke all on table public.mirror_receipts from public, anon, authenticated;
grant select, insert, update on table public.mirror_receipts to authenticated;

commit;
