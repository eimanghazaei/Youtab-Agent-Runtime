import json
from datetime import date

from scripts.youtab.dependency_exception_gate import validate


def _policy(tmp_path, *, accepted="2026-09-12", expires="2026-10-12"):
    path = tmp_path / "exceptions.json"
    path.write_text(json.dumps({"exceptions": [{
        "id": "one", "owner": "@owner", "ecosystem": "npm",
        "package": "dependency", "advisories": ["GHSA-example"],
        "severity": "high", "scope": "build only", "reachability": "build",
        "mitigations": ["sandbox"], "accepted_on": accepted,
        "expires_on": expires, "upstream_fix": "none",
    }]}), encoding="utf-8")
    return path


def test_current_bounded_exception_passes(tmp_path):
    assert validate(_policy(tmp_path), date(2026, 9, 20)) == []


def test_expired_exception_fails(tmp_path):
    assert "expired" in validate(_policy(tmp_path), date(2026, 10, 13))[0]


def test_long_lived_exception_fails(tmp_path):
    errors = validate(
        _policy(tmp_path, accepted="2026-09-12", expires="2027-01-01"),
        date(2026, 9, 20),
    )
    assert any("lifetime exceeds" in error for error in errors)
