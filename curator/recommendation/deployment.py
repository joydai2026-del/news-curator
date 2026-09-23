"""Deployment-shape values read identically by the deploy host and the container.

The Modal function timeout is declared on the deploy host (``modal_app``) and it
bounds what the service inside the container is allowed to promise (``runtime``).
Both read it HERE, from the same environment variable with the same default and
the same bounds, so the two can never disagree about how long one request may
take. A value that would let a request outlive its own container is refused at
boot, by name, instead of surfacing as a 500 after the platform kills it.
"""

from __future__ import annotations

import re

FUNCTION_TIMEOUT_ENV = "NEWS_CURATOR_MODAL_FUNCTION_TIMEOUT_SECONDS"
# The shipped request budget is 225s including the exclusion-heavy paid path
# and retries for both owner-state reads. 240 leaves fifteen seconds of room;
# deploy host and runtime both honor FUNCTION_TIMEOUT_ENV for larger budgets.
# The maximum stays below Modal's own web-endpoint ceiling.
FUNCTION_TIMEOUT_DEFAULT = 240
FUNCTION_TIMEOUT_MINIMUM = 7
FUNCTION_TIMEOUT_MAXIMUM = 300


def bounded_int(env, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"{name} must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def function_timeout_seconds(env) -> int:
    """The container's wall clock for ONE request, in seconds."""
    return bounded_int(env, FUNCTION_TIMEOUT_ENV, FUNCTION_TIMEOUT_DEFAULT,
                       FUNCTION_TIMEOUT_MINIMUM, FUNCTION_TIMEOUT_MAXIMUM)
