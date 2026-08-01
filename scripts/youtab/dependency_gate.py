#!/usr/bin/env python3
"""Verify dependency locks and immutable GitHub Action references."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path


SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
ACTION = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)


def scan(root: Path) -> dict[str, object]:
    errors: list[str] = []
    counts = {
        "python_artifacts": 0,
        "npm_registry_integrity": 0,
        "npm_lock_metadata": 0,
        "github_actions": 0,
    }
    uv = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    for package in uv.get("package", []):
        if "registry" not in package.get("source", {}):
            continue
        artifacts = ([package["sdist"]] if package.get("sdist") else []) + package.get("wheels", [])
        if not artifacts:
            errors.append(f"uv.lock missing artifacts: {package.get('name')}")
        for artifact in artifacts:
            counts["python_artifacts"] += 1
            if not SHA256.fullmatch(str(artifact.get("hash", ""))):
                errors.append(f"uv.lock missing SHA-256: {package.get('name')}")
    for lock in sorted(root.rglob("package-lock.json")):
        if "node_modules" in lock.parts:
            continue
        data = json.loads(lock.read_text(encoding="utf-8"))
        for location, package in data.get("packages", {}).items():
            if "node_modules/" not in location and not location.startswith("node_modules/"):
                continue
            resolved = str(package.get("resolved", ""))
            if package.get("link") or resolved.startswith(("file:", "workspace:")):
                continue
            if resolved.startswith(("http://", "https://")):
                if not package.get("integrity"):
                    errors.append(f"{lock.relative_to(root)} registry artifact missing integrity: {location}")
                else:
                    counts["npm_registry_integrity"] += 1
            elif not resolved and package.get("version"):
                # npm lockfile v3 emits metadata-only records for dependencies nested
                # inside an integrity-protected optional package (not separate fetches).
                counts["npm_lock_metadata"] += 1
            elif package.get("version"):
                errors.append(f"{lock.relative_to(root)} unsupported dependency source: {location}: {resolved}")
    for workflow in sorted((root / ".github/workflows").glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        for match in ACTION.finditer(text):
            action = match.group(1)
            if action.startswith("./"):
                continue
            counts["github_actions"] += 1
            ref = action.rsplit("@", 1)[-1] if "@" in action else ""
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                errors.append(f"un-pinned action: {workflow.relative_to(root)}: {action}")
    return {"schema_version": 1, "counts": counts, "errors": errors, "passed": not errors}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = scan(args.root.resolve())
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
