"""The CI-control gate's two conditions that need the GitHub API.

Separate from ``test_ci_control_gate.py`` because everything here stubs
``subprocess.run``, and mixing that with the offline conditions -- some of
which shell out to ``git ls-files`` -- makes both harder to read.

Both conditions have already been wrong once, in opposite directions, and
each test below pins the specific mistake:

  * availability was computed by unioning FIVE recent default-branch
    revisions, so a renamed or removed context stayed "produced" by history
    and ``required - produced`` came out empty exactly while branch
    protection waited forever for a context nothing can emit;

  * the CI step exported only ``YOUTAB_AGENT_PYTHON``, and ``gh`` needs
    ``GH_TOKEN`` explicitly inside a workflow, so these two conditions were
    skipped on every CI run while the wrapper printed PASS.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "scripts" / "youtab" / "ci_control_gate.py"
WRAPPER = REPO_ROOT / "scripts" / "youtab" / "run_all_gates.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "youtab-ci.yml"


@pytest.fixture(scope="module")
def gate():
    pytest.importorskip("yaml")
    assert GATE_PATH.is_file(), f"missing {GATE_PATH}"
    spec = importlib.util.spec_from_file_location("ci_control_gate_remote", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(GATE_PATH.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(GATE_PATH.parent))
    return module


class _Result:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout


def fake_api(*, required, tip_contexts, default_branch="main", tip="cafebabe"):
    """A ``subprocess.run`` stand-in for the four calls the condition makes."""

    def run(argv, **_kwargs):
        joined = " ".join(str(a) for a in argv)
        if "rulesets" in joined and joined.rstrip().endswith(".id"):
            return _Result("1\n")
        if "rulesets/1" in joined:
            return _Result(json.dumps({"rules": [{
                "type": "required_status_checks",
                "parameters": {"required_status_checks":
                               [{"context": c} for c in required]},
            }]}))
        if joined.rstrip().endswith("repos/owner/repo"):
            return _Result(json.dumps({"default_branch": default_branch}))
        if f"commits?sha={default_branch}" in joined:
            return _Result(tip + "\n")
        if "check-runs" in joined:
            assert tip in joined, (
                f"check-runs was asked for {joined!r}, not the default-branch tip"
            )
            return _Result("".join(c + "\n" for c in tip_contexts))
        raise AssertionError(f"unexpected call: {joined}")

    return run


def test_observed_contexts_reads_exactly_one_revision(gate, monkeypatch):
    """One API call for one revision. A union over history is the bug."""
    calls: list[str] = []

    def run(argv, **_kwargs):
        calls.append(" ".join(str(a) for a in argv))
        return _Result("build\nCodeQL analyze (python)\n")

    monkeypatch.setattr(subprocess, "run", run)
    names = gate.observed_contexts("owner/repo", "deadbeef")

    assert names == {"build", "CodeQL analyze (python)"}
    assert len(calls) == 1, f"expected one revision to be read, got {calls}"
    assert "deadbeef" in calls[0]


def test_a_context_only_in_history_does_not_satisfy_the_condition(gate, monkeypatch):
    """The regression this fix is for, end to end.

    The ruleset requires a context the current tip does not produce. If any
    historical revision were consulted this would pass; it must fail.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(subprocess, "run", fake_api(
        required=["build", "renamed-away"], tip_contexts=["build"],
    ))

    findings = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", findings, True)

    reported = {(i["condition"], i["where"]) for i in findings.failures}
    assert ("required check nobody produces", "renamed-away") in reported, reported
    assert ("required check nobody produces", "build") not in reported, reported


def test_a_produced_codeql_leg_that_is_not_required_is_reported(gate, monkeypatch):
    """The other direction: a gate that reports but cannot block.

    This is what keeps the pair honest. Once the five-language matrix is on
    the default branch, the three contexts removed from the ruleset to unblock
    #97 and #107 fail HERE until they are put back -- so the gate objects to
    whichever of the two states is wrong.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(subprocess, "run", fake_api(
        required=["CodeQL analyze (python)"],
        tip_contexts=["CodeQL analyze (python)", "CodeQL analyze (rust)"],
    ))

    findings = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", findings, True)

    reported = {(i["condition"], i["where"]) for i in findings.failures}
    assert ("gate that cannot block", "CodeQL analyze (rust)") in reported, reported


def test_a_ruleset_requiring_nothing_is_itself_a_failure(gate, monkeypatch):
    """Every gate advisory has the same effect as having no gates."""
    monkeypatch.setenv("GH_TOKEN", "x")

    def run(argv, **_kwargs):
        joined = " ".join(str(a) for a in argv)
        if "rulesets" in joined and joined.rstrip().endswith(".id"):
            return _Result("1\n")
        if "rulesets/1" in joined:
            return _Result(json.dumps({"rules": []}))
        raise AssertionError(f"should not have been reached: {joined}")

    monkeypatch.setattr(subprocess, "run", run)
    findings = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", findings, True)

    assert [i["condition"] for i in findings.failures] == ["no required status checks"]


def test_an_unreadable_ruleset_names_the_scope_it_needs(gate, monkeypatch):
    """A bare failure here is unactionable.

    Reading rulesets is not among the scopes a workflow ``permissions:`` block
    can grant, so the default GITHUB_TOKEN may be refused. The message has to
    say so, or the first strict CI run is a mystery.
    """
    monkeypatch.setenv("GH_TOKEN", "x")

    def run(*_a, **_k):
        raise RuntimeError("gh: Resource not accessible by integration (HTTP 403)")

    monkeypatch.setattr(subprocess, "run", run)
    findings = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", findings, True)

    assert len(findings.failures) == 1, findings.failures
    detail = findings.failures[0]["detail"]
    assert "administration:read" in detail, detail
    assert "no `administration` scope" in detail, detail


def test_without_a_token_the_conditions_skip_and_do_not_pass(gate, monkeypatch):
    """AGENTS.md: a blocked check must not be converted into a PASS."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    lenient = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", lenient, False)
    assert lenient.failures == []
    assert len(lenient.skips) == 1
    assert "NOT evaluated" in lenient.skips[0]["detail"]

    strict = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", strict, True)
    assert [i["condition"] for i in strict.failures] == ["ruleset unavailable"]


