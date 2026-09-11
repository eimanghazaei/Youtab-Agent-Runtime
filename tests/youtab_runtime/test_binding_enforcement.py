"""WAVE-30H — worker pre-dispatch binding enforcement (unit, offline, deterministic).

``_enforce_effective_binding`` runs inside ``establish_managed_admission`` and
returns BEFORE the agent loop constructs any provider client or debits any budget.
A raise here therefore PROVES zero provider calls and zero budget consumption for
the refused run — no client is ever built, no tree/campaign debit occurs. These
tests exercise every fail-closed condition (presence, integrity, tenant, run-scope,
workspace-scope, resolved substrate, provider/model/endpoint drift), the audited
legacy quarantine, and — via ``test_every_rejection_is_zero_spend_zero_provider`` —
assert EXPLICITLY that not one provider-client build or budget debit occurs on any
rejection path.
"""
from __future__ import annotations

import types

import pytest

from youtab_agent_cli import effective_binding as eb
from youtab_agent_cli.worker_admission import (
    ManagedWorkerAdmissionError,
    _enforce_effective_binding,
    _legacy_binding_quarantine_active,
)

_RUN = "run-1"
_WS = "w1"


def _env(tenant="t1", workspace=_WS):
    return types.SimpleNamespace(
        tenant_id=tenant, workspace_id=workspace, task_id=_RUN, root_run_id=_RUN
    )


def _agent(provider=None, model=None, base_url=None):
    a = types.SimpleNamespace(provider=provider, model=model)
    if base_url is not None:
        a.base_url = base_url
    return a


def _binding(provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434",
             tenant="t1", run_id=_RUN, workspace=_WS):
    return eb.build_effective_binding(
        provider=provider, model=model, endpoint=endpoint,
        run_id=run_id, root_run_id=run_id, tenant=tenant, workspace=workspace,
    )


def _enforce(agent, env, binding, task_id=_RUN, *, require_client_match=False,
             authoritative_run_id=_RUN, authoritative_workspace=_WS):
    """Call the gate with the authoritative run/workspace context defaulting to the
    legitimate values (``_RUN`` / ``_WS``) so a test overrides only what it probes."""
    return _enforce_effective_binding(
        agent, env, binding, task_id,
        require_client_match=require_client_match,
        authoritative_run_id=authoritative_run_id,
        authoritative_workspace=authoritative_workspace,
    )


# ── presence / integrity / tenant (enforced for EVERY managed run) ───────────

def test_missing_binding_fails_closed(monkeypatch):
    monkeypatch.delenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", raising=False)
    with pytest.raises(ManagedWorkerAdmissionError, match="no effective binding"):
        _enforce(_agent(), _env(), None)


def test_corrupt_binding_fails_closed():
    with pytest.raises(ManagedWorkerAdmissionError, match="corrupt"):
        _enforce(_agent(), _env(), {"__corrupt__": True})


def test_tampered_hash_fails_closed():
    b = _binding()
    b["model"] = "evil-swap"  # breaks the self-hash
    with pytest.raises(ManagedWorkerAdmissionError, match="hash mismatch"):
        _enforce(_agent(), _env(), b)


def test_cross_tenant_fails_closed():
    b = _binding(tenant="t1")
    with pytest.raises(ManagedWorkerAdmissionError, match="tenant mismatch"):
        _enforce(_agent(), _env(tenant="t2"), b)


def test_valid_binding_stub_worker_passes():
    # Deterministic/stub worker (no provider client): presence/integrity/tenant/
    # run-scope/workspace-scope only.
    _enforce(_agent(), _env(), _binding())


# ── run-scope / workspace-scope (enforced for EVERY managed run) ─────────────

def test_binding_without_run_scope_fails_closed():
    # A present binding that carries no run_id is malformed/stale — refuse.
    b = eb.build_effective_binding(
        provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434",
        tenant="t1", workspace=_WS,  # no run_id
    )
    with pytest.raises(ManagedWorkerAdmissionError, match="no run scope"):
        _enforce(_agent(), _env(), b)


