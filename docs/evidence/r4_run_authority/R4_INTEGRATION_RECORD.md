# R4 integration record (Timeout / durable-execution owner)

Independent source+evidence review of the R4 durable run-authority correction and
its additive integration into the durable-execution owner branch. Nothing pushed,
merged to main, deployed or installed. ADR-0002 remains **Proposed** (owner
acceptance required). Product **NO-GO**.

## Integration
- Reviewed: fix `415a1734a49f04ceced1801f5ddaffd7ff36ee55`, evidence
  `a1f9e33428fcebf643fbe7df6e43882df0c0f3ad`, base correction `87b1deff5`, test
  exposure `2f71295d2`, on `release/r4-startup-failclosed`.
- My branch HEAD `656c371aa` was the exact merge-base of the R4 branch, so
  integration was a **clean fast-forward** (no cherry-pick, no new SHAs, no
  conflicts) preserving the R4 commits' committed history verbatim. The R4
  worktree (`_wt/runtime-r4-regressions`) was not touched.
- Integrated SHA (my branch `feat/runtime-durable-execution-v1`):
  **`a1f9e33428fcebf643fbe7df6e43882df0c0f3ad`**.

## Independent source review — PASS
- `youtab_runtime/durable_run_authority.py`: owner stamp `inst:<uuid>:<epoch>`
  (not PID → immune to PID reuse/namespace collision); SQLite OS file lock
  (msvcrt/fcntl); `mark_lost` fail-stop listeners; host/PID recorded as
  diagnostics only.
- `durable_run_store.py` (SQLite) + `durable_run_store_pg.py` (PostgreSQL): epoch
  table `runtime_authority`; `acquire_instance_authority` (file lock / advisory
  lock `pg_try_advisory_lock` on a dedicated session with keepalives +
  tcp_user_timeout=10s + statement_timeout=5s, epoch bump); `_fence` on every
  durable write (SQLite BEGIN IMMEDIATE epoch check; PG epoch row `FOR SHARE` +
  `pg_locks` advisory-lock-held check → `mark_lost`); `reconcile_prior_instances`
  moves other owners' nonterminal `operation='run'` rows to UNKNOWN, leaving
  ownerless/terminal/other-operation rows alone.
- `gateway/platforms/api_server.py`: authority acquired before readiness; gates
  `/health/ready`, admission (`ensure_held` + owner stamp), post-admission
  dispatch, and pre-execution; `_on_run_authority_lost` interrupts live agents
  and `os._exit(75)`; `connect()` releases authority on failed/cancelled start;
  `disconnect()` releases last. Persist-before-ack / commit-before-emit / no
  uncommitted result / UNKNOWN-not-retried invariants preserved.

## Independent retest on the integrated SHA — PASS
Image `youtab-agent-runtime:r4-integrated-a1f9e3342`, Id
`sha256:8eb7b3a5422be464e04432c84cd56da413baffd09248c58f996fd793fab904a5`,
embedded `/opt/youtab/.youtab_agent_build_sha` == integrated SHA (provenance
match). Build log `r4_integrated_build.log`.
- `tests/durable_execution/test_r4_run_authority.py`: **7 passed** against a real
  `postgres:16-alpine` on 127.0.0.1:55432 (3 SQLite + 4 PG: competing-instance
  refused, **same-PID prior-instance recovery**, **lock-loss fail-stop + next
  holder recovers**, HTTP/SSE terminal-follows-commit). 0 skipped.
- Full `tests/durable_execution/` on real PG: **102 passed, 2 skipped**; ruff clean.
- Live probe on the integrated image (own topology, real HTTP, s6 `gateway run`):
  admitted a slow run (RUNNING durably persisted, owner `inst:17e2eb3d…:1`,
  epoch 1), then `pg_terminate_backend` on the advisory-lock backend →
  gateway logged `authority LOST (epoch=1 … AdminShutdown)` + `fail-stopping`,
  process exited, s6 restarted, new holder took **epoch 2**, startup reconcile
  moved the run to **UNKNOWN**. Client `GET`/`/result` → `unknown`,
  `output:null`, `recovered_from_store:true`; PG row `UNKNOWN|NULL`. **No
  `run.completed` in the SSE stream. No late terminal ack** — the run stayed
  `UNKNOWN` at +10/+20/+30s past the mock's 25s window; whole-DB check found 0
  `SUCCEEDED` rows lacking a committed `status.completed` event.

## Limitations (unchanged from R4, explicitly carried)
1. **Effect window after loss** — a tool call already executing when the lock is
   lost can complete an external effect within the detection window (≈supervisor
   2s, up to ~10–15s on a silent partition). The run can never commit success and
   is marked UNKNOWN, but the effect is not undone. Closing this needs Lane-1
   effect-ledger fencing (out of R4).
2. **Topology is a product contract** — exactly one effectful /v1/runs per DB,
   `Recreate` rollout, direct/session-mode PG (no transaction-mode PgBouncer),
   one scope per DB. Multiplexed profiles sharing a store fail closed.
3. **Standby crash-loop noise** until takeover (correct fail-closed behavior).
4. **Legacy/rollback** — first R4 start marks pre-R4 `pid:` nonterminal rows
   UNKNOWN; after a rollback, `inst:` nonterminal rows need manual resolution.
5. **Environment/scope** — synthetic mock task on one Docker host; branding OCR
   and `uv` gates BLOCKED (environment); broad `unit-integration-e2e` gate FAILs
   pre-existing on this Windows host (POSIX-only tests), identical failure set on
   the unmodified base. **9/12 broad gates PASS is NOT treated as full green.**

## ADR recommendation
**Accept ADR-0002 (store-enforced singleton) as the R4 design.** It correctly
supersedes PID-based liveness, is fail-closed end-to-end, and is proven on real
PostgreSQL. Acceptance should be recorded together with the topology contract
(single effectful instance/DB, Recreate, direct PG DSN) as a hard deployment
constraint, and with the effect-window limitation explicitly deferred to the
Lane-1 effect ledger. Horizontal scale beyond one effectful instance remains a
separate multi-instance-protocol decision, not covered here.

## R1 §6 coordination (sent to Runtime Integration)
Raised four boundary points for confirmation: (1) execution transport = single
effectful /v1/runs per store; (2) RunStore Protocol now requires
acquire_instance_authority/reconcile_prior_instances (fail-closed without them);
(3) DSN must be direct/session-mode (no transaction-mode PgBouncer); (4) recovery
replay treats UNKNOWN as terminal-uncertain, not resumable. Awaiting their reply.