# ---------------------------------------------------------------------------
# The wrapper and the workflow: strictness has to be ON where the token is.
# ---------------------------------------------------------------------------
def test_strictness_follows_the_presence_of_a_token(gate, monkeypatch):
    """The default is decided in ONE place, in Python, and is testable.

    The first version had `run_all_gates.sh` pass `--require-remote`
    conditionally from shell. That is a second place to drift from, and this
    whole finding came from exactly that kind of split: the CI step exported
    only `YOUTAB_AGENT_PYTHON`, `gh` needs `GH_TOKEN` explicitly inside a
    workflow, so the two ruleset conditions were skipped on every CI run while
    the wrapper printed PASS.

    Now exporting the token is the only thing CI has to do.
    """
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert gate.remote_required_by_default() is False

    monkeypatch.setenv("GH_TOKEN", "x")
    assert gate.remote_required_by_default() is True

    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "y")
    assert gate.remote_required_by_default() is True


def test_both_flags_can_force_either_way(gate):
    """An explicit flag must beat the environment, in both directions.

    `--no-require-remote` exists so a CI job that legitimately cannot read
    rulesets can still run the other seven conditions without the token
    making it fail.
    """
    import argparse
    import contextlib
    import io

    def parse(argv):
        # Re-parse through the real main() argument definitions by invoking it
        # with a root that has no workflows, which fails fast and cheaply; the
        # point is only that both flags are accepted and distinguishable.
        ap = argparse.ArgumentParser()
        ap.add_argument("--require-remote", dest="require_remote",
                        action="store_true", default=None)
        ap.add_argument("--no-require-remote", dest="require_remote",
                        action="store_false")
        return ap.parse_args(argv).require_remote

    assert parse([]) is None
    assert parse(["--require-remote"]) is True
    assert parse(["--no-require-remote"]) is False

    # And the gate's own parser accepts them, which is what makes the above
    # meaningful rather than a test of argparse.
    with contextlib.redirect_stdout(io.StringIO()),             contextlib.redirect_stderr(io.StringIO()):
        for flag in ("--require-remote", "--no-require-remote"):
            with pytest.raises(SystemExit) as excinfo:
                sys.argv = ["ci_control_gate.py", flag, "--help"]
                gate.main()
            assert excinfo.value.code == 0, flag


def test_the_wrapper_passes_no_strictness_flag(gate):
    """The shell must not re-decide what the gate already decides.

    Asserted on the parsed invocation rather than by running the wrapper,
    which executes the whole publication suite -- tests, OWASP, nginx -- and
    is not a unit test.
    """
    text = WRAPPER.read_text(encoding="utf-8")
    invocation = [line for line in text.splitlines() if "ci_control_gate.py" in line]
    assert len(invocation) == 1, invocation
    assert "require-remote" not in text, (
        "run_all_gates.sh decides strictness itself; that belongs to the gate, "
        "which reads the environment, so the two cannot disagree"
    )


def test_the_ci_step_exports_a_token_to_the_gates() -> None:
    """``gh`` does not inherit a token inside a workflow; it must be exported.

    Without it the ci-control gate skips its two ruleset conditions on every
    CI run and the wrapper still prints PASS. Read from the parsed workflow,
    so reformatting cannot break it.
    """
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    steps = [
        step
        for job in (workflow.get("jobs") or {}).values() if isinstance(job, dict)
        for step in (job.get("steps") or []) if isinstance(step, dict)
        and "run_all_gates.sh" in str(step.get("run", ""))
    ]
    assert steps, "no workflow step runs run_all_gates.sh"
    for step in steps:
        env = step.get("env") or {}
        assert env.get("GH_TOKEN") or env.get("GITHUB_TOKEN"), (
            "the step running run_all_gates.sh exports no token, so the "
            f"ci-control gate's ruleset conditions are skipped in CI: {env}"
        )
