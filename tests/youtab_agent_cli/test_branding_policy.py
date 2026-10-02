"""A future artifact cannot restore the permanently retired CLI title."""
import json
import subprocess
import sys
from pathlib import Path

from youtab_agent_cli.branding_policy import contains_retired_banner

def _retired_header():
    block, corner = chr(0x2588), chr(0x2557)
    return block * 2 + corner + "  " + block * 2 + corner + block * 7 + corner + block * 6 + corner + " " + block * 3 + corner


def test_compiler_escaped_retired_logo_is_rejected():
    assert contains_retired_banner(_retired_header().encode("unicode_escape"))
    assert not contains_retired_banner(b"Youtab Code")


def test_branding_ci_rejects_retired_artwork_in_tracked_product_source(tmp_path):
    root = Path(__file__).resolve().parents[2]
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "cli.py").write_text('BANNER = "' + _retired_header() + '"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    report = tmp_path / "gate.json"
    result = subprocess.run([sys.executable, str(root / "scripts/youtab/branding_gate.py"), "--root", str(tmp_path), "--output", str(report)], capture_output=True, text=True)
    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert any(f["kind"] == "retired-cli-banner" for f in payload["blocking"])


def test_historical_source_evidence_is_preserved_and_not_executed_as_product(tmp_path):
    from youtab_agent_cli.branding_policy import find_retired_banner
    evidence = tmp_path / "docs" / "evidence" / "old-source.py"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(_retired_header(), encoding="utf-8")
    assert find_retired_banner(tmp_path) is None
    active = tmp_path / "assets" / "renamed-title.txt"
    active.parent.mkdir()
    active.write_text(_retired_header(), encoding="utf-8")
    assert find_retired_banner(tmp_path) == active
