"""D2 approval projection — unit proofs of the worker->durable open seam and the
canonical fenced decide CAS via the process-module seams
(``youtab_runtime.durable_ingress_process``).

These prove, WITHOUT the HTTP layer, that:
  * a worker's approval request opens the SAME durable run's approval under the ONE
    authority (``open_managed_approval`` projects QUEUED->CLAIMED->RUNNING then
    RUNNING->WAITING_APPROVAL, id-bound);
  * the decision is the single-use, id-bound, scope-checked, fenced CAS
    (approve->RUNNING, deny->CANCELLED with no fabricated result);
  * wrong/stale id, duplicate, cross-scope, restart and authority loss all fail
    closed; the durable store is the authoritative replay source.
The product route wiring is proven separately over the REAL signed managed route.
"""

from __future__ import annotations

import pytest

import youtab_runtime.durable_ingress_process as dip
from youtab_runtime.durable_ingress import ApprovalNotOpen, ApprovalScopeMismatch
from youtab_runtime.durable_run_store import RunState

_IDENT = dict(
    tenant_id="t1",
    organization_id="o1",
    workspace_id="w1",
    principal_id="t1:u1",
    agent_id="agent-x",
)
_SCOPE = dict(tenant_id="t1", workspace_id="w1", principal_id="t1:u1")


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_INGRESS", "1")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "sqlite")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_DB_PATH", str(tmp_path / "ingress.db"))
    dip.reset_for_tests()
    try:
        yield tmp_path
    finally:
        dip.reset_for_tests()


def _admit(run_id="t_r1", key="k1", digest="d1"):
    return dip.admit_managed_run(run_id=run_id, idempotency_key=key,
                                 request_digest=digest, **_IDENT)


