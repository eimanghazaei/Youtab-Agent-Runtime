"""Production-path proofs: adapter production=True with a real (non-test)
Ed25519 effect-authorization key, and consume-before-begin failure semantics.

The private key exists ONLY inside the test process (ephemeral, never committed).
This proves the Runtime PRODUCTION verification path (production=True, non-test
issuer/key loaded through the real authority registry). It does NOT permit any
LIVE Simorgh/Gateway/vendor claim.
"""

from __future__ import annotations

import base64
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.effect_authorization import (
    TEST_AUTHORITY_KEY_PREFIX,
    EffectAuthorization,
    TestEffectAuthority,
)
from youtab_runtime.enterprise import recovery as _recovery
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
M_ISSUER, M_KEY = "gateway-authority", "gw-m1"          # manifest signer
A_ISSUER, A_KEY = "brain-effect-authority", "brain-a1"  # NON-test authority key
OP = "crm.contact.update.commit"
NOW = 5_000_000
NOW_DT = datetime.fromtimestamp(NOW, UTC)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "run_journal.db"


@pytest.fixture()
def principal():
    return Principal("tenant-a", "user-a")


def _cap_dict(op):
    oc = providers.operation_class(op)
    return {
        "capability_id": op, "capability_version": "1.0.0", "operation_id": op,
        "request_schema": providers.request_schema_for(op),
        "response_schema": providers.response_schema_for(op),
        "tenant_scope": "tenant-a", "workspace_scope": "ws-acme",
        "risk_class": "high" if oc == "commit" else "read",
        "approval_required": oc == "commit",
        "idempotency": {"supported": oc == "commit",
                        "semantics": "exactly_once" if oc == "commit" else "none"},
        "receipt_supported": True, "reconciliation_supported": True,
        "provenance": "REFERENCE", "provider": "reference",
    }


def _prod_manifest(op):
    signer = Ed25519ManifestSigner.generate(M_ISSUER, M_KEY)
    registry = PublicKeyRegistry({M_ISSUER: {M_KEY: signer.public_bytes()}})
    signed = signer.sign([_cap_dict(op)], issued_at=NOW - 10, expires_at=NOW + 100_000,
                         tenant_scope="tenant-a", workspace_scope="ws-acme")
    allow = AllowlistPolicy({(op, "1.0.0")}, {"reference"})
    return CryptoManifestVerifier(registry, allow, production=True,
                                  clock=lambda: NOW).verify(signed)


def _ephemeral_authority_key():
    """An ephemeral Ed25519 keypair; private key stays in-process only."""
    sk = Ed25519PrivateKey.generate()
    raw = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return sk, base64.b64encode(raw).decode("ascii")


def _prod_adapter(db_path, pub_b64, *, key_id=A_KEY):
    # production=True: authority keys are the trusted PRODUCTION registry (public
    # only). A test-prefixed key would be refused before lookup.
    return Lane1AuthorityAdapter(
        production=True, authority_public_keys={key_id: pub_b64},
        db_path=db_path, clock=lambda: NOW)


def _connector(db_path, adapter, op=OP, worker=None):
    return Lane1GovernedConnector(
        _prod_manifest(op), adapter, RuntimeIdempotencyPolicy(set()),
        worker or WorkerBoundary(repo_root=REPO_ROOT),
        production=True, db_path=db_path, deadline_seconds=30.0)


def _req(op=OP, principal=None, *, business_key="obj-1", run_id="run-1"):
    principal = principal or Principal("tenant-a", "user-a")
    return ConnectorRequest(
        capability_id=op, run_id=run_id, principal=principal,
        raw_workspace="ws-acme", business_key=business_key,
        payload={"business_key": business_key, "field": "status", "value": "v"})


