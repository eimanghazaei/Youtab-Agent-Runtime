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
   kept the gate red on main continuously. That baseline has since been
   rebuilt from a push run, and the asymmetry it exposed is a property of
   this setting rather than of the baseline -- which is why the setting, not
   the baseline, is what this module pins.

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


def test_every_matrix_language_is_both_uploaded_and_gated(workflow: dict) -> None:
    """Every leg needs BOTH mechanisms, and neither is optional per language.

    The upload gives the native alert UI, dismissal with a reason and history.
    The comparator gives enforcement, which the upload does not: a 5000-result
    cap prioritised by severity, a results check that fails only for
    error/critical/high, pull-request gating only for alerts whose every line
    is in the diff, and extraction errors that surface for manual reading.

    The comparator was briefly limited to python and javascript-typescript on
    the argument that the other three hold few findings. That answered the
    wrong question -- the severity gap does not depend on the finding count,
    so a NEW `warning` in `actions`, `c-cpp` or `rust` would have published
    and passed. This test is why that cannot come back: a leg must be in the
    matrix, covered by the upload, AND have a committed baseline for the
    comparator to read.

    A missing baseline is specifically worth failing here rather than in CI,
    because the gate step would fail on every run with a path error -- a
    confusing symptom three steps from its cause.
    """
    import pathlib as _p

    include = workflow["jobs"]["analyze"]["strategy"]["matrix"]["include"]
    assert include, "the CodeQL matrix is empty"

    analyze = [s for s in workflow["jobs"]["analyze"]["steps"]
               if "analyze@" in str(s.get("uses", ""))]
    assert len(analyze) == 1, "expected exactly one CodeQL analyze step"
    with_block = analyze[0].get("with") or {}
    assert with_block.get("upload") == "always", (
        "every language relies on the upload for native tracking; "
        f"upload is {with_block.get('upload')!r}"
    )
    assert with_block.get("output") == "results", (
        "the comparator reads the LOCAL SARIF via `output:`; without it the "
        f"gate step has nothing to read (output is {with_block.get('output')!r})"
    )

    gate = [s for s in workflow["jobs"]["analyze"]["steps"]
            if "codeql_sarif_gate.py" in str(s.get("run", ""))]
    assert len(gate) == 1, "expected exactly one CodeQL comparator step"
    assert not gate[0].get("if"), (
        "the comparator step is conditional "
        f"({gate[0].get('if')!r}); it must run for every language in the matrix"
    )

    baselines = (_p.Path(__file__).resolve().parents[2]
                 / "scripts" / "youtab" / "codeql_baselines")
    for leg in include:
        language = leg["language"]
        path = baselines / f"{language}.json"
        assert path.is_file(), (
            f"{language} is in the CodeQL matrix but {path.name} is missing, so the "
            "comparator step would fail on every run with a path error. Generate it "
            "with scripts/youtab/regenerate_codeql_baseline.py from a complete run."
        )


def test_every_gated_language_is_known_to_the_gate(workflow: dict) -> None:
    """A matrix leg the gate has no language spec for cannot be compared.

    `codeql_sarif_gate.LANGUAGES` carries the query-pack name, the extraction
    diagnostic id and the changed-source predicate for each language. A leg
    missing from it is rejected at runtime as "missing analysis coverage
    metadata", which reads like a corrupt baseline rather than an unsupported
    language. Catch it here instead.
    """
    import sys
    import pathlib as _p

    scripts = _p.Path(__file__).resolve().parents[2] / "scripts" / "youtab"
    sys.path.insert(0, str(scripts))
    try:
        from codeql_sarif_gate import LANGUAGES
    finally:
        sys.path.remove(str(scripts))

    include = workflow["jobs"]["analyze"]["strategy"]["matrix"]["include"]
    unknown = [leg["language"] for leg in include if leg["language"] not in LANGUAGES]
    assert not unknown, (
        f"CodeQL matrix languages with no entry in codeql_sarif_gate.LANGUAGES: "
        f"{unknown}. Add the query pack, extraction diagnostic and changed-source "
        f"predicate there, or the comparator cannot read that leg's SARIF."
    )


def test_all_codeql_action_steps_pin_the_same_release(workflow: dict) -> None:
    """Mixing CodeQL Action versions across steps breaks the run.

    Since CodeQL Action 3.30.4 the non-`init` steps THROW when they load a
    configuration file generated by a different version of `init`, and `init`
    itself warns when it detects a mismatch. The repository is pinned past
    that release, so a mismatch is a hard failure rather than a warning.

    Not hypothetical: dependabot opened #74 bumping `init` alone from 3.37.9
    to 4.38.2 and leaving `analyze` behind, which would have failed every run
    at `analyze` with no SARIF produced. Dependabot updates one `uses:` line
    at a time by design, so nothing upstream couples the two steps -- this
    test is the coupling.

    Read from the PARSED step definitions, not the file text (AGENTS.md: add
    behavior tests, not source-regex snapshots), so reformatting the workflow
    cannot break it and a step added in a new job is still covered.
    """
    pins: dict[str, str] = {}
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses") or "")
            if not uses.startswith("github/codeql-action/"):
                continue
            action, _, ref = uses.partition("@")
            pins[f"{job_name}:{action}"] = ref

    assert pins, "no github/codeql-action steps found in the parsed workflow"
    assert len(set(pins.values())) == 1, (
        "github/codeql-action steps resolve to DIFFERENT refs: "
        + ", ".join(f"{where}@{ref}" for where, ref in sorted(pins.items()))
        + ". Every step must use the same release -- the non-init steps error "
        "out when they load a config generated by a different version of init, "
        "so the run fails at `analyze` with no SARIF. Move them together."
    )
    # A mutable ref defeats the point: `@v4` silently follows upstream, so a
    # future release can change what runs without any commit here.
    ref = next(iter(set(pins.values())))
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (
        f"github/codeql-action is pinned to {ref!r} rather than a full commit SHA; "
        "a floating tag can move under the workflow between runs"
    )