# ---------------------------------------------------- worker opens durable --- #
def test_worker_request_opens_durable_approval(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_open")
    # the SERVER opens the durable approval on the worker's behalf (projection):
    # QUEUED -> CLAIMED -> RUNNING -> WAITING_APPROVAL, binding the id.
    dip.open_managed_approval("t_open", approval_id="ap1")
    auth = dip.get_ingress_authority()
    assert auth.get_run("t_open")["state"] == RunState.WAITING_APPROVAL.value
    reqs = [e for e in auth.get_events("t_open") if e["kind"] == "approval_request"]
    assert reqs and reqs[-1]["payload"]["approval_id"] == "ap1"
    # exact-id re-open is idempotent (no second request, still WAITING_APPROVAL)
    dip.open_managed_approval("t_open", approval_id="ap1")
    reqs2 = [e for e in auth.get_events("t_open") if e["kind"] == "approval_request"]
    assert len(reqs2) == len(reqs)


# ------------------------------------------------------------- approve/deny -- #
def test_approve_consumes_single_use(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_ap")
    dip.open_managed_approval("t_ap", approval_id="ap1")
    out = dip.decide_managed_approval("t_ap", approval_id="ap1", decision="approve", **_SCOPE)
    assert out["decision"] == "approve"
    auth = dip.get_ingress_authority()
    assert auth.get_run("t_ap")["state"] == RunState.RUNNING.value
    # replay of the same decision is refused (no second consume); durable is the
    # authoritative record so the route reports the ORIGINAL decision instead.
    with pytest.raises(ApprovalNotOpen):
        dip.decide_managed_approval("t_ap", approval_id="ap1", decision="approve", **_SCOPE)
    assert dip.prior_approval_decision("t_ap", "ap1") == "approve"


def test_deny_is_terminal_zero_effect(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_dn")
    dip.open_managed_approval("t_dn", approval_id="ap1")
    out = dip.decide_managed_approval("t_dn", approval_id="ap1", decision="deny", **_SCOPE)
    assert out["decision"] == "deny"
    auth = dip.get_ingress_authority()
    row = auth.get_run("t_dn")
    assert row["state"] == RunState.CANCELLED.value
    assert not row.get("result_ref")  # no fabricated receipt
    assert dip.prior_approval_decision("t_dn", "ap1") == "deny"


# --------------------------------------------------------- wrong/stale/dup --- #
def test_wrong_id_refused_open_survives(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_wr")
    dip.open_managed_approval("t_wr", approval_id="expected")
    with pytest.raises(ApprovalNotOpen):
        dip.decide_managed_approval("t_wr", approval_id="wrong", decision="approve", **_SCOPE)
    auth = dip.get_ingress_authority()
    assert auth.get_run("t_wr")["state"] == RunState.WAITING_APPROVAL.value  # untouched
    # the correct id still decides
    dip.decide_managed_approval("t_wr", approval_id="expected", decision="approve", **_SCOPE)
    assert auth.get_run("t_wr")["state"] == RunState.RUNNING.value


def test_decide_without_open_refused(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_no")  # QUEUED, no open approval
    with pytest.raises(ApprovalNotOpen):
        dip.decide_managed_approval("t_no", approval_id="ap1", decision="approve", **_SCOPE)


# --------------------------------------- gap 1: server-owned early projection - #
def test_reconcile_opens_pending_before_decision(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_pj")
    auth = dip.get_ingress_authority()
    assert auth.get_run("t_pj")["state"] == RunState.QUEUED.value  # stale before poll
    # a server-side observation of the worker's pending request opens it durably,
    # BEFORE any human decision — no /approve, no click.
    dip.reconcile_pending_approval("t_pj", pending_approval_id="ap1")
    row = auth.get_run("t_pj")
    assert row["state"] == RunState.WAITING_APPROVAL.value
    # scoped to the admitting tenant/workspace/principal
    assert (row["tenant_id"], row["workspace_id"], row["principal_id"]) == ("t1", "w1", "t1:u1")
    reqs = [e for e in auth.get_events("t_pj") if e["kind"] == "approval_request"]
    assert reqs[-1]["payload"]["approval_id"] == "ap1"
    # idempotent: a second poll appends no second request
    dip.reconcile_pending_approval("t_pj", pending_approval_id="ap1")
    reqs2 = [e for e in auth.get_events("t_pj") if e["kind"] == "approval_request"]
    assert len(reqs2) == len(reqs)


def test_reconcile_noop_when_nothing_pending(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_np")
    dip.reconcile_pending_approval("t_np", pending_approval_id=None)
    assert dip.get_ingress_authority().get_run("t_np")["state"] == RunState.QUEUED.value


def test_reconcile_noop_when_already_decided(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_dd")
    dip.open_managed_approval("t_dd", approval_id="ap1")
    dip.decide_managed_approval("t_dd", approval_id="ap1", decision="approve", **_SCOPE)
    # a late poll must NOT re-open a decided approval
    dip.reconcile_pending_approval("t_dd", pending_approval_id="ap1")
    assert dip.get_ingress_authority().get_run("t_dd")["state"] == RunState.RUNNING.value


def test_pending_approval_visible_after_restart_without_click(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_re")
    dip.reconcile_pending_approval("t_re", pending_approval_id="ap1")  # -> WAITING_APPROVAL

    dip.reset_for_tests()            # prior owner dies
    dip.acquire_ingress_authority()  # new epoch: reconcile prior nonterminal -> UNKNOWN
    auth = dip.get_ingress_authority()
    assert auth.get_run("t_re")["state"] == RunState.UNKNOWN.value
    # a poll (NOT a user click) re-projects the durable pending approval
    dip.reconcile_pending_approval("t_re", pending_approval_id="ap1")
    row = auth.get_run("t_re")
    assert row["state"] == RunState.WAITING_APPROVAL.value
    assert (row["tenant_id"], row["workspace_id"], row["principal_id"]) == ("t1", "w1", "t1:u1")
    # and the human decision then still applies (single-use CAS), no re-execution
    dip.decide_managed_approval("t_re", approval_id="ap1", decision="approve", **_SCOPE)
    assert auth.get_run("t_re")["state"] == RunState.RUNNING.value


# ------------------------------------------------------------- cross-scope --- #
def test_cross_scope_refused(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_cs")
    dip.open_managed_approval("t_cs", approval_id="ap1")
    with pytest.raises(ApprovalScopeMismatch):
        dip.decide_managed_approval(
            "t_cs", approval_id="ap1", decision="approve",
            tenant_id="t1", workspace_id="w1", principal_id="t1:INTRUDER",
        )
    auth = dip.get_ingress_authority()
    assert auth.get_run("t_cs")["state"] == RunState.WAITING_APPROVAL.value


# ----------------------------------------------------------------- restart --- #
def test_decision_survives_restart_no_double_consume(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_rs")
    dip.open_managed_approval("t_rs", approval_id="ap1")
    dip.decide_managed_approval("t_rs", approval_id="ap1", decision="approve", **_SCOPE)

    dip.reset_for_tests()          # instance handoff
    dip.acquire_ingress_authority()  # new epoch; reconcile RUNNING -> UNKNOWN
    # a replayed decision after restart is refused; the prior decision is durable.
    with pytest.raises(ApprovalNotOpen):
        dip.decide_managed_approval("t_rs", approval_id="ap1", decision="approve", **_SCOPE)
    assert dip.prior_approval_decision("t_rs", "ap1") == "approve"


# ------------------------------------------------------------ authority-loss - #
def test_authority_loss_fails_closed(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_al")
    dip.open_managed_approval("t_al", approval_id="ap1")
    dip._on_lost("simulated advisory-lock loss")
    from youtab_runtime.durable_ingress import AuthorityLost
    with pytest.raises(AuthorityLost):
        dip.decide_managed_approval("t_al", approval_id="ap1", decision="approve", **_SCOPE)
    with pytest.raises(AuthorityLost):
        dip.open_managed_approval("t_al", approval_id="ap2")
    with pytest.raises(AuthorityLost):
        dip.prior_approval_decision("t_al", "ap1")


# ------- P1: reconcile is truthful (typed result, never a false pending) ------ #
def test_reconcile_result_accepted(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_ra")
    assert dip.reconcile_pending_approval("t_ra", pending_approval_id="ap1") == \
        dip.PROJECTION_ACCEPTED
    assert dip.get_ingress_authority().get_run("t_ra")["state"] == RunState.WAITING_APPROVAL.value
    assert dip.reconcile_pending_approval("t_ra", pending_approval_id="ap1") == \
        dip.PROJECTION_ACCEPTED  # idempotent: already-open also ACCEPTED


def test_reconcile_result_none_when_nothing_or_decided(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_rn")
    assert dip.reconcile_pending_approval("t_rn", pending_approval_id=None) == dip.PROJECTION_NONE
    dip.open_managed_approval("t_rn", approval_id="ap1")
    dip.decide_managed_approval("t_rn", approval_id="ap1", decision="approve", **_SCOPE)
    assert dip.reconcile_pending_approval("t_rn", pending_approval_id="ap1") == dip.PROJECTION_NONE


def test_reconcile_result_unavailable_on_loss(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_ru")
    dip._on_lost("advisory lock lost")
    # a lost authority MUST report UNAVAILABLE, never silently succeed (the P1 lie)
    assert dip.reconcile_pending_approval("t_ru", pending_approval_id="ap1") == \
        dip.PROJECTION_UNAVAILABLE


def test_reconcile_result_unavailable_when_disabled(monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_INGRESS", raising=False)
    dip.reset_for_tests()
    assert dip.reconcile_pending_approval("t_x", pending_approval_id="ap1") == \
        dip.PROJECTION_UNAVAILABLE


def test_reconcile_no_poll_leaves_durable_not_pending(enabled):
    """Without an observation (no reconcile), the durable run is NOT falsely pending;
    the request lives only in kanban until the authority accepts it."""
    dip.acquire_ingress_authority()
    _admit(run_id="t_npoll")
    assert dip.get_ingress_authority().get_run("t_npoll")["state"] == RunState.QUEUED.value


def test_reconcile_after_restart_before_first_observation(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_rst")
    dip.reset_for_tests()               # restart BEFORE any observation
    dip.acquire_ingress_authority()     # takeover: prior nonterminal -> UNKNOWN
    auth = dip.get_ingress_authority()
    assert auth.get_run("t_rst")["state"] == RunState.UNKNOWN.value  # not a false pending
    assert dip.reconcile_pending_approval("t_rst", pending_approval_id="ap1") == \
        dip.PROJECTION_ACCEPTED         # first observation ingests under new authority
    assert auth.get_run("t_rst")["state"] == RunState.WAITING_APPROVAL.value


def test_durable_approval_events_source_for_rebuild(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_de")
    dip.open_managed_approval("t_de", approval_id="ap1")
    dip.decide_managed_approval("t_de", approval_id="ap1", decision="deny", **_SCOPE)
    evs = dip.durable_approval_events("t_de")
    reqs = [e for e in evs if e["kind"] == "request" and e["approval_id"] == "ap1"]
    assert reqs and all(k in reqs[-1] for k in ("effect_digest", "action", "mode"))
    assert {"kind": "decision", "approval_id": "ap1", "decision": "deny"} in evs


# --------------------------------------------------------------- disabled ---- #
def test_seams_fail_closed_when_disabled(monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_INGRESS", raising=False)
    dip.reset_for_tests()
    from youtab_runtime.durable_ingress import AuthorityLost
    with pytest.raises(AuthorityLost):
        dip.open_managed_approval("t_x", approval_id="ap1")
    with pytest.raises(AuthorityLost):
        dip.decide_managed_approval("t_x", approval_id="ap1", decision="approve", **_SCOPE)
