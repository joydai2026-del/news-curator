"""Secret-keyed identities for private newsletter delivery data.

The key is deliberately separate from Gmail OAuth values and the mailbox
profile guard. It must be exactly 32 bytes encoded as 64 lowercase hex
characters. Only the secret-bearing ingestion job receives it.

Rotation is recoverable but not transparent: linkless story identities change,
so an overlap window can show those stories again and previously stored user
state no longer joins to their old identities. Keep the key stable unless that
bounded replay and identity break are intentional.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping


ENV_IDENTITY_KEY = "NEWS_CURATOR_NEWSLETTER_IDENTITY_KEY"
_KEY_HEX = re.compile(r"[0-9a-f]{64}\Z")


def key_from_env(env: Mapping[str, object]) -> bytes | None:
    """Return the validated 256-bit identity key, never a credential fallback."""

    value = env.get(ENV_IDENTITY_KEY)
    if not isinstance(value, str) or not _KEY_HEX.fullmatch(value):
        return None
    return bytes.fromhex(value)


def opaque_discriminator(key: bytes, namespace: str, private_value: str) -> str:
    """Domain-separated HMAC for a private value that must cross a boundary."""

    material = f"news-curator:{namespace}\0{private_value}".encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()
