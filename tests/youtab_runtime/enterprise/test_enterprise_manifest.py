"""Lane-2 signed manifest: adversarial signature + security-contract proofs."""

from __future__ import annotations

import copy

import pytest

from youtab_runtime.enterprise.manifest import (
    TEST_ONLY_ISSUER,
    AllowlistPolicy,
    KeyRegistry,
    ManifestError,
    ManifestSigner,
    ManifestVerifier,
)

ISSUER = "gateway-authority"
KEY_ID = "k1"
SECRET = b"trusted-secret-key-material-0001"
TEST_SECRET = b"test-signer-secret-DO-NOT-SHIP-00"

NOW = 1_000_000


def _registry(include_test_signer=True):
    keys = {ISSUER: {KEY_ID: SECRET}}
    if include_test_signer:
        keys[TEST_ONLY_ISSUER] = {"tk": TEST_SECRET}
    return KeyRegistry(keys)


def _cap(**over):
    base = {
        "capability_id": "crm.contact.update.commit",
        "capability_version": "1.0.0",
        "operation_id": "crm.contact.update.commit",
        "request_schema": {"contact_id": "str", "field": "str", "value": "str"},
        "response_schema": {"record_id": "str", "revision": "str",
                            "domain": "str", "provenance": "str"},
        "tenant_scope": "tenant-a",
        "workspace_scope": "ws-acme",
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


def _signed(signer_issuer=ISSUER, signer_key=KEY_ID, caps=None,
            issued_at=NOW - 10, expires_at=NOW + 10_000):
    reg = _registry()
    signer = ManifestSigner(reg, signer_issuer, signer_key)
    return signer.sign(caps or [_cap()], issued_at=issued_at, expires_at=expires_at)


def _verifier(production=True, allowlist=None):
    return ManifestVerifier(
        _registry(), allowlist or _allowlist(),
        production=production, clock=lambda: NOW,
    )


# --------------------------------------------------------------------------- #
# Happy path                                                                   #
# --------------------------------------------------------------------------- #
def test_valid_signed_manifest_verifies():
    m = _verifier().verify(_signed())
    cap = m.get("crm.contact.update.commit")
    assert cap.operation_id == "crm.contact.update.commit"
    assert cap.provenance.value == "REFERENCE"
    assert cap.approval_required is True
    assert cap.idempotency_semantics.value == "exactly_once"


# --------------------------------------------------------------------------- #
# Signature / issuer / key adversarial                                         #
# --------------------------------------------------------------------------- #
def test_unsigned_manifest_rejected():
    signed = _signed()
    del signed["signature"]
    with pytest.raises(ManifestError):
        _verifier().verify(signed)


def test_unknown_issuer_rejected():
    signed = _signed()
    signed["issuer"] = "attacker"
    with pytest.raises(ManifestError):
        _verifier().verify(signed)


def test_unknown_key_id_rejected():
    signed = _signed()
    signed["key_id"] = "kX"
    with pytest.raises(ManifestError):
        _verifier().verify(signed)


def test_tampered_capability_field_rejected():
    signed = _signed()
    signed["capabilities"][0]["risk_class"] = "low"  # was high
    with pytest.raises(ManifestError):
        _verifier().verify(signed)


def test_tampered_schema_rejected():
    signed = _signed()
    signed["capabilities"][0]["request_schema"]["secret"] = "str"
    with pytest.raises(ManifestError):
        _verifier().verify(signed)


def test_tampered_schema_digest_rejected():
    signed = _signed()
    signed["capabilities"][0]["request_schema_digest"] = "deadbeef"
    with pytest.raises(ManifestError):
        _verifier().verify(signed)


def test_expired_manifest_rejected():
    with pytest.raises(ManifestError):
        _verifier().verify(_signed(issued_at=NOW - 100, expires_at=NOW - 1))


def test_not_yet_valid_manifest_rejected():
    with pytest.raises(ManifestError):
        _verifier().verify(_signed(issued_at=NOW + 50, expires_at=NOW + 100))


def test_unallowlisted_capability_rejected():
    signed = _signed(caps=[_cap(capability_version="9.9.9")])
    with pytest.raises(ManifestError):
        _verifier().verify(signed)


def test_unallowlisted_provider_rejected():
    al = AllowlistPolicy(
        capabilities={("crm.contact.update.commit", "1.0.0")}, providers=set()
    )
    with pytest.raises(ManifestError):
        _verifier(allowlist=al).verify(_signed())


def test_signature_copied_to_another_capability_rejected():
    """A valid signature from cap A pasted onto cap B (different id/version)."""
    reg = _registry()
    signer = ManifestSigner(reg, ISSUER, KEY_ID)
    good = signer.sign([_cap()], issued_at=NOW - 5, expires_at=NOW + 100)
    stolen_sig = good["capabilities"][0]["signature"]
    forged = copy.deepcopy(good)
    forged["capabilities"][0]["capability_id"] = "crm.contact.read"
    forged["capabilities"][0]["operation_id"] = "crm.contact.read"
    forged["capabilities"][0]["signature"] = stolen_sig
    with pytest.raises(ManifestError):
        _verifier().verify(forged)


def test_added_capability_breaks_envelope_digest():
    signed = _signed()
    extra = copy.deepcopy(signed["capabilities"][0])
    extra["capability_id"] = "crm.contact.read"
    signed["capabilities"].append(extra)
    with pytest.raises(ManifestError):
        _verifier().verify(signed)


def test_test_only_issuer_refused_in_production():
    reg = _registry()
    signer = ManifestSigner(reg, TEST_ONLY_ISSUER, "tk")
    signed = signer.sign([_cap()], issued_at=NOW - 5, expires_at=NOW + 100)
    with pytest.raises(ManifestError):
        _verifier(production=True).verify(signed)


def test_test_only_issuer_allowed_when_not_production():
    reg = _registry()
    signer = ManifestSigner(reg, TEST_ONLY_ISSUER, "tk")
    signed = signer.sign([_cap()], issued_at=NOW - 5, expires_at=NOW + 100)
    al = AllowlistPolicy(
        capabilities={("crm.contact.update.commit", "1.0.0")},
        providers={"reference"},
    )
    v = ManifestVerifier(reg, al, production=False, clock=lambda: NOW)
    m = v.verify(signed)
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
    print(f"\nenterprise_manifest standalone: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
