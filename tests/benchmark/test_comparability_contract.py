"""Dual-track comparability contract tests (WAVE-30C §2).

Proves Track A (local/ECO) and Track B (cloud, Owner-selected non-Anthropic) are
*legitimately* comparable: they load the byte-identical task bank, share the same
output/taskbank schema versions and the same technically-meaningful run limits,
and differ ONLY within each track's ``per_track`` section. Also proves the two
honesty invariants the Owner set: Track A never substitutes a fabricated canonical
model for the Owner-supplied ECO tag, and Track B never defaults to Anthropic and
stays Owner-gated. No provider, no network — validated in the deterministic gate.
"""

from __future__ import annotations

import pytest

from tests.benchmark.harness import tracks
from tests.benchmark.harness.tracks import (
    ComparabilityError,
    SHARED_LIMIT_KEYS,
    load_track,
    track_provenance,
    validate_comparability,
)

pytestmark = pytest.mark.benchmark


def test_comparability_holds_against_real_task_bank():
    summary = validate_comparability()
    assert summary["tracks"] == ["A", "B"]
    assert summary["scenario_count"] == 49
    assert summary["family_count"] == 21
    assert set(summary["shared_limits"]) == set(SHARED_LIMIT_KEYS)


def test_shared_sections_are_identical_across_tracks():
    a = load_track("A")
    b = load_track("B")
    assert a["shared"] == b["shared"], "the shared invariants must be byte-identical"


def test_shared_limits_are_meaningful_and_cost_is_per_track():
    a = load_track("A")
    limits = a["shared"]["shared_limits"]
    assert set(limits) == set(SHARED_LIMIT_KEYS)
    # Cost is deliberately NOT a shared limit — it is per-track (€0 vs ≤€10).
    assert "max_cost_eur" not in limits


def test_track_a_is_local_zero_cost_owner_supplied_model():
    a = load_track("A")["per_track"]
    assert a["execution"] == "local_inference"
    assert a["cost_model"] == "local_zero_api_cost"
    assert a["campaign_budget"] == "excluded"
    assert a["model_identifier_status"] == "OWNER_MODEL_IDENTIFIER_REQUIRED"
    assert a["model_source"] == "owner_supplied_via_env_or_registry"
    # The model must be an env placeholder, never a hardcoded canonical model —
    # "Qwen 3.5 9B" is not a canonical identifier and must not be substituted.
    assert a["model_name"].startswith("${") and a["model_name"].endswith("}")
    # The electricity cost is kept separate from the €10 cloud API budget.
    assert "estimated_electricity_cost_eur" in a["resource_metrics"]


def test_track_b_is_non_anthropic_owner_gated_file_credential():
    b = load_track("B")["per_track"]
    assert b["execution"] == "cloud_api"
    assert b["selection_status"] == "OWNER_SELECTION_REQUIRED"
    assert b["provider_name"] == "OWNER_SELECTION_REQUIRED"
    assert b["model_name"] == "OWNER_SELECTION_REQUIRED"
    assert "anthropic" in [x.lower() for x in b["provider_constraints"]["exclude"]]
    assert b["credential_source"] == "file"
    assert str(b["max_cost_eur"]) == "10.00"


def test_track_b_stage_allocation_sums_to_ceiling():
    from decimal import Decimal

    alloc = load_track("B")["per_track"]["stage_allocation"]
    total = Decimal(alloc["canary"]) + Decimal(alloc["pilot"]) + Decimal(alloc["full"])
    assert total == Decimal("10.00") == Decimal(alloc["cumulative_max"])


def test_track_provenance_stamp_is_self_describing():
    a = track_provenance(load_track("A"))
    b = track_provenance(load_track("B"))
    assert a["track"] == "A" and b["track"] == "B"
    assert a["provider_name"] == "ollama"
    assert b["provider_name"] == "OWNER_SELECTION_REQUIRED"
    assert a["cost_model"] == "local_zero_api_cost"
    assert b["cost_model"] == "cloud_api_eur_budgeted"


def test_unknown_track_id_is_refused():
    with pytest.raises(ComparabilityError, match="unknown track id"):
        load_track("Z")


def test_tracked_deterministic_run_stamps_provenance(tmp_path):
    """A deterministic run with a track stamp carries the track/provider identity
    into every record's provenance (proves the wiring end-to-end, no live call)."""
    from pathlib import Path

    from tests.benchmark.harness.recorder import Recorder
    from tests.benchmark.harness.runner import Runner
    from tests.benchmark.harness.schema import MODE_DETERMINISTIC
    from tests.benchmark.harness.taskbank import validate

    repo_root = Path(__file__).resolve().parents[2]
    scenarios = validate()[:3]
    recorder = Recorder(tmp_path / "out")
    runner = Runner(
        recorder, mode=MODE_DETERMINISTIC, repo_root=repo_root,
        work_root=tmp_path / "work",
        track_provenance=track_provenance(load_track("A")),
    )
    rows = runner.run_all(scenarios, repetitions=1)
    assert rows
    for r in rows:
        prov = r.get("provenance") or {}
        assert prov.get("track") == "A"
        assert prov.get("provider_name") == "ollama"
        assert prov.get("cost_model") == "local_zero_api_cost"


def test_default_untracked_run_adds_no_track_fields(tmp_path):
    """Without a track stamp the deterministic gate's records are unchanged."""
    from pathlib import Path

    from tests.benchmark.harness.recorder import Recorder
    from tests.benchmark.harness.runner import Runner
    from tests.benchmark.harness.schema import MODE_DETERMINISTIC
    from tests.benchmark.harness.taskbank import validate

    repo_root = Path(__file__).resolve().parents[2]
    scenarios = validate()[:2]
    recorder = Recorder(tmp_path / "out")
    runner = Runner(recorder, mode=MODE_DETERMINISTIC, repo_root=repo_root,
                    work_root=tmp_path / "work")
    rows = runner.run_all(scenarios, repetitions=1)
    for r in rows:
        assert "track" not in (r.get("provenance") or {})


def test_manifest_drift_breaks_comparability(tmp_path):
    """If the task bank changes but the pinned comparability hash is not updated,
    the contract fails LOUDLY — the tracks would no longer be pinned to the same
    bank. Simulate by pointing validation at a modified manifest copy."""
    import json

    from tests.benchmark.harness.taskbank import default_manifest_path

    data = json.loads(default_manifest_path().read_text(encoding="utf-8"))
    data["scenarios"] = data["scenarios"][:-1]  # drop one scenario -> hash drifts
    drifted = tmp_path / "manifest.json"
    drifted.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ComparabilityError):
        validate_comparability(manifest_path=drifted)
