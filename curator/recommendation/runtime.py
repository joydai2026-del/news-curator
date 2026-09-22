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
from .deployment import FUNCTION_TIMEOUT_ENV, function_timeout_seconds
from .engine import OpenAIRankLLMEngine, ReviewedRankLLMPromptBuilder, ScoringPolicy
from .rankllm_adapter import RankLLMAdapter, RankerPolicy
from .service import CLAIMED_SECTION_MAX_TRANSPORT_CALLS, RankingService, ServicePolicy
from .supabase_http import SupabaseHTTP, validate_timeout_retries, validate_timeout_seconds


# Authentication, the initial history snapshot, run and view opening all happen
# before the ranking claim. A privacy-epoch boundary may add one precisely scoped
# run close and one replacement open. They consume the Modal function's wall
# clock and therefore belong in the full-request budget, but not in the claim TTL.
PRECLAIM_TRANSPORT_CALLS = 6
# A continuation reads state once while composing older cards and again while
# overlaying the visible page. The ranking claim only contains one such read.
MAX_OWNER_STATE_READS_PER_REQUEST = 2


RANKER_POLICY_DEFAULT = "config/ranker-policy-r1.yaml"
PROMPT_TEMPLATE_ENV = "NEWS_CURATOR_RANKLLM_TEMPLATE"
PROMPT_TEMPLATE_OVERRIDE_ENV = "NEWS_CURATOR_ALLOW_TEMPLATE_OVERRIDE"


def resolve_prompt_template(env, policy, policy_path: Path) -> tuple[Path, str]:
    """The prompt template the ranker boots with, and where it came from.

    In production the POLICY is the source of truth. The environment override
    exists for smoke runs, which boot the image without the service secret and
    need to point at a template of their own. A production secret written in an
    earlier phase still carries a stale `NEWS_CURATOR_RANKLLM_TEMPLATE`, and
    while that env value silently won, every `/rank` fell back with
    `provider_preparation_failed` because the named file was no longer in the
    image. So the override is honoured ONLY in smoke mode or when explicitly
    allowed, and in every mode a template that does not exist refuses the boot
    naming the path and its source, instead of failing per request later.
    """
    configured = _required(policy, "prompt_template")
    override = (env.get(PROMPT_TEMPLATE_ENV) or "").strip()
    allowed = (env.get("NEWS_CURATOR_MODAL_MODE") == "smoke"
               or env.get(PROMPT_TEMPLATE_OVERRIDE_ENV) == "true")
    if override and not allowed:
        # One structured line, on stdout, so the ignored override is visible in
        # the deployment log rather than being a silent difference between the
        # secret and the running service.
        print(json.dumps({"event": "prompt_template_override_ignored",
                          "reason": "policy is the source of truth outside smoke mode",
                          "ignored_env": PROMPT_TEMPLATE_ENV, "ignored_value": override,
                          "using_policy_value": configured,
                          "allow_with": PROMPT_TEMPLATE_OVERRIDE_ENV},
                         sort_keys=True), flush=True)
        override = ""
    source = "env" if override else "policy"
    value = override or configured
    path = policy_reference(policy_path, value)
    if not path.is_file():
        raise ValueError(f"prompt template {path} (from {source}: {value}) does not exist")
    return path, source


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


