#!/usr/bin/env python3
"""Run a blocking high-confidence SAST profile plus a heuristic inventory."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


SCAN_TARGETS = (
    "youtab_runtime",
    "agent",
    "gateway",
    "tools",
    "youtab_agent_cli",
    "plugins",
    "skills",
    "optional-skills",
    "acp_adapter",
    "cron",
    "providers",
    "scripts",
    "cli.py",
    "run_agent.py",
    "utils.py",
    "youtab_state.py",
    "youtab_state_common.py",
    "youtab_state_portability.py",
    "youtab_state_schema.py",
    "youtab_state_search.py",
)
BLOCKING = "S102,S202,S307,S314,S403,S405,S506,S602"
HEURISTIC = "S104,S105,S106,S107,S108,S310,S311,S324,S404,S603,S604,S605,S606,S607,S608"
NOQA = re.compile(r"#\s*noqa:\s*([^\n]+)", re.IGNORECASE)


def _ruff(root: Path, codes: str) -> tuple[int, list[dict[str, object]]]:
    python = os.environ.get("YOUTAB_AGENT_PYTHON") or sys.executable
    completed = subprocess.run(
        [
            python,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--select",
            codes,
            "--output-format",
            "json",
            *SCAN_TARGETS,
        ],
        cwd=root,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True,
        check=False,
    )
    try:
        findings = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        findings = [{"code": "SAST_TOOL_ERROR", "message": completed.stderr or completed.stdout}]
    return completed.returncode, findings


def _dispositions(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    selected = set(BLOCKING.split(","))
    for target in SCAN_TARGETS:
        path = root / target
        paths = [path] if path.is_file() else path.rglob("*.py") if path.exists() else []
        for source in paths:
            try:
                lines = source.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(lines, 1):
                match = NOQA.search(line)
                if not match:
                    continue
                codes = sorted(selected & set(re.findall(r"S\d{3}", match.group(1).upper())))
                if codes:
                    rows.append(
                        {
                            "path": source.relative_to(root).as_posix(),
                            "line": number,
                            "codes": codes,
                            "reason": line.split("--", 1)[1].strip() if "--" in line else "inline reviewed disposition",
                        }
                    )
    return rows


def scan(root: Path) -> dict[str, object]:
    blocking_rc, blocking = _ruff(root, BLOCKING)
    heuristic_rc, heuristic = _ruff(root, HEURISTIC)
    return {
        "schema_version": 1,
        "engine": "ruff-flake8-bandit",
        "targets": list(SCAN_TARGETS),
        "blocking_profile": BLOCKING.split(","),
        "blocking_findings": blocking,
        "blocking_returncode": blocking_rc,
        "reviewed_inline_dispositions": _dispositions(root),
        "heuristic_inventory": {
            "total": len(heuristic),
            "by_code": dict(sorted(Counter(str(item.get("code")) for item in heuristic).items())),
            "tool_returncode": heuristic_rc,
        },
        "passed": blocking_rc == 0 and not blocking,
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
