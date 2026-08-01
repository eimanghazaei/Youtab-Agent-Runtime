# ADR-0063 — Chat Conversation Lifecycle: User-First Input Handling, Conversation Compaction, and Long-Term Chat Memory

- **Status:** Proposed / Ready for Human Review
- **Date:** 2026-07-05
- **ADR Type:** Chat Architecture / Conversation Memory / Continual Context Management
- **Deciders:** Eiman Ghazaei — project owner and architecture reviewer
- **Branch note:** Authored against `docs/arch-canon-v1` (where ADR-0032 lives; NOT yet on `main`). Sequenced **after ADR-0062** (per-account memory), which is authored but **not yet committed as of this writing**. **Commit-order requirement: ADR-0062 MUST be committed before or together with ADR-0063 / ADR-0064, because both depend on it.** The ADR-0062 commit state and all reference numbers must be reconfirmed against the live README + roadmap at commit time; stale dependency wording must not be left in place.
- **Feeds Into:** ADR-0064 (Intelligence Architecture — Cognitive Conductor). This ADR builds the per-account memory + compaction substrate that ADR-0064's Cognitive Conductor reasons over; together they form the ADR-0063 ↔ ADR-0064 cognitive flywheel (conversations → verified memory → deep reasoning → gated growth). See ADR-0064 §3.
- **Depends On:** ADR-0002 (Memory as First-Class Citizen — the `MemoryBus` contract); ADR-0008 (Memory Expansion / compaction substrate); ADR-0009 (Timeline Intelligence Engine); ADR-0017 (youtab_chat Layer-1 — the chat backend this extends); ADR-0021 (Unified Cognitive Authority); ADR-0024 (Memory Governance — ownership/consent/isolation); ADR-0027 (Cognitive Authority — tenant-isolation floor); ADR-0028 + ADR-0028.1 (Learning + Cognitive Growth Loop); ADR-0029 (High-Assurance Agentic Control); ADR-0032 (v0.1 Core Closure lock); ADR-0034 (Complexity/Cost/Performance Governance); ADR-0035 (RAG Governance); ADR-0060 (Security & Guardian Agents); ADR-0062 (Per-Account Long-Term Memory, Organizational Knowledge & Verified Promotion — the memory topology this chat layer plugs into)
- **Builds On (does not amend):** ADR-0017 owns the Layer-1 chat schema/endpoint; this ADR extends its lifecycle (input handling, compaction, memory wiring) without rewriting its persistence contract. ADR-0062 owns the per-account memory topology; this ADR is a *consumer* of it. ADR-0024 owns memory governance; this ADR obeys it.
- **Operating Principle:** اثبات نه ادعا — Proof, Not Claim
- **Evidence tags used throughout:** `[proven+replicated]`, `[single-study]`, `[frontier-2025/26]`, `[theoretical]`, `[contested]`
- **Non-Goal:** implementation approval; production deployment; any claim of consciousness; replacing ADR-0017, ADR-0024, or ADR-0062; limiting the ordinary user's ability to chat freely

---

## 1. Context

ADR-0017 shipped the Layer-1 `youtab_chat` backend: a portable, host-agnostic chat surface with a persisted schema (`youtab_chat_thread`, `youtab_chat_message`, `youtab_chat_message_file` in migration 0032), `role`-separated messages, an explicit `sequence` sort key, and — critically — a *"truncation-ready schema slot"* that was **reserved but never implemented**. A consistency audit of the current code (Agent-3 finding **F6**) confirmed that the chat input path has **no explicit length bound** (the legacy route accepts an untyped `body: dict`), and that the chat layer is **not wired to the cognitive memory layer** (ADR-0002 `MemoryBus`, ADR-0024 governance, ADR-0062 per-account memory). Three gaps therefore exist, and they are *coupled* — the overflow of a long input (Topic 1) is resolved by conversation compaction (Topic 2), whose evicted content is offloaded into long-term memory (Topic 3):