def test_cross_run_substitution_fails_closed():
    # A wholesale, self-consistent binding from ANOTHER run of the SAME tenant
    # (valid hash, matching tenant/workspace) — passes hash + tenant, caught by the
    # run-scope gate because its run_id != the run this worker actually executes.
    foreign = _binding(run_id="run-OTHER")
    assert eb.verify_binding(foreign)  # self-consistent forgery
    with pytest.raises(ManagedWorkerAdmissionError, match="cross-run"):
        _enforce(_agent(), _env(), foreign, authoritative_run_id=_RUN)


def test_cross_run_substitution_fails_closed_real_worker():
    foreign = _binding(run_id="run-OTHER")
    a = _agent(provider="ollama", model="qwen:tag", base_url="http://127.0.0.1:11434")
    with pytest.raises(ManagedWorkerAdmissionError, match="cross-run"):
        _enforce(a, _env(), foreign, require_client_match=True,
                 authoritative_run_id=_RUN)


def test_cross_workspace_substitution_fails_closed():
    # A self-consistent binding from ANOTHER workspace of the SAME tenant/run —
    # caught by the workspace-scope gate against the admitted workspace.
    foreign = _binding(workspace="w-OTHER")
    assert eb.verify_binding(foreign)
    with pytest.raises(ManagedWorkerAdmissionError, match="cross-workspace"):
        _enforce(_agent(), _env(workspace=_WS), foreign, authoritative_workspace=_WS)


def test_unscoped_workspace_matches_unscoped_admission():
    # An unscoped run (binding workspace == "-" / None) is not a spurious mismatch
    # against an unscoped admission ("-"): both normalize to the unscoped sentinel.
    b = _binding(workspace="-")
    _enforce(_agent(), _env(workspace="-"), b, authoritative_workspace="-")
    b_none = _binding(workspace=None)
    _enforce(_agent(), _env(workspace=None), b_none, authoritative_workspace=None)


# ── resolved + drift (enforced only for a real-model worker) ─────────────────

def test_unresolved_binding_fails_closed_for_real_worker():
    b = eb.build_effective_binding(
        provider="openai", model=None, run_id=_RUN, tenant="t1", workspace=_WS)
    with pytest.raises(ManagedWorkerAdmissionError, match="not fully resolved"):
        _enforce(_agent(provider="openai", model="gpt-x"), _env(), b,
                 require_client_match=True)


def test_provider_drift_fails_closed():
    b = _binding(provider="ollama", model="qwen:tag")
    with pytest.raises(ManagedWorkerAdmissionError, match="provider drifted"):
        _enforce(_agent(provider="openai", model="qwen:tag"), _env(), b,
                 require_client_match=True)


def test_model_drift_fails_closed():
    b = _binding(provider="ollama", model="qwen:tag")
    with pytest.raises(ManagedWorkerAdmissionError, match="model drifted"):
        _enforce(_agent(provider="ollama", model="other-model"), _env(), b,
                 require_client_match=True)


