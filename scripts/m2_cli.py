#!/usr/bin/env python3
"""Owner-scoped M2 RPC and rank client with private-file-only responses."""
from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curator.personalization import AgentAuth, AuthConfig, AuthError, MacOSKeychainStorage  # noqa: E402
from curator.personalization.preferences import JsonRestTransport, ResponseTooLarge  # noqa: E402

MAX_INPUT_BYTES = 16 * 1024
# The private-output ceiling is an operational value, not a security boundary:
# it bounds what one owner command may write to her own disk. A real Phase 2
# rank response measured 77,745 bytes on 2026-09-21, so the old hardcoded 64 KiB
# turned every successful `rank` into an opaque failure that could only be
# raised by editing source. Programmable per user.md's operational-policy rule:
# flag first, then environment, then this default.
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
MIN_MAX_OUTPUT_BYTES = 64 * 1024
MAX_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES_ENV = "NEWS_CURATOR_M2_CLI_MAX_OUTPUT_BYTES"


class OutputTooLarge(ValueError):
    """The response was valid; it did not fit under the configured cap."""


def max_output_bytes(value: int | None, env: Mapping[str, str] | None = None) -> int:
    """The private-output cap: --max-output-bytes, then the environment, then the default.

    Validated HERE so an out-of-range value refuses the command before any
    remote effect, rather than surfacing as a truncated or missing receipt.
    """
    source = os.environ if env is None else env
    if value is None:
        raw = source.get(MAX_OUTPUT_BYTES_ENV, "")
        if raw == "":
            return DEFAULT_MAX_OUTPUT_BYTES
        if not raw.isdigit():
            raise ValueError(f"{MAX_OUTPUT_BYTES_ENV} must be a decimal byte count")
        value = int(raw)
    if not MIN_MAX_OUTPUT_BYTES <= value <= MAX_MAX_OUTPUT_BYTES:
        raise ValueError("max output bytes must be between 65536 and 16777216")
    return value


def _bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("configured origin must be exact HTTPS")
    return value.rstrip("/")


