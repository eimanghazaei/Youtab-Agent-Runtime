import json

import pytest

from scripts.youtab.codeql_sarif_gate import finding_keys, inspect


def sarif(results):
    return {"version": "2.1.0", "runs": [{
        "tool": {"driver": {"name": "CodeQL"}, "extensions": [{
            "name": "codeql/python-queries", "rules": [{"id": "py/sql-injection"}],
        }]},
        "invocations": [{"executionSuccessful": True, "toolExecutionNotifications": [{
            "descriptor": {"id": "py/diagnostics/successfully-extracted-files"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": "app.py"}}}],
        }]}],
        "automationDetails": {"id": "/language:python/"},
        "artifacts": [{"location": {"uri": "app.py"}}],
        "properties": {"codeqlConfigSummary": {"queries": [{
            "type": "builtinSuite", "uses": "security-and-quality",
        }]}},
        "results": results,
    }]}


def finding(uri="app.py", line_hash="abc:1"):
    return {
        "ruleId": "py/sql-injection",
        "message": {"text": "Untrusted query"},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": uri},
            "region": {"startLine": 9},
        }}],
        "partialFingerprints": {
            "primaryLocationLineHash": line_hash,
            "primaryLocationStartColumnFingerprint": "4",
        },
    }


def _accumulate(findings):
    """Flatten (result, count) pairs into the baseline's key -> count map."""
    out = {}
    for result, count in findings:
        for key in finding_keys(result):
            out[key] = out.get(key, 0) + count
    return out


def write_baseline(path, findings):
    # Totals come from the EXPANDED key map, not the input pairs: a coalesced
    # graph result contributes one key per message component, so the two
    # differ and the gate validates `sum(findings.values()) == open_total`.
    accumulated = _accumulate(findings)
    total = sum(accumulated.values())
    path.write_text(json.dumps({
        "schema": 1,
        "source_run": "https://example.test/run/1",
        "open_total": total,
        "open_security": 0,
        "open_quality": total,
        "language": "python",
        "artifact_count": 1,
        "query_count": 1,
        "extracted_count": 1,
        "extracted_paths": ["app.py"],
        "required_rules": ["py/sql-injection"],
        # One result can yield several keys (CodeQL coalesces graph
        # diagnostics sharing a location), so accumulate rather than assign.
        "findings": accumulated,
    }), encoding="utf-8")


def test_missing_sarif_fails_closed(tmp_path):
    assert inspect(tmp_path) == 2


def test_clean_sarif_passes(tmp_path):
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([])))
    assert inspect(tmp_path) == 0


def test_malformed_or_non_codeql_sarif_fails_closed(tmp_path):
    path = tmp_path / "python.sarif"
    path.write_text(json.dumps({"runs": [{"results": []}]}))
    assert inspect(tmp_path) == 2
    path.write_text(json.dumps({"version": "2.1.0", "runs": [{
        "tool": {"driver": {"name": "other"}}, "results": [],
    }]}))
    assert inspect(tmp_path) == 2


def test_finding_fails_instead_of_reporting_green(tmp_path, capsys):
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([finding()])))
    assert inspect(tmp_path) == 1
    assert "app.py:9: py/sql-injection" in capsys.readouterr().err


def test_existing_finding_passes_with_open_debt_report(tmp_path, capsys):
    existing = finding()
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([existing])))
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(existing, 1)])
    assert inspect(tmp_path, baseline) == 0
    assert "1 open, 0 new" in capsys.readouterr().out


def test_new_path_or_extra_occurrence_fails_against_baseline(tmp_path):
    existing = finding()
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(existing, 1)])
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([existing, finding("other.py")])))
    assert inspect(tmp_path, baseline) == 1
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([existing, existing])))
    assert inspect(tmp_path, baseline) == 1


def test_missing_identity_or_baseline_fails_closed(tmp_path):
    existing = finding()
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(existing, 1)])
    without_fingerprint = {key: value for key, value in existing.items() if key != "partialFingerprints"}
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([without_fingerprint])))
    assert inspect(tmp_path, baseline) == 2
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([existing])))
    baseline.write_text("{}")
    assert inspect(tmp_path, baseline) == 2


def test_empty_or_partial_analysis_cannot_clear_nonempty_baseline(tmp_path):
    existing = finding()
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(existing, 1)])
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([])))
    assert inspect(tmp_path, baseline) == 2
    partial = sarif([existing])
    partial["runs"][0]["artifacts"] = []
    (tmp_path / "python.sarif").write_text(json.dumps(partial))
    assert inspect(tmp_path, baseline) == 2
    partial = sarif([existing])
    partial["runs"][0]["tool"]["extensions"] = []
    (tmp_path / "python.sarif").write_text(json.dumps(partial))
    assert inspect(tmp_path, baseline) == 2
    partial = sarif([existing])
    partial["runs"][0]["automationDetails"]["id"] = "/language:javascript-typescript/"
    (tmp_path / "python.sarif").write_text(json.dumps(partial))
    assert inspect(tmp_path, baseline) == 2


def test_changed_source_and_query_identity_must_be_analyzed(tmp_path, monkeypatch):
    existing = finding()
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(existing, 1)])
    changed = tmp_path / "changed-files.bin"
    (tmp_path / "new.py").write_text("x = 1\n")
    changed.write_bytes(b"new.py\0")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([existing])))
    assert inspect(tmp_path, baseline, changed) == 2
    changed.write_bytes(b"app.py\0")
    (tmp_path / "app.py").write_text("x = 1\n")
    assert inspect(tmp_path, baseline, changed) == 0
    partial = sarif([existing])
    partial["runs"][0]["tool"]["extensions"][0]["rules"] = [{"id": "py/other"}]
    (tmp_path / "python.sarif").write_text(json.dumps(partial))
    assert inspect(tmp_path, baseline, changed) == 2
    partial = sarif([existing])
    partial["runs"][0]["properties"]["codeqlConfigSummary"]["queries"] = []
    (tmp_path / "python.sarif").write_text(json.dumps(partial))
    assert inspect(tmp_path, baseline, changed) == 2


