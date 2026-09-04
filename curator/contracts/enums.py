"""Frozen vocabularies for the phase-1 contract freeze.

Declarative only. No behavior, no I/O. Every member's wire value is the
lowercase string persisted in records, fixtures, and policy files.

Provider names never appear here. A provider is named only inside an adapter's
own configuration, never in a core contract field.
"""

from __future__ import annotations

from enum import Enum


class Lane(str, Enum):
    """The four candidate lanes. Independent of topic filters."""

    UPDATES = "updates"
    HOT = "hot"
    INTERESTED = "interested"
    SURPRISE = "surprise"


class ActorKind(str, Enum):
    """Who performed an action. Human and agent paths are peers."""

    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class Scope(str, Enum):
    """Authorization scopes named by the plan's authorization matrix."""

    STORIES_READ = "stories:read"
    SEARCH_READ = "search:read"
    FEEDBACK_WRITE = "feedback:write"
    ARTIFACTS_WRITE = "artifacts:write"
    MIRRORS_WRITE = "mirrors:write"
    POLICY_ADMIN = "policy:admin"
    IMPORTS_WRITE = "imports:write"
    DATA_EXPORT = "data:export"
    DATA_DELETE = "data:delete"
    CONVERSATIONS_WRITE = "conversations:write"
    BASKETS_WRITE = "baskets:write"
    PUBLISH_APPROVE = "publish:approve"
    PUBLISH_EXECUTE = "publish:execute"
    SIGNER_USE = "signer:use"


class Decision(str, Enum):
    """Every authorization evaluation records one of these, plus an audit row."""

    ALLOW = "allow"
    DENY = "deny"


class EvidenceClass(str, Enum):
    """SC-04's four classes, exactly. Nothing may be added without a new freeze."""

    OBSERVED = "observed"
    INFERRED = "inferred"
    EXPLICIT = "explicit"
    PASSIVE = "passive"


class EvidenceOrigin(str, Enum):
    """Where the evidence came from. Separate axis from EvidenceClass.

    Imported history is not a fifth evidence class; it is a different origin
    for the same four classes. Keeping the axes separate is what stops an
    imported exposure row from ever presenting as a live explicit action.
    """

    LIVE = "live"
    IMPORTED = "imported"


class ConfidenceBand(str, Enum):
    """The plan's strong / medium / weak evidence strength."""

    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


class EventType(str, Enum):
    """Every learning event the plan's event-semantics table names."""

    MORE_LIKE_THIS = "more_like_this"
    LESS_LIKE_THIS = "less_like_this"
    ALREADY_KNEW_THIS = "already_knew_this"
    SURPRISE_ME = "surprise_me"
    SAVE = "save"
    SAVE_ANSWER = "save_answer"
    ASK_AI_QUESTION = "ask_ai_question"
    ASK_AI_FOLLOW_UP = "ask_ai_follow_up"
    CREATE_REPORT = "create_report"
    READ_MORE = "read_more"
    ACCORDION_EXPAND = "accordion_expand"
    RETURN_TO_STORY = "return_to_story"
    DWELL = "dwell"
    SCROLL = "scroll"
    IMPORTED_MAIL_UNREAD_STATE = "imported_mail_unread_state"
    IMPORTED_BROWSER_VISIT = "imported_browser_visit"


class ArtifactType(str, Enum):
    """Knowledge artifact classes."""

    QUESTION = "question"
    ANSWER = "answer"
    REPORT = "report"
    INSIGHT = "insight"
    SAVE = "save"


class ArtifactStatus(str, Enum):
    """Artifact lifecycle. Retraction and redaction never delete a version."""

    DRAFT = "draft"
    SETTLED = "settled"
    REDACTED = "redacted"
    RETRACTED = "retracted"


class MirrorState(str, Enum):
    """The plan's mirror state machine, frozen."""

    PLANNED = "planned"
    WRITING = "writing"
    SETTLED = "settled"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class PublicationState(str, Enum):
    """The plan's shared publishing state machine, frozen."""

    DRAFT = "draft"
    READY = "ready"
    AUTHORIZED = "authorized"
    PUBLISHING = "publishing"
    SETTLED = "settled"
    FAILED_SAFE = "failed-safe"
    UNKNOWN = "unknown"


class WriteMode(str, Enum):
    """Provider-gated write mode for any external target.

    OVERWRITE_COMPARE_AND_SET is available only to a provider that proves a
    server-enforced atomic conditional write. Everything else is append-only
    or hands the conflict to a human.
    """

    OVERWRITE_COMPARE_AND_SET = "overwrite_compare_and_set"
    APPEND_ONLY_REVISION = "append_only_revision"
    HUMAN_CONFLICT_RESOLUTION = "human_conflict_resolution"


class HealthStatus(str, Enum):
    """The status vocabulary the live collector already emits."""

    FRESH = "fresh"
    STALE = "stale"
    EMPTY = "empty"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    LINK_RESOLUTION_DEGRADED = "link_resolution_degraded"


class PublicationClass(str, Enum):
    """Whether a record may reach a public projection at all."""

    PRIVATE = "private"
    PUBLIC = "public"


class RetentionState(str, Enum):
    """Retention state of a raw import or stored blob."""

    ACTIVE = "active"
    RETRACTED = "retracted"
    PURGED = "purged"


class ReceiptState(str, Enum):
    """Every receipt settles into exactly one of these.

    PARTIAL exists so a deletion that cannot resolve one derived projection can
    never present as green.
    """

    SETTLED = "settled"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SearchResultClass(str, Enum):
    """Search spans normalized stories and knowledge artifacts."""

    STORY = "story"
    ARTIFACT = "artifact"


class SearchOutcome(str, Enum):
    """An empty result is a success. A failure is never rendered as empty."""

    OK = "ok"
    ERROR = "error"


class ScorerKind(str, Enum):
    """Which scorer produced a score set. Only TRANSPARENT is authoritative."""

    TRANSPARENT = "transparent"
    SHADOW_LEARNED = "shadow_learned"


class BandVerdict(str, Enum):
    """Per-band outcome recorded on every ranking run."""

    PASS = "pass"
    FAIL = "fail"
    DISABLED = "disabled"


class CorrectionAction(str, Enum):
    """Append-only correction actions. None of them edits the original row."""

    CORRECT = "correct"
    RETRACT = "retract"
    DELETE_REQUEST = "delete_request"


class CheckpointState(str, Enum):
    """Durable per-route poll state. Greenfield: nothing implements it today."""

    UNINITIALIZED = "uninitialized"
    ADVANCING = "advancing"
    SETTLED = "settled"
    BLOCKED = "blocked"


class PluginState(str, Enum):
    """Registry state of one source plugin registration.

    A plugin is never implicitly enabled. ``REGISTERED`` means the row exists
    and nothing polls it yet; only ``ENABLED`` may be polled.
    """

    REGISTERED = "registered"
    ENABLED = "enabled"
    DISABLED = "disabled"
    RETIRED = "retired"


class ReadbackVerdict(str, Enum):
    """How an ambiguous write was resolved by reading the destination.

    This is the ONLY thing that moves a record out of ``unknown``. A timer, a
    retry, or an operator's opinion is not a readback verdict.
    """

    #: The readback proved the intended content is at the destination.
    POSITIVE = "positive"
    #: The readback proved nothing landed.
    NEGATIVE_CONCLUSIVE = "negative_conclusive"
    #: The readback could not decide. The record stays unknown.
    INCONCLUSIVE = "inconclusive"
