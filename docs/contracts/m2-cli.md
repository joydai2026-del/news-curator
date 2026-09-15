# M2 CLI contract

`scripts/m2_cli.py` is an owner-scoped adapter over the deployed M2 RPCs and rank endpoint. It reuses the existing `AgentAuth` session and the macOS Keychain account derived from the configured Supabase origin. It does not create or copy a browser session, accept service-role credentials, or implement ranker business logic.

Every command requires a new absolute `--output` file. The client atomically creates it as a regular owner-owned `0600` file, rejects existing paths and symlinks, and prints only a fixed completion or failure message. Raw responses, identifiers, cards, history, and tokens never go to stdout or stderr.

Commands: `history`, `rank` (feed when query is absent and search when supplied), `page`, `consent`, `clear-history`, `export`, `event`, `state-event`, and `interest-event`. RPC names are fixed in the client. Combined mutation payloads come from a bounded JSON file and are validated by the existing server RPC. `rank` gets the current owner history first and sends the same binding fields as `static/reader.js`. `page` accepts a bounded JSON input object with one opaque `cursor` string and issues the deployed `GET /page?cursor=` route using URL encoding.

`export` requires a bounded JSON input object with exactly `p_cursor` and `p_expected_fence`. Both may be `null` for the first page. A continuation supplies the opaque response cursor plus the 64-character lowercase-hex fence from the preceding page. The RPC contract takes both values as text; the JSON file prevents unbounded command-line values while preserving the server's exact request shape.

For a same-principal parity check, use one authenticated owner session and compare the private UI/API/CLI receipts by result IDs and order, binding revisions and generations, mode or fallback reason, mutation revision, and export section/count digest. Any behavior change requires the owner-approved action and a fresh history receipt.