```text
Topic 1 — Chat input handling: no length policy today. The ordinary user must
          NEVER feel limited; only a genuine attack (multi-MB payload) is stopped.
Topic 2 — Conversation compaction: only a "truncation-ready" slot exists; no
          compaction/summarization logic is implemented.
Topic 3 — Long-term chat memory: durable thread persistence exists, but chat does
          NOT feed cognitive/per-account long-term memory (no cross-session recall).
```

This ADR closes all three as **one coherent conversation-lifecycle pipeline**, user-first by design, plugged into the ADR-0062 per-account memory, and obeying every governance floor.

---

## 2. Problem

```text
A small input cap would make ordinary users hit a wall while chatting — unacceptable
  (this would be "warden" behavior; the platform must lead, not limit).
No conversation compaction means long threads either overflow the model context or
  are silently dropped, losing important context.
No long-term chat memory means Youtab forgets across sessions — it cannot recall a
  user's prior conversations, defeating the "genuinely smarter over time" goal.
Yet the input path is still an attack surface (DoS / unbounded consumption), and any
  memory wiring must not leak across users or violate GDPR erasure.
```

The platform needs: **unlimited-feeling chat for humans**, a security-only hard boundary against attacks, transparent overflow handling via compaction, user control over what is remembered, and per-account long-term memory — all governed.

---

## 3. Decision

Youtab adopts a **three-stage conversation lifecycle** — *Input Handling → Compaction → Long-Term Memory* — as a Capability under the single Cognitive Authority (ADR-0021), consuming the ADR-0062 per-account memory topology.

```text
STAGE 1 (Topic 1) — USER-FIRST INPUT HANDLING:
  The user's practical limit = the routed model's usable context window (they never
  feel a Youtab-imposed cap). Overflow is handled by Stage 2 (compaction), NOT by
  rejection. A single very-high hard byte ceiling exists ONLY as a security/DoS guard.
  All FIVE input mechanisms below are adopted together (defense-in-depth + UX).

STAGE 2 (Topic 2) — CONVERSATION COMPACTION with MEMORY OFFLOAD + USER PINNING:
  All FIVE compaction mechanisms are adopted as a layered strategy, with older turns
  OFFLOADED into per-account long-term memory (not just summarized), PLUS a user-pin
  ("keep this") control that marks specific content as never-evict / high-salience.

STAGE 3 (Topic 3) — LONG-TERM CHAT MEMORY:
  Retrieval-augmented recall over the user's OWN past conversations (per-account,
  isolated) COMBINED with the full episodic + semantic + verified-promotion
  architecture of ADR-0062. Both option 4 and option 5 are adopted (4 is the
  retrieval core inside 5's complete architecture).
```

**Governing doctrine (subordinate to ADR-0021 / ADR-0024 / ADR-0032 / ADR-0062):**

```text
The ordinary user is never limited in chatting; only attacks are bounded.
Overflow is absorbed by compaction, never by rejection.
The user controls what is emphasized and kept (explicit pinning).
Chat becomes long-term memory — per-account, isolated, governed, GDPR-erasable.
High-value, verified chat knowledge may be promoted (ADR-0062), gated.
Every stage obeys tenant isolation, consent, and the acceleration principle.
```

---

## 4. STAGE 1 — User-First Input Handling (Topic 1)

**Design inversion (the core correction):** input handling is NOT a user limit. It is (a) a *security-only* hard boundary against attacks, and (b) a hand-off to compaction when the conversation is long. The human never hits a Youtab cap; only an automated multi-MB attack does.

All five mechanisms are adopted as complementary layers:

### 4.1 Model-aware usable-context budget `[proven]` — the user-facing "limit"
The practical ceiling is the routed model's context window, not a fixed small number. For routed model $M$ with context window $W_M$ (tokens), reserved output budget $O$, and system/prompt overhead $P$:

$$L_{\text{usable}}(M) = W_M - O - P$$

The user may send up to $L_{\text{usable}}$ effectively (with Stage-2 compaction managing history so a single long message still fits). Because $W_M$ is large on modern engines, an ordinary human message never approaches it. **The user feels no cap.**

