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

`static/auth/callback/index.html` and `static/auth/client.js` provide the human interface without a third-party JavaScript dependency. An existing owner requests an email one-time code from `/auth/v1/otp` with `create_user: false`, then verifies it at `/auth/v1/verify`. Production must configure the Supabase email template to show `{{ .Token }}` and must pre-provision the owner before disabling public signup. A successful verification must include a bounded, decodable access token whose `sub` exactly matches the returned `user.id`. The browser persists only `access_token`, `refresh_token`, `expires_at`, and `user_id` in `sessionStorage`. It never persists the email address, entered code, provider tokens, or the raw response. Errors never display credentials.

The browser exposes `window.NewsCuratorPersonalization.get()` and `.set(input)` as the preference API seam used by the page. They use the same owner-only table and compare-and-swap RPC as the agent client, apply the same input and response bounds, and require the validated session created by email-code verification. When the saved access token has expired, both operations refresh through the exact configured Supabase origin at `/auth/v1/token?grant_type=refresh_token` before any preference request. The refresh request uses only the publishable key and current refresh token, rejects redirects and response URL mismatches before reading a body, requires the same user id, requires a newly rotated refresh token, and persists only the four-field minimal session projection. Any refresh failure erases the saved session.

The same JavaScript still contains the PKCE callback primitives used by the agent login path and their regression tests. The human page does not start that flow.

The checked-in callback is a fail-closed template. Do not edit its placeholders for activation. Materialize a separate build artifact using the exact project origin and public key:

```sh
python scripts/build_auth_callback.py \
  --supabase-url https://PROJECT_REF.supabase.co \
  --publishable-key "$NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY" \
  --output BUILD_ROOT/auth/callback/index.html \
  --site-index BUILD_ROOT/index.html
```

The materializer validates one exact HTTPS origin, rejects wildcard origins and service-role keys, refuses to overwrite the template, and emits an exact `connect-src` origin. The callback may be deployed directly for owner acceptance testing while ranking remains disabled. The workflow replaces the rendered site's dormant marker with a relative settings link only when `NEWS_CURATOR_PERSONALIZATION_ENABLED` is `true`, so the public digest does not advertise personalization before saved interests affect ranking. Unconfigured forks therefore do not advertise a dead settings page. `static/auth/client.js` and `static/auth/styles.css` must be copied to `BUILD_ROOT/auth/` by the ordinary static build. GitHub Pages cannot set a route-specific HTTP CSP or cache policy, so the checked-in meta CSP is the deployable control on this static host.

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
3. Pre-provision the intended owner account, configure the email template to show `{{ .Token }}`, and disable public signup.
4. In Supabase, set the exact deployed site origin and exact static page URL. If the agent login path is activated, also enable the Google provider, configure its Supabase provider callback, and add the native loopback pattern.
5. Run the callback materializer into the static build output using only the project URL and publishable key. Put the same two public values in the agent environment. Confirm no service-role credential appears in either surface, build output, or logs.
6. Run the two-user, anon, expired-token, altered-user-id, direct-update, oversized-data, replay, refresh-rotation, and logout tests against the linked project.
7. Verify email-code sign-in, the deployed meta CSP, session cleanup, interest save/reload, sign-out, and the absence of credentials in the rendered assets in a real browser.
8. Record the linked test receipt separately. Do not relabel local contract tests as cloud proof.
