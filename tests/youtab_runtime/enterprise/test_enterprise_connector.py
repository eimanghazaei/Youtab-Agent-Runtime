"""Lane-2 governed connector: full-spine, worker/subprocess, isolation proofs.

Runs the REAL effect ledger on a throwaway db and REAL worker subprocesses
(``sys.executable``). Covers the operation matrix, preview/commit/reconcile
semantics, worker subprocess proof, cancellation/crash/malformed/forge handling,
retry-no-duplicate, cross-tenant + cross-workspace isolation, and reference
integrity.
"""

from __future__ import annotations

import os
import sys

import pytest

from youtab_runtime import effect_ledger
from youtab_runtime.enterprise import reference_providers as providers
from youtab_runtime.enterprise.authority import (
    ReferenceAuthorityBoundary,
    RuntimeIdempotencyPolicy,
)
from youtab_runtime.enterprise.connector import (
    ConnectorRequest,
    GovernedConnector,
    GovernedConnectorError,
)
from youtab_runtime.enterprise.manifest import (
    TEST_ONLY_ISSUER,
    AllowlistPolicy,
    KeyRegistry,
    ManifestSigner,
    ManifestVerifier,
)
from youtab_runtime.enterprise.worker_boundary import WorkerBoundary
from youtab_runtime.run_journal import Principal

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
TEST_SECRET = b"test-signer-secret-DO-NOT-SHIP-00"
NOW = 2_000_000


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "run_journal.db"


@pytest.fixture()
def principal():
    return Principal("tenant-a", "user-a")


def _registry():
    return KeyRegistry({TEST_ONLY_ISSUER: {"tk": TEST_SECRET}})


def _cap_dict(operation_id, *, workspace="ws-acme", tenant="tenant-a",
              provenance="REFERENCE", provider="reference"):
    op_class = providers.operation_class(operation_id)
    return {
        "capability_id": operation_id,
        "capability_version": "1.0.0",
        "operation_id": operation_id,
        "request_schema": providers.request_schema_for(operation_id),
        "response_schema": providers.response_schema_for(operation_id),
        "tenant_scope": tenant,
        "workspace_scope": workspace,
        "risk_class": "high" if op_class == "commit" else "read",
        "approval_required": op_class == "commit",
        "idempotency": {
            "supported": op_class == "commit",
            "semantics": "exactly_once" if op_class == "commit" else "none",
        },
        "receipt_supported": True,
        "reconciliation_supported": True,
        "provenance": provenance,
        "provider": provider,
    }


def _build(operation_ids, *, workspace="ws-acme", tenant="tenant-a",
           provenance="REFERENCE", provider="reference"):
    reg = _registry()
    caps = [
        _cap_dict(op, workspace=workspace, tenant=tenant,
                  provenance=provenance, provider=provider)
        for op in operation_ids
    ]
    signed = ManifestSigner(reg, TEST_ONLY_ISSUER, "tk").sign(
        caps, issued_at=NOW - 10, expires_at=NOW + 100_000
    )
    allow = AllowlistPolicy(
        capabilities={(op, "1.0.0") for op in operation_ids},
        providers={provider},
    )
    verifier = ManifestVerifier(reg, allow, production=False, clock=lambda: NOW)
    authority = ReferenceAuthorityBoundary(production=False, clock=lambda: NOW)
    return signed, verifier, authority


def _connector(operation_ids, db_path, *, worker=None, **kw):
    signed, verifier, authority = _build(operation_ids, **kw)
    return (
        GovernedConnector(
            signed, verifier, authority,
            RuntimeIdempotencyPolicy(allowed_external_providers=set()),
            worker or WorkerBoundary(repo_root=REPO_ROOT),
            production=False, db_path=db_path, deadline_seconds=30.0,
        ),
        authority,
    )


def _register_commit_approval(authority, operation_id, req, *,
                              tenant="tenant-a", raw_ws="ws-acme"):
    ws = authority.canonicalize_workspace(tenant, raw_ws)
    rd = providers.request_digest_of(
        operation_id, ws.workspace, req.business_key, req.payload
    )
    authority.register_approval(
        "appr-1", operation_id=operation_id, request_digest=rd,
        tenant=tenant, workspace_canonical=ws.workspace,
    )


