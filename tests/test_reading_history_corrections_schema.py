from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "supabase/migrations/202609070001_reading_history.sql"


def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_correction_migration_is_atomic_and_policy_is_exactly_twenty():
    text = sql()
    assert text.startswith("begin;")
    assert text.rstrip().endswith("commit;")
    assert "page_size integer not null default 20" in text
    assert "check (page_size between 1 and 100)" in text
    assert text.count("p_limit < 1 or p_limit > policy_page_size") == 3
    assert text.count("select page_size into policy_page_size") == 3
    assert text.count("limit least(coalesce(p_limit, policy_page_size), policy_page_size)") == 4
    assert "limit least(coalesce(p_limit, fp.page_size), fp.page_size)" not in text


def test_publication_contract_has_empty_topics_unique_history_and_updates():
    text = sql()
    assert "create table public.publication_topics" in text
    assert "jsonb_agg(jsonb_build_object('topic_id', pt.topic_id, 'name', pt.topic_name)" in text
    assert "row_number() over (partition by pe.story_id" in text
    assert text.count("row_number() over (partition by pe.story_id") >= 2
    assert "publication_seq" in text and "topic_ids" in text
    assert "ordering_mode text not null" in text
    assert "ordering_key jsonb not null" in text
    assert "alter table public.publication_topics force row level security" in text
    assert "revoke all on table public.publication_topics from public, anon, authenticated" in text
    assert "foreign key (publication_seq, topic_id)" in text
    assert "references public.publication_topics(publication_seq, topic_id)" in text


def test_zero_or_short_initial_page_has_a_server_issued_history_cursor():
    text = sql()
    latest = text[text.index("create or replace function public.latest_publication"):text.index("create or replace function public.feed_page")]
    assert "'initial_history_cursor'" in latest
    assert "'before_published_at', pr.built_at - make_interval(days => fp.initial_window_days)" in latest
    assert "'before_story_id', ''" in latest
    assert "publication_entries" not in latest
    feed = text[text.index("create or replace function public.feed_page"):text.index("create or replace function public.saved_page")]
    assert "edition_clock" in feed
    assert "pe.published_at >= ec.built_at - make_interval" in feed
    assert "p_after_story_id text default null" in feed
    assert "pe.story_id > p_after_story_id" in feed
    edition_cursor_validation = feed[
        feed.index("if requested_mode = 'edition_rank' and (p_after_position is null)"):
        feed.index("if p_topic_id is not null")
    ]
    assert "p_before_story_id" not in edition_cursor_validation


def test_desired_state_digest_bound_finalizer_and_retention_are_service_only():
    text = sql()
    assert "saved_at = case when p_saved then coalesce(user_story_state.saved_at, now()) else null end" in text
    assert text.count("p_expected_revision is null") == 2
    assert "candidate_digest" in text
    assert "extensions.digest(convert_to(p_candidate::text, 'utf8'), 'sha256')" in text
    assert "publication replay mismatch" in text
    assert "story alias ownership mismatch" in text
    assert "array(select key from jsonb_object_keys(p_candidate)" in text
    assert "create or replace function public.prune_publication_history()" in text
    assert "receipts_pruned" in text
    assert "user_action_receipts_created_idx" in text
    assert "grant execute on function public.prune_publication_history() to service_role" in text
    assert "grant execute on function public.prune_publication_history() to authenticated" not in text


def test_public_feed_and_saved_reads_exclude_url_less_or_unsafe_canonical_rows():
    text = sql()
    assert text.count("canonical_url ~ '^https?://[^/@[:space:]]+(/|$)'") >= 2


def test_read_rpcs_return_private_cas_state_without_user_identity():
    text = sql()
    feed = text[text.index("create or replace function public.feed_page"):text.index("create or replace function public.saved_page")]
    saved = text[text.index("create or replace function public.saved_page"):text.index("create or replace function public.updates_since")]
    assert "'state_revision', coalesce(us.revision, 0)" in feed
    assert "'interests', coalesce(interests.rows, '[]'::jsonb)" in feed
    assert "'state_revision', us.revision" in saved
    assert "'interests', coalesce(interests.rows, '[]'::jsonb)" in saved
    assert "'user_id'" not in feed and "'user_id'" not in saved


