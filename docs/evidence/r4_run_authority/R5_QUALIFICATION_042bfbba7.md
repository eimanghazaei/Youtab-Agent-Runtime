# R5 containment qualification (Timeout/durable owner)

Bounded R5 qualification per `_evidence/master/R5_TIMEOUT_OWNER_QUALIFICATION_HANDOFF_2026-09-23.txt`.
Worktree `F:/Youtab_AI_COS_Platform/_wt/rt-durable-execution`. R4 owner base
`042bfbba7` preserved as comparison base. **R5 COMPONENT evidence only — NOT
Lane-1 effect ledger, NOT R1 approval, NOT cross-repository, NOT product GO. No
push, PR, protected merge, deploy or customer install.**

## State
- HEAD **`f4b7f28250d4b972d397a98928f17ca41a229c2d`**, tree
  **`9d339455056d64408d2f7169da77799314b5d1f4`**, branch
  `feat/runtime-durable-execution-v1`.
- R4 base `042bfbba7` is an ancestor; R5 fix commits `ac7bb5b1e` (reap),
  `2cf4cdf47` (review fixes), `cafdcb278` (proof) all present.
- Clean tracked state (only two untracked peer review docs, left untouched).

## Independent source verification of the four 2cf4cdf47 findings
Verified against `tools/process_registry.py` (`reap_effect_descendants`,
`_enumerate_owned_descendants`, `_host_pid_is_ours`, `_terminate_host_pid`):

| Finding | Fix in source | Verdict |
|---|---|---|
| 1. Enumeration failure must not report vacuous containment | `_enumerate_owned_descendants` returns `(found, ok=False)` if the registry snapshot OR `psutil` tree read raises (L687-690, L703-708); loop sets `enumeration_ok=False` and breaks (L765-767); `contained = enumeration_ok and …` (L806) | PASS |
| 2. Unknown PID identity → never signalled, not contained | `start is None → unidentified.add(pid); continue` (L774-776), never enters `live_owned`/terminate; `contained = … and not unidentified` (L809) | PASS |
| 3. Deadline bounds the WHOLE teardown | `end = started + max(deadline_s,0)` (L755); `time.monotonic() >= end` checked before the sweep's kills (L781) and before each terminate (L785); `budget_exceeded → contained False` (L810); `elapsed_s` returned (L812/823) | PASS |
| 4. Child-spawn race → re-sweep | `while True` re-enumerates each sweep (L763-764); ends only on clean sweep (L779-780) or budget; killed procs can't spawn → set shrinks/converges | PASS |
| Safety: preserve unrelated processes (PID reuse) | `_terminate_host_pid` re-validates kernel start-time before ANY signal and refuses on mismatch/dead (L589-597); reap only signals pids with a known, matching start-time | PASS |

## Pass/fail — executable
Env: worktree `.venv` (Python 3.12.10), real `postgres:16-alpine` on
127.0.0.1:55432 (`YOUTAB_TEST_PG_DSN`). Runner:
`TZ=UTC PYTHONHASHSEED=0 LANG=C.UTF-8 [YOUTAB_TEST_PG_DSN=…] .venv/Scripts/python.exe -m pytest <t> -p no:cacheprovider`.

| # | Check | Command | Result | Exit |
|---|---|---|---|---|
| 1 | Targeted R5 adversarial (4 findings + real detached-descendant reap + documents-gap) | `pytest tests/durable_execution/test_r5_effect_fence.py -v` | **6 passed** | 0 |
| 2 | Canonical R5/R4 regression (full durable suite, real PG) | `pytest tests/durable_execution -q` | **123 passed, 2 skipped** | 0 |
| 3 | Container probe — real detached descendant reaped in the fix-bearing image | `docker run --rm --entrypoint python <img> -c '<spawn detached gc; reap; check sentinel>'` | `contained=true, signalled=1, verified_dead=1, sweeps=2, elapsed_s=0.104, platform=posix, sentinel_exists=false` | 0 |
| 4 | Container probe — enumeration failure → unverified, not applied, no retry | `docker run --rm --entrypoint python <img> -c '<force enumerate=({},False); reap>'` | log `NOT contained (fail-closed) … not applied, no retry`; `enumeration_ok=false, contained=false, targets=0` | 0 |

Adversarial conditions forced (handoff list): process-enumeration failure (#3
targeted `test_enumeration_failure_is_not_contained` + container probe 4);
unavailable start-time / PID reuse (`test_unknown_identity_is_never_killed_and_not_contained`
+ the L589 kill-time guard); child spawning a descendant during teardown
(`test_child_spawn_race_is_caught_by_resweep`); an actual effectful worker losing
authority (`test_failstop_reaps_detached_descendant` reaps a real detached
grandchild via the exact fail-stop primitive so its sentinel effect never lands;
container probe 3 proves the same in the packaged image; and the R4 live probe on
this exact image showed the reap hook firing on real `pg_terminate_backend`
authority loss — `effect-descendant reap on fail-stop: contained=True`, see
R4_OWNER_ACCEPTANCE_042bfbba7.md §live).

Bounded synchronous pre-exit cleanup: confirmed — the reap runs to a clean sweep
or the `deadline_s` bound before `os._exit(75)`; `elapsed_s` is returned/observable
(0.104s in probe 3). Unknown containment (enumeration fail / unidentified /
budget) is reported `contained=false` and logged "not applied, no retry" — never
an applied receipt.

## Image
- `youtab-agent-runtime:r4int-042bfbba7`, Id
  `sha256:0f42a77f64f9bfe0e4f3b8be1bc12c93346870d72980d20439ff95a9fb457eb6`,
  embedded `/opt/youtab/.youtab_agent_build_sha == 042bfbba7…` (provenance match).
  (Local build; no registry RepoDigest — not pushed.)

## No new defect reproduced
Independent adversarial testing reproduced no residual defect in the four review
findings; all fail-closed and identity-safe as claimed. No product code changed.

## Remaining effect-level gap (explicit)
1. **Already-reparented, unregistered descendant.** A descendant that has ALREADY
   reparented to init (its intermediate parent died before the reap) AND is not
   registry-registered is neither in this process's child tree nor a tracked
   session, so it cannot be enumerated/reaped (documented at
   `process_registry.py` L744-748). Mitigation = spawners keep such workers
   registry-registered; the start-time guard is the catch-all. In the real
   fail-stop the reap runs while the gateway is still alive, so its descendants
   are still children (not yet reparented) — this gap is for pre-orphaned workers.
2. **In-flight effect during detection (ADR-0002 §residual).** An external effect
   whose syscall is ALREADY executing when authority is lost can complete within
   the detection/teardown window; R5 reaps descendants that have not yet acted or
   are still enumerable, narrowing but not eliminating the ~10-15s window. Closing
   it fully requires effect-level fencing (Lane-1 effect ledger) — **a separate
   open gate, out of R5 scope**; an ambiguous effect becomes UNKNOWN with
   reconciliation, never an automatic retry.

## Verdict
R5 containment component: the four review findings are independently verified in
source and by adversarial tests; targeted 6/6, regression 123/2 on real PG, and
the fix-bearing container probe shows real-descendant containment + fail-closed
unverified handling on image `sha256:0f42a77f…`. Bounded synchronous pre-exit
cleanup and no-applied-receipt-on-unknown hold. **R5 component PASS; effect-level
closure (Lane-1) remains open.** No push/merge/deploy/install; no product GO.
