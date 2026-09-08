# Private dashboard backend contract

Status: source contract. No dashboard UI or Knowledge Wiki behavior is defined here.

## Summary RPC

`public.dashboard_summary()` is authenticated and owner-scoped. It returns exactly:

- `schema_version`: integer `1`
- `scope`: `current_retained_state`
- `snapshot_at`: timezone-aware database statement timestamp
- `saved_count`: current rows with `saved_at`
- `saved_unread_count`: current saved rows without `read_at`
- `read_count`: current rows with `read_at`
- `active_interest_signal_count`: current story-topic signal rows
- `topic_signals`: bounded rows of exactly `topic_id`, `more_like_count`, and `less_like_count`

Counts describe current retained state. They are not reading-history, activity, completion, or lifetime totals. Topic rows are ordered by total current signals descending, then topic id. `feed_policy.dashboard_topic_limit` controls their maximum and is validated from 1 through 100. The four totals are complete even when the breakdown is truncated.

All JSON integers, including counts, positions, cursors, and revisions, must fit JavaScript's exact integer range, 0 through 9,007,199,254,740,991. The RPC fails rather than rounding or truncating an out-of-range count.

The function requires `auth.uid()`, pins its search path, is revoked from `public` and `anon`, and is granted only to `authenticated`. It returns no user id, story id, raw declared interest, token, or credential. Existing direct-table denials remain unchanged.

## Loaded dashboard snapshot

The UI label is **Download this view**. This is never called an account export. Both browser and CLI use exactly these top-level keys:

- `schema_version`: integer `1`
- `kind`: `loaded_dashboard_snapshot`
- `snapshot_at`: timezone-aware UTC creation time
- `summary`: exact summary RPC object
- `preferences`: exactly `revision`, `locale`, `interests`, `saved_searches`, `created_at`, `updated_at`
- `saved`: exactly `loaded_count`, `displayed_count`, `page_size`, `all_saved_loaded`, `next_cursor`, `items`

`loaded_count` counts distinct Saved cards fetched into the current view. `displayed_count` counts the cards included after any local filter. `all_saved_loaded` becomes true only after a short or empty page proves cursor exhaustion. Otherwise `next_cursor` is the last server-issued `{before_saved_at,before_story_id}` cursor. `page_size` is read from the current publication policy. The CLI loads only the explicitly requested number of pages and never silently drains Saved.

The snapshot contains no access token, refresh token, publishable key, email, or user id. It is generated locally and not uploaded or retained by News Curator. Knowledge Wiki timing and its source-cited AI contract remain separate and unresolved.
