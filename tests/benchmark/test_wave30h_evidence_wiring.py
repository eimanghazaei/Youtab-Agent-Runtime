"""WAVE-30H — benchmark evidence-integrity wiring (harness).

Deterministic, offline (marked ``benchmark`` so it runs in the benchmark gate; no
network, no provider, no secrets). Proves the WAVE-30H evidence-integrity fixes
END-TO-END through the REAL seam->runner->recorder path (not by injecting
provenance into ``_emit_record`` directly), plus the CLI/preflight/auth-client
hardening that guards the same evidence:

* WAVE-30H #5 — ``run_scenario`` forwards the runtime's canonical BOUND identity
  (``bound_provider``/``bound_model``/``bound_binding_version``) alongside the
  DISPATCHED identity through the observation into the persisted record + summary
  (pre-fix code dropped ``bound_*`` here, killing the recorder reconciliation);
* WAVE-30H #5b — a real (local_runtime/real_provider) record that carries an
  attested/bound identity but NO dispatched identity fails closed as a
  ``dispatched_missing`` identity divergence (-> gate exit 6). Mode-gated so
  deterministic records with no identity — or even a stray bound identity — stay
  clean;
* WAVE-30H #6 — the AUTHORITATIVE evidence SHA is the runtime's verified
  ``build_sha`` (threaded as ``runtime_build_sha``), NOT the harness checkout HEAD;
  a deterministic run with no build SHA keeps the local HEAD;
* the fail-closed divergence PRINTER handles EVERY divergence kind without
  KeyError (pre-fix it hard-coded ``d['attested']``/``d['dispatched']``);
* endpoint/scheme class comparisons in the cloud + engine attestation are
  case-insensitive;
* the ``AuthClient`` refuses a credential-bearing (userinfo) base URL.

Out of default collection (``benchmark`` marker); run with ``-m benchmark``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from tests.benchmark.harness import cli as _cli
from tests.benchmark.harness.auth_client import AuthClient, BenchmarkAuthError
from tests.benchmark.harness.preflight import (
    PreflightError,
    assert_cloud_attestation,
    assert_engine_attestation,
)
from tests.benchmark.harness.recorder import Recorder
from tests.benchmark.harness.runner import Runner
from tests.benchmark.harness.schema import (
    MODE_DETERMINISTIC,
    MODE_LOCAL_RUNTIME,
    MODE_REAL_PROVIDER,
    Observation,
    Scenario,
    platform_tag,
    runtime_head,
)
from tests.benchmark.harness.seam import HttpRuntimeSeam
from tests.benchmark.harness.tracks import load_track
from youtab_runtime.run_journal import Principal

pytestmark = pytest.mark.benchmark

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROVIDER = "provider-x"
_MODEL = "provider-x-model-v1"


# --------------------------------------------------------------------------- #
# A seam that returns a REAL Observation whose provenance is built by the true  #
# HttpRuntimeSeam._identity_provenance from a fabricated run-detail dict — so    #
# the bound_*/dispatched_* wiring is exercised through the genuine code path.    #
# --------------------------------------------------------------------------- #
class _FabricatedRuntimeSeam:
    """Drives a scenario the way HttpRuntimeSeam would, but from a canned run
    ``detail`` dict instead of a live runtime — the identity provenance is derived
    by the REAL ``HttpRuntimeSeam._identity_provenance`` so the shape and the
    runner's forwarding logic are proven, not faked."""

    name = HttpRuntimeSeam.name  # "runtime_http"

    def __init__(self, detail: Dict[str, Any]) -> None:
        self._detail = detail

    def run(self, scenario: Scenario, *, home: Path, workspace: Path,
            tenant: str, user: str, run_id: str) -> Observation:
        provenance = HttpRuntimeSeam._identity_provenance(self._detail)
        return Observation(
            run_id=run_id, tenant=tenant, user=user,
            mode=scenario.mode, seam=self.name, platform=platform_tag(),
            events=[], effects=[], artifacts=[],
            durable_status="done", workspace=workspace, pre_hash=None,
            self_reported_success=True, reported_usage=None,
            timings={}, engine_pinned=scenario.engine,
            provenance=provenance,
        )

    def close(self) -> None:  # pragma: no cover - parity with real seam
        pass


def _detail(*, bound: bool, dispatched: bool) -> Dict[str, Any]:
    """A fabricated ``/runs/{id}`` detail dict mirroring what a real runtime returns.

    ``bound`` -> a ``runtime_effective_binding`` (the canonical BOUND identity);
    ``dispatched`` -> the closing-run top-level provider/model (what the worker ran).
    """
    d: Dict[str, Any] = {}
    if bound:
        d["runtime_effective_binding"] = {
            "provider": _PROVIDER, "model": _MODEL, "binding_version": 1,
        }
    if dispatched:
        d["provider"] = _PROVIDER
        d["model"] = _MODEL
    return d


