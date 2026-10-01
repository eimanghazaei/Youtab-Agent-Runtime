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
             authoritative_run_id=_RUN, authoritative_workspace=_WS, created_at=None):
    """Call the gate with the authoritative run/workspace context defaulting to the
    legitimate values (``_RUN`` / ``_WS``) so a test overrides only what it probes."""
    return _enforce_effective_binding(
        agent, env, binding, task_id,
        require_client_match=require_client_match,
        authoritative_run_id=authoritative_run_id,
        authoritative_workspace=authoritative_workspace,
        created_at=created_at,
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


# ── model-manifest digest re-probe (WAVE-30H #7 — tag->manifest TOCTOU) ──────

def _attested(model_digest="a" * 64, model="qwen:tag", endpoint="http://127.0.0.1:11434"):
    return eb.build_effective_binding(
        provider="ollama", model=model, endpoint=endpoint,
        run_id=_RUN, root_run_id=_RUN, tenant="t1", workspace=_WS,
        model_digest=model_digest,
    )


def _real_ollama_agent():
    return _agent(provider="ollama", model="qwen:tag", base_url="http://127.0.0.1:11434")


def test_attested_manifest_toctou_fails_closed(monkeypatch):
    # The tag was re-pointed to a DIFFERENT manifest between attestation and execution.
    import agent.model_metadata as mm
    monkeypatch.setattr(mm, "query_ollama_model_digest",
                        lambda model, base_url, api_key="": "b" * 64)
    with pytest.raises(ManagedWorkerAdmissionError, match="manifest digest drifted"):
        _enforce(_real_ollama_agent(), _env(), _attested(), require_client_match=True)


def test_attested_manifest_probe_failure_fails_closed(monkeypatch):
    import agent.model_metadata as mm
    monkeypatch.setattr(mm, "query_ollama_model_digest", lambda *a, **k: None)
    with pytest.raises(ManagedWorkerAdmissionError, match="manifest digest drifted"):
        _enforce(_real_ollama_agent(), _env(), _attested(), require_client_match=True)


def test_attested_manifest_match_passes(monkeypatch):
    import agent.model_metadata as mm
    # Uppercase live digest -> constant-time compare is case-normalized -> matches.
    monkeypatch.setattr(mm, "query_ollama_model_digest", lambda *a, **k: "A" * 64)
    _enforce(_real_ollama_agent(), _env(), _attested(), require_client_match=True)  # no raise


def test_tampered_attested_digest_fails_hash():
    # Mutating the pinned digest after hashing breaks the self-hash (it is now a
    # _HASH_FIELD) — caught at the integrity gate BEFORE any re-probe.
    b = _attested()
    b["model_digest"] = "c" * 64
    with pytest.raises(ManagedWorkerAdmissionError, match="hash mismatch"):
        _enforce(_real_ollama_agent(), _env(), b, require_client_match=True)


def test_not_probed_binding_skips_reprobe(monkeypatch):
    # An ordinary not_probed run must NOT trigger a manifest re-probe (no regression).
    import agent.model_metadata as mm

    def _boom(*a, **k):
        raise AssertionError("must not re-probe a not_probed binding")

    monkeypatch.setattr(mm, "query_ollama_model_digest", _boom)
    b = _binding(provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434")
    assert b["digest_status"] == "not_probed"
    _enforce(_real_ollama_agent(), _env(), b, require_client_match=True)  # no raise, no probe


# ── legacy quarantine: fail-closed by default, SCOPED to pre-contract, audited ──

# The migration window requires BOTH the future-dated flag AND the operator-declared
# contract-epoch cutoff, AND a run created before that cutoff, AND a successful audit.
_CUTOFF = "1000000000"          # 2001-09-09; the "contract shipped" epoch
_PRE_CONTRACT_CREATED = 999_999_000   # < cutoff -> genuinely legacy
_POST_CONTRACT_CREATED = 2_000_000_000  # >= cutoff -> NOT legacy


def _quarantine_window(monkeypatch):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2099-01-01")
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_CREATED_BEFORE", _CUTOFF)


def test_legacy_quarantine_allows_missing_only_for_pre_contract_run(monkeypatch):
    _quarantine_window(monkeypatch)
    import youtab_agent_cli.worker_admission as wa
    audited = {}
    monkeypatch.setattr(
        wa, "_audit_binding_quarantine",
        lambda task_id, reason: audited.update({"task_id": task_id, "reason": reason}) or True,
    )
    _enforce(_agent(), _env(), None, created_at=_PRE_CONTRACT_CREATED)
    assert audited == {"task_id": _RUN, "reason": "missing_binding"}


def test_post_contract_missing_binding_fails_closed(monkeypatch):
    # WAVE-30H #9: a run created AT/AFTER the contract epoch is not legacy — a
    # missing binding on it is a defect/tamper and fails closed even in the window.
    _quarantine_window(monkeypatch)
    with pytest.raises(ManagedWorkerAdmissionError, match="no effective binding"):
        _enforce(_agent(), _env(), None, created_at=_POST_CONTRACT_CREATED)


def test_unknown_created_at_fails_closed(monkeypatch):
    _quarantine_window(monkeypatch)
    with pytest.raises(ManagedWorkerAdmissionError, match="no effective binding"):
        _enforce(_agent(), _env(), None, created_at=None)


def test_missing_cutoff_fails_closed_even_with_flag(monkeypatch):
    # WAVE-30H #9: the flag alone (no operator-declared cutoff) no longer bypasses —
    # closes the previous "any future date is global fail-open" hole.
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2099-01-01")
    monkeypatch.delenv("YOUTAB_MANAGED_BINDING_LEGACY_CREATED_BEFORE", raising=False)
    with pytest.raises(ManagedWorkerAdmissionError, match="no effective binding"):
        _enforce(_agent(), _env(), None, created_at=_PRE_CONTRACT_CREATED)


def test_audit_write_failure_fails_closed(monkeypatch):
    # WAVE-30H #9: a swallowed audit-write failure must NOT let the run proceed.
    _quarantine_window(monkeypatch)
    import youtab_agent_cli.worker_admission as wa
    monkeypatch.setattr(wa, "_audit_binding_quarantine", lambda task_id, reason: False)
    with pytest.raises(ManagedWorkerAdmissionError, match="audit write failed"):
        _enforce(_agent(), _env(), None, created_at=_PRE_CONTRACT_CREATED)


def test_legacy_quarantine_does_not_excuse_a_present_foreign_binding(monkeypatch):
    # The quarantine only covers a truly MISSING binding. A present but cross-run
    # binding is still refused even inside the migration window (no scope bypass).
    _quarantine_window(monkeypatch)
    with pytest.raises(ManagedWorkerAdmissionError, match="cross-run"):
        _enforce(_agent(), _env(), _binding(run_id="run-OTHER"),
                 created_at=_PRE_CONTRACT_CREATED)


def test_legacy_quarantine_expired_fails_closed(monkeypatch):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2000-01-01")
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_CREATED_BEFORE", _CUTOFF)
    with pytest.raises(ManagedWorkerAdmissionError, match="no effective binding"):
        _enforce(_agent(), _env(), None, created_at=_PRE_CONTRACT_CREATED)


@pytest.mark.parametrize("val", ["forever", "not-a-date", ""])
def test_quarantine_nondate_fails_closed(monkeypatch, val):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", val)
    assert _legacy_binding_quarantine_active() is False


# ── binding-version policy at the worker (WAVE-30H #F7) ───────────────────────

def _v1_binding(**over):
    return eb.build_effective_binding(
        provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434",
        run_id=_RUN, root_run_id=_RUN, tenant="t1", workspace=_WS,
        binding_version=1, **over,
    )


def test_unknown_binding_version_refused_at_worker():
    b = _binding()
    b["binding_version"] = 99  # unknown -> verify_binding fails -> refusal
    with pytest.raises(ManagedWorkerAdmissionError):
        _enforce(_agent(), _env(), b)


def test_unresolved_binding_refused_when_client_match_required():
    # WAVE-30H Batch3: a real-model worker (require_client_match) must refuse a binding
    # whose model is not fully resolved — BEFORE any provider client/budget. Covered
    # here deterministically (single binding, no duplicate); the duplicate-version
    # corruption path is exercised in the subprocess E2E.
    b = _binding(model=None)
    assert b["model_identifier_status"] != "resolved"
    assert eb.verify_binding(b) is True  # self-consistent, just unresolved
    with pytest.raises(ManagedWorkerAdmissionError, match="not fully resolved"):
        _enforce(
            _agent(provider="ollama", model="qwen:tag", base_url="http://127.0.0.1:11434"),
            _env(), b, require_client_match=True,
        )


def test_legacy_v1_refused_outside_migration_window(monkeypatch):
    monkeypatch.delenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", raising=False)
    monkeypatch.delenv("YOUTAB_MANAGED_BINDING_LEGACY_CREATED_BEFORE", raising=False)
    with pytest.raises(ManagedWorkerAdmissionError, match="legacy binding_version"):
        _enforce(_agent(), _env(), _v1_binding(), created_at=999_999_000)


def test_legacy_v1_accepted_in_migration_window(monkeypatch):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2099-01-01")
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_CREATED_BEFORE", "1000000000")
    import youtab_agent_cli.worker_admission as wa
    monkeypatch.setattr(wa, "_audit_binding_quarantine", lambda t, r: True)
    # A genuine pre-contract v1 run inside the audited window is admitted (stub worker).
    _enforce(_agent(), _env(), _v1_binding(), created_at=999_999_000)


def test_legacy_v1_attested_claim_refused(monkeypatch):
    # A v1 binding CANNOT cryptographically possess attested model-digest protection.
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2099-01-01")
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_CREATED_BEFORE", "1000000000")
    b = _v1_binding()
    b["digest_status"] = "attested"                 # forged claim (outside v1 hash)
    b["model_digest"] = "a" * 64
    b["binding_hash"] = eb.compute_binding_hash(b)   # still verifies under v1
    with pytest.raises(ManagedWorkerAdmissionError, match="cannot cryptographically possess"):
        _enforce(_agent(), _env(), b, created_at=999_999_000)


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