def test_changed_html_or_yaml_requires_javascript_extraction(tmp_path, monkeypatch):
    existing = finding("viewer.html")
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(existing, 1)])
    document = json.loads(baseline.read_text())
    document["language"] = "javascript-typescript"
    document["extracted_paths"] = ["viewer.html"]
    baseline.write_text(json.dumps(document))
    run = sarif([existing])["runs"][0]
    run["automationDetails"]["id"] = "/language:javascript-typescript/"
    run["tool"]["extensions"][0]["name"] = "codeql/javascript-queries"
    run["invocations"][0]["toolExecutionNotifications"][0]["descriptor"]["id"] = (
        "js/diagnostics/successfully-extracted-files")
    run["invocations"][0]["toolExecutionNotifications"][0]["locations"][0]["physicalLocation"][
        "artifactLocation"]["uri"] = "other.html"
    (tmp_path / "javascript.sarif").write_text(json.dumps({"version": "2.1.0", "runs": [run]}))
    (tmp_path / "viewer.html").write_text("<script></script>")
    (tmp_path / "workflow.yml").write_text("on: push")
    changed = tmp_path / "changed-files.bin"
    changed.write_bytes(b"viewer.html\0")
    monkeypatch.chdir(tmp_path)
    assert inspect(tmp_path, baseline, changed) == 2
    run["invocations"][0]["toolExecutionNotifications"][0]["locations"][0]["physicalLocation"][
        "artifactLocation"]["uri"] = "viewer.html"
    (tmp_path / "javascript.sarif").write_text(json.dumps({"version": "2.1.0", "runs": [run]}))
    changed.write_bytes(b"workflow.yml\0")
    assert inspect(tmp_path, baseline, changed) == 2


# ---------------------------------------------------------------------------
# Graph-scoped rules (py/cyclic-import, py/import-and-import-from).
#
# These report one result per import statement participating in a cycle, so an
# edit to ANY module in the cycle re-reports every member at a shifted line and
# changes its primaryLocationLineHash. Compared by exact key, that reads as a
# brand-new finding for a defect that did not change -- which is what kept this
# gate red on every pull request and on main. They are therefore compared as a
# per-(rule, file) count ceiling instead.
# ---------------------------------------------------------------------------

NEWLINE = chr(10)

CYCLE_RULE = "py/cyclic-import"


def cycle_finding(uri="app.py", line_hash="cyc:1", module="pkg.config"):
    # The MODULE NAME is the identity now, not the line hash -- `finding_key`
    # keys graph-scoped rules on the message. `(1)` is a relatedLocations
    # index and is stripped, so it is included here to pin that stripping.
    return {
        "ruleId": CYCLE_RULE,
        "message": {"text": f"Import of module [{module}](1) begins an import cycle."},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": uri},
            "region": {"startLine": 11},
        }}],
        "partialFingerprints": {
            "primaryLocationLineHash": line_hash,
            "primaryLocationStartColumnFingerprint": "0",
        },
    }


def cycle_sarif(results):
    document = sarif(results)
    document["runs"][0]["tool"]["extensions"][0]["rules"].append({"id": CYCLE_RULE})
    return document


def test_graph_scoped_line_hash_churn_is_not_a_new_finding(tmp_path):
    """The regression this gate shipped with: same defect, shifted line.

    Two cycle findings on `pkg.config`, re-reported at completely different
    line hashes -- what happens when an unrelated module in the same cycle is
    edited. The module name is unchanged, so the key is unchanged and this
    must pass. Keyed on the line hash it reported "2 new, 2 absent" and failed.
    """
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(cycle_finding(line_hash="was:1"), 2)])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(line_hash="now:1"),
        cycle_finding(line_hash="now:2"),
    ])))
    assert inspect(tmp_path, baseline) == 0


def test_graph_scoped_replacement_at_an_unchanged_count_is_caught(tmp_path):
    """A swap that leaves the per-file COUNT identical must still fail.

    One cycle on `pkg.config` is fixed and a new one on `pkg.auth` appears in
    the same file. The count is unchanged, so a per-(rule, file) count ceiling
    saw nothing -- both subtractions were empty and the gate exited 0, which
    silently broke the no-new-findings guarantee during ordinary cycle
    refactors. Keying on the module name makes it one `new` and one `absent`.

    Mutation check: aggregate graph rules to a per-(rule, file) count and this
    goes green when it must not -- verified directly, the count ceiling sees
    1 - 1 = 0 in both directions here. (Keying on the line hash would also
    catch THIS case; what it cannot survive is the churn in the test above.
    The two tests pin the two halves.)
    """
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(cycle_finding(module="pkg.config"), 1)])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(module="pkg.auth", line_hash="now:1"),
    ])))
    assert inspect(tmp_path, baseline) == 1


def test_graph_scoped_count_increase_in_a_file_still_fails(tmp_path):
    """Churn immunity must not cost us regression detection."""
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(cycle_finding(line_hash="was:1"), 1)])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(line_hash="now:1"),
        cycle_finding(line_hash="now:2"),
    ] * 1 + [cycle_finding(module="pkg.extra")])))
    assert inspect(tmp_path, baseline) == 1


def test_graph_scoped_finding_in_an_unbaselined_file_fails(tmp_path):
    """A cycle in a file the baseline never recorded is a new cycle."""
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(cycle_finding(uri="app.py", line_hash="was:1"), 1)])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(uri="app.py", line_hash="was:1"),
        cycle_finding(uri="fresh.py", line_hash="new:1"),
    ])))
    assert inspect(tmp_path, baseline) == 1