def test_endpoint_drift_fails_closed():
    b = _binding(provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434")
    a = _agent(provider="ollama", model="qwen:tag", base_url="http://127.0.0.1:11435")
    with pytest.raises(ManagedWorkerAdmissionError, match="endpoint drifted"):
        _enforce(a, _env(), b, require_client_match=True)


def test_matching_real_worker_passes():
    b = _binding(provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434")
    a = _agent(provider="ollama", model="qwen:tag", base_url="http://127.0.0.1:11434")
    _enforce(a, _env(), b, require_client_match=True)  # no raise


# ── legacy quarantine: fail-closed by default, bounded, audited ──────────────

def test_legacy_quarantine_allows_missing_when_future_dated(monkeypatch):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2099-01-01")
    import youtab_agent_cli.worker_admission as wa
    audited = {}
    monkeypatch.setattr(
        wa, "_audit_binding_quarantine",
        lambda task_id, reason: audited.update({"task_id": task_id, "reason": reason}),
    )
    _enforce(_agent(), _env(), None)
    assert audited == {"task_id": _RUN, "reason": "missing_binding"}


def test_legacy_quarantine_does_not_excuse_a_present_foreign_binding(monkeypatch):
    # The quarantine only covers a truly MISSING binding. A present but cross-run
    # binding is still refused even inside the migration window (no scope bypass).
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2099-01-01")
    with pytest.raises(ManagedWorkerAdmissionError, match="cross-run"):
        _enforce(_agent(), _env(), _binding(run_id="run-OTHER"))


def test_legacy_quarantine_expired_fails_closed(monkeypatch):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2000-01-01")
    with pytest.raises(ManagedWorkerAdmissionError, match="no effective binding"):
        _enforce(_agent(), _env(), None)


@pytest.mark.parametrize("val", ["forever", "not-a-date", ""])
def test_quarantine_nondate_fails_closed(monkeypatch, val):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", val)
    assert _legacy_binding_quarantine_active() is False


# ── explicit zero-provider / zero-spend proof on every rejection ─────────────

def test_every_rejection_is_zero_spend_zero_provider(monkeypatch):
    """Every rejection path must touch neither a provider client nor a budget.

    Install spies on the ONLY places a managed run can spend or reach a provider —
    the execution-tree budget (``open_tree``/``consume``/``register_retry``), the
    campaign budget (``reserve``), and the provider-client resolver
    (``agent.auxiliary_client.resolve_provider_client``) — that raise if EVER
    called. The gate raises before any of them can run, so a clean
    ``ManagedWorkerAdmissionError`` on every scenario proves zero spend + zero
    provider calls. (The gate never imports these; the spies document the property
    and guard against a future edit that reorders spend ahead of the gate.)
    """
    from youtab_runtime import execution_tree_budget as etb

    def _boom(name):
        def _f(*a, **k):  # noqa: ANN001
            raise AssertionError(f"budget/provider touched on a rejection: {name}")
        return _f

    monkeypatch.setattr(etb, "open_tree", _boom("etb.open_tree"))
    monkeypatch.setattr(etb, "consume", _boom("etb.consume"))
    monkeypatch.setattr(etb, "register_retry", _boom("etb.register_retry"), raising=False)
    try:
        from youtab_runtime import campaign_budget as cb
        monkeypatch.setattr(cb, "reserve", _boom("cb.reserve"), raising=False)
    except Exception:  # noqa: BLE001 — module optional in some builds
        pass
    try:
        import agent.auxiliary_client as ac
        monkeypatch.setattr(
            ac, "resolve_provider_client", _boom("resolve_provider_client"),
            raising=False,
        )
    except Exception:  # noqa: BLE001
        pass

    real = _agent(provider="ollama", model="qwen:tag", base_url="http://127.0.0.1:11434")
    scenarios = [
        # (agent, env, binding, require_client_match, auth_run, auth_ws)
        (_agent(), _env(), None, False, _RUN, _WS),                          # missing
        (_agent(), _env(), {"__corrupt__": True}, False, _RUN, _WS),         # corrupt
        (_agent(), _env(), _tampered(), False, _RUN, _WS),                   # hash
        (_agent(), _env(tenant="t2"), _binding(), False, _RUN, _WS),         # tenant
        (_agent(), _env(), _binding(run_id="run-X"), False, _RUN, _WS),      # cross-run
        (_agent(), _env(), _binding(workspace="w-X"), False, _RUN, _WS),     # cross-ws
        (real, _env(), _binding(provider="openai"), True, _RUN, _WS),        # prov drift
        (real, _env(), _unresolved(), True, _RUN, _WS),                      # unresolved
    ]
    monkeypatch.delenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", raising=False)
    for agent, env, binding, rcm, arun, aws in scenarios:
        with pytest.raises(ManagedWorkerAdmissionError):
            _enforce(agent, env, binding, require_client_match=rcm,
                     authoritative_run_id=arun, authoritative_workspace=aws)


def _tampered():
    b = _binding()
    b["model"] = "evil-swap"
    return b


def _unresolved():
    return eb.build_effective_binding(
        provider="openai", model=None, run_id=_RUN, tenant="t1", workspace=_WS)
