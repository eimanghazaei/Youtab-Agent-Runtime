"""Unit tests for the signed-command layer (AR-PROD-01, Milestone 1).

Covers signature / timestamp / expiry / nonce / replay verification in
``youtab_agent_cli.runtime_command_auth`` — no server, fast.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from youtab_agent_cli import runtime_command_auth as rca

SECRET = "x" * 50  # >= 43 chars so it passes the length floor
CORR = "cid-0123456789abcdef0123456789abcdef"  # valid per the shared regex


def _sign(method, path, tenant, user, body, ts, nonce, secret=SECRET, correlation=CORR):
    canonical = rca.canonical_string(
        method=method, path=path, tenant=tenant, user=user,
        timestamp=str(ts), nonce=nonce, body=body, correlation=correlation,
    )
    return rca.compute_signature(secret, canonical)


def _headers(sig, ts, nonce, correlation=CORR):
    h = {
        rca.SIGNATURE_HEADER: sig,
        rca.TIMESTAMP_HEADER: str(ts),
        rca.NONCE_HEADER: nonce,
    }
    if correlation is not None:
        h[rca.CORRELATION_HEADER] = correlation
    return h


def test_valid_command_passes_and_records_nonce():
    store = rca._MemoryNonceStore()
    now = int(time.time())
    body = b'{"agent":"default","task":"hello"}'
    sig = _sign("POST", "/api/runtime/v1/runs", "tenantA", "userA", body, now, "n1")
    rca.verify_command(
        method="POST", path="/api/runtime/v1/runs", tenant="tenantA", user="userA",
        body=body, headers=_headers(sig, now, "n1"), secret=SECRET, store=store, now=now,
    )
    assert store.seen("n1") is True


def test_replayed_nonce_rejected():
    store = rca._MemoryNonceStore()
    now = int(time.time())
    body = b"{}"
    sig = _sign("POST", "/p", "t", "u", body, now, "dup")
    kwargs = dict(method="POST", path="/p", tenant="t", user="u", body=body,
                  headers=_headers(sig, now, "dup"), secret=SECRET, store=store, now=now)
    rca.verify_command(**kwargs)
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(**kwargs)
    assert ei.value.code == "replayed"
    assert ei.value.http_status == 409


def test_expired_timestamp_rejected():
    store = rca._MemoryNonceStore()
    now = int(time.time())
    old = now - (rca.DEFAULT_WINDOW_SECONDS + 60)
    body = b"{}"
    sig = _sign("POST", "/p", "t", "u", body, old, "n")
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(
            method="POST", path="/p", tenant="t", user="u", body=body,
            headers=_headers(sig, old, "n"), secret=SECRET, store=store, now=now,
        )
    assert ei.value.code == "expired"


def test_tampered_body_rejected():
    store = rca._MemoryNonceStore()
    now = int(time.time())
    sig = _sign("POST", "/p", "t", "u", b"original", now, "n")
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(
            method="POST", path="/p", tenant="t", user="u", body=b"TAMPERED",
            headers=_headers(sig, now, "n"), secret=SECRET, store=store, now=now,
        )
    assert ei.value.code == "bad_signature"
    # A bad-signature probe must NOT burn the nonce.
    assert store.seen("n") is False


def test_signature_bound_to_tenant_and_user():
    """A command signed for (tenantA,userA) can't be replayed as another identity."""
    store = rca._MemoryNonceStore()
    now = int(time.time())
    body = b"{}"
    sig = _sign("POST", "/p", "tenantA", "userA", body, now, "n")
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(
            method="POST", path="/p", tenant="tenantB", user="userA", body=body,
            headers=_headers(sig, now, "n"), secret=SECRET, store=store, now=now,
        )
    assert ei.value.code == "bad_signature"


def test_missing_headers_rejected():
    store = rca._MemoryNonceStore()
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(
            method="POST", path="/p", tenant="t", user="u", body=b"{}",
            headers={}, secret=SECRET, store=store,
        )
    assert ei.value.code == "missing_signature"


def test_unavailable_secret_fails_closed():
    store = rca._MemoryNonceStore()
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(
            method="POST", path="/p", tenant="t", user="u", body=b"{}",
            headers=_headers("x", 1, "n"), secret="", store=store,
        )
    assert ei.value.code == "secret_unavailable"
    assert ei.value.http_status == 503


