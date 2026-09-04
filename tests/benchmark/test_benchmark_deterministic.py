"""Deterministic benchmark gate + synthetic-journey smoke (marked ``benchmark``).

This is the CI-required behaviour: run the whole deterministic task bank against
the REAL WAVE-26 substrate (run journal, effect ledger, egress audit, harness
process control) and assert that every scenario reaches the verdict a correctly-
functioning runtime must yield. Because the side effects are real substrate
writes, a regression in any WAVE-26 module flips a verdict and fails this test.

No provider, no network, no production home. Out of default pytest collection
(the ``benchmark`` marker is excluded by ``addopts``); the dedicated CI job
re-selects it with ``-m benchmark`` and also runs the CLI as an independent
backstop against a silent no-collection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmark.harness.recorder import Recorder
from tests.benchmark.harness.runner import Runner
from tests.benchmark.harness.schema import MODE_DETERMINISTIC
from tests.benchmark.harness.seam import DeterministicSubstrateSeam
from tests.benchmark.harness.taskbank import MIN_SCENARIOS, validate

pytestmark = pytest.mark.benchmark

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_deterministic_bank_matches_expected_verdicts(tmp_path: Path) -> None:
    scenarios = validate()
    assert len(scenarios) >= MIN_SCENARIOS

    recorder = Recorder(tmp_path / "out")
    runner = Runner(recorder, mode=MODE_DETERMINISTIC, repo_root=_REPO_ROOT,
                    work_root=tmp_path / "work")
    rows = runner.run_all(scenarios, repetitions=1)
    summary = recorder.finalize()

    by_id = {s.id: s for s in scenarios}

    # No scenario may fail because the harness itself broke.
    harness_errors = [
        r for r in rows if (r.get("provenance") or {}).get("harness_error")
    ]
    assert not harness_errors, harness_errors

    # Every observed verdict must equal the expected verdict for the scenario.
    # A host-capability gap (e.g. no /proc for the restart scenario) is accepted
    # as unknown by verdict_matches_expected and is not a mismatch.
    mismatches = [
        (r["scenario_id"], r["verdict"], by_id[r["scenario_id"]].expected_verdict,
         r["reason"])
        for r in rows
        if not Runner.verdict_matches_expected(r, by_id[r["scenario_id"]])
    ]
    assert not mismatches, mismatches

    # Coverage + record floor land in the summary.
    assert not summary["families_missing"], summary["families_missing"]
    assert summary["total_records"] >= MIN_SCENARIOS

    # runtime_head must be derived at run time, NOT a hardcoded SHA.
    assert summary["runtime_head"] != "f5582d332"


def test_honesty_divergences_are_flagged(tmp_path: Path) -> None:
    """The over-claim scenarios (state says fail, agent claims success) must
    populate honesty_divergences[] — the headline aggregate."""
    scenarios = validate()
    recorder = Recorder(tmp_path / "out")
    runner = Runner(recorder, mode=MODE_DETERMINISTIC, repo_root=_REPO_ROOT,
                    work_root=tmp_path / "work")
    runner.run_all(scenarios, repetitions=1)
    summary = recorder.finalize()

    flagged = {d["scenario_id"] for d in summary["honesty_divergences"]}
    for expected in (
        "truthful_incomplete_reported_done",
        "truthful_incomplete_missing_artifact",
        "multi_partial_claimed_done",
    ):
        assert expected in flagged, (expected, sorted(flagged))
    # A truthful completion is NOT flagged as a divergence.
    assert "single_complete_ok" not in flagged


def test_metrics_never_fabricate_zero_for_unsupported(tmp_path: Path) -> None:
    """Contract-9: an unsupported metric is 'unknown' + provenance, never 0."""
    scenarios = validate()
    recorder = Recorder(tmp_path / "out")
    runner = Runner(recorder, mode=MODE_DETERMINISTIC, repo_root=_REPO_ROOT,
                    work_root=tmp_path / "work")
    runner.run_all(scenarios, repetitions=1)
    summary = recorder.finalize()
    metrics = summary["metrics"]

    for name in ("tool_selection_accuracy", "tool_argument_accuracy",
                 "total_tokens_per_success", "cost_per_success"):
        assert metrics[name]["value"] == "unknown", (name, metrics[name])
        assert metrics[name].get("computable_in") == "real_provider"

    # Deterministic-computable rate metrics are real numbers, not "unknown".
    for name in ("task_success_rate", "injection_compliance_rate",
                 "duplicate_effect_rate", "cross_principal_leakage_rate"):
        assert metrics[name]["value"] != "unknown", (name, metrics[name])


def test_synthetic_journey_smoke(tmp_path: Path) -> None:
    """Minimal end-to-end journey through the substrate seam: drive one scenario
    and prove the ordered lifecycle events + a passing oracle from durable state.
    """
    from tests.benchmark.harness.oracles import ORACLES

    scenarios = {s.id: s for s in validate()}
    scenario = scenarios["single_complete_ok"]
    seam = DeterministicSubstrateSeam(repo_root=_REPO_ROOT)
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    obs = seam.run(scenario, home=home, workspace=workspace,
                   tenant="smoke-tenant", user="smoke-user", run_id="smoke-run")

    kinds = obs.event_kinds("lifecycle")
    assert "run_started" in kinds and "run_completed" in kinds
    verdict = ORACLES[scenario.oracle](obs, obs.provenance.get("_oracle_params", {}))
    assert verdict.outcome.value == "pass", verdict.reason
    # Ordering authority is seq: events are strictly increasing.
    seqs = [e["seq"] for e in obs.events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_restart_dimension_is_really_exercised_on_capable_hosts(tmp_path: Path) -> None:
    """On a host that can prove (pid, start_time) ownership — the CI ubuntu
    runner (via /proc) or any host with psutil — the restart/state-recovery and
    synthetic-journey scenarios MUST yield a real ``pass``, never a silently
    accepted ``capability_unavailable`` unknown. Without this, the restart
    dimension could read green on CI without ever actually running (reviewer C
    N2). On a genuinely incapable host the test skips.
    """
    from youtab_runtime import harness_process as hp
    from youtab_runtime.run_journal import Principal

    probe = hp.HarnessProcess(isolated_benchmark=True,
                              home=tmp_path / "probe_home",
                              principal=Principal("cap", "probe"))
    try:
        probe.launch(mode=hp.CHILD_MODE_IDLE)
        if not probe.wait_ready(timeout=30):
            pytest.skip("harness child did not become ready on this host")
        capable = probe.prove_ownership() is hp.OwnershipOutcome.OWNED
    finally:
        probe.close()
    if not capable:
        pytest.skip("host cannot prove process ownership; the restart dimension "
                    "is exercised on capable CI hosts, not here")

    scenarios = {s.id: s for s in validate()}
    must_really_pass = [scenarios[sid] for sid in
                        ("restart_recovers_state", "synthetic_journey_full")
                        if sid in scenarios]
    assert must_really_pass, "restart/journey scenarios missing from the bank"

    recorder = Recorder(tmp_path / "out_cap")
    runner = Runner(recorder, mode=MODE_DETERMINISTIC, repo_root=_REPO_ROOT,
                    work_root=tmp_path / "work_cap")
    rows = {r["scenario_id"]: r
            for r in runner.run_all(must_really_pass, repetitions=1)}
    for s in must_really_pass:
        r = rows[s.id]
        assert r["verdict"] == "pass", (s.id, r["verdict"], r.get("reason"))
        assert not (r.get("provenance") or {}).get("capability_unavailable"), s.id
