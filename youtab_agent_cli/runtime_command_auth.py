"""Signed-command verification for mutating Agent Runtime commands (AR-PROD-01).

The service-bearer (``RuntimeServiceProvider``) proves *which service* is
calling; this module proves *this specific command was authorised, is fresh,
and has not been replayed*. It is applied to the mutating runtime endpoints
(create-run, cancel, retry) so a captured request cannot be re-sent, tampered,
or delayed past its validity window.

Contract (provider-neutral, no external key custody required):

  * The gateway signs each mutating command with HMAC-SHA256 over a canonical
    string binding method, path, tenant, user, timestamp, nonce, and a hash of
    the request body, using the shared ``YOUTAB_AGENT_RUNTIME_SERVICE_SECRET``.
  * Headers carried on the request:
      - ``X-Youtab-Runtime-Timestamp`` : unix seconds when the command was signed
      - ``X-Youtab-Runtime-Nonce``     : a unique, single-use token
      - ``X-Youtab-Runtime-Signature`` : lowercase hex HMAC-SHA256 digest
  * Verification is fail-closed:
      - missing/short secret         -> ``secret_unavailable`` (surfaced 503)
      - missing header               -> ``missing_signature``  (401)
      - timestamp outside the window -> ``expired``            (401)
      - signature mismatch           -> ``bad_signature``      (401)
      - nonce already seen           -> ``replayed``           (409)
    The nonce is recorded only AFTER the signature and freshness checks pass, so
    a bad-signature probe cannot burn a legitimate nonce.

The signature binds tenant + user (the gateway-verified identity headers), so a
valid command for one tenant/user cannot be lifted onto another — the canonical
string, and therefore the digest, differ.

``NonceStore`` is injectable: the default is a small SQLite table; tests pass an
in-memory store. Both prune entries older than the freshness window on write, so
the store cannot grow without bound.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol

# Default freshness window: a command must be presented within this many seconds
# of its signing timestamp (guards against delayed replay while tolerating
# modest clock skew between the gateway and the engine).
DEFAULT_WINDOW_SECONDS = 300

TIMESTAMP_HEADER = "x-youtab-runtime-timestamp"
NONCE_HEADER = "x-youtab-runtime-nonce"
SIGNATURE_HEADER = "x-youtab-runtime-signature"


class CommandAuthError(Exception):
    """Raised when a signed command fails verification.

    ``code`` is a stable machine-readable class; ``http_status`` is the status
    the router should surface. ``message`` is safe to return to the caller (it
    never echoes the secret or the expected signature).
    """

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class NonceStore(Protocol):
    """Persistence seam for single-use nonces."""

    def seen(self, nonce: str) -> bool:
        """True if ``nonce`` was already recorded (i.e. this is a replay)."""
        ...

    def record(self, nonce: str, ts: int) -> None:
        """Record ``nonce`` as used at signing time ``ts`` and prune old rows."""
        ...


@dataclass
class _MemoryNonceStore:
    """In-memory nonce store (tests / single-process fallback)."""

    window_seconds: int = DEFAULT_WINDOW_SECONDS

    def __post_init__(self) -> None:
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    def seen(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._seen

    def record(self, nonce: str, ts: int) -> None:
        cutoff = int(time.time()) - self.window_seconds
        with self._lock:
            self._seen[nonce] = ts
            # Prune anything older than the freshness window — it can never be
            # accepted again anyway (it would fail the ``expired`` check first).
            stale = [n for n, t in self._seen.items() if t < cutoff]
            for n in stale:
                self._seen.pop(n, None)


class SqliteNonceStore:
    """SQLite-backed nonce store keyed by nonce, with time-based pruning."""

    def __init__(self, db_path: str, *, window_seconds: int = DEFAULT_WINDOW_SECONDS) -> None:
        self._db_path = db_path
        self._window_seconds = window_seconds
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runtime_command_nonces ("
                "  nonce   TEXT PRIMARY KEY,"
                "  seen_at INTEGER NOT NULL"
                ")"
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def seen(self, nonce: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runtime_command_nonces WHERE nonce = ?", (nonce,)
            ).fetchone()
            return row is not None

    def record(self, nonce: str, ts: int) -> None:
        cutoff = int(time.time()) - self._window_seconds
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runtime_command_nonces (nonce, seen_at) "
                "VALUES (?, ?)",
                (nonce, ts),
            )
            conn.execute(
                "DELETE FROM runtime_command_nonces WHERE seen_at < ?", (cutoff,)
            )
            conn.commit()


def canonical_string(
    *, method: str, path: str, tenant: str, user: str, timestamp: str, nonce: str, body: bytes
) -> str:
    """Build the canonical string that the signature covers.

    Binds the HTTP verb, the exact path, the gateway-verified tenant+user, the
    freshness fields, and a hash of the raw body. Any tampering with any of
    these changes the digest.
    """
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return "\n".join([
        method.upper(),
        path,
        tenant,
        user,
        str(timestamp),
        nonce,
        body_hash,
    ])


def compute_signature(secret: str, canonical: str) -> str:
    """Lowercase hex HMAC-SHA256 of ``canonical`` under ``secret``."""
    return hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_command(
    *,
    method: str,
    path: str,
    tenant: str,
    user: str,
    body: bytes,
    headers: "dict[str, str]",
    secret: str,
    store: NonceStore,
    now: Optional[int] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> None:
    """Verify a signed mutating command. Raise :class:`CommandAuthError` on failure.

    ``headers`` is a case-insensitively-accessed mapping (a Starlette
    ``request.headers`` works directly). On success the nonce is recorded and
    the function returns ``None``.
    """
    if not secret or len(secret) < 43:
        # No usable signing key — the surface must not accept unsigned mutations.
        raise CommandAuthError(
            "secret_unavailable",
            "runtime command signing secret is unavailable",
            503,
        )

    ts_raw = _get_header(headers, TIMESTAMP_HEADER)
    nonce = _get_header(headers, NONCE_HEADER)
    signature = _get_header(headers, SIGNATURE_HEADER)
    if not ts_raw or not nonce or not signature:
        raise CommandAuthError(
            "missing_signature",
            "signed command requires timestamp, nonce, and signature headers",
            401,
        )

    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        raise CommandAuthError("missing_signature", "malformed command timestamp", 401)

    current = int(time.time()) if now is None else int(now)
    if abs(current - ts) > window_seconds:
        raise CommandAuthError(
            "expired",
            "command timestamp outside the accepted freshness window",
            401,
        )

    expected = compute_signature(
        secret,
        canonical_string(
            method=method,
            path=path,
            tenant=tenant,
            user=user,
            timestamp=ts_raw,
            nonce=nonce,
            body=body,
        ),
    )
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise CommandAuthError("bad_signature", "command signature mismatch", 401)

    # Freshness + signature pass BEFORE we consult/burn the nonce, so a probe
    # with a bad signature can never consume a legitimate nonce (or grow the
    # store). Only a fully-valid command records its nonce.
    if store.seen(nonce):
        raise CommandAuthError("replayed", "command nonce already used", 409)
    store.record(nonce, ts)


def _get_header(headers: "dict[str, str]", name: str) -> str:
    """Case-insensitive header read that works for dicts and Starlette Headers."""
    try:
        value = headers.get(name)
    except AttributeError:
        value = None
    if value is None:
        # Fall back to a manual case-insensitive scan for a plain dict whose
        # keys aren't already lowercased.
        for k, v in dict(headers).items():
            if k.lower() == name:
                value = v
                break
    return (value or "").strip()
