# ADR-0002 — Cognitive growth and persistent agent memory

**Status:** PROPOSED · **Scope:** task-scoped agents and Super-Agent Units under the Youtab Agent Runtime
**Supersedes:** nothing · **Related:** `YOUTAB_AGENT_RUNTIME_BOUNDARY.md`, `ADR-0001`, `ADR-0003` (verified execution), `CODE_AGENT_WORKSPACE_CONTRACT.md` (Living Canon), `docs/roadmap/SIMORGH_UNIFIED_COGNITIVE_ARCHITECTURE.md`, Master Tracker WS#15
**Deciders:** Owner (ratification required — this ADR is not Canon until the Owner accepts it)

> This is a **proposal**. It records a decision the Owner must ratify before Workstreams #16
> (Secret Broker & Provider Proxy), #17 (capability engine & execution sandbox), #19 (workspace
> ingress & Code-to-Agent), #26 (MemoryBus) and #27 (learning pipeline) open any code. Nothing here
> is implemented yet, and no row may treat it as accepted while its status reads PROPOSED. Companion
> **ADR-0003** governs verified completion and execution.

> **Guiding principle — one central brain, many growing cognitive agents.** Simorgh remains Youtab's
> unified system-level brain and orchestrator. Agents are many growing cognitive components that may
> **independently maintain and improve their own scoped working, episodic, semantic, skill and
> long-term memory**, learn and specialise, **without hard-coded cognitive ceilings**. Only promotion
> to shared Simorgh memory, cross-agent sharing, cross-boundary effects and sensitive operations
> require governance. This ADR is the memory-integrity expression of that principle.

> **Living Canon, not immutable.** Once ratified, this architecture is **Living Canon** in the sense of
> `CODE_AGENT_WORKSPACE_CONTRACT.md`: it may be amended, extended, versioned or superseded through a
> reviewed ADR/PR change that records the reason, affected contracts, threat-model/security impact,
> migration, rollback, and executable acceptance evidence. The newest Owner-ratified version is
> authoritative; older versions stay traceable in Git history; there is no silent drift. Canon status
> never becomes a hard ceiling on agent memory, learning or capability, and never silently weakens the
> protected invariant set — **tenant isolation, data ownership, deletion/erasure, evidence, rollback,
> verifier independence, or Simorgh's role as the one brain** (the same set guarded by `ADR-0003` and
> `CODE_AGENT_WORKSPACE_CONTRACT.md`).