def _gate_languages():
    import sys
    import pathlib as _p

    scripts = _p.Path(__file__).resolve().parents[2] / "scripts" / "youtab"
    sys.path.insert(0, str(scripts))
    try:
        from codeql_sarif_gate import LANGUAGES

        return LANGUAGES, scripts
    finally:
        sys.path.remove(str(scripts))


#: Files each extractor DOES read, and files it does not. The second column
#: is the one that matters: the changed-file check demands every CHANGED file
#: the predicate calls source to appear in the run's extraction inventory, and
#: exits 2 when one does not. So a predicate that claims more than its
#: extractor reads is not a loose approximation -- it is a required check that
#: fails on a file CodeQL was never going to look at.
#:
#: That happened. The `actions` predicate matched every `.y{a,}ml` under
#: `.github/`, while the extractor covers only `.github/workflows/*.y{a,}ml`
#: and `**/action.y{a,}ml`. This repository tracks five files in the gap --
#: `.github/dependabot.yml` and four `.github/ISSUE_TEMPLATE/*.yml` -- and
#: `dependabot.yml` is edited routinely, so the gate would have blocked those
#: pull requests with "CodeQL did not extract changed source files".
#:
#: Stated as cases rather than derived from the committed inventories, because
#: those are not sound ground truth here: `python` and `javascript-typescript`
#: were captured at 48243e2a, which predates files now in the tree, and the
#: three newer baselines name a pull-request MERGE commit that is absent from
#: a fresh clone. A derived test would drift or skip rather than fail.
#:
#: Under-claiming is deliberately not asserted. It is not a hole: the gate's
#: condition is `name in baseline_extracted_paths or is_source(name)`, so a
#: file already in the inventory is checked whether the predicate claims it or
#: not. The `python` extractor reads `.yaml` as well as `.py`, for instance,
#: and that costs nothing.
CHANGED_SOURCE_CASES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "actions": (
        (".github/workflows/codeql.yml",
         ".github/workflows/ci.yaml",
         ".github/actions/retry/action.yml",
         "action.yml",
         "deep/nested/dir/action.yaml"),
        (".github/dependabot.yml",
         ".github/ISSUE_TEMPLATE/bug_report.yml",
         ".github/ISSUE_TEMPLATE/config.yml",
         # The extractor does not recurse below workflows/.
         ".github/workflows/nested/deep.yml",
         ".github/workflows/README.md",
         "docker-compose.yml",
         "web/pnpm-workspace.yaml"),
    ),
    "python": (
        ("agent/conversation_loop.py", "scripts/youtab/codeql_sarif_gate.py"),
        ("README.md", "pyproject.toml", "native/fts5_cjk/fts5_cjk.c"),
    ),
    "javascript-typescript": (
        ("web/src/app.ts", "web/src/app.tsx", "a.js", "a.mjs", "a.cjs",
         "page.html", "package.json", "tsconfig.json", "tsconfig.build.json"),
        ("agent/conversation_loop.py", "src/lib.rs", "Cargo.toml"),
    ),
    "c-cpp": (
        ("native/fts5_cjk/fts5_cjk.c", "a.cpp", "a.cc", "a.cxx", "a.h", "a.hpp"),
        ("agent/conversation_loop.py", "src/lib.rs", "Makefile"),
    ),
    "rust": (
        ("apps/bootstrap-installer/src-tauri/src/main.rs", "src/lib.rs"),
        ("Cargo.toml", "Cargo.lock", "agent/conversation_loop.py"),
    ),
}


def test_every_language_has_changed_source_cases() -> None:
    """A new language must arrive with its boundary pinned.

    Without this, adding a leg to `LANGUAGES` and the matrix would silently
    ship an untested predicate -- and an over-broad one blocks pull requests
    on files its extractor never reads.
    """
    languages, _ = _gate_languages()
    missing = sorted(set(languages) - set(CHANGED_SOURCE_CASES))
    assert not missing, (
        f"languages in codeql_sarif_gate.LANGUAGES with no entry in "
        f"CHANGED_SOURCE_CASES: {missing}. Add the files its extractor reads and, "
        f"more importantly, nearby files it does not."
    )
    stale = sorted(set(CHANGED_SOURCE_CASES) - set(languages))
    assert not stale, f"CHANGED_SOURCE_CASES names unknown languages: {stale}"


@pytest.mark.parametrize("language", sorted(CHANGED_SOURCE_CASES))
def test_changed_source_predicate_claims_exactly_its_extractors_files(
    language: str,
) -> None:
    """Each predicate must claim its own sources and nothing adjacent."""
    languages, _ = _gate_languages()
    is_source = languages[language]["extracted"]
    sources, not_sources = CHANGED_SOURCE_CASES[language]

    for name in sources:
        assert is_source(name), (
            f"{language}: {name} is a source file for this extractor but the "
            f"predicate does not claim it"
        )
    for name in not_sources:
        assert not is_source(name), (
            f"{language}: {name} is NOT read by this extractor, so claiming it "
            f"makes the changed-file check exit 2 on any pull request that edits "
            f"it -- a required check failing on a file CodeQL never looks at"
        )


