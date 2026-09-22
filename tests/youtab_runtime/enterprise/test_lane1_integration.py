"""Integrated Lane-1 + Lane-2 adversarial proofs on the REAL canonical spine.

Uses the real Lane-1 modules (approval, effect_ledger, worker_lease) via
Lane1AuthorityAdapter and real worker subprocesses — no reference authority
double. The signed manifest is verified with the HMAC test signer
(production=False) purely to obtain a verified CapabilityManifest; the Ed25519
production verification path is covered in test_manifest_crypto.
"""

from __future__ import annotations

import os
import sys

import pytest

from youtab_runtime import approval as _approval
from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.enterprise import providers
from youtab_runtime.enterprise.authority import RuntimeIdempotencyPolicy
from youtab_runtime.enterprise.connector import ConnectorRequest
from youtab_runtime.enterprise.lane1_adapter import Lane1AuthorityAdapter
from youtab_runtime.enterprise.lane1_connector import (
    Lane1GovernedConnector,
    Lane1GovernedConnectorError,
)
from youtab_runtime.enterprise.manifest import (
    TEST_ONLY_ISSUER,
    AllowlistPolicy,
    KeyRegistry,
    ManifestError,
    ManifestSigner,
    ManifestVerifier,
)
from youtab_runtime.enterprise.worker_boundary import WorkerBoundary
from youtab_runtime.run_journal import Principal

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
TEST_SECRET = b"test-signer-secret-DO-NOT-SHIP-00"
NOW = 3_000_000


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
    oc = providers.operation_class(operation_id)
    return {
        "capability_id": operation_id, "capability_version": "1.0.0",
        "operation_id": operation_id,
        "request_schema": providers.request_schema_for(operation_id),
        "response_schema": providers.response_schema_for(operation_id),
        "tenant_scope": tenant, "workspace_scope": workspace,
        "risk_class": "high" if oc == "commit" else "read",
        "approval_required": oc == "commit",
        "idempotency": {"supported": oc == "commit",
                        "semantics": "exactly_once" if oc == "commit" else "none"},
        "receipt_supported": True, "reconciliation_supported": True,
        "provenance": provenance, "provider": provider,
    }


def _manifest(operation_ids, **kw):
    reg = _registry()
    caps = [_cap_dict(op, **kw) for op in operation_ids]
    signed = ManifestSigner(reg, TEST_ONLY_ISSUER, "tk").sign(
        caps, issued_at=NOW - 10, expires_at=NOW + 100_000
    )
    allow = AllowlistPolicy({(op, "1.0.0") for op in operation_ids},
                            {kw.get("provider", "reference")})
    verifier = ManifestVerifier(reg, allow, production=False, clock=lambda: NOW)
    return verifier.verify(signed)


def _connector(operation_ids, db_path, *, worker=None, **kw):
    manifest = _manifest(operation_ids, **kw)
    adapter = Lane1AuthorityAdapter(db_path=db_path, clock=lambda: NOW)
    conn = Lane1GovernedConnector(
        manifest, adapter, RuntimeIdempotencyPolicy(set()),
        worker or WorkerBoundary(repo_root=REPO_ROOT),
        production=True, db_path=db_path, deadline_seconds=30.0,
    )
    return conn, adapter


def _commit_req(op, principal, *, workspace="ws-acme", business_key="obj-1",
                approval_id="appr-1"):
    payload = ({"business_key": business_key, "field": "status", "value": "active"}
               if providers.operation_class(op) in ("preview", "commit")
               else {"business_key": business_key})
    return ConnectorRequest(
        capability_id=op, run_id="run-1", principal=principal,
        raw_workspace=workspace, business_key=business_key, payload=payload,
        approval_id=approval_id,
    )


def _issue_approval(adapter, db_path, op, req, *, approval_id="appr-1",
                    principal=None, ttl=3600):
    principal = principal or req.principal
    ws = adapter.canonicalize_workspace(principal.tenant, req.raw_workspace)
    rd = providers.request_digest_of(op, ws.workspace, req.business_key, req.payload)
    edigest = adapter.effect_digest_for(op, ws.workspace, rd)
    _approval.issue_approval(approval_id, edigest, principal, ws.workspace,
                             expires_at=NOW + ttl, db_path=db_path)


# --------------------------------------------------------------------------- #
# Operation matrix on the real ledger                                          #
# --------------------------------------------------------------------------- #
COMMIT_OPS = ["crm.contact.update.commit", "erp.order.commit",
              "sap.business_object.update.commit"]
READ_OPS = ["crm.contact.read", "erp.inventory.read", "sap.business_object.read"]
PREVIEW_OPS = ["crm.contact.update.preview", "erp.order.preview",
               "sap.business_object.update.preview"]


