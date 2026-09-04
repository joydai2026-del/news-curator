"""Learning ledger store: append-only events, evidence, and deletion receipts.

See docs/contracts/ledger-storage.md for what this package implements and what
is deliberately not built yet (profile snapshot builder, rebuild, search
index, mirrors).
"""

from __future__ import annotations

from .base import LedgerStore
from .memory import InMemoryLedgerStore, LedgerError
from .ownership import is_subject_bound, ownership_violations

__all__ = [
    "InMemoryLedgerStore",
    "LedgerError",
    "LedgerStore",
    "is_subject_bound",
    "ownership_violations",
]
