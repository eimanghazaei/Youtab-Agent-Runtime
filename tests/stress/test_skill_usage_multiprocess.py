"""Multi-process stress regression for tools/skill_usage.py counter bumps.

Reproduces (and guards against a regression of) the silent lost-update flake:
N processes each do M ``bump_use`` calls against a FRESH ``YOUTAB_AGENT_HOME``
concurrently, repeated several rounds. The invariant is simple and total:

    final use_count == N * M  every round  (zero dropped increments)

The bug this pins down lived in ``_usage_file_lock``'s bootstrap. It seeded a
brand-new lock file with a TRUNCATING ``write_text(" ")``. When several
processes raced the very first touch of a fresh home, one process's truncating
write hit byte 0 while another held the byte-0 ``msvcrt.locking`` lock, so
Windows raised ERROR_LOCK_VIOLATION (PermissionError). ``_mutate`` swallows
that in its best-effort ``except`` and the increment was silently dropped while
the process still exited 0 — the classic 149/150.

The race lives entirely in the FIRST touch of a fresh home (once the lock file
exists the truncating branch is never taken again), so every worker is released
from a shared ``multiprocessing.Barrier`` to make all N hit that first touch
together — that is what makes the pre-fix drop reproducible. Against the pre-fix
bootstrap this FAILS (final < N*M on some round); with the create-if-missing
(no-truncate) bootstrap it PASSES every round.

Sizing note: N is deliberately kept at 8. Pushing the worker count higher
(>=12) starves ``msvcrt.locking``'s own bounded 10-attempt retry budget and
drops increments *independently of this bug* (a separate limitation of the
lock primitive, not addressed here) — which would make the test fail even on
fixed code. N=8 / M=25 sits below that starvation threshold, so post-fix runs
are clean, while the barrier keeps the first-touch collision reproducible.

Runtime is bounded but not instant: Windows ``spawn`` start-up dominates. Run
directly (opt-in; the stress suite is excluded from the default pytest run):

    python tests/stress/test_skill_usage_multiprocess.py
"""

import multiprocessing as mp
import os
import sys
import tempfile
from pathlib import Path


NUM_WORKERS = 8
BUMPS_PER_WORKER = 25
NUM_ROUNDS = 10
SKILL_NAME = "stress-skill"
WORKER_TIMEOUT_S = 30
WT = str(Path(__file__).resolve().parents[2])


def worker_bump(youtab_home: str, barrier, bumps: int) -> None:
    """Bump one skill's use_count ``bumps`` times against a shared home.

    Runs in a fresh spawned process. Waits on the shared ``barrier`` so all
    workers are released together and race the FIRST touch of the fresh home
    (where the lock file is bootstrapped and where the lost-update bug bit).
    """
    os.environ["YOUTAB_AGENT_HOME"] = youtab_home
    os.environ["HOME"] = youtab_home
    sys.path.insert(0, WT)

    from tools import skill_usage

    barrier.wait()

    for _ in range(bumps):
        skill_usage.bump_use(SKILL_NAME)


def _run_round(round_idx: int) -> tuple[bool, str]:
    """Run one race round against a fresh home. Returns (ok, detail)."""
    home = tempfile.mkdtemp(prefix=f"youtab_skill_usage_stress_{round_idx}_")

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(NUM_WORKERS)
    procs = []
    for _ in range(NUM_WORKERS):
        p = ctx.Process(
            target=worker_bump, args=(home, barrier, BUMPS_PER_WORKER)
        )
        p.start()
        procs.append(p)

    for p in procs:
        p.join(timeout=WORKER_TIMEOUT_S)
        if p.is_alive():
            p.terminate()
            p.join()
            return False, f"round {round_idx}: a worker hung and was terminated"

    exit_bad = [p.exitcode for p in procs if p.exitcode not in (0, None)]
    if exit_bad:
        return False, f"round {round_idx}: worker exit codes {exit_bad}"

    # Read the final counter through the same module the workers wrote with.
    os.environ["YOUTAB_AGENT_HOME"] = home
    os.environ["HOME"] = home
    sys.path.insert(0, WT)
    from tools import skill_usage

    rec = skill_usage.load_usage().get(SKILL_NAME) or {}
    final = int(rec.get("use_count") or 0)
    expected = NUM_WORKERS * BUMPS_PER_WORKER
    if final != expected:
        return False, (
            f"round {round_idx}: use_count={final} expected={expected} "
            f"({expected - final} increments silently dropped)"
        )
    return True, f"round {round_idx}: use_count={final} (ok)"


def main() -> None:
    print(
        f"skill_usage stress: {NUM_WORKERS} workers x {BUMPS_PER_WORKER} bumps "
        f"x {NUM_ROUNDS} rounds (expected {NUM_WORKERS * BUMPS_PER_WORKER}/round)"
    )
    failures = []
    for r in range(NUM_ROUNDS):
        ok, detail = _run_round(r)
        print(f"  {detail}")
        if not ok:
            failures.append(detail)

    print()
    if failures:
        print("=" * 60)
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("OK: ZERO DROPPED INCREMENTS across all rounds")


if __name__ == "__main__":
    main()
