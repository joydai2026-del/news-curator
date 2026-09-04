"""Compatibility re-export of the shared ownership rule.

The rule moved to ``curator/ownership.py`` on 2026-09-02: it is not
ledger-specific, and keeping it here forced ``curator.sources.checkpoint`` to
import ``curator.ledger`` (and, through that package's ``__init__``, its base
and in-memory store) just to ask whether a checkpoint names a human. This
module stays so existing ``from ..ledger.ownership import ...`` call sites keep
working; there is no second copy of the rule, only this re-export.
"""

from __future__ import annotations

from curator.ownership import (
    INVISIBLE_ID_SQL_CLASS,
    is_receipt_wrapper,
    is_subject_bound,
    noncanonical_id_reason,
    ownership_id_sql_check,
    ownership_violations,
    receipt_wrapper_violations,
)

__all__ = [
    "INVISIBLE_ID_SQL_CLASS",
    "is_receipt_wrapper",
    "is_subject_bound",
    "noncanonical_id_reason",
    "ownership_id_sql_check",
    "ownership_violations",
    "receipt_wrapper_violations",
]
