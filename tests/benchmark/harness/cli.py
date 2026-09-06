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
``--secret-file``. The secret is read (strict tier — owner-only 0400 / protected
DACL) only to construct the signing client; it is NEVER logged, displayed, copied
or persisted by this tool.

Live-benchmark safety (WAVE-30B): a live run is bounded by an explicit stage
profile (``--stage canary|pilot|full``) or per-limit flags, gated by an
authenticated runtime preflight (``--expected-sha`` must match the running build;
redaction + budget enforcement must be on), and an output directory that is
outside the repo and not cloud-synced. A Canary runs EXACTLY one selected
scenario with one request and no retry/failover, and cannot silently expand to
the full bank.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .preflight import (
    STAGE_PROFILES,
    PreflightError,
    assert_engine_attestation,
    assert_live_safety,
    validate_output_dir,
    verify_runtime_sha,
)
from .recorder import Recorder
from .runner import Runner
from .schema import (
    MODE_DETERMINISTIC,
    MODE_LOCAL_RUNTIME,
    MODE_REAL_PROVIDER,
    Scenario,
)
from .taskbank import MIN_SCENARIOS, validate

_LIVE_MODES = {MODE_LOCAL_RUNTIME, MODE_REAL_PROVIDER}

_LIMIT_FLAG_TO_KEY = {
    "max_input_tokens": "max_input_tokens",
    "max_output_tokens": "max_output_tokens",
    "max_total_tokens": "max_total_tokens",
    "max_iterations": "max_iterations",
    "max_requests": "max_requests",
    "max_retries": "max_retries",
    "max_runtime_seconds": "max_runtime_seconds",
    "max_concurrency": "max_concurrency",
    "failure_threshold": "failure_threshold",
}


def _read_secret_file(path: Path) -> str:
    """Read the service secret from an Owner-controlled file with the hardened
    strict-tier loader (owner-only 0400 / protected DACL, no symlink, no BOM, not
    inside the repo or a cloud-synced folder). The value is returned for in-memory
    use only and never echoed."""
    from youtab_agent_cli.secret_file import SecretFileError, read_secret_file

    try:
        return read_secret_file(
            str(path),
            var="YOUTAB_AGENT_RUNTIME_SERVICE_SECRET",
            require_secure_perms=True,
            forbid_repo_and_cloud=True,
        )
    except SecretFileError as exc:
        raise SystemExit(f"secret file rejected: {exc}") from exc


def _build_limits(args) -> Optional[dict]:
    """Assemble the per-run limit dict from an optional stage profile + explicit
    per-limit flags (flags override the profile)."""
    limits: dict = {}
    if args.stage:
        limits.update(STAGE_PROFILES[args.stage]["limits"])
    for flag, key in _LIMIT_FLAG_TO_KEY.items():
        val = getattr(args, flag, None)
        if val is not None:
            limits[key] = val
    if args.max_cost_eur is not None:
        limits["max_cost_eur"] = args.max_cost_eur
    return limits or None


def _select_scenarios(scenarios: List[Scenario], args) -> List[Scenario]:
    """Apply --scenario / --limit / --canary selection with fail-closed checks."""
    selected = scenarios
    if args.scenario:
        by_id = {s.id: s for s in scenarios}
        missing = [sid for sid in args.scenario if sid not in by_id]
        if missing:
            raise SystemExit(f"unknown scenario id(s): {missing}")
        selected = [by_id[sid] for sid in args.scenario]
    if args.limit is not None:
        if args.limit <= 0:
            raise SystemExit("--limit must be positive")
        selected = selected[: args.limit]
    if args.canary:
        # A Canary is EXACTLY one explicitly selected scenario; it must never
        # silently expand to the bank.
        if not args.scenario and args.limit is None:
            raise SystemExit(
                "--canary requires an explicit --scenario <id> (or --limit 1); "
                "refusing to run a canary over the whole bank"
            )
        if len(selected) != 1:
            raise SystemExit(
                f"--canary must select exactly ONE scenario (selected {len(selected)})"
            )
    return selected


