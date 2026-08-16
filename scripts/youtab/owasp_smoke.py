#!/usr/bin/env python3
"""Deterministic OWASP/API/LLM smoke over the managed boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys


CASES = (
    "tests/youtab_runtime/test_contracts.py",
    "tests/youtab_runtime/test_policy.py",
    "tests/youtab_runtime/test_security.py",
    "tests/youtab_runtime/test_worker_e2e.py",
)


def main() -> int:
    python = os.environ.get("YOUTAB_AGENT_PYTHON") or sys.executable
    command = [python, "-m", "pytest", "-q", *CASES]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    result = {
        "schema_version": 1,
        "suite": "youtab-managed-owasp-smoke",
        "standards": [
            "OWASP Top 10:2025",
            "OWASP API Security Top 10:2023",
            "OWASP Top 10 for LLM Applications:2025",
        ],
        "deterministic_controls": [
            "signed-command-integrity",
            "tenant-and-task-isolation",
            "replay-rejection",
            "authority-non-escalation",
            "effect-proposal-only",
            "prompt-injection-non-escalation",
            "shared-resource-envelope",
            "ssrf-private-network-denial",
            "path-traversal-denial",
            "secret-output-redaction",
            "managed-worker-no-public-listener",
        ],
        "network_calls": 0,
        "external_effects": 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "passed": completed.returncode == 0,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