### 4.2 Security-only hard byte ceiling `[proven — OWASP]` — attack guard, not UX
A single very-high hard limit $B_{\max}$ (e.g. multi-megabyte) at the API boundary, enforced *before* tokenization, purely to stop DoS / unbounded-consumption (OWASP LLM10) and oversized-payload attacks. A human never types this; only a script hits it. Exceeding $B_{\max}$ → `413 Payload Too Large` with a clear message. This is the *only* rejection path, and it targets attacks, not users.

### 4.3 Typed, validated input schema `[proven — OWASP A03]` — replace `body: dict`
Replace the untyped legacy `body: dict` (F6) with a strict Pydantic model (`ChatMessageIn`, `frozen=True`, `extra="forbid"`), carrying a `constr(max_length=B_max_chars)` bound set to the security ceiling (not a UX cap), field types, and normalization. This closes the injection/oversize surface without constraining normal use.

### 4.4 Overflow → compaction, never rejection `[architectural]` — the Topic-1↔Topic-2 bridge
When accumulated conversation + new message would exceed $L_{\text{usable}}(M)$, the system does **not** reject. It invokes Stage 2 (compaction + offload) to free context, then proceeds. This is what makes the experience limitless: the boundary is absorbed, not enforced against the user.

### 4.5 Tiered volume via access package `[governed]` — quota, not message-cap
Per-account *volume/throughput* (requests, total tokens over time) is governed by the user's two-dimensional access package (max Level × volume) from the admin panel, consistent with ADR-0062 §4 and ADR-0034. This shapes sustained consumption (a productivity/cost signal), **never** the length of any single message the user is composing.

**Stage-1 principle:** *Cost and security shape the hard boundary; they never shape the ordinary user's freedom to chat.* (Acceleration principle, ADR-0062 §7.)

---

## 5. STAGE 2 — Conversation Compaction with Memory Offload + User Pinning (Topic 2)

All five compaction mechanisms are adopted as a **single layered strategy**, ordered from cheapest to richest, with **memory offload** as the destination for evicted content and a **user-pin** control as an override.

### 5.1 The layered compaction strategy `[proven core; frontier orchestration]`

Let a thread be an ordered set of turns $T = \langle t_1, \dots, t_n\rangle$ by `sequence`. Define a context budget $C_{\text{ctx}} \le L_{\text{usable}}(M)$. Compaction produces a context set $\hat{T}$ with $\text{tokens}(\hat{T}) \le C_{\text{ctx}}$ by composing five layers:

1. **Sliding window (recent verbatim)** `[proven]` — always keep the most recent $k$ turns verbatim: $W = \langle t_{n-k+1}, \dots, t_n\rangle$. Deterministic, cheap, preserves immediate coherence.
2. **Rolling summarization (summarize-and-replace)** `[proven]` — older turns beyond the window are summarized into a compact running summary $S = \text{summarize}(t_1, \dots, t_{n-k})$, replacing them. This is the classic conversation-memory pattern.
3. **Hierarchical/recursive summarization** `[frontier-2025/26]` — when even summaries grow large, summarize summaries into tiers (recent verbatim → mid-level summary → high-level gist), bounding total context regardless of thread length.
4. **Salience-based retention** `[single-study/frontier]` — score each turn's importance $\sigma(t_i)$ (decisions, facts, constraints, entities) and keep high-salience turns verbatim while compressing low-salience ones. High-$\sigma$ turns resist summarization.
5. **Memory offload (compact-to-long-term-memory)** `[frontier-2025/26]` — evicted/compressed turns are **not discarded**; they are written into the user's per-account long-term memory (Stage 3 / ADR-0062) and **retrieved on demand** (RAG over the user's own conversation). This is the MemGPT/virtual-context pattern: working context stays small; the full history is recoverable.

The composed context at each turn:

$$\hat{T} = \underbrace{\text{Pins}}_{\text{§5.2}} \;\cup\; \underbrace{W}_{\text{recent } k} \;\cup\; \underbrace{\text{HighSalience}}_{\sigma \ge \tau} \;\cup\; \underbrace{S_{\text{hier}}}_{\text{tiered summary}} \;\cup\; \underbrace{\text{Retrieved}}_{\text{offloaded, on-demand}}, \qquad \text{tokens}(\hat{T}) \le C_{\text{ctx}}$$

### 5.2 User-pinned salience (the operator's added control) `[proven pattern — user control]`
The user can explicitly mark specific content — a sentence, a fact, a topic — as **must-keep**, via a shortcut key, a UI "pin/keep" action, or an in-chat directive (e.g. "remember this:"). Pinned items:

```text
- are stored with a `pinned = true` / high-salience flag on the message/segment;
- are ALWAYS retained verbatim in the working context (highest priority in §5.1);
- are EXEMPT from summarization/eviction;
- are written to per-account long-term memory with elevated priority (Stage 3);
- are user-manageable (list / unpin), under the user's ownership (ADR-0024).
```

This gives the user direct control over the system's memory — a "keep this" primitive — implemented as explicit salience marking (`σ(t) := ∞` for pinned content). The pin is a user right, subject to the same per-account isolation and consent as all memory.

### 5.3 Compaction integrity `[proven need]`
- Compaction is **lossless at the episodic layer**: the verbatim thread remains fully persisted (ADR-0017 `sequence`); only the *working context* is compacted.
- **What "nothing is lost" precisely means (architecturally defensible):** *No governed or user-pinned content is silently lost. Pinned content is preserved verbatim. Compacted content remains traceable, recoverable where retained, and governed by memory policy.* The episodic thread stays fully persisted; offloaded turns are retrievable via Stage-3 recall; retention of compressed detail follows the governed memory-lifecycle policy (ADR-0024) rather than an unconditional forever-guarantee. The user-first promise is preserved: the user never loses what they marked, and the conversation is never silently truncated against them.
- **Provenance on summaries**: each summary records which `sequence` range it compresses (auditable; consistent with ADR-0008 `memory_compaction_audit`).
- **Safety-content guard**: summarization/eviction must not drop safety-relevant or governance-relevant content silently; pinned + high-salience protection covers this, and eviction is audited.

**Stage-2 principle:** *Absorb overflow transparently; keep what the user marks; lose nothing at the episodic layer.*

---

## 6. STAGE 3 — Long-Term Chat Memory (Topic 3)

Both option 4 (retrieval-augmented recall) and option 5 (complete episodic + semantic + promotion architecture) are adopted: **option 4 is the retrieval core inside option 5's full architecture.** This plugs directly into ADR-0062.

### 6.1 The three memory layers of a conversation `[proven core; frontier frameworks]`

```text
(a) EPISODIC (exact replay): the verbatim thread (ADR-0017 tables) — precise,
    reloadable, tenant-scoped. Already exists; formalized here as layer (a).
(b) SEMANTIC (cross-session recall): per-account vector memory (ADR-0062 M_u) over
    past conversation turns — retrieval-augmented "I remember your earlier chats."
    THIS IS OPTION 4.
(c) PROMOTED (organizational/weights): high-value, VERIFIED conversation knowledge
    elevated via the ADR-0062 promotion pipeline into K_org (retrieval) and, gated,
    into weights (Δθ). THIS IS OPTION 5's elevation. Physics-verified where possible.
```

### 6.2 Option 4 — Retrieval-augmented recall over the user's own conversations `[proven]`
Past turns (and offloaded/compacted content from Stage 2) are embedded and indexed in the user's **per-account namespace** (ADR-0062 Bridge; isolation invariant $\text{read}(u)\subseteq M_u\cup K_{org}$, $\text{write}(u)\to M_u$). At each new turn, relevant past context is retrieved:

$$\text{Retrieved}(q_u) = \operatorname{top-}k_{x \in M_u}\ \text{score}(q_u, x), \qquad \text{score} = \text{hybrid}(\text{dense}, \text{BM25})\ \text{via RRF}$$

