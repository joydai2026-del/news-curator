"""Validated composition root shared by local ASGI tests and Modal."""

from __future__ import annotations

import os
import json
import hashlib
from pathlib import Path

import httpx
import yaml

from .asgi import RankingASGI
from .composition import RETENTION_INPUTS_FILE, boot_retention_days, load_composition_policy
from .engine import OpenAIRankLLMEngine, ReviewedRankLLMPromptBuilder, ScoringPolicy
from .rankllm_adapter import RankLLMAdapter, RankerPolicy
from .service import RankingService, ServicePolicy
from .supabase_http import DEFAULT_TIMEOUT_SECONDS, SupabaseHTTP, validate_timeout_seconds


RANKER_POLICY_DEFAULT = "config/ranker-policy-r1.yaml"


def load_ranker_policy(env, policy_path: str | Path | None = None, *, root: Path | None = None):
    """The ranker policy and the path it was read from.

    Service mode and smoke mode both come through here, so neither can drift
    onto a config filename of its own. `root` anchors a relative path for a
    caller that cannot rely on the working directory (the Modal image).
    """
    path = Path(policy_path or env.get("NEWS_CURATOR_RANKER_POLICY") or RANKER_POLICY_DEFAULT)
    if root is not None and not path.is_absolute():
        path = Path(root) / path
    policy = yaml.safe_load(path.read_text())
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ValueError("invalid ranker policy")
    return path, policy


def policy_reference(policy_path: Path, value: str | Path) -> Path:
    """Resolve a `config/...` path the policy names, relative to the policy itself.

    The values are written from the tree root (`config/<name>`) and the policy
    lives at `<root>/config/<name>`, so the root is the policy's grandparent.
    Resolving against it instead of the process working directory means a
    checkout and an image read the same file. An absolute override is honored.
    """
    path = Path(value)
    return path if path.is_absolute() else policy_path.parent.parent / path