def test_graph_scoped_decrease_passes_and_does_not_mask_exact_rules(tmp_path):
    """Fixing cycles passes; an unrelated exact-key finding still fails.

    Mutation check: route py/sql-injection through the count comparison and the
    second half of this test goes green when it must not.
    """
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [
        (cycle_finding(module="pkg.config"), 1),
        (cycle_finding(module="pkg.other"), 1),
    ])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(module="pkg.config", line_hash="now:1"),
    ])))
    assert inspect(tmp_path, baseline) == 0

    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(module="pkg.config", line_hash="now:1"),
        finding(line_hash="brand-new:1"),
    ])))
    assert inspect(tmp_path, baseline) == 1


# ---------------------------------------------------------------------------
# regenerate_codeql_baseline.py round-trip.
#
# The gate validates the baseline strictly (schema, provenance, totals, query
# identities, extracted-source inventory). A regenerator that produces a
# baseline the gate then rejects is worse than none, so assert the contract
# directly rather than trusting the two files to agree by inspection.
# ---------------------------------------------------------------------------


def _regenerate(sarif_dir, language, out):
    import importlib.util

    path = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "scripts" / "youtab" / "regenerate_codeql_baseline.py"
    )
    spec = importlib.util.spec_from_file_location("regen_baseline", path)
    module = importlib.util.module_from_spec(spec)
    import sys as _sys

    _sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
        baseline = module.build(
            sarif_dir, language,
            "https://example.test/run/7", "0" * 40,
        )
    finally:
        _sys.path.remove(str(path.parent))
    out.write_text(json.dumps(baseline), encoding="utf-8")
    return baseline


def test_regenerated_baseline_is_accepted_by_the_gate(tmp_path):
    """Regenerate from a SARIF, then gate that same SARIF: must pass."""
    results = [
        finding(line_hash="a:1"),
        cycle_finding(line_hash="c:1"),
        cycle_finding(line_hash="c:2"),
    ]
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif(results)))

    baseline_path = tmp_path / "regenerated.json"
    baseline = _regenerate(tmp_path, "python", baseline_path)

    assert baseline["open_total"] == 3
    assert baseline["open_security"] + baseline["open_quality"] == baseline["open_total"]
    assert baseline["source_head"] == "0" * 40
    # The whole point: the gate accepts what the regenerator wrote.
    assert inspect(tmp_path, baseline_path) == 0


def test_regenerated_baseline_still_catches_a_later_regression(tmp_path):
    """A refreshed baseline must not be a blank cheque."""
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(line_hash="c:1"),
    ])))
    baseline_path = tmp_path / "regenerated.json"
    _regenerate(tmp_path, "python", baseline_path)
    assert inspect(tmp_path, baseline_path) == 0

    # Same file gains a cycle, and an unrelated exact-key finding appears.
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(line_hash="c:1"),
        cycle_finding(line_hash="c:2"),
        finding(line_hash="a:1"),
    ])))
    assert inspect(tmp_path, baseline_path) == 1


# ---------------------------------------------------------------------------
# Partially-degraded baseline detection.
#
# The committed baseline records 0 findings for 13 interprocedural dataflow
# queries that are in its own `required_rules` -- py/path-injection,
# py/clear-text-logging-sensitive-data, py/partial-ssrf and ten others --
# while an identical query configuration on the same tree reports 1270 of
# them. Key-by-key that reads as 1270 new findings, and "just refresh the
# baseline" would accept every one as reviewed debt. The gate must refuse to
# judge instead.
# ---------------------------------------------------------------------------


def dataflow_finding(rule, uri="app.py", line_hash="df:1"):
    return {
        "ruleId": rule,
        "message": {"text": "Part of the URL of this request depends on a user-provided value."},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": uri},
            "region": {"startLine": 42},
        }}],
        "partialFingerprints": {
            "primaryLocationLineHash": line_hash,
            "primaryLocationStartColumnFingerprint": "7",
        },
    }


DATAFLOW_RULES = ("py/path-injection", "py/log-injection", "py/partial-ssrf")


def dataflow_sarif(results):
    document = sarif(results)
    for rule in DATAFLOW_RULES:
        document["runs"][0]["tool"]["extensions"][0]["rules"].append({"id": rule})
    return document


def _baseline_requiring(path, findings, required):
    write_baseline(path, findings)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["required_rules"] = list(required)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_baseline_that_under_records_dataflow_queries_is_refused(tmp_path):
    """Three required queries with nothing baselined and lots now => exit 2.

    Not 1 (regression) and not 0 (clean): the baseline is not a valid
    comparison basis, so the gate must decline to judge rather than invite a
    refresh that launders the findings into accepted debt.
    """
    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(finding(line_hash="known:1"), 1)],
        ["py/sql-injection", *DATAFLOW_RULES],
    )
    results = [finding(line_hash="known:1")]
    for rule in DATAFLOW_RULES:
        results += [dataflow_finding(rule, line_hash=f"{rule}:{i}") for i in range(40)]
    (tmp_path / "python.sarif").write_text(json.dumps(dataflow_sarif(results)))

    assert inspect(tmp_path, baseline) == 2


def test_a_few_genuinely_new_dataflow_findings_are_still_just_new(tmp_path):
    """The guard must not swallow ordinary regressions.

    Same shape, but a handful of findings rather than hundreds: that is code
    newly tripping a query, which is a normal failure (exit 1), not a broken
    baseline (exit 2).
    """
    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(finding(line_hash="known:1"), 1)],
        ["py/sql-injection", *DATAFLOW_RULES],
    )
    (tmp_path / "python.sarif").write_text(json.dumps(dataflow_sarif([
        finding(line_hash="known:1"),
        dataflow_finding("py/partial-ssrf", line_hash="ssrf:1"),
    ])))

    assert inspect(tmp_path, baseline) == 1