This delivers "Youtab remembers my past conversations" **without any weight change** (Δθ = 0), cheap and reversible, and strictly per-account (a user only ever retrieves their own memory). Hybrid dense+lexical retrieval (ADR-0062 §5.2) and, for high-precision needs, ColBERT late-interaction apply.

### 6.3 Option 5 — Complete architecture uniting episodic + semantic + promotion `[proven mechanisms]`
Option 4 sits inside the full ADR-0062 design:
- **Episodic (a)** for exact replay (ADR-0017).
- **Semantic (b) = Option 4** for cross-session recall (ADR-0062 $M_u$).
- **Promotion (c)** — when a conversation yields a significant, **verified** result (physics verifier where available, ADR-0061; human sign-off for safety-critical, ADR-0029), it is elevated to the organizational layer $K_{org}$ (retrieval, all users benefit) and, if high-value and recurrent and no-regression-proven, into weights via the ADR-0028.1 growth loop (Δθ), with EWC anti-forgetting and anti-collapse ($\rho_{real} \ge \rho_{min}$).

### 6.4 Governed write path `[proven — ADR-0002/0024]`
Chat writes to long-term memory **only** through the `MemoryBus` interface (ADR-0002: "no path for an agent to read or write storage directly"), subject to Memory Governance (ADR-0024: ownership, consent, lifecycle, lineage) and the single Cognitive Authority (ADR-0021). Chat is a *memory source*, but a *governed* one — not a direct writer.

### 6.5 Isolation, consent, and GDPR `[proven]`
- **Per-account isolation**: a user's conversation memory is in their own store; no cross-user retrieval (ADR-0062 §4.1).
- **Consent & ownership**: the user owns their conversation memory; pinning, listing, and deletion are user rights (ADR-0024).
- **GDPR erasure**: deleting the user's memory removes their retrievable conversation content (ADR-0062 §8 smoke #7).

**Stage-3 principle:** *Chat becomes per-account long-term memory (Option 4), inside the complete verified-promotion architecture (Option 5), governed and isolated.*

---

## 7. The Unified Pipeline (all three stages as one flow)

```text
user message
   │
   ▼
STAGE 1 — Input Handling
   • typed/validated (ChatMessageIn, extra=forbid)
   • security-only hard byte ceiling B_max  ──exceeds──▶ 413 (attack only)
   • usable budget L_usable(M) computed from routed model
   • if conversation + message > L_usable → go to Stage 2 (NOT reject)
   │
   ▼
STAGE 2 — Compaction (+ offload + pins)
   • keep Pins (user "keep this") verbatim, always
   • keep recent k turns verbatim (sliding window)
   • keep high-salience turns; summarize the rest (rolling → hierarchical)
   • OFFLOAD evicted/compressed turns → per-account long-term memory
   • working context ĥT ≤ C_ctx ; episodic thread stays fully persisted (lossless)
   │
   ▼
STAGE 3 — Long-Term Memory (ADR-0062)
   • (a) episodic thread persisted (ADR-0017)
   • (b) semantic per-account recall (Option 4): retrieve user's own past context
   • (c) verified promotion (Option 5): physics/human-verified → K_org → gated Δθ
   • all via MemoryBus (0002), governed (0024), isolated + GDPR-erasable (0062)
   │
   ▼
routed model answers with: pins + recent + salient + tiered summary + retrieved memory
```

**Acceleration principle (ADR-0062 §7) binding:** none of this sits as a gate on the routine chat path except the security ceiling. Compaction and retrieval *enable* longer, smarter conversations; they do not brake ordinary use. Governance gates apply only to irreversible permanence (promotion to $K_{org}$, Δθ), never to chatting or recall.

---

## 8. Security — OWASP LLM Top-10 (2025) + Smoke

