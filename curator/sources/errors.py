"""Sanitized error types for the source transport.

Transport failures cross logging and health-report boundaries.  They therefore
carry only the configured source id and a stable reason code.  The destination,
credentials, response fragments, and underlying exception text stay private to
the transport implementation.
"""

from __future__ import annotations

from enum import Enum


class SafeTransportReason(str, Enum):
    INVALID_URL = "invalid_url"
    UNSAFE_HOST = "unsafe_host"
    RESOLUTION_FAILED = "resolution_failed"
    CONNECT_FAILED = "connect_failed"
    PEER_MISMATCH = "peer_mismatch"
    TLS_VALIDATION_FAILED = "tls_validation_failed"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INVALID_REQUEST = "invalid_request"
    CREDENTIAL_ORIGIN_MISMATCH = "credential_origin_mismatch"
    REDIRECT_REJECTED = "redirect_rejected"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNSUPPORTED_CONTENT_ENCODING = "unsupported_content_encoding"
    UNSUPPORTED_MIME_TYPE = "unsupported_mime_type"


class SafeTransportError(Exception):
    """A deliberately low-information transport failure."""

    __slots__ = ("source_id", "reason")

    def __init__(self, source_id: str, reason: SafeTransportReason) -> None:
        self.source_id = _safe_source_id(source_id)
        self.reason = reason
        super().__init__(self.source_id, self.reason.value)

    @property
    def reason_code(self) -> str:
        return self.reason.value

    def __str__(self) -> str:
        return f"{self.source_id}: {self.reason.value}"


def _safe_source_id(value: object) -> str:
    text = str(value or "unknown")
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch in "._-")[:80]
    return cleaned or "unknown"
