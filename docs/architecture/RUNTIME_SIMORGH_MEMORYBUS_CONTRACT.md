# Runtime ↔ Simorgh MemoryBus — Cross-Repository Dependency Contract (v1, DRAFT)

Status: **DRAFT / NON-LIVE.** This contract defines the typed seam between the
Youtab Agent Runtime and One Brain (Simorgh / Youtab AI OS) for scoped memory
retrieval and governed promotion. No live Simorgh endpoint exists in this
repository; the Runtime ships `youtab_runtime.memory.bus.ReferenceMemoryBus`, a
deterministic, in-process, **NON-LIVE** implementation for tests only. Nothing
in this contract is LIVE-integrated or production-wired.

This contract is additive and consistent with:
- `docs/architecture/YOUTAB_AGENT_RUNTIME_BOUNDARY.md` — runtime holds no sovereign-memory authority.
- ADR-0002 (cognitive-growth & persistent memory) — **PROPOSED**, not yet ratified in-tree.
- `AGENTS.md` — durable memory flows through the governed MemoryBus; direct sovereign writes forbidden.

## 1. Ownership boundary

| Concern | Owner |
|---|---|
| Sovereign semantic/episodic memory, validation, supersession, erasure policy, org-wide retrieval | **Simorgh / One Brain** |
| Scoped read consumption, promotion *candidates*, execution-local working state, evidence-of-use | **Agent Runtime** |
| Canonical tenant/org/workspace/principal/agent identity + admission | **Gateway / Workspace Authority** |
| Approvals, external effects, receipts, reconciliation | **Effect ledger** (never natural-language memory) |

The Runtime may **read** within a command's `allowed_memory_scopes` and may
**propose** promotions. It may never self-authorize a promotion: a
`PromotionCandidate` carries `runtime_authorized = False` (identical discipline
to `youtab_runtime.contracts.EffectProposal`).

## 2. Transport & identity requirements (a live client MUST satisfy)

- Every request is bound to a `MemoryScope` derived only from an admitted
  `BrainCommandEnvelope` + admission placement (`youtab_runtime.memory.scope`).
  Scope is never taken from tool arguments, filenames, cwd, or the latest session.
- Requests are signed and authenticated; Simorgh verifies scope authorization
  server-side (retrieval authorization happens **before** any content reaches a model).
- Bounded deadlines and bounded retries; **no silent fallback** to another
  tenant/profile. On failure the client returns an explicit **degraded** result
  (`MemoryQueryResult.degraded = True`), never a guessed or empty-as-success answer.
- Version/capability negotiation via the `schema_version` literals below.

## 3. Typed messages (source of truth: `youtab_runtime/memory/bus.py`)

| Message | `schema_version` | Purpose |
|---|---|---|
| `MemoryQuery` | `youtab.memory-query.v1` | scoped, bounded read (text, memory_types, limit, min_trust, deadline) |
| `MemoryQueryResult` | `youtab.memory-query-result.v1` | `source ∈ {live,reference}`, `is_live`, `degraded`, `results[]` with citations + scores |
| `RetrievedMemory` | — | one `MemoryClaim` + `score ∈ [0,1]` + `citation` |
| `PromotionCandidate` | `youtab.memory-promotion.v1` | claim + justification; `runtime_authorized=False` |
| `MemoryClaim` | `youtab.memory-claim.v1` | the canonical record (scope, type, content/ref, hash, trust, status, provenance, retention) |

Client operations (`MemoryBusClient` Protocol): `query`, `store_candidate`,
`feedback`, `supersede`, `erase` — all scope-bound.

## 4. LIVE vs NON-LIVE classification (mandatory)

- `ReferenceMemoryBus` stamps **every** result `source="reference"`, `is_live=False`.
  A reference answer is type-observably distinct from a live one and must never be
  treated as sovereign authority or as a receipt.
- A future live client MUST stamp `source="live"`, `is_live=True`, and MUST NOT be
  interchangeable with reference results in any authority/receipt path.
- The reference bus never validates a promotion candidate; only a governed Simorgh
  step may move a claim to `VALIDATED`.

## 5. What the Simorgh / Gateway repository must provide to go LIVE

1. An authenticated MemoryBus endpoint implementing the five operations above with
   server-side scope authorization and the governed promotion pipeline
   (de-identification, eligibility, provenance, quality, poisoning/injection screen,
   licence check, independent verification, tenant-leakage check) per ADR-0002 §.
2. Ratification of ADR-0002 (or a superseding ADR) — the Master Tracker lists the
   cognitive-growth memory ADR (#15) as a blocking prerequisite for memory-sharing.
3. Erasure semantics reaching relational + vector + graph + cache + outbox + summaries
   (cryptographic erasure / key destruction), with tombstones that retain no payload/embeddings.
4. Reconciliation of `SECURITY.md`'s "single-tenant personal agent" posture with the
   multi-tenant enterprise memory model, or an explicit deployment-class split.

## 6. Open cross-repo items (unresolved exact-SHA dependencies)

- Live Simorgh MemoryBus endpoint — **NOT PRESENT** in any repo reachable here.
- ADR-0002 ratification — PROPOSED on branch `feat/adr-0002-cognitive-growth-memory`.
- Effect-ledger persistence for promotion receipts — Lane-only
  (`youtab_runtime/effect_ledger.py`, integration dependency; not duplicated here).
