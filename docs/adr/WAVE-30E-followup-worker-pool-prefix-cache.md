# WAVE-30E follow-up (ADR-gated): persistent worker pool + prefix cache

**Status:** PROPOSED — requires an approved ADR before any implementation.
**Scope:** deferred out of the WAVE-30E performance session by Owner instruction
because each item changes a process- or tenant-isolation boundary and no
already-approved architecture covers it.

This document is a concrete design + risk contract for the two deferred
optimizations so a future ADR can accept/reject them on their merits. It does
**not** authorize implementation.

## Why deferred

The WAVE-30E session shipped only additive, default-off, isolation-neutral
changes (instrumentation; task-scoped lean tool loadout via the existing
tool_search bridge; opt-in Ollama `keep_alive`; task-aware num_ctx recording).
The two items below cross an isolation boundary and therefore need governance:

| Item | Boundary crossed | Primary hazard |
|---|---|---|
| Persistent worker pool | process reuse across tasks/tenants | secret/state bleed, cancellation correctness, per-task binding leakage |
| Cross-request prefix cache | shared cached context across requests | cross-tenant / cross-org context or KV-cache leakage |

## A. Persistent / warm worker pool

**Goal:** remove the ~12 s runtime+worker+agent init observed on every dispatch
(`t_799c219d` first heartbeat at +12 s) by reusing pre-initialized workers.

**Design sketch (for the ADR to evaluate):**
- A bounded pool of pre-forked worker processes, each pinned to **one tenant at a
  time**; a worker is returned to the pool only after a full reset that clears:
  the run-limit enforcer, engine/provider/model binding, secret material,
  conversation state, memory handles, kanban claim, and env-derived config.
- **Hard invariants the ADR must require:**
  1. A worker never serves two principals without a verified reset (prove with a
     cross-principal isolation test that fails closed).
  2. The engine-binding + digest attestation + `€0` classification + worker-attempt
     limit (WAVE-30D) are re-established per task, never inherited.
  3. Cancellation/timeout (`enforce_max_runtime`) must terminate or fully quarantine
     a pooled worker — a timed-out worker must never silently return to the pool.
  4. Secret boundary: a pooled worker must re-load its secret per task through the
     strict loader; no secret persists in a reused process.
- **Cold vs warm labeling:** a pooled (warm) worker's timings MUST be recorded as
  warm and never mixed with cold-start numbers (reuses `model_timings.cold_start`).

**Open questions for the ADR:** pool sizing vs. tenant count; reset verification
cost vs. the init it saves; interaction with the single-operator dashboard plane
vs. the multi-tenant `/api/runtime/v1` plane; failure isolation (a poisoned worker
must not degrade the pool).

## B. Cross-request prefix / prefix-KV cache

**Goal:** reuse the immutable authorized prefix (system prompt + tool schemas)
across requests so prompt-eval is not repaid every call.

**Design sketch (for the ADR to evaluate):**
- Cache key MUST bind: tenant + org + principal scope + engine/model + exact
  serialized prefix hash. A cache entry is reusable ONLY within the identical
  security scope — never across tenant/org/principal boundaries.
- Two candidate layers, both ADR-gated:
  1. **Application prefix cache** (our side): memoize the assembled prefix bytes
     per scope to avoid re-serialization only — no model state shared.
  2. **Server KV/prefix cache** (Ollama side): relies on the inference server's
     own prefix reuse; the ADR must confirm the server keys KV per model/context
     and cannot leak KV across concurrent requests, especially with
     `OLLAMA_NUM_PARALLEL > 1`.
- **Hard invariants:** no cross-tenant/org/principal reuse; cache poisoning
  impossible (key includes the exact prefix hash); eviction cannot serve a stale
  prefix that dropped a policy/safety block.

**Open questions for the ADR:** does the lean loadout (smaller, more variable
prefix) reduce the payoff? Interaction with `OLLAMA_NUM_PARALLEL` context-memory
multiplication; measured prompt-eval savings vs. leakage risk.

## Companion research items (measure before deciding — not code changes)

Recorded here so the ADR has the evidence checklist (from the Owner's
research-backed Ollama guidance):
- Confirm the model is 100% GPU-offloaded (`/api/ps` `size_vram == size`); record
  `OLLAMA_NUM_PARALLEL`, `OLLAMA_FLASH_ATTENTION`, `OLLAMA_KV_CACHE_TYPE`.
- Do NOT enable `q8_0`/`q4_0` KV-cache quantization in the official benchmark
  without a separate quality A/B + comparability decision.
- Do NOT claim the SSH tunnel is a bottleneck without direct network + server-side
  timing evidence (the WAVE-30E native-timing instrumentation provides the
  server-side split: `load` vs `prompt_eval` vs `eval`).
- Streaming improves TTFT / perceived latency only — never total generation time;
  report it honestly (the TTFT instrumentation measures exactly this).
- Preload/keep_alive is a WARM-path improvement only; cold-start is reported
  separately and never masked.