def _scenario(sid: str, mode: str) -> Scenario:
    return Scenario(
        id=sid, family="duplicate_run", mode=mode,
        title="t", oracle="run_completed_ok", executor="noop",
    )


def _drive(*, mode: str, detail: Dict[str, Any], out: Path,
           runtime_build_sha: Optional[str] = None):
    """Run one scenario through the REAL run_scenario + finalize path using the
    fabricated seam. Returns ``(row, summary)``."""
    recorder = Recorder(out)
    seam = _FabricatedRuntimeSeam(detail)
    runner = Runner(recorder, mode=mode, seam=seam, repo_root=_REPO_ROOT,
                    runtime_build_sha=runtime_build_sha)
    row = runner.run_scenario(_scenario("s1", mode), 0)
    summary = recorder.finalize()
    return row, summary


# --------------------------------------------------------------------------- #
# 1. bound_* carried end-to-end through seam -> runner -> recorder              #
# --------------------------------------------------------------------------- #
def test_bound_identity_carried_end_to_end(tmp_path):
    row, summary = _drive(
        mode=MODE_LOCAL_RUNTIME,
        detail=_detail(bound=True, dispatched=True),
        out=tmp_path / "out",
    )
    prov = row["provenance"]
    # WAVE-30H #5: bound_* survived run_scenario's identity projection into the record.
    assert prov["bound_provider"] == _PROVIDER
    assert prov["bound_model"] == _MODEL
    assert prov["bound_binding_version"] == 1
    # dispatched_* rode alongside.
    assert prov["dispatched_provider"] == _PROVIDER
    assert prov["dispatched_model"] == _MODEL
    # ...and the finalized summary's effective identity exposes the bound identity.
    assert summary["effective_identity"]["bound_provider"] == _PROVIDER
    assert summary["effective_identity"]["bound_model"] == _MODEL
    # Three-way agreement (bound == dispatched, no attested) certifies clean.
    assert summary["identity_divergences"] == []
    assert _cli.identity_gate_exit_code(summary) == 0


def test_real_provider_mode_also_carries_bound_identity(tmp_path):
    # The forwarding is mode-independent across the real-runtime tracks.
    row, summary = _drive(
        mode=MODE_REAL_PROVIDER,
        detail=_detail(bound=True, dispatched=True),
        out=tmp_path / "out",
    )
    assert row["provenance"]["bound_provider"] == _PROVIDER
    assert summary["effective_identity"]["bound_provider"] == _PROVIDER


# --------------------------------------------------------------------------- #
# 2. missing dispatched identity fails closed (mode-gated)                      #
# --------------------------------------------------------------------------- #
def test_missing_dispatched_identity_fails_closed(tmp_path):
    # bound identity present, but the worker exposed NO dispatched identity: a real
    # managed run must not certify green with no proof of what actually executed.
    _, summary = _drive(
        mode=MODE_LOCAL_RUNTIME,
        detail=_detail(bound=True, dispatched=False),
        out=tmp_path / "out",
    )
    kinds = {d["kind"] for d in summary["identity_divergences"]}
    assert "dispatched_missing" in kinds
    d = next(x for x in summary["identity_divergences"]
             if x["kind"] == "dispatched_missing")
    assert d["bound"]["provider"] == _PROVIDER
    assert d["dispatched"] == {"provider": None, "model": None}
    assert _cli.identity_gate_exit_code(summary) == 6


def test_deterministic_record_with_no_identity_stays_clean(tmp_path):
    # The specified deterministic-mode negative: no attested/bound identity at all.
    _, summary = _drive(
        mode=MODE_DETERMINISTIC,
        detail=_detail(bound=False, dispatched=False),
        out=tmp_path / "out",
    )
    assert summary["identity_divergences"] == []
    assert _cli.identity_gate_exit_code(summary) == 0


def test_mode_gate_excludes_deterministic_from_dispatched_missing(tmp_path):
    # Guards the mode gate itself: even if a deterministic record somehow carried a
    # bound identity with no dispatched identity, the dispatched_missing fail-closed
    # is scoped to the real-runtime tracks and must NOT fire here.
    _, summary = _drive(
        mode=MODE_DETERMINISTIC,
        detail=_detail(bound=True, dispatched=False),
        out=tmp_path / "out",
    )
    kinds = {d["kind"] for d in summary["identity_divergences"]}
    assert "dispatched_missing" not in kinds
    assert _cli.identity_gate_exit_code(summary) == 0


