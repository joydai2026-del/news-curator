"""One-line structured diagnostics for a swallowed exception.

An `except Exception:` that returns a stable fallback value has to say what
went wrong, or a production failure is silent by construction. These helpers
print exactly one JSON line naming the exception class, a safe detail string,
and the first traceback frame inside curator/, with two hard rules:

- The exception message is surfaced ONLY when `str(exc)` is EXACTLY one of
  the reviewed literals in KNOWN_DIAGNOSTIC_MESSAGES below. A ValueError
  raised from curator/ code is not automatically safe: `raise ValueError(f"...
  {error}")` or any other f-string/concatenation can carry a wrapped vendor
  message or request-derived text, and would leak it if location or type
  alone were trusted. Only a hand-reviewed, constant-string literal is
  trusted, and anything else (a vendor exception, a dynamic curator message,
  or a coincidental match on a different exception's text) gets the literal
  "suppressed" instead.
- Never pass prompt text, story titles/summaries, tokens, keys, or request
  bodies as extra fields. Callers pass only counts and ids they already hold.

KNOWN_DIAGNOSTIC_MESSAGES is kept honest by
tests/test_diagnostics_known_messages.py, which re-scans curator/recommendation/
and curator/contracts/ with the ast module for every constant-string
`raise ValueError(...)` / `TypeError(...)` / `KeyError(...)` and asserts this
set is a superset of what it finds. Adding a new literal raise there means
adding it here too, or that test fails.
"""

from __future__ import annotations

import json
import sys
import traceback

# Regenerate by scanning curator/recommendation/ and curator/contracts/ for
# every `raise ValueError("...")` / TypeError / KeyError whose argument is a
# plain string constant (no f-string, no concatenation, no call). See
# tests/test_diagnostics_known_messages.py for the exact scan.
KNOWN_DIAGNOSTIC_MESSAGES = frozenset({
    'ranker policy must declare a `supabase` section with timeout_retries',
    'ranker policy supabase section must declare timeout_retries',
    'supabase.timeout_retries must be an integer',
    'NEWS_CURATOR_MODAL_MODE must be service or smoke',
    'NEWS_CURATOR_RANKER_CONTEXT_SHA256 must be lowercase SHA-256',
    'Supabase keys must be configured',
    'Supabase origin must be a fixed HTTPS origin',
    'an enabled ranking service requires a non-empty preview owner allowlist',
    'body must be an object',
    'body_too_large',
    'candidate IDs must be unique',
    'candidate IDs must exactly match the selected eligible-candidate registry',
    'candidate content fields have invalid types',
    'candidate count must be positive',
    'canonical registry IDs must be unique',
    'cursor signing key must contain at least 32 bytes',
    'cursor_ttl_seconds must be a positive integer',
    'cursor_ttl_seconds must cover the complete reading run',
    'display_language must be a supported language',
    'effective_policy_digest must be lowercase SHA-256',
    'empty history requires revision zero',
    'enabled ranker requires a non-empty preview owner allowlist',
    'enabled ranker requires a scoped model key',
    'exclusive_category_id must be a category id',
    'exclusivity_policy_id must be a policy id',
    'fallback results require a reason',
    'history action value must be boolean when present',
    'history event IDs must be unique',
    'history event fields have invalid types',
    'history revision must name the newest included event',
    'history story context must contain valid strings',
    'invalid ASGI configuration',
    'invalid observed usage settlement',
    'invalid preview owner allowlist',
    'invalid provider HTTP category',
    'invalid provider token budget',
    'invalid provider token count',
    'invalid provider transport configuration',
    'invalid ranker image context entry',
    'invalid ranker policy',
    'invalid_corpus_cursor',
    'invalid_eligibility',
    'invalid_exclude_story_ids',
    'invalid_independent_source_count',
    'invalid_optional_string',
    'invalid_page_size',
    'invalid_translation_overlay',
    'model results cannot carry a fallback reason',
    'model tokenizer mismatch',
    'maximum_excluded_story_ids must be between 0 and 1000',
    'now must be timezone-aware',
    'observed usage exceeded its reservation',
    'ordered history must use strict revision order',
    'output budget cannot accommodate the provider answer and reasoning allowance',
    'owner actor kind must be a supported enum',
    'preview_owner_ids entries must be non-empty owner ids',
    'provider endpoint must be HTTPS',
    'ranked IDs must be an exact unique permutation',
    'ranker cost limits must be positive',
    'ranker deadline must be within sixty seconds',
    'ranker endpoint must be a credential-free HTTPS origin',
    'ranker endpoint must be a fixed API base',
    'ranker image context cannot contain symlinks',
    'ranker image context file is missing or unsafe',
    'ranker image context file mismatch',
    'ranker image context has missing or extra files',
    'ranker image context manifest digest mismatch',
    'ranker image context manifest files are invalid',
    'ranker permits at most one retry',
    'ranker policy `prompt` must be a mapping',
    'ranker policy `supabase` must be a mapping',
    'ranker policy must declare a `supabase` section with timeout_seconds',
    'ranker policy supabase section must declare timeout_seconds',
    'ranker prompt.max_history_events must be an integer between 0 and 200',
    'ranker prompt.max_model_candidates must be an integer between 5 and 100',
    'ranker token prices must be finite non-negative numbers',
    'request and owner must use the supported contract types',
    'request collections and query must use the supported contract types',
    'response mode must be a supported enum',
    'response must use the supported contract type',
    'response receipt does not match the expected request',
    'reviewed tokenizer cache missing or changed',
    'schema and history revisions must be valid',
    'supabase.timeout_seconds must be a number of seconds',
    'token estimates must be non-negative',
    'tokenizer cache environment mismatch',
    'unknown model tokenizer',
    'unreviewed tokenizer encoding',
    'unsafe or duplicate ranker image context path',
    'unsupported schema version',
})


def _is_curator_file(filename: str) -> bool:
    normalized = filename.replace("\\", "/")
    return "/curator/" in normalized or normalized.startswith("curator/")


def _format_frame(frame: traceback.FrameSummary) -> str:
    filename = frame.filename.replace("\\", "/")
    module = "curator/" + filename.split("/curator/", 1)[1] if "/curator/" in filename else filename
    return f"{module}:{frame.name}:{frame.lineno}"


def first_curator_frame(exc: BaseException) -> str | None:
    """module:function:line of the first traceback frame inside curator/, or None.

    Traceback order is outermost (closest to the except clause) first, so this
    is the curator boundary the exception crossed, not necessarily where it
    originated.
    """
    for frame in traceback.extract_tb(exc.__traceback__):
        if _is_curator_file(frame.filename):
            return _format_frame(frame)
    return None


def exception_detail(exc: BaseException) -> str:
    """The exception message, only when it exactly matches a reviewed literal."""
    message = str(exc)
    return message if message in KNOWN_DIAGNOSTIC_MESSAGES else "suppressed"


def log_suppressed_exception(event: str, exc: BaseException, *, stream=None, **fields) -> None:
    """Print ONE structured JSON line for an exception an `except Exception:`
    block is about to swallow.

    `fields` are additional non-sensitive context (counts, ids) the caller
    already has in scope; never prompt text, story titles/summaries, tokens,
    keys, or request bodies. `stream` defaults to stdout; pass sys.stderr to
    match an existing call site's convention.
    """
    payload = {
        "event": event,
        "exception_class": type(exc).__name__,
        "detail": exception_detail(exc),
        **fields,
    }
    frame = first_curator_frame(exc)
    if frame is not None:
        payload["frame"] = frame
    print(json.dumps(payload, separators=(",", ":")), file=stream or sys.stdout, flush=True)
