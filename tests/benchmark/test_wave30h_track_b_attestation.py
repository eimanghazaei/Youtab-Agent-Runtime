"""WAVE-30H — Track B cloud attestation + effective-identity provenance (harness).

Deterministic, offline (marked ``benchmark`` so it runs in the benchmark gate; no
network, no provider, no secrets). Proves the net-new Track-B cloud path:

* ``assert_cloud_attestation`` ACCEPTS the Owner-selected identity and FAILS CLOSED
  on provider substitution, model substitution, a missing/blank/placeholder
  effective identity, an excluded (first-party) provider, a non-file credential
  source, and an unarmed budget;
* the CLI requires ``--track B`` + ``--expected-provider``/``--expected-model`` for a
  live ``real_provider`` run (fail-fast, before any evidence dir is minted);
* a run whose worker DISPATCHED a provider/model different from the attested one is
  flagged as an identity divergence (a fail-closed condition the CLI turns into a
  non-zero exit), even if every verdict passed;
* the effective identity is persisted into ``summary.json`` + ``provenance.json`` and
  frozen into ``MANIFEST.sha256`` (post-finalize tampering is detectable);
* Track A's engine attestation is unchanged.

Out of default collection (``benchmark`` marker); run with ``-m benchmark``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.benchmark.harness.preflight import (
    PreflightError,
    assert_cloud_attestation,
    assert_engine_attestation,
)
from tests.benchmark.harness.recorder import (
    PROVENANCE_FILE,
    Recorder,
    verify_manifest,
)
from tests.benchmark.harness.runner import Runner
from tests.benchmark.harness.schema import (
    MODE_REAL_PROVIDER,
    Scenario,
    Verdict,
)
from tests.benchmark.harness.tracks import load_track

pytestmark = pytest.mark.benchmark

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Owner-selected identity — neutral, non-secret placeholders (never a credential).
_EXP_PROVIDER = "provider-x"
_EXP_MODEL = "provider-x-model-v1"

_EFFECTIVE_KEYS = {
    "effective_provider",
    "effective_model",
    "effective_endpoint_class",
    "effective_cost_policy",
    "effective_credential_source",
    "effective_binding_version",
    "effective_model_ref",
}

# Overrides routed into the nested effective_binding (vs top-level posture).
_BINDING_OVERRIDES = {
    "provider", "model", "model_ref", "model_identifier_status",
    "execution", "endpoint_class", "provider_cost_policy",
    "binding_version", "digest_status",
}


def _posture(**over):
    """A runtime preflight posture whose canonical ``effective_binding`` attests the
    Owner-selected cloud identity. Overrides for binding fields are routed into the
    nested binding; others (credential source, budget) stay top-level."""
    binding = {
        "binding_version": 1,
        "provider": _EXP_PROVIDER,
        "model": _EXP_MODEL,
        "model_ref": f"{_EXP_PROVIDER}/{_EXP_MODEL}",
        "model_identifier_status": "resolved",
        "execution": "cloud",
        "endpoint_class": "cloud",
        "provider_cost_policy": "campaign_budget_eur",
        "digest_status": "not_applicable",
        "model_digest": None,
        "bound_at": "2026-09-11T00:00:00Z",
    }
    p = {
        "effective_binding": binding,
        "provider_credential_source": "file",
        "budget_enforcement_enabled": True,
    }
    for key, value in over.items():
        if key in _BINDING_OVERRIDES:
            binding[key] = value
        else:
            p[key] = value
    return p


@pytest.fixture(scope="module")
def track_b():
    return load_track("B")


# --- positive: accepts the Owner-selected identity, returns non-secret fields ---

def test_accepts_correct_identity(track_b):
    eff = assert_cloud_attestation(
        _posture(), track_b,
        expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
    )
    assert eff["effective_provider"] == _EXP_PROVIDER
    assert eff["effective_model"] == _EXP_MODEL
    assert eff["effective_endpoint_class"] == "cloud"
    assert eff["effective_cost_policy"] == "campaign_budget_eur"
    assert eff["effective_credential_source"] == "file"
    # The binding spine the recorder reconciles against is carried through.
    assert eff["effective_binding_version"] == 1
    assert eff["effective_model_ref"] == f"{_EXP_PROVIDER}/{_EXP_MODEL}"
    # ONLY non-secret identity fields are returned — never a key/credential value.
    assert set(eff) == _EFFECTIVE_KEYS


def test_provider_match_is_case_insensitive(track_b):
    assert_cloud_attestation(
        _posture(provider=_EXP_PROVIDER.upper()), track_b,
        expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
    )  # no raise


def test_endpoint_class_pin_accepts_cloud(track_b):
    assert_cloud_attestation(
        _posture(), track_b,
        expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
        expected_endpoint_class="cloud",
    )  # no raise


# --- axis 1: provider substitution ------------------------------------------

def test_provider_substitution_refused(track_b):
    with pytest.raises(PreflightError, match="provider mismatch"):
        assert_cloud_attestation(
            _posture(provider="other-cloud"), track_b,
            expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
        )


def test_excluded_first_party_provider_refused(track_b):
    # The comparability contract forbids the first-party provider; even if the Owner
    # mistakenly expects it and the runtime reports it, the track exclusion refuses.
    with pytest.raises(PreflightError, match="excluded"):
        assert_cloud_attestation(
            _posture(provider="anthropic"), track_b,
            expected_provider="anthropic", expected_model=_EXP_MODEL,
        )


# --- axis 2: model substitution ---------------------------------------------

def test_model_substitution_refused(track_b):
    with pytest.raises(PreflightError, match="model mismatch"):
        assert_cloud_attestation(
            _posture(model="provider-x-model-v2"), track_b,
            expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
        )


# --- axis 3: missing / blank / placeholder identity + posture invariants -----

@pytest.mark.parametrize("over", [
    {"provider": None},
    {"provider": ""},
    {"provider": "OWNER_SELECTION_REQUIRED"},
    {"model": None},
    {"model": ""},
    {"model": "OWNER_SELECTION_REQUIRED"},
])
def test_missing_or_placeholder_identity_refused(track_b, over):
    with pytest.raises(PreflightError):
        assert_cloud_attestation(
            _posture(**over), track_b,
            expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
        )


def test_non_file_credential_refused(track_b):
    with pytest.raises(PreflightError, match="file-based provider credential"):
        assert_cloud_attestation(
            _posture(provider_credential_source="env"), track_b,
            expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
        )


def test_unarmed_budget_refused(track_b):
    with pytest.raises(PreflightError, match="budget enforcement"):
        assert_cloud_attestation(
            _posture(budget_enforcement_enabled=False), track_b,
            expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
        )


def test_missing_effective_binding_refused(track_b):
    # One contract: without a runtime binding, --expected-* can only be checked
    # against nothing — fail closed, never fall back to loose config names.
    posture = _posture()
    posture.pop("effective_binding")
    with pytest.raises(PreflightError, match="no effective binding"):
        assert_cloud_attestation(
            posture, track_b,
            expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
        )


def test_non_metered_cost_policy_on_cloud_refused(track_b):
    # A billed cloud run can never attest a local/free cost policy.
    with pytest.raises(PreflightError, match="metered"):
        assert_cloud_attestation(
            _posture(provider_cost_policy="local_zero_verified"), track_b,
            expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
        )


def test_requires_owner_expected_identity(track_b):
    with pytest.raises(PreflightError):
        assert_cloud_attestation(
            _posture(), track_b, expected_provider="", expected_model=_EXP_MODEL)
    with pytest.raises(PreflightError):
        assert_cloud_attestation(
            _posture(), track_b, expected_provider=_EXP_PROVIDER, expected_model="")


def test_endpoint_class_mismatch_refused(track_b):
    with pytest.raises(PreflightError, match="endpoint class"):
        assert_cloud_attestation(
            _posture(), track_b,
            expected_provider=_EXP_PROVIDER, expected_model=_EXP_MODEL,
            expected_endpoint_class="loopback",  # a cloud run is never loopback
        )


# --- CLI early guards (fail-fast, no network, no evidence dir minted) --------

def test_cli_real_provider_requires_track_b(tmp_path):
    from tests.benchmark.harness.cli import main
    rc = main(["run", "--mode", "real_provider", "--out", str(tmp_path / "o")])
    assert rc == 6


def test_cli_real_provider_requires_expected_identity(tmp_path):
    from tests.benchmark.harness.cli import main
    rc = main(["run", "--mode", "real_provider", "--track", "B",
               "--out", str(tmp_path / "o")])
    assert rc == 6


# --- effective-identity persistence + fail-closed divergence (recorder) ------

def _runner(out: Path) -> Runner:
    return Runner(Recorder(out), mode=MODE_REAL_PROVIDER, repo_root=_REPO_ROOT)


def _scn(sid: str = "s1") -> Scenario:
    return Scenario(
        id=sid, family="duplicate_run", mode=MODE_REAL_PROVIDER,
        title="t", oracle="run_completed_ok", executor="noop",
    )


# The attested identity threaded onto every record by the CLI attestation step.
_ATTESTED = {
    "effective_provider": _EXP_PROVIDER,
    "effective_model": _EXP_MODEL,
    "effective_endpoint_class": "cloud",
    "effective_cost_policy": "campaign_budget_eur",
    "effective_credential_source": "file",
}


def _emit(runner: Runner, *, provenance, sid: str = "s1", run_id: str = "r1"):
    return runner._emit_record(
        _scn(sid), 0, run_id, {"tenant": "t", "user": "u"},
        Verdict.unknown("state-only", source=["state"]),
        observation=None, wall_ms=1.0, provenance=dict(provenance),
    )


def test_effective_identity_persisted_into_all_evidence(tmp_path):
    out = tmp_path / "out"
    runner = _runner(out)
    _emit(runner, provenance={**_ATTESTED,
                              "bound_provider": _EXP_PROVIDER,
                              "bound_model": _EXP_MODEL,
                              "dispatched_provider": _EXP_PROVIDER,
                              "dispatched_model": _EXP_MODEL})
    summary = runner.recorder.finalize()

    assert summary["effective_identity"]["effective_provider"] == _EXP_PROVIDER
    assert summary["effective_identity"]["effective_model"] == _EXP_MODEL
    assert summary["identity_divergences"] == []
    assert summary["identity_divergence_count"] == 0

    prov = json.loads((out / PROVENANCE_FILE).read_text(encoding="utf-8"))
    assert prov["effective_identity"]["effective_provider"] == _EXP_PROVIDER
    assert prov["effective_identity"]["effective_model"] == _EXP_MODEL

    results = (out / "results.jsonl").read_text(encoding="utf-8")
    assert _EXP_PROVIDER in results and _EXP_MODEL in results

    # Fresh evidence verifies clean — identity is frozen into the manifest.
    assert verify_manifest(out) == {"ok": True, "mismatches": [], "missing": []}


def test_retry_and_repetition_preserve_identical_identity(tmp_path):
    out = tmp_path / "out"
    runner = _runner(out)
    for i in range(2):  # a retry/repetition landing a second record on the run
        _emit(runner, sid=f"s{i}", run_id=f"r{i}",
              provenance={**_ATTESTED,
                          "bound_provider": _EXP_PROVIDER,
                          "bound_model": _EXP_MODEL,
                          "dispatched_provider": _EXP_PROVIDER,
                          "dispatched_model": _EXP_MODEL})
    summary = runner.recorder.finalize()
    # Both records carry the same attested+dispatched identity: no divergence.
    assert summary["identity_divergences"] == []
    assert summary["effective_identity"]["effective_provider"] == _EXP_PROVIDER
    assert summary["effective_identity"]["dispatched_provider"] == _EXP_PROVIDER


def test_dispatched_provider_divergence_is_flagged(tmp_path):
    # axis 4: a run bound correctly (attested == bound) but that actually DISPATCHED on
    # a different provider is caught and fails closed. The drift flags against BOTH the
    # attested and the bound side (attested_vs_dispatched + bound_vs_dispatched).
    out = tmp_path / "out"
    runner = _runner(out)
    _emit(runner, provenance={**_ATTESTED,
                              "bound_provider": _EXP_PROVIDER,
                              "bound_model": _EXP_MODEL,
                              "dispatched_provider": "other-cloud",
                              "dispatched_model": _EXP_MODEL})
    summary = runner.recorder.finalize()
    kinds = {d["kind"] for d in summary["identity_divergences"]}
    assert {"attested_vs_dispatched", "bound_vs_dispatched"} <= kinds
    d = next(x for x in summary["identity_divergences"]
             if x["kind"] == "attested_vs_dispatched")
    assert d["attested"]["provider"] == _EXP_PROVIDER
    assert d["dispatched"]["provider"] == "other-cloud"


def test_dispatched_model_divergence_is_flagged(tmp_path):
    out = tmp_path / "out"
    runner = _runner(out)
    _emit(runner, provenance={**_ATTESTED,
                              "bound_provider": _EXP_PROVIDER,
                              "bound_model": _EXP_MODEL,
                              "dispatched_provider": _EXP_PROVIDER,
                              "dispatched_model": "provider-x-model-v2"})
    summary = runner.recorder.finalize()
    kinds = {d["kind"] for d in summary["identity_divergences"]}
    assert {"attested_vs_dispatched", "bound_vs_dispatched"} <= kinds
    d = next(x for x in summary["identity_divergences"]
             if x["kind"] == "attested_vs_dispatched")
    assert d["dispatched"]["model"] == "provider-x-model-v2"


def test_attested_vs_bound_divergence_is_flagged(tmp_path):
    # The runtime BOUND a different provider than the preflight ATTESTED.
    out = tmp_path / "out"
    runner = _runner(out)
    _emit(runner, provenance={**_ATTESTED,
                              "bound_provider": "other-cloud",
                              "bound_model": _EXP_MODEL,
                              "dispatched_provider": "other-cloud",
                              "dispatched_model": _EXP_MODEL})
    summary = runner.recorder.finalize()
    kinds = {d["kind"] for d in summary["identity_divergences"]}
    assert "attested_vs_bound" in kinds
    assert summary["identity_divergence_count"] >= 1


def test_bound_vs_dispatched_divergence_is_flagged(tmp_path):
    # The worker DISPATCHED a different model than the run was BOUND to.
    out = tmp_path / "out"
    runner = _runner(out)
    _emit(runner, provenance={**_ATTESTED,
                              "bound_provider": _EXP_PROVIDER,
                              "bound_model": _EXP_MODEL,
                              "dispatched_provider": _EXP_PROVIDER,
                              "dispatched_model": "provider-x-model-v2"})
    summary = runner.recorder.finalize()
    kinds = {d["kind"] for d in summary["identity_divergences"]}
    assert "bound_vs_dispatched" in kinds


def test_three_way_agreement_is_clean(tmp_path):
    # attested == bound == dispatched → no divergence certifies cleanly.
    out = tmp_path / "out"
    runner = _runner(out)
    _emit(runner, provenance={**_ATTESTED,
                              "bound_provider": _EXP_PROVIDER,
                              "bound_model": _EXP_MODEL,
                              "dispatched_provider": _EXP_PROVIDER,
                              "dispatched_model": _EXP_MODEL})
    summary = runner.recorder.finalize()
    assert summary["identity_divergences"] == []
    assert summary["effective_identity"]["bound_provider"] == _EXP_PROVIDER


def test_cli_fails_closed_on_identity_divergence(tmp_path):
    # The CLI turns a real finalized-summary divergence into a non-zero exit (6),
    # even when every verdict is fine (evidence is kept; the run never certifies).
    from tests.benchmark.harness.cli import identity_gate_exit_code

    out = tmp_path / "out"
    runner = _runner(out)
    _emit(runner, provenance={**_ATTESTED,
                              "dispatched_provider": "other-cloud",
                              "dispatched_model": _EXP_MODEL})
    summary = runner.recorder.finalize()
    # The exact gate the CLI runs over the finalized summary.
    assert identity_gate_exit_code(summary) == 6


def test_identity_gate_passes_clean_and_empty_summaries():
    from tests.benchmark.harness.cli import identity_gate_exit_code
    assert identity_gate_exit_code({"identity_divergences": []}) == 0
    assert identity_gate_exit_code({}) == 0  # no key (Track A / deterministic)


# --- axis 5: evidence tampering on the persisted effective identity ----------

def _finalized_track_b(out: Path) -> None:
    runner = _runner(out)
    _emit(runner, provenance={**_ATTESTED,
                              "dispatched_provider": _EXP_PROVIDER,
                              "dispatched_model": _EXP_MODEL})
    runner.recorder.finalize()


def test_tampered_summary_identity_detected(tmp_path):
    out = tmp_path / "out"
    _finalized_track_b(out)
    (out / "summary.json").write_text("{}", encoding="utf-8")  # tamper
    res = verify_manifest(out)
    assert res["ok"] is False
    assert "summary.json" in res["mismatches"]


def test_tampered_provenance_identity_detected(tmp_path):
    out = tmp_path / "out"
    _finalized_track_b(out)
    prov = json.loads((out / PROVENANCE_FILE).read_text(encoding="utf-8"))
    prov["effective_identity"]["effective_provider"] = "anthropic"  # tamper
    (out / PROVENANCE_FILE).write_text(json.dumps(prov), encoding="utf-8")
    res = verify_manifest(out)
    assert res["ok"] is False
    assert PROVENANCE_FILE in res["mismatches"]


def test_tampered_results_identity_detected(tmp_path):
    out = tmp_path / "out"
    _finalized_track_b(out)
    (out / "results.jsonl").write_text(
        '{"tampered": "provider-y"}\n', encoding="utf-8")  # tamper
    res = verify_manifest(out)
    assert res["ok"] is False
    assert "results.jsonl" in res["mismatches"]


# --- Track A attestation is unchanged (regression fence) --------------------

def test_track_a_attestation_unchanged():
    track_a = load_track("A")
    eco_tag = "youtab-qwen35-9b-agent-64k:latest"
    att = {
        "engine_profile": "eco.v01", "engine_bound": True, "provider": "ollama",
        "model": eco_tag, "model_ref": f"ollama/{eco_tag}",
        "model_identifier_status": "resolved", "execution": "local",
        "endpoint_class": "loopback", "endpoint_authorized": True,
        "provider_cost_policy": "local_zero_verified",
        "ollama_model_digest": None, "ollama_digest_status": "not_applicable",
    }
    # The cloud attestation addition did not alter the Track A engine path.
    assert_engine_attestation(
        {"engine_attestation": att, "engine_attestation_error": None}, track_a
    )  # no raise