# --------------------------------------------------------------------------- #
# Operation matrix                                                             #
# --------------------------------------------------------------------------- #
COMMIT_OPS = [
    "crm.contact.update.commit", "erp.order.commit",
    "sap.business_object.update.commit",
]
READ_OPS = [
    "crm.contact.read", "erp.inventory.read", "sap.business_object.read",
]
PREVIEW_OPS = [
    "crm.contact.update.preview", "erp.order.preview",
    "sap.business_object.update.preview",
]


def _biz_payload(op):
    oc = providers.operation_class(op)
    if oc in ("preview", "commit"):
        return {"business_key": "obj-1", "field": "status", "value": "active"}
    return {"business_key": "obj-1"}


def test_read_ops_no_effect(db_path, principal):
    for op in READ_OPS:
        conn, _ = _connector([op], db_path)
        r = conn.execute(ConnectorRequest(
            capability_id=op, run_id="run-1", principal=principal,
            raw_workspace="ws-acme", business_key="obj-1", payload=_biz_payload(op),
        ))
        assert r.effect_state == "non_mutating"
        assert r.provenance == "REFERENCE"
    assert effect_ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_preview_creates_zero_effect_commit_creates_one(db_path, principal):
    ops = ["crm.contact.update.preview", "crm.contact.update.commit"]
    conn, authority = _connector(ops, db_path)
    prev = ConnectorRequest(
        capability_id="crm.contact.update.preview", run_id="run-1",
        principal=principal, raw_workspace="ws-acme", business_key="obj-1",
        payload=_biz_payload("crm.contact.update.preview"),
    )
    r1 = conn.execute(prev)
    assert r1.effect_state == "non_mutating"
    assert effect_ledger.list_effects("run-1", principal, db_path=db_path) == []

    commit = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1",
        principal=principal, raw_workspace="ws-acme", business_key="obj-1",
        payload=_biz_payload("crm.contact.update.commit"), approval_id="appr-1",
    )
    _register_commit_approval(authority, "crm.contact.update.commit", commit)
    r2 = conn.execute(commit)
    assert r2.effect_state == "committed"
    effs = effect_ledger.list_effects("run-1", principal, db_path=db_path)
    assert len(effs) == 1  # exactly one effect
    assert effs[0].detail.get("mutating") is True


def test_commit_matrix(db_path, principal):
    for i, op in enumerate(COMMIT_OPS):
        conn, authority = _connector([op], db_path)
        req = ConnectorRequest(
            capability_id=op, run_id=f"run-{i}", principal=principal,
            raw_workspace="ws-acme", business_key="obj-1",
            payload=_biz_payload(op), approval_id="appr-1",
        )
        _register_commit_approval(authority, op, req)
        r = conn.execute(req)
        assert r.effect_state == "committed", (op, r.effect_state)
        assert r.provenance == "REFERENCE"
        assert r.result_digest and r.provider_result_digest


def test_cad_matrix_real_computation(db_path, principal):
    # geometry.inspect
    conn, _ = _connector(["cad.geometry.inspect"], db_path)
    r = conn.execute(ConnectorRequest(
        capability_id="cad.geometry.inspect", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="part-1",
        payload={"geometry": {"kind": "box",
                              "dimensions": {"width": 3, "height": 4, "depth": 5}}},
    ))
    assert r.output["volume"] == pytest.approx(60.0)
    # dfm.check
    conn, _ = _connector(["cad.dfm.check"], db_path)
    r = conn.execute(ConnectorRequest(
        capability_id="cad.dfm.check", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="part-1",
        payload={"geometry": {"kind": "box",
                              "dimensions": {"width": 1, "height": 4, "depth": 5}},
                 "min_feature_size": 2.0},
    ))
    assert r.output["passed"] is False
    # fea.run — now a governed EFFECT (commit class), library-backed via numpy.
    conn, authority = _connector(["cad.fea.run"], db_path)
    payload = {"model": {"length": 2.0, "area": 0.01, "youngs_modulus": 200e9,
                         "force": 1000.0, "elements": 8}}
    req = ConnectorRequest(
        capability_id="cad.fea.run", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="part-1", payload=payload,
        approval_id="appr-1",
    )
    _register_commit_approval(authority, "cad.fea.run", req)
    r = conn.execute(req)
    assert r.output["tip_displacement"] == pytest.approx(1e-6, rel=1e-9)
    assert r.output["backend"] == "numpy"
    assert r.effect_state == "committed"


