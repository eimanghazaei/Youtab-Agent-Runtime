"""WAVE-27: benchmark artifact redaction (marked ``benchmark``).

Proves that a secret carried inside a seam/host exception or a verdict reason is
scrubbed by ``youtab_runtime.redaction`` BEFORE it is persisted to
``results.jsonl`` / ``summary.json``, without changing any verdict or pass/fail
outcome (only the stored string representation is scrubbed).

Out of default pytest collection (``benchmark`` marker excluded by addopts); run
with ``-m benchmark`` like the rest of the deterministic benchmark suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmark.harness.recorder import Recorder
from tests.benchmark.harness.runner import Runner
from tests.benchmark.harness.schema import (
    MODE_DETERMINISTIC,
    BenchmarkRecord,
    Scenario,
)

pytestmark = pytest.mark.benchmark

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Inline-secret-shaped tokens the hardened scrubber must catch. AWS key ids and
# a long generic token are used because both are removed by scrub_text (which
# redact_error runs), so their absence proves the artifact was scrubbed.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_GENERIC = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP1234"


class _ExplodingSeam:
    """A seam whose run() raises a host/seam exception carrying secrets — the
    exact situation where ``str(exc)`` would otherwise land raw in the artifact.
    """

    name = "exploding-seam"

    def run(self, *_a, **_k):
        raise RuntimeError(
            f"upstream call to https://svc.internal/v1?token={_GENERIC} "
            f"failed with credential {_AWS_KEY}"
        )


def _scenario() -> Scenario:
    # Any valid family/mode is fine; the seam raises before the oracle is used.
    return Scenario(
        id="redaction_seam_explode",
        family="single_step_completion",
        mode=MODE_DETERMINISTIC,
        title="seam raises a secret-bearing exception",
        oracle="unused",
        executor="unused",
    )


def test_seam_exception_secret_not_written_to_artifacts(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "out")
    runner = Runner(recorder, mode=MODE_DETERMINISTIC, seam=_ExplodingSeam(),
                    repo_root=_REPO_ROOT, work_root=tmp_path / "work")
    row = runner.run_scenario(_scenario(), repetition=0)
    summary = recorder.finalize()

    # Verdict OUTCOME is unchanged: a harness/seam error is still an honest unknown.
    assert row["verdict"] == "unknown"
    # Provenance still records that a harness error occurred (truthiness preserved
    # so the CI gate's harness-error detection keeps working)...
    assert row["provenance"].get("harness_error")

    # ...but the raw secrets are gone from every persisted surface.
    results_txt = (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8")
    summary_txt = (tmp_path / "out" / "summary.json").read_text(encoding="utf-8")
    for blob in (results_txt, summary_txt, str(row), str(summary)):
        assert _AWS_KEY not in blob
        assert _GENERIC not in blob
    # The scrub marker is present in the stored reason.
    assert "[redacted]" in row["reason"]


def test_verdict_reason_and_provenance_secret_scrubbed_by_recorder(
    tmp_path: Path,
) -> None:
    """The recorder is the durable write chokepoint: even a record whose reason
    and provenance already carry a secret (e.g. an emitter forgot to scrub) is
    redacted before hitting results.jsonl / summary.json."""
    recorder = Recorder(tmp_path / "out")
    record = BenchmarkRecord(
        scenario_id="s1",
        scenario_family="single_step_completion",
        repetition=0,
        mode=MODE_DETERMINISTIC,
        runtime_head="deadbeef",
        platform_tag="win32",
        engine_pinned="deterministic-worker",
        principal={"tenant": "t", "user": "u"},
        run_id="r1",
        verdict="fail",
        self_reported_success=True,  # over-claim -> exercises divergence path
        honesty_divergence=False,
        observation_source=["state"],
        metrics={"latency_ms": 1.0},  # no terminal_status -> not "done"
        evidence_refs=[],
        provenance={"harness_error": f"boom {_AWS_KEY}",
                    "note": f"leaked={_GENERIC}"},
        reason=f"seam blew up with credential {_AWS_KEY} token {_GENERIC}",
    )
    stored = recorder.record(record)
    summary = recorder.finalize()

    # Honesty-divergence logic is unaffected (verdict=fail + claimed success +
    # not-done terminal status -> a real divergence), proving redaction did not
    # alter the verdict pipeline.
    assert stored["honesty_divergence"] is True
    assert summary["honesty_divergence_count"] == 1

    results_txt = (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8")
    summary_txt = (tmp_path / "out" / "summary.json").read_text(encoding="utf-8")
    for blob in (results_txt, summary_txt, str(stored), str(summary)):
        assert _AWS_KEY not in blob
        assert _GENERIC not in blob
    # The divergence record in the summary carries the (redacted) reason.
    assert summary["honesty_divergences"][0]["reason"] == stored["reason"]
    assert "[redacted]" in stored["reason"]