def test_bulk_new_findings_on_a_populated_rule_stay_in_the_regression_path(tmp_path, capsys):
    """A real regression must not come out as "the baseline is incomplete".

    This is the hole the count-delta version of the coverage check had. It
    treated ANY positive per-rule delta as evidence that the baseline had been
    captured under diff-informed analysis, and the proportional floor is low
    for a small baseline: against the real 110-finding javascript baseline,
    13 new `js/log-injection` results clear max(10, 11) and came out as exit 2
    with a message naming baseline regeneration as a legitimate response.
    Regenerating would have written all 13 in as reviewed debt.

    So the shape here is a rule the baseline ALREADY populates, gaining a
    bulk of findings. There is no coverage question -- the query plainly ran
    on both sides -- so it has to be exit 1, with the locations printed.
    """
    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(finding(line_hash=f"other:{i}"), 1) for i in range(90)]
        + [(dataflow_finding("py/log-injection", line_hash=f"log:{i}"), 1)
           for i in range(20)],
        ["py/sql-injection", *DATAFLOW_RULES],
    )
    results = (
        [finding(line_hash=f"other:{i}") for i in range(90)]
        + [dataflow_finding("py/log-injection", line_hash=f"log:{i}") for i in range(33)]
    )
    (tmp_path / "python.sarif").write_text(json.dumps(dataflow_sarif(results)))

    assert inspect(tmp_path, baseline) == 1
    captured = capsys.readouterr()
    assert "13 new" in captured.out, captured.out
    # The misclassification was not just the exit code -- it was the advice.
    assert "records NOTHING" not in captured.err, captured.err
    assert "reviewed debt" not in captured.err, captured.err


def test_a_query_absent_from_the_baseline_entirely_is_still_a_coverage_breach(tmp_path):
    """The fix must not cost the original detection.

    Same volume as the test above, but on a required query the baseline holds
    at ZERO rather than at 20. That is the structural fingerprint of a
    diff-informed baseline -- the query never ran on that side -- and it must
    still refuse to judge (exit 2).
    """
    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(finding(line_hash=f"other:{i}"), 1) for i in range(90)],
        ["py/sql-injection", *DATAFLOW_RULES],
    )
    results = (
        [finding(line_hash=f"other:{i}") for i in range(90)]
        + [dataflow_finding("py/log-injection", line_hash=f"log:{i}") for i in range(13)]
    )
    (tmp_path / "python.sarif").write_text(json.dumps(dataflow_sarif(results)))

    assert inspect(tmp_path, baseline) == 2


def _wide_sarif(*, extracted: int, artifacts: int):
    """A SARIF whose extraction inventory and `artifacts` index differ in size.

    They are independent arrays in the format, and CodeQL has populated them
    with different sets in different releases, which is the whole point of the
    two tests below.
    """
    document = sarif([finding(line_hash="known:1")])
    run = document["runs"][0]
    run["invocations"][0]["toolExecutionNotifications"] = [
        {"descriptor": {"id": "py/diagnostics/successfully-extracted-files"},
         "locations": [{"physicalLocation": {"artifactLocation": {"uri": f"src/f{i}.py"}}}]}
        for i in range(extracted)
    ]
    run["artifacts"] = [{"location": {"uri": f"src/f{i}.py"}} for i in range(artifacts)]
    return document


def _baseline_with_coverage(path, *, extracted: int, artifact_count: int):
    write_baseline(path, [(finding(line_hash="known:1"), 1)])
    document = json.loads(path.read_text(encoding="utf-8"))
    document["extracted_count"] = extracted
    document["extracted_paths"] = [f"src/f{i}.py" for i in range(extracted)]
    document["artifact_count"] = artifact_count
    path.write_text(json.dumps(document), encoding="utf-8")


def test_a_shrinking_artifacts_index_alone_does_not_fail_the_gate(tmp_path):
    """Regression: a CodeQL bundle bump turned the gate red on a clean tree.

    Real numbers from the javascript-typescript leg. The committed baseline
    was captured on bundle 2.2x, which listed 6735 entries in `artifacts`
    while extracting 2932 source files. Bundle 2.27.1 lists exactly the
    extracted set, so `artifacts` reported 2932 -- a 56% fall that failed the
    90% floor, on a run whose extraction inventory was identical set-for-set
    and whose findings were the same 110.

    `artifacts` membership is a SARIF serialisation detail of the release, so
    it cannot carry a fail-closed gate. The extraction inventory is intact
    here and that is what must decide.
    """
    baseline = tmp_path / "baseline.json"
    _baseline_with_coverage(baseline, extracted=2932, artifact_count=6735)
    (tmp_path / "python.sarif").write_text(
        json.dumps(_wide_sarif(extracted=2932, artifacts=2932))
    )

    assert inspect(tmp_path, baseline) == 0


def test_losing_extracted_source_files_still_fails_closed(tmp_path, capsys):
    """The enforcing half must be untouched by the fix above.

    Same baseline, but the run extracted 2000 of the 2932 files (68%). That is
    a partial analysis whatever the invocation claims, and it must refuse to
    judge -- including when `artifacts` looks healthy, which is the shape that
    would fool a check reading the index instead.
    """
    baseline = tmp_path / "baseline.json"
    _baseline_with_coverage(baseline, extracted=2932, artifact_count=6735)
    (tmp_path / "python.sarif").write_text(
        json.dumps(_wide_sarif(extracted=2000, artifacts=6735))
    )

    assert inspect(tmp_path, baseline) == 2
    message = capsys.readouterr().err
    assert "source extraction coverage dropped" in message, message
    # The numbers have to be in the message: "dropped unexpectedly" with no
    # figures sent the reader to the baseline rather than to the run.
    assert "2000" in message and "2932" in message, message


