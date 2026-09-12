#!/usr/bin/env python3
"""Reject incomplete or expired dependency-risk exceptions."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


REQUIRED = {
    "id", "owner", "ecosystem", "package", "advisories", "severity",
    "scope", "reachability", "mitigations", "accepted_on", "expires_on",
    "upstream_fix",
}
MAX_EXCEPTION_DAYS = 45


def validate(path: Path, today: date) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    seen: set[str] = set()
    for entry in data.get("exceptions", []):
        missing = sorted(REQUIRED - set(entry))
        exception_id = str(entry.get("id", "<missing-id>"))
        if missing:
            errors.append(f"{exception_id}: missing fields: {', '.join(missing)}")
            continue
        if exception_id in seen:
            errors.append(f"{exception_id}: duplicate id")
        seen.add(exception_id)
        accepted = date.fromisoformat(entry["accepted_on"])
        expires = date.fromisoformat(entry["expires_on"])
        if expires < today:
            errors.append(f"{exception_id}: expired on {expires.isoformat()}")
        if (expires - accepted).days > MAX_EXCEPTION_DAYS:
            errors.append(f"{exception_id}: lifetime exceeds {MAX_EXCEPTION_DAYS} days")
        if not entry["advisories"] or not entry["mitigations"]:
            errors.append(f"{exception_id}: advisories and mitigations must be non-empty")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy", type=Path, default=Path("security/dependency-exceptions.json")
    )
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    errors = validate(args.policy, args.today)
    if errors:
        print("dependency exception gate: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("dependency exception gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