def effective_ranking_policy_digest(policy: dict, composition_path: Path | None,
                                    prompt_path: Path) -> str:
    """Bind a reading-run view to every input that can change its ranking.

    Public policy/model fields stay human-readable on the wire. This internal
    digest additionally covers the complete validated configuration, prompt
    bytes, and ranking implementation, so a code-only deploy cannot inherit an
    older deploy's frozen view.
    """
    implementation = {}
    curator_root = Path(__file__).resolve().parents[1]
    for source in sorted(curator_root.rglob("*.py")):
        implementation[source.relative_to(curator_root.parent).as_posix()] = \
            hashlib.sha256(source.read_bytes()).hexdigest()
    composition_document = None
    if composition_path is not None:
        composition_document = yaml.safe_load(composition_path.read_text(encoding="utf-8"))
    material = {
        "schema_version": 1,
        "ranker_policy": policy,
        "composition_policy": composition_document,
        "prompt_template_sha256": hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        "implementation": implementation,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


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
    exclusive_scan_max_batches, exclusive_continuation_max_batches = \
        exclusive_scan_limits(policy)
    # Lowering the exclusive scan is allowed to shorten that view, but it must
    # never under-size the independent general paid path measured by the claim
    # budget harness. Raising it still grows both claim and function budgets.
    claimed_section_transport_calls = claimed_transport_call_budget(policy)
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
    history_events, model_candidates = prompt_budget(policy)
    ranker_policy = RankerPolicy(provider_id=_required(policy, "provider"), model_id=_required(policy, "model"),
        endpoint=_required(policy, "endpoint"), prompt_revision=_required(policy, "prompt_revision"),
        deadline_seconds=policy["deadline_seconds"], max_retries=policy["max_retries"],
        request_cost_limit_usd=policy["request_cost_limit_usd"], daily_cost_limit_usd=policy["daily_cost_limit_usd"],
        input_cost_per_million_tokens_usd=policy.get("input_cost_per_million_tokens_usd"),
        output_cost_per_million_tokens_usd=policy.get("output_cost_per_million_tokens_usd"),
        max_history_events=history_events, max_model_candidates=model_candidates)
    # Read before the composition policy, because the claim-window check inside
    # it is sized against this value.
    supabase_timeout = supabase_timeout_seconds(policy)
    supabase_retries = supabase_timeout_retries(policy)
    effective_claimed_transport_calls = claimed_section_transport_calls + supabase_retries
    effective_full_request_transport_calls = full_request_transport_call_budget(policy)
    # And before anything is served: one request must be able to finish inside
    # the container's own wall clock. See assert_request_fits_function_timeout.
    assert_request_fits_function_timeout(policy, supabase_timeout, env,
        effective_full_request_transport_calls)
    # The composition policy is loaded FIRST: it decides whether the ranker asks
    # for action predictions or for a bare permutation, which changes the schema,
    # the prompt and the output budget together.
    composition_reference = env.get("NEWS_CURATOR_COMPOSITION_POLICY") or policy.get("composition_policy")
    composition_path = policy_reference(path, composition_reference) if composition_reference else None
    # Cross-file check at boot: the corpus retention window (sources.yaml) must
    # still cover the window the feed reads back (this policy).
    composition = load_composition_policy(
        composition_path,
        # Local checkout first, then the value staged into the image. Missing
        # from BOTH is a refusal, never a skipped check.
        retention_days=boot_retention_days(env.get("NEWS_CURATOR_SOURCES", "sources.yaml"),
                                           policy_reference(path, RETENTION_INPUTS_FILE)),
        # The claim window is validated against everything it protects: the
        # provider call AND the claimed section's Supabase round trips, which
        # are bounded by the per-call timeout this policy now sets.
        provider_deadline_seconds=policy["deadline_seconds"],
        settle_window_seconds=policy.get("settle_window_seconds", 5),
        supabase_timeout_seconds=supabase_timeout,
        claimed_section_transport_calls=effective_claimed_transport_calls,
    ) if composition_path else None
    scoring = ScoringPolicy.from_composition(composition) if composition else None
    prompt_path, _ = resolve_prompt_template(env, policy, path)
    prompt = ReviewedRankLLMPromptBuilder(str(prompt_path))
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
        timeout_seconds=supabase_timeout, timeout_retries=supabase_retries)
    service_policy = ServicePolicy(policy_version=_required(policy, "prompt_revision"),
        model_version=_required(policy, "model"), provider_policy_id=_required(policy, "prompt_revision"),
        tenant_id=tenant_id, candidate_limit=policy["candidate_limit"], maximum_page_size=policy["maximum_page_size"],
        maximum_excluded_story_ids=policy["maximum_excluded_story_ids"],
        exclusive_scan_max_batches=exclusive_scan_max_batches,
        exclusive_continuation_max_batches=exclusive_continuation_max_batches,
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
        composition=composition,
        effective_policy_digest=effective_ranking_policy_digest(
            policy, composition_path, prompt_path))
    service = RankingService(auth=transport, store=transport, adapter=adapter, policy=service_policy,
        cursor_key=cursor_key)
    return RankingASGI(service=service, reader_origin=reader_origin,
        maximum_body_bytes=policy["maximum_request_body_bytes"])


def assert_request_fits_function_timeout(policy, supabase_timeout: float, env,
                                         full_request_transport_calls: int | None = None) -> float:
    """Refuse the boot when one request may outlive the container that serves it.

    The service has a typed 200 answer for every way a provider call can go
    wrong, including `provider_deadline`. None of it reaches the reader if the
    platform kills the function first: on 2026-09-21 a POST /rank was killed at
    15.2s and answered 500, because the function timeout was 15 while the
    request was allowed to spend longer than that.

    Every term is named, and every one of them is config:

        provider deadline (`deadline_seconds`)
      + settle window (`settle_window_seconds`)
      + full-request Supabase budget
        ((pre-claim + claimed-section calls + owner-state retry attempts)
         x `supabase.timeout_seconds`)
      MUST be strictly below the function timeout (NEWS_CURATOR_MODAL_FUNCTION_TIMEOUT_SECONDS)

    Raising any of the three means raising the function timeout with them, which
    is a deploy-time environment change, never a code change.
    """
    if full_request_transport_calls is None:
        full_request_transport_calls = full_request_transport_call_budget(policy)
    deadline = float(policy["deadline_seconds"])
    settle = float(policy.get("settle_window_seconds", 5))
    transport = float(supabase_timeout) * full_request_transport_calls
    worst_case = deadline + settle + transport
    timeout = function_timeout_seconds(env)
    if worst_case >= timeout:
        raise ValueError(
            f"one request may take up to {worst_case}s (deadline_seconds {deadline} "
            f"+ settle_window_seconds {settle} + {full_request_transport_calls} "
            f"full-request Supabase calls x supabase.timeout_seconds {supabase_timeout} "
            f"= {transport}), which is not below the function timeout "
            f"{FUNCTION_TIMEOUT_ENV} ({timeout}s)")
    return worst_case


