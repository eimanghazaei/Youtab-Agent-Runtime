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
