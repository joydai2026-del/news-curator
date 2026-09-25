-- pg-delta: transaction=false
-- This standalone statement avoids blocking ingestion and readers while the
-- retained-corpus dedupe index is built. If a concurrent build fails, inspect
-- the index validity and repair it before retrying this migration.
create index concurrently retained_corpus_dedupe_peer_idx on public.retained_corpus_observations
  (language, md5(public.m2_story_dedupe_key(title)), published_at, story_id);
