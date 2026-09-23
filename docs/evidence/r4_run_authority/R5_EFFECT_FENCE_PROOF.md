# R5 — effect-descendant reap on authority-loss fail-stop (proof)

Owner-approved local design/implementation/test (`R5_PROCESS_SUPERVISION_OWNER_DECISION_2026-09-23.md`).
**Product NO-GO. R5 not closed** — the reap closes the post-detection descendant
window, not the detector's 2–15 s latency window (see §Evaluation). No
push/PR/merge/deploy/install. ADR number pending coordination (do NOT reuse ADR-0002).

| Item | Value |
|---|---|
| Source SHA (review-hardened) | `2cf4cdf47aa09d07c2f9c1b3f9ba2bfb6e626d3b`, tree `192e6dcdd437f82beea10cc9a3cb7012738044fa` |
| Image | `youtab-agent-runtime:r5-2cf4cdf47`, Id `sha256:7ef3318c20c0017a6fe427649ba794ddd95ed472d8d7687e5e78052ead44c352` |
| Embedded SHA | `/opt/youtab/.youtab_agent_build_sha` == source SHA (provenance match) |
| Build log | `docs/evidence/r4_run_authority/r5fix_image_build.log` (exit 0) |
| Prior (pre-review) source/image | `ac7bb5b1ebcb…` / `r5-ac7bb5b1e` (`sha256:ce121bdd…`) — superseded by the review fixes below |
| Raw evidence | `docs/evidence/r4_run_authority/R5_LIVE_RAW.txt` |

## Review fixes (R5_AC7_REVIEW_2026-09-23.md, sha256 C497F850…) — all four addressed
Reviewed at `ac7bb5b1`; fixed at `2cf4cdf47`. Full durable suite **108 passed / 2
skipped** on real PG; ruff clean. Tests in `tests/durable_execution/test_r5_effect_fence.py`.

1. **Enumeration failure → never false containment.** Enumeration is factored into
   `_enumerate_owned_descendants() -> (found, ok)`; a registry-snapshot or psutil
   failure sets `enumeration_ok=False` → `contained=False`. Test:
   `test_enumeration_failure_is_not_contained`.
2. **Unknown identity → never an unguarded kill.** A target is signalled ONLY when
   its start-time is known AND `_host_pid_is_ours` matches; a start-time-less
   target is recorded `unidentified`, never signalled, and forces
   `contained=False`. Test: `test_unknown_identity_is_never_killed_and_not_contained`
   (a real live child with forced-`None` identity is NOT killed; `contained=False`).
3. **Deadline bounds the WHOLE teardown.** `deadline_s` (default 8 s) spans all
   sweeps + terminations + settle; when spent, no further target is signalled and
   `contained=False` (`budget_exceeded`); `elapsed_s` is returned (worst-case
   overrun ≤ one in-flight `_terminate_host_pid` timeout). Test:
   `test_budget_exceeded_is_not_contained_and_does_not_signal` (deadline 0 → no
   signal, child alive, `elapsed_s ≤ 2s`).
4. **Child-spawn race → repeat sweep.** The reap loops enumerate→signal until a
   clean sweep finds no live owned descendant (or the deadline). A child spawned
   during teardown is caught next sweep; a killed process cannot spawn more, so
   the live set strictly shrinks and converges. Test:
   `test_child_spawn_race_is_caught_by_resweep` (a pid injected on sweep 2 is
   caught; `sweeps≥2`, both pids `verified_dead`, `contained=True`).

Re-proved on the fix image `r5-2cf4cdf47`:
- POSIX in-container real detached child: `contained=True enumeration_ok=True
  targets=1 signalled=[…] verified_dead=[…] sweeps=2 elapsed=0.103s` — effect
  sentinel NOT written.
- Live gateway fail-stop on real PG authority loss: `effect-descendant reap on
  fail-stop: contained=True targets=0 … enumeration_ok=True sweeps=1 elapsed=0.009s
  platform=posix` → restart → epoch 2 → run UNKNOWN. (`contained=True` here is
  truthful, not vacuous: enumeration SUCCEEDED and found no descendant.)

## What was built (single hook, no second reaper)
`ProcessRegistry.reap_effect_descendants()` in `tools/process_registry.py` — the
existing process-supervision authority. The durable fail-stop
(`gateway/platforms/api_server.py::_on_run_authority_lost`) calls it AFTER
`agent.interrupt()` (cooperative) and BEFORE `os._exit(75)`.

- Targets: (1) registry-tracked host sessions (catches descendants reparented to
  init) and (2) the live OS descendant tree of this process (catches untracked
  spawns: MCP, browser, code-exec, detached/new-session children).
- Reuses the registry's start-time-guarded `_terminate_host_pid` (POSIX psutil
  child-tree + SIGTERM→SIGKILL + killpg; Windows `taskkill /PID <pid> /T /F`).
- Bounded deadline; idempotent; start-time guard (`_host_pid_is_ours`) refuses a
  recycled PID / unrelated process — never kills a non-descendant.
