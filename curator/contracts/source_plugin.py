"""Source plugin contract: an ADDITIVE layer over the live SourceAdapter.

Declarative only. No behavior, no I/O.

Freezes: plan "Source plugin contract" plus its recorded supersession note, and
SC-23. The live two-member Protocol (``type_key``, ``validate_options``,
``fetch``) in ``curator/sources/base.py`` is NOT replaced. ``SourcePlugin``
below restates it and adds the four capabilities the plan names on top:
``discover``/``poll``, ``normalize``, ``checkpoint``, and ``capabilities``.
``health`` already exists inside ``SourceResult`` and stays there.

Migration status per capability is recorded in ``docs/contracts/source-plugin.md``.
Checkpoint is greenfield: Gate 0c found no durable cursor anywhere in
``curator/sources/`` beyond one inert ``SourceContext.durable_store`` field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from .enums import CheckpointState, HealthStatus, PluginState


@dataclass(frozen=True)
class SourceCapabilities:
    """What one adapter can actually do. Declared, never inferred.

    Exists because the live registry has an undeclared behavioural split: every
    route is handed search queries but only one adapter consumes them.
    """

    plugin_id: str
    plugin_version: str
    supports_poll: bool
    supports_push: bool
    supports_full_text: bool
    supports_trend_signal: bool
    supports_social_signal: bool
    supports_deletion: bool
    supports_incremental_checkpoint: bool
    consumes_search_queries: bool
    languages: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SourceRights:
    """DECLARED AND DEFERRED. Gate 0c disposition D4.

    Syndication terms live today as prose comments in configuration that no
    code can read. That is safe only while routes are added by hand. The field
    is frozen here so it is not discovered late, and is NOT populated or
    enforced until discover/poll lands, because programmatic route discovery is
    what makes machine-readable rights load-bearing.
    """

    # Stable identifier of the terms that govern this route, not free text.
    terms_id: str
    # True only when a human recorded the terms against this exact route.
    verified: bool
    # Deferred means: writers may leave this unset and no gate reads it yet.
    deferred: bool = True
    public_projection_eligible: bool | None = None
    verbatim_quote_eligible: bool | None = None


@dataclass(frozen=True)
class SourceProvenance:
    """Per-item provenance the normalized record must preserve.

    ``fetched_at`` and ``adapter_version`` are the additions over what the live
    Item already carries: today an item records when it was published but not
    when it was seen, nor by which adapter build.
    """

    source_id: str
    plugin_id: str
    adapter_version: str
    original_item_id: str
    url: str
    canonical_url: str
    fetched_at: datetime
    published_at: datetime | None
    transform_version: str
    published_at_is_estimated: bool = False
    echo_eligible: bool = True
    raw_response_digest: str = ""


@dataclass(frozen=True)
class NormalizedSourceDocument:
    """The single schema every adapter's normalize step must produce.

    Candidate generators and ranking read this. They never branch on a
    provider name, and no provider name appears in any field above.
    """

    document_id: str
    tenant_id: str
    title: str
    url: str
    canonical_url: str
    language: str
    provenance: SourceProvenance
    summary: str = ""
    image_url: str = ""
    native_rank: int | None = None
    native_score: int | None = None
    topic_tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SourceCheckpoint:
    """Durable per-route poll state. GREENFIELD: nothing implements this today.

    Advances only after the normalized writes for that batch settle. A blocked
    checkpoint must not be skipped forward; the next poll resumes from the last
    settled cursor, which is what makes a resume neither replay nor silently
    skip.
    """

    plugin_id: str
    source_id: str
    tenant_id: str
    state: CheckpointState
    cursor: str
    watermark: datetime | None
    last_settled_run_id: str
    # The health receipt this checkpoint's settlement is proven against.
    # Required, like cursor: may be empty while not yet settled, but the
    # field itself is never absent (plan "Core records": source_checkpoints
    # carries a health receipt reference).
    health_receipt_id: str
    updated_at: datetime
    # Conditional-GET state. Absent everywhere in the live collector today.
    etag: str = ""
    last_modified: str = ""
    consecutive_failures: int = 0
    backoff_until: datetime | None = None


@dataclass(frozen=True)
class SourceHealthRecord:
    """Health as a durable cross-run record.

    The live ``SourceHealth`` is per-run only, which is why three routes served
    a parseable but frozen archive for 77 to 237 days without escalating.
    ``consecutive_failures`` and ``newest_item_age_hours`` together are what a
    dead-but-200 route trips on.
    """

    source_id: str
    plugin_id: str
    status: HealthStatus
    usable_items: int
    newest_item_age_hours: float | None
    max_age_hours: float
    observed_at: datetime
    reason_code: str = ""
    consecutive_failures: int = 0
    last_success_at: datetime | None = None


@dataclass(frozen=True)
class SourcePluginRegistration:
    """One registry row for a plugin (`source_plugins`, plan "Core records").

    A plugin is never implicitly enabled. ``REGISTERED`` means the row exists
    and nothing polls it yet; only ``ENABLED`` may be polled. ``capabilities``
    is embedded rather than looked up separately, so a registry row and its
    declared capabilities cannot drift apart.
    """

    plugin_id: str
    plugin_version: str
    tenant_id: str
    config_reference: str
    capabilities: SourceCapabilities
    state: PluginState
    registered_at: datetime


class SourcePlugin(Protocol):
    """The frozen plugin surface.

    The first three members are the LIVE contract, restated unchanged so the
    extension is visibly additive. The last four are the additive layer.
    """

    type_key: str

    def validate_options(self, spec: Any) -> Mapping[str, Any]: ...

    def fetch(self, spec: Any, context: Any) -> Any: ...

    # --- additive layer ------------------------------------------------
    def capabilities(self) -> SourceCapabilities: ...

    def discover(
        self, spec: Any, context: Any, checkpoint: SourceCheckpoint
    ) -> Sequence[Any]: ...

    def normalize(
        self, spec: Any, raw_items: Sequence[Any]
    ) -> Sequence[NormalizedSourceDocument]: ...

    def advance_checkpoint(
        self, checkpoint: SourceCheckpoint, settled_documents: Sequence[str]
    ) -> SourceCheckpoint: ...