def test_archive_attestation_url_identity_and_receipt_binding_are_enforced():
    text = sql()
    assert "site_sha256 text not null" in text
    assert "'site_sha256', existing.site_sha256" in text
    assert "returns jsonb language plpgsql" in text
    canonical_table = text[text.index("create table public.canonical_stories"):
                           text.index("create index canonical_stories_feed_idx")]
    assert "story_id = 'story:' || encode(extensions.digest(canonical_url, 'sha256')" in canonical_table
    assert "convert_to" not in canonical_table
    assert "pg_advisory_xact_lock(hashtextextended(caller::text || ':state:' || resource" in text
    assert "operation = 'set_story_state'" in text
    assert "request_digest = request_hash" in text


def test_saved_cards_and_lossless_updates_have_complete_keysets():
    text = sql()
    saved = text[text.index("create or replace function public.saved_page"):text.index("create or replace function public.updates_since")]
    updates = text[text.index("create or replace function public.updates_since"):text.index("create or replace function public.set_story_state")]
    for field in ("'topic_ids'", "'topic_ranks'", "'source_name'", "'source_kind'",
                  "'ranking_explanation'", "'coverage_mentions'", "'score_components'", "'ordering_mode'",
                  "'ordering_key'", "'publication_seq'", "'language'", "'position'",
                  "'page_order_mode'", "'next_cursor'", "'interests'"):
        assert field in saved
    assert "to_jsonb(cm) - 'mention_id'" in saved
    assert "'publication_topic_id'" not in saved
    assert "left join lateral (" in saved
    assert "'publication_seq', coalesce(card.publication_seq, 0)" in saved
    assert "'position', coalesce(card.position, 0)" in saved
    assert "'ordering_mode', coalesce(card.ordering_mode, 'weighted_total')" in saved
    assert "'topic_ranks', coalesce(card.topic_ranks, '{}'::jsonb)" in saved
    assert "'source_kind', coalesce(card.source_kind, s.source_kind)" in saved
    assert "'source_name', coalesce(card.source_name, s.source_name)" in saved
    assert "from public.story_topics st where st.story_id = s.story_id" in saved
    assert "limit 20" in saved
    assert "p_after_publication_seq bigint" in updates
    assert "p_after_published_at timestamptz" in updates
    assert "p_after_story_id text" in updates
    prune = text[text.index("create or replace function public.prune_publication_history"):
                 text.index("revoke all on table public.feed_policy")]
    assert "us.story_id = pe.story_id and us.saved_at is not null" not in prune
    assert "delete from public.user_action_receipts where created_at < receipt_cutoff" in prune


def test_save_requires_retained_topic_identity_for_fallback_cards():
    text = sql()
    state = text[text.index("create or replace function public.set_story_state"):
                 text.index("create or replace function public.set_story_interest")]
    assert "if p_saved and not exists (" in state
    assert "select 1 from public.story_topics where story_id = p_story_id" in state
    assert "raise exception 'story is not saveable'" in state


def test_writes_validate_resources_and_bound_new_receipts_after_replay():
    text = sql()
    state = text[text.index("create or replace function public.set_story_state"):
                 text.index("create or replace function public.set_story_interest")]
    interest = text[text.index("create or replace function public.set_story_interest"):
                    text.index("create or replace function public.finalize_archive")]
    assert "from public.canonical_stories where story_id = p_story_id" in state
    assert "where story_id = p_story_id and topic_id = p_topic_id" in interest
    for body in (state, interest):
        assert body.index("if answer is not null then") < body.index("receipt limit reached")
        assert "':receipt-cap'" in body
        assert "receipt_max_per_user" in body
