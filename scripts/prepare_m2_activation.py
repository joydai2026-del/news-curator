#!/usr/bin/env python3
"""Prepare bounded M2 deployment artifacts. No migration, Modal, or GitHub writes."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_REF = re.compile(r"^[a-z0-9]{20}$")
OWNER_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
PUBLISHABLE_KEY = re.compile(r"^sb_publishable_[A-Za-z0-9_-]+$")
SECRET_KEY = re.compile(r"^sb_secret_[A-Za-z0-9_-]+$")
MANAGEMENT_ORIGIN = "https://api.supabase.com"
MAX_PRIVATE_INPUT_BYTES = 64 * 1024
REQUIRED_SECRET_FIELDS = (
    "NEWS_CURATOR_MODEL_API_KEY", "NEWS_CURATOR_SUPABASE_URL",
    "NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY", "NEWS_CURATOR_SUPABASE_SERVICE_ROLE_KEY",
    "NEWS_CURATOR_CURSOR_SIGNING_KEY", "NEWS_CURATOR_TENANT_ID",
    "NEWS_CURATOR_PREVIEW_OWNER_IDS", "NEWS_CURATOR_READER_ORIGIN",
)
# The prompt template is OPTIONAL: the ranker policy is the source of truth in
# production, and the service now ignores this key outside smoke mode. Carrying
# it as REQUIRED is what put a Phase 1 path into the production secret and made
# every /rank fall back with provider_preparation_failed once the image stopped
# staging that file. When it IS given, it must name a file that exists in this
# checkout, so a path that cannot resolve is refused here instead of at boot.
OPTIONAL_SECRET_FIELDS = ("NEWS_CURATOR_RANKLLM_TEMPLATE",)
IMAGE_ROOT = "/opt/news-curator/"
REPO_ROOT = Path(__file__).resolve().parents[1]


def image_template_repo_path(value: str, root: Path | None = None) -> Path:
    """The checkout file an image template path names. Raises when it is absent."""
    if not value.startswith(IMAGE_ROOT + "config/") or ".." in Path(value).parts:
        raise ValueError("invalid image template path")
    path = (root or REPO_ROOT) / value[len(IMAGE_ROOT):]
    if not path.is_file():
        raise ValueError(f"template {value} is not present in this checkout at {path}")
    return path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep a management bearer token on the fixed Supabase API origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_text(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_size > MAX_PRIVATE_INPUT_BYTES):
            raise ValueError("invalid private input")
        data = os.read(descriptor, MAX_PRIVATE_INPUT_BYTES + 1)
        if len(data) > MAX_PRIVATE_INPUT_BYTES:
            raise ValueError("invalid private input")
        return data.decode("utf-8")
    finally:
        os.close(descriptor)


def private_json(path: Path) -> dict:
    value = json.loads(private_text(path))
    if not isinstance(value, dict):
        raise ValueError("private binding must be an object")
    return value


def binding(path: Path, expected_project_ref: str) -> tuple[str, str]:
    value = private_json(path)
    ref, owners = value.get("project_ref"), value.get("owners")
    if (not isinstance(expected_project_ref, str) or not PROJECT_REF.fullmatch(expected_project_ref)
            or not isinstance(ref, str) or not PROJECT_REF.fullmatch(ref)
            or ref != expected_project_ref):
        raise ValueError("invalid project reference")
    if not isinstance(owners, list) or len(owners) != 1 or not isinstance(owners[0], dict):
        raise ValueError("activation requires exactly one owner")
    owner = owners[0].get("owner_id")
    if not isinstance(owner, str) or not OWNER_ID.fullmatch(owner):
        raise ValueError("invalid private owner identifier")
    return ref, owner


def token(helper: Path) -> str:
    spec = importlib.util.spec_from_file_location("m2_supabase_management", helper)
    if spec is None or spec.loader is None:
        raise ValueError("management token helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = module.token()
    if not isinstance(value, str) or not value:
        raise ValueError("management token unavailable")
    return value


def request_json(ref: str, access: str, path: str, *, payload: dict | None = None) -> object:
    if not PROJECT_REF.fullmatch(ref) or not path.startswith("/"):
        raise ValueError("invalid management request")
    url = f"{MANAGEMENT_ORIGIN}/v1/projects/{ref}{path}"
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme, parsed.netloc) != ("https", "api.supabase.com") or not parsed.path.startswith(f"/v1/projects/{ref}/"):
        raise ValueError("invalid management origin")
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": "Bearer " + access, "Content-Type": "application/json", "User-Agent": "SupabaseCLI/2.75.0"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
        return json.load(response)


def query(ref: str, access: str, statement: str) -> list[dict]:
    rows = request_json(ref, access, "/database/query", payload={"query": statement, "read_only": True})
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("read-only query returned invalid rows")
    return rows


def secure_write(path: Path, value: object) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError("output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("private output mode changed")


def dotenv_value(path: Path, name: str) -> str:
    found: str | None = None
    for line in private_text(path).splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key == name:
            if found is not None or not value:
                raise ValueError("model key missing or duplicated")
            found = value
    if found is None:
        raise ValueError("model key missing")
    return found


def api_keys(ref: str, access: str, service_key_name: str) -> tuple[str, str]:
    rows = request_json(ref, access, "/api-keys?reveal=true")
    if not isinstance(rows, list):
        raise ValueError("API key response invalid")
    publishable = [row.get("api_key") for row in rows if isinstance(row, dict) and row.get("type") == "publishable" and row.get("name") == "default"]
    service = [row.get("api_key") for row in rows if isinstance(row, dict) and row.get("type") == "secret" and row.get("name") == service_key_name]
    if (len(publishable) != 1 or len(service) != 1
            or not isinstance(publishable[0], str) or not PUBLISHABLE_KEY.fullmatch(publishable[0])
            or not isinstance(service[0], str) or not SECRET_KEY.fullmatch(service[0])):
        raise ValueError("required scoped API keys unavailable")
    return publishable[0], service[0]


def baseline(args: argparse.Namespace) -> None:
    ref, _ = binding(args.binding, args.expected_project_ref)
    access = token(args.management_token_helper)
    migrations = query(ref, access, "select version::text,name from supabase_migrations.schema_migrations order by version")
    feed_policy = query(ref, access, "select count(*)::int as rows, md5(coalesce(string_agg(to_jsonb(p)::text,'' order by to_jsonb(p)::text),'')) as fingerprint from public.feed_policy p")
    table_counts = query(ref, access, "select (select count(*)::bigint from public.user_preferences) as user_preferences, (select count(*)::bigint from public.user_story_state) as user_story_state, (select count(*)::bigint from public.user_story_interests) as user_story_interests")
    acl = query(ref, access, "with targets(signature) as (values ('public.set_story_state(text,boolean,boolean,bigint,text)'),('public.set_story_interest(text,text,text,bigint,text)')), resolved as (select signature,to_regprocedure(signature) as function_oid from targets) select resolved.signature,resolved.function_oid::text as function_oid,case when proc.proacl is null then '<null>' else proc.proacl::text end as privileges from resolved left join pg_proc proc on proc.oid=resolved.function_oid order by resolved.signature")
    retained_presence = query(ref, access, "select to_regclass('public.retained_corpus_observations') is not null as table_exists")
    if len(retained_presence) != 1 or type(retained_presence[0].get("table_exists")) is not bool:
        raise ValueError("retained corpus presence response invalid")
    retained = (query(ref, access, "select count(*)::bigint as observations, max(last_ingested_at)::text as latest_ingested_at, max(last_ready_at)::text as latest_ready_at from public.retained_corpus_observations")
                if retained_presence[0]["table_exists"] else [])
    secure_write(args.output, {"environment":"production read-only management queries", "production_write":False, "project_ref_sha256":hashlib.sha256(ref.encode()).hexdigest(), "migrations":migrations, "feed_policy":feed_policy, "m1_table_counts":table_counts, "m1_function_acls":acl, "retained_ingestion":{"table_exists":retained_presence[0]["table_exists"],"aggregate":retained}})
    print(json.dumps({"stage":"baseline","output":str(args.output),"migration_count":len(migrations),"retained_table_exists":retained_presence[0]["table_exists"],"retained_observation_rows":retained[0].get("observations") if len(retained)==1 else None}, sort_keys=True))


def stage_secret(args: argparse.Namespace) -> None:
    ref, owner = binding(args.binding, args.expected_project_ref)
    access = token(args.management_token_helper)
    publishable, service = api_keys(ref, access, args.service_key_name)
    model = dotenv_value(args.model_env, "NEWS_CURATOR_MODEL_API_KEY")
    if not args.reader_origin.startswith("https://"):
        raise ValueError("invalid reader origin")
    if args.template is not None:
        image_template_repo_path(args.template)
    secret = {
        "NEWS_CURATOR_MODEL_API_KEY": model,
        "NEWS_CURATOR_SUPABASE_URL": f"https://{ref}.supabase.co",
        "NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY": publishable,
        "NEWS_CURATOR_SUPABASE_SERVICE_ROLE_KEY": service,
        "NEWS_CURATOR_CURSOR_SIGNING_KEY": base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode(),
        "NEWS_CURATOR_TENANT_ID": ref,
        "NEWS_CURATOR_PREVIEW_OWNER_IDS": json.dumps([owner], separators=(",", ":")),
        "NEWS_CURATOR_READER_ORIGIN": args.reader_origin,
    }
    if args.template is not None:
        secret["NEWS_CURATOR_RANKLLM_TEMPLATE"] = args.template
    expected = set(REQUIRED_SECRET_FIELDS) | ({OPTIONAL_SECRET_FIELDS[0]} if args.template is not None else set())
    if set(secret) != expected or len(base64.urlsafe_b64decode(secret["NEWS_CURATOR_CURSOR_SIGNING_KEY"] + "=")) < 32:
        raise RuntimeError("secret payload invariant failed")
    secure_write(args.output, secret)
    print(json.dumps({"stage":"stage-secret","output":str(args.output),"field_names":sorted(secret),"owner_allowlist_count":1,"cursor_random_bytes":32}, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--expected-project-ref", required=True)
    parser.add_argument("--management-token-helper", type=Path, required=True)
    commands = parser.add_subparsers(dest="stage", required=True)
    baseline_parser = commands.add_parser("baseline")
    baseline_parser.add_argument("--output", type=Path, required=True)
    secret_parser = commands.add_parser("stage-secret")
    secret_parser.add_argument("--model-env", type=Path, required=True)
    secret_parser.add_argument("--reader-origin", required=True)
    secret_parser.add_argument("--template", default=None,
        help="Optional image path for the smoke-mode prompt template. Must exist in this checkout.")
    secret_parser.add_argument("--service-key-name", default="news_curator_github")
    secret_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.stage == "baseline": baseline(args)
        else: stage_secret(args)
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, urllib.error.URLError) as error:
        print(f"activation preparation failed: {type(error).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