def exclusive_scan_limits(policy) -> tuple[int, int]:
    """Validated operational limits for initial and continuation scans."""
    values = []
    for key, maximum in (("exclusive_scan_max_batches", 50),
                         ("exclusive_continuation_max_batches", 10)):
        value = policy.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError(f"{key} must be an integer between 1 and {maximum}")
        values.append(value)
    return values[0], values[1]


def claimed_transport_call_budget(policy) -> int:
    """Call bound shared by claim-window and function-timeout validation."""
    initial_batches, _ = exclusive_scan_limits(policy)
    return max(CLAIMED_SECTION_MAX_TRANSPORT_CALLS, initial_batches + 10)


def full_request_transport_call_budget(policy) -> int:
    """Base request calls plus retries for both continuation owner-state reads."""
    return (claimed_transport_call_budget(policy) + PRECLAIM_TRANSPORT_CALLS
            + MAX_OWNER_STATE_READS_PER_REQUEST * supabase_timeout_retries(policy))


def prompt_budget(policy) -> tuple[int, int]:
    """`prompt.max_history_events` and `prompt.max_model_candidates`, validated here.

    How much of a request the model is shown is an operational value: it decides
    the token bound, and through it whether a call finishes inside the deadline
    at all. It lives in the policy with a safe default, and an unknown key or an
    out-of-range value refuses the boot rather than being silently ignored.
    """
    section = policy.get("prompt", {})
    if not isinstance(section, dict):
        raise ValueError("ranker policy `prompt` must be a mapping")
    unknown = set(section) - {"max_history_events", "max_model_candidates"}
    if unknown:
        raise ValueError(f"unknown ranker policy prompt keys: {sorted(unknown)}")
    # The ranges are enforced by RankerPolicy.validate, which is the one place
    # that knows them; this only refuses a value of the wrong shape.
    for key in ("max_history_events", "max_model_candidates"):
        if key in section and type(section[key]) is not int:
            raise ValueError(f"ranker policy prompt.{key} must be an integer")
    # One home for the default: the dataclass that enforces the range.
    return (section.get("max_history_events", RankerPolicy.max_history_events),
            section.get("max_model_candidates", RankerPolicy.max_model_candidates))


def supabase_timeout_seconds(policy) -> float:
    """`supabase.timeout_seconds` from the ranker policy, validated at boot.

    A per-call budget is an operational value, so it belongs in the policy file
    rather than in the transport's signature: the heavy Phase 2 candidate query
    grows with the corpus, and raising the ceiling must not require a code
    change. Validated HERE, at startup, so a bad value refuses the boot instead
    of surfacing as an opaque 503 on the first request.
    """
    # REQUIRED, not defaulted. A misspelled section name (`supabse:`) would
    # otherwise fall back to the old 3.0 on a policy file that reads as correct,
    # which is the original outage with the fix apparently applied. An absent
    # section is a refused boot; DEFAULT_TIMEOUT_SECONDS stays the transport's
    # own signature default for callers that build it directly.
    if "supabase" not in policy:
        raise ValueError("ranker policy must declare a `supabase` section with timeout_seconds")
    section = policy["supabase"]
    if not isinstance(section, dict):
        raise ValueError("ranker policy `supabase` must be a mapping")
    unknown = set(section) - {"timeout_seconds", "timeout_retries"}
    if unknown:
        raise ValueError(f"unknown ranker policy supabase keys: {sorted(unknown)}")
    if "timeout_seconds" not in section:
        raise ValueError("ranker policy supabase section must declare timeout_seconds")
    return validate_timeout_seconds(section["timeout_seconds"])


def supabase_timeout_retries(policy) -> int:
    if "supabase" not in policy:
        raise ValueError("ranker policy must declare a `supabase` section with timeout_retries")
    section = policy["supabase"]
    if not isinstance(section, dict):
        raise ValueError("ranker policy `supabase` must be a mapping")
    if "timeout_retries" not in section:
        raise ValueError("ranker policy supabase section must declare timeout_retries")
    return validate_timeout_retries(section["timeout_retries"])


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
