"""WAVE-30D — Track A engine-binding + attestation gate (harness side).

Deterministic, offline (marked ``benchmark`` so it runs in the benchmark gate):

* ``assert_engine_attestation`` accepts a correct Track A binding and FAILS CLOSED
  on every wrong engine/provider/execution/endpoint-class/cost-policy/model, and
  on a mismatched/unverified model-manifest digest;
* the HTTP seam refuses to dispatch a local_runtime run with no bound engine;
* the CLI refuses ``--mode local_runtime`` without an engine-bound track;
* the comparability contract now pins ``engine_profile=eco.v01`` and the track
  provenance carries it.
"""
from __future__ import annotations

import types

import pytest

from tests.benchmark.harness.preflight import (
    PreflightError,
    assert_engine_attestation,
)
from tests.benchmark.harness.tracks import (
    load_track,
    track_provenance,
    validate_comparability,
)

pytestmark = pytest.mark.benchmark

_ECO_TAG = "youtab-qwen35-9b-agent-64k:latest"
_DIGEST = "b7b9afeaf023a549a9e6fc7960694f3c32fc490d415bbdf1751e2f390cf4ae48"


def _att(**over):
    base = {
        "engine_profile": "eco.v01",
        "engine_bound": True,
        "provider": "ollama",
        "model": _ECO_TAG,
        "model_ref": f"ollama/{_ECO_TAG}",
        "model_identifier_status": "resolved",
        "execution": "local",
        "endpoint_class": "loopback",
        "endpoint_authorized": True,
        "provider_cost_policy": "local_zero_verified",
        "ollama_model_digest": _DIGEST,
        "ollama_digest_status": "verified_present",
    }
    base.update(over)
    return base


def _posture(att=None, *, error=None):
    return {"engine_attestation": att, "engine_attestation_error": error}


@pytest.fixture(scope="module")
def track_a():
    return load_track("A")


def test_attestation_accepts_correct_binding(track_a):
    assert_engine_attestation(_posture(_att()), track_a)  # no raise


def test_attestation_accepts_matching_digest(track_a):
    assert_engine_attestation(_posture(_att()), track_a,
                              expected_model_digest=_DIGEST)


@pytest.mark.parametrize("over", [
    {"engine_profile": "amour.v03"},
    {"provider": "openai"},
    {"execution": "cloud"},
    {"endpoint_authorized": False},
    {"endpoint_class": "public"},
    {"endpoint_class": "hostname"},
    {"provider_cost_policy": "campaign_budget_eur"},
    {"provider_cost_policy": "unpriced"},
    {"model": None, "model_identifier_status": "OWNER_MODEL_IDENTIFIER_REQUIRED"},
    {"model_identifier_status": "OWNER_MODEL_IDENTIFIER_REQUIRED"},
])
def test_attestation_fails_closed_on_wrong_binding(track_a, over):
    with pytest.raises(PreflightError):
        assert_engine_attestation(_posture(_att(**over)), track_a)


def test_attestation_fails_when_missing_or_errored(track_a):
    with pytest.raises(PreflightError):
        assert_engine_attestation(_posture(None), track_a)
    with pytest.raises(PreflightError):
        assert_engine_attestation(_posture(None, error="unknown_or_unbound_engine"),
                                  track_a)


def test_attestation_digest_pin_fails_closed(track_a):
    # wrong digest
    with pytest.raises(PreflightError):
        assert_engine_attestation(_posture(_att()), track_a,
                                  expected_model_digest="a" * 64)
    # digest not verified by the runtime
    with pytest.raises(PreflightError):
        assert_engine_attestation(
            _posture(_att(ollama_model_digest=None, ollama_digest_status="probe_failed")),
            track_a, expected_model_digest=_DIGEST)
    # malformed expected digest (must be full 64-hex)
    with pytest.raises(PreflightError):
        assert_engine_attestation(_posture(_att()), track_a,
                                  expected_model_digest="deadbeef")


def test_seam_refuses_local_run_without_bound_engine(tmp_path):
    from tests.benchmark.harness.seam import HttpRuntimeSeam

    seam = HttpRuntimeSeam(
        "http://127.0.0.1:1", "x" * 50, tenant="t", user="u",
        engine=None, require_engine=True,
    )
    scenario = types.SimpleNamespace(engine=None, params={}, title="t",
                                     mode="deterministic")
    try:
        with pytest.raises(RuntimeError):
            seam.run(scenario, home=tmp_path, workspace=tmp_path,
                     tenant="t", user="u", run_id="r1")
    finally:
        seam.close()


def test_cli_refuses_local_runtime_without_engine_bound_track(tmp_path):
    from tests.benchmark.harness.cli import main

    out = tmp_path / "out"  # value irrelevant; guard fires before dir validation
    rc = main(["run", "--mode", "local_runtime", "--out", str(out)])
    assert rc == 6


def test_track_a_provenance_carries_engine_profile(track_a):
    prov = track_provenance(track_a)
    assert prov["engine_profile"] == "eco.v01"
    assert prov["provider_name"] == "ollama"
    assert prov["cost_model"] == "local_zero_api_cost"


def test_comparability_contract_pins_eco_engine():
    summary = validate_comparability()
    assert summary["track_a_provenance"]["engine_profile"] == "eco.v01"
