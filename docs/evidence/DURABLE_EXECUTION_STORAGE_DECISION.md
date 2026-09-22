# Durable Execution — Canonical Storage Decision + Store Inventory (P0-A)

## Existing-store inventory (mechanical, base `c7650a1b`)

| Store | Location | Owns | Overlap with run execution? |
|---|---|---|---|
| Kanban board | `youtab_agent_cli/kanban_db.py` — `tasks`, `task_runs`, `task_events`, `task_links`, `task_comments`, `task_attachments` | Kanban **board/queue product** (statuses triage/todo/ready/running/review/done/blocked/archived), claim lease, retry breaker | Adjacent, NOT the API run authority. Board product, not `/v1/runs`. |
| Async delegations | `youtab_state_common.py` / `state.db` — `async_delegations` | Background delegation **identity + recoverable status** | Adjacent (delegation layer). |
| Response store | `gateway/platforms/api_server.py:639` `ResponseStore` → `response_store.db` | `/v1/responses` conversation/response snapshots (durable) | Different surface (`/v1/responses`), not `/v1/runs`. |
| Delivery ledger | `gateway/delivery_ledger.py` — `delivery_obligations` | Outbound reply effect ledger | Effect-delivery, not run execution. |
| Cron executions | `cron/executions.py` — `executions` | Scheduled-execution audit ledger | Audit, not interactive run. |
| Verification evidence | `agent/verification_evidence.py` — `verification_events`, `verification_state` | Verification evidence | Unrelated. |
| Session/message DB | `youtab_state_common.py` — `sessions`, `messages`, … | Conversation transcript | Transcript, not run execution state. |
| **`/v1/runs`** | `gateway/platforms/api_server.py` — `_run_statuses`/`_run_streams` (**in-memory dicts**) | Interactive API run status + SSE | **THE GAP: no durable store exists.** |

**Finding:** there is **no existing canonical durable store for `/v1/runs` execution
state** on base — it is RAM-only (proven lost on restart in P0-A/P0-H). There is
also no `run_journal` / `goal_store` / `agent_run_store` table on base. Therefore
`DurableRunStore` fills the gap; it is **not** a second authority duplicating an
existing run store. `run_journal`/`effect_ledger`/`worker_lease` are **Lane-1
deliverables absent on base** and are consumed via typed boundaries (IR-1/IR-3),
never re-created.

## Decision

- **Canonical run-execution authority = `RunStore`** (one typed interface,
  `youtab_runtime/durable_run_store.py`).
  - `SqliteRunStore` — local/offline **Desktop** backend (this base). Local
    correctness evidence only (fenced claim under DELETE mode), NOT multi-host
    scale evidence.
  - `PostgresRunStore` — **server/enterprise** backend (declared stub; same
    contract; integrated later). Selected via `create_run_store(backend=...)`.
  - **Exactly one authoritative store per deployment**; never two synchronized
    authorities.
- **Boundaries (no duplication):** Kanban stays the board product; ResponseStore
  stays `/v1/responses`; effect idempotency/receipts/UNKNOWN stay in the Lane-1
  effect ledger (IR-1); worker lease/reconciliation stay in Lane-1 (IR-3);
  memory stays in Memory/Simorgh; the Gateway owns signed identity/scope/authz;
  the UI is a consumer only. A Kanban task that spawns a run references it by id;
  neither syncs the other's authoritative state.

## Actual idempotency index (after P0-B fix, commit `a40fb392`)

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_idempotency
  ON runs(tenant_id, workspace_id, principal_id, operation, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```
- same scope + same `request_digest` → replay original run;
- same scope + different digest → `IdempotencyConflict` (fail closed);
- same key in another tenant/workspace/principal/operation → independent run;
- scoped lookup → no cross-tenant existence leak.
Concurrency: 6 racing scoped-idempotent creators → exactly one row (BEGIN IMMEDIATE serializes; the loser sees the winner's row and replays).

## Deadline matrix (explicitly separated; no universal max runtime)

| # | Deadline | Meaning | Owner | On expiry |
|---|---|---|---|---|
| 1 | `wait_timeout` (client/UI) | how long a caller waits synchronously | caller | end wait, return `WaitResult` (RUNNING+task_id+reconnect); task untouched |
| 2 | HTTP request timeout | one HTTP request lifetime | transport | connection ends; **never** cancels the run |
| 3 | parent waiting budget | how long a parent agent blocks on a child | parent | detach; return RUNNING+task_id; child continues under its own lease |
| 4 | `lease_expiry` (`ttl_seconds`) | worker ownership/fencing window | RunStore lease | fenced reclaim; stale epoch cannot commit |
| 5 | `execution_deadline` (persisted policy) | the task's own max allowed execution | run policy | controlled cancel/pause + recovery evidence (the ONLY time a duration stops a task) |
| 6 | approval deadline | how long a `WAITING_APPROVAL` gate waits | approval policy | typed expiry event; not a silent kill |
| 7 | provider/tool step deadline | one model/tool/network call | step caller | cancel/retry THAT step (idempotency-classified); task survives |

Only (5) `execution_deadline`, an explicit authorized cancellation, a safety
violation, or an unrecoverable terminal failure may stop a run. (1)(2)(3) and a
UI/SSE disconnect never do. A larger timeout is never the fix.