# --------------------------------------------------------------------------- #
# Worker subprocess proof                                                      #
# --------------------------------------------------------------------------- #
def test_worker_runs_as_real_subprocess(db_path, principal):
    """The default boundary spawns python -m ...worker; a typed request crosses
    the boundary and typed output comes back."""
    conn, _ = _connector(["crm.contact.read"], db_path)
    r = conn.execute(ConnectorRequest(
        capability_id="crm.contact.read", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="obj-1", payload={"business_key": "obj-1"},
    ))
    assert r.output["found"] is True
    assert r.output["record_id"]


def _custom_worker(script):
    return WorkerBoundary(python_argv=[sys.executable, "-c", script], repo_root=REPO_ROOT)


def _commit_conn_with_worker(db_path, script):
    conn, authority = _connector(
        ["crm.contact.update.commit"], db_path,
        worker=_custom_worker(script),
    )
    req = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1",
        principal=Principal("tenant-a", "user-a"), raw_workspace="ws-acme",
        business_key="obj-1", payload=_biz_payload("crm.contact.update.commit"),
        approval_id="appr-1",
    )
    _register_commit_approval(authority, "crm.contact.update.commit", req)
    return conn, req


def test_worker_deadline_marks_unknown(db_path):
    conn, req = _commit_conn_with_worker(
        db_path, "import sys,time; sys.stdin.read(); time.sleep(60)"
    )
    conn._deadline = 2.0  # tight deadline for the test
    with pytest.raises(GovernedConnectorError):
        conn.execute(req)
    eff = effect_ledger.list_effects("run-1", req.principal, db_path=db_path)
    assert len(eff) == 1 and eff[0].state.value == "unknown"


def test_worker_crash_marks_unknown(db_path):
    conn, req = _commit_conn_with_worker(
        db_path, "import sys; sys.stdin.read(); sys.exit(3)"
    )
    with pytest.raises(GovernedConnectorError):
        conn.execute(req)
    eff = effect_ledger.list_effects("run-1", req.principal, db_path=db_path)
    assert len(eff) == 1 and eff[0].state.value == "unknown"


def test_worker_malformed_fails_closed(db_path):
    conn, req = _commit_conn_with_worker(
        db_path, "import sys; sys.stdin.read(); print('not-json{{{')"
    )
    with pytest.raises(GovernedConnectorError):
        conn.execute(req)
    eff = effect_ledger.list_effects("run-1", req.principal, db_path=db_path)
    assert len(eff) == 1 and eff[0].state.value == "unknown"


def test_worker_cannot_forge_receipt(db_path):
    forged = (
        "import sys,json; sys.stdin.read();"
        "print(json.dumps({'ok':True,'result':{'record_id':'x','revision':'y',"
        "'provenance':'REFERENCE','result_digest':'zzz'},"
        "'receipt':{'state':'committed-by-worker','approval':'self-granted'}}))"
    )
    conn, req = _commit_conn_with_worker(db_path, forged)
    r = conn.execute(req)
    # The Runtime mints the receipt: state from the ledger, approval from
    # authority — NOT the worker's forged fields.
    assert r.effect_state == "committed"
    assert r.approval_id == "appr-1"


def test_worker_provenance_confusion_fails_closed(db_path):
    liar = (
        "import sys,json; sys.stdin.read();"
        "print(json.dumps({'ok':True,'result':{'record_id':'x','revision':'y',"
        "'provenance':'LIVE','result_digest':'zzz'}}))"
    )
    conn, req = _commit_conn_with_worker(db_path, liar)
    with pytest.raises(GovernedConnectorError):
        conn.execute(req)
    eff = effect_ledger.list_effects("run-1", req.principal, db_path=db_path)
    assert eff[0].state.value == "unknown"


# --------------------------------------------------------------------------- #
# Retry / dedup                                                                #
# --------------------------------------------------------------------------- #
def test_retry_after_commit_dedups(db_path, principal):
    conn, authority = _connector(["crm.contact.update.commit"], db_path)
    req = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1",
        principal=principal, raw_workspace="ws-acme", business_key="obj-1",
        payload=_biz_payload("crm.contact.update.commit"), approval_id="appr-1",
    )
    _register_commit_approval(authority, "crm.contact.update.commit", req)
    r1 = conn.execute(req)
    r2 = conn.execute(req)
    assert r1.effect_id == r2.effect_id
    assert r2.deduplicated is True
    assert r2.effect_state == "committed"
    assert len(effect_ledger.list_effects("run-1", principal, db_path=db_path)) == 1


