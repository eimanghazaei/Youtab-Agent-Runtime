"""Regression tests for the Independent Agent Review gate (scripts/youtab/
independent_agent_review.py) — the SHA-bound, fail-closed governance gate that
replaced Greptile (docs/governance/INDEPENDENT_AGENT_REVIEW.md).

Focus: LOW-2 — the gate must reject a CONTRADICTORY record that reports
blocking-severity findings in ``findings_by_severity`` without enumerating them,
so a mis-authored record cannot pass with unaddressed high-severity counts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_GATE = _REPO / "scripts" / "youtab" / "independent_agent_review.py"


def _load():
    spec = importlib.util.spec_from_file_location("iar_gate", _GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rec(**over):
    module = _load()
    base = {f: "x" for f in module.REQUIRED_FIELDS}
    base.update(
        head_sha="deadbeef",
        verdict="PROVEN",
        unresolved_findings=[],
        findings_by_severity={"critical": 0, "high": 0, "medium": 0, "low": 0},
    )
    base.update(over)
    return module, base


def test_clean_proven_passes():
    module, rec = _rec()
    assert module._gate_passes(rec, "deadbeef") == (True, [])


def test_low_2_contradictory_severity_counts_fail_closed():
    # Reports High:2 but enumerates none — contradictory, must fail closed.
    module, rec = _rec(findings_by_severity={"high": 2}, unresolved_findings=[])
    ok, reasons = module._gate_passes(rec, "deadbeef")
    assert ok is False
    assert any("contradictory" in r for r in reasons), reasons


def test_low_2_found_and_resolved_blocking_is_consistent_and_passes():
    module, rec = _rec(
        findings_by_severity={"high": 1},
        unresolved_findings=[],
        resolved_findings=[{"severity": "high", "summary": "fixed pre-merge"}],
    )
    assert module._gate_passes(rec, "deadbeef")[0] is True


def test_unresolved_blocking_finding_fails_closed():
    module, rec = _rec(
        findings_by_severity={"high": 1},
        unresolved_findings=[{"severity": "high", "summary": "still open"}],
    )
    assert module._gate_passes(rec, "deadbeef")[0] is False


def test_non_integer_severity_count_fails_closed():
    module, rec = _rec(findings_by_severity={"critical": "two"})
    assert module._gate_passes(rec, "deadbeef")[0] is False


def test_sha_mismatch_fails_closed():
    module, rec = _rec()
    assert module._gate_passes(rec, "some-other-sha")[0] is False


def test_non_proven_verdict_fails_closed():
    module, rec = _rec(verdict="NOT PROVEN")
    assert module._gate_passes(rec, "deadbeef")[0] is False