def _build_seam(args, repo_root: Path, limits: Optional[dict], *,
                engine: Optional[str] = None, require_engine: bool = False):
    if args.mode == MODE_DETERMINISTIC:
        return None  # runner builds the deterministic substrate seam
    if not args.base_url or not args.secret_file:
        raise SystemExit(
            f"mode {args.mode!r} requires --base-url and --secret-file")
    from .seam import HttpRuntimeSeam

    secret = _read_secret_file(Path(args.secret_file))
    return HttpRuntimeSeam(
        args.base_url, secret, tenant=args.tenant, user=args.user, limits=limits,
        engine=engine, require_engine=require_engine)


def _run(args) -> int:
    repo_root = Path(__file__).resolve().parents[3]
    manifest = Path(args.manifest) if args.manifest else None

    is_live = args.mode in _LIVE_MODES
    if args.canary and not args.stage:
        args.stage = "canary"
    limits = _build_limits(args)

    # A subset run (canary / explicit selection / pilot) bypasses ONLY the >=40
    # floor + family coverage; an official full run keeps the full-bank contract.
    subset_mode = bool(args.canary or args.scenario or args.limit is not None
                       or args.stage in {"canary", "pilot"})
    scenarios: List[Scenario] = validate(manifest, require_full_bank=not subset_mode)

    if not subset_mode and len(scenarios) < args.min_scenarios:
        print(f"FATAL: {len(scenarios)} scenarios < required {args.min_scenarios}",
              file=sys.stderr)
        return 2

    scenarios = _select_scenarios(scenarios, args)
    if not scenarios:
        print("FATAL: no scenarios selected", file=sys.stderr)
        return 2

    # Canary hard caps: one request, no retry/failover, concurrency 1.
    if args.canary:
        args.max_workers = 1

    # Load the dual-track contract EARLY (before the seam) so a live Track A run
    # can bind the track's engine profile (WAVE-30D §B1). ``track_prov`` is the
    # per-record provenance stamp (WAVE-30C §2); ``track_engine`` is the engine
    # the live seam pins.
    track = None
    track_prov = None
    track_engine = None
    if args.track:
        from .tracks import ComparabilityError, load_track, track_provenance
        try:
            track = load_track(args.track)
        except ComparabilityError as exc:
            print(f"FATAL: --track rejected: {exc}", file=sys.stderr)
            return 6
        track_prov = track_provenance(track)
        track_engine = (track.get("per_track", {}) or {}).get("engine_profile")

    # A live local_runtime run MUST bind an engine (Track A eco.v01) — never
    # dispatch on the worker's default model (WAVE-30D §B1).
    if args.mode == MODE_LOCAL_RUNTIME and not track_engine:
        print("FATAL: --mode local_runtime requires an engine-bound track "
              "(--track A); refusing to run on the worker's default model",
              file=sys.stderr)
        return 6

    # Live-run safety gate (WAVE-30B §12/§13): validate the output dir and verify
    # the runtime is the authorized build with a sound safety posture BEFORE any
    # provider call.
    if is_live:
        try:
            out_dir = validate_output_dir(Path(args.out), repo_root=repo_root,
                                          force=args.force)
        except PreflightError as exc:
            print(f"FATAL: output dir rejected: {exc}", file=sys.stderr)
            return 6
        args.out = str(out_dir)
        if not args.expected_sha:
            print("FATAL: a live run requires --expected-sha (the authorized SHA)",
                  file=sys.stderr)
            return 6

    seam = _build_seam(args, repo_root, limits, engine=track_engine,
                       require_engine=(args.mode == MODE_LOCAL_RUNTIME))

    if is_live and seam is not None:
        try:
            posture = seam.preflight()
            assert_live_safety(posture)
            verify_runtime_sha(
                posture,
                expected_sha=args.expected_sha,
                repo_root=repo_root,
                require_clean_worktree=args.require_clean_worktree,
            )
            # Verify the runtime's EFFECTIVE engine binding matches the Track A
            # contract before the first task (engine/provider/model/endpoint-class/
            # cost-policy + optional model digest) — fail closed on any mismatch.
            if args.mode == MODE_LOCAL_RUNTIME and track is not None:
                assert_engine_attestation(
                    posture, track,
                    expected_model_digest=args.expected_model_digest,
                )
        except PreflightError as exc:
            seam.close()
            print(f"FATAL: preflight refused the live run: {exc}", file=sys.stderr)
            return 6
        except Exception as exc:  # noqa: BLE001 - any preflight failure is fatal
            seam.close()
            print(f"FATAL: preflight could not be verified: {exc}", file=sys.stderr)
            return 6

    recorder = Recorder(Path(args.out))
    runner = Runner(recorder, mode=args.mode, seam=seam, repo_root=repo_root,
                    tenant=args.tenant, user=args.user, track_provenance=track_prov)

    rows = runner.run_all(scenarios, repetitions=args.repetitions,
                          max_workers=args.max_workers)
    summary = recorder.finalize()

    by_id = {s.id: s for s in scenarios}
    print(json.dumps({
        "generated_label": summary["generated_label"],
        "runtime_head": summary["runtime_head"],
        "mode": args.mode,
        "track": args.track,
        "stage": args.stage,
        "scenarios_run": len(scenarios),
        "total_records": summary["total_records"],
        "verdict_counts": summary["verdict_counts"],
        "families_covered": len(summary["families_covered"]),
        "families_missing": summary["families_missing"],
        "honesty_divergence_count": summary["honesty_divergence_count"],
        "parity_mismatches": list(summary["parity_mismatches"].keys()),
        "limits": limits,
    }, indent=2))

    exit_code = 0
    # Family-coverage completeness is only required for a full official run.
    if not subset_mode and summary["families_missing"]:
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

    if seam is not None:
        seam.close()

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
    run.add_argument("--track", choices=["A", "B"], default=None,
                     help="stamp records with a dual-track identity "
                          "(A=local/ECO, B=cloud); see tests/benchmark/COMPARABILITY.md")
    run.add_argument("--tenant", default="bench-tenant")
    run.add_argument("--user", default="bench-user")
    run.add_argument("--base-url", default=None, help="runtime origin (non-deterministic)")
    run.add_argument("--secret-file", default=None,
                     help="Owner service-secret file (non-deterministic; never logged)")

    # Scenario selection + canary (WAVE-30B §7).
    run.add_argument("--scenario", action="append", default=None,
                     help="select a scenario by id (repeatable)")
    run.add_argument("--limit", type=int, default=None,
                     help="run only the first N selected scenarios")
    run.add_argument("--canary", action="store_true",
                     help="Stage-1 canary: EXACTLY one scenario, one request, no retry")

    # Stage profile + per-run limits (WAVE-30B §8/§10).
    run.add_argument("--stage", choices=sorted(STAGE_PROFILES.keys()), default=None,
                     help="apply a bounded stage limit profile (canary|pilot|full)")
    run.add_argument("--max-input-tokens", type=int, default=None)
    run.add_argument("--max-output-tokens", type=int, default=None)
    run.add_argument("--max-total-tokens", type=int, default=None)
    run.add_argument("--max-iterations", type=int, default=None)
    run.add_argument("--max-requests", type=int, default=None)
    run.add_argument("--max-retries", type=int, default=None)
    run.add_argument("--max-runtime-seconds", type=int, default=None)
    run.add_argument("--max-concurrency", type=int, default=None)
    run.add_argument("--max-cost-eur", default=None,
                     help="per-run cost cap in EUR (<= the €10 campaign ceiling)")
    run.add_argument("--failure-threshold", type=int, default=None)

    # Live-run gate (WAVE-30B §12/§13).
    run.add_argument("--campaign-id", default=None,
                     help="durable campaign id for the shared €10 budget ledger")
    run.add_argument("--expected-sha", default=None,
                     help="authorized runtime build SHA (required for a live run)")
    run.add_argument("--expected-model-digest", default=None,
                     help="Owner-supplied full 64-hex Ollama model manifest digest; "
                          "when set, a live Track A run verifies the runtime-attested "
                          "digest matches EXACTLY (no-prefix) before dispatch")
    run.add_argument("--require-clean-worktree", dest="require_clean_worktree",
                     action="store_true", default=True)
    run.add_argument("--no-require-clean-worktree", dest="require_clean_worktree",
                     action="store_false")
    run.add_argument("--force", action="store_true",
                     help="overwrite prior artifacts in --out (else refused)")

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
