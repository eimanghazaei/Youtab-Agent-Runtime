"""WAVE-26 shared redaction policy (frozen contract 6).

Every WAVE-26 event/ledger record passes its payload through these helpers
before it is persisted, so the durable journal, the effect ledger and the egress
audit can never become a secondary store of secrets — nor an exfiltration
channel in their own right.

Rules enforced here:
  * Secret-shaped keys (token/secret/password/api key/authorization/cookie/
    signature/credential/bearer/private key) are replaced with ``"[redacted]"``.
  * Large or arbitrary values are stored as a bounded summary plus a SHA-256
    digest, never verbatim.
  * URLs keep only scheme/host/port and a coarse path shape — query strings and
    userinfo (which routinely carry credentials or PII) are dropped.
  * HTTP headers keep only a small allowlist; everything else is dropped.

The functions are pure and import only the stdlib so they are safe to call from
any process, including a network-denied benchmark sandbox.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

REDACTED = "[redacted]"
_MAX_SUMMARY_CHARS = 512
_MAX_ARG_ITEMS = 64

# Substrings that mark a mapping key as secret-bearing. Matched case-insensitively
# against the key. Deliberately broad: false positives only cost visibility, a
# miss leaks a secret.
_SECRET_KEY_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth_header",
    "cookie",
    "signature",
    "credential",
    "bearer",
    "private_key",
    "privatekey",
    "access_key",
    "session_key",
    "client_secret",
    "refresh_token",
    "x-youtab-runtime-signature",
)

# Header names that may be retained verbatim; every other header is dropped.
_HEADER_ALLOWLIST = frozenset(
    {
        "content-type",
        "content-length",
        "accept",
        "user-agent",
        "host",
        "x-request-id",
        "x-correlation-id",
        "x-youtab-correlation-id",
    }
)

# Value patterns that look like bearer tokens / long high-entropy secrets even
# when they appear inside free text (error messages, tracebacks).
_INLINE_SECRET_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._\-]{12,}"
    r"|(?:sk|pk|ghp|gho|xoxb|xoxp)-[A-Za-z0-9._\-]{12,}"
    r"|[A-Za-z0-9_\-]{40,})"
)


def digest(data: Any) -> str:
    """Return a stable SHA-256 hex digest of ``data`` (JSON-normalised)."""
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        try:
            raw = json.dumps(data, sort_keys=True, ensure_ascii=False,
                             default=repr).encode("utf-8")
        except Exception:
            raw = repr(data).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _key_is_secret(key: str) -> bool:
    low = str(key).lower()
    return any(sub in low for sub in _SECRET_KEY_SUBSTRINGS)


def scrub_text(text: str, *, max_chars: int = _MAX_SUMMARY_CHARS) -> str:
    """Scrub inline secrets from free text and bound its length."""
    if not isinstance(text, str):
        text = str(text)
    scrubbed = _INLINE_SECRET_RE.sub(REDACTED, text)
    if len(scrubbed) > max_chars:
        scrubbed = scrubbed[:max_chars] + "…[truncated]"
    return scrubbed


def redact_mapping(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact secret-shaped keys and bound the structure.

    Returns a JSON-serialisable structure: secret keys become ``"[redacted]"``,
    strings are scrubbed and bounded, and oversized containers are truncated with
    a marker so a record can never balloon or smuggle raw payloads.
    """
    if _depth > 6:
        return {"_truncated_depth": True}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ARG_ITEMS:
                out["_truncated_items"] = True
                break
            key = str(k)
            if _key_is_secret(key):
                out[key] = REDACTED
            else:
                out[key] = redact_mapping(v, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = list(value)[:_MAX_ARG_ITEMS]
        red = [redact_mapping(v, _depth=_depth + 1) for v in items]
        if len(value) > _MAX_ARG_ITEMS:
            red.append({"_truncated_items": len(value) - _MAX_ARG_ITEMS})
        return red
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"_bytes_digest": digest(value), "_len": len(value)}
    return scrub_text(repr(value))


def redact_tool_args(tool_name: str, args: Any) -> dict[str, Any]:
    """Return a redacted, bounded representation of a tool's arguments."""
    return {
        "tool_name": str(tool_name),
        "args": redact_mapping(args),
    }


def redact_result(result: Any) -> dict[str, Any]:
    """Summarise a tool/effect result as a digest + bounded summary, never raw."""
    kind = type(result).__name__
    try:
        summary_src = result if isinstance(result, str) else json.dumps(
            result, sort_keys=True, ensure_ascii=False, default=repr
        )
    except Exception:
        summary_src = repr(result)
    return {
        "type": kind,
        "digest": digest(result),
        "summary": scrub_text(summary_src),
    }


def redact_headers(headers: Any) -> dict[str, str]:
    """Keep only allowlisted headers; drop Authorization/Cookie/etc. entirely."""
    if not isinstance(headers, Mapping):
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        name = str(k).lower()
        if name in _HEADER_ALLOWLIST:
            out[name] = scrub_text(str(v), max_chars=256)
    return out


def _path_shape(path: str) -> str:
    """Coarse path shape: keep segment count and static-looking segments, mask
    anything that looks like an id/token so a URL path cannot leak a secret."""
    if not path or path == "/":
        return "/"
    segs = [s for s in path.split("/") if s]
    shaped = []
    for s in segs:
        if len(s) >= 20 or re.search(r"\d", s):
            shaped.append(":var")
        else:
            shaped.append(s)
    return "/" + "/".join(shaped)


def redact_url(url: str) -> dict[str, Any]:
    """Return scheme/host/port/path-shape only — no query, no userinfo."""
    try:
        parts = urlsplit(str(url))
    except Exception:
        return {"scheme": None, "host": None, "port": None, "path_shape": None,
                "parse_error": True}
    host = parts.hostname
    return {
        "scheme": parts.scheme or None,
        "host": host,
        "port": parts.port,
        "path_shape": _path_shape(parts.path or ""),
        # Never include parts.query or parts.username/password.
        "had_query": bool(parts.query),
        "had_userinfo": bool(parts.username or parts.password),
    }


def redact_error(message: Any) -> str:
    """Scrub and bound an error message for storage."""
    return scrub_text("" if message is None else str(message))