def test_read_ops_zero_effect(db_path, principal):
    for op in READ_OPS:
        conn, _ = _connector([op], db_path)
        r = conn.execute(_commit_req(op, principal, approval_id=None))
        assert r.effect_state == "non_mutating"
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_preview_zero_commit_one(db_path, principal):
    conn, adapter = _connector(PREVIEW_OPS[:1] + COMMIT_OPS[:1], db_path)
    conn.execute(_commit_req(PREVIEW_OPS[0], principal, approval_id=None))
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []
    creq = _commit_req(COMMIT_OPS[0], principal)
    _issue_approval(adapter, db_path, COMMIT_OPS[0], creq)
    r = conn.execute(creq)
    assert r.effect_state == "committed"
    assert len(_ledger.list_effects("run-1", principal, db_path=db_path)) == 1


def test_commit_matrix_real_approval(db_path, principal):
    for i, op in enumerate(COMMIT_OPS):
        conn, adapter = _connector([op], db_path)
        aid = f"appr-{i}"
        req = ConnectorRequest(
            capability_id=op, run_id=f"run-{i}", principal=principal,
            raw_workspace="ws-acme", business_key="obj-1",
            payload={"business_key": "obj-1", "field": "s", "value": "v"},
            approval_id=aid,
        )
        ws = adapter.canonicalize_workspace("tenant-a", "ws-acme")
        rd = providers.request_digest_of(op, ws.workspace, "obj-1", req.payload)
        edigest = adapter.effect_digest_for(op, ws.workspace, rd)
        _approval.issue_approval(aid, edigest, principal, ws.workspace,
                                 expires_at=NOW + 3600, db_path=db_path)
        r = conn.execute(req)
        assert r.effect_state == "committed", (op, r.effect_state)


CAD_MODEL = {"length": 2.0, "area": 0.01, "youngs_modulus": 200e9,
             "force": 1000.0, "elements": 8}


def test_cad_ops_real_compute(db_path, principal):
    conn, _ = _connector(["cad.fea.run"], db_path)
    r = conn.execute(ConnectorRequest(
        capability_id="cad.fea.run", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="part-1",
        payload={"model": CAD_MODEL},
    ))
    assert r.output["tip_displacement"] == pytest.approx(1e-6, rel=1e-9)


def test_cad_library_backed_fea_cross_checks_connector(db_path, principal):
    """The numpy-backed solver agrees with both the analytical form and the
    connector's reference-oracle output within the documented tolerance."""
    from youtab_runtime.enterprise import cad_lib

    conn, _ = _connector(["cad.fea.run"], db_path)
    r = conn.execute(ConnectorRequest(
        capability_id="cad.fea.run", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="part-1", payload={"model": CAD_MODEL},
    ))
    xc = cad_lib.cross_check_against_reference(CAD_MODEL)
    lib = cad_lib.fea_run_numpy(CAD_MODEL)
    assert lib["backend"] == "numpy"
    assert lib["tip_displacement"] == pytest.approx(r.output["tip_displacement"], rel=1e-6)
    assert xc  # raised if the two backends diverged


# --------------------------------------------------------------------------- #
# Approval / authority negative proofs                                         #
# --------------------------------------------------------------------------- #
def test_commit_without_approval_refused(db_path, principal):
    conn, _ = _connector(["crm.contact.update.commit"], db_path)
    req = _commit_req("crm.contact.update.commit", principal, approval_id=None)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req)
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_expired_approval_creates_no_effect(db_path, principal):
    conn, adapter = _connector(["crm.contact.update.commit"], db_path)
    req = _commit_req("crm.contact.update.commit", principal)
    _issue_approval(adapter, db_path, "crm.contact.update.commit", req, ttl=-1)
    with pytest.raises(Exception):
        conn.execute(req)
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_changed_payload_reused_approval_rejected(db_path, principal):
    conn, adapter = _connector(["crm.contact.update.commit"], db_path)
    good = _commit_req("crm.contact.update.commit", principal)
    _issue_approval(adapter, db_path, "crm.contact.update.commit", good)
    # Different payload -> different request digest -> approval digest mismatch.
    bad = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1",
        principal=principal, raw_workspace="ws-acme", business_key="obj-1",
        payload={"business_key": "obj-1", "field": "status", "value": "TAMPERED"},
        approval_id="appr-1",
    )
    with pytest.raises(Exception):
        conn.execute(bad)
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_approval_single_use_second_effect_refused(db_path, principal):
    conn, adapter = _connector(["crm.contact.update.commit"], db_path)
    req = _commit_req("crm.contact.update.commit", principal)
    _issue_approval(adapter, db_path, "crm.contact.update.commit", req)
    conn.execute(req)  # consumes approval, commits effect
    # A DIFFERENT business key reusing the same approval id must fail (consumed).
    req2 = _commit_req("crm.contact.update.commit", principal, business_key="obj-2")
    with pytest.raises(Exception):
        conn.execute(req2)


