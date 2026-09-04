"""Benchmark CLI entry point.

Run from the repo root:

    python -m tests.benchmark.harness.cli run --mode deterministic --out .bench

Deterministic mode needs no provider, no network and no secrets. It is the
CI-required mode. ``--check-expected`` (default in deterministic mode) turns the
run into a self-verifying gate: the observed verdict of every scenario must equal
the verdict a correctly-functioning runtime should yield, so a WAVE-26 substrate
regression fails the gate. It also enforces the ``>=40`` scenario floor and fails
if ANY run raised a harness error.

Real-provider / isolated-local modes drive the durable ``/api/runtime/v1`` plane
through the real signing client and require an Owner-supplied ``--base-url`` and
``--secret-file``. The secret is read from the file only to construct the signing
client; it is NEVER logged, displayed, copied or persisted by this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

from .recorder import Recorder
from .runner import Runner
from .schema import (
    MODE_DETERMINISTIC,
    MODE_LOCAL_RUNTIME,
    MODE_REAL_PROVIDER,
    Scenario,
)
from .taskbank import MIN_SCENARIOS, validate


def _read_secret_file(path: Path) -> str:
    """Read a service secret from an Owner-controlled file. The value is returned
    for in-memory use only and never echoed."""
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise SystemExit(f"secret file {path} is empty")
    return secret


def _build_seam(args, repo_root: Path):
    if args.mode == MODE_DETERMINISTIC:
        return None  # runner builds the deterministic substrate seam
    if not args.base_url or not args.secret_file:
        raise SystemExit(
            f"mode {args.mode!r} requires --base-url and --secret-file")
    from .seam import HttpRuntimeSeam

    secret = _read_secret_file(Path(args.secret_file))
    return HttpRuntimeSeam(
        args.base_url, secret, tenant=args.tenant, user=args.user)


def _run(args) -> int:
    repo_root = Path(__file__).resolve().parents[3]
    manifest = Path(args.manifest) if args.manifest else None
    scenarios: List[Scenario] = validate(manifest)  # enforces >=40 + coverage

    if len(scenarios) < args.min_scenarios:
        print(f"FATAL: {len(scenarios)} scenarios < required {args.min_scenarios}",
              file=sys.stderr)
        return 2

    recorder = Recorder(Path(args.out))
    seam = _build_seam(args, repo_root)
    runner = Runner(recorder, mode=args.mode, seam=seam, repo_root=repo_root,
                    tenant=args.tenant, user=args.user)

    rows = runner.run_all(scenarios, repetitions=args.repetitions,
                          max_workers=args.max_workers)
    summary = recorder.finalize()

    by_id = {s.id: s for s in scenarios}
    print(json.dumps({
        "generated_label": summary["generated_label"],
        "runtime_head": summary["runtime_head"],
        "mode": args.mode,
        "total_records": summary["total_records"],
        "verdict_counts": summary["verdict_counts"],
        "families_covered": len(summary["families_covered"]),
        "families_missing": summary["families_missing"],
        "honesty_divergence_count": summary["honesty_divergence_count"],
        "parity_mismatches": list(summary["parity_mismatches"].keys()),
    }, indent=2))

    exit_code = 0
    if summary["families_missing"]:
        print(f"FATAL: families missing: {summary['families_missing']}",
              file=sys.stderr)
        exit_code = 3

    # Any harness error is a hard failure (a scenario that could not be judged
    # because the harness itself broke — never allowed to read green).
    harness_errors = [r for r in rows if (r.get("provenance") or {}).get("harness_error")]
    if harness_errors:
        for r in harness_errors:
            print(f"HARNESS-ERROR {r['scenario_id']}: "
                  f"{r['provenance']['harness_error']}", file=sys.stderr)
        exit_code = 4

    if args.check_expected:
        mismatches = [
            r for r in rows
            if not Runner.verdict_matches_expected(r, by_id[r["scenario_id"]])
        ]
        for r in mismatches:
            exp = by_id[r["scenario_id"]].expected_verdict
            print(f"UNEXPECTED-VERDICT {r['scenario_id']}: got {r['verdict']!r} "
                  f"expected {exp!r} — {r['reason']}", file=sys.stderr)
        if mismatches:
            exit_code = 5

    print(f"\nresults: {recorder.results_path}")
    print(f"summary: {recorder.summary_path}")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="benchmark", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run the benchmark")
    run.add_argument("--mode", choices=sorted(
        {MODE_DETERMINISTIC, MODE_LOCAL_RUNTIME, MODE_REAL_PROVIDER}),
        default=MODE_DETERMINISTIC)
    run.add_argument("--out", required=True, help="output dir for results")
    run.add_argument("--manifest", default=None, help="task manifest path")
    run.add_argument("--repetitions", type=int, default=1)
    run.add_argument("--max-workers", type=int, default=1,
                     help="cross-scenario thread pool size (each run isolated)")
    run.add_argument("--min-scenarios", type=int, default=MIN_SCENARIOS)
    run.add_argument("--tenant", default="bench-tenant")
    run.add_argument("--user", default="bench-user")
    run.add_argument("--base-url", default=None, help="runtime origin (non-deterministic)")
    run.add_argument("--secret-file", default=None,
                     help="Owner service-secret file (non-deterministic; never logged)")
    check = run.add_mutually_exclusive_group()
    check.add_argument("--check-expected", dest="check_expected",
                       action="store_true", default=None,
                       help="fail if any verdict != expected (default in deterministic)")
    check.add_argument("--no-check-expected", dest="check_expected",
                       action="store_false")

    validate_cmd = sub.add_parser("validate", help="validate the task bank only")
    validate_cmd.add_argument("--manifest", default=None)
    return p


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "validate":
        scenarios = validate(Path(args.manifest) if args.manifest else None)
        print(f"OK: {len(scenarios)} scenarios, all families covered "
              f"(>= {MIN_SCENARIOS})")
        return 0
    if args.cmd == "run":
        if args.check_expected is None:
            args.check_expected = (args.mode == MODE_DETERMINISTIC)
        return _run(args)
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