> **Verified-execution precedence.** The verified-completion foundation in **ADR-0003** is implemented
> **first** and **governs the MemoryBus (#26), learning-pipeline (#27) and every later implementation
> slice (orchestration #30, frontend #32 included) from their first commit**: every claim these slices
> make — that isolation holds, that erasure propagated, that a promotion was screened — is a
> **measured, independently verified** fact, never a self-declared one. **No memory or learning slice
> may mark its own work `VERIFIED_COMPLETE`.**

## Context

The product goal is that agents are *growing cognitive components*, not stateless tools: they keep
working, episodic, semantic, skill/procedural and long-term memory; they learn from experience and
**autonomously** improve their own methods; they get stronger and more specialised over time; their
memory and identity survive an engine change; and they share genuinely useful, de-identified
knowledge back to Simorgh — the one central brain and system orchestrator. There are **no hard
ceilings** on an agent's intelligence, memory, learning, reasoning or tool use.

That goal meets a hard authority boundary (`YOUTAB_AGENT_RUNTIME_BOUNDARY.md`): Youtab has **one
cognitive authority: One Brain** (Simorgh); the runtime is an execution plane, *"not another brain,
policy sovereign, memory authority, or effect authorizer."* Effects — including writes to **sovereign**
memory — are `youtab.effect-proposal.v1` records (`runtime_authorized=false`) admitted by the Brain
Effect Gate; the objective, and recalled memory, are **data, not instruction**; cross-tenant access is
rejected.

*Sovereign memory* means any scope carrying system- or Brain-level authority — shared/system,
cross-tenant, cross-user, cross-workspace, cross-agent, or promotion-granting scopes. It is distinct
from an agent's **own delegated memory**: the scope `(tenant, user, workspace, agent, purpose)` that a
signed command delegates to that agent.

The tension to resolve: **agents must be able to learn and grow at the speed of their own work —
which means autonomously managing their own scoped memory — without the runtime becoming a second
brain, a sovereign-memory authority, or a cross-tenant leak.** This ADR decides that reconciliation.
It chooses no storage engine and writes no code.

> **Boundary reconciliation (ratification-blocking).** The boundary doc carries two clauses that bear
> on this, and they are not fully consistent with each other: (a) *"memory writes are never
> self-authorized [and] become youtab.effect-proposal.v1 records"*; and (b) *"the runtime cannot write
> or promote **sovereign** memory. Memory use must remain inside the command's allowed **read**
> scopes."* Clause (b)'s word "sovereign" already supports this ADR's reading; clause (a)'s blanket
> "memory writes" and clause (b)'s "read scopes," read literally, do not. This ADR proposes to refine
> **both**: the Effect-Gate requirement attaches to **sovereign** memory (shared/system, cross-boundary,
> promotion); an agent's writes **inside its own delegated `(tenant,user,workspace,agent,purpose)`
> namespace** are non-sovereign "memory use within allowed scopes," and the admission grammar must grant
> a **write-capable own-scope** (not read-only). **The ordinary-vs-sovereign determination is made
> system-side by the MemoryBus — default-sovereign and fail-closed, an allowlist of ordinary operations
> rather than a denylist of sovereign ones; an agent cannot label its own write "ordinary" to bypass the
> gate.** Ratifying this ADR entails ratifying that refinement and amending **both** boundary clauses;
> until then the documents are in tension by design, and this is called out, not hidden.

## Decision

**Cognitive growth is unbounded; cognitive authority is not.** An agent may accumulate arbitrarily
large and sophisticated memory, learn arbitrarily good methods, and reason arbitrarily deeply — and
it does so **autonomously within its own delegated scope**. Every gain is subject to the same fixed
security boundary. Intelligence escalation is never authority escalation. The controls are isolation,
scope, the system-side gate, and **resource policy** — never a cap on capability.

### 1. Five memory tiers; agent-autonomous within its own scope, gated only at the boundary

An agent **autonomously creates, updates, consolidates and uses** its own memory — all five tiers —
inside its delegated `(tenant, user, workspace, agent, purpose)` scope, with **no global approval
gate**. Forcing every ordinary scoped operation through the Effect Gate would make agent learning
artificially slow and cognitively constrained; that is explicitly rejected.

| Tier | Purpose | Owner scope (namespace key) | Persistence | Ordinary operation |
|---|---|---|---|---|
| **Working** | the active task's scratch state | task, **tenant/user-bound** (ephemeral) | discarded & **zeroed at deactivation** (no residue survives worker/warm-container reuse) | autonomous |
| **Episodic** | completed tasks and outcomes | tenant·user·workspace·agent·purpose | durable, agent namespace | **autonomous** create/update/consolidate/read |
| **Semantic** | learned facts | tenant·user·workspace·agent·purpose | durable, agent namespace | **autonomous** |
| **Skill / procedural** | methods & tool strategies that worked | tenant·user·workspace·agent·purpose | durable, agent namespace | **autonomous** |
| **Long-term** | cross-session continuity & identity | tenant·user·workspace·agent·purpose | durable, agent namespace | **autonomous** |

`purpose` is **both a namespace partition key and an authorization dimension**: durable memory is
stored partitioned by the full 5-tuple, and reading or writing across a different `purpose` is not an
ordinary operation — it is gated like any other scope crossing. (This resolves the load-bearing role
`purpose` plays; its grammar is finalised at ratification, but that it partitions storage is decided
here, not deferred.)

The Effect Gate is **required only** for operations that leave the agent's own scope or carry
system-level authority:

- **promotion into Simorgh's shared / system memory;**
- **sharing between agents;**
- **cross-workspace, cross-user, or cross-`purpose` access;**
- **external side effects;**
- **changes to global policy or authority;**
- **sensitive or high-impact actions.**

The set of *ordinary* (un-gated) operations is a **system-side allowlist**; anything not on it is
sovereign and fails closed. The recognizer is MemoryBus-side, not agent-influenced.

**Resource policy, not a cognitive ceiling.** Because ordinary writes bypass the gate, MemoryBus
enforces **per-scope write quotas, rate limits and back-pressure** to prevent storage exhaustion,
cost blow-out and cross-tenant noisy-neighbour effects. This is an Owner-governed **resource** control
and must never be tuned into a limit on what an agent may *learn or hold* — it bounds write *rate and
footprint*, not capability. (`SIMORGH_UNIFIED_COGNITIVE_ARCHITECTURE.md` explicitly permits governed
resource policy while forbidding its use as a cognitive limitation.)

### 2. MemoryBus: agent-owned namespaces and Simorgh-consolidated namespaces, engine-independent

Durable memory lives in the system-owned **MemoryBus**, which exposes two namespace classes:

- **Agent-owned namespaces**, keyed `(tenant, user, workspace, agent, purpose)` — the agent writes
  here autonomously; no read ever crosses a tenant, and cross-agent / cross-purpose read *within* a
  tenant is not implicit (it needs an explicit shared scope and is gate-governed).
- **Simorgh-consolidated namespaces** — populated only through the gated promotion pipeline (§4).

MemoryBus is addressed by identity, **not** by engine, model weights or provider state, so **an engine
swap preserves the agent's memory, learned skill and identity** (a non-regression contract, §6).

### 3. Recalled memory is data, not instruction; provenance is system-attributed

Content read from any tier is **data**: it cannot expand the signed command contract, grant a toolset
or scope the command did not carry, or re-authorize an effect. Poisoned or adversarial memory
therefore cannot escalate authority — at worst it degrades one task's reasoning, which the completion
record's uncertainty/conflict fields and the independent Verifier (`ADR-0003`) surface. A **skill
replay executes only within the current command's allowed toolsets**; a skill referencing a tool the
command did not authorize **fails closed**.

Provenance, confidence, timestamp and source links on every durable record are **attributed by the
system** (MemoryBus / Effect Gate at admission), **not asserted by the agent** — a compromised agent
cannot stamp poisoned memory as high-confidence with clean provenance. System-attribution proves
**origin, not truth**: it establishes *which agent wrote this, when*, and nothing more. Source links
**carry no user/conversation identifier** (they reference records by opaque handle), so a retained
provenance chain is never itself the private payload.

### 4. Governed promotion & the learning pipeline (agent → Simorgh → other agents)

Agents may autonomously learn within their own scope. **Promotion into shared Simorgh knowledge** is
the gated boundary, and it moves **distilled, provenance-bearing, reusable artifacts** — never raw
private content. Raw private conversations, private files or tenant-confidential content are **never**
transferred into shared memory. Eligible shared artifacts are things like: a verified skill, a
generalized strategy, a tool-use improvement, a failure pattern, a tested workflow, a non-identifying
semantic fact, a safety/reliability lesson.

Data is eligible for system-improvement learning only if it is: explicitly **opted-in** by the user
*or* genuinely public and appropriately **licensed**; classified **non-private and non-sensitive**;
free of secrets, credentials and protected identifiers; **purpose-compatible**; **provenance-recorded**;
within retention/deletion policy; and permitted for the intended learning use. "Non-private" alone is
not sufficient, and **opt-in alone does not license system-wide readability** (§ tenancy below).

Promotion into shared Simorgh knowledge requires, as gated steps: **de-identification; eligibility
classification; provenance; quality evaluation; poisoning / prompt-injection screening; copyright /
licence checks; independent verification (`ADR-0003`); tenant-leakage checks; Simorgh-governed
promotion.** **Simorgh is the sole authority that consolidates or promotes across agents.** Because
promotion is the primary cross-tenant / privacy leak boundary, **de-identification, eligibility
classification and poisoning / prompt-injection screening execute system-side, are not
agent-influenceable, and are hardened against agent-crafted evasion or encoding** — in the same sense
as the default-sovereign recognizer (§1); a compromised agent cannot shape memory content to slip
encoded private data past the classifier.

**Corroboration, not repetition.** Because ordinary writes are self-authored, promotion quality
evaluation must weight **independent-source corroboration** and **cap the influence of any single
agent's self-authored provenance chain** — a high-volume, internally-consistent body of one agent's
own memory is not evidence of truth and must not clear promotion by volume.

**Output tenancy (the leak boundary).** The output of consolidation **inherits the tenancy of its
inputs**:

- **Single-tenant input** → output is shared only *within that tenant*, never across tenants; a result
  derived from tenant A's memory is never readable by tenant B.
- **Multi-tenant input** → output is **non-shared by default**. It may become **system-wide** only if
  **every** input is *independently* eligible (opt-in or public+licensed) and the result provably
  derives from **no** tenant's private memory. A pattern distilled from A's + B's private memory is
  **not** system-wide.
- **Residual / mosaic disclosure** is screened: individually-de-identified promotions must not
  cumulatively re-identify a tenant or user (an aggregation/anonymity threshold, not just per-artifact
  de-identification). Single-source distillations are treated as **high-risk**.

Simorgh may distribute verified generalized learning to other agents **without exposing the originating
user or tenant**. **Poisoned-skill blast radius** is bounded: promoted skills roll out via
**canary/staged** distribution, and rollback of a poisoned or low-quality promoted artifact **reaches
every consumer** that received it.

The pipeline must support: **revocation** from future learning use; **deletion / cryptographic
erasure** (see §5); **supersession** of incorrect knowledge; **confidence recalibration**; **provenance
chains**; and **rollback** of poisoned or low-quality promoted knowledge.

### 5. Lawful deletion, erasure and retention (no absolute "never delete")

Corrections **supersede** prior records with provenance — but supersession is not the only path. The
model must support: **configurable retention**; **Owner-requested deletion**; **tenant/user deletion**;
**GDPR / right-to-erasure**; **cryptographic erasure** (key destruction, so backups and replicas
become unreadable without per-record purge); and **legal or policy retention holds**.

- A **non-sensitive audit/tombstone record** may prove that an action occurred, but it **must not
  preserve the deleted private payload** — and "payload" **includes derived embeddings/vectors**, which
  can leak source text. Only **non-sensitive audit evidence is retained, and only where legally
  permitted** (a legal hold or a lawful-basis retention requirement); where retention is not permitted,
  even the audit record is minimised to the non-sensitive fact of the act.
- Erasure reaches **derived memories, embeddings and backups/replicas**: erasing a source record
  triggers **supersession/rollback of promoted artifacts and embeddings sourced from it**, reaching
  every consumer (§4), and **cryptographic erasure (key destruction) renders backups and replicas
  unreadable** without a per-record purge. De-identified artifacts that provably retain no path to the
  source are out of erasure scope; those that do are in.
- **Per-erasure-unit key granularity (normative).** Cryptographic erasure requires **per-record (or
  per-erasure-unit) key granularity**, so that destroying one subject's key renders **only** that
  subject's records — including embeddings and backup/replica copies — unreadable, without a shared key
  either defeating erasure or over-erasing other subjects' recoverability.
- **Erasure is verified by positive, checkable facts, not a self-report.** Erasure verification
  (`ADR-0003`) measures that a known ciphertext becomes **undecryptable after key destruction** and that
  **derivative supersession/rollback is observed at every recorded consumer** — never a MemoryBus
  self-attestation that erasure "occurred" (a Verifier cannot observe absence-of-data across every
  replica).
- **The ADR-0003 verification stores are in scope.** Verifier evidence bundles and persisted
  attempt/completion history — which may retain private file contents (`ADR-0003` §4) and stand up
  independently of MemoryBus — are **tenant-partitioned durable stores subject to this same §5 regime**:
  deletion, retention, legal hold and cryptographic erasure. On a tenant/user erasure request their
  retained private payloads are erased (key destruction reaching backups/replicas); only the
  **non-sensitive, non-payload completion fact** may be retained, and only where legally permitted.
- **Legal-hold vs erasure precedence:** a legal hold **suspends** erasure of the held records; a user
  erasure request during a hold is **queued and honoured on release** (lawful basis recorded), never
  silently dropped or unlawfully executed. Records under hold remain **encrypted, tenant-isolated and
  access-logged** for the hold's duration.

Deletion, erasure, holds, propagation and supersession are **normative, testable behaviours**
(§ acceptance criteria), not aspirations.

### 6. Growth is monotonic under governance (non-regression), and gating is not over-applied

Capability, memory and learned skill must **not weaken over time or after an engine swap**. Gate or
Simorgh **denial of a promotion is an authorization decision, not a capability cap** — it refuses a
specific boundary-crossing effect and must never be tuned into a de-facto ceiling on what an agent may
learn or hold in its own scoped memory. Conversely, **an ordinary scoped operation must not be silently
forced through the gate** (over-gating is itself a regression and is tested, §acceptance).

### Why not the alternatives

- **Route every agent memory write through Simorgh/the gate.** Rejected: it makes ordinary learning
  slow and cognitively constrained, and misreads the boundary — the gate is for *sovereign*/shared
  memory, not an agent's own delegated namespace.
- **Store memory in the engine / model context.** Rejected: memory would die on every engine swap and
  could not be tenant-isolated, audited, or erased on request.
- **Absolute "supersede, never delete."** Rejected: it cannot honor Owner/tenant deletion, GDPR
  erasure or legal holds, and a tombstone that keeps the payload (or its embedding) is itself a leak.
- **Promote raw "non-private" data, or treat opt-in as system-wide.** Rejected: "non-private" alone is
  insufficient; only distilled, de-identified, eligibility-classified, screened, independently-verified
  artifacts may be promoted, and opt-in licenses tenant-scoped, not system-wide, readability.
- **Cap agent memory/intelligence to keep it "safe."** Rejected: the goal forbids cognitive ceilings;
  isolation + scope + system-side gate + resource policy already contain a maximally capable agent.
- **Let agents share knowledge peer-to-peer.** Rejected: peer promotion has no cross-tenant firewall;
  consolidation routes through Simorgh, the one authority that can enforce non-leakage on input *and*
  output.
- **Trust write volume as corroboration.** Rejected: a single agent's self-authored repetition is not
  independent evidence; promotion weights independent corroboration.

## Consequences

- The **ADR-0003 verified-execution foundation is built first and governs #26/#27 and, per ADR-0003,
  every later implementation slice (orchestration #30, frontend #32 included)**: MemoryBus and the
  learning pipeline report completion only through the independent Verifier, against measured evidence,
  and can never self-declare `VERIFIED_COMPLETE`.
- A **MemoryBus service slice (#26)** (agent-owned + Simorgh-consolidated namespaces; autonomous
  scoped read/write; system-side sovereign recognizer; per-scope write quota/back-pressure; gated
  promotion; tenant isolation; provenance/confidence system-attribution; deletion / crypto-erasure /
  retention / legal-hold / derivative-propagation; supersession) is the prerequisite the sandbox (#17)
  and workspace ingress (#19) build on.
- A **learning-pipeline slice (#27)** depends on both MemoryBus (#26) and **Simorgh's consolidation
  authority existing** — its "Simorgh-governed promotion" acceptance cannot be met until Simorgh is
  built (see the Simorgh architecture doc's integration order).
- The `youtab.agent-command.v1` **memory-scope grammar** gains a write-capable `(…, purpose)`
  own-scope; the `youtab.effect-proposal.v1` **promotion record** and the **completion/evidence**
  binding (see `ADR-0003`) need concrete shapes.
- The Effect Gate gains a **promotion / learning-pipeline** decision path; ordinary scoped memory ops
  do **not** traverse it.
- No engine, chat surface or frontend may read/write agent memory except through the MemoryBus
  contract — Chat and Agent must not grow divergent memory stores.

## Open questions (must be answered at ratification)

1. **Boundary refinement** — accept the sovereign-vs-own-scope split and the system-side, default-
   sovereign recognizer, and amend **both** boundary clauses (line 13 and line 15) to grant a
   write-capable own-scope. (Ratification-blocking; it changes an invariant.)
2. **MemoryBus substrate** — datastore, and how the 5-tuple keying, isolation, per-scope quotas and
   **cryptographic erasure** (incl. backups/replicas) are enforced at rest.
3. **Consolidation split & output tenancy** — what an agent may *propose* vs. what only Simorgh may
   decide, and the admission rule for multi-tenant-input / system-wide output. (Ratification-blocking:
   leakage boundary.)
4. **Retention, erasure & mosaic** — retention tiers; legal-hold semantics; the tombstone/audit shape
   that proves an act without keeping the payload *or its embedding*; the aggregation/anonymity
   threshold for residual disclosure.
5. **Skill representation** — how skill memory is stored and *safely* replayed within the current
   command's toolsets (the fail-closed rule is fixed in §3; the representation is open).

## Acceptance criteria (executable — for the implementing slices, not this ADR)

Any slice that opens memory code must prove:

- An agent **autonomously** creates/updates/consolidates/reads its own five tiers within its scope
  **without** an Effect-Gate round-trip; **and an ordinary scoped op is not silently forced through the
  gate** (no over-gating).
- **An agent cannot reclassify a sovereign write as ordinary** — the recognizer is system-side,
  default-sovereign, fail-closed.
- **Per-scope write quota / rate limit / back-pressure** bounds write volume without capping what may
  be learned/held.
- A read or write **outside the command's allowed scopes** (incl. cross-`purpose`), or **cross-tenant**,
  is refused.
- **Promotion, cross-agent/user/workspace/purpose access, external effects, global policy changes and
  sensitive actions each require the gate.**
- **Engine swap preserves** an agent's memory, learned skill and identity (non-regression).
- **Recalled memory cannot widen** the signed contract; a **skill replay** referencing a tool outside
  the current command's toolsets is refused.
- Provenance/confidence are **system-attributed** (origin, not truth); agent-asserted values are
  refused; **source links carry no identifier**.
- **Consolidation output tenancy:** single-tenant-input output is not readable by another tenant;
  **multi-tenant-input output is non-shared unless every input is independently eligible and no private
  input is used**; mosaic re-identification across promotions is screened.
- **Promotion classifiers are system-side and un-gameable:** de-identification, eligibility
  classification and poisoning/injection screening run system-side and **reject agent-crafted evasion /
  encoding** — an agent cannot shape memory content to slip encoded private data past them.
- **Deletion/erasure:** Owner/tenant/user deletion and GDPR/cryptographic erasure remove the private
  payload **and its embeddings**, **propagate to promoted derivatives and every consumer**, and reach
  **backups/replicas** via **per-erasure-unit key destruction** (one subject's key destroys only that
  subject's records); erasure is proven by **ciphertext-undecryptable + supersession-observed-at-every-
  consumer**, not a self-report; a retained **audit/tombstone does not preserve the payload**; a **legal
  hold** suspends erasure (held records stay encrypted/tenant-isolated/access-logged) and the request is
  honoured on release.
- **ADR-0003 verification stores obey §5:** Verifier evidence bundles and attempt/completion history
  are tenant-partitioned and, on erasure, their retained private payloads are erased (reaching
  backups/replicas) while only the non-sensitive completion fact may be retained where legally permitted.

### Learning-pipeline mutation controls (each turns RED when the control is removed, GREEN when restored)

- **private data cannot be promoted;** **opt-out data cannot be promoted;**
- **cross-tenant source identity cannot leak** (input, single- or multi-tenant consolidation output);
- **forged provenance is rejected;** **single-agent self-authored volume cannot clear promotion**
  (corroboration required);
- **unverified agent claims cannot enter shared memory;**
- **poisoned or prompt-injected memory is quarantined**, and a **poisoned promoted skill rolls back to
  every consumer** (bounded blast radius);
- **agent-crafted evasion cannot defeat the system-side de-identification / eligibility / screening
  classifiers;**
- **revocation and deletion (incl. cryptographic erasure) work and reach derivatives/embeddings**, and
  **erasure reaches the ADR-0003 evidence/attempt-history stores** (backups/replicas via
  per-erasure-unit key destruction).

### General mutation controls

Removing the tenant-isolation check, the **system-side sovereign recognizer**, the **system-side
promotion-classifier hardening**, the **write-quota** control, the memory-scope (incl. `purpose`)
check, the **over-gating** guard, the recalled-memory-cannot-widen-scope check, the multi-tenant
consolidation-output check, the skill-replay toolset check, the **working-tier zeroing-on-deactivation**
guard, the system-attribution of provenance/confidence, the **per-erasure-unit key granularity**, or
the deletion/erasure-propagation path (**including to the ADR-0003 evidence stores**) each turns the
suite **red**; restoring each returns it **green**.

## Non-goals

This ADR selects no datastore, defines no wire schema, and authorizes no code, deployment, or Simorgh
integration. It is the decision that gates those; they proceed only after ratification. Verified
completion, the corrective loop, sub-agent decomposition, live execution visibility and verifier
independence are decided in the companion **ADR-0003**.
