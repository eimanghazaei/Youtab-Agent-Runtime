"""Process-local, non-reversible cache discriminators for credentials."""

import hashlib
import hmac
import secrets


_CACHE_HMAC_KEY = secrets.token_bytes(32)


def credential_cache_fingerprint(value: str) -> str:
    """Return a stable-for-this-process credential key without exposing its bytes."""
    return hmac.new(
        _CACHE_HMAC_KEY, value.encode("utf-8"), hashlib.sha256
    ).hexdigest()