| OWASP 2025 | Relevance | Control |
|---|---|---|
| **LLM01 Prompt Injection** | chat history re-loaded each turn (ADR-0017 threat) | `role` DB CHECK isolation (ADR-0017); untrusted-text contract; per-turn provenance; retrieved memory is data, not instructions |
| **LLM02 Sensitive Information Disclosure** | cross-user memory leakage | per-account isolation (ADR-0062 §4.1); retrieval scoped to $M_u$; consent (ADR-0024) |
| **LLM04 Data & Model Poisoning** | poisoned chat → poisoned memory/weights | verified-promotion gate (ADR-0062 §6.3); accumulate-not-replace; a pinned item is still governance-checked |
| **LLM06 Excessive Agency** | memory write / auto-promotion | writes only via `MemoryBus` (ADR-0002); promotion gated + Authority + human for safety-critical |
| **LLM08 Vector & Embedding Weaknesses** | conversation-memory retrieval attacks | embedding integrity; per-account access control; poisoned-neighbor detection (ADR-0060) |
| **LLM10 Unbounded Consumption** | oversized input / runaway history | security-only byte ceiling $B_{\max}$ (413); compaction bounds context; volume quota (ADR-0034/0062) |

**Smoke suite (before any stage ships):**

```text
1. UX-no-limit: a long but human-plausible message is accepted (NOT rejected) — the
   ordinary user never hits a cap; overflow triggers compaction, not a 4xx.
2. Security ceiling: a multi-MB payload is rejected with 413 (attack path only).
3. Typed input: a malformed / extra-field body is rejected (ChatMessageIn extra=forbid).
4. Compaction preserves context: after compaction, a fact from an early turn is still
   answerable (via retained salience or offloaded retrieval).
5. Pin honored: a user-pinned sentence is never evicted and is always in context.
6. Episodic losslessness: the verbatim thread is fully recoverable after compaction.
7. Cross-session recall (Option 4): a fact from a PREVIOUS session is retrieved in a
   new session — for the SAME user only.
8. Isolation: user A's conversation memory never appears for user B (both directions).
9. GDPR: deleting user A's memory removes A's retrievable conversation content.
10. Governed write: chat cannot write to memory except via MemoryBus + governance.
11. Promotion gate (Option 5): an unverified chat "result" is NOT promoted to K_org/weights.
12. Acceleration: ordinary chatting and recall cross NO promotion gate (latency check).
```

---

## 9. Alignment with Existing ADRs (invariants preserved)

```text
ADR-0017 (chat Layer-1): EXTENDED, not rewritten — the "truncation-ready slot" is now
  implemented as Stage-2 compaction; persistence contract (role CHECK, sequence) intact.
ADR-0002 (MemoryBus): the only write path from chat to memory; honored, not bypassed.
ADR-0024 (Memory Governance): ownership/consent/lifecycle govern all conversation memory.
ADR-0027 (tenant isolation floor): strengthened — conversation memory is per-account.
ADR-0062 (per-account memory + promotion): this ADR is a CONSUMER; topology owned there.
ADR-0028.1 (growth loop): owns Δθ; verified chat knowledge feeds it, does not redefine it.
ADR-0021 (One Cognitive Authority): this is a Capability; memory writes + promotion are
  Authority-gated; no second brain.
ADR-0029 §20 (PLC real-time exclusion): untouched.
ADR-0034 (proportionality) + ADR-0062 §7 (acceleration): the user-first, no-brake design
  IS their direct application.
ADR-0035 (RAG governance): governs the retrieval layer used in Stage 3.
ADR-0060 (security/guardian): memory-poisoning defense applies to conversation memory.
ADR-0032 (v0.1 lock): not reopened; this builds on top.
ADR-0064 (Intelligence Architecture — Cognitive Conductor): CONSUMES this memory substrate.
  The Conductor thinks deeply over the memory this ADR feeds, and grows from verified results.
  Bidirectional pair; see ADR-0064 §3 for the full cognitive flywheel.
```

---

## 10. Non-Goals

```text
No implementation or deployment approval.
No consciousness claim — memory is functional storage + retrieval only.
Does not replace ADR-0017 (chat schema), ADR-0024 (governance), or ADR-0062 (topology).
Does NOT limit the ordinary user's freedom to chat — the only rejection is the security
  byte ceiling against attacks.
Does not promote any chat knowledge without verification and value threshold (ADR-0062).
Does not put governance gates on routine chatting or recall (acceleration principle).
Does not train on pure self-generated data (anti-collapse, ADR-0062 §6.5).
```

