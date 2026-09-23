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


def test_restart_does_not_revive_unknown_approval(enabled):
    """Finding 1: after a restart, a prior pending run is UNKNOWN (terminal-uncertain).
    reconcile must NOT revive it — no UNKNOWN->RUNNING, no re-open — until a validated
    checkpoint + resume fence (staged) proves resume safe. Fail-closed."""
    dip.acquire_ingress_authority()
    _admit(run_id="t_re")
    dip.reconcile_pending_approval("t_re", pending_approval_id="ap1")  # -> WAITING_APPROVAL

    dip.reset_for_tests()            # prior owner dies
    dip.acquire_ingress_authority()  # new epoch: reconcile prior nonterminal -> UNKNOWN
    auth = dip.get_ingress_authority()
    assert auth.get_run("t_re")["state"] == RunState.UNKNOWN.value
    # a re-observation must NOT re-open it; the run stays UNKNOWN (no RUNNING revival)
    assert dip.reconcile_pending_approval("t_re", pending_approval_id="ap1") == \
        dip.PROJECTION_NOT_ACCEPTED
    assert auth.get_run("t_re")["state"] == RunState.UNKNOWN.value
    # and a decision cannot consume it (it is not durably WAITING_APPROVAL)
    with pytest.raises(ApprovalNotOpen):
        dip.decide_managed_approval("t_re", approval_id="ap1", decision="approve", **_SCOPE)


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


def test_reconcile_after_restart_does_not_open_unknown(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_rst")
    dip.reset_for_tests()               # restart BEFORE any observation
    dip.acquire_ingress_authority()     # takeover: prior nonterminal -> UNKNOWN
    auth = dip.get_ingress_authority()
    assert auth.get_run("t_rst")["state"] == RunState.UNKNOWN.value  # not a false pending
    # finding 1: UNKNOWN is terminal-uncertain — reconcile must NOT open it.
    assert dip.reconcile_pending_approval("t_rst", pending_approval_id="ap1") == \
        dip.PROJECTION_NOT_ACCEPTED
    assert auth.get_run("t_rst")["state"] == RunState.UNKNOWN.value


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


# ── D2: single-use effect-claim fence (the effect-count 0/1 gate) ─────────────
from youtab_runtime.durable_run_store import EffectAlreadyClaimed, EffectClaimRefused  # noqa: E402

_BIND = {"effect_digest": "eff-1", "arguments_digest": "args-1",
         "authorization_id": "auth-1", "checkpoint_digest": "cp-1", "command_id": None}


def _approved_run(run_id="t_fx"):
    """Admit -> open (with a full binding) -> approve, leaving the run RUNNING with an
    approved, effect-bound approval ready to claim."""
    dip.acquire_ingress_authority()
    _admit(run_id=run_id)
    dip.open_managed_approval(run_id, approval_id="ap1", payload=dict(_BIND))
    dip.decide_managed_approval(run_id, approval_id="ap1", decision="approve", **_SCOPE)
    return dip.get_ingress_authority()


def test_effect_claim_grants_single_use_fence(enabled):
    auth = _approved_run("t_c1")
    fence = dip.claim_managed_effect("t_c1", approval_id="ap1", attempt_id="att-1",
                                     binding=dict(_BIND))
    assert fence["attempt_id"] == "att-1" and fence["expires_at"] > 0
    assert fence["replayed"] is False
    claims = [e for e in auth.get_events("t_c1") if e["kind"] == "effect_claim"]
    assert len(claims) == 1
    # same attempt is idempotent (returns the existing fence, no second claim)
    again = dip.claim_managed_effect("t_c1", approval_id="ap1", attempt_id="att-1",
                                     binding=dict(_BIND))
    assert again["replayed"] is True
    assert len([e for e in auth.get_events("t_c1") if e["kind"] == "effect_claim"]) == 1
    # a DIFFERENT attempt after one exists is refused single-use (no second apply)
    with pytest.raises(EffectAlreadyClaimed):
        dip.claim_managed_effect("t_c1", approval_id="ap1", attempt_id="att-2",
                                 binding=dict(_BIND))
    assert len([e for e in auth.get_events("t_c1") if e["kind"] == "effect_claim"]) == 1


def test_effect_claim_binding_mismatch_refused(enabled):
    _approved_run("t_c2")
    for f, bad in [("effect_digest", "eff-X"), ("arguments_digest", "args-X"),
                   ("authorization_id", "auth-X"), ("checkpoint_digest", "cp-X")]:
        b = dict(_BIND); b[f] = bad
        with pytest.raises(EffectClaimRefused):
            dip.claim_managed_effect("t_c2", approval_id="ap1", attempt_id="a", binding=b)
    # never claimed under a mismatched binding
    auth = dip.get_ingress_authority()
    assert [e for e in auth.get_events("t_c2") if e["kind"] == "effect_claim"] == []


def test_effect_claim_requires_approved(enabled):
    # pending (opened, not decided) -> refused
    dip.acquire_ingress_authority()
    _admit(run_id="t_c3")
    dip.open_managed_approval("t_c3", approval_id="ap1", payload=dict(_BIND))
    with pytest.raises(EffectClaimRefused):
        dip.claim_managed_effect("t_c3", approval_id="ap1", attempt_id="a", binding=dict(_BIND))


def test_effect_claim_denied_refused(enabled):
    dip.acquire_ingress_authority()
    _admit(run_id="t_c4")
    dip.open_managed_approval("t_c4", approval_id="ap1", payload=dict(_BIND))
    dip.decide_managed_approval("t_c4", approval_id="ap1", decision="deny", **_SCOPE)
    with pytest.raises(EffectClaimRefused):  # denied + run CANCELLED (inactive)
        dip.claim_managed_effect("t_c4", approval_id="ap1", attempt_id="a", binding=dict(_BIND))


def test_effect_claim_inactive_run_refused(enabled):
    auth = _approved_run("t_c5")
    auth.store.set_state("t_c5", RunState.CANCELLED, strict=False)  # e.g. cancelled
    with pytest.raises(EffectClaimRefused):
        dip.claim_managed_effect("t_c5", approval_id="ap1", attempt_id="a", binding=dict(_BIND))


def test_effect_claim_concurrent_single_winner(enabled):
    import threading
    _approved_run("t_c6")
    barrier = threading.Barrier(4)
    out: list = []

    def worker(n):
        barrier.wait()
        try:
            dip.claim_managed_effect("t_c6", approval_id="ap1", attempt_id=f"att-{n}",
                                     binding=dict(_BIND))
            out.append("ok")
        except EffectAlreadyClaimed:
            out.append("refused")
        except Exception as exc:  # surface anything unexpected
            out.append(f"err:{type(exc).__name__}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert out.count("ok") == 1 and out.count("refused") == 3, out
    auth = dip.get_ingress_authority()
    assert len([e for e in auth.get_events("t_c6") if e["kind"] == "effect_claim"]) == 1


def test_effect_claim_authority_loss_fails_closed(enabled):
    _approved_run("t_c7")
    dip._on_lost("advisory lock lost")
    from youtab_runtime.durable_ingress import AuthorityLost
    with pytest.raises(AuthorityLost):
        dip.claim_managed_effect("t_c7", approval_id="ap1", attempt_id="a", binding=dict(_BIND))
