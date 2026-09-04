"""WAVE-27 A06 supply-chain CVE gate (scripts/youtab/cve_audit.py).

State-based: every assertion is on the Report/JSON the pure ``evaluate`` step
produces from concrete inputs, or on the file the CLI actually writes. Nothing
here asserts "a mock was called"; the network/subprocess boundary is exercised
separately (:func:`parse_pip_audit`) against captured pip-audit JSON shapes.

The subject is the gate's *decision*: a known CVE with no valid triage blocks; a
non-expired allowlist entry suppresses it; an expired one does not; and a real
tooling failure fails closed rather than passing.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "_cve_audit", REPO / "scripts" / "youtab" / "cve_audit.py"
)
cve = importlib.util.module_from_spec(_SPEC)
# Register before exec so dataclass string annotations resolve (from __future__).
sys.modules[_SPEC.name] = cve
_SPEC.loader.exec_module(cve)

TODAY = dt.date(2026, 9, 4)


def _finding(package="pillow", version="12.2.0", vuln_id="PYSEC-2026-2253",
             aliases=("CVE-2026-54059",), fix=("12.3.0",)) -> cve.Finding:
    return cve.Finding(package=package, version=version, vuln_id=vuln_id,
                       aliases=tuple(aliases), fix_versions=tuple(fix))


def _entry(reason="triaged", expires=dt.date(2026, 12, 3), id=None, package=None):
    return cve.AllowEntry(reason=reason, expires=expires, id=id, package=package)


# ── the pure decision ────────────────────────────────────────────────────────


def test_a_known_vuln_with_no_allowlist_blocks():
    report = cve.evaluate([_finding()], [], TODAY)
    assert report.passed is False
    assert len(report.blocking) == 1
    assert report.blocking[0].vuln_id == "PYSEC-2026-2253"
    assert not report.suppressed


def test_no_findings_is_a_clean_pass():
    report = cve.evaluate([], [], TODAY)
    assert report.passed is True
    assert report.blocking == []


def test_a_matching_nonexpired_entry_suppresses_by_primary_id():
    report = cve.evaluate([_finding()], [_entry(id="PYSEC-2026-2253")], TODAY)
    assert report.passed is True
    assert len(report.suppressed) == 1
    assert report.suppressed[0][0].vuln_id == "PYSEC-2026-2253"
    assert report.blocking == []


def test_an_entry_can_match_by_alias_cve_not_only_primary_id():
    # The operator triaged the CVE number; the primary id is PYSEC. Both address
    # the same advisory, so the alias must match.
    report = cve.evaluate([_finding()], [_entry(id="CVE-2026-54059")], TODAY)
    assert report.passed is True
    assert report.suppressed and not report.blocking


def test_a_package_wide_entry_covers_every_advisory_of_that_dist():
    findings = [
        _finding(vuln_id="PYSEC-2026-2253", aliases=("CVE-2026-54059",)),
        _finding(vuln_id="PYSEC-2026-3451", aliases=("CVE-2026-59199",)),
    ]
    report = cve.evaluate(findings, [_entry(package="Pillow")], TODAY)  # case-insensitive
    assert report.passed is True
    assert len(report.suppressed) == 2
    assert not report.blocking


def test_an_expired_entry_does_not_suppress_and_is_reported():
    entry = _entry(id="PYSEC-2026-2253", expires=dt.date(2026, 1, 1))
    report = cve.evaluate([_finding()], [entry], TODAY)
    assert report.passed is False, "an expired allowlist entry must not suppress"
    assert len(report.blocking) == 1
    assert len(report.expired_hits) == 1
    assert report.expired_hits[0][0].vuln_id == "PYSEC-2026-2253"


def test_a_valid_entry_wins_over_an_expired_one_for_the_same_finding():
    finding = _finding()
    expired = _entry(id="PYSEC-2026-2253", expires=dt.date(2020, 1, 1))
    valid = _entry(package="pillow", expires=dt.date(2026, 12, 3))
    report = cve.evaluate([finding], [expired, valid], TODAY)
    assert report.passed is True
    assert report.suppressed and not report.blocking


def test_an_allowlist_entry_matching_nothing_is_stale_not_a_pass():
    entry = _entry(id="CVE-1999-0001")
    report = cve.evaluate([_finding()], [entry], TODAY)
    # It still blocks the real finding; the unused entry is surfaced as stale.
    assert report.passed is False
    assert entry in report.stale_entries


# ── parsing the tool output (the subprocess boundary, on captured shapes) ─────


def test_parse_pip_audit_dependencies_shape():
    document = {
        "dependencies": [
            {"name": "h2", "version": "4.4.0", "vulns": [
                {"id": "PYSEC-2026-3628", "aliases": ["CVE-2026-71554"],
                 "fix_versions": ["4.4.1"], "description": "host header smuggling"},
            ]},
            {"name": "anyio", "version": "4.14.2", "vulns": []},
        ]
    }
    findings = cve.parse_pip_audit(document)
    assert len(findings) == 1
    f = findings[0]
    assert (f.package, f.version, f.vuln_id) == ("h2", "4.4.0", "PYSEC-2026-3628")
    assert "CVE-2026-71554" in f.identifiers
    assert f.fix_versions == ("4.4.1",)


def test_parse_pip_audit_bare_list_shape():
    document = [
        {"name": "mcp", "version": "1.26.0",
         "vulns": [{"id": "PYSEC-2026-3482", "aliases": [], "fix_versions": ["1.27.2"]}]},
    ]
    findings = cve.parse_pip_audit(document)
    assert len(findings) == 1
    assert findings[0].package == "mcp"


def test_parse_pip_audit_rejects_non_list_payload():
    with pytest.raises(cve.CveAuditError):
        cve.parse_pip_audit({"not_dependencies": 1})


# ── the allowlist loader ─────────────────────────────────────────────────────


def test_load_allowlist_reads_id_reason_and_expiry(tmp_path: Path):
    p = tmp_path / "a.toml"
    p.write_text(
        '[[ignore]]\nid = "CVE-2025-71176"\nreason = "test-only dep"\n'
        'expires = 2026-12-03\n',
        encoding="utf-8",
    )
    entries = cve.load_allowlist(p)
    assert len(entries) == 1
    assert entries[0].id == "CVE-2025-71176"
    assert entries[0].expires == dt.date(2026, 12, 3)


def test_load_allowlist_missing_file_is_empty(tmp_path: Path):
    assert cve.load_allowlist(tmp_path / "nope.toml") == []


def test_load_allowlist_rejects_entry_with_no_reason(tmp_path: Path):
    p = tmp_path / "a.toml"
    p.write_text('[[ignore]]\nid = "CVE-1"\nexpires = 2026-12-03\n', encoding="utf-8")
    with pytest.raises(cve.CveAuditError, match="reason"):
        cve.load_allowlist(p)


def test_load_allowlist_rejects_entry_with_neither_id_nor_package(tmp_path: Path):
    p = tmp_path / "a.toml"
    p.write_text('[[ignore]]\nreason = "x"\nexpires = 2026-12-03\n', encoding="utf-8")
    with pytest.raises(cve.CveAuditError, match="id.*package"):
        cve.load_allowlist(p)


def test_load_allowlist_rejects_entry_with_no_expiry(tmp_path: Path):
    p = tmp_path / "a.toml"
    p.write_text('[[ignore]]\nid = "CVE-1"\nreason = "x"\n', encoding="utf-8")
    with pytest.raises(cve.CveAuditError, match="expires"):
        cve.load_allowlist(p)


# ── the shipped allowlist is itself well-formed and honest ───────────────────


def test_the_shipped_allowlist_is_empty_after_wave28_source_fixes():
    entries = cve.load_allowlist(REPO / "scripts" / "youtab" / "cve_allowlist.toml")
    # WAVE-28 closed every previously-open advisory at the source (pin bump +
    # uv.lock regen): the four runtime CVEs (cryptography/h2/mcp/pillow) AND the
    # two build/test-only ones (pytest/setuptools) are all fixed, so nothing is
    # suppressed. The gate is now blocking with an EMPTY allowlist — the honest
    # end state. If a future exception is added it must still be justified and
    # not pre-expired (asserted below), but the shipped default is zero.
    assert entries == [], (
        "the shipped allowlist must be empty: fix advisories at the source (bump "
        "the pin + regenerate uv.lock), do not suppress them. If an exception is "
        f"genuinely unavoidable it needs Owner sign-off. Found: {[e.id for e in entries]}"
    )
    for e in entries:
        assert e.reason.strip(), "every shipped exception must carry a justification"
        assert e.expires > dt.date(2026, 9, 4), "a shipped exception must not be pre-expired"


# ── the JSON report + CLI exit contract ──────────────────────────────────────


def test_render_json_is_machine_readable_and_lists_blocking():
    report = cve.evaluate([_finding()], [], TODAY)
    payload = cve.render_json(report)
    assert payload["passed"] is False
    assert payload["blocking_count"] == 1
    assert payload["blocking"][0]["id"] == "PYSEC-2026-2253"
    assert payload["blocking"][0]["fix_versions"] == ["12.3.0"]
    # round-trips as JSON
    assert json.loads(json.dumps(payload))["passed"] is False


def test_cli_writes_report_and_exits_nonzero_on_blocking(tmp_path, monkeypatch):
    out = tmp_path / "cve.json"
    monkeypatch.setattr(cve, "run_pip_audit", lambda *a, **k: [_finding()])
    code = cve.main(["--output", str(out), "--today", "2026-09-04",
                     "--allowlist", str(tmp_path / "empty.toml")])
    assert code == 1
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["passed"] is False
    assert written["blocking"][0]["package"] == "pillow"


def test_cli_exits_zero_when_all_findings_are_triaged(tmp_path, monkeypatch):
    allowlist = tmp_path / "a.toml"
    allowlist.write_text('[[ignore]]\npackage = "pillow"\nreason = "accepted"\n'
                         'expires = 2026-12-03\n', encoding="utf-8")
    monkeypatch.setattr(cve, "run_pip_audit", lambda *a, **k: [_finding()])
    code = cve.main(["--today", "2026-09-04", "--allowlist", str(allowlist)])
    assert code == 0


def test_cli_fails_closed_when_pip_audit_cannot_run(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise cve.CveAuditError("pip-audit did not emit JSON")
    monkeypatch.setattr(cve, "run_pip_audit", boom)
    code = cve.main(["--today", "2026-09-04", "--allowlist", str(tmp_path / "e.toml")])
    assert code == 2, "a tool that cannot run must fail closed, never pass"
