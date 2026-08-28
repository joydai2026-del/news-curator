"""The newsletter lane: JJ's own subscriptions, read from a dedicated mailbox.

Four modules, one entry point:

    lane.fetch()      the orchestrator; the only thing the pipeline calls
    gmail.py          Gmail REST v1 over requests + the stdlib email parser
    state.py          the committed cursor (watermark + salted content hashes)
    sanitize.py       the URL privacy rule: publisher link, or no link
    adapters.py       the five-sender allowlist and its parsers

Importing this package does nothing and calls nothing. The lane is dark unless
a caller passes an explicit flag AND the three Gmail secrets are in the
environment, so a fork gets the same product with this lane simply absent.
"""

from __future__ import annotations

from .lane import LaneResult, enabled, fetch  # noqa: F401
from .state import STATE_FILENAME, NewsletterState  # noqa: F401

__all__ = ["LaneResult", "NewsletterState", "STATE_FILENAME", "enabled", "fetch"]
