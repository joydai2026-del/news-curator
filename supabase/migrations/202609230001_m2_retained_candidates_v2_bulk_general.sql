begin;

-- The live general pool spent about 5.8 seconds on two serial candidate RPCs
-- per continuation pass. Its first hundred rows can be frozen exclusions, so
-- returning up to 200 in one RPC reduces repeated full-corpus dedupe work.
-- Named lanes remain capped at 100 in both SQL and the ranker transport. This
-- changes only the general-pool limit, not dedupe or cursor rules.
-- Use the live function definition so this migration cannot accidentally
-- replace a later correction to its selection logic with an older copy.
do $$
declare
  definition text;
  old_check constant text := 'p_limit > 100';
begin
  select pg_get_functiondef(
    'public.m2_retained_candidates_v2(text,text,text,text[],text[],integer,integer,integer,integer,timestamptz,text,integer,integer,integer)'::regprocedure
  ) into definition;
  if definition is null or
     length(definition) - length(replace(definition, old_check, '')) <> length(old_check) then
    raise exception 'm2_retained_candidates_v2 limit guard changed; inspect before applying bulk read';
  end if;
  execute replace(definition, old_check,
    'p_limit > (case when p_lane is null then 200 else 100 end)');
end;
$$;

commit;
