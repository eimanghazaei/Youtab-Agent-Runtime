"""Integrated Lane-1 (current authority) + Lane-2 adversarial proofs.

Runs the governed connector against the REAL current Lane-1 authority model:
  * manifests verified with the PRODUCTION Ed25519 verifier (production=True,
    trusted non-test issuer/key) — the production manifest path, not HMAC;
  * effect authority is an externally-signed EffectAuthorization that the Runtime
    only VERIFIES and single-use CONSUMES via approval.reserve_and_consume_
    authorization — the Runtime never mints (issue_approval is gone);
  * the canonical effect ledger, worker lease + evidence reconcile, and real
    worker subprocesses.

The effect-authorization signer is TestEffectAuthority (test-prefixed key,
adapter production=False) because production authority keys come from the Brain
(a remaining Gateway/Brain dependency); the production authority guard is proven
negatively (a test authority is refused when the adapter is production=True).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.effect_authorization import TestEffectAuthority
from youtab_runtime.enterprise import reference_providers as providers
from youtab_runtime.enterprise.authority import RuntimeIdempotencyPolicy
from youtab_runtime.enterprise.connector import ConnectorRequest
from youtab_runtime.enterprise.lane1_adapter import Lane1AuthorityAdapter
from youtab_runtime.enterprise.lane1_connector import (
    Lane1GovernedConnector,
    Lane1GovernedConnectorError,
)
from youtab_runtime.enterprise.manifest import AllowlistPolicy
from youtab_runtime.enterprise.manifest_crypto import (
    CryptoManifestVerifier,
    Ed25519ManifestSigner,
    PublicKeyRegistry,
)
from youtab_runtime.enterprise.worker_boundary import WorkerBoundary
from youtab_runtime.run_journal import Principal

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ISSUER = "gateway-authority"
KEY_ID = "gw-k1"
NOW = 4_000_000
NOW_DT = datetime.fromtimestamp(NOW, UTC)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "run_journal.db"


@pytest.fixture()
def principal():
    return Principal("tenant-a", "user-a")


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


def _prod_manifest(operation_ids, *, production=True, **kw):
    signer = Ed25519ManifestSigner.generate(ISSUER, KEY_ID)
    registry = PublicKeyRegistry({ISSUER: {KEY_ID: signer.public_bytes()}})
    caps = [_cap_dict(op, **kw) for op in operation_ids]
    signed = signer.sign(
        caps, issued_at=NOW - 10, expires_at=NOW + 100_000,
        tenant_scope=kw.get("tenant", "tenant-a"),
        workspace_scope=kw.get("workspace", "ws-acme"),
    )
    allow = AllowlistPolicy({(op, "1.0.0") for op in operation_ids},
                            {kw.get("provider", "reference")})
    verifier = CryptoManifestVerifier(registry, allow, production=production,
                                      clock=lambda: NOW)
    return verifier.verify(signed)


def _connector(operation_ids, db_path, *, authority=None, worker=None,
               adapter_production=False, **kw):
    authority = authority or TestEffectAuthority()
    manifest = _prod_manifest(operation_ids, **kw)
    adapter = Lane1AuthorityAdapter(
        production=adapter_production, test_authority_keys=authority.keyring(),
        db_path=db_path, clock=lambda: NOW,
    )
    conn = Lane1GovernedConnector(
        manifest, adapter, RuntimeIdempotencyPolicy(set()),
        worker or WorkerBoundary(repo_root=REPO_ROOT),
        production=True, db_path=db_path, deadline_seconds=30.0,
    )
    return conn, adapter, authority


def _req(op, principal, *, business_key="obj-1", run_id="run-1", workspace="ws-acme",
         payload=None):
    if payload is None:
        payload = ({"business_key": business_key, "field": "status", "value": "active"}
                   if providers.operation_class(op) in ("preview", "commit")
                   else {"business_key": business_key})
    return ConnectorRequest(
        capability_id=op, run_id=run_id, principal=principal,
        raw_workspace=workspace, business_key=business_key, payload=payload,
    )


def _mint(authority, adapter, op, req, *, authz_id="authz-000000000001", ttl=3600):
    ws = adapter.canonicalize_workspace(req.principal.tenant, req.raw_workspace)
    rd = providers.request_digest_of(op, ws.workspace, req.business_key, req.payload)
    edigest = adapter.effect_digest_for(op, ws.workspace, rd)
    return authority.mint(
        authorization_id=authz_id, tenant_id=req.principal.tenant,
        user_id=req.principal.user, workspace_id=ws.workspace,
        command_id="cmd-00000001", capability=op, operation="write",
        effect_digest=edigest, issued_at=NOW_DT - timedelta(seconds=10),
        expires_at=NOW_DT + timedelta(seconds=ttl),
    )


# --------------------------------------------------------------------------- #
# Production manifest path + no-mint authority                                 #
# --------------------------------------------------------------------------- #
def test_commit_with_signed_authorization(db_path, principal):
    op = "crm.contact.update.commit"
    conn, adapter, authority = _connector([op], db_path)
    req = _req(op, principal)
    r = conn.execute(req, authorization=_mint(authority, adapter, op, req))
    assert r.effect_state == "committed"
    assert len(_ledger.list_effects("run-1", principal, db_path=db_path)) == 1


def test_commit_without_authorization_refused(db_path, principal):
    op = "crm.contact.update.commit"
    conn, _, _ = _connector([op], db_path)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(_req(op, principal))
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_commit_capability_without_approval_required_refused(db_path, principal):
    """A signed manifest that marks a COMMIT op approval_required=False must be
    refused: mutating effects are always per-effect authorization-gated."""
    op = "crm.contact.update.commit"
    signer = Ed25519ManifestSigner.generate(ISSUER, KEY_ID)
    registry = PublicKeyRegistry({ISSUER: {KEY_ID: signer.public_bytes()}})
    cap = _cap_dict(op)
    cap["approval_required"] = False  # mislabelled mutating capability
    cap["idempotency"] = {"supported": False, "semantics": "none"}
    signed = signer.sign([cap], issued_at=NOW - 10, expires_at=NOW + 100_000,
                         tenant_scope="tenant-a", workspace_scope="ws-acme")
    allow = AllowlistPolicy({(op, "1.0.0")}, {"reference"})
    manifest = CryptoManifestVerifier(registry, allow, production=True,
                                      clock=lambda: NOW).verify(signed)
    authority = TestEffectAuthority()
    adapter = Lane1AuthorityAdapter(production=False, test_authority_keys=authority.keyring(),
                                    db_path=db_path, clock=lambda: NOW)
    conn = Lane1GovernedConnector(
        manifest, adapter, RuntimeIdempotencyPolicy(set()),
        WorkerBoundary(repo_root=REPO_ROOT), production=True, db_path=db_path)
    req = _req(op, principal)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=_mint(authority, adapter, op, req))
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_runtime_cannot_mint_no_issue_approval():
    import youtab_runtime.approval as ap
    from youtab_runtime.enterprise import lane1_adapter
    # The Runtime-minted approval API is gone from Lane-1 entirely...
    assert not hasattr(ap, "issue_approval")
    assert not hasattr(ap, "consume_approval")
    # ...and the adapter never CALLS it (docstring mentions are not calls).
    src = open(lane1_adapter.__file__, encoding="utf-8").read()
    assert "issue_approval(" not in src
    assert "reserve_and_consume_authorization" in src


def test_read_ops_zero_effect(db_path, principal):
    for op in ["crm.contact.read", "erp.inventory.read", "sap.business_object.read"]:
        conn, _, _ = _connector([op], db_path)
        assert conn.execute(_req(op, principal)).effect_state == "non_mutating"
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_preview_zero_commit_one(db_path, principal):
    ops = ["crm.contact.update.preview", "crm.contact.update.commit"]
    conn, adapter, authority = _connector(ops, db_path)
    conn.execute(_req("crm.contact.update.preview", principal))
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []
    creq = _req("crm.contact.update.commit", principal)
    conn.execute(creq, authorization=_mint(authority, adapter,
                                           "crm.contact.update.commit", creq))
    assert len(_ledger.list_effects("run-1", principal, db_path=db_path)) == 1


def test_commit_matrix(db_path, principal):
    for i, op in enumerate(["crm.contact.update.commit", "erp.order.commit",
                            "sap.business_object.update.commit"]):
        conn, adapter, authority = _connector([op], db_path)
        req = _req(op, principal, run_id=f"run-{i}")
        r = conn.execute(req, authorization=_mint(authority, adapter, op, req,
                                                  authz_id=f"authz-00000000000{i}"))
        assert r.effect_state == "committed", (op, r.effect_state)


CAD_MODEL = {"length": 2.0, "area": 0.01, "youngs_modulus": 200e9,
             "force": 1000.0, "elements": 8}


def test_cad_library_backed_fea_governed(db_path, principal):
    from youtab_runtime.enterprise import cad_lib

    op = "cad.fea.run"
    conn, adapter, authority = _connector([op], db_path)
    req = _req(op, principal, business_key="part-1", payload={"model": CAD_MODEL})
    r = conn.execute(req, authorization=_mint(authority, adapter, op, req))
    assert r.output["backend"] == "numpy"
    assert r.effect_state == "committed"
    assert r.output["tip_displacement"] == pytest.approx(1e-6, rel=1e-9)
    assert cad_lib.cross_check_against_reference(CAD_MODEL)


# --------------------------------------------------------------------------- #
# Authorization adversarial                                                    #
# --------------------------------------------------------------------------- #
def test_changed_request_reusing_authorization_rejected(db_path, principal):
    op = "crm.contact.update.commit"
    conn, adapter, authority = _connector([op], db_path)
    auth = _mint(authority, adapter, op, _req(op, principal))
    bad = _req(op, principal, payload={"business_key": "obj-1", "field": "status",
                                       "value": "TAMPERED"})
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(bad, authorization=auth)
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_authorization_single_use(db_path, principal):
    op = "crm.contact.update.commit"
    conn, adapter, authority = _connector([op], db_path)
    req = _req(op, principal)
    auth = _mint(authority, adapter, op, req)
    conn.execute(req, authorization=auth)
    req2 = _req(op, principal, business_key="obj-2")
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req2, authorization=auth)


def test_expired_authorization_creates_no_effect(db_path, principal):
    op = "crm.contact.update.commit"
    conn, adapter, authority = _connector([op], db_path)
    req = _req(op, principal)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=_mint(authority, adapter, op, req, ttl=-1))
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_foreign_key_authorization_rejected(db_path, principal):
    op = "crm.contact.update.commit"
    conn, adapter, _ = _connector([op], db_path)
    other = TestEffectAuthority()  # signer NOT in the adapter's keyring
    req = _req(op, principal)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=_mint(other, adapter, op, req))


def test_test_authority_refused_when_adapter_production(db_path, principal):
    op = "crm.contact.update.commit"
    conn, adapter, authority = _connector([op], db_path, adapter_production=True)
    req = _req(op, principal)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=_mint(authority, adapter, op, req))
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


# --------------------------------------------------------------------------- #
# Production manifest verification negatives (integrated)                      #
# --------------------------------------------------------------------------- #
def test_hmac_manifest_refused_in_production():
    from youtab_runtime.enterprise.manifest import (
        TEST_ONLY_ISSUER,
        KeyRegistry,
        ManifestError,
        ManifestSigner,
    )
    reg = KeyRegistry({TEST_ONLY_ISSUER: {"tk": b"x" * 32}})
    signed = ManifestSigner(reg, TEST_ONLY_ISSUER, "tk").sign(
        [_cap_dict("crm.contact.read")], issued_at=NOW - 5, expires_at=NOW + 100)
    ed_registry = PublicKeyRegistry({ISSUER: {KEY_ID: b"\x00" * 32}})
    allow = AllowlistPolicy({("crm.contact.read", "1.0.0")}, {"reference"})
    with pytest.raises(ManifestError):
        CryptoManifestVerifier(ed_registry, allow, production=True,
                               clock=lambda: NOW).verify(signed)


def test_production_manifest_rejects_unknown_key():
    from youtab_runtime.enterprise.manifest import ManifestError
    signer = Ed25519ManifestSigner.generate(ISSUER, KEY_ID)
    signed = signer.sign([_cap_dict("crm.contact.read")], issued_at=NOW - 5,
                         expires_at=NOW + 100, tenant_scope="tenant-a",
                         workspace_scope="ws-acme")
    allow = AllowlistPolicy({("crm.contact.read", "1.0.0")}, {"reference"})
    with pytest.raises(ManifestError):
        CryptoManifestVerifier(PublicKeyRegistry({}), allow, production=True,
                               clock=lambda: NOW).verify(signed)


# --------------------------------------------------------------------------- #
# Ledger / isolation / crash on the real spine                                 #
# --------------------------------------------------------------------------- #
def test_cross_tenant_isolation(db_path):
    a, b = Principal("tenant-a", "user-a"), Principal("tenant-b", "user-b")
    op = "crm.contact.update.commit"
    conn, adapter, authority = _connector([op], db_path)
    req = _req(op, a)
    r = conn.execute(req, authorization=_mint(authority, adapter, op, req))
    assert _ledger.get_effect(r.effect_id, b, db_path=db_path) is None
    assert _ledger.get_effect(r.effect_id, a, db_path=db_path) is not None


def test_foreign_workspace_receipt_rejected(db_path, principal):
    op = "crm.contact.update.commit"
    conn, adapter, authority = _connector([op], db_path)
    req = _req(op, principal)
    conn.execute(req, authorization=_mint(authority, adapter, op, req))
    assert conn.get_commit_receipt("run-1", principal, "ws-acme", op, "obj-1") is not None
    assert conn.get_commit_receipt("run-1", principal, "ws-globex", op, "obj-1") is None


def test_retry_after_commit_single_winner(db_path, principal):
    op = "crm.contact.update.commit"
    conn, adapter, authority = _connector([op], db_path)
    req = _req(op, principal)
    auth = _mint(authority, adapter, op, req)
    r1 = conn.execute(req, authorization=auth)
    r2 = conn.execute(req, authorization=auth)
    assert r1.effect_id == r2.effect_id and r2.deduplicated is True
    assert len(_ledger.list_effects("run-1", principal, db_path=db_path)) == 1


def _crash_conn(db_path, script):
    worker = WorkerBoundary(python_argv=[sys.executable, "-c", script],
                            repo_root=REPO_ROOT)
    conn, adapter, authority = _connector(["crm.contact.update.commit"], db_path,
                                          worker=worker)
    p = Principal("tenant-a", "user-a")
    req = _req("crm.contact.update.commit", p)
    return conn, req, _mint(authority, adapter, "crm.contact.update.commit", req)


def test_worker_crash_unknown_not_retried(db_path):
    conn, req, auth = _crash_conn(db_path, "import sys; sys.stdin.read(); sys.exit(3)")
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=auth)
    eff = _ledger.list_effects("run-1", req.principal, db_path=db_path)
    assert len(eff) == 1 and eff[0].state.value == "unknown"
    r = conn.execute(req, authorization=auth)
    assert r.deduplicated is True and r.effect_state == "unknown"


def test_worker_cannot_forge_receipt(db_path):
    forged = ("import sys,json; sys.stdin.read();"
              "print(json.dumps({'ok':True,'result':{'record_id':'x','revision':'y',"
              "'provenance':'REFERENCE','result_digest':'z'},"
              "'receipt':{'state':'committed-by-worker'}}))")
    conn, req, auth = _crash_conn(db_path, forged)
    r = conn.execute(req, authorization=auth)
    assert r.effect_state == "committed"


def test_provenance_confusion_fails_closed(db_path):
    liar = ("import sys,json; sys.stdin.read();"
            "print(json.dumps({'ok':True,'result':{'record_id':'x','revision':'y',"
            "'provenance':'LIVE','result_digest':'z'}}))")
    conn, req, auth = _crash_conn(db_path, liar)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=auth)


def test_live_capability_refused(db_path, principal):
    conn, _, _ = _connector(["crm.contact.read"], db_path,
                            provenance="LIVE", provider="salesforce")
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(_req("crm.contact.read", principal))


def test_fabricated_reconciliation_evidence_rejected(db_path):
    # Drive an effect to UNKNOWN via a crashing worker, then try to reconcile it
    # terminal with FABRICATED evidence (wrong operation digest) -> refused.
    conn, req, auth = _crash_conn(db_path, "import sys; sys.stdin.read(); sys.exit(3)")
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=auth)
    eff = _ledger.list_effects("run-1", req.principal, db_path=db_path)[0]
    ws = conn._authority.canonicalize_workspace(req.principal.tenant, req.raw_workspace)
    with pytest.raises(Exception):
        conn._authority.reconcile_to_terminal(
            eff.effect_id, req.principal,
            operation_digest="deadbeef" * 8,  # fabricated / wrong digest
            workspace_id=ws.workspace, outcome="succeeded",
            result_digest="x", provenance="reference",
        )
    # Still ambiguous — never blind-terminal on forged evidence.
    still = _ledger.get_effect(eff.effect_id, req.principal, db_path=db_path)
    assert still.state.value == "unknown"
    # With correct evidence (real operation digest) it resolves terminal.
    state = conn._authority.reconcile_to_terminal(
        eff.effect_id, req.principal, operation_digest=eff.target_scope_digest,
        workspace_id=ws.workspace, outcome="succeeded", result_digest="x",
        provenance="reference",
    )
    assert state == "committed"


def test_reconcile_query_only(db_path, principal):
    ops = ["crm.contact.update.commit", "crm.effect.reconcile"]
    conn, adapter, authority = _connector(ops, db_path)
    creq = _req("crm.contact.update.commit", principal)
    conn.execute(creq, authorization=_mint(authority, adapter,
                                           "crm.contact.update.commit", creq))
    before = len(_ledger.list_effects("run-1", principal, db_path=db_path))
    recon = ConnectorRequest(
        capability_id="crm.effect.reconcile", run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key="obj-1",
        payload={"checked_operation_id": "crm.contact.update.commit",
                 "business_key": "obj-1", "original_request_digest": "x"})
    r = conn.execute(recon)
    assert r.reconciled is True
    assert len(_ledger.list_effects("run-1", principal, db_path=db_path)) == before


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
