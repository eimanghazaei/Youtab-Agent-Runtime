import json

from scripts.youtab.codeql_sarif_gate import inspect


def sarif(results):
    return {"version": "2.1.0", "runs": [{
        "tool": {"driver": {"name": "CodeQL"}}, "results": results,
    }]}


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
    (tmp_path / "python.sarif").write_text(json.dumps(sarif([{
        "ruleId": "py/sql-injection",
        "message": {"text": "Untrusted query"},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "app.py"},
            "region": {"startLine": 9},
        }}],
    }])))
    assert inspect(tmp_path) == 1
    assert "app.py:9: py/sql-injection" in capsys.readouterr().err
