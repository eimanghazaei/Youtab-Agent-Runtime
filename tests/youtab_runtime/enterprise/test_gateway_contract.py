"""Machine-readable Gateway contract: schemas are parsed and enforced by code.

These tests prove the versioned JSON Schema artifacts under
``docs/lane2/contracts`` are actually loaded and used to validate real payloads
(including a genuinely Ed25519-signed manifest built with
``manifest_crypto.Ed25519ManifestSigner``), not merely present as documentation.
Standalone-runnable in the style of ``test_enterprise_manifest.py``.
"""

from __future__ import annotations

import copy

import pytest

from youtab_runtime.enterprise import gateway_contract as gc
from youtab_runtime.enterprise.manifest import TEST_ONLY_ISSUER
from youtab_runtime.enterprise.manifest_crypto import Ed25519ManifestSigner

ISSUER = "gateway-authority"
KEY_ID = "k1"
NOW = 1_000_000
TENANT = "tenant-a"
WORKSPACE = "ws-acme"
GATEWAY_SHA = "a" * 40


def _cap(**over):
    base = {
        "capability_id": "crm.contact.update.commit",
        "capability_version": "1.0.0",
        "operation_id": "crm.contact.update.commit",
        "request_schema": {"business_key": "str", "field": "str", "value": "str"},
        "response_schema": {
            "record_id": "str",
            "revision": "str",
            "provenance": "str",
            "result_digest": "str",
        },
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


def _real_signed_manifest(caps=None):
    """A genuinely Ed25519-signed envelope produced by the real signer."""
    signer = Ed25519ManifestSigner.generate(TEST_ONLY_ISSUER, KEY_ID)
    return signer.sign(
        caps or [_cap()],
        issued_at=NOW - 10,
        expires_at=NOW + 10_000,
        tenant_scope=TENANT,
        workspace_scope=WORKSPACE,
    )


def _discovery():
    return {"gateway_sha": GATEWAY_SHA, "manifest": _real_signed_manifest()}


def _operation_envelope(**over):
    base = {
        "operation_id": "crm.contact.update.commit",
        "workspace": WORKSPACE,
        "business_key": "acct-42",
        "payload": {"business_key": "acct-42", "field": "name", "value": "X"},
        "request_digest": "b" * 64,
        "provenance": "REFERENCE",
        "result": {"record_id": "r", "revision": "v", "provenance": "REFERENCE"},
    }
    base.update(over)
    return base


def _reconciliation(**over):
    base = {
        "checked_operation_id": "crm.contact.update.commit",
        "business_key": "acct-42",
        "original_request_digest": "c" * 64,
        "expected_digest": "d" * 64,
        "provenance": "REFERENCE",
    }
    base.update(over)
    return base


def _health(**over):
    base = {"status": "ok", "gateway_sha": GATEWAY_SHA, "capabilities_available": True}
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Artifacts are loadable                                                       #
# --------------------------------------------------------------------------- #
def test_every_schema_loads_and_has_id():
    for name in gc.SCHEMA_NAMES:
        schema = gc.load_schema(name)
        assert isinstance(schema, dict)
        assert isinstance(schema.get("$id"), str) and schema["$id"]


def test_unknown_schema_name_fails_closed():
    with pytest.raises(gc.GatewayContractError):
        gc.load_schema("does-not-exist")
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("does-not-exist", {})


# --------------------------------------------------------------------------- #
# Real signed manifest validates                                              #
# --------------------------------------------------------------------------- #
def test_real_ed25519_manifest_validates():
    gc.validate_against("gateway_manifest", _real_signed_manifest())


def test_discovery_response_validates():
    gc.validate_against("capability_discovery", _discovery())


def test_manifest_missing_required_field_rejected():
    m = _real_signed_manifest()
    del m["signature"]
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("gateway_manifest", m)


def test_manifest_bad_provenance_enum_rejected():
    m = _real_signed_manifest()
    m["capabilities"][0]["provenance"] = "MADE_UP"
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("gateway_manifest", m)


def test_manifest_bad_algorithm_rejected():
    m = _real_signed_manifest()
    m["algorithm"] = "hmac-sha256"
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("gateway_manifest", m)


def test_manifest_bad_risk_class_rejected():
    m = _real_signed_manifest()
    m["capabilities"][0]["risk_class"] = "catastrophic"
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("gateway_manifest", m)


def test_manifest_bad_idempotency_semantics_rejected():
    m = _real_signed_manifest()
    m["capabilities"][0]["idempotency"]["semantics"] = "sometimes"
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("gateway_manifest", m)


def test_manifest_bad_digest_pattern_rejected():
    m = _real_signed_manifest()
    m["manifest_digest"] = "not-a-hex-digest"
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("gateway_manifest", m)


def test_manifest_empty_capabilities_rejected():
    m = _real_signed_manifest()
    m["capabilities"] = []
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("gateway_manifest", m)


def test_manifest_unknown_extra_property_rejected():
    m = _real_signed_manifest()
    m["surprise"] = True
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("gateway_manifest", m)


# --------------------------------------------------------------------------- #
# Operation envelope                                                          #
# --------------------------------------------------------------------------- #
def test_operation_envelope_validates():
    gc.validate_against("operation_envelope", _operation_envelope())


def test_operation_envelope_without_optional_result_validates():
    env = _operation_envelope()
    del env["result"]
    gc.validate_against("operation_envelope", env)


def test_operation_envelope_bad_request_digest_rejected():
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against(
            "operation_envelope", _operation_envelope(request_digest="short")
        )


def test_operation_envelope_bad_provenance_rejected():
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against(
            "operation_envelope", _operation_envelope(provenance="LOCAL")
        )


def test_operation_envelope_missing_field_rejected():
    env = _operation_envelope()
    del env["operation_id"]
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("operation_envelope", env)


# --------------------------------------------------------------------------- #
# Reconciliation evidence                                                     #
# --------------------------------------------------------------------------- #
def test_reconciliation_evidence_validates():
    gc.validate_against("reconciliation_evidence", _reconciliation())


def test_reconciliation_bad_expected_digest_rejected():
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against(
            "reconciliation_evidence", _reconciliation(expected_digest="nope")
        )


def test_reconciliation_missing_field_rejected():
    rec = _reconciliation()
    del rec["checked_operation_id"]
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("reconciliation_evidence", rec)


# --------------------------------------------------------------------------- #
# Health                                                                      #
# --------------------------------------------------------------------------- #
def test_health_validates():
    gc.validate_against("health", _health())


def test_health_bad_gateway_sha_rejected():
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("health", _health(gateway_sha="deadbeef"))


def test_health_bad_status_enum_rejected():
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("health", _health(status="fine"))


def test_health_non_bool_available_rejected():
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("health", _health(capabilities_available="yes"))


def test_discovery_bad_gateway_sha_rejected():
    d = _discovery()
    d["gateway_sha"] = "xyz"
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("capability_discovery", d)


def test_discovery_propagates_manifest_violation():
    d = _discovery()
    d["manifest"]["capabilities"][0]["provenance"] = "BOGUS"
    with pytest.raises(gc.GatewayContractError):
        gc.validate_against("capability_discovery", d)


def test_deep_copy_of_valid_manifest_still_valid():
    # Guards against accidental in-place mutation across cases.
    gc.validate_against("gateway_manifest", copy.deepcopy(_real_signed_manifest()))


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
    print(
        f"\ngateway_contract standalone: {passed} passed, {failed} failed "
        f"(jsonschema={gc.USING_JSONSCHEMA})"
    )
    raise SystemExit(1 if failed else 0)