- FAIL-CLOSED: returns `contained=False` with surviving owned PIDs when it cannot
  verify containment; logged CRITICAL, never reported as applied, never retried.

## Proof

### 1. Strict-xfail negative test → real pass (acceptance #1)
`tests/durable_execution/test_r5_effect_fence.py`:
- `..._DOCUMENTS_GAP`: a no-reap fail-stop orphans a detached descendant → it
  writes its effect sentinel after exit (the gap).
- `..._reaps_detached_descendant`: the REAL reap runs before exit → detached
  descendant killed, `contained=True`, sentinel absent (effect prevented).

### 2. POSIX real-descendant kill (Linux container, R5 image)
Detached child in its own process group (pgid == pid); reap → `contained=True`,
`killed=[child]`, `tree=1`, `platform=posix`; the child's delayed sentinel was
**never written**. POSIX process-group teardown exercised with a real kill.

### 3. Windows real-descendant kill (host)
`test_failstop_reaps_detached_descendant` runs on this Windows host: a
`CREATE_NEW_PROCESS_GROUP` child is killed by `taskkill /T /F`; sentinel absent.
Windows tree-kill teardown exercised with a real kill. (The `taskkill` primitive
also killed a sub-child in a direct probe — tree containment confirmed.)

### 4. Live gateway fail-stop on real PostgreSQL authority loss (acceptance #2, partial)
R5 image gateway + real PG + streaming mock, booted via s6 `gateway run`. Admitted
a run (RUNNING durably persisted), then `pg_terminate_backend` on the advisory-lock
backend:
- gw logged `authority LOST (epoch=1 … AdminShutdown)` → `fail-stopping` →
  **`effect-descendant reap on fail-stop: contained=True killed=0 tracked=0 tree=0
  platform=posix`** → restart → epoch 2 → `startup reconcile: 1 abandoned run
  moved to UNKNOWN`.
- Run terminal state (served from store): `status:unknown`, `terminal:false`,
  `output:null`, `recovered_from_store:true`; PG row `UNKNOWN|NULL`. No
  `run.completed`, no terminal success. R4 invariants preserved under the new code.
- The reap **fires in the real fail-stop path**. `tree=0` because this idle run
  had no effect-capable descendant. A descendant spawned by a real in-gateway
  agent tool (terminal/MCP) during a live run — killed in that same run — is the
  one end-to-end step NOT yet demonstrated (mechanically identical to §2/§3, which
  reap real OS descendants regardless of spawner). Recorded as a remaining item;
  it is gated by tool-call/approval orchestration, not by the reap.

## Evaluation — same-authority check before each external call
The reap closes descendant survival **after** authority loss is DETECTED. It does
NOT close the detector's ~2–15 s latency (supervisor 2 s poll + `tcp_user_timeout`
10 s), during which authority is not yet known lost and effects proceed normally.

A same-authority check immediately before each external call
(`authority.ensure_held()` at each effect site) would REFUSE to START a new effect
once loss is detected, narrowing the window — but it (a) is pervasive across all
tool/agent effect sites (large blast radius, cross-lane), (b) has a TOCTOU gap
(authority can drop between the check and the syscall), and (c) cannot abort an
effect already in-flight. Full closure requires effect-level fencing through the
Lane-1 effect ledger (the effect and the authority committed atomically), which is
out of R5. **Therefore R5 is NOT full GO while an effect can still occur during
the detection-latency window.** Path to closure: reap (R5, done) → pre-call
same-authority gate at effect admission (next, narrows latency) → Lane-1 effect
ledger (future, closes it).

## Platforms / limitations / remaining gates
- POSIX process-group teardown: proved live (Linux container). Windows tree-kill:
  proved on host. Windows Job Object (`youtab_runtime/win_job_supervisor`) is a
  separate existing mechanism; the reap reuses the registry's `taskkill /T /F`
  tree-kill (no second reaper), so the Job Object path is not additionally
  required here.
- Container/UID path (acceptance #3): the live probe ran the container as root;
  a non-root deployable UID path is NOT exercised — remaining.
- "Preserve unrelated processes": guaranteed structurally (only descendants of
  this process + registry-own sessions are enumerated) and by the start-time guard
  (a recycled/unrelated PID is refused — observed live). Not a dedicated live test.
- Full agent-tool-spawned descendant killed in a live run (acceptance #2 end-to-end):
  remaining (see §4).
- Broad gates unchanged from R4: branding OCR + `uv` BLOCKED (environment);
  `unit-integration-e2e` FAILs pre-existing POSIX-only on this Windows host,
  identical set on base. 9/12 NOT treated as full green.
- R1/R2/R3 remain separate gates; no 24-hour candidate freeze from this proof.

## Verdict
Descendant survival after DETECTED authority loss: **closed** (reap, proved on
POSIX + Windows, fires live). Detection-latency effect window: **open** (needs
pre-call gate + Lane-1). **R5 NOT closed; product NO-GO.** ADR to be drafted with
Runtime Integration under a coordinated new number (not ADR-0002); Proposed until
review.
