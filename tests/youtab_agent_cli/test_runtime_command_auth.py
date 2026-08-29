"""Unit tests for the signed-command layer (AR-PROD-01, Milestone 1).

Covers signature / timestamp / expiry / nonce / replay verification in
``youtab_agent_cli.runtime_command_auth`` — no server, fast.
"""
from __future__ import annotations

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
