# Personalization contract

Status: local implementation. Cloud activation is not complete.

## Boundary

The public news page remains readable without an account. Authentication exists only to read and write the signed-in user's private preferences. The browser and agent use a Supabase publishable key. A `service_role` key is prohibited in both flows.

## Data contract

`public.user_preferences` has one row per `auth.users.id`.

| Field | Bound |
|---|---|
| `locale` | Required. `en` or `zh`. |
| `interests` | Required one-dimensional array, at most 20 trimmed strings, each 1 to 80 characters and at most 160 UTF-8 bytes. |
| `saved_searches` | Required JSON array, at most 20 objects and 8,192 serialized bytes total. |
| Saved search | Exactly `id`, `query`, and `enabled`. IDs are unique, trimmed, 1 to 64 characters and at most 128 UTF-8 bytes. Queries are trimmed, 1 to 300 characters and at most 600 UTF-8 bytes. `enabled` is Boolean. |
| `revision` | Server-owned non-negative integer. Starts at 0 and increments by one. |

Authenticated users may select and delete their own row. They may insert only `user_id`, `locale`, `interests`, and `saved_searches`, with `user_id = auth.uid()`. They have no direct update grant. Updates use `compare_and_swap_user_preferences(expected_revision, new_locale, new_interests, new_saved_searches)`.

The RPC returns one of:

- `{"status":"updated","revision":N,"updated_at":...}`
- `{"status":"conflict","revision":N}`
- `{"status":"not_found"}`

Invalid data and unauthenticated calls are errors. A conflict does not change the row. RLS separately prevents access to any other user's row.

## Browser path

`static/auth/callback/index.html` and `static/auth/client.js` use Supabase Authorization Code with PKCE without a third-party JavaScript dependency. The verifier, state, and minimal session remain in `sessionStorage`. The single-use `client_state` is carried inside the exact `redirect_to` URL and verified after redirect. It does not rely on Supabase forwarding an application-supplied provider OAuth `state`. Callback parameters are copied into memory and immediately removed from browser history. A successful exchange must include a bounded, decodable access token whose `sub` exactly matches the returned `user.id`. The browser persists only `access_token`, `refresh_token`, `expires_at`, and `user_id`. It never persists provider tokens, an optional provider ID token, the authorization code, or the raw response. Errors never display tokens or authorization codes.

The browser exposes `window.NewsCuratorPersonalization.get()` and `.set(input)` as the human preference API seam. They use the same owner-only table and compare-and-swap RPC as the agent client, apply the same input and response bounds, and require the validated session stored by the callback. When the saved access token has expired, both operations refresh through the exact configured Supabase origin at `/auth/v1/token?grant_type=refresh_token` before any preference request. The refresh request uses only the publishable key and current refresh token, rejects redirects and response URL mismatches before reading a body, requires the same user id, requires a newly rotated refresh token, and persists only the four-field minimal session projection. Any refresh failure erases the saved session. The initial callback is bound by the single-use `client_state`, its exact `redirect_to`, and the PKCE verifier. Refresh is instead bound to the rotating refresh token and existing user id. No visual preference interface is part of this backend contract.

Every callback consumes state and verifier before branching on its result. This includes provider errors, callbacks without a code, invalid state, and token-exchange failures. Callback query parameters are removed from browser history on all of those paths.

The checked-in callback is a fail-closed template. Do not edit its placeholders for activation. Materialize a separate build artifact using the exact project origin and public key:

```sh
python scripts/build_auth_callback.py \
  --supabase-url https://PROJECT_REF.supabase.co \
  --publishable-key "$NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY" \
  --output BUILD_ROOT/auth/callback/index.html
```

The materializer validates one exact HTTPS origin, rejects wildcard origins and service-role keys, refuses to overwrite the template, and emits an exact `connect-src` origin. `static/auth/client.js` must be copied to `BUILD_ROOT/auth/client.js` by the ordinary static build. Configure the host to send the generated CSP as an HTTP header plus `Cache-Control: no-store` on the callback.