def test_sqlite_store_persists_and_prunes(tmp_path):
    db = str(tmp_path / "nonces.db")
    store = rca.SqliteNonceStore(db, window_seconds=1)
    now = int(time.time())
    store.record("fresh", now)
    assert store.seen("fresh") is True
    # An old nonce is pruned on the next write.
    store.record("old", now - 100)
    store.record("trigger", now)
    assert store.seen("old") is False


# --- WAVE-30G: atomic single-use nonce under concurrency / restart / fd hygiene ---

def _count_concurrent_claims(store, *, threads: int = 32) -> int:
    """N threads race to ``claim`` the SAME nonce; return how many succeeded.

    A correct single-use store returns exactly 1; a check-then-act (TOCTOU)
    store returns >1. The barrier maximises the race so every thread reaches
    the check at once.
    """
    nonce = "nonce-concurrent-0001"
    now = int(time.time())
    barrier = threading.Barrier(threads)
    accepted: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        if store.claim(nonce, now):
            with lock:
                accepted.append(1)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return len(accepted)


def test_claim_admits_exactly_once_memory():
    # Meaningful even single-process: a lockless dict check-then-add would
    # double-admit under the GIL; the in-store lock makes claim atomic.
    assert _count_concurrent_claims(rca._MemoryNonceStore()) == 1


def test_claim_admits_exactly_once_sqlite_threads(tmp_path):
    # Threads through ONE store instance: proves the per-process lock path.
    # Cross-process atomicity (the property that does NOT depend on that lock)
    # is proven separately in test_claim_atomic_across_processes below.
    store = rca.SqliteNonceStore(str(tmp_path / "n.db"))
    assert _count_concurrent_claims(store) == 1


def _mp_claim_worker(db_path: str, start_at: float, q) -> None:
    """Top-level (spawn-picklable) worker: each process gets its OWN store /
    connection, so the per-process ``threading.Lock`` cannot serialise across
    them — only the atomic ``INSERT OR IGNORE`` + ``changes()`` on the shared DB
    file can enforce single-use. Busy-wait to a shared start for a real race."""
    store = rca.SqliteNonceStore(db_path)
    while time.time() < start_at:
        pass
    q.put(1 if store.claim("mp-shared-nonce", int(time.time())) else 0)


