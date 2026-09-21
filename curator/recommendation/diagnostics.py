"""One-line structured diagnostics for a swallowed exception.

An `except Exception:` that returns a stable fallback value has to say what
went wrong, or a production failure is silent by construction. These helpers
print exactly one JSON line naming the exception class, a safe detail string,
and the first traceback frame inside curator/, with two hard rules:

- The exception message is surfaced ONLY when the exception was actually
  RAISED (the `raise` statement itself) from inside curator/ code, and is one
  of our own ValueError/TypeError/KeyError (a fixed literal written by us).
  Any other exception, including a ValueError raised by a caller-supplied
  engine or store whose raise site is outside curator/, gets the literal
  "suppressed" instead, so a vendor or transport exception can never leak
  request-derived text into a log line.
- Never pass prompt text, story titles/summaries, tokens, keys, or request
  bodies as extra fields. Callers pass only counts and ids they already hold.
"""

from __future__ import annotations

import json
import sys
import traceback

_OWN_MESSAGE_TYPES = (ValueError, TypeError, KeyError)


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


def _raised_within_curator(exc: BaseException) -> bool:
    """True when the `raise` statement itself executed inside curator/ code."""
    frames = traceback.extract_tb(exc.__traceback__)
    return bool(frames) and _is_curator_file(frames[-1].filename)


def exception_detail(exc: BaseException) -> str:
    """The exception message, only for our own fixed-literal curator/ raises."""
    if isinstance(exc, _OWN_MESSAGE_TYPES) and _raised_within_curator(exc):
        return str(exc)
    return "suppressed"


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
