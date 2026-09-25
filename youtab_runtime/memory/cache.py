"""Encrypted, scope-partitioned local cache for Runtime working/retrieval data.

This is NOT Simorgh authority, an approval store, an effect ledger, a receipt
store, a secret store, or a long-term brain. It is a per-Runtime convenience
cache whose records are AEAD-encrypted and bound to the full scope tuple.

Every record is encrypted with AES-256-GCM using a unique nonce; the associated
data (AAD) is the canonical scope tuple + schema version + record key, so a
lookup under a different scope fails authentication and returns nothing. The
data-encryption key (DEK) is wrapped by a platform :class:`KeyStore` and never
stored in plaintext. If no key store is available the cache is DISABLED — there
is no plaintext fallback. Keys and plaintext are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .keystore import KeyStore, RevokedKeyError
from .scope import MemoryScope

SCHEMA_VERSION = "youtab.memory-cache.v1"
_NONCE_LEN = 12


class CacheDisabled(RuntimeError):
    """Raised when a cache operation is attempted with no available key store."""


class CacheFull(RuntimeError):
    """Raised when a write would exceed the cache's bounded size."""


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


class EncryptedScopedCache:
    def __init__(
        self,
        root_dir: str,
        keystore: KeyStore | None,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = root_dir
        self._keystore = keystore
        self._max_bytes = max_bytes
        self._clock = clock
        self._dek: bytes | None = None
        self._bytes = 0  # incremental size of .rec files; avoids O(N) walks per put
        self._enabled = bool(keystore is not None and keystore.is_available())
        if self._enabled:
            os.makedirs(self._root, exist_ok=True)
            try:
                os.chmod(self._root, 0o700)
            except OSError:
                pass  # Windows: best-effort; ACLs govern instead
            self._dek = self._load_or_create_dek()
            self._bytes = self._total_bytes()  # one-time walk at open (amortized)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def status(self) -> str:
        return "enabled" if self._enabled else "disabled_no_keystore"

    # ---- key management ----------------------------------------------------

    def _keyring_path(self) -> str:
        return os.path.join(self._root, "keyring.json")

    def _load_or_create_dek(self) -> bytes:
        assert self._keystore is not None
        path = self._keyring_path()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                ring = json.load(fh)
            return self._keystore.unwrap(_b64d(ring["wrapped_dek"]))
        dek = os.urandom(32)
        ring = {
            "key_id": self._keystore.key_id,
            "key_version": self._keystore.key_version,
            "wrapped_dek": _b64e(self._keystore.wrap(dek)),
        }
        self._atomic_write(path, json.dumps(ring).encode("utf-8"))
        return dek

    def rewrap(self, new_keystore: KeyStore) -> None:
        """Rotate the KEK: re-wrap the existing DEK with a new key store."""

        if not self._enabled or self._dek is None:
            raise CacheDisabled("cache disabled; cannot rewrap")
        if not new_keystore.is_available():
            raise CacheDisabled("new key store unavailable")
        ring = {
            "key_id": new_keystore.key_id,
            "key_version": new_keystore.key_version,
            "wrapped_dek": _b64e(new_keystore.wrap(self._dek)),
        }
        self._atomic_write(self._keyring_path(), json.dumps(ring).encode("utf-8"))
        self._keystore = new_keystore

    # ---- record I/O --------------------------------------------------------

    def _scope_dir(self, scope: MemoryScope) -> str:
        h = hashlib.sha256(scope.partition_key().encode("utf-8")).hexdigest()
        return os.path.join(self._root, h)

    def _record_path(self, scope: MemoryScope, key: str) -> str:
        kh = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return os.path.join(self._scope_dir(scope), f"{kh}.rec")

    def _aad(self, scope: MemoryScope, key: str) -> bytes:
        payload = {"scope": scope.partition_key(), "schema": SCHEMA_VERSION, "key": key}
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _atomic_write(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    def _total_bytes(self) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(self._root):
            for f in files:
                if f.endswith(".rec"):
                    total += os.path.getsize(os.path.join(dirpath, f))
        return total

    # ---- public API --------------------------------------------------------

    def put(
        self, scope: MemoryScope, key: str, value: bytes, *, ttl_seconds: float | None = None
    ) -> None:
        if not self._enabled or self._dek is None:
            raise CacheDisabled("cache disabled; refusing to write plaintext")
        nonce = os.urandom(_NONCE_LEN)
        aad = self._aad(scope, key)
        ct = AESGCM(self._dek).encrypt(nonce, value, aad)
        now = self._clock()
        rec = {
            "key_id": self._keystore.key_id if self._keystore else "",
            "key_version": self._keystore.key_version if self._keystore else 0,
            "nonce": _b64e(nonce),
            "ct": _b64e(ct),
            "aad_sha256": hashlib.sha256(aad).hexdigest(),
            "created_at": now,
            "expires_at": (now + ttl_seconds) if ttl_seconds is not None else None,
            "tombstone": False,
        }
        data = json.dumps(rec).encode("utf-8")
        path = self._record_path(scope, key)
        old_size = self._file_size(path)  # O(1) stat, not an O(N) walk
        if self._bytes - old_size + len(data) > self._max_bytes:
            raise CacheFull("cache size bound exceeded")
        self._atomic_write(path, data)
        self._bytes += len(data) - old_size

    def get(self, scope: MemoryScope, key: str) -> bytes | None:
        if not self._enabled or self._dek is None:
            raise CacheDisabled("cache disabled")
        path = self._record_path(scope, key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as fh:
                rec = json.loads(fh.read().decode("utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
            self._quarantine(path)
            return None
        if rec.get("tombstone"):
            return None
        exp = rec.get("expires_at")
        if exp is not None and self._clock() >= exp:
            self._delete_path(path)
            return None
        aad = self._aad(scope, key)
        try:
            return AESGCM(self._dek).decrypt(_b64d(rec["nonce"]), _b64d(rec["ct"]), aad)
        except (InvalidTag, ValueError, KeyError, TypeError):
            # tamper, corruption, or a record from a different scope/key:
            # fail closed, quarantine.
            self._quarantine(path)
            return None

    def delete(self, scope: MemoryScope, key: str) -> None:
        if not self._enabled:
            raise CacheDisabled("cache disabled")
        self._delete_path(self._record_path(scope, key))

    def tombstone(self, scope: MemoryScope, key: str) -> None:
        """Write an erasure tombstone. Full-cache behaviour is explicit.

        A tombstone is a written record, so it is subject to ``max_bytes`` exactly
        like :meth:`put`. If it cannot fit, this raises :class:`CacheFull`, the
        previous record and the size counter are left unchanged, and erasure is NOT
        reported as successful. To free space and erase when the cache is full, use
        :meth:`delete` or :meth:`purge_scope`, which only remove records.
        """

        if not self._enabled:
            raise CacheDisabled("cache disabled")
        now = self._clock()
        rec = {"tombstone": True, "created_at": now, "expires_at": None,
                "nonce": "", "ct": "", "aad_sha256": "", "key_id": "", "key_version": 0}
        data = json.dumps(rec).encode("utf-8")
        path = self._record_path(scope, key)
        old_size = self._file_size(path)  # O(1) stat
        if self._bytes - old_size + len(data) > self._max_bytes:
            raise CacheFull("cache size bound exceeded; tombstone not written (erasure not applied)")
        self._atomic_write(path, data)
        self._bytes += len(data) - old_size

    def purge_scope(self, scope: MemoryScope) -> None:
        """Erasure: remove every record under a scope partition."""

        d = self._scope_dir(scope)
        if os.path.isdir(d):
            for f in os.listdir(d):
                self._delete_path(os.path.join(d, f))

    def sweep_expired(self) -> None:
        """On-demand O(N) reclamation of expired records (NOT on the put hot path).

        Expiry is enforced lazily on get(); this is an optional maintenance sweep.
        """

        self._evict_expired()

    def size_bytes(self) -> int:
        """Current tracked size of .rec records (incrementally maintained)."""

        return self._bytes

    # ---- internals ---------------------------------------------------------

    def _file_size(self, path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _delete_path(self, path: str) -> None:
        size = self._file_size(path) if path.endswith(".rec") else 0
        try:
            os.remove(path)
            self._bytes -= size
        except OSError:
            pass

    def _quarantine(self, path: str) -> None:
        qdir = os.path.join(self._root, ".quarantine")
        os.makedirs(qdir, exist_ok=True)
        size = self._file_size(path) if path.endswith(".rec") else 0
        try:
            os.replace(path, os.path.join(qdir, f"{os.path.basename(path)}.{os.urandom(4).hex()}"))
            self._bytes -= size  # moved out of the .rec tree
        except OSError:
            self._delete_path(path)

    def _evict_expired(self) -> None:
        now = self._clock()
        for dirpath, _dirs, files in os.walk(self._root):
            for f in files:
                if not f.endswith(".rec"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    with open(p, encoding="utf-8") as fh:
                        rec = json.load(fh)
                except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
                    continue
                exp = rec.get("expires_at")
                if exp is not None and now >= exp:
                    self._delete_path(p)


def open_cache(
    root_dir: str,
    keystore: KeyStore | None,
    **kw: object,
) -> EncryptedScopedCache:
    """Open a cache; if no key store is available it comes back DISABLED."""

    return EncryptedScopedCache(root_dir, keystore, **kw)  # type: ignore[arg-type]
