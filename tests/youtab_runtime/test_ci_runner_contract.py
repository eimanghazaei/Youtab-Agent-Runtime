"""The required gate must prove a first-execution pass.

Background
----------
``scripts/run_tests_parallel.py`` re-runs a failing test FILE once by default
(``_DEFAULT_FILE_RETRIES = 1``) and counts a pass-on-retry as passed, reporting
it as FLAKY. Useful locally; not acceptable in the required CI gate, where a
test that only passes on the second attempt is not genuinely green.

Two independent defects let a hidden retry survive an operator's attempt to
turn retries off, and each gets a test here:

1. ``scripts/run_tests.sh`` execs the runner under ``env -i`` — an empty
   environment with an explicit opt-in whitelist. Every
   ``YOUTAB_AGENT_TEST_*`` knob the runner documents in its own ``--help``
   was missing from that whitelist, so ``YOUTAB_AGENT_TEST_FILE_RETRIES=0``
   never reached the runner. It fell back to ``1``. A run the operator had
   labelled ``file-retries=0`` still reported files that passed only on the
   retry, because the flag had been eaten by the plumbing.

2. The required gate (``scripts/youtab/run_all_gates.sh``) invoked
   ``run_tests.sh`` without ``--file-retries 0``, so the gate ran with the
   retry default regardless of what any environment variable said.

These are consistency tests over the real files, deliberately written to fail
loudly rather than vacuously: each one first asserts that it actually located
what it set out to parse. An earlier flag-consistency test in this repo passed
for months because its regex matched nothing and ``set() <= set()`` is true.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_TESTS_SH = REPO_ROOT / "scripts" / "run_tests.sh"
RUNNER_PY = REPO_ROOT / "scripts" / "run_tests_parallel.py"
GATES_SH = REPO_ROOT / "scripts" / "youtab" / "run_all_gates.sh"


def _runner_env_knobs_read_by_the_runner() -> set[str]:
    """Every ``YOUTAB_AGENT_TEST_*`` the runner actually reads at runtime.

    Matches ``os.environ.get("NAME"`` only — a name that appears solely in a
    comment or a help string is documentation, not a read, and forwarding it
    is not required for correctness.
    """
    source = RUNNER_PY.read_text(encoding="utf-8")
    return set(
        re.findall(
            r'os\.environ\.get\(\s*["\'](YOUTAB_AGENT_TEST_[A-Z_]+)["\']',
            source,
        )
    )


def _runner_env_knobs_forwarded_by_the_wrapper() -> set[str]:
    """Every knob listed in ``run_tests.sh``'s RUNNER_ENV forwarding loop.

    Parses the loop body specifically rather than scanning the whole file:
    the surrounding comments name several of these variables, and a
    whole-file scan would report a variable as "forwarded" when it is only
    mentioned in prose.
    """
    source = RUN_TESTS_SH.read_text(encoding="utf-8")
    match = re.search(
        r"^RUNNER_ENV=\(\)\s*\nfor _runner_var in(.*?)^do\b",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, (
        "could not find the RUNNER_ENV forwarding loop in run_tests.sh. "
        "If it was renamed or restructured, update this test — do not delete "
        "it: without the forwarding loop, every documented YOUTAB_AGENT_TEST_* "
        "knob is silently dropped by `env -i`."
    )
    return set(re.findall(r"YOUTAB_AGENT_TEST_[A-Z_]+", match.group(1)))


def test_documented_runner_knobs_survive_the_hermetic_env() -> None:
    """Every env knob the runner reads must cross ``env -i`` in the wrapper.

    This is the control that makes ``YOUTAB_AGENT_TEST_FILE_RETRIES=0``
    mean what it says. Mutation check: delete any name from the RUNNER_ENV
    loop in run_tests.sh and this test goes red.
    """
    read = _runner_env_knobs_read_by_the_runner()
    forwarded = _runner_env_knobs_forwarded_by_the_wrapper()

    # Non-vacuity guards: if either parse silently returns nothing, the
    # subset assertion below is trivially true and proves nothing.
    assert read, (
        "parsed zero YOUTAB_AGENT_TEST_* reads out of run_tests_parallel.py — "
        "the regex no longer matches the source, so this test is vacuous"
    )
    assert forwarded, (
        "parsed zero forwarded names out of run_tests.sh's RUNNER_ENV loop — "
        "this test is vacuous"
    )
    assert "YOUTAB_AGENT_TEST_FILE_RETRIES" in read, (
        "the runner no longer reads YOUTAB_AGENT_TEST_FILE_RETRIES; if the "
        "retry knob moved, re-anchor this test on its replacement"
    )

    missing = read - forwarded
    assert not missing, (
        f"run_tests.sh drops {sorted(missing)} at the `env -i` boundary. "
        "The runner documents these as environment knobs, so setting one and "
        "getting the built-in default instead is a silent lie — which is "
        "exactly how a run labelled 'file-retries=0' still retried."
    )


def test_required_gate_disables_file_retries() -> None:
    """The required CI gate must run the suite with retries off.

    Mutation check: drop ``--file-retries 0`` from run_all_gates.sh and this
    test goes red.
    """
    source = GATES_SH.read_text(encoding="utf-8")
    match = re.search(
        r"^scripts/run_tests\.sh\b(.*?)\|", source, re.DOTALL | re.MULTILINE
    )
    assert match is not None, (
        "could not find the run_tests.sh invocation in run_all_gates.sh — "
        "this test cannot vouch for the gate's retry setting"
    )
    invocation = " ".join(match.group(1).split())
    assert "--file-retries 0" in invocation, (
        "the required gate invokes run_tests.sh without `--file-retries 0`, "
        f"so it runs with the retry default and a pass-on-retry counts as "
        f"green. Invocation was: {invocation!r}"
    )


def test_file_retries_zero_lets_a_flake_fail(tmp_path: Path) -> None:
    """``--file-retries 0`` must surface a first-attempt failure as a failure.

    The probe fails on its first run and would pass on a second. With retries
    enabled the runner reports it FLAKY and exits 0; with retries disabled it
    must exit non-zero and say nothing about flakes. This is the behavioural
    half of the contract — the two tests above only check wiring.
    """
    # The probe lives in its own directory and is passed POSITIONALLY. The
    # runner's --files/--paths values are colon-separated lists, which a
    # Windows absolute path ("C:\\...") splits straight down the drive letter;
    # positional roots are not split, so this stays runnable on both POSIX and
    # native Windows.
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    marker = tmp_path / "ran-once"
    probe = probe_dir / "test_retry_disabled_probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path

            def test_fails_first_then_passes():
                marker = Path({str(marker)!r})
                if not marker.exists():
                    marker.write_text("failed once")
                    assert False, "simulated first-attempt flake"
                assert True
            """
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PY),
            str(probe_dir),
            "--file-retries",
            "0",
            "-j",
            "1",
            "-q",
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    assert proc.returncode != 0, (
        "a file that failed its first attempt was reported as a pass with "
        f"--file-retries 0:\n{proc.stdout}"
    )
    assert "FLAKY" not in proc.stdout, (
        f"the runner retried despite --file-retries 0:\n{proc.stdout}"
    )
    assert "simulated first-attempt flake" in proc.stdout, (
        f"the failing assertion was not reported:\n{proc.stdout}"
    )
    # The probe must have run exactly once: a second run would have found the
    # marker and passed, which is the retry we are asserting did not happen.
    assert marker.exists(), "probe never ran at all — the test proves nothing"
