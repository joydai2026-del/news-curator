begin;

-- A retained story becomes owner-actionable only after the ranking service has
-- returned it in that owner's still-valid frozen order. Existing publication,
-- private-discovery, and prior-state access remains unchanged.
create or replace function public.discovery_story_access(p_user_id uuid,p_story_id text) returns boolean
language sql stable security definer set search_path=pg_catalog,public as $$
  select p_user_id is not null and (
    exists(select 1 from public.publication_entries e join public.publication_runs r using(publication_seq) where e.story_id=p_story_id and r.finalized_at is not null)
    or exists(select 1 from public.private_discovery_entries where owner_user_id=p_user_id and story_id=p_story_id)
    or exists(select 1 from public.user_story_state where user_id=p_user_id and story_id=p_story_id)
    or exists(
      select 1 from public.m2_frozen_rankings f
      cross join lateral jsonb_array_elements(f.cards) card
      join public.retained_corpus_observations retained on retained.story_id=card->>'story_id'
      where f.user_id=p_user_id and f.expires_at>statement_timestamp()
        and card->>'story_id'=p_story_id
    )
  )
$$;
revoke all on function public.discovery_story_access(uuid,text) from public,anon,authenticated;

-- Preserve the existing composite foreign key. Register public retained
-- categories in the shared registry used by the unchanged interest RPC.
insert into public.story_topics(story_id,topic_id,topic_name)
select story_id,category_id,category_id from public.retained_corpus_categories
on conflict (story_id,topic_id) do nothing;

create function public.m2_register_retained_story_topic()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  insert into public.story_topics(story_id,topic_id,topic_name)
  values(new.story_id,new.category_id,new.category_id)
  on conflict (story_id,topic_id) do nothing;
  return new;
end; $$;
create trigger m2_register_retained_story_topic_after_insert
after insert on public.retained_corpus_categories
for each row execute function public.m2_register_retained_story_topic();
revoke all on function public.m2_register_retained_story_topic() from public,anon,authenticated;

commit;
