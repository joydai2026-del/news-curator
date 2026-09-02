#!/usr/bin/env python3
"""Agent login lifecycle for private News Curator preferences."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socket
import sys
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curator.personalization import (  # noqa: E402
    AgentAuth,
    AuthConfig,
    AuthError,
    MacOSKeychainStorage,
    MemoryTokenStorage,
    PreferenceClient,
    PreferenceInput,
)
from curator.personalization.preferences import MAX_INPUT_BYTES  # noqa: E402


@dataclass
class _CallbackResult:
    url: str | None = None


def _receive_callback(auth: AgentAuth, timeout: float) -> tuple[object, str]:
    result = _CallbackResult()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if self.client_address[0] != "127.0.0.1" or self.headers.get("Host") != expected_host:
                self.send_error(400)
                return
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            result.url = f"http://{expected_host}{self.path}"
            body = b"Sign in received. You may close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = timeout
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    attempt, authorize_url = auth.begin_login(redirect)
    if not webbrowser.open(authorize_url, new=1, autoraise=True):
        server.server_close()
        raise AuthError("The browser could not be opened.")
    server.handle_request()
    server.server_close()
    if result.url is None:
        raise AuthError("Sign in timed out.")
    return attempt, result.url


def _storage(memory_only: bool, account: str):
    return MemoryTokenStorage() if memory_only else MacOSKeychainStorage(account=account)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("login", "status", "refresh", "logout", "get", "set"))
    parser.add_argument("--memory-only", action="store_true", help="Keep the session only for this process.")
    parser.add_argument("--timeout", type=float, default=180.0, help="Loopback callback timeout in seconds.")
    parser.add_argument("--input", help="For set, a validated JSON file or - for standard input.")
    return parser


def _read_preference_input(location: str | None) -> PreferenceInput:
    if not location:
        raise ValueError("set requires --input FILE or --input -.")
    if location == "-":
        raw = sys.stdin.read(MAX_INPUT_BYTES + 1)
    else:
        path = Path(location)
        if not path.is_file():
            raise ValueError("The preference input file does not exist.")
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("The preference input file is too large.")
        raw = path.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("The preference input is too large.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("The preference input is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The preference input must be a JSON object.")
    return PreferenceInput.from_mapping(payload)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.timeout <= 600:
        print("Error: timeout must be between 1 and 600 seconds.", file=sys.stderr)
        return 2
    if args.memory_only and args.command != "login":
        print(
            "Error: --memory-only has no cross-process session. Use persistent Keychain for this command.",
            file=sys.stderr,
        )
        return 2
    if args.input and args.command != "set":
        print("Error: --input is valid only with set.", file=sys.stderr)
        return 2
    url = os.environ.get("NEWS_CURATOR_SUPABASE_URL", "")
    key = os.environ.get("NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY", "")
    try:
        config = AuthConfig(url, key)
        account = urllib.parse.urlsplit(config.supabase_url).hostname or "news-curator"
        auth = AgentAuth(config, _storage(args.memory_only, account))
        if args.command == "login":
            attempt, callback = _receive_callback(auth, args.timeout)
            auth.finish_login(attempt, callback)
            print(
                "Authentication validated for this process; the session is discarded on exit."
                if args.memory_only
                else "Signed in. Session is protected by macOS Keychain."
            )
        elif args.command == "status":
            auth.valid_session()
            print("Signed in with a valid session.")
        elif args.command == "refresh":
            auth.refresh()
            print("Session refreshed and rotated.")
        elif args.command == "logout":
            auth.logout()
            print("Signed out and erased the local session.")
        elif args.command == "get":
            session = auth.valid_session()
            preference = PreferenceClient(config).get(session)
            print(json.dumps({"status": "ok", "preference": preference.as_dict()} if preference else {"status": "not_found"}))
        else:
            update = _read_preference_input(args.input)
            session = auth.valid_session()
            print(json.dumps(PreferenceClient(config).set(session, update)))
        return 0
    except ValueError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 2
    except (AuthError, socket.error):
        print("Authentication failed safely. No token or authorization code was printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