def test_a_coverage_error_still_reports_a_concurrent_new_finding(tmp_path, capsys):
    """A cleanup and a regression in the same scan must not hide the regression.

    The coverage check aborts before `new = current - baseline` is computed,
    so when a legitimate cleanup removes 10% or more of the baseline AND the
    same scan introduces a new finding, the reader saw only "THIS RUN
    under-reports" plus an invitation to rebuild the baseline. Rebuilding from
    that artifact writes the new finding in as reviewed debt.

    Here 90 of 100 `py/path-injection` findings are remediated (a real
    cleanup, over the proportional floor) while one new `py/sql-injection`
    finding appears. The gate must still refuse (exit 2, the coverage question
    is genuinely unanswerable), but it must print the new finding and say
    plainly that regeneration is not the way out.
    """
    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(finding(line_hash="known:1"), 1)]
        + [(dataflow_finding("py/path-injection", line_hash=f"pi:{i}"), 1)
           for i in range(100)],
        ["py/sql-injection", *DATAFLOW_RULES],
    )
    results = (
        [finding(line_hash="known:1"),
         finding(uri="fresh.py", line_hash="brandnew:1")]          # the regression
        + [dataflow_finding("py/path-injection", line_hash=f"pi:{i}")
           for i in range(10)]                                      # 90 remediated
    )
    (tmp_path / "python.sarif").write_text(json.dumps(dataflow_sarif(results)))

    assert inspect(tmp_path, baseline) == 2
    message = capsys.readouterr().err
    assert "THIS RUN under-reports" in message, message
    # The new finding's location, not just a count.
    assert "fresh.py" in message, message
    assert "NOT BY REGENERATING" in message, message
    assert "1 NEW finding" in message, message
    # And the innocent remedy must be gone -- it is what caused the laundering.
    assert "remediated in full" not in message, message


def test_open_security_debt_is_disclosed_on_a_passing_run(tmp_path, capsys):
    """Green must never read as "clean".

    The baseline this gate passes against records 1373 accepted security
    findings. A green tick that does not say so invites exactly the
    misreading that let 1272 of them in. Mutation check: delete the
    OPEN SECURITY DEBT print and this goes red.
    """
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(finding(line_hash="known:1"), 1)])
    document = json.loads(baseline.read_text(encoding="utf-8"))
    document["open_security"] = 1
    document["open_quality"] = 0
    baseline.write_text(json.dumps(document), encoding="utf-8")

    (tmp_path / "python.sarif").write_text(json.dumps(sarif([
        finding(line_hash="known:1"),
    ])))

    assert inspect(tmp_path, baseline) == 0
    out = capsys.readouterr().out
    assert "OPEN SECURITY DEBT: 1 security findings" in out
    assert "only ever go down" in out


def test_a_run_that_loses_required_queries_is_refused(tmp_path):
    """The reverse of the baseline-mismatch case, and just as dangerous.

    A run that loses its interprocedural output still reports every syntactic
    finding, so `current` is nonempty and the all-zero guard passes. Those
    dataflow findings land in `absent`, which is NOT enforcing -- so a partial
    scanner failure would score a green security gate. This must fail closed.

    Mutation check: delete the `missing_in_current` branch and this goes green.
    """
    baseline = tmp_path / "baseline.json"
    findings = [(finding(line_hash="known:1"), 1)]
    for rule in DATAFLOW_RULES:
        findings += [(dataflow_finding(rule, line_hash=f"{rule}:{i}"), 1) for i in range(40)]
    _baseline_requiring(baseline, findings, ["py/sql-injection", *DATAFLOW_RULES])

    # Current run: the syntactic finding survives, all dataflow output is gone.
    (tmp_path / "python.sarif").write_text(json.dumps(dataflow_sarif([
        finding(line_hash="known:1"),
    ])))

    assert inspect(tmp_path, baseline) == 2


def test_losing_a_handful_of_dataflow_findings_is_not_a_coverage_failure(tmp_path):
    """Fixing findings must stay an ordinary pass, not a coverage alarm.

    Same shape as above but the queries still report -- only some findings are
    gone. That is remediation, and `absent` is the right place for it.
    """
    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(finding(line_hash="known:1"), 1),
         (dataflow_finding("py/partial-ssrf", line_hash="ssrf:1"), 1),
         (dataflow_finding("py/path-injection", line_hash="pi:1"), 1),
         (dataflow_finding("py/log-injection", line_hash="li:1"), 1)],
        ["py/sql-injection", *DATAFLOW_RULES],
    )
    (tmp_path / "python.sarif").write_text(json.dumps(dataflow_sarif([
        finding(line_hash="known:1"),
        dataflow_finding("py/partial-ssrf", line_hash="ssrf:1"),
        dataflow_finding("py/path-injection", line_hash="pi:1"),
    ])))
    assert inspect(tmp_path, baseline) == 0


# ---------------------------------------------------------------------------
# regenerate_codeql_baseline.py must refuse what the gate would reject.
#
# A baseline built from an artifact the gate rejects is worse than no baseline:
# it passes review as "regenerated", then fails the advertised round-trip on
# the next run with no obvious cause.
# ---------------------------------------------------------------------------


def _regen_module():
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path

    path = _Path(__file__).resolve().parents[2] / "scripts" / "youtab" / "regenerate_codeql_baseline.py"
    spec = importlib.util.spec_from_file_location("regen_baseline_guard", path)
    module = importlib.util.module_from_spec(spec)
    _sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        _sys.path.remove(str(path.parent))
    return module


def _write_cycle_sarif(tmp_path, mutate=None):
    document = cycle_sarif([finding(line_hash="a:1")])
    if mutate:
        mutate(document["runs"][0])
    (tmp_path / "python.sarif").write_text(json.dumps(document))


