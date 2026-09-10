#!/usr/bin/env python3
"""Independent Agent Review gate — SHA-bound evidence recorder + commit-status poster.

Governance: docs/governance/INDEPENDENT_AGENT_REVIEW.md (Owner decision 2026-09-10,
replaces Greptile). This helper implements the NON-self-referential gate mechanism:

* ``record``  — write an append-only evidence record ``docs/governance/reviews/<sha>.json``
                for the exact reviewed head SHA (refuses to overwrite an existing one).
* ``verify``  — validate a record: exact-SHA match, allowed verdict, no unresolved
                Critical/High/Medium finding. Exits non-zero (fail closed) otherwise.
* ``post``    — post the ``Independent Agent Review`` commit status for the exact SHA
                via ``gh`` (state success only when ``verify`` passes).

The evidence record is recorded OUT OF BAND (it is never committed into the same SHA it
reviews). The commit status is the machine-enforced gate; branch protection requires the
``Independent Agent Review`` context (see policy §9 for the Owner/admin ruleset action).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

CONTEXT = "Independent Agent Review"
ALLOWED_VERDICTS = {"PROVEN", "FAILED", "NOT PROVEN", "BLOCKED"}
BLOCKING_SEVERITIES = {"critical", "high", "medium"}
REQUIRED_FIELDS = (
    "repository", "pr_number", "base_branch", "base_sha", "head_branch", "head_sha",
    "review_agent_identity", "review_timestamp_utc", "review_scope",
    "commands_and_tests_examined", "findings_by_severity", "unresolved_findings",
    "verdict",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LEDGER = _REPO_ROOT / "docs" / "governance" / "reviews"


def _record_path(head_sha: str) -> Path:
    return _LEDGER / f"{head_sha}.json"


def _load(head_sha: str) -> dict:
    p = _record_path(head_sha)
    if not p.exists():
        raise SystemExit(f"FAIL: no review record for {head_sha} ({p}) — gate pending")
    return json.loads(p.read_text(encoding="utf-8"))


def _gate_passes(rec: dict, head_sha: str) -> tuple[bool, list[str]]:
    """Return (passes, reasons_it_fails). Fail-closed on any doubt."""
    reasons: list[str] = []
    for f in REQUIRED_FIELDS:
        if f not in rec:
            reasons.append(f"missing field: {f}")
    if rec.get("head_sha") != head_sha:
        reasons.append(
            f"SHA mismatch: record head_sha={rec.get('head_sha')} != {head_sha}"
        )
    verdict = rec.get("verdict")
    if verdict not in ALLOWED_VERDICTS:
        reasons.append(f"verdict not in {sorted(ALLOWED_VERDICTS)}: {verdict!r}")
    if verdict != "PROVEN":
        reasons.append(f"verdict is not PROVEN (fail-closed): {verdict!r}")
    unresolved = rec.get("unresolved_findings") or []
    for finding in unresolved:
        sev = str((finding or {}).get("severity", "")).lower()
        if sev in BLOCKING_SEVERITIES:
            reasons.append(f"unresolved {sev} finding: {(finding or {}).get('summary')}")
    return (not reasons), reasons


def cmd_verify(args: argparse.Namespace) -> int:
    rec = _load(args.head_sha)
    passes, reasons = _gate_passes(rec, args.head_sha)
    if passes:
        print(f"PASS: Independent Agent Review PROVEN for {args.head_sha}")
        return 0
    print("FAIL (gate closed):", file=sys.stderr)
    for r in reasons:
        print(f"  - {r}", file=sys.stderr)
    return 1


def cmd_record(args: argparse.Namespace) -> int:
    _LEDGER.mkdir(parents=True, exist_ok=True)
    path = _record_path(args.head_sha)
    if path.exists():
        # Append-only: never overwrite an earlier review record.
        raise SystemExit(f"FAIL: review record already exists for {args.head_sha} "
                         f"({path}) — append-only, refusing to overwrite")
    rec = json.loads(Path(args.record).read_text(encoding="utf-8"))
    rec["head_sha"] = args.head_sha
    missing = [f for f in REQUIRED_FIELDS if f not in rec]
    if missing:
        raise SystemExit(f"FAIL: record missing required fields: {missing}")
    if rec.get("verdict") not in ALLOWED_VERDICTS:
        raise SystemExit(f"FAIL: verdict must be one of {sorted(ALLOWED_VERDICTS)}")
    path.write_text(json.dumps(rec, indent=2, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8")
    print(f"recorded {path}")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    rec = _load(args.head_sha)
    passes, reasons = _gate_passes(rec, args.head_sha)
    state = "success" if passes else "failure"
    desc = ("PROVEN — independent review passed" if passes
            else f"gate closed: {reasons[0] if reasons else 'not proven'}")[:140]
    proc = subprocess.run(
        ["gh", "api", "-X", "POST",
         f"repos/{args.repo}/statuses/{args.head_sha}",
         "-f", f"state={state}",
         "-f", f"context={CONTEXT}",
         "-f", f"description={desc}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"FAIL: could not post commit status ({proc.returncode})")
    print(f"posted status context={CONTEXT!r} state={state} sha={args.head_sha}")
    return 0 if passes else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="fail-closed gate check of a review record")
    v.add_argument("--head-sha", required=True)
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("record", help="write an append-only review evidence record")
    r.add_argument("--head-sha", required=True)
    r.add_argument("--record", required=True, help="path to the review-record JSON")
    r.set_defaults(func=cmd_record)

    o = sub.add_parser("post", help="post the Independent Agent Review commit status")
    o.add_argument("--head-sha", required=True)
    o.add_argument("--repo", default="eimanghazaei/Youtab-Agent-Runtime")
    o.set_defaults(func=cmd_post)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
