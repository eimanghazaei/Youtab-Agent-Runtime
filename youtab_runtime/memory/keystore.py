"""Platform key-store abstraction for wrapping the cache data-encryption key.

The cache never persists a plaintext key. A :class:`KeyStore` wraps (encrypts) the
cache DEK with a platform-protected key-encryption key:

* Windows: DPAPI (``CryptProtectData``/``CryptUnprotectData``, user scope) via ctypes.
* Provisioned: an explicitly injected 32-byte KEK (e.g. from a secret manager),
  never hardcoded or committed — for Linux/macOS bring-your-own or tests.

If no key store is available the cache stays DISABLED; there is no plaintext
fallback. Keys are never logged. Rotation bumps ``key_version``; a revoked key
refuses to unwrap (fail closed).
"""

from __future__ import annotations

import sys
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KeyStoreUnavailable(RuntimeError):
    """Raised/returned when no platform key store can protect the cache key."""


class RevokedKeyError(RuntimeError):
    """Raised when a wrapped key cannot be unwrapped because the key is revoked."""


@runtime_checkable
class KeyStore(Protocol):
    @property
    def key_id(self) -> str: ...

    @property
    def key_version(self) -> int: ...

    def is_available(self) -> bool: ...

    def wrap(self, data: bytes) -> bytes: ...

    def unwrap(self, blob: bytes) -> bytes: ...


class ProvisionedKeyStore:
    """AES-GCM wrapping with an explicitly provisioned 32-byte KEK.

    The KEK is supplied at construction (from a secret manager / OS keyring), never
    committed. Used on platforms without a native store, and in tests.
    """

    _WRAP_NONCE_LEN = 12

    def __init__(
        self,
        kek: bytes,
        *,
        key_id: str = "provisioned",
        key_version: int = 1,
        revoked: bool = False,
    ) -> None:
        if len(kek) != 32:
            raise ValueError("KEK must be 32 bytes (AES-256)")
        self._aead = AESGCM(kek)
        self._key_id = key_id
        self._key_version = key_version
        self._revoked = revoked

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def key_version(self) -> int:
        return self._key_version

    def is_available(self) -> bool:
        return not self._revoked

    def wrap(self, data: bytes) -> bytes:
        if self._revoked:
            raise RevokedKeyError("key is revoked")
        import os

        nonce = os.urandom(self._WRAP_NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, data, b"youtab-kek-wrap")

    def unwrap(self, blob: bytes) -> bytes:
        if self._revoked:
            raise RevokedKeyError("key is revoked")
        nonce, ct = blob[: self._WRAP_NONCE_LEN], blob[self._WRAP_NONCE_LEN :]
        return self._aead.decrypt(nonce, ct, b"youtab-kek-wrap")


class DpapiKeyStore:
    """Windows DPAPI-backed wrapping (user scope) via ctypes. No extra deps."""

    def __init__(self, *, key_version: int = 1, revoked: bool = False) -> None:
        self._key_version = key_version
        self._revoked = revoked

    @property
    def key_id(self) -> str:
        return "dpapi-user"

    @property
    def key_version(self) -> int:
        return self._key_version

    def is_available(self) -> bool:
        return sys.platform == "win32" and not self._revoked

    # ---- ctypes DPAPI plumbing --------------------------------------------

    @staticmethod
    def _crypt(data: bytes, protect: bool) -> bytes:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        def _to_blob(b: bytes) -> DATA_BLOB:
            buf = ctypes.create_string_buffer(b, len(b))
            return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

        blob_in = _to_blob(data)
        blob_out = DATA_BLOB()
        windll = getattr(ctypes, "windll")  # noqa: B009 — Windows-only attr, hidden from type checker
        crypt32 = windll.crypt32
        kernel32 = windll.kernel32
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        # CRYPTPROTECT_UI_FORBIDDEN = 0x1
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
        if not ok:
            raise KeyStoreUnavailable("DPAPI operation failed")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)

    def wrap(self, data: bytes) -> bytes:
        if self._revoked:
            raise RevokedKeyError("key is revoked")
        if sys.platform != "win32":
            raise KeyStoreUnavailable("DPAPI is Windows-only")
        return self._crypt(data, protect=True)

    def unwrap(self, blob: bytes) -> bytes:
        if self._revoked:
            raise RevokedKeyError("key is revoked")
        if sys.platform != "win32":
            raise KeyStoreUnavailable("DPAPI is Windows-only")
        return self._crypt(blob, protect=False)


def default_keystore() -> KeyStore | None:
    """Return the native platform key store, or None if none is available.

    None means the cache must stay DISABLED (no plaintext fallback). A caller may
    instead pass an explicit :class:`ProvisionedKeyStore`.
    """

    if sys.platform == "win32":
        ks = DpapiKeyStore()
        return ks if ks.is_available() else None
    # macOS Keychain / Linux libsecret wrappers are provisioning-specific and are
    # intentionally not auto-selected here; callers provide a ProvisionedKeyStore.
    return None
