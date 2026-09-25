# Memory & Continuity State — Ownership Matrix (v1)

Definitive owner for each state category. The Runtime never owns authority.

| State category | Owner | Runtime role | Notes / base artifact |
|---|---|---|---|
| Persistent semantic organizational memory | **Simorgh / AI-OS** | read (scoped) + propose candidates | authority; ADR-0002 cognitive-growth (PROPOSED) |
| Persistent episodic organizational memory | **Simorgh / AI-OS** | read (scoped) + propose | authority |
| Validated memory claims / promotion | **Simorgh** governed pipeline | submit `PromotionCandidate(runtime_authorized=False)` | Runtime never validates/promotes |
| Supersession of canonical claims | **Simorgh** | request only | Runtime never supersedes canonical |
| Cross-agent / cross-workspace sharing | **Simorgh / Gateway** | request only | needs authorization |
| CRM/ERP/SAP/CAD source-of-truth records | **Source systems** | store id/ref only, query source | router routes raw payload → SOURCE_SYSTEM |
| Approvals / effects / receipts / idempotency | **Effect ledger** (Lane-owned) | reference refs only | `effect_ledger.py` is a Lane integration dep |
| Skills / procedural knowledge | **Skills registry** (via gate) | propose only | router → BRAIN_MEMORYBUS gate |
| Canonical tenant/org/workspace/principal identity | **Gateway / Workspace authority** | consume signed placement | org/workspace/agent/run not yet in envelope |
| Task identity, event journal, checkpoint PERSISTENCE, resume tokens, lease/fencing, heartbeat, lifecycle, cancellation, reconciliation | **Durable Execution session** | reference `run_id`/resume handle | `continuity/checkpoint.py` = IMPLEMENTED_NOT_INTEGRATED, pending their review |
| — Runtime-owned below — | | | |
| Working/task context (execution-local) | **Runtime** | own | router → RUNTIME_WORKING |
| Always-injected capsule (rules/profile) | **Runtime** | own, bounded | legacy MEMORY.md/USER.md; token-capped |
| Scoped retrieval assembly / consumption | **Runtime** | own | retrieval pipeline (planned) |
| Local encrypted memory cache | **Runtime** | own | ADR-0006 D4/D8 (planned) |
| Pending memory outbox (transport) | **Runtime** | own | ADR-0006 D3/D10 (planned) |
| MemoryClaim transport envelope schema | **Runtime** | own | `youtab.memory-claim.v1` DTO |
| Simorgh MemoryBus client contract | **Runtime** | own (contract) | live client not built |
| Token/context budget policy | **Runtime** | own | `memory/budget.py` |
| Execution continuity *references* (not persistence) | **Runtime** | own refs | cites Durable `run_id`/resume handle |
