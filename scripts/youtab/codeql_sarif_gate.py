"""Fail closed on CodeQL SARIF while repository Code Scanning is unavailable."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def inspect(directory: Path) -> int:
    paths = sorted(directory.glob("*.sarif"))
    if not paths:
        print(f"CodeQL SARIF missing in {directory}", file=sys.stderr)
        return 2

    findings: list[tuple[str, str, int, str]] = []
    try:
        for path in paths:
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("version") != "2.1.0":
                raise ValueError(f"{path}: unsupported SARIF version")
            runs = document["runs"]
            if not isinstance(runs, list) or not runs:
                raise ValueError(f"{path}: no analysis runs")
            for run in runs:
                if ((run.get("tool") or {}).get("driver") or {}).get("name") != "CodeQL":
                    raise ValueError(f"{path}: analysis is not from CodeQL")
                results = run["results"]
                if not isinstance(results, list):
                    raise ValueError(f"{path}: invalid results")
                for result in results:
                    if any(item.get("status") == "accepted" for item in result.get("suppressions", [])):
                        continue
                    location = ((result.get("locations") or [{}])[0]
                                .get("physicalLocation") or {})
                    artifact = location.get("artifactLocation") or {}
                    region = location.get("region") or {}
                    findings.append((
                        str(result.get("ruleId", "unknown")),
                        str(artifact.get("uri", "unknown")),
                        int(region.get("startLine") or 0),
                        str((result.get("message") or {}).get("text", "")),
                    ))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"CodeQL SARIF could not be verified: {exc}", file=sys.stderr)
        return 2

    for rule, file, line, message in findings[:100]:
        print(f"{file}:{line}: {rule}: {message[:300]}", file=sys.stderr)
    print(f"CodeQL SARIF: {len(findings)} unsuppressed finding(s) across {len(paths)} file(s)")
    if len(findings) > 100:
        print(f"... {len(findings) - 100} more findings", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: codeql_sarif_gate.py SARIF_DIRECTORY", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(inspect(Path(sys.argv[1])))