def _mint(sk, adapter, req, *, key_id=A_KEY, issuer=A_ISSUER, op=OP,
          authz_id="authz-prod-0000001", ttl=3600, business_key=None,
          tenant=None, user=None, effect_digest=None):
    ws = adapter.canonicalize_workspace(req.principal.tenant, req.raw_workspace)
    rd = providers.request_digest_of(op, ws.workspace, req.business_key, req.payload)
    ed = effect_digest or adapter.effect_digest_for(op, ws.workspace, rd)
    unsigned = EffectAuthorization(
        issuer=issuer, authorization_id=authz_id,
        tenant_id=tenant or req.principal.tenant, user_id=user or req.principal.user,
        workspace_id=ws.workspace, command_id="cmd-00000001", capability=op,
        operation="write", effect_digest=ed,
        issued_at=NOW_DT - timedelta(seconds=10),
        expires_at=NOW_DT + timedelta(seconds=ttl), key_id=key_id, signature="0" * 64)
    sig = sk.sign(unsigned.canonical_payload())
    return unsigned.model_copy(update={"signature": base64.b64encode(sig).decode("ascii")})


# --------------------------------------------------------------------------- #
# Item 1 — production=True authority path                                      #
# --------------------------------------------------------------------------- #
def test_production_authority_commit_succeeds(db_path, principal):
    sk, pub = _ephemeral_authority_key()
    adapter = _prod_adapter(db_path, pub)
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    r = conn.execute(req, authorization=_mint(sk, adapter, req))
    assert r.effect_state == "committed"
    assert len(_ledger.list_effects("run-1", principal, db_path=db_path)) == 1
    # workspace-bound receipt visible in-workspace, hidden cross-workspace.
    assert conn.get_commit_receipt("run-1", principal, "ws-acme", OP, "obj-1") is not None
    assert conn.get_commit_receipt("run-1", principal, "ws-globex", OP, "obj-1") is None


def test_production_uses_real_subprocess(db_path, principal):
    sk, pub = _ephemeral_authority_key()
    adapter = _prod_adapter(db_path, pub)
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    r = conn.execute(req, authorization=_mint(sk, adapter, req))
    assert r.output["provenance"] == "REFERENCE" and r.output["record_id"]


def test_test_issuer_rejected_in_production(db_path, principal):
    # A test-prefixed authority key with adapter production=True — refused.
    test_auth = TestEffectAuthority()
    adapter = Lane1AuthorityAdapter(
        production=True, test_authority_keys=test_auth.keyring(),
        authority_public_keys={}, db_path=db_path, clock=lambda: NOW)
    assert test_auth.key_id.startswith(TEST_AUTHORITY_KEY_PREFIX)
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    ws = adapter.canonicalize_workspace("tenant-a", "ws-acme")
    rd = providers.request_digest_of(OP, ws.workspace, "obj-1", req.payload)
    ed = adapter.effect_digest_for(OP, ws.workspace, rd)
    auth = test_auth.mint(
        authorization_id="authz-test-000001", tenant_id="tenant-a", user_id="user-a",
        workspace_id=ws.workspace, command_id="cmd-00000001", capability=OP,
        operation="write", effect_digest=ed,
        issued_at=NOW_DT - timedelta(seconds=10), expires_at=NOW_DT + timedelta(hours=1))
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=auth)
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_unknown_key_rejected(db_path, principal):
    sk, pub = _ephemeral_authority_key()
    adapter = _prod_adapter(db_path, pub)  # trusts A_KEY
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    # Authorization signed with a DIFFERENT key id not in the registry.
    auth = _mint(sk, adapter, req, key_id="unknown-key")
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=auth)
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_revoked_key_rejected(db_path, principal):
    # Revocation in the Lane-1 authority model = the key id is no longer in the
    # trusted production registry. The signer holds A_KEY but the adapter trusts
    # only a rotated key id, so the old (revoked) key fails closed.
    sk, pub = _ephemeral_authority_key()
    adapter = Lane1AuthorityAdapter(
        production=True, authority_public_keys={"brain-a2-rotated": pub},
        db_path=db_path, clock=lambda: NOW)
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    auth = _mint(sk, adapter, req, key_id=A_KEY)  # revoked/old key id
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=auth)


def test_wrong_workspace_rejected(db_path, principal):
    sk, pub = _ephemeral_authority_key()
    adapter = _prod_adapter(db_path, pub)
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    # Mint bound to a different workspace_id than the request resolves to.
    auth = _mint(sk, adapter, req)
    bad = auth.model_copy(update={"workspace_id": "tenant-a/ws-other"})
    # Re-sign so the signature is valid but the binding is wrong.
    bad = bad.model_copy(update={"signature": base64.b64encode(
        sk.sign(bad.canonical_payload())).decode("ascii")})
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=bad)


