"""Both shipped artifacts must load, agree and keep up — on *this* machine.

Background
----------
``test_wake_word_model_assets.py`` scores both artifacts on the committed
fixtures, and required CI runs it on ``ubuntu-latest`` only. That leaves the
two platforms where the binaries are actually loaded by users unexecuted:
Windows loads ``hey_youtab.onnx``, and macOS ARM64 loads ``hey_youtab.tflite``
because openWakeWord's ONNX embedding model returns near-zero scores there
(dscripka/openWakeWord#336). A wheel that fails to load, a runtime that lowers
a kernel differently, or an architecture-specific numeric difference would all
be invisible to a Linux-only gate.

This file is the per-OS half. It runs ``scripts/wakeword/verify_backends.py``
in-process, so what CI executes on each runner and what an operator executes on
a laptop are the same code path, and it asserts the three things acceptance
asks for and the regression suite does not measure:

* the two backends agree, per sample *and* per decision;
* latency is measured, and is under openWakeWord's 80 ms rescoring interval —
  a classifier slower than its own input cannot detect anything, however
  accurate it is;
* memory is measured at all, which is platform-specific code
  (``GetProcessMemoryInfo`` on Windows, ``getrusage`` elsewhere, with a
  kilobytes/bytes unit difference between Linux and macOS that is a silent
  1024x error if it is wrong).

Both backends are imported unconditionally, for the reason
``test_wake_word_model_assets.py`` records: a missing one is a broken
environment, and skipping the only test of a shipped binary is worse than a red
run.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WAKEWORDS = REPO / "tools" / "wakewords"
WORKFLOW = REPO / ".github" / "workflows" / "youtab-ci.yml"

#: Small on purpose. The measurement's job here is to prove the instrument
#: works on this OS; the operator's run uses the script's default of 20 passes
#: for a stable tail.
REPEATS = 2


@pytest.fixture(scope="module")
def verifier():
    spec = importlib.util.spec_from_file_location(
        "_verify_backends", REPO / "scripts" / "wakeword" / "verify_backends.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def report(verifier):
    return verifier.verify(repeats=REPEATS)


def test_the_report_covers_the_whole_committed_fixture_set(report):
    """Non-vacuity for everything below.

    Every assertion in this file is over ``report``. A verifier that scored an
    empty feature array would satisfy "no disagreements" and "no false
    accepts" perfectly, so the shape of what was scored is asserted first.
    """
    assert report["fixtures"]["count"] == 33, report["fixtures"]
    assert report["fixtures"]["positives"] >= 8
    assert report["fixtures"]["negatives"] >= 12
    for backend in ("onnx", "tflite"):
        scores = report["backends"][backend]["scores"]
        assert len(scores) == 33, f"{backend} scored {len(scores)} of 33 fixtures"
        # A backend returning one constant would agree with itself across every
        # sample and pass the parity check trivially.
        assert len(set(scores.values())) > 10, (
            f"{backend} returned {len(set(scores.values()))} distinct scores over "
            "33 fixtures; it is not discriminating between them"
        )


def test_every_criterion_the_verifier_checks_passes_on_this_platform(report):
    failed = sorted(name for name, ok in report["checks"].items() if not ok)
    assert not failed, (
        f"{report['environment']['system']} {report['environment']['machine']}: "
        f"{failed}\nfull report: {report['checks']}"
    )
    assert report["passed"]


def test_the_two_backends_agree_on_this_platform(report):
    parity = report["parity"]
    assert parity["max_abs_delta"] <= parity["tolerance"], (
        f"backends diverge by {parity['max_abs_delta']:.3e} on "
        f"{parity['worst_sample']} — over the {parity['tolerance']:.0e} tolerance"
    )


def test_latency_was_measured_and_is_under_the_rescoring_interval(report, verifier):
    for backend, measured in report["backends"].items():
        latency = measured["latency"]
        assert latency["inferences"] == 33 * REPEATS, (
            f"{backend} timed {latency['inferences']} inferences, not {33 * REPEATS}"
        )
        # > 0 rather than >= 0: a zero median means the clock was never read,
        # which is exactly what a mis-wired timing loop produces.
        assert latency["median_ms"] > 0.0, f"{backend} latency was never measured"
        assert latency["median_ms"] <= latency["p95_ms"] <= latency["max_ms"]
        assert latency["p95_ms"] < verifier.FRAME_INTERVAL_MS, (
            f"{backend} p95 {latency['p95_ms']:.2f} ms exceeds the "
            f"{verifier.FRAME_INTERVAL_MS} ms interval openWakeWord rescores at"
        )
        assert measured["load_ms"] > 0.0


def test_memory_was_measured_on_this_operating_system(report):
    """The platform-specific half, which is where this can silently fail.

    A ``getrusage`` unit mistake or a mis-declared ``ctypes`` restype does not
    raise; it returns a number that is wrong by 1024x or is zero.
    """
    for backend, measured in report["backends"].items():
        peak = measured["peak_rss_bytes"]
        assert peak > measured["artifact_bytes"], (
            f"{backend} peak RSS of {peak} B is smaller than the model file "
            f"itself ({measured['artifact_bytes']} B); the reading is wrong"
        )
        # A Python process with numpy and an inference runtime loaded is tens
        # of megabytes. Anything under 8 MB is a unit error, not a small
        # process.
        assert peak > 8_000_000, f"{backend} peak RSS reads as {peak} B"


def test_the_report_is_bound_to_the_bytes_it_measured(report):
    """A measurement that does not name the artifact is a measurement of nothing.

    Same rule as ``tools/wakewords/SHA256SUMS``: the numbers belong to those
    exact files, and a report that outlives them has to be detectably stale.
    """
    lines = (WAKEWORDS / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    recorded = {name: digest for digest, name in (line.split() for line in lines if line.strip())}
    assert len(recorded) == 2, recorded
    for backend, measured in report["backends"].items():
        on_disk = hashlib.sha256((WAKEWORDS / measured["artifact"]).read_bytes()).hexdigest()
        assert measured["sha256"] == on_disk
        assert measured["sha256"] == recorded[measured["artifact"]], (
            f"{measured['artifact']} does not match SHA256SUMS"
        )


def test_the_report_records_which_artifact_this_platform_would_load(report):
    """An evidence file has to say which platform it is evidence about.

    macOS ARM64 loads tflite and everywhere else loads ONNX, so a run that did
    not record the resolved framework could not be read as a qualification of
    either.
    """
    import tools.wake_word as ww

    assert report["environment"]["runtime_framework"] == ww.default_inference_framework()
    assert report["environment"]["runtime_framework"] in {"onnx", "tflite"}
    assert report["environment"]["system"]
    assert report["environment"]["machine"]
    for backend in ("onnxruntime", "ai_edge_litert"):
        version = report["environment"][backend]
        assert version and "NOT INSTALLED" not in version, f"{backend}: {version}"


# ── the CI wiring ────────────────────────────────────────────────────────────


def test_ci_runs_this_on_every_os_a_runner_exists_for():
    """The gate is only per-OS if CI actually runs it per OS.

    Everything above passes on one machine and proves nothing about the other
    two. This asserts the matrix that makes it a claim about platforms.
    """
    text = WORKFLOW.read_text(encoding="utf-8")

    job = text.find("\n  wake-word-backends:")
    assert job != -1, (
        "no wake-word-backends job in youtab-ci.yml; re-anchor this test rather "
        "than deleting it — the per-OS claim depends on that job existing"
    )
    body = text[job : text.find("\n  javascript:", job)]
    assert body.strip(), "the wake-word-backends job body could not be sliced out"

    matrix = re.search(r"^\s*os:\s*\[(?P<list>[^\]]*)\]", body, re.MULTILINE)
    assert matrix, "the job declares no `os:` matrix"
    runners = {entry.strip() for entry in matrix.group("list").split(",")}
    assert {"ubuntu-latest", "windows-latest"} <= runners, (
        f"the OS matrix is {runners}; Windows is where users load the ONNX file"
    )

    assert "scripts/wakeword/verify_backends.py" in body, (
        "the job does not run the verifier, so its matrix proves nothing"
    )
    assert "tests/tools/test_wake_word_backend_parity.py" in body


def test_the_macos_arm_runner_is_active_and_the_intel_trap_is_still_recorded():
    """macOS ARM64 must actually run, because it is the only platform that loads
    the tflite artifact.

    This test used to assert the opposite: that the leg was absent and that the
    absence was recorded as an Owner action. That was right while it was off,
    and it is why enabling the leg turned every wake-word job red on this one
    assertion. The decision has since been taken and the leg is on, so what
    needs pinning has inverted -- but not weakened. It is now an error for the
    leg to be *missing*, which is a stronger claim than the one it replaces.

    `default_inference_framework()` returns tflite on Darwin ARM64 and onnx
    everywhere else, so without this leg the shipped `.tflite` is never executed
    on the OS that loads it, and the parity claim rests on two platforms that
    both load the other file.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    job = text[text.find("\n  wake-word-backends:") : text.find("\n  javascript:")]

    # Non-vacuity: prove we sliced the real job before judging its contents.
    assert "verify_backends.py" in job, (
        "the wake-word-backends slice does not contain the verifier; this test "
        "is reading the wrong part of the workflow and must be re-anchored"
    )

    assert "macos-latest" in job, (
        "the macOS ARM64 leg is gone. The shipped .tflite is then never loaded "
        "on the only OS that loads it in production."
    )

    # It has to be in the *matrix*, not merely mentioned in a comment -- which
    # is exactly the state this test used to describe.
    matrix = re.search(r"^\s*os:\s*\[(?P<list>[^\]]*)\]", job, re.MULTILINE)
    include = re.search(r"^\s*include:\s*$(?P<body>(?:\n\s+.*)+?)(?=\n\s*#|\n\s*\w+:)",
                        job, re.MULTILINE)
    active = (matrix.group("list") if matrix else "") + (
        include.group("body") if include else "")
    assert "macos-latest" in active, (
        "macos-latest is named in the job but not in the active matrix or its "
        "include: block, so no macOS job is produced"
    )

    # macos-13 is Intel, and neither pinned wheel exists for macOS x86_64:
    # onnxruntime dropped universal2 after 1.22.0 and ai-edge-litert has only
    # ever shipped arm64 for Darwin. Enabling it would fail at pip install, so
    # the trap stays documented even though the leg is now on.
    assert "macos-13" in job, "the Intel-runner trap is not recorded"
    assert "macos-13" not in active, (
        "macos-13 is in the active matrix; that runner is Intel and cannot "
        "install either pinned backend"
    )
