#!/usr/bin/env python3
"""Create an unsigned in-toto/SLSA-compatible statement for later CI signing."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subjects = []
    for path in [ROOT / "uv.lock", ROOT / "package-lock.json", args.sbom]:
        subjects.append({"name": str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name), "digest": {"sha256": sha(path)}})
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://youtab.io/build-types/agent-runtime-source/v1",
                "externalParameters": {"gitCommit": commit},
                "internalParameters": {"network": "not-used-by-generator"},
                "resolvedDependencies": [
                    {"uri": "git+https://github.com/NousResearch/hermes-agent.git", "digest": {"gitCommit": "cc4cab2f592e60a197e796506de9168f74baf3ea"}}
                ],
            },
            "runDetails": {
                "builder": {"id": "https://youtab.io/builders/local-untrusted-bootstrap/v1"},
                "metadata": {"invocationId": commit},
            },
        },
        "youtab": {"signatureStatus": "unsigned-local-evidence; release gate requires CI signing"},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(statement, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