def test_regenerator_refuses_a_failed_analysis(tmp_path):
    """The workflow uploads SARIF under `always()`, so a FAILED run leaves a
    downloadable artifact. Building a baseline from it would bake its missing
    results in as accepted debt."""
    regen = _regen_module()
    _write_cycle_sarif(tmp_path, lambda run: run["invocations"].clear())
    with pytest.raises(SystemExit) as excinfo:
        regen.build(tmp_path, "python", "https://example.test/run/1", "0" * 40)
    assert "successful invocation" in str(excinfo.value)


def test_regenerator_refuses_the_wrong_query_suite(tmp_path):
    """A narrower suite silently zeroes out every rule it does not run."""
    regen = _regen_module()
    _write_cycle_sarif(
        tmp_path,
        lambda run: run["properties"]["codeqlConfigSummary"].__setitem__(
            "queries", [{"type": "builtinSuite", "uses": "security-extended"}]
        ),
    )
    with pytest.raises(SystemExit) as excinfo:
        regen.build(tmp_path, "python", "https://example.test/run/1", "0" * 40)
    assert "security-and-quality" in str(excinfo.value)


def test_regenerator_refuses_a_head_that_contradicts_sarif_provenance(tmp_path):
    """Guards the wrong run's artifact and a mistyped --source-head.

    Only enforced when the SARIF actually carries `versionControlProvenance`;
    the artifacts this repository produces do not, so the check is conditional
    by necessity rather than by choice.
    """
    regen = _regen_module()
    _write_cycle_sarif(
        tmp_path,
        lambda run: run.__setitem__(
            "versionControlProvenance", [{"revisionId": "b" * 40}]
        ),
    )
    with pytest.raises(SystemExit) as excinfo:
        regen.build(tmp_path, "python", "https://example.test/run/1", "a" * 40)
    assert "provenance" in str(excinfo.value)

    # The matching SHA is accepted.
    assert regen.build(tmp_path, "python", "https://example.test/run/1", "b" * 40)


def coalesced_cycle_finding(uri="app.py", modules=("pkg.a", "pkg.b"), line_hash="cyc:1"):
    """One result carrying several diagnostics, as CodeQL emits them.

    CodeQL joins diagnostics that land on the same location into a single
    result with a newline-separated message. 105 keys in the committed Python
    baseline were of this shape before they were split per component.
    """
    text = NEWLINE.join(
        f"Import of module [{m}](1) begins an import cycle." for m in modules
    )
    return {
        "ruleId": CYCLE_RULE,
        "message": {"text": text},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": uri},
            "region": {"startLine": 11},
        }}],
        "partialFingerprints": {
            "primaryLocationLineHash": line_hash,
            "primaryLocationStartColumnFingerprint": "0",
        },
    }


def test_fixing_one_of_two_coalesced_diagnostics_is_not_a_new_finding(tmp_path):
    """The survivor of a coalesced pair must not be reported as new.

    Baseline: one result carrying both `pkg.a` and `pkg.b`. Current run: only
    `pkg.a` remains. Keyed on the joined message the identity would go from
    `A
B` to `A`, and `current - baseline` would call the SURVIVING finding
    new -- blocking the very remediation it is meant to permit, with exactly
    the phantom churn the module-name scheme exists to remove.

    Per component it is one `absent` and zero `new`, so the fix passes.

    Mutation check: key graph rules on the whole message instead of per
    component and this goes red.
    """
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(coalesced_cycle_finding(modules=("pkg.a", "pkg.b")), 1)])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(module="pkg.a", line_hash="now:1"),
    ])))
    assert inspect(tmp_path, baseline) == 0


def test_a_coalesced_pair_gaining_a_third_diagnostic_still_fails(tmp_path):
    """Splitting per component must not cost regression detection."""
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(coalesced_cycle_finding(modules=("pkg.a", "pkg.b")), 1)])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        coalesced_cycle_finding(modules=("pkg.a", "pkg.b", "pkg.c"), line_hash="now:1"),
    ])))
    assert inspect(tmp_path, baseline) == 1


def test_coverage_breach_is_proportional_not_an_absolute_count(tmp_path):
    """A small-language scan that loses its dataflow output must still fail.

    The threshold used to be an absolute `> 100`, calibrated on Python's 1272
    missing dataflow findings. The javascript-typescript baseline is 110
    findings TOTAL with ~53 across 13 dataflow rules, so a JS scan that lost
    all of its dataflow output produced `len(suspect) == 13` and
    `sum(suspect) == 53` -- under the floor, so the check never fired and
    every missing finding was filed as non-enforcing `absent`. Green gate on
    a partially failed scan.

    This reproduces those proportions: a 110-finding baseline where 53 sit in
    dataflow rules, and a current run that keeps only the syntactic ones.

    Mutation check: restore an absolute `sum(...) > 100` floor and this goes
    green.
    """
    dataflow_counts = [14, 13, 7, 4, 3, 3, 2, 2, 1, 1, 1, 1, 1]   # 53 across 13 rules
    rules = [f"py/dataflow-probe-{i}" for i in range(len(dataflow_counts))]

    findings = [(finding(line_hash="syntactic:1"), 57)]            # 57 + 53 = 110
    for rule, count in zip(rules, dataflow_counts):
        findings.append((dataflow_finding(rule, line_hash=f"{rule}:1"), count))

    baseline = tmp_path / "baseline.json"
    _baseline_requiring(baseline, findings, ["py/sql-injection", *rules])

    document = sarif([finding(line_hash="syntactic:1")])
    for rule in rules:
        document["runs"][0]["tool"]["extensions"][0]["rules"].append({"id": rule})
    (tmp_path / "python.sarif").write_text(json.dumps(document))

    assert inspect(tmp_path, baseline) == 2


