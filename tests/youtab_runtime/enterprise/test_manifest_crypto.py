"""Ed25519 signed manifest: adversarial signature + security-contract proofs."""

from __future__ import annotations

import copy

import pytest

from youtab_runtime.enterprise.manifest import (
    TEST_ONLY_ISSUER,
    AllowlistPolicy,
    ManifestError,
)
from youtab_runtime.enterprise.manifest_crypto import (
    CryptoManifestVerifier,
    Ed25519ManifestSigner,
    PublicKeyRegistry,
)

ISSUER = "gateway-authority"
KEY_ID = "k1"
KEY_ID_2 = "k2"

NOW = 1_000_000
TENANT = "tenant-a"
WORKSPACE = "ws-acme"


def _cap(**over):
    base = {
        "capability_id": "crm.contact.update.commit",
        "capability_version": "1.0.0",
        "operation_id": "crm.contact.update.commit",
        "request_schema": {"contact_id": "str", "field": "str", "value": "str"},
        "response_schema": {"record_id": "str", "revision": "str",
                            "domain": "str", "provenance": "str"},
        "tenant_scope": TENANT,
        "workspace_scope": WORKSPACE,
        "risk_class": "high",
        "approval_required": True,
        "idempotency": {"supported": True, "semantics": "exactly_once"},
        "receipt_supported": True,
        "reconciliation_supported": True,
        "provenance": "REFERENCE",
        "provider": "reference",
    }
    base.update(over)
    return base


def _allowlist():
    return AllowlistPolicy(
        capabilities={("crm.contact.update.commit", "1.0.0")},
        providers={"reference"},
    )


def _signer(issuer=ISSUER, key_id=KEY_ID):
    return Ed25519ManifestSigner.generate(issuer, key_id)


def _registry_for(*signers, revoked=None):
    keys: dict[str, dict[str, bytes]] = {}
    for s in signers:
        keys.setdefault(s._issuer, {})[s._key_id] = s.public_bytes()
    return PublicKeyRegistry(keys, revoked=revoked)


def _signed(signer, caps=None, issued_at=NOW - 10, expires_at=NOW + 10_000,
            tenant_scope=TENANT, workspace_scope=WORKSPACE):
    return signer.sign(
        caps or [_cap()],
        issued_at=issued_at, expires_at=expires_at,
        tenant_scope=tenant_scope, workspace_scope=workspace_scope,
    )


def _verifier(registry, production=True, allowlist=None):
    return CryptoManifestVerifier(
        registry, allowlist or _allowlist(),
        production=production, clock=lambda: NOW,
    )


# --------------------------------------------------------------------------- #
# Happy path                                                                   #
# --------------------------------------------------------------------------- #
def test_valid_signed_manifest_verifies():
    signer = _signer()
    m = _verifier(_registry_for(signer)).verify(_signed(signer))
    cap = m.get("crm.contact.update.commit")
    assert cap.operation_id == "crm.contact.update.commit"
    assert cap.provenance.value == "REFERENCE"
    assert cap.approval_required is True
    assert cap.idempotency_semantics.value == "exactly_once"


def test_scope_pin_matches():
    signer = _signer()
    m = _verifier(_registry_for(signer)).verify(
        _signed(signer), expected_tenant=TENANT, expected_workspace=WORKSPACE
    )
    assert len(m) == 1


# --------------------------------------------------------------------------- #
# Signature / issuer / key adversarial                                         #
# --------------------------------------------------------------------------- #
def test_unsigned_manifest_rejected():
    signer = _signer()
    signed = _signed(signer)
    del signed["signature"]
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(signed)


def test_unknown_issuer_rejected():
    signer = _signer()
    signed = _signed(signer)
    signed["issuer"] = "attacker"
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(signed)


def test_unknown_key_id_rejected():
    signer = _signer()
    signed = _signed(signer)
    signed["key_id"] = "kX"
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(signed)


def test_revoked_key_rejected():
    signer = _signer()
    reg = _registry_for(signer, revoked={(ISSUER, KEY_ID)})
    with pytest.raises(ManifestError):
        _verifier(reg).verify(_signed(signer))