def configured_token_counter(policy, env):
    """Load only the reviewed, locally cached encoding. Never download at startup."""
    encoding_name = _required(policy, "tokenizer_encoding")
    # The encoding data is an adapter dependency, pinned alongside its wheel.
    reviewed = {"o200k_base": (
        "fb374d419588a4632f3f557e76b4b70aebbca790",
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d")}
    if encoding_name not in reviewed:
        raise ValueError("unreviewed tokenizer encoding")
    filename, expected_hash = reviewed[encoding_name]
    cache_dir = _required(env, "TIKTOKEN_CACHE_DIR")
    cache_file = Path(cache_dir) / filename
    if not cache_file.is_file() or hashlib.sha256(cache_file.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("reviewed tokenizer cache missing or changed")
    # tiktoken reads this process-level path. Refuse a divergent injected path
    # rather than changing global state across application instances.
    if os.environ.get("TIKTOKEN_CACHE_DIR") != cache_dir:
        raise ValueError("tokenizer cache environment mismatch")
    import tiktoken
    try:
        if tiktoken.encoding_name_for_model(_required(policy, "model")) != encoding_name:
            raise ValueError("model tokenizer mismatch")
    except KeyError as error:
        raise ValueError("unknown model tokenizer") from error
    encoding = tiktoken.get_encoding(encoding_name)
    return lambda value: len(encoding.encode(value, disallowed_special=()))


def build_application(*, environ=None, policy_path: str | None = None):
    env = os.environ if environ is None else environ
    path, policy = load_ranker_policy(env, policy_path)
    enabled = policy.get("service_enabled") is True
    reader_origin = _required(env, "NEWS_CURATOR_READER_ORIGIN")
    supabase_origin = _required(env, "NEWS_CURATOR_SUPABASE_URL")
    publishable = _required(env, "NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY")
    service_key = _required(env, "NEWS_CURATOR_SUPABASE_SERVICE_ROLE_KEY")
    cursor_key = _required(env, "NEWS_CURATOR_CURSOR_SIGNING_KEY").encode()
    tenant_id = _required(env, "NEWS_CURATOR_TENANT_ID")
    preview_ids = preview_owner_allowlist(env, enabled=enabled)
    provider_key = env.get("NEWS_CURATOR_MODEL_API_KEY", "")
    if enabled and not provider_key:
        raise ValueError("enabled ranker requires a scoped model key")
    ranker_policy = RankerPolicy(provider_id=_required(policy, "provider"), model_id=_required(policy, "model"),
        endpoint=_required(policy, "endpoint"), prompt_revision=_required(policy, "prompt_revision"),
        deadline_seconds=policy["deadline_seconds"], max_retries=policy["max_retries"],
        request_cost_limit_usd=policy["request_cost_limit_usd"], daily_cost_limit_usd=policy["daily_cost_limit_usd"],
        input_cost_per_million_tokens_usd=policy.get("input_cost_per_million_tokens_usd"),
        output_cost_per_million_tokens_usd=policy.get("output_cost_per_million_tokens_usd"))
    # The composition policy is loaded FIRST: it decides whether the ranker asks
    # for action predictions or for a bare permutation, which changes the schema,
    # the prompt and the output budget together.
    composition_path = env.get("NEWS_CURATOR_COMPOSITION_POLICY") or policy.get("composition_policy")
    # Cross-file check at boot: the corpus retention window (sources.yaml) must
    # still cover the window the feed reads back (this policy).
    composition = load_composition_policy(
        policy_reference(path, composition_path),
        # Local checkout first, then the value staged into the image. Missing
        # from BOTH is a refusal, never a skipped check.
        retention_days=boot_retention_days(env.get("NEWS_CURATOR_SOURCES", "sources.yaml"),
                                           policy_reference(path, RETENTION_INPUTS_FILE)),
        # The claim window is validated against the call it protects.
        provider_deadline_seconds=policy["deadline_seconds"],
        settle_window_seconds=policy.get("settle_window_seconds", 5),
    ) if composition_path else None
    scoring = ScoringPolicy.from_composition(composition) if composition else None
    prompt = ReviewedRankLLMPromptBuilder(str(policy_reference(path,
        env.get("NEWS_CURATOR_RANKLLM_TEMPLATE") or _required(policy, "prompt_template"))))
    engine = OpenAIRankLLMEngine(prompt_builder=prompt, endpoint=ranker_policy.endpoint, api_key=provider_key or "disabled",
        model=ranker_policy.model_id, maximum_output_tokens=policy["maximum_output_tokens"],
        reasoning_token_allowance=policy["reasoning_token_allowance"],
        prompt_framing_token_allowance=policy["prompt_framing_token_allowance"],
        prompt_framing_tokens_per_message=policy["prompt_framing_tokens_per_message"],
        token_counter=configured_token_counter(policy, env),
        reasoning_effort=policy["reasoning_effort"], verbosity=policy["verbosity"],
        scoring=scoring,
        client_factory=lambda: httpx.AsyncClient(timeout=None, follow_redirects=False))
    adapter = RankLLMAdapter(policy=ranker_policy, engine=engine)
    transport = SupabaseHTTP(origin=supabase_origin, publishable_key=publishable, service_role_key=service_key,
        timeout_seconds=supabase_timeout_seconds(policy))
    service_policy = ServicePolicy(policy_version=_required(policy, "prompt_revision"),
        model_version=_required(policy, "model"), provider_policy_id=_required(policy, "prompt_revision"),
        tenant_id=tenant_id, candidate_limit=policy["candidate_limit"], maximum_page_size=policy["maximum_page_size"],
        maximum_excluded_story_ids=policy["maximum_excluded_story_ids"],
        cursor_ttl_seconds=policy["cursor_ttl_seconds"], daily_cost_limit_usd=policy["daily_cost_limit_usd"],
        preview_owner_ids=tuple(preview_ids), enabled=enabled,
        display_language=policy.get("display_language", "en"),
        exclusive_category_id=policy.get("exclusive_category_id", ""),
        other_lane_enabled=policy.get("other_lane_enabled", True),
        # Required, not defaulted: an empty value used to become a silent
        # fallback, which made ServicePolicy's boot validation unreachable.
        exclusivity_policy_id=_required(policy, "exclusivity_policy_id"),
        # The feed recipe. Loaded and VALIDATED at startup, so an out-of-range
        # composition value fails the boot rather than silently changing the mix.
        # Unsetting composition_policy is the documented rollback to the
        # pre-Phase-2 window; it is a config change, not a revert.
        composition=composition)
    service = RankingService(auth=transport, store=transport, adapter=adapter, policy=service_policy,
        cursor_key=cursor_key)
    return RankingASGI(service=service, reader_origin=reader_origin,
        maximum_body_bytes=policy["maximum_request_body_bytes"])


def supabase_timeout_seconds(policy) -> float:
    """`supabase.timeout_seconds` from the ranker policy, validated at boot.

    A per-call budget is an operational value, so it belongs in the policy file
    rather than in the transport's signature: the heavy Phase 2 candidate query
    grows with the corpus, and raising the ceiling must not require a code
    change. Validated HERE, at startup, so a bad value refuses the boot instead
    of surfacing as an opaque 503 on the first request.
    """
    section = policy.get("supabase", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("ranker policy `supabase` must be a mapping")
    unknown = set(section) - {"timeout_seconds"}
    if unknown:
        raise ValueError(f"unknown ranker policy supabase keys: {sorted(unknown)}")
    return validate_timeout_seconds(section.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))


def preview_owner_allowlist(env, *, enabled: bool) -> tuple[str, ...]:
    """Parse NEWS_CURATOR_PREVIEW_OWNER_IDS, failing CLOSED when it is empty.

    ``RankingService`` only applies the gate when the tuple is non-empty
    (``service.py`` ``if self._policy.preview_owner_ids and ...``), so an unset,
    blank or ``[]`` variable used to mean "everybody is an owner" on an enabled
    service: the exact opposite of what an allowlist is for. An empty allowlist
    now means nobody, and an enabled service refuses to boot without one rather
    than serving the paid path to every signed-in user.
    """
    raw = env.get("NEWS_CURATOR_PREVIEW_OWNER_IDS", "")
    parsed = json.loads(raw) if raw.strip() else []
    if not isinstance(parsed, list) or any(
            not isinstance(value, str) or not value.strip() for value in parsed):
        raise ValueError("invalid preview owner allowlist")
    owners = tuple(parsed)
    if enabled and not owners:
        raise ValueError("enabled ranker requires a non-empty preview owner allowlist")
    return owners


def _required(values, key):
    value = values.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{key} must be configured")
    return value
