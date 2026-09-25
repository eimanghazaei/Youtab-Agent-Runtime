# Durable Execution ↔ Enterprise Memory — Interface Request (v1, DRAFT)

Status: **DRAFT.** Ownership split acknowledged; this is a request, not a claim.

## Ownership boundary (as directed by Owner)

**Durable Execution session owns:** task identity, task event journal, TaskCheckpoint
*persistence*, resume tokens, lease/fencing, heartbeat/progress, parent/child lifecycle,
cancellation, restart/reconnect, late-result and reconciliation.

**Enterprise Memory session owns:** scoped memory contract, MemoryClaim transport schema,
Memory Router, Simorgh MemoryBus client contract, retrieval assembly, local cache/outbox
for memory transport, token/context budget, semantic/episodic/procedural consumption.

## Status of the checkpoint capsule in this branch

`youtab_runtime/continuity/checkpoint.py` (`TaskCheckpoint`, `CheckpointChain`) is
classified **IMPLEMENTED_NOT_INTEGRATED — pending Durable Execution owner review**. It is:

- pure (no I/O), NOT wired into `state_meta` or any live path;
- offered as a candidate schema for the Durable owner to accept, adapt, or drop;
- explicitly **not** a competing checkpoint schema and not a persistence mechanism.

The Master Integrator decides its fate.

## What Memory needs FROM Durable Execution (so memory can reference continuity)

Memory does not persist or lease anything. It only needs a **stable, typed reference** it
can attach to retrieval/evidence records:

1. `run_id` — the canonical durable run identifier (string, admission-bound).
2. `resume_point_ref` — an opaque, Durable-owned handle naming the latest safe resume
   position, so a memory retrieval record can cite "context assembled at resume point X".
3. `committed_effect_refs(run_id)` — read access to which effects already committed, so
   retrieval/outbox never re-proposes a completed effect. (Effect truth stays Lane/Durable-owned.)

## What Durable Execution may take FROM this Memory branch (optional)

- The `TaskCheckpoint` field set as a starting point for its own schema, if useful.
- The content-addressing idea (model-neutral resume) — but hostile-tamper integrity
  (keyed HMAC / signature / append-only fenced storage) is the Durable owner's to add;
  the hash chain here is only tamper-evident against accidental corruption.

## Non-goals (Memory will NOT do)

- Will not wire `TaskCheckpoint` into `state_meta`.
- Will not implement leasing, fencing, heartbeats, or cancellation.
- Will not duplicate or compete with the Durable task event journal.
