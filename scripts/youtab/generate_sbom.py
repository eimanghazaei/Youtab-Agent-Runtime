#!/usr/bin/env python3
"""Emit a CycloneDX 1.5 SBOM for the installed Python environment.

An SBOM is the inventory a CVE feed is matched against: without it, "which
versions of which libraries do we actually run" is answered by guesswork. This
script produces a CycloneDX 1.5 JSON document from ``importlib.metadata`` — i.e.
from what is really installed in the interpreter, not from what a lockfile claims
— so it stays correct even when the runtime image drifts from ``uv.lock``.

CycloneDX assembly is done directly from stdlib rather than via ``cyclonedx-py``
so this has no dependency of its own and cannot itself widen the supply-chain
surface it exists to document. If ``cyclonedx-py`` is installed it is NOT used;
the format emitted here is a stable subset that validates against the 1.5 schema.

This is an ARTIFACT producer, not a gate: it exits 0 as long as it can write the
file. Vulnerability *enforcement* is ``cve_audit.py``'s job.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from importlib import metadata as _md
from pathlib import Path
from typing import Any, Optional


CYCLONEDX_SPEC_VERSION = "1.5"


def _purl(name: str, version: str) -> str:
    """A minimal PEP 508 -> package URL for a PyPI distribution."""
    safe_name = name.strip().lower().replace(" ", "-")
    return f"pkg:pypi/{safe_name}@{version}"


def _licenses(dist: _md.Distribution) -> list[dict[str, Any]]:
    meta = dist.metadata
    names: list[str] = []
    # Prefer the SPDX-ish "License-Expression" then classifiers then "License".
    expr = meta.get("License-Expression")
    if expr:
        return [{"expression": expr}]
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License :: "):
            names.append(classifier.split(" :: ")[-1].strip())
    lic = meta.get("License")
    if lic and lic.strip() and lic.strip().lower() != "unknown" and not names:
        names.append(lic.strip().splitlines()[0][:128])
    return [{"license": {"name": n}} for n in dict.fromkeys(names)]


def collect_components() -> list[dict[str, Any]]:
    """One CycloneDX component per installed distribution, sorted by name."""
    seen: dict[str, dict[str, Any]] = {}
    for dist in _md.distributions():
        try:
            name = dist.metadata["Name"]
            version = dist.version
        except Exception:
            continue
        if not name:
            continue
        key = f"{name.lower()}@{version}"
        if key in seen:
            continue
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": _purl(name, version or "0"),
            "name": name,
            "version": version or "0",
            "purl": _purl(name, version or "0"),
        }
        licenses = _licenses(dist)
        if licenses:
            component["licenses"] = licenses
        seen[key] = component
    return sorted(seen.values(), key=lambda c: (c["name"].lower(), c["version"]))


def build_sbom(
    *,
    components: Optional[list[dict[str, Any]]] = None,
    now: Optional[_dt.datetime] = None,
    tool_name: str = "youtab-generate-sbom",
) -> dict[str, Any]:
    """Assemble the CycloneDX document. Pure over its inputs (testable)."""
    comps = collect_components() if components is None else components
    timestamp = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # A serial number that is stable for a given component set (content-addressed)
    # so re-running on an unchanged environment yields an identical SBOM.
    fingerprint = hashlib.sha256(
        json.dumps(sorted([c["name"], c["version"]] for c in comps)).encode()
    ).hexdigest()
    serial = f"urn:uuid:{fingerprint[:8]}-{fingerprint[8:12]}-{fingerprint[12:16]}-" \
             f"{fingerprint[16:20]}-{fingerprint[20:32]}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "serialNumber": serial,
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": [{"vendor": "Youtab", "name": tool_name}],
            "component": {
                "type": "application",
                "bom-ref": "youtab-agent-runtime",
                "name": "youtab-agent-runtime",
            },
        },
        "components": comps,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path(".youtab/evidence/sbom.cdx.json"))
    args = parser.parse_args(argv)
    sbom = build_sbom()
    payload = json.dumps(sbom, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    print(f"SBOM: wrote {len(sbom['components'])} components -> {args.output}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
