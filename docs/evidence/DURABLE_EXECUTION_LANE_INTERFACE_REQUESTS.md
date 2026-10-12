# Durable Execution — Typed Interface Requests

For Master Integrator. These are the boundaries this stream will CALL but must NOT
implement/duplicate. Each names the canonical owner and the typed contract.

---

## IR-1 — Canonical effect ledger boundary (owned by Lane 1/2)

**Why:** P0-E proved the base gap — `youtab_runtime/effect_ledger.py` is ABSENT on
origin/main (`c7650a1b`), so Kanban's retry path re-runs a failed-after-effect task
with no per-effect idempotency (duplicate-effect window). Lane 1 already ships the
canonical `youtab_runtime/effect_ledger.py` (WAVE-26, "durable effect-level
idempotency ledger, frozen contract 5/8"). Durable Execution must call it, never
build a second `effects` table.

**Requested typed surface (as observed on Lane 1 `4edbbe78`):**
- `compute_effect_id(logical_action, target_scope, principal, ...) -> effect_id: str`
- `derive_provider_idempotency_key(effect_id) -> str`
- `EffectRecord{ effect_id, target_scope_digest, provider_idempotency_key, state, principal, is_terminal, executable }`
- record/lookup by `effect_id`; a committed effect returns `committed` (the
  idempotency guarantee); recovery of an in-flight effect yields UNKNOWN.

**Contract Durable Execution needs at the boundary:**
1. `begin_effect(effect_id, principal, target_scope_digest) -> EffectRecord` (idempotent).
2. `commit_effect(effect_id, receipt) -> immutable receipt`.
3. `lookup(effect_id) -> {absent | in_flight | committed(receipt) | unknown}`.
4. Distinctions that MUST hold (Saga/RPC-ambiguity grounding):
   - rejection **before** execution → **no effect row**;
   - crash **after possible** effect → **UNKNOWN / RECONCILIATION_REQUIRED** (never blind retry);
   - **confirmed committed** → **immutable receipt**, safe to re-observe;
   - a non-idempotent unknown outcome is **never** auto-retried.

**Durable Execution side:** the task/run journal stores only the `effect_id` +
receipt reference on the task event, and gates re-execution on `lookup(effect_id)`.
Until Lane merges on the integrated base, this is called through a thin typed
adapter with a NotImplemented/absent-boundary guard (fail-closed), not a local ledger.

---

## IR-2 — Web/Electron durable task consumer contract (backend owned here; UI = Frontend session)

**Why:** P0-H proved `/v1/runs` is in-memory + single-consumer SSE with no
reconnect-from-sequence and no restart survival. This session may implement the
BACKEND durable task/event/reconnect contract; it must NOT edit frontend/Electron
UI files (Frontend session owns those). This is the typed consumer contract the UI
and Master Integrator will bind to.

**Typed contract:**
- `task_id: str` — durable, workspace/tenant-scoped, created BEFORE work starts.
- `GET /v1/tasks/{task_id}` → `{ task_id, state, accepted_at, policy_deadline, current_step, last_checkpoint_at }`.
- `state ∈ { QUEUED, RUNNING, WAITING_APPROVAL, WAITING_EXTERNAL, PAUSED, BLOCKED, CANCELLING, SUCCEEDED, FAILED, CANCELLED, UNKNOWN, RECONCILIATION_REQUIRED }`.
- `GET /v1/tasks/{task_id}/events?from_seq=<n>` (or `Last-Event-ID`) → append-only,
  monotonic `seq` per task; reconnect resumes from `from_seq` (multi-consumer safe).
- `GET /v1/tasks/{task_id}/result` → terminal result retrievable by id after
  disconnect/restart (durable store, mirror of `/v1/responses` ResponseStore pattern).
- `POST /v1/tasks/{task_id}/cancel` → cooperative cancel + acknowledgement event.
- Stale-client rule: a disconnect never cancels a running task; only an explicit
  cancel does; a stale connection cannot overwrite newer state.

**Non-goals for this session:** no frontend/Electron `.tsx`/UI edits; no fake
elapsed-time percentage; UI integration is a separate consumer handoff.

---

## IR-3 — Worker lease / reconciliation boundary (owned by Lane 1/2)

`youtab_runtime/worker_lease.py` and Lane 2 `reconciliation_evidence.schema.json`
are absent on base. Durable Execution's progress-stall watchdog (C-2.2b) and
parent/child lifecycle will consume the canonical lease/fencing + reconciliation
evidence contract rather than re-implement UNKNOWN/reconciliation. Typed request:
`acquire_lease(task_id, owner, epoch) / renew(fencing_token) / reconcile(evidence)`.
