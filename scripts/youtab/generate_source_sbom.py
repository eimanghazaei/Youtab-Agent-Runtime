#!/usr/bin/env python3
"""Generate a deterministic CycloneDX source/dependency SBOM without network."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def component(group: str, name: str, version: str, purl: str, properties: list[dict[str, str]] | None = None) -> dict:
    item = {
        "type": "library",
        "group": group,
        "name": name,
        "version": version,
        "bom-ref": purl,
        "purl": purl,
    }
    if properties:
        item["properties"] = properties
    return item


def python_components() -> list[dict]:
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    rows = []
    for package in data.get("package", []):
        name, version = package.get("name"), package.get("version")
        if not name or not version:
            continue
        purl = f"pkg:pypi/{quote(name)}@{quote(version)}"
        source = package.get("source", {})
        rows.append(component("python", name, version, purl, [{"name": "youtab:source", "value": json.dumps(source, sort_keys=True)}]))
    return rows


def npm_components() -> list[dict]:
    rows: dict[str, dict] = {}
    locks = [
        ROOT / "package-lock.json",
        ROOT / "website/package-lock.json",
        ROOT / "scripts/whatsapp-bridge/package-lock.json",
        ROOT / "plugins/platforms/photon/sidecar/package-lock.json",
    ]
    for lock in locks:
        data = json.loads(lock.read_text(encoding="utf-8"))
        for location, package in data.get("packages", {}).items():
            if not location or package.get("link") or not package.get("version"):
                continue
            name = package.get("name") or location.rsplit("node_modules/", 1)[-1]
            version = str(package["version"])
            purl = f"pkg:npm/{quote(name, safe='@/')}@{quote(version)}"
            props = [
                {"name": "youtab:lockfile", "value": str(lock.relative_to(ROOT))},
                {"name": "youtab:integrity", "value": str(package.get("integrity", ""))},
            ]
            rows[purl] = component("npm", name, version, purl, props)
    return list(rows.values())


def manifest_components() -> list[dict]:
    rows = []
    base = ROOT / ".youtab/supply-chain"
    containers = json.loads((base / "containers.lock.json").read_text(encoding="utf-8"))
    for image in containers["images"]:
        ref = f"urn:youtab:container:{image['sha256']}"
        rows.append({
            "type": "container",
            "name": image["role"],
            "version": image["sha256"],
            "bom-ref": ref,
            "hashes": [{"alg": "SHA-256", "content": image["sha256"]}],
            "properties": [{"name": "youtab:internal-copy", "value": image["internal_copy"]}],
        })
    binaries = json.loads((base / "binaries.lock.json").read_text(encoding="utf-8"))
    for asset in binaries["assets"]:
        ref = f"urn:youtab:file:{asset['sha256']}"
        rows.append({
            "type": "file",
            "name": asset["name"],
            "version": asset["sha256"][:12],
            "bom-ref": ref,
            "hashes": [{"alg": "SHA-256", "content": asset["sha256"]}],
            "properties": [{"name": "youtab:internal-path", "value": asset["internal_path"]}],
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()
    components = python_components() + npm_components() + manifest_components()
    components.sort(key=lambda row: row["bom-ref"])
    serial_seed = "\n".join(row["bom-ref"] for row in components) + commit
    serial = hashlib.sha256(serial_seed.encode()).hexdigest()
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{serial[:8]}-{serial[8:12]}-{serial[12:16]}-{serial[16:20]}-{serial[20:32]}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "youtab-agent-runtime",
                "version": commit,
                "bom-ref": f"pkg:github/eimanghazaei/youtab-agent-runtime@{commit}",
                "properties": [
                    {"name": "youtab:git-tree", "value": tree},
                    {"name": "youtab:uv-lock-sha256", "value": digest(ROOT / "uv.lock")},
                    {"name": "youtab:npm-lock-sha256", "value": digest(ROOT / "package-lock.json")},
                ],
            },
            "properties": [
                {"name": "youtab:scope", "value": "source-and-declared-dependencies"},
                {"name": "youtab:runtime-image-sbom-required-separately", "value": "true"},
            ],
        },
        "components": components,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(components)} components to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
