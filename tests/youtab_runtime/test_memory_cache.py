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


def test_size_counter_matches_full_recount(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    s = _scope()
    for i in range(20):
        cache.put(s, f"k{i}", b"payload" * (i + 1))
    cache.delete(s, "k3")
    cache.tombstone(s, "k4")
    # incremental counter must equal a fresh full walk
    assert cache.size_bytes() == cache._total_bytes()  # type: ignore[attr-defined]
    # reopening recomputes the same size from disk
    reopened = EncryptedScopedCache(str(tmp_path), _ks())
    assert reopened.size_bytes() == cache.size_bytes()


def test_put_latency_does_not_grow_with_store_size(tmp_path) -> None:
    import time
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    s = _scope()

    def batch(prefix: str, n: int) -> float:
        t0 = time.perf_counter()
        for i in range(n):
            cache.put(s, f"{prefix}{i}", b"x" * 256)
        return (time.perf_counter() - t0) / n

    early = batch("e", 40)          # store ~empty
    for i in range(400):            # grow the store
        cache.put(s, f"fill{i}", b"x" * 256)
    late = batch("l", 40)           # store has 400+ records
    # O(1) put: late per-op must NOT be a large multiple of early (was ~6x, O(N)).
    assert late <= early * 3 + 0.005


def _live_bytes(root) -> int:
    total = 0
    for dp, _dd, files in os.walk(str(root)):
        for f in files:
            if f.endswith(".rec"):
                total += os.path.getsize(os.path.join(dp, f))
    return total


def _counter_matches(cache, root) -> bool:
    return cache.size_bytes() == _live_bytes(root) == cache._total_bytes()  # type: ignore[attr-defined]


def test_interrupted_atomic_write_does_not_miscount(tmp_path, monkeypatch) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    s = _scope()
    cache.put(s, "k0", b"first")
    before = cache.size_bytes()
    # force the atomic write to fail mid-put: the size counter must NOT advance
    monkeypatch.setattr(cache, "_atomic_write",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        cache.put(s, "k1", b"x" * 999)
    assert cache.size_bytes() == before
    assert _counter_matches(cache, tmp_path)  # counter == actual live bytes


def test_ttl_expiry_updates_size_counter(tmp_path) -> None:
    t = {"now": 100.0}
    cache = EncryptedScopedCache(str(tmp_path), _ks(), clock=lambda: t["now"])
    s = _scope()
    cache.put(s, "keep", b"stay")
    cache.put(s, "temp", b"gone", ttl_seconds=10)
    assert _counter_matches(cache, tmp_path)
    t["now"] = 200.0
    assert cache.get(s, "temp") is None       # lazy expiry deletes on read
    assert _counter_matches(cache, tmp_path)  # size reflects the removal
    assert cache.get(s, "keep") == b"stay"


def test_sweep_expired_reclaims_and_updates_size(tmp_path) -> None:
    t = {"now": 100.0}
    cache = EncryptedScopedCache(str(tmp_path), _ks(), clock=lambda: t["now"])
    s = _scope()
    for i in range(10):
        cache.put(s, f"t{i}", b"x" * 100, ttl_seconds=5)
    cache.put(s, "perm", b"y" * 100)
    full = cache.size_bytes()
    assert full > 0
    t["now"] = 200.0
    cache.sweep_expired()
    assert cache.size_bytes() < full
    assert _counter_matches(cache, tmp_path)
    assert cache.get(s, "perm") == b"y" * 100


def test_delete_and_tombstone_update_size(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    s = _scope()
    cache.put(s, "a", b"aaa")
    cache.put(s, "b", b"bbb")
    cache.delete(s, "a")
    assert _counter_matches(cache, tmp_path)
    cache.tombstone(s, "b")
    assert _counter_matches(cache, tmp_path)  # tombstone rec still counts, tracked
    cache.purge_scope(s)
    assert cache.size_bytes() == 0
    assert _counter_matches(cache, tmp_path)


def test_quarantine_updates_size(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    s = _scope()
    cache.put(s, "k", b"payload")
    before = cache.size_bytes()
    path = cache._record_path(s, "k")  # type: ignore[attr-defined]
    data = bytearray(open(path, "rb").read()); data[-10] ^= 0xFF
    open(path, "wb").write(bytes(data))
    assert cache.get(s, "k") is None  # tamper -> quarantine
    assert cache.size_bytes() < before
    assert _counter_matches(cache, tmp_path)


def test_reopen_recomputes_size_and_enforces_bound(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks())
    s = _scope()
    for i in range(20):
        cache.put(s, f"k{i}", b"x" * 200)
    cache.delete(s, "k0")
    size = cache.size_bytes()
    reopened = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=size + 100)
    assert reopened.size_bytes() == size == _live_bytes(tmp_path)
    with pytest.raises(CacheFull):  # bound honored immediately after reopen
        reopened.put(s, "big", b"z" * 500)


def test_bound_never_exceeded_across_many_puts(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=20_000)
    s = _scope()
    stored = 0
    for i in range(500):
        try:
            cache.put(s, f"k{i}", b"x" * 256)
            stored += 1
        except CacheFull:
            pass
        assert cache.size_bytes() <= 20_000            # invariant on every step
        assert _live_bytes(tmp_path) <= 20_000
    assert stored > 0 and stored < 500                 # some accepted, some refused


def test_concurrency_is_unsupported_single_writer_contract(tmp_path) -> None:
    # The size counter is per-INSTANCE and in-memory; the cache is a single-writer,
    # single-instance contract. Two instances over the same directory do NOT share
    # the counter, so the bound is only guaranteed per-instance. This test PROVES the
    # limitation rather than claiming concurrent safety.
    s = _scope()
    a = EncryptedScopedCache(str(tmp_path), _ks())
    a.put(s, "ka", b"x" * 300)
    b = EncryptedScopedCache(str(tmp_path), _ks())  # second instance, same dir
    a_view = a.size_bytes()
    b.put(s, "kb", b"y" * 300)
    # instance A's counter does NOT see B's write (stale) -> not concurrency-safe
    assert a.size_bytes() == a_view
    # actual live bytes exceed either single-instance view
    assert _live_bytes(tmp_path) > a.size_bytes()
    # a fresh reopen recomputes the true size from disk
    fresh = EncryptedScopedCache(str(tmp_path), _ks())
    assert fresh.size_bytes() == _live_bytes(tmp_path)


def test_tombstone_new_key_when_full_fails_closed(tmp_path) -> None:
    # PR#56 finding: tombstone() must enforce max_bytes. A tombstone for a NEW key
    # in a full cache raises CacheFull; size stays within the bound; erasure not applied.
    cache = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=1)
    s = _scope()
    with pytest.raises(CacheFull):
        cache.tombstone(s, "new-key")
    assert cache.size_bytes() <= 1
    assert _live_bytes(tmp_path) <= 1


def test_tombstone_preserves_previous_record_and_counter_when_full(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=400)
    s = _scope()
    cache.put(s, "keep", b"x" * 100)
    before_size = cache.size_bytes()
    before_val = cache.get(s, "keep")
    with pytest.raises(CacheFull):
        cache.tombstone(s, "unrelated-new-key")  # would exceed the tiny bound
    assert cache.get(s, "keep") == before_val    # previous record preserved
    assert cache.size_bytes() == before_size     # counter unchanged
    assert _counter_matches(cache, tmp_path)


def test_tombstone_replacing_existing_key_fits_and_shrinks(tmp_path) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=10_000)
    s = _scope()
    cache.put(s, "k", b"x" * 5000)
    big = cache.size_bytes()
    cache.tombstone(s, "k")  # replaces a large record with a tiny tombstone -> fits
    assert cache.size_bytes() < big
    assert cache.get(s, "k") is None
    assert _counter_matches(cache, tmp_path)


def test_tombstone_at_capacity_boundary(tmp_path) -> None:
    # Size a bound so exactly one tombstone-for-a-new-key fits, then one more does not.
    cache = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=10_000)
    s = _scope()
    cache.tombstone(s, "t1")            # fits (empty cache)
    one = cache.size_bytes()
    tight = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=one)  # exactly full
    with pytest.raises(CacheFull):
        tight.tombstone(s, "t2")        # a second new tombstone overflows
    assert tight.size_bytes() <= one


def test_tombstone_interrupted_write_does_not_miscount(tmp_path, monkeypatch) -> None:
    cache = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=10_000)
    s = _scope()
    cache.put(s, "k", b"payload")
    before = cache.size_bytes()
    monkeypatch.setattr(cache, "_atomic_write",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        cache.tombstone(s, "k2")
    assert cache.size_bytes() == before
    assert _counter_matches(cache, tmp_path)


def test_delete_and_purge_still_erase_when_full(tmp_path) -> None:
    # Erasure that FREES space must work even at capacity (delete/purge remove files).
    cache = EncryptedScopedCache(str(tmp_path), _ks(), max_bytes=2_000)
    s = _scope()
    cache.put(s, "a", b"x" * 400)
    cache.put(s, "b", b"y" * 400)
    # cache is near/at bound; delete and purge must still succeed
    cache.delete(s, "a")
    assert cache.get(s, "a") is None
    cache.purge_scope(s)
    assert cache.get(s, "b") is None
    assert cache.size_bytes() == 0
    assert _counter_matches(cache, tmp_path)


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