# --------------------------------------------------------------------------- #
# 3. evidence SHA is the runtime build SHA (WAVE-30H #6)                        #
# --------------------------------------------------------------------------- #
def test_evidence_sha_is_runtime_build_sha(tmp_path):
    build_sha = "a" * 40
    row, summary = _drive(
        mode=MODE_LOCAL_RUNTIME,
        detail=_detail(bound=True, dispatched=True),
        out=tmp_path / "out",
        runtime_build_sha=build_sha,
    )
    # Every record + the summary stamp the runtime's verified build SHA, not the
    # harness checkout HEAD.
    assert row["runtime_head"] == build_sha
    assert summary["runtime_head"] == build_sha
    assert build_sha != runtime_head(_REPO_ROOT)  # the local HEAD is different


def test_deterministic_run_without_build_sha_keeps_local_head(tmp_path):
    row, summary = _drive(
        mode=MODE_DETERMINISTIC,
        detail=_detail(bound=False, dispatched=False),
        out=tmp_path / "out",
        runtime_build_sha=None,
    )
    expected = runtime_head(_REPO_ROOT)
    assert row["runtime_head"] == expected
    assert summary["runtime_head"] == expected
    assert row["runtime_head"] != "a" * 40


# --------------------------------------------------------------------------- #
# 4. the fail-closed divergence printer handles EVERY kind (no KeyError)        #
# --------------------------------------------------------------------------- #
def _pair(kind: str, left: str, right: str) -> Dict[str, Any]:
    """A pair divergence in the exact shape recorder.finalize emits: it carries
    ONLY its two populated sides (named by the kind), never all three."""
    return {
        "scenario_id": f"scn-{kind}", "run_id": f"run-{kind}", "kind": kind,
        left: {"provider": "a-provider", "model": "a-model"},
        right: {"provider": "b-provider", "model": "b-model"},
    }


def _dispatched_missing() -> Dict[str, Any]:
    return {
        "scenario_id": "scn-missing", "run_id": "run-missing",
        "kind": "dispatched_missing",
        "attested": {"provider": _PROVIDER, "model": _MODEL},
        "bound": {"provider": _PROVIDER, "model": _MODEL},
        "dispatched": {"provider": None, "model": None},
    }


def test_printer_handles_every_divergence_kind(capsys):
    summary = {"identity_divergences": [
        _pair("attested_vs_bound", "attested", "bound"),
        _pair("bound_vs_dispatched", "bound", "dispatched"),
        _pair("attested_vs_dispatched", "attested", "dispatched"),
        _dispatched_missing(),
    ]}
    # Pre-fix this raised KeyError (d['attested']/d['dispatched'] hard-coded) for
    # bound_vs_dispatched and attested_vs_bound. It must now print every kind.
    _cli._print_identity_divergences(summary)  # no exception
    err = capsys.readouterr().err
    for kind in ("attested_vs_bound", "bound_vs_dispatched",
                 "attested_vs_dispatched", "dispatched_missing"):
        assert f"[{kind}]" in err


def test_printer_on_empty_summary_is_silent(capsys):
    _cli._print_identity_divergences({})  # no key, no exception
    _cli._print_identity_divergences({"identity_divergences": []})
    assert capsys.readouterr().err == ""


def test_printer_on_real_recorder_divergence(tmp_path, capsys):
    # A divergence produced by the REAL recorder (missing dispatched) prints cleanly.
    _, summary = _drive(
        mode=MODE_LOCAL_RUNTIME,
        detail=_detail(bound=True, dispatched=False),
        out=tmp_path / "out",
    )
    _cli._print_identity_divergences(summary)
    assert "[dispatched_missing]" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 5. scheme/endpoint-class comparisons are case-insensitive                     #
# --------------------------------------------------------------------------- #
def _cloud_posture(endpoint_class: str) -> Dict[str, Any]:
    return {
        "effective_binding": {
            "binding_version": 1,
            "provider": _PROVIDER,
            "model": _MODEL,
            "model_ref": f"{_PROVIDER}/{_MODEL}",
            "model_identifier_status": "resolved",
            "execution": "cloud",
            "endpoint_class": endpoint_class,
            "provider_cost_policy": "campaign_budget_eur",
            "digest_status": "not_applicable",
            "model_digest": None,
            "bound_at": "2026-09-11T00:00:00Z",
        },
        "provider_credential_source": "file",
        "budget_enforcement_enabled": True,
    }


def test_cloud_attestation_endpoint_class_case_insensitive():
    track_b = load_track("B")
    eff = assert_cloud_attestation(
        _cloud_posture("CLOUD"), track_b,
        expected_provider=_PROVIDER, expected_model=_MODEL,
        expected_endpoint_class="cloud",
    )  # must NOT raise despite the case mismatch
    assert eff["effective_endpoint_class"] == "cloud"