def test_retry_after_crash_not_reexecuted(db_path):
    conn, req = _commit_conn_with_worker(
        db_path, "import sys; sys.stdin.read(); sys.exit(3)"
    )
    with pytest.raises(GovernedConnectorError):
        conn.execute(req)
    # Effect is now unknown; a retry must NOT re-run (returns deduped unknown).
    r = conn.execute(req)
    assert r.deduplicated is True
    assert r.effect_state == "unknown"


# --------------------------------------------------------------------------- #
# Reconcile — query/verify only                                                #
# --------------------------------------------------------------------------- #
def test_reconcile_query_only(db_path, principal):
    ops = ["crm.contact.update.commit", "crm.effect.reconcile"]
    conn, authority = _connector(ops, db_path)
    commit = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1",
        principal=principal, raw_workspace="ws-acme", business_key="obj-1",
        payload=_biz_payload("crm.contact.update.commit"), approval_id="appr-1",
    )
    _register_commit_approval(authority, "crm.contact.update.commit", commit)
    conn.execute(commit)
    before = len(effect_ledger.list_effects("run-1", principal, db_path=db_path))

    recon = ConnectorRequest(
        capability_id="crm.effect.reconcile", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="obj-1",
        payload={"checked_operation_id": "crm.contact.update.commit",
                 "business_key": "obj-1", "original_request_digest": "ignored"},
    )
    r = conn.execute(recon)
    assert r.reconciled is True
    after = len(effect_ledger.list_effects("run-1", principal, db_path=db_path))
    assert after == before  # reconcile creates no new effect


def test_reconcile_without_prior_fails_closed(db_path, principal):
    conn, _ = _connector(["crm.effect.reconcile"], db_path)
    recon = ConnectorRequest(
        capability_id="crm.effect.reconcile", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="ghost",
        payload={"checked_operation_id": "crm.contact.update.commit",
                 "business_key": "ghost", "original_request_digest": "x"},
    )
    with pytest.raises(GovernedConnectorError):
        conn.execute(recon)


# --------------------------------------------------------------------------- #
# Approval binding / client authority                                          #
# --------------------------------------------------------------------------- #
def test_commit_without_approval_refused(db_path, principal):
    conn, _ = _connector(["crm.contact.update.commit"], db_path)
    req = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1",
        principal=principal, raw_workspace="ws-acme", business_key="obj-1",
        payload=_biz_payload("crm.contact.update.commit"),
    )
    with pytest.raises(GovernedConnectorError):
        conn.execute(req)


def test_commit_with_mismatched_approval_refused(db_path, principal):
    conn, authority = _connector(["crm.contact.update.commit"], db_path)
    ws = authority.canonicalize_workspace("tenant-a", "ws-acme")
    # Approval bound to a DIFFERENT request digest.
    authority.register_approval(
        "appr-1", operation_id="crm.contact.update.commit",
        request_digest="WRONG", tenant="tenant-a", workspace_canonical=ws.workspace,
    )
    req = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1",
        principal=principal, raw_workspace="ws-acme", business_key="obj-1",
        payload=_biz_payload("crm.contact.update.commit"), approval_id="appr-1",
    )
    with pytest.raises(Exception):
        conn.execute(req)


# --------------------------------------------------------------------------- #
# Isolation                                                                    #
# --------------------------------------------------------------------------- #
def test_cross_tenant_isolation(db_path):
    a = Principal("tenant-a", "user-a")
    b = Principal("tenant-b", "user-b")
    conn_a, auth_a = _connector(["crm.contact.update.commit"], db_path)
    req_a = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1", principal=a,
        raw_workspace="ws-acme", business_key="obj-1",
        payload=_biz_payload("crm.contact.update.commit"), approval_id="appr-1",
    )
    _register_commit_approval(auth_a, "crm.contact.update.commit", req_a)
    ra = conn_a.execute(req_a)
    # Principal B cannot see A's effect.
    assert effect_ledger.get_effect(ra.effect_id, b, db_path=db_path) is None
    assert effect_ledger.get_effect(ra.effect_id, a, db_path=db_path) is not None


