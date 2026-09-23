"""WAVE-27 CodeQL dataflow SAST workflow (.github/workflows/codeql.yml).

State-based: assertions are on the actual bytes of the committed workflow and on
the result ``dependency_gate.scan`` returns over the real repo. The workflow's
value is dataflow coverage of both shipped languages; its constraint is that it
must not break the required ``python-security`` gate, which enforces 40-hex
SHA-pinning across every file under .github/workflows/.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CODEQL = REPO / ".github" / "workflows" / "codeql.yml"

_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
_SHA = re.compile(r"[0-9a-f]{40}")


def test_codeql_workflow_exists():
    assert CODEQL.is_file(), "codeql.yml must be committed"


def test_every_action_is_pinned_to_a_40_hex_sha():
    text = CODEQL.read_text(encoding="utf-8")
    uses = _USES.findall(text)
    assert uses, "the workflow declares no actions"
    for action in uses:
        ref = action.rsplit("@", 1)[-1] if "@" in action else ""
        assert _SHA.fullmatch(ref), f"un-pinned action in codeql.yml: {action}"


def test_it_covers_both_shipped_languages():
    text = CODEQL.read_text(encoding="utf-8")
    assert "python" in text
    # CodeQL analyses TS via the javascript extractor; either spelling is fine.
    assert "javascript" in text


def test_it_runs_the_codeql_analyze_action():
    text = CODEQL.read_text(encoding="utf-8")
    assert "github/codeql-action/init@" in text
    assert "github/codeql-action/analyze@" in text


def test_adding_codeql_did_not_break_the_dependency_pinning_gate():
    """The required python-security gate scans EVERY workflow; codeql.yml included."""
    spec = importlib.util.spec_from_file_location(
        "_dep_gate", REPO / "scripts" / "youtab" / "dependency_gate.py"
    )
    dep_gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dep_gate)
    result = dep_gate.scan(REPO)
    assert result["passed"], f"dependency_gate now fails: {result['errors']}"
    # And codeql.yml's actions are counted, proving they were seen and accepted.
    assert result["counts"]["github_actions"] >= 3