# --------------------------------------------------------------------------- #
# Identity / isolation                                                         #
# --------------------------------------------------------------------------- #
def test_changed_capability_version_new_effect(db_path, principal):
    # Two manifests differing only in version -> different effect identity.
    conn1, ad1 = _connector(["crm.contact.update.commit"], db_path)
    req = _commit_req("crm.contact.update.commit", principal)
    _issue_approval(ad1, db_path, "crm.contact.update.commit", req)
    r1 = conn1.execute(req)
    # Build a v2 manifest for the same op.
    reg = _registry()
    cap = _cap_dict("crm.contact.update.commit")
    cap["capability_version"] = "2.0.0"
    signed = ManifestSigner(reg, TEST_ONLY_ISSUER, "tk").sign(
        [cap], issued_at=NOW - 10, expires_at=NOW + 100_000)
    allow = AllowlistPolicy({("crm.contact.update.commit", "2.0.0")}, {"reference"})
    m2 = ManifestVerifier(reg, allow, production=False, clock=lambda: NOW).verify(signed)
    conn2 = Lane1GovernedConnector(
        m2, Lane1AuthorityAdapter(db_path=db_path, clock=lambda: NOW),
        RuntimeIdempotencyPolicy(set()), WorkerBoundary(repo_root=REPO_ROOT),
        production=True, db_path=db_path)
    _issue_approval(conn2._authority, db_path, "crm.contact.update.commit", req,
                    approval_id="appr-2")
    req2 = ConnectorRequest(
        capability_id="crm.contact.update.commit", run_id="run-1",
        principal=principal, raw_workspace="ws-acme", business_key="obj-1",
        payload=req.payload, approval_id="appr-2")
    r2 = conn2.execute(req2)
    assert r1.effect_id != r2.effect_id  # version folded into identity


def test_cross_tenant_isolation(db_path):
    a, b = Principal("tenant-a", "user-a"), Principal("tenant-b", "user-b")
    conn, adapter = _connector(["crm.contact.update.commit"], db_path)
    req = _commit_req("crm.contact.update.commit", a)
    _issue_approval(adapter, db_path, "crm.contact.update.commit", req)
    r = conn.execute(req)
    assert _ledger.get_effect(r.effect_id, b, db_path=db_path) is None
    assert _ledger.get_effect(r.effect_id, a, db_path=db_path) is not None


def test_foreign_workspace_receipt_rejected(db_path, principal):
    conn, adapter = _connector(["crm.contact.update.commit"], db_path)
    req = _commit_req("crm.contact.update.commit", principal)
    _issue_approval(adapter, db_path, "crm.contact.update.commit", req)
    conn.execute(req)
    assert conn.get_commit_receipt("run-1", principal, "ws-acme",
                                   "crm.contact.update.commit", "obj-1") is not None
    assert conn.get_commit_receipt("run-1", principal, "ws-globex",
                                   "crm.contact.update.commit", "obj-1") is None


# --------------------------------------------------------------------------- #
# Retry / single-winner / crash                                               #
# --------------------------------------------------------------------------- #
def test_retry_after_commit_single_winner(db_path, principal):
    conn, adapter = _connector(["crm.contact.update.commit"], db_path)
    req = _commit_req("crm.contact.update.commit", principal)
    _issue_approval(adapter, db_path, "crm.contact.update.commit", req)
    r1 = conn.execute(req)
    r2 = conn.execute(req)  # duplicate -> loses the claim, dedups
    assert r1.effect_id == r2.effect_id
    assert r2.deduplicated is True and r2.effect_state == "committed"
    assert len(_ledger.list_effects("run-1", principal, db_path=db_path)) == 1


def _crash_conn(db_path, script):
    worker = WorkerBoundary(python_argv=[sys.executable, "-c", script],
                            repo_root=REPO_ROOT)
    conn, adapter = _connector(["crm.contact.update.commit"], db_path, worker=worker)
    req = _commit_req("crm.contact.update.commit", Principal("tenant-a", "user-a"))
    _issue_approval(adapter, db_path, "crm.contact.update.commit", req)
    return conn, req


def test_worker_crash_after_effect_unknown_not_retried(db_path):
    conn, req = _crash_conn(db_path, "import sys; sys.stdin.read(); sys.exit(3)")
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req)
    eff = _ledger.list_effects("run-1", req.principal, db_path=db_path)
    assert len(eff) == 1 and eff[0].state.value == "unknown"
    r = conn.execute(req)  # retry must NOT re-execute
    assert r.deduplicated is True and r.effect_state == "unknown"


