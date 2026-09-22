"""Dedicated adversarial proofs for the Lane-2 -> Lane-1 reconciliation adapter.

These tests pin the reconciliation shim added at integration time
(:meth:`Lane1AuthorityAdapter.reconcile_to_terminal`) against the CURRENT
Lane-1 contract: the removed ``worker_lease.EffectEvidence`` is gone, and the
adapter must build a verified
:class:`~youtab_runtime.effect_evidence.ReconciliationEvidence` bound to the
effect's identity, then route it through Lane-1's evidence verifier. The adapter
never relaxes Lane-1 and introduces no second ledger/receipt type.

Every negative case asserts the effect stays NON-terminal (never blind-terminal)
and that a foreign principal can never read the owner's effect/receipt.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.effect_authorization import TestEffectAuthority
from youtab_runtime.enterprise import reference_providers as providers
from youtab_runtime.enterprise.authority import (
    AuthorityError,
    RuntimeIdempotencyPolicy,
)
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
OP = "crm.contact.update.commit"
#: A well-formed 64-hex reference result digest (the reference model requires it).
RD = "a" * 64
CRASH = "import sys; sys.stdin.read(); sys.exit(3)"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "run_journal.db"


def _cap_dict(operation_id, *, workspace="ws-acme", tenant="tenant-a"):
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
        "provenance": "REFERENCE", "provider": "reference",
    }


def _prod_manifest(operation_ids):
    signer = Ed25519ManifestSigner.generate(ISSUER, KEY_ID)
    registry = PublicKeyRegistry({ISSUER: {KEY_ID: signer.public_bytes()}})
    caps = [_cap_dict(op) for op in operation_ids]
    signed = signer.sign(caps, issued_at=NOW - 10, expires_at=NOW + 100_000,
                         tenant_scope="tenant-a", workspace_scope="ws-acme")
    allow = AllowlistPolicy({(op, "1.0.0") for op in operation_ids}, {"reference"})
    verifier = CryptoManifestVerifier(registry, allow, production=True,
                                      clock=lambda: NOW)
    return verifier.verify(signed)


def _connector(db_path, *, script=CRASH):
    authority = TestEffectAuthority()
    worker = WorkerBoundary(python_argv=[sys.executable, "-c", script],
                            repo_root=REPO_ROOT)
    adapter = Lane1AuthorityAdapter(
        production=False, test_authority_keys=authority.keyring(),
        db_path=db_path, clock=lambda: NOW,
    )
    conn = Lane1GovernedConnector(
        _prod_manifest([OP]), adapter, RuntimeIdempotencyPolicy(set()),
        worker, production=True, db_path=db_path, deadline_seconds=30.0,
    )
    return conn, adapter, authority


def _mint(authority, adapter, req, *, authz_id="authz-000000000001"):
    ws = adapter.canonicalize_workspace(req.principal.tenant, req.raw_workspace)
    rd = providers.request_digest_of(OP, ws.workspace, req.business_key, req.payload)
    edigest = adapter.effect_digest_for(OP, ws.workspace, rd)
    return authority.mint(
        authorization_id=authz_id, tenant_id=req.principal.tenant,
        user_id=req.principal.user, workspace_id=ws.workspace,
        command_id="cmd-00000001", capability=OP, operation="write",
        effect_digest=edigest, issued_at=NOW_DT - timedelta(seconds=10),
        expires_at=NOW_DT + timedelta(seconds=3600),
    )


def _unknown_effect(db_path, *, business_key="obj-1", authz_id="authz-000000000001"):
    """Drive one effect to the ambiguous UNKNOWN state via a crashing worker and
    return (conn, adapter, principal, canonical-workspace, effect-record)."""
    conn, adapter, authority = _connector(db_path)
    principal = Principal("tenant-a", "user-a")
    req = ConnectorRequest(
        capability_id=OP, run_id="run-1", principal=principal,
        raw_workspace="ws-acme", business_key=business_key,
        payload={"business_key": business_key, "field": "status", "value": "active"},
    )
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=_mint(authority, adapter, req, authz_id=authz_id))
    eff = [e for e in _ledger.list_effects("run-1", principal, db_path=db_path)
           if e.state.value == "unknown"][0]
    ws = adapter.canonicalize_workspace(principal.tenant, req.raw_workspace)
    return conn, adapter, principal, ws, eff


def _assert_unknown(db_path, principal, eff):
    cur = _ledger.get_effect(eff.effect_id, principal, db_path=db_path)
    assert cur is not None and cur.state.value == "unknown"


# 1) committed reconciliation with valid, reproduced evidence ---------------- #
def test_committed_reconciliation_with_valid_evidence(db_path):
    conn, adapter, principal, ws, eff = _unknown_effect(db_path)
    state = adapter.reconcile_to_terminal(
        eff.effect_id, principal, operation_digest=eff.target_scope_digest,
        workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
        provenance="reference", recompute_reference=lambda: RD,
    )
    assert state == "committed"


# 2) failed reconciliation with valid, reproduced evidence ------------------- #
def test_failed_reconciliation_with_valid_evidence(db_path):
    conn, adapter, principal, ws, eff = _unknown_effect(db_path)
    state = adapter.reconcile_to_terminal(
        eff.effect_id, principal, operation_digest=eff.target_scope_digest,
        workspace_id=ws.workspace, outcome="failed", result_digest=RD,
        provenance="reference", recompute_reference=lambda: RD,
    )
    assert state == "failed"


# 3) foreign tenant/principal/workspace rejection ---------------------------- #
def test_foreign_principal_and_workspace_rejected(db_path):
    conn, adapter, principal, ws, eff = _unknown_effect(db_path)
    foreign = Principal("tenant-x", "user-x")
    # Foreign principal does not own the effect -> refused, stays ambiguous.
    with pytest.raises(AuthorityError):
        adapter.reconcile_to_terminal(
            eff.effect_id, foreign, operation_digest=eff.target_scope_digest,
            workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
            provenance="reference", recompute_reference=lambda: RD,
        )
    _assert_unknown(db_path, principal, eff)
    # Foreign workspace binding -> refused, stays ambiguous.
    with pytest.raises(AuthorityError):
        adapter.reconcile_to_terminal(
            eff.effect_id, principal, operation_digest=eff.target_scope_digest,
            workspace_id="tenant-x/ws-evil", outcome="succeeded", result_digest=RD,
            provenance="reference", recompute_reference=lambda: RD,
        )
    _assert_unknown(db_path, principal, eff)


# 4) wrong effect id / wrong operation digest rejection ---------------------- #
def test_wrong_effect_id_or_digest_rejected(db_path):
    conn, adapter, principal, ws, eff = _unknown_effect(db_path)
    # Wrong operation digest (fabricated) -> OperationDigestMismatch, stays ambiguous.
    with pytest.raises(AuthorityError):
        adapter.reconcile_to_terminal(
            eff.effect_id, principal, operation_digest="deadbeef" * 8,
            workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
            provenance="reference", recompute_reference=lambda: RD,
        )
    _assert_unknown(db_path, principal, eff)
    # Wrong effect id -> not owned / not found, stays ambiguous.
    with pytest.raises(AuthorityError):
        adapter.reconcile_to_terminal(
            "effect-does-not-exist-000", principal,
            operation_digest=eff.target_scope_digest,
            workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
            provenance="reference", recompute_reference=lambda: RD,
        )
    _assert_unknown(db_path, principal, eff)


# 5) stale / replayed evidence rejection ------------------------------------- #
def test_stale_and_replayed_evidence_rejected(db_path):
    conn, adapter, principal, ws, eff = _unknown_effect(db_path)
    # Stale: observed far in the past relative to the adapter clock (NOW).
    with pytest.raises(AuthorityError):
        adapter.reconcile_to_terminal(
            eff.effect_id, principal, operation_digest=eff.target_scope_digest,
            workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
            provenance="reference", recompute_reference=lambda: RD,
            observed_at=NOW_DT - timedelta(days=2), max_age_seconds=60.0,
        )
    _assert_unknown(db_path, principal, eff)
    # Legitimate resolution, then REPLAY: a second reconcile of the now-terminal
    # effect is refused (not in an ambiguous state).
    assert adapter.reconcile_to_terminal(
        eff.effect_id, principal, operation_digest=eff.target_scope_digest,
        workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
        provenance="reference", recompute_reference=lambda: RD,
    ) == "committed"
    with pytest.raises(AuthorityError):
        adapter.reconcile_to_terminal(
            eff.effect_id, principal, operation_digest=eff.target_scope_digest,
            workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
            provenance="reference", recompute_reference=lambda: RD,
        )


# 6) copied evidence rejection ----------------------------------------------- #
def test_copied_evidence_across_effects_rejected(db_path):
    # Two distinct ambiguous effects; evidence "copied" from A (its digest) must
    # not reconcile B, whose stored target-scope digest differs.
    conn, adapter, principal, ws, eff_a = _unknown_effect(
        db_path, business_key="obj-A", authz_id="authz-00000000000A")
    # Create a second UNKNOWN effect in a fresh journal to guarantee a distinct
    # target-scope digest to "copy" from.
    _, _, _, _, eff_second = _unknown_effect(
        db_path.parent / "second.db", business_key="obj-B")
    assert eff_second.target_scope_digest != eff_a.target_scope_digest
    # Reconcile A using B's (copied) operation digest -> mismatch, A stays unknown.
    with pytest.raises(AuthorityError):
        adapter.reconcile_to_terminal(
            eff_a.effect_id, principal,
            operation_digest=eff_second.target_scope_digest,  # copied from B
            workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
            provenance="reference", recompute_reference=lambda: RD,
        )
    _assert_unknown(db_path, principal, eff_a)


# 7) no terminal transition without verified evidence ------------------------ #
def test_no_terminal_transition_without_recompute(db_path):
    conn, adapter, principal, ws, eff = _unknown_effect(db_path)
    # Reference provenance with NO recompute source -> fail closed, stays unknown.
    with pytest.raises(AuthorityError):
        adapter.reconcile_to_terminal(
            eff.effect_id, principal, operation_digest=eff.target_scope_digest,
            workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
            provenance="reference", recompute_reference=None,
        )
    _assert_unknown(db_path, principal, eff)
    # Recompute that does NOT reproduce the digest -> also refused.
    with pytest.raises(AuthorityError):
        adapter.reconcile_to_terminal(
            eff.effect_id, principal, operation_digest=eff.target_scope_digest,
            workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
            provenance="reference", recompute_reference=lambda: "b" * 64,
        )
    _assert_unknown(db_path, principal, eff)


# 8) receipt remains foreign-principal unreadable ---------------------------- #
def test_receipt_foreign_principal_unreadable(db_path):
    conn, adapter, principal, ws, eff = _unknown_effect(db_path)
    assert adapter.reconcile_to_terminal(
        eff.effect_id, principal, operation_digest=eff.target_scope_digest,
        workspace_id=ws.workspace, outcome="succeeded", result_digest=RD,
        provenance="reference", recompute_reference=lambda: RD,
    ) == "committed"
    # Owner can read the committed effect; a foreign principal cannot.
    assert _ledger.get_effect(eff.effect_id, principal, db_path=db_path).state.value \
        == "committed"
    foreign = Principal("tenant-x", "user-x")
    assert _ledger.get_effect(eff.effect_id, foreign, db_path=db_path) is None
