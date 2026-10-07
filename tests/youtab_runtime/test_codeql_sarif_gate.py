import json

from scripts.youtab.codeql_sarif_gate import finding_key, inspect


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


def write_baseline(path, findings):
    total = sum(count for _, count in findings)
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
        "findings": {finding_key(result): count for result, count in findings},
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

CYCLE_RULE = "py/cyclic-import"


def cycle_finding(uri="app.py", line_hash="cyc:1"):
    return {
        "ruleId": CYCLE_RULE,
        "message": {"text": "Import of module begins an import cycle."},
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

    Baseline records two cycle findings in app.py. The current run reports two
    cycle findings in app.py at completely different line hashes -- which is
    what happens when an unrelated module in the same cycle is edited. Count is
    unchanged, so this must pass. Before the per-file count comparison it
    reported "2 new, 2 absent" and failed.
    """
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [
        (cycle_finding(line_hash="was:1"), 1),
        (cycle_finding(line_hash="was:2"), 1),
    ])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(line_hash="now:1"),
        cycle_finding(line_hash="now:2"),
    ])))
    assert inspect(tmp_path, baseline) == 0


def test_graph_scoped_count_increase_in_a_file_still_fails(tmp_path):
    """Churn immunity must not cost us regression detection."""
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, [(cycle_finding(line_hash="was:1"), 1)])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(line_hash="now:1"),
        cycle_finding(line_hash="now:2"),
    ])))
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
        (cycle_finding(line_hash="was:1"), 1),
        (cycle_finding(line_hash="was:2"), 1),
    ])
    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(line_hash="now:1"),
    ])))
    assert inspect(tmp_path, baseline) == 0

    (tmp_path / "python.sarif").write_text(json.dumps(cycle_sarif([
        cycle_finding(line_hash="now:1"),
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
