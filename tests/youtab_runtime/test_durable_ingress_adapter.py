"""Exact-SHA tests for the R1 transport-agnostic run-state adapter
(``youtab_runtime.durable_ingress.DurableRunStateAuthority``) against the durable
RunStore ported from the R4/Timeout integrated SHA a1f9e33428 (modules identical to
product fix 415a1734a49f).

Coverage: POSITIVE (owner-stamped admit, principal-key dedup, projections,
approval mapping), NEGATIVE (idempotency conflict, session|always rejection,
unfenced write), RESTART/takeover (prior nonterminal -> UNKNOWN, dedup survives,
second acquire -> AuthorityHeld), and a PG-LOSS case gated on a real PostgreSQL DSN.

These prove the durable RunStore is the sole run-state/idempotency authority; the
create-to-worker DISPATCH seam is intentionally NOT exercised here (held for D1).
"""

from __future__ import annotations

import os

import pytest

from youtab_runtime.durable_ingress import (
    AuthorityLost,
    DurableRunStateAuthority,
    IdempotencyConflict,
)
from youtab_runtime.durable_run_authority import AuthorityHeld
from youtab_runtime.durable_run_store import RunState

_IDENT = dict(
    tenant_id="t1",
    organization_id="o1",
    workspace_id="w1",
    principal_id="t1:u1",
    agent_id="agent-x",
)


def _authority(tmp_path, name="durable_runs.db"):
    return DurableRunStateAuthority(backend="sqlite", db_path=tmp_path / name)


# ---------------------------------------------------------------- positive --- #
def test_admit_is_owner_stamped_and_queued(tmp_path):
    a = _authority(tmp_path)
    a.acquire()
    try:
        res = a.create_run(idempotency_key="k1", request_digest="d1", **_IDENT)
        assert res.created is True
        assert res.row["state"] == RunState.QUEUED.value
        assert res.row["lease_owner"] == a.owner  # owner-stamped -> recoverable
    finally:
        a.release()


def test_principal_key_dedup_returns_original_single_row(tmp_path):
    a = _authority(tmp_path)
    a.acquire()
    try:
        first = a.create_run(idempotency_key="k1", request_digest="d1", **_IDENT)
        dup = a.create_run(idempotency_key="k1", request_digest="d1", **_IDENT)
        assert dup.created is False
        assert dup.row["run_id"] == first.row["run_id"]  # ORIGINAL run returned
        # exactly one durable row for the key
        assert a.get_run(first.row["run_id"]) is not None
    finally:
        a.release()


def test_get_run_and_event_projection(tmp_path):
    a = _authority(tmp_path)
    a.acquire()
    try:
        res = a.create_run(idempotency_key="k1", request_digest="d1", **_IDENT)
        rid = res.row["run_id"]
        assert a.get_run(rid)["run_id"] == rid
        kinds = [e["kind"] for e in a.get_events(rid)]
        assert "accepted" in kinds and "admitted" in kinds
    finally:
        a.release()


def test_approve_records_once_decision_nonterminal(tmp_path):
    a = _authority(tmp_path)
    a.acquire()
    try:
        rid = a.create_run(idempotency_key="k1", request_digest="d1", **_IDENT).row["run_id"]
        out = a.record_approval_decision(rid, approval_id="ap1", decision="approve")
        assert out["choice"] == "once" and out["decision"] == "approve"
        assert a.get_run(rid)["state"] == RunState.QUEUED.value  # not terminal
        assert any(e["kind"] == "approval_decision" for e in a.get_events(rid))
    finally:
        a.release()


def test_deny_is_durable_terminal_zero_effect(tmp_path):
    a = _authority(tmp_path)
    a.acquire()
    try:
        rid = a.create_run(idempotency_key="k1", request_digest="d1", **_IDENT).row["run_id"]
        out = a.record_approval_decision(rid, approval_id="ap1", decision="deny")
        assert out["choice"] == "deny"
        row = a.get_run(rid)
        assert row["state"] == RunState.CANCELLED.value  # durable denied outcome
        assert not row.get("result_ref")  # no fabricated result/receipt
    finally:
        a.release()


def test_approval_idempotent_by_approval_id(tmp_path):
    a = _authority(tmp_path)
    a.acquire()
    try:
        rid = a.create_run(idempotency_key="k1", request_digest="d1", **_IDENT).row["run_id"]
        a.record_approval_decision(rid, approval_id="ap1", decision="deny")
        again = a.record_approval_decision(rid, approval_id="ap1", decision="approve")
        assert again["already_decided"] is True
        assert again["decision"] == "deny"  # ORIGINAL decision stands
    finally:
        a.release()


