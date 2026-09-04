begin;

-- These plain string literals must reach PostgreSQL's regex engine with their
-- backslashes intact. Match the guard used by the original ledger migration.
set standard_conforming_strings = on;

-- Python 3.12's Unicode tables assign U+13439-U+1343F as format characters.
-- New databases receive the expanded full class from the original schema
-- migration. These additive checks close the same gap on databases that
-- already applied that migration.
alter table public.tenant_members add constraint tenant_members_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');

alter table public.learning_events add constraint learning_events_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');
alter table public.learning_events add constraint learning_events_actor_id_unicode15_cf_check check (actor_id !~ '[\U00013439-\U0001343F]');
alter table public.learning_events add constraint learning_events_user_id_unicode15_cf_check check (user_id !~ '[\U00013439-\U0001343F]');

alter table public.correction_events add constraint correction_events_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');
alter table public.correction_events add constraint correction_events_actor_id_unicode15_cf_check check (actor_id !~ '[\U00013439-\U0001343F]');
alter table public.correction_events add constraint correction_events_user_id_unicode15_cf_check check (user_id !~ '[\U00013439-\U0001343F]');

alter table public.raw_imports add constraint raw_imports_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');
alter table public.raw_imports add constraint raw_imports_actor_id_unicode15_cf_check check (actor_id !~ '[\U00013439-\U0001343F]');
alter table public.raw_imports add constraint raw_imports_user_id_unicode15_cf_check check (user_id !~ '[\U00013439-\U0001343F]');

alter table public.evidence_items add constraint evidence_items_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');
alter table public.evidence_items add constraint evidence_items_actor_id_unicode15_cf_check check (actor_id !~ '[\U00013439-\U0001343F]');
alter table public.evidence_items add constraint evidence_items_user_id_unicode15_cf_check check (user_id !~ '[\U00013439-\U0001343F]');

alter table public.knowledge_artifacts add constraint knowledge_artifacts_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');
alter table public.knowledge_artifacts add constraint knowledge_artifacts_actor_id_unicode15_cf_check check (actor_id !~ '[\U00013439-\U0001343F]');
alter table public.knowledge_artifacts add constraint knowledge_artifacts_user_id_unicode15_cf_check check (user_id !~ '[\U00013439-\U0001343F]');

alter table public.artifact_versions add constraint artifact_versions_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');
alter table public.artifact_versions add constraint artifact_versions_actor_id_unicode15_cf_check check (actor_id !~ '[\U00013439-\U0001343F]');
alter table public.artifact_versions add constraint artifact_versions_user_id_unicode15_cf_check check (user_id !~ '[\U00013439-\U0001343F]');

alter table public.artifact_relations add constraint artifact_relations_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');
alter table public.artifact_relations add constraint artifact_relations_actor_id_unicode15_cf_check check (actor_id !~ '[\U00013439-\U0001343F]');
alter table public.artifact_relations add constraint artifact_relations_user_id_unicode15_cf_check check (user_id !~ '[\U00013439-\U0001343F]');

alter table public.deletion_receipts add constraint deletion_receipts_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');
alter table public.deletion_receipts add constraint deletion_receipts_actor_id_unicode15_cf_check check (actor_id !~ '[\U00013439-\U0001343F]');
alter table public.deletion_receipts add constraint deletion_receipts_user_id_unicode15_cf_check check (user_id !~ '[\U00013439-\U0001343F]');

alter table public.mirror_receipts add constraint mirror_receipts_tenant_id_unicode15_cf_check check (tenant_id !~ '[\U00013439-\U0001343F]');
alter table public.mirror_receipts add constraint mirror_receipts_actor_id_unicode15_cf_check check (actor_id !~ '[\U00013439-\U0001343F]');
alter table public.mirror_receipts add constraint mirror_receipts_user_id_unicode15_cf_check check (user_id !~ '[\U00013439-\U0001343F]');

commit;
