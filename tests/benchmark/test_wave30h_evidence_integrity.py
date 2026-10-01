"""WAVE-30H A5: benchmark evidence integrity — append-only, immutable, checksummed.

Proves the harness can NEVER truncate, overwrite, replace, or silently drop a
prior run's benchmark evidence; that every live run receives a UNIQUE evidence
directory; and that ``provenance.json`` + ``MANIFEST.sha256`` make any
post-finalize edit/deletion detectable. These are the regressions that guard the
Owner A5 mandate ("previous evidence cannot be replaced, truncated or deleted").

Out of default collection (``benchmark`` marker excluded by addopts); run with
``-m benchmark`` like the rest of the deterministic benchmark suite.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from tests.benchmark.harness import preflight as pf
from tests.benchmark.harness.recorder import (
    MANIFEST_FILE,
    PROVENANCE_FILE,
    EvidenceExistsError,
    Recorder,
    verify_manifest,
)
from tests.benchmark.harness.runner import Runner
from tests.benchmark.harness.schema import MODE_DETERMINISTIC, Scenario

pytestmark = pytest.mark.benchmark

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _ExplodingSeam:
    """A seam that raises — produces a real (unknown-verdict) record cheaply."""

    name = "exploding-seam"

    def run(self, *_a, **_k):
        raise RuntimeError("boom")


def _scenario() -> Scenario:
    return Scenario(
        id="a5_evidence",
        family="single_step_completion",
        mode=MODE_DETERMINISTIC,
        title="evidence integrity fixture",
        oracle="unused",
        executor="unused",
    )


def _finalized_dir(tmp_path: Path) -> Path:
    """Run one scenario into a fresh dir and finalize → real evidence artifacts."""
    out = tmp_path / "out"
    rec = Recorder(out)
    runner = Runner(
        rec, mode=MODE_DETERMINISTIC, seam=_ExplodingSeam(),
        repo_root=_REPO_ROOT, work_root=tmp_path / "work",
    )
    runner.run_scenario(_scenario(), repetition=0)
    rec.finalize()
    return out


# --- append-only: never truncate/overwrite/replace prior evidence -----------


def test_recorder_refuses_prior_evidence_and_never_truncates(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.jsonl").write_text("PRIOR-RECORD\n", encoding="utf-8")
    # Construction fails closed instead of truncating (the old behaviour destroyed
    # prior evidence with an unconditional write_text("")).
    with pytest.raises(EvidenceExistsError):
        Recorder(out)
    # The prior bytes survive the refusal — not truncated, not replaced.
    assert (out / "results.jsonl").read_text(encoding="utf-8") == "PRIOR-RECORD\n"


def test_second_run_cannot_replace_a_finalized_dir(tmp_path: Path) -> None:
    out = _finalized_dir(tmp_path)
    before_summary = (out / "summary.json").read_text(encoding="utf-8")
    before_results = (out / "results.jsonl").read_text(encoding="utf-8")
    with pytest.raises(EvidenceExistsError):
        Recorder(out)  # a second run must not reuse a dir already holding evidence
    # Every prior artifact is intact — cannot be replaced/truncated/deleted.
    assert (out / "summary.json").read_text(encoding="utf-8") == before_summary
    assert (out / "results.jsonl").read_text(encoding="utf-8") == before_results


# --- immutable provenance + SHA-256 manifest --------------------------------


def test_finalize_writes_provenance_and_manifest(tmp_path: Path) -> None:
    out = _finalized_dir(tmp_path)
    prov = json.loads((out / PROVENANCE_FILE).read_text(encoding="utf-8"))
    assert "runtime_head" in prov
    assert "created_at_utc" in prov
    assert prov["evidence_policy"].startswith("append-only")
    assert (out / MANIFEST_FILE).exists()
    # Freshly finalized evidence verifies clean.
    assert verify_manifest(out) == {"ok": True, "mismatches": [], "missing": []}


def test_manifest_detects_post_finalize_tampering(tmp_path: Path) -> None:
    out = _finalized_dir(tmp_path)
    (out / "summary.json").write_text("{}", encoding="utf-8")  # tamper
    res = verify_manifest(out)
    assert res["ok"] is False
    assert "summary.json" in res["mismatches"]


def test_manifest_detects_deleted_artifact(tmp_path: Path) -> None:
    out = _finalized_dir(tmp_path)
    (out / PROVENANCE_FILE).unlink()  # delete a recorded artifact
    res = verify_manifest(out)
    assert res["ok"] is False
    assert PROVENANCE_FILE in res["missing"]


# --- validate_output_dir: --force can never overwrite evidence --------------


def test_validate_output_dir_force_cannot_overwrite_evidence(tmp_path: Path) -> None:
    out = tmp_path / "ev"
    out.mkdir()
    (out / "summary.json").write_text('{"x": 1}', encoding="utf-8")
    with pytest.raises(pf.PreflightError, match="force cannot"):
        pf.validate_output_dir(out, repo_root=_REPO_ROOT, force=True)
    assert (out / "summary.json").read_text(encoding="utf-8") == '{"x": 1}'


# --- unique per-run evidence directory --------------------------------------


def test_mint_run_evidence_dir_is_unique_sha_and_runid_bound(tmp_path: Path) -> None:
    base = tmp_path / "base"
    d1 = pf.mint_run_evidence_dir(base, runtime_head="b500615637d8ab", run_id="camp-1")
    d2 = pf.mint_run_evidence_dir(base, runtime_head="b500615637d8ab", run_id="camp-2")
    assert d1.exists() and d2.exists()
    assert d1 != d2
    assert d1.parent == base and d2.parent == base
    assert d1.name.startswith("run-") and "b500615637d8" in d1.name
    assert d1.name.endswith("camp-1") and d2.name.endswith("camp-2")


def test_mint_run_evidence_dir_refuses_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Freeze the clock so two mints compute the SAME target name → the second must
    # fail closed rather than reuse/overwrite an existing run directory.
    class _FixedClock:
        @staticmethod
        def now(tz=None):
            return _dt.datetime(2026, 9, 10, 12, 0, 0, tzinfo=_dt.timezone.utc)

    monkeypatch.setattr(pf, "datetime", _FixedClock)
    base = tmp_path / "b"
    first = pf.mint_run_evidence_dir(base, runtime_head="abc123def456", run_id=None)
    assert first.exists()
    with pytest.raises(pf.PreflightError, match="already exists"):
        pf.mint_run_evidence_dir(base, runtime_head="abc123def456", run_id=None)
