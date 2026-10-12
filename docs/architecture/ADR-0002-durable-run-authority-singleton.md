# ADR-0002 — Durable `/v1/runs` server is a store-enforced singleton

**Status:** Proposed (owner acceptance required) · **Scope:** server durable mode
(`YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND` set), R4 enterprise release
**Supersedes:** PID-based owner liveness (`lease_owner=pid:<n>`) for `/v1/runs`
**Related:** `R4_RUN_OWNER_FENCING_DECISION_2026-09-23.md`, ADR-0001

## Context

Durable admission stamped runs `lease_owner=pid:<n>`, and startup treated
`pid == os.getpid()` as the only live owner. A numeric PID cannot tell a dead
prior process that had the same PID apart from a live peer. Container PID
namespaces make that worse, because every container's runtime is commonly PID 1
or 7. The result was that a same-PID orphan stayed `RUNNING` forever, and a
second replica on the same store marked a live peer's run `UNKNOWN`. Making
startup fail on recovery errors (`87b1deff5`) did not fix the ownership
question.

## Decision

**One Runtime instance at a time holds the exclusive run authority for a durable
store. The product enforces this; a deployment replica count does not.**

1. **Acquire before readiness.** On PostgreSQL the adapter constructor opens a
   dedicated autocommit session with TCP keepalives, `tcp_user_timeout=10s` and
   `statement_timeout=5s`. On that session it calls
   `pg_try_advisory_lock(0x59544152, crc32(scope) & 0x7fffffff)` without
   waiting, with scope `v1_runs`. SQLite takes an exclusive non-blocking OS
   lock on `<db>.v1_runs.authority.lock`. If another instance holds the lock,
   startup raises `AuthorityHeld`. A failure to acquire, or a store failure,
   also makes startup fail. `/health/ready` returns 503 unless the authority is
   held.
2. **Epoch and identity.** Each acquisition durably bumps
   `runtime_authority.epoch` and records a fresh random `instance_id`. Runs are
   stamped `inst:<instance_id>:<epoch>`. The host and PID are stored only as
   diagnostics and are never used to decide liveness.
3. **Recover only after exclusivity.** The new holder moves every nonterminal
   `operation='run'` row that another owner stamped to `UNKNOWN`, whatever PID
   it carried. It emits `state.unknown` with `reason=prior_instance_superseded`.
   Ownerless queued rows, terminal rows and other operations (for example local
   `delegate` children) are left unchanged. Nothing is resumed or resubmitted.
4. **Fence every durable write.** Every write transaction on an
   authority-bearing store first runs a check. On PostgreSQL it reads the epoch
   row `FOR SHARE` and checks two things: the row still names this
   `instance_id`/`epoch`, and `pg_locks` shows the recorded lock backend still
   holding the advisory lock. SQLite checks the epoch inside `BEGIN IMMEDIATE`.
   A takeover's epoch `UPDATE` must wait for in-flight fenced writes, so each
   write is ordered strictly before the takeover or refused after it.
5. **Fail-stop on loss.** A supervisor re-checks the lock and epoch on the lock
   session every 2 s. A fenced write can also detect the loss first. Either path
   marks the authority lost exactly once. After that, admission returns 503, no
   202 is sent, dispatch is refused, readiness returns 503, and a terminal
   commit is refused. A refused terminal surfaces as `reconciliation_required`,
   and uncommitted output is never shown. Live agents are interrupted and the
   process exits with code 75 so the supervisor restarts it and it re-competes.
6. **Release** happens on `disconnect()` and when `connect()` fails or is
   cancelled. This lets a retried adapter in the same gateway acquire the
   authority again.

Invariants carried forward unchanged: persist-before-ack, commit-before-emit,
no uncommitted result, `UNKNOWN` never auto-retried, and local/desktop mode
unfenced and unchanged.

## Supported topology (R4)

- Exactly **one effectful `/v1/runs` Runtime per durable store (database)**.
  More replicas may exist only as hot standbys. They fail startup (or crash-loop
  under a restart policy) until the holder is gone. Use the `Recreate` update
  strategy, not a rolling update, so that old and new instances never overlap.
  An overlapping pre-R4 binary has no fencing.
- The authority connection must be a direct or session-mode PostgreSQL
  connection. Transaction-mode PgBouncer breaks session advisory locks, so do
  not use it here.
- One scope per database. Several multiplexed API-server profiles cannot share
  one store: the second profile fails closed at startup.

## Consequences and residual risk

- **Effect window after loss.** A tool call already executing when the lock
  session dies can finish its external effect during detection. Detection takes
  at most the supervisor interval plus the PostgreSQL/TCP loss detection time
  before exit. The run can still never be committed as success, and the next
  holder marks it `UNKNOWN` with no retry. Closing this window needs effect-level
  fencing through the Lane-1 effect ledger, which is outside R4.
- **Legacy upgrade.** The first R4 start marks any pre-R4 `pid:` nonterminal
  `/v1/runs` rows `UNKNOWN`. This is correct only if no pre-R4 process is still
  running, which is the purpose of the `Recreate` requirement.
- **Horizontal scale.** Multi-replica execution needs the multi-instance
  protocol from the decision document: per-run leases with fenced epochs and
  effect fencing. This ADR does not provide it.

## Rollback

Revert the R4 commit. The additive `runtime_authority` table is harmless to the
prior code. Rows stamped `inst:` are ignored by the old PID reconciler: they
never parse as a PID, so they are not reconciled and would stay nonterminal.
After a rollback, an operator must resolve any `inst:` nonterminal rows by hand.