## Agent path

The CLI reads only:

- `NEWS_CURATOR_SUPABASE_URL`
- `NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY`

`login` binds an OS-assigned port on `127.0.0.1`, opens the browser, puts a single-use `client_state` in that exact `redirect_to`, accepts only the exact `/callback` route and Host header, verifies the returned `client_state`, and exchanges the code with the same PKCE attempt. Login succeeds only when the response includes a bounded, decodable access token whose `sub` exactly matches the returned `user.id`. It does not require Supabase to return an optional provider ID token. Both agent HTTP transports explicitly disable environment proxies and install no cookie, HTTP-auth, proxy-auth, or netrc handler. Persistent mode uses the macOS `security` command with `-w` last so the secret enters through the prompt/stdin path instead of process arguments. Real Keychain prompt, access-control, save, reload, and deletion behavior is not live-verified and must remain a separate activation check. `login --memory-only` is a one-process authentication check and explicitly discards its session on exit. Other commands reject `--memory-only` because a separate CLI process cannot reuse that session. There is no plaintext fallback. Refresh requires the returned user id to exactly match the saved session, requires a present and newly rotated refresh token, validates a bounded access token, and accepts only a future expiry no more than 24 hours away. It then replaces the minimal session atomically. Any HTTP, parsing, validation, identity, rotation, expiry, or persistence failure erases the saved session and returns only a low-information authentication error. Logout attempts remote revocation and always erases the local session. The CLI has no command that prints an access token, refresh token, or authorization code.

`get` reads the one row visible through RLS. `set` validates a bounded JSON object and calls the CAS RPC with the saved row's `revision`. If no row exists and `expected_revision` is 0, it creates the caller's row using the authenticated session user id and server-default revision. A concurrent first insert becomes a conflict. The client never sends a caller-selected revision or user id on an update.

Example command shape, with values supplied through the process environment:

```sh
python scripts/personalization_cli.py login
python scripts/personalization_cli.py status
python scripts/personalization_cli.py refresh
python scripts/personalization_cli.py get
python scripts/personalization_cli.py set --input preferences.json
python scripts/personalization_cli.py set --input -
python scripts/personalization_cli.py logout
```

Set input has exactly these fields:

```json
{
  "expected_revision": 0,
  "locale": "en",
  "interests": ["agents"],
  "saved_searches": [
    {"id": "daily", "query": "agent news", "enabled": true}
  ]
}
```

`get` and successful `set` may print the caller's preference row as JSON. They never print the access token, refresh token, or authorization code.

For native loopback OAuth, the local config admits only `http://127.0.0.1:*/callback`; the CLI narrows that pattern at runtime to its one bound port and exact path. Production Supabase redirect configuration must contain the static callback exactly and the native loopback pattern only. Do not admit `localhost`, arbitrary paths, arbitrary hosts, or HTTPS wildcards.

## Cloud activation checklist

Cloud-linked status requires all of the following. None is implied by local tests.

1. Create or select the intended Supabase project outside this repository.
2. Apply the checked-in migration and independently inspect grants, RLS policies, and function security.
3. Enable Google Auth in Supabase with credentials stored only in the Supabase project settings.
4. In Google Cloud, allow the exact Supabase provider callback shown by Supabase. Do not guess it.
5. In Supabase, set the exact deployed site origin, exact static callback, and the single loopback pattern documented above.
6. Run the callback materializer into the static build output using only the project URL and publishable key. Put the same two public values in the agent environment. Confirm no service-role credential appears in either surface, build output, or logs.
7. Run the two-user, anon, expired-token, altered-user-id, direct-update, oversized-data, replay, refresh-rotation, and logout tests against the linked project.
8. Verify callback history cleanup and the deployed HTTP CSP and no-store headers in a real browser.
9. Record the linked test receipt separately. Do not relabel local contract tests as cloud proof.
