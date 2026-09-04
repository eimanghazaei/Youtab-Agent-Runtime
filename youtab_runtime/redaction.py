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
# when they appear inside free text (error messages, tracebacks). Deliberately
# broad and high-recall: false positives only cost visibility, a miss leaks a
# secret. The prefixed shapes catch provider tokens BELOW the generic 40-char
# floor (AWS key ids are 20 chars, Google keys 39), and the base64 alternative
# catches standard-base64 blobs the url-safe generic class ([A-Za-z0-9_-]{40,})
# misses because it excludes ``+ / =``.
_INLINE_SECRET_RE = re.compile(
    r"(?i)("
    r"bearer\s+[A-Za-z0-9._\-]{12,}"
    # GitHub PATs/tokens (classic prefixes + fine-grained github_pat_).
    r"|(?:sk|pk|ghp|gho|ghu|ghs|ghr)-[A-Za-z0-9._\-]{12,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    # Slack tokens: xoxb / xoxp / xoxa / xoxr / xoxs.
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    # AWS access key ids (prefix + 16 uppercase alnum).
    r"|(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16}"
    # Google API keys (AIza + 35).
    r"|AIza[A-Za-z0-9_\-]{35}"
    # JWTs (three base64url segments) even when not prefixed by "bearer".
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    # Standard-base64 high-entropy blob (with + / and optional padding).
    r"|[A-Za-z0-9+/]{40,}={0,2}"
    # Generic url-safe high-entropy run.
    r"|[A-Za-z0-9_\-]{40,}"
    r")"
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
    """Summarise a tool/effect result as a digest + bounded summary, never raw.

    Structured (non-string) results are run through :func:`redact_mapping` FIRST
    so a secret-shaped *value* under a secret-shaped *key* (e.g.
    ``{"password": "hunter2"}``) is dropped before it can survive in the summary —
    ``scrub_text`` alone is value-based and would miss a short keyed secret. The
    digest is taken over the original result (a hash, not a leak) so provenance
    is preserved.
    """
    kind = type(result).__name__
    if isinstance(result, str):
        summary_src = result
    else:
        try:
            summary_src = json.dumps(
                redact_mapping(result), sort_keys=True, ensure_ascii=False,
                default=repr,
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


# Keys whose STRING value is structural metadata (a digest, id, hostname, shape,
# classification) — never a credential — so it is kept verbatim at the journal
# chokepoint rather than run through the inline-secret scrubber. Without this a
# legitimately-stored 64-hex ``digest`` (audit evidence) is shape-identical to a
# secret and would be wrongly redacted. Secret-shaped keys are handled first and
# are never in this set.
_SAFE_VERBATIM_KEYS = frozenset(
    {
        "digest", "_bytes_digest", "_len", "event_id", "run_id", "child_run_id",
        "original_run_id", "correlation_id", "effect_id", "tool_call_id",
        "target_scope_digest", "provider_idempotency_key", "path_shape", "host",
        "scheme", "port", "dest_class", "policy_decision", "adapter",
        "effect_ref", "effect_type", "state", "kind", "category",
        "schema_version", "dedupe_key", "process_id", "http_status", "bytes_out",
    }
)


def _safe_verbatim_key(key: str) -> bool:
    low = key.lower()
    if low in _SAFE_VERBATIM_KEYS:
        return True
    return low.endswith(("_digest", "_hash", "_id", "_seq"))


def _redact_journal(value: Any, *, _depth: int = 0) -> Any:
    """Redact a run-journal payload: key-based secret redaction + numeric
    preservation + free-text scrubbing + structural bounding.

    Rules:
      * A secret-shaped key with a str/bytes/container value -> ``"[redacted]"``;
        with a numeric/bool/None value -> **kept** (a number cannot carry a
        credential, and usage keys like ``input_tokens`` legitimately contain the
        substring "token" with an integer *count* value — blanket key redaction
        would destroy usage measurement).
      * A non-secret key whose value is a *structural* string (digest / id /
        hostname / shape — see :data:`_SAFE_VERBATIM_KEYS`) is kept verbatim so
        audit evidence (digests) survives.
      * Any other string is run through :func:`scrub_text` so an inline secret in
        free text (an error message, a note) is caught even under a non-secret
        key — defense in depth over the emitters' own scrubbing.
      * Containers are bounded (64 items, depth 6); bytes become a digest summary.
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
                if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
                    out[key] = v  # a number/None under a secret key is a count
                else:
                    out[key] = REDACTED  # string/bytes/container secret -> drop
            elif isinstance(v, str) and _safe_verbatim_key(key):
                out[key] = v  # structural metadata / digest — keep verbatim
            else:
                out[key] = _redact_journal(v, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = list(value)[:_MAX_ARG_ITEMS]
        red = [_redact_journal(v, _depth=_depth + 1) for v in items]
        if len(value) > _MAX_ARG_ITEMS:
            red.append({"_truncated_items": len(value) - _MAX_ARG_ITEMS})
        return red
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"_bytes_digest": digest(value), "_len": len(value)}
    return scrub_text(repr(value))


def redact_journal_payload(payload: Any) -> Any:
    """Redaction chokepoint applied to EVERY run-journal payload before persist.

    This makes redaction an *invariant of the substrate* rather than a
    convention each emitter must remember: whatever a caller hands
    ``run_journal.append_event``, no string/bytes secret and no over-large
    structure can land in the durable journal. Numeric values under secret-shaped
    keys are preserved so usage counts survive (see :func:`_redact_journal`).
    Idempotent: re-redacting an already-redacted payload is a no-op.
    """
    return _redact_journal(payload)
