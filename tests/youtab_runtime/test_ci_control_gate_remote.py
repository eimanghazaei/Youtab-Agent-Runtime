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


def ruleset_body(required, *, enforcement="active", include=None, name="rs"):
    """A branch ruleset as the API actually returns one.

    `conditions.ref_name` is the real shape -- this repository's own reads
    `{"include": ["refs/heads/main"], "exclude": []}` -- because the gate now
    filters on it, and a stub without it would test nothing.
    """
    return {
        "name": name,
        "enforcement": enforcement,
        "conditions": {"ref_name": {
            "include": ["refs/heads/main"] if include is None else include,
            "exclude": [],
        }},
        "rules": [{
            "type": "required_status_checks",
            "parameters": {"required_status_checks":
                           [{"context": c} for c in required]},
        }],
    }


def fake_api(*, required, tip_contexts, default_branch="main", tip="cafebabe",
             rulesets=None):
    """A ``subprocess.run`` stand-in for the calls the condition makes.

    `rulesets` maps id -> body, so a test can supply a disabled one or one
    scoped to another branch.
    """
    bodies = rulesets if rulesets is not None else {"1": ruleset_body(required)}

    def run(argv, **_kwargs):
        joined = " ".join(str(a) for a in argv)
        if "rulesets" in joined and joined.rstrip().endswith(".id"):
            return _Result("".join(i + "\n" for i in bodies))
        for ruleset_id, body in bodies.items():
            if f"rulesets/{ruleset_id}" in joined:
                return _Result(json.dumps(body))
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


def test_a_pull_request_is_judged_against_its_own_head_too(
    gate, monkeypatch, tmp_path
):
    """Otherwise the change that INTRODUCES a required context deadlocks.

    This audit runs inside `python-security` on `pull_request`. When a pull
    request adds the `actions`, `c-cpp` and `rust` CodeQL legs and they are
    made required, the default-branch tip cannot have reported them yet, so
    `required - produced` fails forever on the only pull request that could
    fix it.

    Not the history union that was removed earlier: that unioned five PAST
    revisions of main, so a context renamed away still looked produced. These
    two are both CURRENT states -- what main does now, and what the change
    under review proposes.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"head": {"sha": "feedface"}}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))

    def run(argv, **_kwargs):
        joined = " ".join(str(a) for a in argv)
        if "rulesets" in joined and joined.rstrip().endswith(".id"):
            return _Result("1\n")
        if "rulesets/1" in joined:
            return _Result(json.dumps(ruleset_body(["build", "CodeQL analyze (rust)"])))
        if joined.rstrip().endswith("repos/owner/repo"):
            return _Result(json.dumps({"default_branch": "main"}))
        if "commits?sha=main" in joined:
            return _Result("cafebabe\n")
        if "commits/cafebabe/check-runs" in joined:
            return _Result("build\n")              # main does not have it yet
        if "commits/feedface/check-runs" in joined:
            return _Result("build\nCodeQL analyze (rust)\n")
        raise AssertionError("unexpected call: " + joined)

    monkeypatch.setattr(subprocess, "run", run)
    findings = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", findings, True)

    assert findings.failures == [], findings.failures


def test_outside_a_pull_request_only_the_tip_is_consulted(gate, monkeypatch):
    """A context neither main nor a pull request produces is still reported."""
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_api(
        required=["build", "nobody-makes-this"], tip_contexts=["build"],
    ))

    findings = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", findings, True)

    assert [(i["condition"], i["where"]) for i in findings.failures] == [
        ("required check nobody produces", "nobody-makes-this")
    ], findings.failures


def test_a_ruleset_requiring_nothing_is_itself_a_failure(gate, monkeypatch):
    """Every gate advisory has the same effect as having no gates."""
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(subprocess, "run", fake_api(
        required=[], tip_contexts=["build"],
        rulesets={"1": ruleset_body([])},
    ))
    findings = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", findings, True)

    assert [i["condition"] for i in findings.failures] == ["no required status checks"]


@pytest.mark.parametrize("body,why", [
    (lambda: ruleset_body(["release-only"], enforcement="disabled"),
     "a DISABLED ruleset enforces nothing"),
    (lambda: ruleset_body(["release-only"], include=["refs/heads/release/*"]),
     "a ruleset scoped to release/* does not protect main"),
])
def test_a_ruleset_that_does_not_protect_the_default_branch_is_ignored(
    gate, monkeypatch, body, why
):
    """Unioning every branch ruleset produced a FALSE lockout report.

    Required contexts are compared against the default branch's check runs,
    so a context required only on `release/*`, or by a disabled ruleset, read
    as "required but nobody produces it" -- failing the required
    python-security job on every pull request. A false lockout report from the
    lockout detector.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(subprocess, "run", fake_api(
        required=[], tip_contexts=["build"],
        rulesets={"1": ruleset_body(["build"]), "2": body()},
    ))

    findings = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, "owner/repo", findings, True)

    assert findings.failures == [], f"{why}: {findings.failures}"


