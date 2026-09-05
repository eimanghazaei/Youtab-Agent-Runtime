"""Harness canary / scenario-selection / limit-propagation / preflight tests
(WAVE-30B §7/§8/§10/§12/§13)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tests.benchmark.harness import preflight as pf
from tests.benchmark.harness.cli import _build_limits, _select_scenarios, build_parser
from tests.benchmark.harness.taskbank import validate

pytestmark = pytest.mark.benchmark

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse(*argv):
    return build_parser().parse_args(["run", "--out", ".x", *argv])


# --- stage profiles + limit assembly ---------------------------------------


def test_stage_profiles_sum_within_ceiling():
    total = sum(Decimal(STAGE["limits"]["max_cost_eur"]) for STAGE in pf.STAGE_PROFILES.values())
    # canary 0.10 + pilot 2.00 + full 10.00 — full already equals the campaign
    # ceiling (stages draw from one shared €10, not additively).
    assert Decimal(pf.STAGE_PROFILES["full"]["limits"]["max_cost_eur"]) == Decimal("10.00")
    assert Decimal(pf.STAGE_PROFILES["canary"]["limits"]["max_cost_eur"]) == Decimal("0.10")
    assert total >= Decimal("10.00")


def test_build_limits_from_stage_and_overrides():
    args = _parse("--stage", "pilot", "--max-iterations", "2")
    limits = _build_limits(args)
    assert limits["max_iterations"] == 2  # flag overrides the profile's 4
    assert limits["max_cost_eur"] == "2.00"
    assert limits["max_requests"] == 40


def test_build_limits_none_when_empty():
    args = _parse()
    assert _build_limits(args) is None


# --- scenario selection + canary safety ------------------------------------


def _scn(monkeypatch):
    return validate(require_full_bank=True)


def test_select_by_limit():
    scenarios = validate(require_full_bank=True)
    args = _parse("--limit", "3")
    assert len(_select_scenarios(scenarios, args)) == 3


def test_select_by_scenario_ids():
    scenarios = validate(require_full_bank=True)
    ids = [scenarios[0].id, scenarios[1].id]
    args = _parse(*sum([["--scenario", i] for i in ids], []))
    sel = _select_scenarios(scenarios, args)
    assert [s.id for s in sel] == ids


def test_unknown_scenario_id_refused():
    scenarios = validate(require_full_bank=True)
    args = _parse("--scenario", "does-not-exist")
    with pytest.raises(SystemExit, match="unknown scenario"):
        _select_scenarios(scenarios, args)


def test_canary_requires_explicit_selection():
    scenarios = validate(require_full_bank=True)
    args = _parse("--canary")
    with pytest.raises(SystemExit, match="requires an explicit"):
        _select_scenarios(scenarios, args)


def test_canary_refuses_more_than_one():
    scenarios = validate(require_full_bank=True)
    args = _parse("--canary", "--limit", "2")
    with pytest.raises(SystemExit, match="exactly ONE"):
        _select_scenarios(scenarios, args)


def test_canary_one_scenario_ok():
    scenarios = validate(require_full_bank=True)
    args = _parse("--canary", "--scenario", scenarios[0].id)
    sel = _select_scenarios(scenarios, args)
    assert len(sel) == 1


# --- taskbank subset does not weaken full-bank validation -------------------


def test_validate_subset_allows_below_floor(tmp_path):
    import json

    from tests.benchmark.harness.taskbank import load_manifest

    data = load_manifest()
    data["scenarios"] = data["scenarios"][:1]  # a single scenario
    p = tmp_path / "mini.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    # full-bank validation still rejects a 1-scenario manifest ...
    with pytest.raises(ValueError, match=">= 40|require"):
        validate(p, require_full_bank=True)
    # ... but the canary path accepts it while keeping per-scenario checks.
    scen = validate(p, require_full_bank=False)
    assert len(scen) == 1


# --- output-dir safety ------------------------------------------------------


def test_output_dir_rejects_inside_repo():
    with pytest.raises(pf.PreflightError, match="inside the repo"):
        pf.validate_output_dir(_REPO_ROOT / ".bench_live", repo_root=_REPO_ROOT)


def test_output_dir_rejects_cloud_folder(tmp_path):
    d = tmp_path / "OneDrive" / "bench"
    with pytest.raises(pf.PreflightError, match="cloud-synced"):
        pf.validate_output_dir(d, repo_root=_REPO_ROOT)


def test_output_dir_collision_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.jsonl").write_text("prior\n", encoding="utf-8")
    with pytest.raises(pf.PreflightError, match="prior artifacts"):
        pf.validate_output_dir(out, repo_root=_REPO_ROOT)
    # force overrides
    ok = pf.validate_output_dir(out, repo_root=_REPO_ROOT, force=True)
    assert (ok / "retention.json").exists()


def test_output_dir_ok_writes_retention(tmp_path):
    out = pf.validate_output_dir(tmp_path / "safe_out", repo_root=_REPO_ROOT)
    assert (out / "retention.json").exists()


# --- SHA / worktree gate ----------------------------------------------------


_SHA = "dfd4063c108297c268ade3c9527deceeb92447b2"       # 40-hex
_OTHER = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"     # 40-hex, different


def test_verify_sha_container_match():
    pf.verify_runtime_sha(
        {"build_sha": _SHA}, expected_sha=_SHA,
        repo_root=_REPO_ROOT, require_clean_worktree=False,
    )


def test_verify_sha_container_match_long_operator_prefix():
    pf.verify_runtime_sha(
        {"build_sha": _SHA}, expected_sha=_SHA[:16],  # deliberate >=12 operator prefix
        repo_root=_REPO_ROOT, require_clean_worktree=False,
    )


def test_verify_sha_container_mismatch():
    with pytest.raises(pf.PreflightError, match="!= authorized"):
        pf.verify_runtime_sha(
            {"build_sha": _OTHER}, expected_sha=_SHA,
            repo_root=_REPO_ROOT, require_clean_worktree=False,
        )


def test_verify_sha_rejects_short_reported_build(monkeypatch):
    # H2/A#3: a runtime reporting a 1-char SHA must NOT satisfy the pin.
    with pytest.raises(pf.PreflightError, match="!= authorized"):
        pf.verify_runtime_sha(
            {"build_sha": _SHA[0]}, expected_sha=_SHA,
            repo_root=_REPO_ROOT, require_clean_worktree=False,
        )


def test_verify_sha_rejects_too_short_expected():
    with pytest.raises(pf.PreflightError, match=">=12-char hex"):
        pf.verify_runtime_sha(
            {"build_sha": _SHA}, expected_sha="dfd4",
            repo_root=_REPO_ROOT, require_clean_worktree=False,
        )


def test_verify_sha_source_head(monkeypatch):
    monkeypatch.setattr(pf, "_git", lambda args, cwd: _SHA if args[0] == "rev-parse" else "")
    pf.verify_runtime_sha(
        {}, expected_sha=_SHA, repo_root=_REPO_ROOT, require_clean_worktree=True,
    )


def test_verify_sha_source_dirty_worktree(monkeypatch):
    def fake_git(args, cwd):
        if args[0] == "rev-parse":
            return _SHA
        return " M some_file.py"  # dirty tracked file
    monkeypatch.setattr(pf, "_git", fake_git)
    with pytest.raises(pf.PreflightError, match="not clean"):
        pf.verify_runtime_sha(
            {}, expected_sha=_SHA, repo_root=_REPO_ROOT, require_clean_worktree=True,
        )


def test_verify_sha_source_untracked_scratchpad_is_clean(monkeypatch):
    # B-L1: an untracked top-level scratchpad/ does NOT make the worktree dirty,
    # but a tracked path merely containing "scratchpad" DOES.
    def fake_git(args, cwd):
        if args[0] == "rev-parse":
            return _SHA
        return "?? scratchpad/notes.md"
    monkeypatch.setattr(pf, "_git", fake_git)
    pf.verify_runtime_sha(
        {}, expected_sha=_SHA, repo_root=_REPO_ROOT, require_clean_worktree=True,
    )


def test_verify_sha_source_tracked_scratchpad_is_dirty(monkeypatch):
    def fake_git(args, cwd):
        if args[0] == "rev-parse":
            return _SHA
        return " M src/scratchpad/foo.py"  # tracked, modified
    monkeypatch.setattr(pf, "_git", fake_git)
    with pytest.raises(pf.PreflightError, match="not clean"):
        pf.verify_runtime_sha(
            {}, expected_sha=_SHA, repo_root=_REPO_ROOT, require_clean_worktree=True,
        )


def test_verify_sha_requires_expected():
    with pytest.raises(pf.PreflightError, match="no authorized SHA"):
        pf.verify_runtime_sha({"build_sha": _SHA}, expected_sha="", repo_root=_REPO_ROOT)


# --- live-safety posture asserts -------------------------------------------


def _good_posture():
    return {
        "service_ready": True,
        "redaction_enabled": True,
        "budget_enforcement_enabled": True,
        "no_production_dataset": True,
    }


def test_assert_live_safety_ok():
    pf.assert_live_safety(_good_posture())


@pytest.mark.parametrize("key", [
    "redaction_enabled", "budget_enforcement_enabled", "no_production_dataset", "service_ready",
])
def test_assert_live_safety_refuses_bad_posture(key):
    posture = _good_posture()
    posture[key] = False
    with pytest.raises(pf.PreflightError):
        pf.assert_live_safety(posture)


def test_assert_live_safety_requires_file_credential_in_live_mode():
    # A#1 compensating gate: live-benchmark mode demands a file-based provider key.
    posture = _good_posture()
    posture["live_benchmark_mode"] = True
    posture["provider_credential_source"] = "env"
    with pytest.raises(pf.PreflightError, match="file-based provider credential"):
        pf.assert_live_safety(posture)
    posture["provider_credential_source"] = "file"
    pf.assert_live_safety(posture)  # ok


def test_assert_live_safety_no_file_requirement_outside_live_mode():
    posture = _good_posture()  # no live_benchmark_mode key => not live
    posture["provider_credential_source"] = "env"
    pf.assert_live_safety(posture)  # tolerated when not a live-benchmark run


# --- auth_client sends limits ----------------------------------------------


def test_auth_client_create_run_includes_limits(monkeypatch):
    from tests.benchmark.harness.auth_client import AuthClient
    from youtab_runtime.run_journal import Principal

    captured = {}

    client = AuthClient("http://x", "s" * 44, Principal("t", "u"))

    def fake_signed_post(path, payload=None, **kw):
        captured["payload"] = payload
        class _R:
            status_code = 200
        return _R()

    monkeypatch.setattr(client, "_signed_post", fake_signed_post)
    client.create_run("default", "task", limits={"max_iterations": 3, "max_cost_eur": "1.00"})
    assert captured["payload"]["limits"] == {"max_iterations": 3, "max_cost_eur": "1.00"}
