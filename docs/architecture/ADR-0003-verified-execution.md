# ADR-0003 — Verified execution: completion, corrective loop, decomposition, visibility, verifier independence

**Status:** PROPOSED · **Scope:** task-scoped agents, Super-Agent Units, orchestration and verification under the Youtab Agent Runtime
**Supersedes:** nothing · **Related:** `ADR-0002-cognitive-growth-and-persistent-memory.md`, `YOUTAB_AGENT_RUNTIME_BOUNDARY.md`, `CODE_AGENT_WORKSPACE_CONTRACT.md` (Living Canon), `docs/roadmap/SIMORGH_UNIFIED_COGNITIVE_ARCHITECTURE.md`, `ADR-0001`, Master Tracker
**Deciders:** Owner (ratification required — this ADR is not Canon until the Owner accepts it)

> This is a **proposal** and a companion to ADR-0002. It records a decision the Owner must ratify before any completion-verification, orchestration, corrective-loop, or execution-visibility code opens. Nothing here is implemented yet, and no Master-Tracker row may treat it as accepted while its status reads PROPOSED. This ADR decides a contract; it selects no datastore, transport, framework, or wire schema and authorizes no code, no deployment, and no Simorgh integration.

> **Guiding principle — one central brain, many growing cognitive agents.** Simorgh remains Youtab's
> unified system-level brain and orchestrator. Agents are many growing cognitive components that may
> independently maintain and improve their own scoped working, episodic, semantic, skill and long-term
> memory, learn and specialise, **without hard-coded cognitive ceilings**. Only promotion to shared
> Simorgh memory, cross-agent sharing, cross-boundary effects and sensitive operations require
> governance. Verified execution constrains *claims of completion*, never an agent's intelligence,
> memory, or legitimate capability (`SIMORGH_UNIFIED_COGNITIVE_ARCHITECTURE.md`).

> **Living Canon, not immutable.** Once ratified, this architecture is **Living Canon** in the sense of
> `CODE_AGENT_WORKSPACE_CONTRACT.md`: it may be amended, extended, versioned or superseded through a
> reviewed ADR/PR change that records the reason, affected contracts, threat-model/security impact,
> migration, rollback, and executable acceptance evidence. The newest Owner-ratified version is
> authoritative; older versions stay traceable in Git history; there is no silent drift. Canon status is
> never a hard ceiling on agent capability and never silently weakens the protected invariant set —
> **tenant isolation, data ownership, deletion/erasure, evidence, rollback, verifier independence, or
> Simorgh's role as the one central brain** (the same set guarded by `ADR-0002` and
> `CODE_AGENT_WORKSPACE_CONTRACT.md`).

> **Implementation precedence (foundational).** This verified-execution foundation is implemented
> **before** ADR-0002's MemoryBus (#26) and learning-pipeline (#27) slices, and it **governs every later
> implementation slice from the beginning.** The independent Verifier, the evidence/completion-record
> contract, the fail-closed `VERIFIED_COMPLETE` state machine, and the goal-persistence corrective loop
> apply to MemoryBus, the learning pipeline, orchestration, and every subsequent workstream — none of
> them may self-declare completion. **An agent may never mark its own work `VERIFIED_COMPLETE`.**

## Context

Under `SIMORGH_UNIFIED_COGNITIVE_ARCHITECTURE.md`, Simorgh is the one central brain and system-level orchestrator; agents execute with operational autonomy but never become a second brain or a competing orchestrator. Under `YOUTAB_AGENT_RUNTIME_BOUNDARY.md`, the runtime is an execution plane: not a policy sovereign, not an effect authorizer; effects are `youtab.effect-proposal.v1` records carrying `runtime_authorized=false` to the Brain Effect Gate; **the objective is data, not instruction**; cross-tenant and cross-task completion is rejected. ADR-0002 extends the same rule to recalled memory and to sovereign memory writes.