def test_expired_manifest_rejected():
    signer = _signer()
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(
            _signed(signer, issued_at=NOW - 100, expires_at=NOW - 1)
        )


def test_not_yet_valid_manifest_rejected():
    signer = _signer()
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(
            _signed(signer, issued_at=NOW + 50, expires_at=NOW + 100)
        )


def test_tampered_capability_field_rejected():
    signer = _signer()
    signed = _signed(signer)
    signed["capabilities"][0]["risk_class"] = "low"  # was high
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(signed)


def test_tampered_schema_rejected():
    signer = _signer()
    signed = _signed(signer)
    signed["capabilities"][0]["request_schema"]["secret"] = "str"
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(signed)


def test_tampered_schema_digest_rejected():
    signer = _signer()
    signed = _signed(signer)
    signed["capabilities"][0]["request_schema_digest"] = "deadbeef"
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(signed)


def test_signature_copied_to_another_capability_rejected():
    """A valid signature from cap A pasted onto cap B (different id/version)."""
    signer = _signer()
    good = _signed(signer)
    stolen_sig = good["capabilities"][0]["signature"]
    forged = copy.deepcopy(good)
    forged["capabilities"][0]["capability_id"] = "crm.contact.read"
    forged["capabilities"][0]["operation_id"] = "crm.contact.read"
    forged["capabilities"][0]["signature"] = stolen_sig
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(forged)


def test_added_capability_breaks_envelope_digest():
    signer = _signer()
    signed = _signed(signer)
    extra = copy.deepcopy(signed["capabilities"][0])
    extra["capability_id"] = "crm.contact.read"
    signed["capabilities"].append(extra)
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(signed)


# --------------------------------------------------------------------------- #
# Algorithm confusion                                                          #
# --------------------------------------------------------------------------- #
def test_algorithm_confusion_hmac_rejected():
    signer = _signer()
    signed = _signed(signer)
    signed["algorithm"] = "hmac-sha256"
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(signed)


def test_algorithm_none_rejected():
    signer = _signer()
    signed = _signed(signer)
    signed["algorithm"] = "none"
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(signed)


# --------------------------------------------------------------------------- #
# Tenant / workspace scope                                                     #
# --------------------------------------------------------------------------- #
def test_tenant_scope_mismatch_rejected():
    signer = _signer()
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(
            _signed(signer), expected_tenant="tenant-z", expected_workspace=WORKSPACE
        )


def test_workspace_scope_mismatch_rejected():
    signer = _signer()
    with pytest.raises(ManifestError):
        _verifier(_registry_for(signer)).verify(
            _signed(signer), expected_tenant=TENANT, expected_workspace="ws-other"
        )


# --------------------------------------------------------------------------- #
# Key rotation                                                                 #
# --------------------------------------------------------------------------- #
def test_key_rotation_old_revoked_new_accepted():
    old = _signer(key_id=KEY_ID)
    new = _signer(key_id=KEY_ID_2)
    reg = _registry_for(old, new)
    # Both verify before rotation.
    assert len(_verifier(reg).verify(_signed(new))) == 1
    # Rotate: revoke the old key id.
    reg.revoke(ISSUER, KEY_ID)
    with pytest.raises(ManifestError):
        _verifier(reg).verify(_signed(old))
    # New key still verifies.
    assert len(_verifier(reg).verify(_signed(new))) == 1


# --------------------------------------------------------------------------- #
# Test-only issuer gating                                                      #
# --------------------------------------------------------------------------- #
def test_test_only_issuer_refused_in_production():
    signer = _signer(issuer=TEST_ONLY_ISSUER, key_id="tk")
    reg = _registry_for(signer)
    with pytest.raises(ManifestError):
        _verifier(reg, production=True).verify(_signed(signer))


def test_test_only_issuer_allowed_when_not_production():
    signer = _signer(issuer=TEST_ONLY_ISSUER, key_id="tk")
    reg = _registry_for(signer)
    m = _verifier(reg, production=False).verify(_signed(signer))
    assert len(m) == 1


if __name__ == "__main__":  # pragma: no cover - standalone smoke run
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\nmanifest_crypto standalone: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