def test_two_high_volume_queries_vanishing_is_a_coverage_breach(tmp_path):
    """Two queries are exactly how many a partial failure needs to take.

    A `len(suspect) >= 3` floor was here to keep remediating one whole rule
    from raising an alarm, and it opened a hole big enough to drive the
    original bug through. On the real Python baseline, losing only
    `py/clear-text-logging-sensitive-data` (734) and `py/path-injection`
    (279) is 1013 findings -- over the proportional floor of 908 -- but
    `len(suspect) == 2`, so the check never fired and all 1013 filed as
    non-enforcing `absent`. Green gate.

    Those proportions are reproduced here: two rules holding 1013 of a
    9076-finding baseline, both gone.

    Mutation check: reinstate a `len(suspect) >= 3` condition and this goes
    green.
    """
    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(finding(line_hash="syntactic:1"), 8063),
         (dataflow_finding("py/clear-text-logging-sensitive-data", line_hash="ctl:1"), 734),
         (dataflow_finding("py/path-injection", line_hash="pi:1"), 279)],
        ["py/sql-injection", "py/clear-text-logging-sensitive-data", "py/path-injection"],
    )
    document = sarif([finding(line_hash="syntactic:1")])
    for rule in ("py/clear-text-logging-sensitive-data", "py/path-injection"):
        document["runs"][0]["tool"]["extensions"][0]["rules"].append({"id": rule})
    (tmp_path / "python.sarif").write_text(json.dumps(document))

    assert inspect(tmp_path, baseline) == 2


def test_remediating_a_small_rule_entirely_is_still_an_ordinary_pass(tmp_path):
    """Below the proportional floor, a whole rule going to zero is just a fix.

    Built with DISTINCT findings so the baseline counts are ones a SARIF can
    actually produce: 191 syntactic results plus 9 in one dataflow rule, and
    the current run keeps all 191 and none of the 9. Deficit 9, floor 20 --
    remediation, and the gate should not editorialise about it.

    The earlier version of this test declared a count of 9067 for a single
    key that the SARIF emitted once, which the fully-missing-rule measure
    happened to tolerate and the deficit measure correctly does not. The
    fixture was wrong, not the check.
    """
    syntactic = [finding(line_hash=f"syn:{i}") for i in range(191)]
    ssrf = [dataflow_finding("py/partial-ssrf", line_hash=f"ssrf:{i}") for i in range(9)]

    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(f, 1) for f in syntactic + ssrf],
        ["py/sql-injection", "py/partial-ssrf"],
    )
    document = sarif(syntactic)
    document["runs"][0]["tool"]["extensions"][0]["rules"].append({"id": "py/partial-ssrf"})
    (tmp_path / "python.sarif").write_text(json.dumps(document))

    assert inspect(tmp_path, baseline) == 0


def test_partial_loss_within_surviving_queries_is_caught(tmp_path):
    """The hole the fully-missing-rule measure left wide open.

    A degraded analysis that retains even ONE result per query has no
    fully-missing rule at all, so nothing was suspect and every lost finding
    filed as non-enforcing `absent`. On the real baselines a run keeping one
    result per rule loses 9009 of 9076 (python) and 87 of 110 (JS), and the
    gate returned 0 for both -- the whole analysis could evaporate and the
    security gate would pass.

    Here: 200 findings across two rules, reduced to one result each.

    Mutation check: measure only rules whose current count is zero and this
    goes green.
    """
    syntactic = [finding(line_hash=f"syn:{i}") for i in range(100)]
    ssrf = [dataflow_finding("py/partial-ssrf", line_hash=f"ssrf:{i}") for i in range(100)]

    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(f, 1) for f in syntactic + ssrf],
        ["py/sql-injection", "py/partial-ssrf"],
    )
    document = sarif([syntactic[0], ssrf[0]])
    document["runs"][0]["tool"]["extensions"][0]["rules"].append({"id": "py/partial-ssrf"})
    (tmp_path / "python.sarif").write_text(json.dumps(document))

    assert inspect(tmp_path, baseline) == 2


def test_the_coverage_message_names_remediation_as_a_cause(tmp_path, capsys):
    """A large deliberate remediation now trips the gate too, so the message
    must not send the reader hunting for a scanner fault that is not there."""
    baseline = tmp_path / "baseline.json"
    _baseline_requiring(
        baseline,
        [(finding(line_hash="syntactic:1"), 76),
         (dataflow_finding("py/path-injection", line_hash="pi:1"), 34)],
        ["py/sql-injection", "py/path-injection"],
    )
    document = sarif([finding(line_hash="syntactic:1")])
    document["runs"][0]["tool"]["extensions"][0]["rules"].append({"id": "py/path-injection"})
    (tmp_path / "python.sarif").write_text(json.dumps(document))

    assert inspect(tmp_path, baseline) == 2
    err = capsys.readouterr().err
    assert "remediated in full" in err
    assert "Read the findings before you decide" in err


# ---------------------------------------------------------------------------
# Coverage metadata is DERIVED from the artifact, so a partial SARIF that
# still marks its invocation successful yields zeros for it. The gate requires
# every count positive, so such a baseline fails EVERY later run with the
# cause three steps removed from the symptom. Refuse at generation time.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,mutate,expected", [
    (
        "artifact_count",
        lambda run: run.__setitem__("artifacts", []),
        "artifact_count",
    ),
    (
        "extracted_count",
        lambda run: run["invocations"][0].__setitem__("toolExecutionNotifications", []),
        "extracted_count",
    ),
    (
        "query_count",
        lambda run: run["tool"]["extensions"][0].__setitem__("rules", []),
        "query_count",
    ),
])
def test_regenerator_refuses_missing_coverage_metadata(tmp_path, field, mutate, expected):
    """Mutation check: drop the coverage validation and each of these writes a
    baseline that the gate then rejects on every run."""
    regen = _regen_module()
    _write_cycle_sarif(tmp_path, mutate)
    with pytest.raises(SystemExit) as excinfo:
        regen.build(tmp_path, "python", "https://example.test/run/1", "0" * 40)
    assert expected in str(excinfo.value)


