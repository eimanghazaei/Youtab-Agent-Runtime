#!/usr/bin/env python3
"""Deterministic dependency-lock and release-admission checks for Youtab."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ACTION = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)


def check_uv_lock(errors: list[str], counts: dict[str, int]) -> None:
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = data.get("package", [])
    registry = 0
    for package in packages:
        source = package.get("source", {})
        if "registry" not in source:
            continue
        registry += 1
        artifacts = []
        if package.get("sdist"):
            artifacts.append(package["sdist"])
        artifacts.extend(package.get("wheels", []))
        if not artifacts:
            errors.append(f"uv.lock: no artifacts for {package.get('name')}")
        for artifact in artifacts:
            digest = str(artifact.get("hash", "")).removeprefix("sha256:")
            if not SHA256.fullmatch(digest):
                errors.append(f"uv.lock: bad/missing SHA-256 for {package.get('name')}")
    counts["python_registry_packages"] = registry


def walk_npm_lock(path: Path, errors: list[str]) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    locked = 0
    for location, package in data.get("packages", {}).items():
        if not location or package.get("link"):
            continue
        # npm workspaces are source directories in this repository, not
        # downloaded registry packages, and therefore have no integrity field.
        if "node_modules/" not in location and not location.startswith("node_modules/"):
            continue
        resolved = str(package.get("resolved", ""))
        if resolved.startswith(("file:", "workspace:")):
            continue
        if package.get("version") and not package.get("integrity"):
            errors.append(f"{path.relative_to(ROOT)}: missing integrity at {location}")
        else:
            locked += 1
    return locked


def check_actions(errors: list[str], counts: dict[str, int]) -> None:
    external = 0
    for workflow in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        for match in ACTION.finditer(workflow.read_text(encoding="utf-8")):
            value = match.group(1)
            if value.startswith("./"):
                continue
            external += 1
            ref = value.rsplit("@", 1)[-1] if "@" in value else ""
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                errors.append(f"{workflow.relative_to(ROOT)}: action not SHA-pinned: {value}")
    counts["external_github_actions"] = external


def check_manifests(errors: list[str], blockers: list[str], counts: dict[str, int]) -> None:
    base = ROOT / ".youtab/supply-chain"
    for filename, key in (("containers.lock.json", "images"), ("binaries.lock.json", "assets")):
        data = json.loads((base / filename).read_text(encoding="utf-8"))
        rows = data.get(key, [])
        counts[key] = len(rows)
        for row in rows:
            if not SHA256.fullmatch(str(row.get("sha256", ""))):
                errors.append(f"{filename}: invalid SHA-256 for {row.get('name') or row.get('role')}")
        blockers.extend(f"{filename}: {item}" for item in data.get("blockers", []))


def check_brand_artifacts(paths: list[Path], errors: list[str]) -> None:
    forbidden = re.compile(r"\b(?:Hermes|Nous\s*Research|NousResearch)\b", re.IGNORECASE)
    for path in paths:
        if not path.exists():
            errors.append(f"public branding artifact missing: {path}")
            continue
        candidates = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        for candidate in candidates:
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if forbidden.search(text):
                errors.append(f"public branding leak: {candidate}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("source", "release"), default="source")
    parser.add_argument("--public-artifact", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    errors: list[str] = []
    blockers: list[str] = []
    counts: dict[str, int] = {}
    check_uv_lock(errors, counts)
    npm_locks = [
        ROOT / "package-lock.json",
        ROOT / "website/package-lock.json",
        ROOT / "scripts/whatsapp-bridge/package-lock.json",
        ROOT / "plugins/platforms/photon/sidecar/package-lock.json",
    ]
    counts["npm_integrity_entries"] = sum(walk_npm_lock(p, errors) for p in npm_locks)
    check_actions(errors, counts)
    check_manifests(errors, blockers, counts)
    check_brand_artifacts(args.public_artifact, errors)

    if args.mode == "release":
        required = {
            "YOUTAB_INTERNAL_OCI_PROOF": "internal OCI copy/signature proof",
            "YOUTAB_RUNTIME_IMAGE_SBOM": "runtime image SBOM",
            "YOUTAB_SIGNED_PROVENANCE": "signed provenance",
            "YOUTAB_OFF_GITHUB_BUNDLE_PROOF": "off-GitHub bundle proof",
        }
        import os

        for env_name, description in required.items():
            value = os.environ.get(env_name)
            if not value or not Path(value).is_file():
                errors.append(f"release proof missing: {description} ({env_name})")
        errors.extend(blockers)

    result = {
        "schema_version": 1,
        "mode": args.mode,
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "counts": counts,
        "errors": errors,
        "known_release_blockers": blockers,
        "passed": not errors,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