def test_claim_atomic_across_processes(tmp_path):
    """N SEPARATE processes race to claim one nonce on a shared DB file; exactly
    one wins. This is the property the cross-process replay guarantee rests on
    (the thread test above cannot prove it — its single lock would mask a
    non-atomic INSERT)."""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    db = str(tmp_path / "mp.db")
    rca.SqliteNonceStore(db)  # pre-create the table so workers race only on claim
    n = 8
    q = ctx.Queue()
    start_at = time.time() + 2.0  # generous headroom for spawn + import on CI
    procs = [
        ctx.Process(target=_mp_claim_worker, args=(db, start_at, q))
        for _ in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    results = [q.get() for _ in range(n)]
    assert sum(results) == 1  # exactly one process claimed the nonce


def test_claim_fails_closed_on_sqlite_error(tmp_path, monkeypatch):
    """A store error during the atomic claim must fail CLOSED (503), never admit
    the command and never surface as an unhandled 500."""
    store = rca.SqliteNonceStore(str(tmp_path / "n.db"))

    def _boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_connect", _boom)
    with pytest.raises(rca.CommandAuthError) as ei:
        store.claim("n", int(time.time()))
    assert ei.value.code == "nonce_store_unavailable"
    assert ei.value.http_status == 503


def test_claim_rejects_replay_across_restart(tmp_path):
    """A fresh store on the same DB file models a worker after restart / a
    sibling process: a nonce claimed by the first must be rejected by the
    second (durable, cross-process single-use)."""
    db = str(tmp_path / "n.db")
    now = int(time.time())
    first = rca.SqliteNonceStore(db)
    assert first.claim("shared-nonce", now) is True
    second = rca.SqliteNonceStore(db)  # "restart" / sibling process
    assert second.claim("shared-nonce", now) is False


def test_verify_command_concurrent_same_nonce_admits_once(tmp_path):
    """End-to-end: many concurrent fully-valid commands sharing one nonce yield
    exactly one acceptance; the rest are rejected 409 replayed."""
    store = rca.SqliteNonceStore(str(tmp_path / "n.db"))
    now = int(time.time())
    body = b"{}"
    sig = _sign("POST", "/p", "t", "u", body, now, "dup")
    threads = 24
    barrier = threading.Barrier(threads)
    ok: list[int] = []
    replayed: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            rca.verify_command(
                method="POST", path="/p", tenant="t", user="u", body=body,
                headers=_headers(sig, now, "dup"), secret=SECRET, store=store, now=now,
            )
            with lock:
                ok.append(1)
        except rca.CommandAuthError as exc:
            if exc.code == "replayed":
                with lock:
                    replayed.append(1)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(ok) == 1
    assert len(replayed) == threads - 1


# --- WAVE-30H R1: canonical v2 golden vectors (cross-repo conformance oracle) ---
# These MUST match docs/architecture/CANONICAL-SIGNATURE-GRANT.md §4 and the
# gateway connector byte-for-byte. TEST-ONLY values.

GOLDEN_SECRET = "test-secret-do-not-use-in-prod-000000000000000"  # len 46
GOLDEN_BODY = b'{"agent":"default","task":"hello"}'
GOLDEN_HMAC = "d2f948992594ac90d88cf16b2feacbfb9eee20629d4a9c3affc4af08c6d573c0"
GOLDEN_NONCE = "n-0123456789abcdef0123456789abcdef"
GOLDEN_CORR = "cid-0123456789abcdef"
GOLDEN_KEYID = "svc-hmac-2026a"
EMPTY_BODY_HMAC = "ffa3e9a2c7616bccdbbea361850f204dbae602173ef189cc267d87d6562d2234"


def _v2_headers(sig, ts, nonce, correlation, key_id, proto=None):
    return {
        rca.SIGNATURE_HEADER: sig,
        rca.TIMESTAMP_HEADER: str(ts),
        rca.NONCE_HEADER: nonce,
        rca.CORRELATION_HEADER: correlation,
        rca.PROTO_HEADER: proto or rca.CANONICAL_V2,
        rca.KEYID_HEADER: key_id,
    }


def test_canonical_v2_matches_golden_vector():
    canonical = rca.canonical_string_v2(
        method="POST", path="/api/runtime/v1/runs", tenant="tenant-alpha",
        workspace="ws-default", user="user-eiman", timestamp="1700000000",
        nonce=GOLDEN_NONCE, body=GOLDEN_BODY, correlation=GOLDEN_CORR,
        key_id=GOLDEN_KEYID,
    )
    assert rca.compute_signature(GOLDEN_SECRET, canonical) == GOLDEN_HMAC


def test_canonical_v2_empty_body_matches_golden_vector():
    canonical = rca.canonical_string_v2(
        method="POST", path="/api/runtime/v1/runs/run-123/cancel",
        tenant="tenant-alpha", workspace="ws-default", user="user-eiman",
        timestamp="1700000000", nonce="n-ffffffffffffffffffffffffffffffff",
        body=b"", correlation="cid-ffffffffffffffff", key_id=GOLDEN_KEYID,
    )
    assert rca.compute_signature(GOLDEN_SECRET, canonical) == EMPTY_BODY_HMAC


def test_verify_command_v2_accepts_valid_and_binds_new_fields():
    # A non-raising return IS the acceptance proof: the presented signature had to
    # equal the golden HMAC, which only matches if the verify-side canonical v2
    # string (incl. workspace + key_id) is byte-identical to the signer's. (We do
    # not assert store.seen() here — the golden ts is ancient, so the memory
    # store prunes the just-claimed nonce by wall-clock; single-use is proven in
    # the concurrent/replay tests that use a realistic now.)
    store = rca._MemoryNonceStore()
    rca.verify_command(
        method="POST", path="/api/runtime/v1/runs", tenant="tenant-alpha",
        workspace="ws-default", user="user-eiman", body=GOLDEN_BODY,
        headers=_v2_headers(GOLDEN_HMAC, 1700000000, GOLDEN_NONCE, GOLDEN_CORR, GOLDEN_KEYID),
        secret=GOLDEN_SECRET, store=store, now=1700000000,
    )


def test_verify_command_v2_workspace_tamper_fails():
    # workspace is a signed field in v2 — presenting a different workspace than
    # was signed must fail the signature (cross-tenant/workspace lift defence).
    store = rca._MemoryNonceStore()
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(
            method="POST", path="/api/runtime/v1/runs", tenant="tenant-alpha",
            workspace="ws-ATTACKER", user="user-eiman", body=GOLDEN_BODY,
            headers=_v2_headers(GOLDEN_HMAC, 1700000000, GOLDEN_NONCE, GOLDEN_CORR, GOLDEN_KEYID),
            secret=GOLDEN_SECRET, store=store, now=1700000000,
        )
    assert ei.value.code == "bad_signature"
    assert store.seen(GOLDEN_NONCE) is False  # a bad-sig probe must not burn the nonce


def test_verify_command_v2_keyid_tamper_fails():
    store = rca._MemoryNonceStore()
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(
            method="POST", path="/api/runtime/v1/runs", tenant="tenant-alpha",
            workspace="ws-default", user="user-eiman", body=GOLDEN_BODY,
            headers=_v2_headers(GOLDEN_HMAC, 1700000000, GOLDEN_NONCE, GOLDEN_CORR, "svc-OTHER-key"),
            secret=GOLDEN_SECRET, store=store, now=1700000000,
        )
    assert ei.value.code == "bad_signature"


def test_verify_command_v2_missing_keyid_fails_closed():
    store = rca._MemoryNonceStore()
    headers = _v2_headers(GOLDEN_HMAC, 1700000000, GOLDEN_NONCE, GOLDEN_CORR, GOLDEN_KEYID)
    del headers[rca.KEYID_HEADER]
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(
            method="POST", path="/api/runtime/v1/runs", tenant="tenant-alpha",
            workspace="ws-default", user="user-eiman", body=GOLDEN_BODY,
            headers=headers, secret=GOLDEN_SECRET, store=store, now=1700000000,
        )
    assert ei.value.code == "missing_signature"


def test_verify_command_unsupported_proto_fails_closed():
    store = rca._MemoryNonceStore()
    headers = _v2_headers(GOLDEN_HMAC, 1700000000, GOLDEN_NONCE, GOLDEN_CORR, GOLDEN_KEYID,
                          proto="youtab.runtime-sig.v999")
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(
            method="POST", path="/api/runtime/v1/runs", tenant="tenant-alpha",
            workspace="ws-default", user="user-eiman", body=GOLDEN_BODY,
            headers=headers, secret=GOLDEN_SECRET, store=store, now=1700000000,
        )
    assert ei.value.code == "unsupported_proto"
    assert ei.value.http_status == 400


def test_sqlite_store_closes_every_connection(tmp_path, monkeypatch):
    """Every connection SqliteNonceStore opens is deterministically closed —
    ``with sqlite3.connect()`` alone only ends the transaction, leaking the fd
    until GC. Guards the ``contextlib.closing`` wrapping."""
    real_connect = sqlite3.connect
    opened: list[object] = []
    closed = {"n": 0}

    class _Tracked:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def close(self):
            closed["n"] += 1
            return self._conn.close()

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_conn"), name)

        def __setattr__(self, name, value):
            setattr(object.__getattribute__(self, "_conn"), name, value)

        def __enter__(self):
            return self._conn.__enter__()

        def __exit__(self, *a):
            return self._conn.__exit__(*a)

    def _fake_connect(*a, **k):
        conn = _Tracked(real_connect(*a, **k))
        opened.append(conn)
        return conn

    monkeypatch.setattr(rca.sqlite3, "connect", _fake_connect)
    store = rca.SqliteNonceStore(str(tmp_path / "n.db"))  # 1 connect (ctor)
    now = int(time.time())
    store.claim("a", now)
    store.seen("a")
    store.record("b", now)
    assert len(opened) >= 4
    assert closed["n"] == len(opened)  # no leaked connections