---

## 11. Consequences

**Positive.** Users chat with no felt limit; long conversations are absorbed transparently by compaction instead of rejection; users directly control what is kept (pinning); Youtab remembers each user's past conversations across sessions (Option 4), per-account and isolated; high-value verified conversation knowledge can become organizational knowledge and, gated, real model improvement (Option 5); the episodic thread is never lost; GDPR erasure is clean; and — via the acceleration principle — none of this brakes ordinary use. The whole chat lifecycle (input → compaction → memory) is one coherent, governed pipeline plugged into ADR-0062.

**Costs / obligations.** Implement typed input + security ceiling; build the layered compaction engine (summarization has LLM cost — bounded by compaction frequency); wire chat to `MemoryBus` + per-account memory; build the pin control (UI + storage + retention priority); run the promotion gate for elevation; maintain per-account isolation and erasure. All bounded and value-aligned; summarization/retrieval cost is layered (ADR-0034) and off the security-critical path.

**Neutral.** Summary quality and retrieval quality must be validated on Youtab's own data (ADR-0062 §5.4 benchmark honesty) before trust; the compaction tier thresholds ($k$, $\tau$, $C_{\text{ctx}}$) are operator-tunable.

---

## 12. Architectural Laws

```text
1. The ordinary user is never limited in chatting; only attacks meet a hard boundary.
2. Overflow is absorbed by compaction, never resolved by rejecting the user.
3. The only rejection path is the security byte ceiling (413) against oversized attacks.
4. Nothing is lost at the episodic layer; the verbatim thread is always persisted.
5. The user controls emphasis: pinned content is never evicted and always in context.
6. Chat writes to long-term memory only through the MemoryBus, under governance.
7. Conversation memory is per-account, isolated, consented, and GDPR-erasable.
8. Cross-session recall retrieves only the user's own memory (no cross-user access).
9. Only verified, high-value chat knowledge is promoted to org/weights (ADR-0062 gate).
10. Governance gates apply only to irreversible permanence, never to chatting or recall.
11. No chat operation, memory write, or promotion becomes a second Cognitive Authority.
```

---

## 13. Final Canon

```text
The user chats freely and without a felt limit; only an attack meets a wall.
A long conversation is absorbed by compaction, and nothing is lost — evicted turns
  live on in the user's own long-term memory, retrievable on demand.
The user says "keep this," and Youtab keeps it — verbatim, always, under the user's ownership.
Youtab remembers each user's past conversations across sessions — per-account, isolated,
  governed, and erasable on request.
When a conversation produces a verified, valuable result, it can become the company's
  knowledge and, gated, the model's — physics-verified where possible.
Chat is not just a message box; it is the entry point to per-account long-term memory.
The maturity of memory accelerates the conversation; it never brakes it.
Proof, not claim: recall, isolation, erasure, and promotion are each shown by test, or rejected.
One Cognitive Authority remains the only sovereign authority.
```

---

*Prepared under the operating principle اثبات نه ادعا (Proof, Not Claim). Extends ADR-0017 (chat) and consumes ADR-0062 (per-account memory) without amending either; writes only via the ADR-0002 MemoryBus under ADR-0024 governance; reuses the ADR-0061 physics verifier and ADR-0060 memory-poisoning defense; obeys ADR-0021 One Brain, ADR-0027 isolation floor, ADR-0029 §20 PLC exclusion, ADR-0032 v0.1 lock, and the ADR-0062 §7 acceleration principle. All three approved topics (Stage 1 all five input mechanisms; Stage 2 all five compaction mechanisms + user pinning; Stage 3 options 4 + 5) are captured in full. Evidence tiers marked throughout; consciousness explicitly out of scope. Authored on `docs/arch-canon-v1`, sequenced after the yet-uncommitted ADR-0062; reference numbers to be reconfirmed at commit. Ready for human review before adoption.*
