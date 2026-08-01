#!/usr/bin/env python3
"""High-confidence secret scanner for source and configuration files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "openai-style-key": re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}
SKIP_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}
FIXTURE_PARTS = {"fixtures", "testdata", "snapshots"}


def _load_fixture_policy(root: Path) -> tuple[dict[tuple[str, str, str], dict[str, object]], list[str]]:
    policy_path = root / "security/secret-fixture-policy.json"
    if not policy_path.exists():
        return {}, ["missing security/secret-fixture-policy.json"]
    data = json.loads(policy_path.read_text(encoding="utf-8"))
    entries: dict[tuple[str, str, str], dict[str, object]] = {}
    errors: list[str] = []
    for entry in data.get("fixtures", []):
        key = (str(entry.get("path", "")), str(entry.get("kind", "")), str(entry.get("sha256", "")))
        if not all(key) or key in entries:
            errors.append(f"invalid or duplicate fixture policy entry: {key[0]}:{key[1]}")
            continue
        entries[key] = entry
    return entries, errors


def scan(root: Path) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    dispositions: list[dict[str, object]] = []
    fixture_policy, policy_errors = _load_fixture_policy(root)
    observed: Counter[tuple[str, str, str]] = Counter()
    matches: list[tuple[str, str, str, int]] = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        parts = set(path.relative_to(root).parts)
        if not path.is_file() or parts & SKIP_PARTS or parts & FIXTURE_PARTS:
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                relative = path.relative_to(root).as_posix()
                digest = hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()
                key = (relative, name, digest)
                observed[key] += 1
                matches.append((relative, name, digest, text.count("\n", 0, match.start()) + 1))

    for relative, name, digest, line in matches:
        entry = fixture_policy.get((relative, name, digest))
        if entry is None:
            findings.append({"kind": name, "path": relative, "line": line})
            continue
        dispositions.append(
            {
                "kind": name,
                "path": relative,
                "line": line,
                "reason": entry["reason"],
            }
        )

    for key, entry in fixture_policy.items():
        expected = int(entry.get("expected_occurrences", -1))
        actual = observed[key]
        if expected < 1 or actual != expected:
            policy_errors.append(
                f"fixture inventory mismatch: {key[0]}:{key[1]} expected={expected} actual={actual}"
            )

    return {
        "schema_version": 2,
        "files_scanned": scanned,
        "findings": findings,
        "fixture_dispositions": dispositions,
        "policy_errors": policy_errors,
        "passed": not findings and not policy_errors,
    }


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