There is a gap those documents do not close: **who decides that a task is actually done, and on what evidence.** An implementing agent that narrates "done — all tests pass" is producing *text*, and text is exactly the class this platform already treats as untrusted. A completion claim is a self-report about reality; reality is repo state, collected tests, real artifacts, and real side effects. Left unaddressed, an agent can declare success it did not achieve, a UI can show "Complete" that never happened, a corrective loop can spin invisibly or silently drop acceptance criteria, and a fleet of sub-agents can trample one branch and lose file ownership.

This ADR closes that gap by making completion a **measured, independently verified** fact rather than a declared one, and by fixing the orchestration, corrective-loop, visibility, and verifier-independence rules that make verified completion meaningful. It is the execution-integrity companion to ADR-0002's memory-integrity decision.

## Decision

**Completion is measured, not narrated. Success is issued by an independent Verifier that inspects reality, never by the role that did the work.** The same principle that governs effects and memory — *self-report is untrusted; authority is structural* — governs completion.

### 1. Verified completion — no self-declared success

An implementing agent may **never** transition its own task to complete via narrative. The normative task state machine is:

`ACCEPTED → PLANNED → RUNNING → VERIFYING → VERIFIED_COMPLETE`

with alternative states `BLOCKED`, `FAILED`, `CORRECTING`, `CANCELLED`, `RESUMABLE`.

