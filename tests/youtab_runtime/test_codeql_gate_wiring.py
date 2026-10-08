"""The CodeQL workflow must measure the same thing on every event.

``CODEQL_ACTION_DIFF_INFORMED_QUERIES: "false"`` is an *undocumented* feature
flag of the CodeQL action -- there is no ``with:`` input for it, so the
environment variable is the only lever, and nothing in GitHub's documented
surface will warn anyone who removes it.

Without it the action defaults to diff-informed analysis on ``pull_request``
and restricts dataflow query results to the lines the pull request touched.
Two consequences, both of which this repository already paid for:

1. The same tree yields a different finding count per event -- 7701 on a
   pull request versus 8971 on a push to main. The committed baseline this
   repository used to carry was captured from a ``pull_request`` run and
   recorded zero findings for all 13 interprocedural dataflow queries, which
   kept the gate red on main continuously. That baseline is gone now that
   Code Scanning does the tracking, but the measurement asymmetry it exposed
   is a property of the setting, not of the baseline.

2. A pull request can introduce a dataflow vulnerability whose source and
   sink lines it does not itself touch. Diff-informed analysis drops that
   result, the pull request goes green, and the finding surfaces on main
   after the merge -- where nothing gates it.

This test is the only thing standing between that setting and a future
cleanup commit. It asserts the wiring, not CodeQL's behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEQL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yml"

DIFF_INFORMED_ENV = "CODEQL_ACTION_DIFF_INFORMED_QUERIES"


@pytest.fixture(scope="module")
def workflow() -> dict:
    yaml = pytest.importorskip("yaml")
    assert CODEQL_WORKFLOW.is_file(), f"missing {CODEQL_WORKFLOW}"
    loaded = yaml.safe_load(CODEQL_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "codeql.yml did not parse as a mapping"
    return loaded


def _env_values(workflow: dict) -> list[str]:
    """Every value the flag is given anywhere in the workflow.

    Workflow-level, job-level and step-level ``env`` are all honoured by the
    runner, so accept the flag wherever it is declared rather than pinning it
    to one location and breaking on a legitimate move.
    """
    found: list[str] = []
    top = workflow.get("env") or {}
    if DIFF_INFORMED_ENV in top:
        found.append(str(top[DIFF_INFORMED_ENV]))
    for job in (workflow.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        job_env = job.get("env") or {}
        if DIFF_INFORMED_ENV in job_env:
            found.append(str(job_env[DIFF_INFORMED_ENV]))
        for step in job.get("steps") or []:
            if isinstance(step, dict) and DIFF_INFORMED_ENV in (step.get("env") or {}):
                found.append(str(step["env"][DIFF_INFORMED_ENV]))
    return found


def test_diff_informed_queries_is_disabled(workflow: dict) -> None:
    """Mutation check: delete the env entry and this goes red."""
    values = _env_values(workflow)
    assert values, (
        f"{DIFF_INFORMED_ENV} is not set anywhere in codeql.yml. Without it the "
        "CodeQL action defaults to diff-informed analysis on pull requests and "
        "clips dataflow findings to the diff, so pull-request and push runs "
        "measure different things and the no-new-findings baseline stops "
        "meaning anything. See this module's docstring."
    )
    # The action lower-cases the value and only treats the exact string
    # "false" as a disable, so "0"/"no"/"False " would silently re-enable it.
    assert all(value.strip().lower() == "false" for value in values), (
        f"{DIFF_INFORMED_ENV} is set to {values!r}; the action disables the "
        'feature only on the literal string "false" (it lower-cases the value '
        'and compares to "false"), so any other value leaves diff-informed '
        "analysis ON."
    )


def test_codeql_runs_on_push_to_main_and_pull_request(workflow: dict) -> None:
    """Both events must be analysed, or there is nothing to compare.

    ``pull_request`` alone never verifies main itself; ``push`` alone never
    gates a change before it lands.
    """
    triggers = workflow.get(True) or workflow.get("on") or {}
    assert "pull_request" in triggers, "codeql.yml no longer runs on pull_request"
    assert "push" in triggers, "codeql.yml no longer runs on push"
    branches = (triggers.get("push") or {}).get("branches") or []
    assert "main" in branches, f"push trigger does not cover main: {branches!r}"