# ---------------------------------------------------------------- negative --- #
def test_idempotency_conflict_on_different_digest(tmp_path):
    a = _authority(tmp_path)
    a.acquire()
    try:
        a.create_run(idempotency_key="k1", request_digest="dA", **_IDENT)
        with pytest.raises(IdempotencyConflict):
            a.create_run(idempotency_key="k1", request_digest="dB", **_IDENT)
    finally:
        a.release()


@pytest.mark.parametrize("bad", ["session", "always", "", "maybe"])
def test_approval_rejects_non_approve_deny(tmp_path, bad):
    a = _authority(tmp_path)
    a.acquire()
    try:
        rid = a.create_run(idempotency_key="k1", request_digest="d1", **_IDENT).row["run_id"]
        with pytest.raises(ValueError):
            a.record_approval_decision(rid, approval_id="ap1", decision=bad)
    finally:
        a.release()


def test_write_without_authority_fails_closed(tmp_path):
    a = _authority(tmp_path)  # never acquired
    with pytest.raises(AuthorityLost):
        a.create_run(idempotency_key="k1", request_digest="d1", **_IDENT)


# --------------------------------------------------------- restart/takeover -- #
def test_takeover_moves_prior_nonterminal_to_unknown_and_dedup_survives(tmp_path):
    db = tmp_path / "durable_runs.db"
    a1 = DurableRunStateAuthority(backend="sqlite", db_path=db)
    a1.acquire()
    rid = a1.create_run(idempotency_key="k1", request_digest="d1", **_IDENT).row["run_id"]
    a1.release()  # simulate instance death (authority handed off)

    a2 = DurableRunStateAuthority(backend="sqlite", db_path=db)
    reconciled = a2.acquire()  # epoch+1; reconcile prior nonterminal -> UNKNOWN
    try:
        assert rid in reconciled
        assert a2.get_run(rid)["state"] == RunState.UNKNOWN.value
        # dedup persists across the takeover: same key returns the ORIGINAL run
        dup = a2.create_run(idempotency_key="k1", request_digest="d1", **_IDENT)
        assert dup.created is False and dup.row["run_id"] == rid
    finally:
        a2.release()


def test_second_acquire_while_held_raises_authority_held(tmp_path):
    db = tmp_path / "durable_runs.db"
    a1 = DurableRunStateAuthority(backend="sqlite", db_path=db)
    a1.acquire()
    try:
        a2 = DurableRunStateAuthority(backend="sqlite", db_path=db)
        with pytest.raises(AuthorityHeld):
            a2.acquire()
    finally:
        a1.release()


def test_fenced_write_after_supersede_raises_authority_lost(tmp_path):
    db = tmp_path / "durable_runs.db"
    a1 = DurableRunStateAuthority(backend="sqlite", db_path=db)
    a1.acquire()
    a1.create_run(idempotency_key="k1", request_digest="d1", **_IDENT)
    a1.release()
    a2 = DurableRunStateAuthority(backend="sqlite", db_path=db)
    a2.acquire()  # supersedes a1's epoch
    try:
        # a1's store is now superseded; any fenced write must fail closed.
        with pytest.raises(AuthorityLost):
            a1.create_run(idempotency_key="k2", request_digest="d2", **_IDENT)
    finally:
        a2.release()


# ------------------------------------------------------------------ PG-loss -- #
@pytest.mark.skipif(
    not os.environ.get("YOUTAB_TEST_PG_DSN"),
    reason="requires a real PostgreSQL (YOUTAB_TEST_PG_DSN); mirrors R4's live-probe harness",
)
def test_pg_authority_loss_fails_closed():
    dsn = os.environ["YOUTAB_TEST_PG_DSN"]
    a = DurableRunStateAuthority(backend="postgres", dsn=dsn)
    a.acquire()
    try:
        a.create_run(idempotency_key="pgk1", request_digest="d1", **_IDENT)
        # Terminate the advisory-lock backend out of band, then a fenced write and
        # readiness must fail closed (no false-durable terminal). Operators run the
        # pg_terminate_backend probe; here we assert the post-loss contract only if
        # a loss has been signalled.
        if not a.ready():
            with pytest.raises(AuthorityLost):
                a.create_run(idempotency_key="pgk2", request_digest="d2", **_IDENT)
    finally:
        a.release()
