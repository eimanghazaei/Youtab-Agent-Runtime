# ADR-0006 — Runtime enterprise memory: transport, cache, outbox and context boundary

**Status:** PROPOSED (draft for Owner review — NOT self-accepted). 2026-09-22.
**Author:** Eiman.
**Number note:** if `ADR-0006` collides with a sibling-branch ADR at integration,
the Master Integrator should renumber; the content is number-independent.

> No LIVE implementation may rely on this ADR until Owner explicitly ratifies it.
> This branch ships only inert, fail-closed, NON-LIVE contracts consistent with it.

## Context

Youtab wants agents that work for days, resume safely, retrieve the right
organizational knowledge across sessions, switch models, and serve multiple
authorized employees — without the Runtime becoming a second brain.

This ADR is **additive** and defers to, rather than replaces, existing decisions:
- **ADR-0002 (cognitive-growth & persistent memory)** — PROPOSED on branch
  `feat/adr-0002-cognitive-growth-memory`: the five-tier model, MemoryBus,
  governed promotion, erasure, non-regression. That ADR owns the *authority*
  memory model. This ADR does not restate or override it.
- **ADR-0002 (Simorgh admission-to-execution)** — Accepted on branch
  `evidence/runtime-enterprise-reference-v1`: the signed `BrainCommandEnvelope`
  admission contract (Option B, optional-then-mandatory behind a fail-closed
  flag). This ADR consumes that envelope; it does not change admission.
- **`YOUTAB_AGENT_RUNTIME_BOUNDARY.md`** — the runtime holds no sovereign-memory
  authority.
- **`SECURITY.md` §2** — states a *single-tenant personal agent* trust model.
  This is in tension with multi-tenant enterprise memory (see Open Question OQ-1).

## Scope of THIS ADR

Only the **Runtime-owned** side of memory: transport envelope, local cache,
outbox, retrieval assembly, degraded-mode, context budget, and the migration of
the legacy `MEMORY.md`/`USER.md` capsule. Authority, validation and promotion
remain Simorgh's. Task identity, checkpoint persistence, leasing and the event
journal remain the **Durable Execution** owner's (see interface request doc).

## Decisions proposed (each needs Owner confirmation)

- **D1 — Runtime DTO/cache/outbox vs Simorgh authority.** The Runtime `MemoryClaim`
  is a versioned transport ENVELOPE (`youtab.memory-claim.v1`), never a canonical
  authority. The Runtime may read (scoped) and propose (candidates); it never
  validates enterprise truth, promotes AI inference, supersedes canonical claims,
  or cross-shares without Simorgh/Gateway authorization.
- **D2 — Scope tuple.** `(tenant, organization, workspace, principal, agent, run,
  purpose)`. Only `tenant`+`principal` are envelope-bound today; org/workspace/
  agent/run require a signed Gateway placement (dependency request filed). Until
  then, scope beyond tenant+principal is admission-trusted and LIVE multi-tenant
  behavior stays disabled.
- **D3 — Async outbox vs synchronous writes.** Promotions and feedback are written
  to a transactional, idempotent, **append-only outbox** and synced to Simorgh
  asynchronously; the Runtime never blocks a task on a sovereign write and never
  writes sovereignly itself. Retrieval reads are synchronous with a deadline.
- **D4 — Offline behavior.** An encrypted, scope-partitioned local cache serves
  reads offline; writes queue in the outbox; on reconnect the outbox syncs
  idempotently with conflict/supersession handling and a convergence barrier
  (D10) before any "durable" claim.
- **D5 — Trust-source and claim lifecycle.** `PENDING → VALIDATED → SUPERSEDED /
  REJECTED`; only `VALIDATED` feeds production retrieval; `AI_INFERRED` cannot be
  born validated and cannot self-promote. The Runtime-local `with_status` helper
  is for candidate bookkeeping/tests only, never authoritative promotion.
- **D6 — Validation/promotion ownership.** Simorgh's governed pipeline only
  (de-identification, eligibility, provenance, quality, poisoning/injection screen,
  licence, independent verification, tenant-leakage). The Runtime submits
  `PromotionCandidate(runtime_authorized=False)`.
- **D7 — Erasure propagation.** Erasure must reach relational + vector + graph +
  cache + outbox + summaries (cryptographic erasure / key destruction); tombstones
  retain no payload/embeddings. Runtime is responsible only for its cache+outbox
  legs; Simorgh owns authority-side erasure.
- **D8 — Cache encryption and retention.** Local cache is encrypted at rest,
  scope-partitioned, with per-`retention_policy` expiry; no plaintext secrets in
  cache, embeddings, logs, or outbox.
- **D9 — Availability / degraded mode.** Authority/policy memory outages fail
  closed. Non-authoritative personalization may degrade explicitly: a
  `MemoryBusResult(degraded=True, reason=…)` with empty results and NO silent
  tenant fallback; the UI/receipt must state retrieval was unavailable.
- **D10 — Convergence barrier.** Before the Runtime reports a memory write as
  remotely durable, the outbox must confirm Simorgh acknowledgement; until then it
  is "pending local".
- **D11 — Observability and audit.** Audit events for memory create/read/update/
  supersede/delete/export; retrieval records cite which evidence was used
  (provenance + scores) and whether the source was live or reference.
- **D12 — Migration from MEMORY.md/USER.md.** The legacy capsule becomes the
  bounded always-injected `CAPSULE` tier under a per-deployment token cap
  (backward-compatible: the legacy 2200+1375 chars provably fit). No data loss;
  the on-disk files remain the capsule's storage until a later slice migrates them.
- **D13 — Rollback.** Every slice is additive and default-off; disabling the
  Simorgh client + outbox returns the Runtime to today's local-capsule behavior.

## Open questions (must be answered at ratification)

- **OQ-1.** Reconcile `SECURITY.md`'s single-tenant posture with multi-tenant
  enterprise memory: adopt a deployment-class split (personal vs enterprise) or
  update the trust model. **Owner decision required.**
- **OQ-2.** Confirm the Gateway signed-placement mechanism (envelope v2 vs separate
  `youtab.scope-placement.v1` token) — see `RUNTIME_SCOPE_GATEWAY_DEPENDENCY.md`.
- **OQ-3.** Confirm this ADR composes with (does not fork) the PROPOSED
  cognitive-growth ADR-0002; the Master Tracker lists that ADR (#15) as the
  blocking prerequisite for memory-sharing-with-Simorgh.
- **OQ-4.** Whether the frozen `TaskCheckpoint` capsule is adopted by the Durable
  Execution owner (accept / adapt / drop).

## Consequences

- Enables the Runtime-owned transport/cache/outbox/budget without granting any
  sovereign authority; keeps large enterprise memory external and selectively
  retrieved.
- Costs: a real dependency on a signed Gateway placement and a live Simorgh
  MemoryBus endpoint, neither of which exists yet; both are documented as
  unresolved cross-repo dependencies.

## Non-goals

- Not a second brain, not a second vector/graph authority, not GraphRAG.
- Not task leasing/journal/checkpoint persistence (Durable Execution owner).
- Not effect-ledger truth (Lane-owned).
