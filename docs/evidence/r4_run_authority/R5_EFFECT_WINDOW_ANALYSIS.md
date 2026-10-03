# R5 — residual external-effect window after authority loss (trace, design, blocker)

Bounded task on the integrated durable branch (worktree `_wt/rt-durable-execution`,
HEAD `f86d64da6`). Traces ADR-0002 limitation #1 (the ~10–15 s effect window
after PostgreSQL advisory-lock loss), designs the smallest fence, and reports a
concrete boundary blocker. **No R5 GO. No push/PR/merge/deploy/install. No ADR
allocated or accepted. R4 worktree not touched.**

## What the window actually is (traced)

On authority loss the fail-stop is (my owned seam,
`gateway/platforms/api_server.py::_on_run_authority_lost`):
1. `agent.interrupt("durable run authority lost")` for each active run, then
2. `self._authority_fail_stop()` → `os._exit(75)`.

`AIAgent.interrupt()` (`run_agent.py:2937`) is **cooperative**: it aborts the
active LLM request socket and calls `tools.interrupt._set_interrupt(True, tid)`
for the execution thread + tracked tool-worker threads. It sets flags; it does
**not** synchronously reap OS descendants. Effect gates poll `is_interrupted()`
at boundaries (`agent/tool_executor.py`, `tools/environments/base.py` subprocess
wait loop, `tools/mcp_tool.py`, `tools/code_execution_tool.py`,
`tools/browser_tool.py`, …), and the environment wait loop is what eventually
`killpg`s a subprocess group (`tools/environments/local.py`).

The window therefore has two distinct parts:

- **A. Detection latency.** Authority loss is detected by the 2 s supervisor
  poll, a failing fenced write, or TCP loss (`tcp_user_timeout=10s`) — up to
  ~10–15 s on a silent partition. Until detection, writes/effects proceed
  normally. This is already *bounded* by the supervisor interval + keepalive
  settings in the PG authority (`durable_run_store_pg.PostgresInstanceAuthority`).
  Tightening it is a tuning tradeoff (false-positive fail-stops vs. window), not
  a fence.

- **B. Descendant survival across the fail-stop (the real gap).** `interrupt()`
  only flags; `os._exit(75)` fires immediately after, before the cooperative
  worker gates + `killpg` teardown can run. Tool effect sites spawn subprocesses
  with `start_new_session=True` / `CREATE_NEW_PROCESS_GROUP` (own
  session/process group, deliberately isolated from the parent), so `os._exit`
  **orphans** them. An orphaned descendant keeps running and can complete a NEW
  external effect after authority was lost.

## Proof (executable negative test)

`tests/durable_execution/test_r5_effect_fence.py` reproduces B deterministically
(no LLM/agent stack): a harness spawns a new-session grandchild that writes a
sentinel (the stand-in external effect) after a delay, then `os._exit(75)`s
without reaping it.
- `test_current_failstop_orphans_new_session_descendant_DOCUMENTS_GAP` — **PASSES**:
  the orphaned grandchild survives the fail-stop and writes the sentinel.
- `test_failstop_must_leave_no_descendant_effect` — **xfail (strict)**: the
  acceptance contract (no descendant effect after fail-stop), red until the
  teardown seam lands.

(The R4 live proof already showed part A + the durable side: after
`pg_terminate_backend`, the run never commits success and is set UNKNOWN. R5 adds
the descendant-effect dimension B, which durable state alone cannot close.)

## Smallest fence (design)

On authority loss, **before `os._exit`, synchronously tear down the descendant
process tree** so no orphan can act, then exit. Concretely:
1. Set the cooperative interrupt for every active run (already done via
   `agent.interrupt()`), AND
2. Invoke a **single synchronous descendant-teardown hook** that kills the whole
   descendant tree (POSIX: `killpg` each tool session/group; Windows: a job
   object with `KILL_ON_JOB_CLOSE`, or `taskkill /T`), bounded by a short
   deadline, THEN `os._exit(75)`.

Step 2 must be **one authority**, not a new parallel reaper. The mechanisms
already exist in the process-supervision layer — `tools/environments/local.py`
and `tools/mcp_tool.py` (`killpg`), `youtab_runtime/win_job_supervisor.py` (job
object). The fence is: expose one idempotent "reap all effect descendants now"
teardown in that layer and have the durable fail-stop call it before exit.

## Boundary determination → BLOCKER (owner decision required)

The durable fail-stop trigger (`_on_run_authority_lost`) is mine. The
**descendant-teardown mechanism is not** — it lives in `tools/environments`,
`tools/mcp_tool`, and `win_job_supervisor`, which are cross-cutting
process-supervision, not durable-execution modules. Implementing a reaper inside
durable-execution would create a **second process-supervision authority** that
duplicates and can race the existing `killpg`/job-object teardown — the same
"one authority, no parallel mechanism" principle just locked for D1.

Therefore the boundary is **not unambiguous**, and per the task I did **not**
implement across it. The open decision is an architecture one:

> **R5 decision needed:** where does the synchronous "reap all effect
> descendants on fail-stop" seam live, and who owns it? Recommended: a single
> idempotent teardown hook in the process-supervision layer
> (`tools/environments` + `win_job_supervisor`), invoked by the durable
> fail-stop before `os._exit`. This needs its own ADR-numbered decision and the
> owner's (Eiman's) sign-off; it is coordinated with Runtime Integration but not
> implemented here.

## What is safe to do now (and what is not)

- **Done (owned, additive):** the executable negative-test + this analysis
  (evidence only; no product behavior changed).
- **Not done (deliberately):** no reaper added to durable-execution (would be a
  false fence / second authority), no tuning of supervisor timeouts (a tradeoff,
  not a fix), no edits to `tools/` or the R4 worktree.
- **Secondary mitigation (noted, not a fence):** part A can be narrowed by the
  PG authority supervisor interval / keepalive, but that only shrinks the window;
  B still needs the teardown seam.

## Coordination

Sent to Runtime Integration: R5 confirms an orphaned-descendant effect window
that durable state cannot close; the fix is a single supervision-layer teardown
hook called by the fail-stop; requesting agreement on ownership + a new ADR
number (not ADR-0002), pending Eiman's sign-off. No seam wired.
