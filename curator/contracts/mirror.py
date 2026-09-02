"""Mirror contract: writing a canonical artifact version to an external target.

Declarative only. No behavior, no I/O.
Freezes: plan "Canonical artifacts are versioned" plus the provider write-mode
table, and SC-26.

State machine (frozen):

    planned -> writing -> settled
    planned -> writing -> conflict
    planned -> writing -> unknown

``conflict`` and ``unknown`` are terminal for that attempt. Neither permits an
overwrite or an automatic retry. Settlement requires a target readback whose
checksum matches the intended artifact version, not merely a 2xx response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .enums import MirrorState, WriteMode
from .tenant import Ownership

# The only legal transitions. Anything else is a contract violation.
MIRROR_TRANSITIONS: tuple[tuple[MirrorState, MirrorState], ...] = (
    (MirrorState.PLANNED, MirrorState.WRITING),
    (MirrorState.WRITING, MirrorState.SETTLED),
    (MirrorState.WRITING, MirrorState.CONFLICT),
    (MirrorState.WRITING, MirrorState.UNKNOWN),
)

MIRROR_TERMINAL_STATES: tuple[MirrorState, ...] = (
    MirrorState.SETTLED,
    MirrorState.CONFLICT,
    MirrorState.UNKNOWN,
)


@dataclass(frozen=True)
class MirrorAdapterDescriptor:
    """What one mirror target may do. Write mode is provider-gated.

    ``OVERWRITE_COMPARE_AND_SET`` requires ``proves_atomic_conditional_write``.
    A client-side read-compare-write is not an atomic conditional write and can
    lose a human edit between the pre-read and the write, then read back its own
    content and call it settled.
    """

    adapter_id: str
    adapter_version: str
    write_mode: WriteMode
    proves_atomic_conditional_write: bool
    # For a git-backed target the precondition is the commit parent; for a
    # server-enforced target it is a version or entity tag. Named, never a
    # provider brand.
    precondition_kind: str
    supports_readback: bool = True


@dataclass(frozen=True)
class MirrorReceipt(Ownership):
    """One attempt to place one artifact version on one target."""

    receipt_id: str
    artifact_id: str
    artifact_version: int
    adapter_id: str
    target_id: str
    state: MirrorState
    idempotency_key: str
    attempted_at: datetime
    expected_prior_checksum: str
    attempted_checksum: str
    # Predecessor linkage. Required on every receipt (empty tuple/string for
    # a genuine first attempt) rather than defaulted away: an attacker who
    # simply omits these three fields must fail structural validation rather
    # than pass as a clean first attempt (reproduced: stripping all three
    # from a same-idempotency-key retry off a conflicted receipt made an
    # automatic retry valid).
    prior_receipt_ids: tuple[str, ...]
    # The state the immediately preceding attempt (named in prior_receipt_ids)
    # settled into. Empty string only when prior_receipt_ids is empty (no
    # prior attempt at all). Lets the validator recompute whether this
    # attempt is a legal resolution of a conflict or an unknown, without
    # needing to look another fixture up.
    prior_attempt_state: str
    # A recorded human (or otherwise out-of-band) resolution reference.
    # Required whenever this receipt claims to resolve a prior conflict or
    # unknown into settled or writing: its absence is exactly the automatic
    # retry the state machine forbids.
    resolution_ref: str
    readback_checksum: str = ""
    settled_at: datetime | None = None
    reason_code: str = ""
    # A revision-appending target records the new revision it created rather
    # than claiming it replaced the old one.
    created_revision_id: str = ""
