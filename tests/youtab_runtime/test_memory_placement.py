from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from youtab_runtime.memory import (
    PlacementError,
    PlacementVerifier,
    SignedScopePlacement,
    VerifiedPlacement,
)

from .helpers import keypair, signed_envelope


def _gw_keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return priv, base64.b64encode(pub).decode("ascii")


def _placement(priv, *, now=None, tenant="tenant-alpha", principal="user-alpha",
               command_id="command-0001", trace_id="trace-00000001", issuer="youtab-gateway",
               nonce="nonce-placement-0001", expires_delta=timedelta(minutes=5), key_id="gw-key-1"):
    cur = now or datetime.now(UTC)
    raw = {
        "schema_version": "youtab.scope-placement.v1", "algorithm": "ed25519", "issuer": issuer,
        "command_id": command_id, "trace_id": trace_id, "tenant_id": tenant, "principal_id": principal,
        "organization_id": "org-acme", "workspace_id": "ws-sales", "agent_id": "agent-01",
        "run_id": "run-1", "purpose": "default", "delegation_authority_ref": "grant-123",
        "nonce": nonce, "issued_at": (cur - timedelta(seconds=1)).isoformat(),
        "expires_at": (cur + expires_delta).isoformat(), "key_id": key_id,
        "signature": "placeholder-signature-that-is-long-enough",
    }
    unsigned = SignedScopePlacement.model_validate(raw)
    raw["signature"] = base64.b64encode(priv.sign(unsigned.canonical_payload())).decode("ascii")
    return SignedScopePlacement.model_validate(raw)


def _env():
    private, _ = keypair()
    return signed_envelope(private)


def test_valid_placement_verifies_to_scope() -> None:
    priv, pub = _gw_keypair()
    v = PlacementVerifier({"gw-key-1": pub})
    verified = v.verify(_placement(priv), envelope=_env())
    assert isinstance(verified, VerifiedPlacement)
    assert verified.scope.workspace_id == "ws-sales"


def test_bad_signature_fails_closed() -> None:
    priv, pub = _gw_keypair()
    other, _ = _gw_keypair()
    v = PlacementVerifier({"gw-key-1": pub})
    p = _placement(other)  # signed by the WRONG key
    with pytest.raises(PlacementError):
        v.verify(p, envelope=_env())


def test_expired_placement_rejected() -> None:
    priv, pub = _gw_keypair()
    v = PlacementVerifier({"gw-key-1": pub})
    p = _placement(priv, expires_delta=timedelta(seconds=-1))
    with pytest.raises(PlacementError):
        v.verify(p, envelope=_env())


def test_cross_tenant_mismatch_rejected() -> None:
    priv, pub = _gw_keypair()
    v = PlacementVerifier({"gw-key-1": pub})
    p = _placement(priv, tenant="tenant-evil")  # envelope is tenant-alpha
    with pytest.raises(PlacementError):
        v.verify(p, envelope=_env())


def test_unknown_key_id_rejected() -> None:
    priv, pub = _gw_keypair()
    v = PlacementVerifier({"other-key": pub})
    with pytest.raises(PlacementError):
        v.verify(_placement(priv), envelope=_env())


def test_replayed_nonce_rejected() -> None:
    priv, pub = _gw_keypair()
    v = PlacementVerifier({"gw-key-1": pub})
    env = _env()
    p = _placement(priv)
    v.verify(p, envelope=env)
    with pytest.raises(PlacementError):
        v.verify(p, envelope=env)  # same nonce again


def test_command_trace_mismatch_rejected() -> None:
    priv, pub = _gw_keypair()
    v = PlacementVerifier({"gw-key-1": pub})
    p = _placement(priv, command_id="command-9999")
    with pytest.raises(PlacementError):
        v.verify(p, envelope=_env())


def test_verified_placement_cannot_be_hand_forged() -> None:
    # VerifiedPlacement requires verified=True literal; a caller cannot mint one
    # that bypasses the verifier by setting verified=False.
    with pytest.raises(Exception):
        VerifiedPlacement(scope=None, verified=False)  # type: ignore[arg-type]