def _input_object(location: str | None) -> dict[str, Any]:
    if not location:
        raise ValueError("command requires --input FILE")
    path = Path(location)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("input file is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input file is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def _export_input(location: str | None) -> dict[str, Any]:
    value = _input_object(location)
    if set(value) != {"p_cursor", "p_expected_fence"}:
        raise ValueError("export input is invalid")
    cursor, fence = value["p_cursor"], value["p_expected_fence"]
    if cursor is not None and (not isinstance(cursor, str) or len(cursor.encode("utf-8")) > 2048):
        raise ValueError("export input is invalid")
    if fence is not None and (not isinstance(fence, str) or len(fence) != 64 or any(char not in "0123456789abcdef" for char in fence)):
        raise ValueError("export input is invalid")
    return value


def _validate_output_path(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute() or ".." in path.parts or path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise ValueError("output path is invalid")
    return path


def _private_output(path_value: str, payload: Any, limit: int = DEFAULT_MAX_OUTPUT_BYTES) -> None:
    path = _validate_output_path(path_value)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > limit:
        # The byte counts are the CLI's own measurements of its own response
        # size. They name nothing about the owner or her data, so they are safe
        # to print, and without them a successful call is indistinguishable
        # from an authentication failure.
        raise OutputTooLarge(f"output too large ({len(raw)} bytes > cap {limit} bytes)")
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".m2-cli-", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        os.unlink(temporary)
        temporary = None
        stored = path.stat()
        if not stat.S_ISREG(stored.st_mode) or stored.st_uid != os.geteuid() or (stored.st_mode & 0o077) != 0:
            raise ValueError("output file privacy check failed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _auth_config() -> AuthConfig:
    return AuthConfig(os.environ.get("NEWS_CURATOR_SUPABASE_URL", ""), os.environ.get("NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY", ""))


def _session(config: AuthConfig, expected_email: str | None, *, minimum_validity: float = 30.0):
    account = urllib.parse.urlsplit(config.supabase_url).hostname or "news-curator"
    session = AgentAuth(config, MacOSKeychainStorage(account=account)).valid_session(
        leeway=minimum_validity)
    if expected_email:
        status, profile = JsonRestTransport().request("GET", f"{config.supabase_url}/auth/v1/user", headers={
            "apikey": config.publishable_key, "authorization": f"Bearer {session.access_token}"})
        if status != 200 or not isinstance(profile, dict) or profile.get("email") != expected_email:
            raise AuthError("The authenticated owner did not match the configured scope.")
    return session


def _headers(config: AuthConfig, session) -> dict[str, str]:
    return {"apikey": config.publishable_key, "authorization": f"Bearer {session.access_token}", "accept": "application/json", "content-type": "application/json"}


_RPC_BUILDERS = {
    "history": ("m2_history_snapshot", lambda args: {"p_limit": args.limit}),
    "consent": ("set_behavior_consent", lambda args: {"p_learning_enabled": args.learning, "p_provider_processing_enabled": args.provider_processing, "p_provider_policy_id": args.provider_policy_id}),
    "clear-history": ("clear_behavior_history", lambda args: {}),
    "export": ("m2_owner_export_page", lambda args: _export_input(args.input)),
    "event": ("append_behavior_event", lambda args: _input_object(args.input)),
    "state-event": ("set_story_state_with_event", lambda args: _input_object(args.input)),
    "interest-event": ("set_story_interest_with_event", lambda args: _input_object(args.input)),
    # Every hourly page stays reviewable after the fact: what she was shown, in
    # order, with each card's pool and its visible label.
    "reading-pages": ("m2_owner_reading_pages", lambda args: {"p_hour_start": args.hour,
                                                              "p_limit": args.limit or 24}),
}
_RPC_NAMES = {item[0] for item in _RPC_BUILDERS.values()}
RPC_TIMEOUT_SECONDS = 15.0
RANK_TIMEOUT_SECONDS = 310.0
MAX_TIMEOUT_SECONDS = 600.0
SESSION_REFRESH_MARGIN_SECONDS = 30.0


def _command_timeout(command: str, value: float | None) -> float:
    timeout = value if value is not None else (
        RANK_TIMEOUT_SECONDS if command in {"rank", "page"} else RPC_TIMEOUT_SECONDS)
    if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout is invalid")
    return timeout


def _session_minimum_validity(command: str, timeout: float) -> float:
    # Rank has two sequential remote legs: the history snapshot and the paid
    # rank request. The remaining commands have one. The margin also covers the
    # optional owner-profile verification performed after refresh.
    remote_legs = 2 if command == "rank" else 1
    return timeout * remote_legs + SESSION_REFRESH_MARGIN_SECONDS


def _rpc(config: AuthConfig, session, name: str, body: Mapping[str, Any], timeout: float,
         max_response_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> Any:
    if name not in _RPC_NAMES:
        raise ValueError("unsupported RPC")
    status, payload = JsonRestTransport(max_response_bytes=max_response_bytes).request("POST", f"{config.supabase_url}/rest/v1/rpc/{name}", headers=_headers(config, session), body=body, timeout=timeout)
    if status != 200:
        raise AuthError("The M2 request was denied or unavailable.")
    return payload


def _ranker_origin(args: argparse.Namespace) -> str:
    return _origin(args.ranker_origin or os.environ.get("NEWS_CURATOR_M2_RANKER_ORIGIN", ""))


def _rank(config: AuthConfig, session, args: argparse.Namespace,
          max_response_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> Any:
    origin = _ranker_origin(args)
    policy = args.policy_version or os.environ.get("NEWS_CURATOR_M2_POLICY_VERSION", "")
    model = args.model_version or os.environ.get("NEWS_CURATOR_M2_MODEL_VERSION", "")
    if not all(isinstance(value, str) and 0 < len(value) <= 256 for value in (policy, model)):
        raise ValueError("rank policy and model versions are required")
    if not 1 <= args.page_size <= 50 or (args.category is not None and len(args.category) > 80) or (args.query is not None and len(args.query) > 600):
        raise ValueError("rank input is invalid")
    history = _rpc(config, session, "m2_history_snapshot", {"p_limit": None}, args.timeout,
                   max_response_bytes)
    required = ("included_history_revision", "history_revision", "history_generation", "consent_revision")
    if not isinstance(history, dict) or any(isinstance(history.get(field), bool) or not isinstance(history.get(field), int) or history[field] < 0 for field in required):
        raise AuthError("The M2 history response was invalid.")
    body = {"schema_version": 1, "policy_version": policy, "model_version": model,
            "history_revision": history["included_history_revision"], "server_commit_revision": history["history_revision"],
            "history_generation": history["history_generation"], "consent_revision": history["consent_revision"],
            "page_size": args.page_size, "eligibility": {"category": args.category, "query": args.query},
            "exclude_story_ids": args.exclude_story_id}
    status, payload = JsonRestTransport(max_response_bytes=max_response_bytes).request("POST", f"{origin}/rank", headers={"authorization": f"Bearer {session.access_token}", "accept": "application/json", "content-type": "application/json"}, body=body, timeout=args.timeout)
    if status != 200:
        raise AuthError("The M2 rank request was denied or unavailable.")
    return {"history": history, "ranking": payload}


def _page(config: AuthConfig, session, args: argparse.Namespace,
          max_response_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> Any:
    cursor = _input_object(args.input).get("cursor")
    if not isinstance(cursor, str) or not cursor or len(cursor.encode("utf-8")) > 4096:
        raise ValueError("page input is invalid")
    query = urllib.parse.urlencode({"cursor": cursor})
    status, payload = JsonRestTransport(max_response_bytes=max_response_bytes).request("GET", f"{_ranker_origin(args)}/page?{query}", headers={"authorization": f"Bearer {session.access_token}", "accept": "application/json"}, timeout=args.timeout)
    if status != 200:
        raise AuthError("The M2 page request was denied or unavailable.")
    return payload


def _hour(value: str) -> str:
    """An ISO-8601 instant naming the hour to review. The RPC truncates it."""
    from datetime import datetime
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("hour must be an ISO-8601 timestamp") from exc
    return value


def _validate_args(args: argparse.Namespace) -> None:
    bounded = (args.provider_policy_id, args.category, args.query, args.expected_owner_email)
    if any(value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > 1024) for value in bounded):
        raise ValueError("input is invalid")
    if args.limit is not None and not 1 <= args.limit <= 100:
        raise ValueError("history limit is invalid")
    if len(args.exclude_story_id) > 50 or any(not isinstance(value, str) or not value or len(value) > 256 for value in args.exclude_story_id):
        raise ValueError("excluded story IDs are invalid")
    if args.command == "reading-pages" and not args.hour:
        raise ValueError("reading-pages requires --hour")
    if args.command == "consent" and (args.learning is None or args.provider_processing is None):
        raise ValueError("consent requires both Boolean flags")
    if args.command == "consent" and args.learning is False and args.provider_processing is True:
        raise ValueError("provider processing requires learning")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(*_RPC_BUILDERS, "rank", "page"))
    parser.add_argument("--output", required=True, help="New absolute private JSON output path.")
    parser.add_argument("--expected-owner-email", help="Optional expected owner email for scoped E2E use.")
    parser.add_argument("--input", help="Bounded JSON input file for export, page, and mutation commands.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--learning", type=_bool)
    parser.add_argument("--provider-processing", type=_bool)
    parser.add_argument("--provider-policy-id")
    parser.add_argument("--ranker-origin")
    parser.add_argument("--policy-version")
    parser.add_argument("--model-version")
    parser.add_argument("--page-size", type=int, default=25)
    parser.add_argument("--category")
    parser.add_argument("--query")
    parser.add_argument("--exclude-story-id", action="append", default=[])
    parser.add_argument("--hour", type=_hour, help="ISO-8601 instant naming the hour of pages to review.")
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--max-output-bytes", type=int, default=None,
                        help=f"Private output cap in bytes (65536..16777216). Falls back to ${MAX_OUTPUT_BYTES_ENV}, then 1 MiB.")
    return parser


def _failure_class(error: BaseException) -> str:
    """Name WHY the command failed, without naming anything the owner owns.

    Before this, every failure printed one identical line, so a 78 KB success
    that overflowed the cap looked exactly like a denied token. The label is
    derived from the exception type only; the message is included solely for
    OutputTooLarge, whose text is the CLI's own byte counts.
    """
    if isinstance(error, OutputTooLarge):
        return str(error)
    if isinstance(error, ResponseTooLarge):
        return f"response exceeded cap {error.limit} bytes"
    if isinstance(error, AuthError):
        # The shared transport turns an unreachable endpoint into AuthError too,
        # so this label names both rather than overclaiming a denied token.
        return "auth or service unavailable"
    # socket.error IS OSError in Python 3, so a network class has to name the
    # network subclasses; everything else OSError is a local file or env fault.
    if isinstance(error, (TimeoutError, ConnectionError, socket.gaierror, socket.herror)):
        return "network"
    if isinstance(error, ValueError):
        return "invalid input"
    return "local file or environment error"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.timeout = _command_timeout(args.command, args.timeout)
        limit = max_output_bytes(args.max_output_bytes)
        _validate_args(args)
        _validate_output_path(args.output)  # Refuse an unsafe output before any remote effect.
        config = _auth_config()
        session = _session(
            config,
            args.expected_owner_email,
            minimum_validity=_session_minimum_validity(args.command, args.timeout),
        )
        if args.command == "rank":
            payload = _rank(config, session, args, limit)
        elif args.command == "page":
            payload = _page(config, session, args, limit)
        else:
            name, build = _RPC_BUILDERS[args.command]
            payload = _rpc(config, session, name, build(args), args.timeout, limit)
        _private_output(args.output, payload, limit)
        print(f"M2 {args.command} completed. Private output written.")
        return 0
    except (AuthError, ValueError, socket.error, OSError) as error:
        print(f"M2 request failed safely ({_failure_class(error)}). "
              "No credential or private response was printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
