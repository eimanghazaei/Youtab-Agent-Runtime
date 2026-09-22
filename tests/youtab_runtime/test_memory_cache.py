from __future__ import annotations

import os

import pytest

from youtab_runtime.memory import (
    CacheDisabled,
    CacheFull,
    DpapiKeyStore,
    EncryptedScopedCache,
    MemoryScope,
    ProvisionedKeyStore,
    RevokedKeyError,
    ScopeAdmission,
)

from .helpers import keypair, signed_envelope


def _scope(tenant: str = "tenant-alpha", ws: str = "ws-sales") -> MemoryScope:
    private, _ = keypair()
    nonce = "nonce-" + (tenant + ws).encode().hex()[:16].ljust(16, "0")
    env = signed_envelope(private, tenant_id=tenant, nonce=nonce)
    return MemoryScope.from_admission(
        env, ScopeAdmission(organization_id="org-acme", workspace_id=ws,
                            agent_id="agent-01", run_id="run-1"))


def _ks() -> ProvisionedKeyStore:
    return ProvisionedKeyStore(b"\x01" * 32, key_id="test-kek", key_version=1)


def test_roundtrip_encrypts_at_rest(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    assert cache.enabled
    s = _scope()
    cache.put(s, "prefs", b"user likes metric units")
    assert cache.get(s, "prefs") == b"user likes metric units"
    # plaintext must NOT appear on disk
    blob = b""
    for dp, _d, files in os.walk(str(tmp_path)):
        for f in files:
            blob += open(os.path.join(dp, f), "rb").read()
    assert b"metric units" not in blob


def test_cross_scope_lookup_returns_nothing(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    a = _scope(tenant="tenant-alpha")
    b = _scope(tenant="tenant-beta")
    cache.put(a, "k", b"alpha-secret")
    assert cache.get(b, "k") is None       # different tenant
    other_ws = _scope(tenant="tenant-alpha", ws="ws-eng")
    assert cache.get(other_ws, "k") is None  # different workspace
    assert cache.get(a, "k") == b"alpha-secret"


def test_tamper_is_detected_and_quarantined(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    s = _scope()
    cache.put(s, "k", b"payload")
    # corrupt the ciphertext byte in the record file
    path = cache._record_path(s, "k")  # type: ignore[attr-defined]
    data = bytearray(open(path, "rb").read())
    data[-10] ^= 0xFF
    open(path, "wb").write(bytes(data))
    assert cache.get(s, "k") is None  # fail closed
    assert os.path.isdir(os.path.join(str(tmp_path), ".quarantine"))


def test_ttl_expiry(tmp_path) -> None:
    t = {"now": 1000.0}
    cache = EncryptedScopedCache(str(tmp_path), _ks(), clock=lambda: t["now"])
    s = _scope()
    cache.put(s, "k", b"temp", ttl_seconds=10)
    assert cache.get(s, "k") == b"temp"
    t["now"] = 1011.0
    assert cache.get(s, "k") is None


def test_tombstone_and_erasure(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    s = _scope()
    cache.put(s, "k", b"v")
    cache.tombstone(s, "k")
    assert cache.get(s, "k") is None
    cache.put(s, "k2", b"v2")
    cache.purge_scope(s)
    assert cache.get(s, "k2") is None


def test_disabled_when_no_keystore_never_writes_plaintext(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), None)
    assert not cache.enabled
    assert cache.status() == "disabled_no_keystore"
    with pytest.raises(CacheDisabled):
        cache.put(_scope(), "k", b"v")


def test_revoked_key_disables_cache_and_refuses_reads(tmp_path) -> None:
    ks = _ks()
    cache = EncryptedScopedCache(str(tmp_path), ks)
    s = _scope()
    cache.put(s, "k", b"v")
    # a revoked keystore reports unavailable -> cache opens DISABLED (fail closed,
    # no plaintext), and reads are refused rather than served.
    revoked = ProvisionedKeyStore(b"\x01" * 32, key_id="test-kek", revoked=True)
    disabled = EncryptedScopedCache(str(tmp_path), revoked)
    assert not disabled.enabled
    with pytest.raises(CacheDisabled):
        disabled.get(s, "k")


def test_wrong_kek_cannot_unwrap_and_fails_closed(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    cache.put(_scope(), "k", b"v")
    # a different (available) KEK cannot unwrap the DEK -> raises, no plaintext.
    wrong = ProvisionedKeyStore(b"\x09" * 32, key_id="test-kek", key_version=1)
    with pytest.raises((RevokedKeyError, CacheDisabled, Exception)):
        EncryptedScopedCache(str(tmp_path), wrong)


def test_kek_rotation_rewrap_preserves_data(tmp_path) -> None:
    ks1 = ProvisionedKeyStore(b"\x01" * 32, key_id="kek1", key_version=1)
    cache = EncryptedScopedCache(str(tmp_path), ks1)
    s = _scope()
    cache.put(s, "k", b"survives-rotation")
    ks2 = ProvisionedKeyStore(b"\x02" * 32, key_id="kek2", key_version=2)
    cache.rewrap(ks2)
    # reopen with the NEW keystore only -> DEK unwraps, data intact
    reopened = EncryptedScopedCache(str(tmp_path), ks2)
    assert reopened.get(s, "k") == b"survives-rotation"


def test_bounded_size_refuses_overflow(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=200)
    s = _scope()
    with pytest.raises(CacheFull):
        cache.put(s, "big", b"x" * 500)


def test_atomic_write_leaves_no_partial_on_disk(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    s = _scope()
    cache.put(s, "k", b"v")
    # no leftover temp files
    leftover = [f for _dp, _d, files in os.walk(str(tmp_path)) for f in files if ".tmp-" in f]
    assert leftover == []


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_dpapi_keystore_roundtrip_on_windows(tmp_path) -> None:
    ks = DpapiKeyStore()
    assert ks.is_available()
    cache = EncryptedScopedCache(str(tmp_path), ks)
    s = _scope()
    cache.put(s, "k", b"dpapi-protected")
    assert cache.get(s, "k") == b"dpapi-protected"
    reopened = EncryptedScopedCache(str(tmp_path), DpapiKeyStore())
    assert reopened.get(s, "k") == b"dpapi-protected"
