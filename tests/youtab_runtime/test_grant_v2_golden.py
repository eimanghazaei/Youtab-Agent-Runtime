"""Canonical v2 execution-grant (Ed25519) conformance — shared golden oracle.

Proves the engine reproduces the exact grant canonicalization/signature in
``docs/architecture/CANONICAL-SIGNATURE-GRANT.md`` §4.3. The committed fixture
``golden_grant_v2.json`` is the cross-repo conformance oracle: any drift in the
v2 field set, ordering, separators or digest rule changes the sha256 and fails
here. Values are TEST ONLY (Ed25519 seed = 32 bytes 0x01).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from youtab_runtime.contracts import BrainCommandEnvelopeV2

_FIXTURE = Path(__file__).with_name("golden_grant_v2.json")
_AT = datetime(2026, 9, 7, 12, 10, 0, tzinfo=UTC)  # inside [issued, expires) and < deadline


def _load() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_fixture_canonical_sha256_matches_spec() -> None:
    data = _load()
    grant = BrainCommandEnvelopeV2.model_validate(data["grant"])
    digest = hashlib.sha256(grant.canonical_payload()).hexdigest()
    assert digest == data["canonical_sha256"]
    # The spec literal (§4.3) is pinned here so a doc/code divergence is caught.
    assert digest == "7e1ce8c8d98969a171f496cc8f95a0c15843510fb4b1049809b005b75606dea9"


def test_golden_grant_verifies_with_public_key() -> None:
    data = _load()
    grant = BrainCommandEnvelopeV2.model_validate(data["grant"])
    grant.verify(data["ed25519_public_key_b64"], now=_AT)  # must not raise


def test_public_key_is_the_test_seed_key() -> None:
    data = _load()
    assert data["ed25519_public_key_b64"] == "iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w="


def test_tampered_objective_fails_signature() -> None:
    data = _load()
    tampered = copy.deepcopy(data["grant"])
    tampered["objective"] = "Do something else entirely."
    grant = BrainCommandEnvelopeV2.model_validate(tampered)
    with pytest.raises(ValueError, match="invalid command signature"):
        grant.verify(data["ed25519_public_key_b64"], now=_AT)


def test_tampered_budget_fails_signature() -> None:
    data = _load()
    tampered = copy.deepcopy(data["grant"])
    tampered["reasoning"]["max_cost_micros"] = 999_999_999
    grant = BrainCommandEnvelopeV2.model_validate(tampered)
    with pytest.raises(ValueError, match="invalid command signature"):
        grant.verify(data["ed25519_public_key_b64"], now=_AT)


def test_wrong_public_key_fails_signature() -> None:
    data = _load()
    grant = BrainCommandEnvelopeV2.model_validate(data["grant"])
    other = base64.b64encode(bytes([2]) * 32).decode()  # not the signer's key
    with pytest.raises(ValueError):
        grant.verify(other, now=_AT)


def test_expired_grant_rejected_before_signature_check() -> None:
    data = _load()
    grant = BrainCommandEnvelopeV2.model_validate(data["grant"])
    after_expiry = datetime(2026, 9, 7, 13, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="expired"):
        grant.verify(data["ed25519_public_key_b64"], now=after_expiry)


def test_extra_field_is_forbidden() -> None:
    data = _load()
    bad = copy.deepcopy(data["grant"])
    bad["surprise"] = "nope"
    with pytest.raises(ValueError):
        BrainCommandEnvelopeV2.model_validate(bad)


def test_missing_v2_field_is_rejected() -> None:
    data = _load()
    bad = copy.deepcopy(data["grant"])
    del bad["workspace_id"]  # a v2-mandated field
    with pytest.raises(ValueError):
        BrainCommandEnvelopeV2.model_validate(bad)


def test_v1_schema_version_is_not_accepted_by_v2_model() -> None:
    data = _load()
    bad = copy.deepcopy(data["grant"])
    bad["schema_version"] = "youtab.agent-command.v1"
    with pytest.raises(ValueError):
        BrainCommandEnvelopeV2.model_validate(bad)