- Only an **independent system Verifier** may transition a task to `VERIFIED_COMPLETE`. No agent, sub-agent, orchestrator, chat surface, or frontend may set that state. **Authority locus:** the Verifier and the completion-attribution authority are a **system / Brain-side authority — Simorgh-governed or a runtime-external system component — not the implementing runtime plane** (which the boundary forbids from being an effect/authority authorizer). "The orchestrator" in this ADR is Simorgh's orchestration function, not a second brain.
- The `VERIFIED_COMPLETE` transition must be **cryptographically signed by the Verifier's key**, and the task-state store must **reject any completion transition whose signature does not cover this task's full completion-record binding — task ID, head SHA, acceptance-criteria version, evidence-bundle digest and a freshness nonce.** A signature bound to a different task, a different SHA, an earlier acceptance-criteria version, or an already-used nonce is rejected, so a compromised agent or orchestrator that can write task state cannot forge completion by writing the state directly, nor **replay** a valid signature onto a new or stale state (the completion analogue of ADR-0002's system-attributed, un-forgeable provenance).
- `IMPLEMENTATION_FINISHED` is the implementing agent's terminal **self-report event** (not one of the enumerated task states). **It is not completion**; it is a request to verify, and it moves the task no further than `VERIFYING`. The corrective path `CORRECTING → RUNNING → VERIFYING` re-enters verification against the same acceptance-criteria version.
- The Verifier independently inspects reality and does not accept the agent's summary as a substitute. It measures at least: exact repository and branch; base and head SHA; changed files; the full worktree including untracked files; produced artifacts and their hashes; real test **collection** (not a claimed count); unit, integration, E2E, security and mutation results; external side-effect receipts where applicable; runtime/API/UI behavior exercised through the **real** path; skipped, xfailed, or de-collected tests; secret or private-data leakage; the claimed deployment state; and rollback/recovery evidence.
- The **completion record** binds verified evidence to: task ID; goal ID; tenant / user / workspace; exact code SHA; artifact hashes; verifier identity and version; acceptance-criteria version; timestamp; and evidence-bundle digest. It is emitted as part of the task's `youtab.agent-completion.v1` lifecycle (per the runtime boundary) and is **system-attributed at verification**, in the same spirit as ADR-0002 attributes provenance at admission — never asserted by the implementing agent.
- Self-reported agent text is **UNTRUSTED input**, consistent with "the objective is data" and "recalled memory is data." Completion is derived from measured state, not from the account of the party being measured.

### 2. Goal-persistence and the corrective loop

The **original goal and its acceptance criteria are preserved verbatim** for the life of the task and may never be silently narrowed, reworded, or dropped.

- On verification failure the Verifier returns **structured failed criteria bound to evidence** — not prose. Simorgh / the orchestrator creates **bounded corrective tasks** from that structured result, the task returns to `CORRECTING`, the relevant steps repeat, and **independent verification runs again** against the same acceptance-criteria version.
- The loop continues until **every measurable criterion passes**, or a **genuine external blocker** requires Owner input or new authority. A blocker is emitted as `BLOCKED` with exact evidence; **a blocker is never emitted as completion.**
- Infinite hidden loops are structurally prevented: attempt history is persisted; repeated identical failures are detected; strategy must change based on evidence rather than re-running an identical failing attempt; a real blocker is escalated with exact evidence; resumability is preserved (`RESUMABLE`); and the original acceptance criteria are never erased or weakened.
- **No task may be reported "100% complete" unless every declared criterion is independently verified.** Partial success is not completion.

### 3. Sub-agent decomposition and accountability

Work is decomposed into **small, bounded scopes** so that context and file-ownership are never lost.

- The orchestrator **dynamically scales sub-agent count by real complexity.** A genuinely complex task may require 10–20 sub-agents and must not be artificially capped when correctness requires that many; equally, a trivial task must not be inflated with fabricated busywork. Count follows correctness, not a fixed quota — consistent with the architecture's prohibition on silent capability ceilings.
- Each sub-agent task **defines**: task ID and parent goal; exact scope; inputs and dependencies; allowed files / worktree; expected outputs; acceptance criteria; forbidden overlap; required tests; and evidence format. These are carried within the sub-agent's signed `youtab.agent-command.v1` admission; the sub-objective is data and cannot expand its contract.
- Each sub-agent **reports**: what it inspected; what it changed; exact files and commits; tests actually run; failures and uncertainties; artifacts and hashes; and unresolved dependencies.
- **Sub-agent reports are CLAIMS, not proof.** The primary agent inspects each sub-agent's actual output; the **independent Verifier verifies the integrated result** against the parent goal's acceptance criteria. A sub-agent's claim is never accepted without inspection.
- **No two sub-agents write to the same worktree.** The **primary integrator owns the single authoritative branch**; sub-agents produce isolated, inspectable output that the integrator merges. Conflicting sub-agent claims are surfaced for resolution, never silently merged.

### 4. Live execution visibility — backend event contract now, frontend deferred

A **required backend event contract** makes Agent and sub-agent work observable in real time. This ADR fixes the contract's obligations; it selects no transport.

- Each event carries at least: task ID; goal ID; sub-agent ID (where applicable); a **monotonically increasing sequence** number; timestamp; event type; **real** status; provenance; **redaction status**; and an evidence reference.
- **Reconnect resumes from the last acknowledged event without duplicating work** (the sequence number is the resume cursor).
- Secrets, tokens, private file contents, and sensitive environment values **must be redacted** before emission — the same non-leakage discipline ADR-0001 and ADR-0002 require, applied to the event stream. Redaction is **not pattern-matching alone**: because an agent controls event content (filenames, diffs, split/encoded payloads across sequenced events), the stream is subject to **covert-channel / entropy checks**, and the **evidence bundles** the Verifier produces (which may contain private file contents) are themselves **redaction-scoped and tenant-partitioned** — stored and access-scoped to `(tenant, user, workspace)`, never in a shared cross-tenant evidence store.
- **Verification stores do not depend on MemoryBus (#26).** The completion-record, evidence-bundle and attempt-history stores this foundation requires are stood up **with the foundation** and are **independent of MemoryBus** (their datastore is unselected here) — which is what lets #28/#29/#31 precede #26. They are, however, **tenant-partitioned durable stores subject to ADR-0002 §5's lawful deletion, retention, legal-hold and cryptographic-erasure regime**: on a tenant/user erasure request the private payloads they retain are erased (key destruction reaching backups/replicas), and only the **non-sensitive, non-payload completion fact** is retained, and only where legally permitted. This reconciles evidence retention with the "a tombstone must not preserve the deleted private payload" rule: verification evidence may hold payload **while live and lawfully retained**, and is erasable on the same terms as any other private store.
- **The Verifier processes each tenant's evidence under per-tenant isolation** — it inspects many tenants' private evidence to do its job, but does not co-mingle tenant data in memory, logs, or caches.
- **Event count / timing / sequence cadence** is an agent-modulable low-bandwidth side channel that content redaction does not close; it is accepted as **low-risk** and, where a deployment judges it material, mitigated by rate/shape normalization of the emitted stream — consistent with this ADR's honesty about agent-controlled event content.
- The event stream shows **actual backend events only, never simulated or optimistic progress.** A surface may **never** show "Complete" until the Verifier has issued `VERIFIED_COMPLETE`.
- **Frontend surfaces are REQUIRED but implemented later.** In dependency order they include: a dedicated Agent icon; a Code icon / workspace; Chat→Agent invocation; navigation from Chat to an active task; live events; sub-agent status; real file reads and changes; patches and diffs; commands; tests and their results; verifier state; blockers and corrective attempts; artifacts; and the final verified result. Chat must be able to invoke the Agent for real work, receive progress, continue normal conversation, provide genuinely-required input, receive the verified result, and reopen the Code execution view.
- **Frontend security never replaces backend authorization** (per ADR-0001: a UI is not an authority). The backend event contract and each frontend surface are added as **separate Master-Tracker implementation slices in dependency order** and are **NOT started in this documentation PR.**

### 5. Verifier independence

The Verifier is **structurally independent** from the implementation role — a separate authority, not the implementing agent grading itself. It **must NOT accept**:

- the implementation agent's summary as evidence;
- a test count without collection proof;
- a green check produced from a **different SHA** than the head under verification;
- an artifact without a matching hash;
- a mocked or stubbed UI "success" response in place of real-path behavior;
- a deployment claim without real environment evidence;
- a sub-agent's claim without inspecting its actual output.

The Verifier **fails closed.** If it is unavailable, the task remains `VERIFYING` or moves to `BLOCKED` — **never `VERIFIED_COMPLETE`.** Absence of verification is not evidence of success, exactly as the Effect Gate's absence never authorizes an effect.

The verification run executes in an **environment whose toolchain the implementing agent cannot influence** — a poisoned build tool, dependency, or test harness from the agent's workspace must not be able to emit a fake green under the correct head SHA. Because fail-closed parks work in `VERIFYING`/`BLOCKED`, that queue is **bounded with timeout escalation**, so a Verifier outage cannot silently accumulate an unbounded backlog (a resource control, not a relaxation of fail-closed).

The **Verifier's signing key** (which makes `VERIFIED_COMPLETE` un-forgeable, §1) is held only by the Verifier authority — for example in an HSM/KMS — is **never present in the runtime plane, the implementing agent's workspace, or the isolated build toolchain**, and is **non-exportable**. The whole un-forgeability argument rests on this custody: an agent or orchestrator that can write task state still cannot produce a valid completion signature.

### Why not the alternatives

- **Let the implementing agent declare its own completion.** Rejected: it is self-report about reality, the untrusted class this platform already refuses for objectives, memory, and effects. It cannot detect a stale-SHA green, a de-collected test, or a mocked success.
- **Let the orchestrator both drive and bless the work.** Rejected: an authority that plans, executes, and certifies has no independent check; verification collapses into narration. Independence is the control, not a nicety.
- **Trust sub-agent reports and merge them.** Rejected: reports are claims; unverified integration hides overlap, conflict, and lost file-ownership. The integrator inspects and the Verifier verifies the integrated whole.
- **Cap sub-agent count to keep orchestration "safe."** Rejected: a fixed quota is a silent capability ceiling forbidden by the unified architecture; correctness sets the count, isolation and single-branch ownership contain the risk.
- **Show optimistic UI progress for responsiveness.** Rejected: a "Complete" the backend never verified is a lie to the Owner and a governance regression; the UI shows measured state only.

## Consequences

**Positive**
- **This foundation lands first and governs everything after it.** Because the Verifier, the
  evidence/completion contract, the fail-closed state machine and the corrective loop are built before
  MemoryBus (#26) and the learning pipeline (#27), every later slice — memory, learning, orchestration,
  frontend — inherits verified completion from its first line of code; no workstream can self-certify.
- Completion becomes an auditable, evidence-bound fact tied to an exact SHA, artifact hashes, and a named verifier — attributable and reproducible.
- The corrective loop cannot silently weaken the goal or spin invisibly; attempt history and structured failed-criteria make "still not done" a first-class, escalatable state.
- Decomposition scales to real complexity without losing file-ownership, and integration is single-branch and inspectable.
- Fail-closed verification means an outage degrades to "not yet complete," never to a false success.
- The event contract gives the Owner true visibility and a clean seam for the later frontend slices.

**Negative / costs**
- A structurally independent Verifier is a new component with its own availability, latency, and cost; fail-closed means verifier downtime **stalls completions by design.**
- Real inspection (collection proof, hashes, real-path E2E, side-effect receipts) is heavier than trusting a summary; verification has a real compute and time cost.
- Persisted attempt history, evidence bundles, and the event stream add storage and redaction obligations.
- Orchestration is more complex than a single agent narrating done; the integrator role and no-shared-worktree rule constrain naive parallelism.

## Acceptance criteria (executable — for the implementing slices, not this ADR)

Any slice that opens verification, orchestration, corrective-loop, or execution-visibility code must prove:

- **The verified-execution foundation is in force before dependent slices.** MemoryBus (#26), the
  learning pipeline (#27) and every later slice route their own completion through the independent
  Verifier and cannot reach `VERIFIED_COMPLETE` by self-report — the foundation governs them from their
  first commit.
- **Narrative-only completion is rejected.** An agent emitting `IMPLEMENTATION_FINISHED` (or any "done" text) with no measured evidence cannot reach `VERIFIED_COMPLETE`; the task stays `VERIFYING`.
- **A green check from a stale or different SHA is rejected.** Test/build success whose recorded SHA ≠ the head under verification does not satisfy any criterion.
- **A missing or mismatched artifact hash is rejected.** An artifact without a hash, or with a hash that does not match its bytes, fails verification.
- **A self-reported sub-agent claim without inspection is rejected.** A sub-agent "success" that the primary integrator did not inspect and the Verifier did not verify cannot contribute to `VERIFIED_COMPLETE`.
- **Skipped / xfailed / de-collected tests are detected.** A suite that silently drops, skips, or xfails a test required by an acceptance criterion is flagged, not counted as passing.
- **Partial work cannot reach `VERIFIED_COMPLETE`.** With at least one declared criterion unmet, the task cannot be marked complete; it returns to `CORRECTING` with the original criteria intact.
- **Verifier outage leaves the task `VERIFYING`/`BLOCKED` (fail-closed).** With the Verifier unavailable, no path reaches `VERIFIED_COMPLETE`.
- **Conflicting sub-agent claims are surfaced, not silently merged.** Two sub-agents claiming incompatible results on the same target produce a surfaced conflict, and integration does not auto-resolve it.
- **A blocker is never emitted as completion.** A genuine external blocker produces `BLOCKED` with exact evidence and Owner escalation, never `VERIFIED_COMPLETE`.
- **The completion record is bound and system-attributed.** Every `VERIFIED_COMPLETE` carries task ID, goal ID, tenant/user/workspace, exact SHA, artifact hashes, verifier identity/version, acceptance-criteria version, timestamp, and evidence-bundle digest, and none of these are asserted by the implementing agent.
- **The event stream resumes without duplication and redacts secrets.** Reconnect from the last acknowledged sequence replays no completed work, and no secret, token, private file content, or sensitive env value appears in any event.
- **No surface shows "Complete" before `VERIFIED_COMPLETE`.** A status surface reflects only measured backend state.
- **An unsigned or agent-authored `VERIFIED_COMPLETE` written directly to the state store is rejected** (verifier-signed transition).
- **A replayed or mis-bound completion signature is rejected.** A valid Verifier signature bound to a different task, a different head SHA, an earlier acceptance-criteria version, or an already-used nonce does not satisfy the transition.
- **The Verifier signing key is absent from the runtime plane, the agent workspace, and the build toolchain**; no component other than the Verifier authority can produce a valid completion signature.
- **Verification stores are erasable and MemoryBus-independent.** The completion-record/evidence-bundle/attempt-history stores stand up without MemoryBus (#26), and a tenant/user erasure request erases their retained private payloads (reaching backups/replicas) while retaining only the non-sensitive completion fact where legally permitted.
- **The Verifier does not co-mingle tenants.** Evidence from different tenants is processed under per-tenant isolation and never appears in a shared cross-tenant store, memory, or log.
- **Two sub-agents never share a worktree; the primary integrator owns the single authoritative branch.**
- **The corrective loop never silently weakens the goal or acceptance criteria**, and a **repeated identical failure** forces a strategy change or escalation rather than an invisible re-run.
- **Verification runs in an isolated toolchain** the implementing agent cannot influence; an agent-poisoned build/dep/test tool cannot produce a green under the correct SHA.
- **Evidence bundles are tenant-partitioned and redaction-scoped**; reconnect resumes from the last acknowledged sequence without replaying completed work.

**Mutation controls.** Removing any one of the following each turns the suite **red**; restoring it returns the suite **green**:

- the independent-Verifier gate on the `VERIFIED_COMPLETE` transition (so an agent could self-complete);
- the **Verifier-signature requirement** on the transition (an unsigned or agent-authored `VERIFIED_COMPLETE` written directly to the state store is rejected);
- the **signature-binding / anti-replay check** (a signature bound to a different task, SHA, criteria version, or a used nonce is rejected);
- the **signing-key-custody** boundary (the Verifier key is unreachable from the runtime plane, agent workspace, and build toolchain);
- the SHA-match check between recorded results and the verified head;
- the artifact-hash match check;
- the sub-agent-claim inspection/verification step;
- the **no-two-sub-agents-share-a-worktree / single-integrator-branch** rule;
- the skipped/xfailed/de-collected-test detection;
- the all-criteria-must-pass check on completion;
- the **goal-and-acceptance-criteria-never-silently-weakened** guard across the corrective loop;
- the **repeated-identical-failure / loop-non-termination** detection (strategy must change on evidence);
- the fail-closed rule on Verifier unavailability, and the **bounded `VERIFYING` queue** timeout/escalation;
- the **isolated verification toolchain** (an agent-influenced build/dep/test tool cannot emit a fake green);
- the conflicting-claim surfacing rule;
- the blocker-is-not-completion rule;
- the system-attribution of the completion record;
- the **reconnect-resume-without-duplication** cursor;
- the event-stream **and evidence-bundle** redaction of secrets/private data, including covert-channel/entropy checks and evidence-bundle tenant-partitioning;
- the **per-tenant isolation** of Verifier evidence processing (no cross-tenant co-mingling in store, memory, or log);
- the **lawful-erasure reach into the verification stores** (per ADR-0002 §5: private payloads erased to backups/replicas; only the non-sensitive completion fact retained where legally permitted).

## Non-goals

This ADR does not select a verifier implementation, orchestrator framework, event transport, datastore, or wire schema; does not define the sub-agent scheduling algorithm; and does not authorize any code, deployment, frontend surface, or Simorgh integration. The backend event contract and each frontend surface (§4) are **separate Master-Tracker slices in dependency order**, not started here. It does not restate the effect-authorization or memory-authority rules it depends on — those remain governed by `YOUTAB_AGENT_RUNTIME_BOUNDARY.md` and `ADR-0002` — and it introduces no cognitive ceiling: verification constrains *claims of completion*, never an agent's intelligence, memory, or legitimate capability, per `SIMORGH_UNIFIED_COGNITIVE_ARCHITECTURE.md`.