def test_wrong_request_digest_rejected(db_path, principal):
    sk, pub = _ephemeral_authority_key()
    adapter = _prod_adapter(db_path, pub)
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    # Authorization minted for a different effect digest (copied from elsewhere).
    auth = _mint(sk, adapter, req, effect_digest="a" * 64)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=auth)


def test_expired_authorization_rejected(db_path, principal):
    sk, pub = _ephemeral_authority_key()
    adapter = _prod_adapter(db_path, pub)
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=_mint(sk, adapter, req, ttl=-1))
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


def test_copied_authorization_rejected(db_path, principal):
    sk, pub = _ephemeral_authority_key()
    adapter = _prod_adapter(db_path, pub)
    conn = _connector(db_path, adapter)
    good = _req(principal=principal)
    auth = _mint(sk, adapter, good)  # bound to good's digest
    other = _req(principal=principal, business_key="obj-2")
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(other, authorization=auth)  # digest mismatch


def test_authorization_replay_rejected(db_path, principal):
    sk, pub = _ephemeral_authority_key()
    adapter = _prod_adapter(db_path, pub)
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    auth = _mint(sk, adapter, req)
    conn.execute(req, authorization=auth)  # consumes single-use id
    # Replay the SAME authorization id for a different effect -> already consumed.
    req2 = _req(principal=principal, business_key="obj-2")
    auth2 = _mint(sk, adapter, req2, authz_id=auth.authorization_id)
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req2, authorization=auth2)


# --------------------------------------------------------------------------- #
# Item 2 — consume-before-begin failure semantics                             #
# --------------------------------------------------------------------------- #
def test_consume_then_begin_failure_is_recoverable(db_path, principal, monkeypatch):
    sk, pub = _ephemeral_authority_key()
    adapter = _prod_adapter(db_path, pub)
    conn = _connector(db_path, adapter)
    req = _req(principal=principal)
    auth = _mint(sk, adapter, req)

    # Inject a failure at effect creation, AFTER the authorization is consumed.
    def _boom(*a, **k):
        raise RuntimeError("simulated ledger failure at begin_effect")

    monkeypatch.setattr(_ledger, "begin_effect", _boom)
    with pytest.raises(Lane1GovernedConnectorError) as ei:
        conn.execute(req, authorization=auth)
    assert _recovery.ORPHAN_REASON in str(ei.value)

    # No external execution, no committed effect.
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []
    # Deterministic recovery record persisted, keyed by the authorization id.
    rec = _recovery.get_orphaned_authorization(auth.authorization_id, db_path=db_path)
    assert rec is not None and rec["reason"] == _recovery.ORPHAN_REASON
    assert rec["authorization_id"] == auth.authorization_id

    # A retry with the SAME (now consumed) authorization is refused — no double
    # execution, even once begin_effect works again.
    monkeypatch.undo()
    with pytest.raises(Lane1GovernedConnectorError):
        conn.execute(req, authorization=auth)
    assert _ledger.list_effects("run-1", principal, db_path=db_path) == []


if __name__ == "__main__":  # pragma: no cover - standalone smoke run
    import inspect
    import tempfile
    import traceback
    from pathlib import Path as _Path

    class _MP:
        def __init__(self):
            self._u = []

        def setattr(self, obj, name, val):
            self._u.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, val in reversed(self._u):
                setattr(obj, name, val)
            self._u = []

    import shutil

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        params = inspect.signature(fn).parameters
        td = tempfile.mkdtemp()
        kwargs = {}
        if "db_path" in params:
            kwargs["db_path"] = _Path(td) / "run_journal.db"
        if "principal" in params:
            kwargs["principal"] = Principal("tenant-a", "user-a")
        mp = _MP()
        if "monkeypatch" in params:
            kwargs["monkeypatch"] = mp
        try:
            fn(**kwargs)
            passed += 1
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        finally:
            mp.undo()
            # Windows can briefly hold the sqlite file after close; cleanup is
            # best-effort and never affects the test result.
            shutil.rmtree(td, ignore_errors=True)
    print(f"\nlane1_production_authority standalone: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