def _engine_attestation(endpoint_class: str) -> Dict[str, Any]:
    eco_tag = "youtab-qwen35-9b-agent-64k:latest"
    return {
        "engine_attestation": {
            "engine_profile": "eco.v01", "engine_bound": True, "provider": "ollama",
            "model": eco_tag, "model_ref": f"ollama/{eco_tag}",
            "model_identifier_status": "resolved", "execution": "local",
            "endpoint_class": endpoint_class, "endpoint_authorized": True,
            "provider_cost_policy": "local_zero_verified",
            "ollama_model_digest": None, "ollama_digest_status": "not_applicable",
        },
        "engine_attestation_error": None,
    }


def test_engine_attestation_endpoint_class_case_insensitive():
    track_a = load_track("A")
    # An upper-cased verified-local class must still pass the membership check.
    assert_engine_attestation(_engine_attestation("LOOPBACK"), track_a)  # no raise


def test_engine_attestation_non_local_class_still_refused():
    # The case-fold must not weaken the check: a genuinely non-local class refuses.
    track_a = load_track("A")
    with pytest.raises(PreflightError, match="endpoint class"):
        assert_engine_attestation(_engine_attestation("PUBLIC"), track_a)


# --------------------------------------------------------------------------- #
# 6. AuthClient refuses a credential-bearing base URL                           #
# --------------------------------------------------------------------------- #
_SECRET = "x" * 43  # meets the 43-char signing floor so the userinfo check is reached


def test_auth_client_refuses_userinfo_base_url():
    principal = Principal("bench-tenant", "bench-user")
    with pytest.raises(BenchmarkAuthError, match="credentials"):
        AuthClient("https://user:secret@host", _SECRET, principal)


def test_auth_client_accepts_clean_base_url():
    principal = Principal("bench-tenant", "bench-user")
    client = AuthClient("https://host", _SECRET, principal)
    try:
        assert client.principal is principal
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# 7. three-way identity requires COMPLETE provider+model tuples (Batch2 #F5)    #
# --------------------------------------------------------------------------- #
def _detail_parts(*, bp=None, bm=None, dp=None, dm=None) -> Dict[str, Any]:
    """A run-detail with independently-controllable bound/dispatched provider/model,
    so a PARTIAL (provider XOR model) side can be exercised."""
    d: Dict[str, Any] = {}
    if bp is not None or bm is not None:
        d["runtime_effective_binding"] = {"provider": bp, "model": bm, "binding_version": 2}
    if dp is not None:
        d["provider"] = dp
    if dm is not None:
        d["model"] = dm
    return d


def _kinds(summary):
    return {d.get("kind") for d in (summary.get("identity_divergences") or [])}


def test_partial_dispatched_provider_only_fails_closed(tmp_path):
    _, summary = _drive(mode=MODE_LOCAL_RUNTIME, out=tmp_path / "e",
                        detail=_detail_parts(bp=_PROVIDER, bm=_MODEL, dp=_PROVIDER))
    assert "dispatched_incomplete" in _kinds(summary)
    assert _cli.identity_gate_exit_code(summary) == 6


def test_partial_dispatched_model_only_fails_closed(tmp_path):
    _, summary = _drive(mode=MODE_LOCAL_RUNTIME, out=tmp_path / "e",
                        detail=_detail_parts(bp=_PROVIDER, bm=_MODEL, dm=_MODEL))
    assert "dispatched_incomplete" in _kinds(summary)
    assert _cli.identity_gate_exit_code(summary) == 6


def test_partial_bound_identity_fails_closed(tmp_path):
    _, summary = _drive(mode=MODE_REAL_PROVIDER, out=tmp_path / "e",
                        detail=_detail_parts(bp=_PROVIDER, dp=_PROVIDER, dm=_MODEL))
    assert "bound_incomplete" in _kinds(summary)
    assert _cli.identity_gate_exit_code(summary) == 6


def test_all_three_complete_and_equal_is_clean(tmp_path):
    # bound + dispatched complete & equal (attested absent here) -> no divergence.
    _, summary = _drive(mode=MODE_LOCAL_RUNTIME, out=tmp_path / "e",
                        detail=_detail_parts(bp=_PROVIDER, bm=_MODEL,
                                             dp=_PROVIDER, dm=_MODEL))
    assert _cli.identity_gate_exit_code(summary) == 0


def test_complete_bound_vs_dispatched_mismatch_fails_closed(tmp_path):
    _, summary = _drive(mode=MODE_REAL_PROVIDER, out=tmp_path / "e",
                        detail=_detail_parts(bp=_PROVIDER, bm=_MODEL,
                                             dp="other-provider", dm=_MODEL))
    assert "bound_vs_dispatched" in _kinds(summary)
    assert _cli.identity_gate_exit_code(summary) == 6