def test_cross_workspace_receipt_isolation(db_path, principal):
    conn, authority = _connector(["crm.contact.update.commit"], db_path)
    req = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1",
        principal=principal, raw_workspace="ws-acme", business_key="obj-1",
        payload=_biz_payload("crm.contact.update.commit"), approval_id="appr-1",
    )
    _register_commit_approval(authority, "crm.contact.update.commit", req)
    conn.execute(req)
    # Same tenant+user, correct workspace -> visible.
    got = conn.get_commit_receipt(
        "run-1", principal, "ws-acme", "crm.contact.update.commit", "obj-1"
    )
    assert got is not None and got["state"] == "committed"
    # Same tenant+user, DIFFERENT workspace -> not visible.
    other = conn.get_commit_receipt(
        "run-1", principal, "ws-globex", "crm.contact.update.commit", "obj-1"
    )
    assert other is None


# --------------------------------------------------------------------------- #
# Reference integrity / provenance                                             #
# --------------------------------------------------------------------------- #
def test_live_capability_refused_no_vendor_worker(db_path, principal):
    conn, _ = _connector(
        ["crm.contact.read"], db_path, provenance="LIVE", provider="salesforce"
    )
    req = ConnectorRequest(
        capability_id="crm.contact.read", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="obj-1", payload={"business_key": "obj-1"},
    )
    with pytest.raises(GovernedConnectorError):
        conn.execute(req)


def test_reference_authority_refused_in_production():
    signed, verifier, authority = _build(["crm.contact.read"])
    with pytest.raises(GovernedConnectorError):
        GovernedConnector(
            signed, ManifestVerifier(_registry(), AllowlistPolicy(
                {("crm.contact.read", "1.0.0")}, {"reference"}),
                production=True, clock=lambda: NOW),
            authority, RuntimeIdempotencyPolicy(set()),
            WorkerBoundary(repo_root=REPO_ROOT), production=True,
        )


def test_worker_env_scrubbed_of_secrets():
    b = WorkerBoundary(repo_root=REPO_ROOT)
    os.environ["YOUTAB_TEST_FAKE_SECRET"] = "topsecret"
    try:
        env = b._child_env()
    finally:
        del os.environ["YOUTAB_TEST_FAKE_SECRET"]
    assert "YOUTAB_TEST_FAKE_SECRET" not in env
    assert "PYTHONPATH" in env


if __name__ == "__main__":  # pragma: no cover - standalone smoke run
    import inspect
    import tempfile
    import traceback
    from pathlib import Path as _Path

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        params = inspect.signature(fn).parameters
        with tempfile.TemporaryDirectory() as td:
            kwargs = {}
            if "db_path" in params:
                kwargs["db_path"] = _Path(td) / "run_journal.db"
            if "principal" in params:
                kwargs["principal"] = Principal("tenant-a", "user-a")
            try:
                fn(**kwargs)
                passed += 1
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {fn.__name__}")
                traceback.print_exc()
    print(f"\nenterprise_connector standalone: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


def test_worker_request_size_bounded(db_path, principal):
    from youtab_runtime.enterprise.worker_boundary import (
        WorkerBoundary,
        WorkerBoundaryError,
    )
    b = WorkerBoundary(repo_root=REPO_ROOT, max_request_bytes=10)
    with pytest.raises(WorkerBoundaryError):
        b.run({"operation_id": "crm.contact.read", "workspace": "w",
               "business_key": "k", "payload": {}, "request_digest": "d",
               "provenance": "REFERENCE"}, deadline_seconds=10.0)


def test_worker_response_size_bounded(db_path):
    import sys as _sys
    from youtab_runtime.enterprise.worker_boundary import (
        WorkerBoundary,
        WorkerMalformed,
    )
    big = ("import sys,json; sys.stdin.read();"
           "print(json.dumps({'ok':True,'result':{'x':'a'*100000}}))")
    b = WorkerBoundary(python_argv=[_sys.executable, "-c", big],
                       repo_root=REPO_ROOT, max_response_bytes=100)
    with pytest.raises(WorkerMalformed):
        b.run({"operation_id": "x"}, deadline_seconds=10.0)