def test_worker_timeout_marks_unknown(db_path):
    conn, req = _crash_conn(db_path, "import sys,time; sys.stdin.read(); time.sleep(60)")
    conn._deadline = 2.0
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req)
    eff = _ledger.list_effects("run-1", req.principal, db_path=db_path)
    assert eff[0].state.value == "unknown"


def test_worker_malformed_fails_closed(db_path):
    conn, req = _crash_conn(db_path, "import sys; sys.stdin.read(); print('x{{')")
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req)
    eff = _ledger.list_effects("run-1", req.principal, db_path=db_path)
    assert eff[0].state.value == "unknown"


def test_worker_cannot_forge_receipt(db_path):
    forged = ("import sys,json; sys.stdin.read();"
              "print(json.dumps({'ok':True,'result':{'record_id':'x','revision':'y',"
              "'provenance':'REFERENCE','result_digest':'z'},"
              "'receipt':{'state':'committed-by-worker'}}))")
    conn, req = _crash_conn(db_path, forged)
    r = conn.execute(req)
    assert r.effect_state == "committed"  # from the ledger, not the worker
    eff = _ledger.list_effects("run-1", req.principal, db_path=db_path)
    assert eff[0].state.value == "committed"


# --------------------------------------------------------------------------- #
# Lease loss / reconciliation-without-evidence                                 #
# --------------------------------------------------------------------------- #
def test_lease_loss_blocks_commit(db_path, principal):
    # A worker that succeeds, but we reconcile the effect away mid-flight to
    # simulate lease loss: patch the worker to reconcile before returning.
    op = "crm.contact.update.commit"
    conn, adapter = _connector([op], db_path)
    req = _commit_req(op, principal)
    _issue_approval(adapter, db_path, op, req)
    # Pre-compute the effect id and, via a wrapper worker, move it to
    # reconciliation_required before settle.
    orig = conn._run_worker

    def sabotage(cap, ws, r, rd, payload=None):
        out = orig(cap, ws, r, rd, payload)
        scope = conn._effect_identity(cap, ws, rd, None)
        eid = _ledger.compute_effect_id(r.run_id, r.principal, cap.operation_id, scope)
        _ledger.mark_reconciliation_required(eid, r.principal, db_path=db_path)
        return out

    conn._run_worker = sabotage
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req)
    eff = _ledger.list_effects("run-1", principal, db_path=db_path)
    assert eff[0].state.value == "reconciliation_required"  # not committed


def test_reconcile_query_only_and_needs_evidence(db_path, principal):
    ops = ["crm.contact.update.commit", "crm.effect.reconcile"]
    conn, adapter = _connector(ops, db_path)
    creq = _commit_req("crm.contact.update.commit", principal)
    _issue_approval(adapter, db_path, "crm.contact.update.commit", creq)
    conn.execute(creq)
    before = len(_ledger.list_effects("run-1", principal, db_path=db_path))
    recon = ConnectorRequest(
        capability_id="crm.effect.reconcile", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="obj-1",
        payload={"checked_operation_id": "crm.contact.update.commit",
                 "business_key": "obj-1", "original_request_digest": "x"})
    r = conn.execute(recon)
    assert r.reconciled is True
    assert len(_ledger.list_effects("run-1", principal, db_path=db_path)) == before


# --------------------------------------------------------------------------- #
# Manifest / provenance                                                        #
# --------------------------------------------------------------------------- #
def test_tampered_manifest_rejected_before_connector(db_path):
    reg = _registry()
    cap = _cap_dict("crm.contact.read")
    signed = ManifestSigner(reg, TEST_ONLY_ISSUER, "tk").sign(
        [cap], issued_at=NOW - 10, expires_at=NOW + 100_000)
    signed["capabilities"][0]["risk_class"] = "low"  # tamper
    allow = AllowlistPolicy({("crm.contact.read", "1.0.0")}, {"reference"})
    with pytest.raises(ManifestError):
        ManifestVerifier(reg, allow, production=False, clock=lambda: NOW).verify(signed)


def test_live_capability_refused(db_path, principal):
    conn, _ = _connector(["crm.contact.read"], db_path,
                         provenance="LIVE", provider="salesforce")
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(_commit_req("crm.contact.read", principal, approval_id=None))


def test_provenance_confusion_fails_closed(db_path):
    liar = ("import sys,json; sys.stdin.read();"
            "print(json.dumps({'ok':True,'result':{'record_id':'x','revision':'y',"
            "'provenance':'LIVE','result_digest':'z'}}))")
    conn, req = _crash_conn(db_path, liar)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req)


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
    print(f"\nlane1_integration standalone: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