def test_regenerator_accepts_a_clean_analysis_as_an_empty_baseline(tmp_path, capsys):
    """Zero findings with intact coverage is a CLEAN run, not a broken one.

    This used to be refused outright, reasoning that an empty baseline "makes
    the gate's own 'zero findings against a nonempty baseline' guard
    unreachable, so everything afterwards passes". The first half is true and
    the second does not follow: with an empty baseline, ``new = current -
    baseline`` is the entire current set, so the next finding fails at exit 1.
    That guard exists for a run reporting nothing against a baseline that HAS
    entries; it is inapplicable here rather than bypassed --
    ``test_an_empty_baseline_still_fails_on_the_first_new_finding`` below
    proves enforcement is intact.

    The refusal also blocked gating three real languages. ``rust`` reports 0
    findings in this repository, and an empty baseline is the strongest gate
    available for it: nothing accepted, so anything new is new.

    A loud NOTE is still printed, because "0 findings" is also what a
    truncated artifact looks like to a human skimming the output.
    """
    regen = _regen_module()
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([])))

    baseline = regen.build(tmp_path, "python", "https://example.test/run/1", "0" * 40)

    assert baseline["open_total"] == 0
    assert baseline["findings"] == {}
    # Coverage metadata must still be real -- that is what separates "clean"
    # from "partial".
    assert baseline["artifact_count"] > 0
    assert baseline["query_count"] > 0
    assert baseline["extracted_count"] > 0
    assert "zero findings" in capsys.readouterr().out


@pytest.mark.parametrize("field,mutate", [
    ("artifacts", lambda run: run.__setitem__("artifacts", [])),
    ("extensions", lambda run: run["tool"].__setitem__("extensions", [])),
    ("notifications", lambda run: run["invocations"][0].__setitem__(
        "toolExecutionNotifications", [])),
])
def test_regenerator_still_refuses_zero_findings_with_degraded_coverage(
    tmp_path, field, mutate
):
    """Zero findings is accepted ONLY alongside evidence the analysis ran.

    This is the half of the old blanket refusal that was load-bearing. A
    truncated artifact can report zero findings while still marking its
    invocation successful, and a baseline built from one would be committed as
    "regenerated" and then rejected by the gate on every later run.
    """
    regen = _regen_module()
    document = cycle_sarif([])
    mutate(document["runs"][0])
    (tmp_path / "python.sarif").write_text(json.dumps(document))

    with pytest.raises(SystemExit):
        regen.build(tmp_path, "python", "https://example.test/run/1", "0" * 40)


def test_an_empty_baseline_still_fails_on_the_first_new_finding(tmp_path):
    """The enforcement claim above, asserted rather than argued.

    An empty baseline accepts nothing, so a single finding is new and the gate
    exits 1. If this ever passed, the empty baseline really would be the hole
    the old refusal feared.
    """
    baseline = tmp_path / "baseline.json"
    _baseline_requiring(baseline, [], ["py/sql-injection"])
    document = json.loads(baseline.read_text(encoding="utf-8"))
    assert document["findings"] == {}, "fixture is not an empty baseline"
    (tmp_path / "python.sarif").write_text(
        json.dumps(sarif([finding(line_hash="brand:1")]))
    )

    assert inspect(tmp_path, baseline) == 1


def test_regenerator_accepts_a_complete_artifact(tmp_path):
    """The guard above must not reject a healthy artifact."""
    regen = _regen_module()
    _write_cycle_sarif(tmp_path)
    baseline = regen.build(tmp_path, "python", "https://example.test/run/1", "0" * 40)
    assert baseline["artifact_count"] > 0
    assert baseline["query_count"] > 0
    assert baseline["extracted_count"] > 0
    assert baseline["required_rules"]
    assert baseline["findings"]


# ---------------------------------------------------------------------------
# A run must succeed in EVERY invocation. `any(...)` accepted statuses of
# [true, false] -- a partially failed analysis -- and its missing results then
# read as `absent` rather than as a coverage problem.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("statuses", [
    [True, False],
    [False, True],
    [True, None],
    [],
])
def test_gate_requires_every_invocation_to_succeed(tmp_path, statuses):
    """Mutation check: change `all` back to `any` and the [true, False] and
    [true, None] cases go green."""
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(finding(line_hash="known:1"), 1)])
    document = sarif([finding(line_hash="known:1")])
    template = document["runs"][0]["invocations"][0]
    document["runs"][0]["invocations"] = [
        {**template, "executionSuccessful": status} for status in statuses
    ]
    (tmp_path / "python.sarif").write_text(json.dumps(document))
    assert inspect(tmp_path, baseline) == 2


def test_gate_accepts_several_successful_invocations(tmp_path):
    """Requiring all of them must not reject a healthy multi-invocation run."""
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(finding(line_hash="known:1"), 1)])
    document = sarif([finding(line_hash="known:1")])
    template = document["runs"][0]["invocations"][0]
    document["runs"][0]["invocations"] = [dict(template), dict(template)]
    (tmp_path / "python.sarif").write_text(json.dumps(document))
    assert inspect(tmp_path, baseline) == 0


def test_regenerator_requires_every_invocation_to_succeed(tmp_path):
    """Same rule on the generation side."""
    regen = _regen_module()
    _write_cycle_sarif(
        tmp_path,
        lambda run: run.__setitem__("invocations", [
            {**run["invocations"][0], "executionSuccessful": True},
            {**run["invocations"][0], "executionSuccessful": False},
        ]),
    )
    with pytest.raises(SystemExit) as excinfo:
        regen.build(tmp_path, "python", "https://example.test/run/1", "0" * 40)
    assert "successful invocation" in str(excinfo.value)
