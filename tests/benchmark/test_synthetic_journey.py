"""End-to-end synthetic journey + >=20-principal concurrency proof (``benchmark``).

Two Owner-§9 additions, judged ONLY from observable state / side effects:

* the coherent 18-step ``synthetic_journey`` (start -> auth -> run -> effects ->
  injection-deny -> fault+recovery -> restart+recover -> complete -> shutdown),
  in a pass variant and TWO adversarial variants that inject a REAL violation
  (an egress that succeeds to the attacker; a duplicate committed effect) so the
  journey oracle proves it catches a real breach;
* ``concurrency_isolation_20`` — >=20 concurrent DISTINCT principals with zero
  state/event/artifact/authority leakage, each committed exactly once, none stuck.

Everything runs against the REAL WAVE-26 substrate (run journal, effect ledger,
egress audit) + the real harness-owned process, in isolated homes, with bounded
waits and no arbitrary sleeps. A host that cannot prove process ownership yields
an honest ``unknown`` (accepted by ``verdict_matches_expected``), never a false
pass/fail — so this stays CI-runnable on capability-limited hosts.

Out of default collection (the ``benchmark`` marker is excluded by ``addopts``);
the dedicated CI job re-selects it with ``-m benchmark``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from tests.benchmark.harness.executors import CapabilityUnavailable
from tests.benchmark.harness.oracles import ORACLES
from tests.benchmark.harness.recorder import Recorder
from tests.benchmark.harness.runner import Runner
from tests.benchmark.harness.schema import MODE_DETERMINISTIC, Observation, Scenario
from tests.benchmark.harness.seam import DeterministicSubstrateSeam
from tests.benchmark.harness.taskbank import validate

pytestmark = pytest.mark.benchmark

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _scenarios() -> dict[str, Scenario]:
    return {s.id: s for s in validate()}


def _run_via_runner(scenario: Scenario, tmp_path: Path) -> dict:
    recorder = Recorder(tmp_path / "out")
    runner = Runner(recorder, mode=MODE_DETERMINISTIC, repo_root=_REPO_ROOT,
                    work_root=tmp_path / "work")
    row = runner.run_scenario(scenario)
    # A harness/seam bug (not a capability gap) must never masquerade as a result.
    assert not (row.get("provenance") or {}).get("harness_error"), row
    return row


def _seam_observe(scenario: Scenario, tmp_path: Path) -> Optional[Observation]:
    """Drive one scenario through the deterministic seam, returning the assembled
    Observation. Returns None when the host cannot prove process ownership (an
    honest capability gap, not a runtime defect)."""
    seam = DeterministicSubstrateSeam(repo_root=_REPO_ROOT)
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    try:
        return seam.run(scenario, home=home, workspace=workspace,
                        tenant="bench-tenant", user="bench-user",
                        run_id=f"jrny-{scenario.id}")
    except CapabilityUnavailable:
        return None


# --------------------------------------------------------------------------- #
# Registry wiring                                                              #
# --------------------------------------------------------------------------- #
def test_journey_and_concurrency_registered() -> None:
    scenarios = _scenarios()
    for sid in ("synthetic_journey_full", "synthetic_journey_forbidden_egress_leak",
                "synthetic_journey_duplicate_commit", "concurrency_isolation_20"):
        assert sid in scenarios, sid
    assert "synthetic_journey_complete" in ORACLES
    assert "concurrency_principals_isolated" in ORACLES


# --------------------------------------------------------------------------- #
# Synthetic journey: pass variant                                             #
# --------------------------------------------------------------------------- #
def test_synthetic_journey_pass(tmp_path: Path) -> None:
    scenario = _scenarios()["synthetic_journey_full"]
    row = _run_via_runner(scenario, tmp_path)
    assert Runner.verdict_matches_expected(row, scenario), row

    # On a capable host, assert the observable evidence for the headline steps.
    obs = _seam_observe(scenario, tmp_path / "seam")
    if obs is None:
        pytest.skip("host cannot prove process ownership; owned-kill unavailable")
    params = obs.provenance.get("_oracle_params", {})
    verdict = ORACLES[scenario.oracle](obs, params)
    assert verdict.outcome.value == "pass", verdict.reason

    proc = set(obs.event_kinds("process"))
    assert {"spawned", "ready", "killed", "recovered", "shutdown"} <= proc, proc
    life = set(obs.event_kinds("lifecycle"))
    assert {"run_created", "run_started", "resume_claimed", "run_completed"} <= life
    # Exactly one committed effect; the forbidden effect never committed.
    committed = [e for e in obs.effects if e.get("state") == "committed"]
    assert len(committed) == 1, committed
    # An authorized AND a denied egress decision, and no breach to the attacker.
    kinds = [e.get("kind") for e in obs.egress_events()]
    assert "authorized" in kinds and "denied" in kinds, kinds
    assert "succeeded" not in kinds and "attempted" not in kinds, kinds


# --------------------------------------------------------------------------- #
# Synthetic journey: adversarial variants MUST fail (honesty proof)           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sid", [
    "synthetic_journey_forbidden_egress_leak",
    "synthetic_journey_duplicate_commit",
])
def test_synthetic_journey_adversarial_fails(sid: str, tmp_path: Path) -> None:
    scenario = _scenarios()[sid]
    assert scenario.expected_verdict == "fail"
    row = _run_via_runner(scenario, tmp_path)
    # On a capable host the injected breach flips the verdict to fail; on an
    # ownership-incapable host it is an accepted unknown (capability gap).
    assert Runner.verdict_matches_expected(row, scenario), row

    obs = _seam_observe(scenario, tmp_path / "seam")
    if obs is None:
        pytest.skip("host cannot prove process ownership; owned-kill unavailable")
    params = obs.provenance.get("_oracle_params", {})
    verdict = ORACLES[scenario.oracle](obs, params)
    assert verdict.outcome.value == "fail", (
        "the journey oracle must catch the injected breach", verdict.reason)


# --------------------------------------------------------------------------- #
# >= 20-principal concurrency proof                                           #
# --------------------------------------------------------------------------- #
def test_concurrency_20_principals_isolated(tmp_path: Path) -> None:
    scenario = _scenarios()["concurrency_isolation_20"]
    assert int(scenario.params.get("principals", 0)) >= 20

    # No subprocess/ownership needed -> the seam always produces an Observation.
    obs = _seam_observe(scenario, tmp_path / "seam")
    assert obs is not None
    params = obs.provenance.get("_oracle_params", {})
    verdict = ORACLES[scenario.oracle](obs, params)
    assert verdict.outcome.value == "pass", verdict.reason

    data = obs.provenance["concurrency_principals"]
    assert data["principal_count"] >= 20
    assert not data["worker_errors"], data["worker_errors"]
    # The base observing principal is foreign to every worker -> sees nothing.
    assert obs.events == [] and obs.effects == []
    # Every principal isolated: own state present, foreign probe empty.
    for e in data["per_principal"]:
        assert e["own_events"] > 0, e
        assert e["own_committed_effects"] == 1, e
        assert e["own_terminal"] == "done", e
        assert e["own_token_in_file"] is True, e
        assert e["foreign_events_visible"] == 0, e
        assert e["foreign_effects_visible"] == 0, e
        assert e["foreign_token_in_file"] is False, e

    # Also assert via the runner gate for parity with the deterministic bank.
    row = _run_via_runner(scenario, tmp_path / "runner")
    assert Runner.verdict_matches_expected(row, scenario), row
