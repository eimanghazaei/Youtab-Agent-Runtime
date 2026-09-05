"""WAVE-27 SBOM generator (scripts/youtab/generate_sbom.py).

State-based: assertions are on the CycloneDX document structure and on the file
the CLI actually writes. The SBOM is the inventory the CVE feed is matched
against, so the test pins the invariants a downstream scanner relies on:
CycloneDX 1.5 envelope, one component per distribution with a name/version/purl,
and a content-stable serial number.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "_gen_sbom", REPO / "scripts" / "youtab" / "generate_sbom.py"
)
sbom = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sbom
_SPEC.loader.exec_module(sbom)

_NOW = dt.datetime(2026, 9, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
_COMPONENTS = [
    {"type": "library", "bom-ref": "pkg:pypi/pillow@12.2.0", "name": "pillow",
     "version": "12.2.0", "purl": "pkg:pypi/pillow@12.2.0"},
    {"type": "library", "bom-ref": "pkg:pypi/anyio@4.14.2", "name": "anyio",
     "version": "4.14.2", "purl": "pkg:pypi/anyio@4.14.2"},
]


def test_envelope_is_cyclonedx_1_5():
    doc = sbom.build_sbom(components=_COMPONENTS, now=_NOW)
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] == "1.5"
    assert doc["serialNumber"].startswith("urn:uuid:")
    assert doc["metadata"]["timestamp"] == "2026-09-04T12:00:00Z"
    assert doc["metadata"]["component"]["name"] == "youtab-agent-runtime"


def test_every_component_has_name_version_and_pypi_purl():
    doc = sbom.build_sbom(components=_COMPONENTS, now=_NOW)
    assert len(doc["components"]) == 2
    for comp in doc["components"]:
        assert comp["name"] and comp["version"]
        assert comp["purl"] == f"pkg:pypi/{comp['name']}@{comp['version']}"
        assert comp["type"] == "library"


def test_serial_number_is_content_stable():
    a = sbom.build_sbom(components=_COMPONENTS, now=_NOW)
    b = sbom.build_sbom(components=list(reversed(_COMPONENTS)),
                        now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc))
    # Same component SET (order/time differ) -> identical serial number.
    assert a["serialNumber"] == b["serialNumber"]


def test_serial_number_changes_when_a_component_changes():
    changed = [dict(_COMPONENTS[0], version="12.3.0"), _COMPONENTS[1]]
    a = sbom.build_sbom(components=_COMPONENTS, now=_NOW)
    b = sbom.build_sbom(components=changed, now=_NOW)
    assert a["serialNumber"] != b["serialNumber"]


def test_collect_components_reflects_the_real_environment():
    comps = sbom.collect_components()
    # The interpreter running the test is a real Python env with pytest installed.
    names = {c["name"].lower() for c in comps}
    assert "pytest" in names
    for c in comps:
        assert c["purl"].startswith("pkg:pypi/")
        assert c["version"]


def test_cli_writes_a_valid_json_sbom_file(tmp_path: Path):
    out = tmp_path / "sbom.cdx.json"
    code = sbom.main(["--output", str(out)])
    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] == "1.5"
    assert len(doc["components"]) > 0
    # self-reference: the SBOM of this env includes pytest
    assert any(c["name"].lower() == "pytest" for c in doc["components"])