def test_ruleset_protects_handles_the_special_ref_tokens(gate):
    """`~ALL` and `~DEFAULT_BRANCH` are not fnmatch patterns."""
    assert gate.ruleset_protects(ruleset_body([], include=["~ALL"]), "main")
    assert gate.ruleset_protects(
        ruleset_body([], include=["~DEFAULT_BRANCH"]), "main")
    assert gate.ruleset_protects(
        ruleset_body([], include=["refs/heads/ma*"]), "main")
    assert not gate.ruleset_protects(ruleset_body([], include=[]), "main")
    assert not gate.ruleset_protects(
        ruleset_body([], include=["refs/heads/main"], enforcement="evaluate"), "main")
    # exclude wins over include
    excluded = ruleset_body([], include=["~ALL"])
    excluded["conditions"]["ref_name"]["exclude"] = ["refs/heads/main"]
    assert not gate.ruleset_protects(excluded, "main")


def test_strict_mode_without_a_repository_fails(gate, monkeypatch):
    """Strict mode exists so a condition cannot quietly stop running.

    With a token exported but no `--repo` and no `GITHUB_REPOSITORY`, the
    earlier code recorded a skip and `main()` printed "PASS with 1 SKIPPED" --
    making the explicitly strict audit non-enforcing on an authenticated
    local or publication run.
    """
    monkeypatch.setenv("GH_TOKEN", "x")

    strict = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, None, strict, True)
    assert [i["condition"] for i in strict.failures] == ["repository unknown"]

    lenient = gate.Findings()
    gate.check_required_checks(REPO_ROOT, {}, None, lenient, False)
    assert lenient.failures == []
    assert len(lenient.skips) == 1


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


def test_the_default_is_decided_by_the_gate_not_by_its_caller(gate, monkeypatch):
    """The contract the wrapper relies on, asserted behaviourally.

    The previous version of this test read `run_all_gates.sh` as raw text and
    asserted `"require-remote" not in text`. That is a source-regex inventory
    assertion, which AGENTS.md:31 prohibits -- a comment mentioning the flag,
    or a line wrap, would fail it without any behaviour changing. It was the
    second time I wrote one.

    What actually matters is that the gate resolves strictness itself, so the
    wrapper does not have to and cannot disagree with it. That is a property
    of `main()`'s argument handling plus `remote_required_by_default()`, and
    both are observable without reading a file.
    """
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert gate.remote_required_by_default() is False

    monkeypatch.setenv("GH_TOKEN", "x")
    assert gate.remote_required_by_default() is True

    # And an explicit flag must still win over the environment, in both
    # directions, so a caller that needs to override can -- which is the
    # reason the wrapper does not need to decide.
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--require-remote", dest="require_remote",
                        action="store_true", default=None)
    parser.add_argument("--no-require-remote", dest="require_remote",
                        action="store_false")
    assert parser.parse_args([]).require_remote is None
    assert parser.parse_args(["--require-remote"]).require_remote is True
    assert parser.parse_args(["--no-require-remote"]).require_remote is False


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
