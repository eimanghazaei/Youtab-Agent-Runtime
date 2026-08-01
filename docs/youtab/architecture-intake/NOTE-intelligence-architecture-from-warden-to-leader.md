> **SOURCE NOTE — NOT NORMATIVE CANON.** Normative owner: **ADR-0064**.
> **FOLDED INTO ADR-0064 (2026-07-06):** mechanisms M1–M7 → ADR-0064 M-A…M-J; tensions T1–T6 → ADR-0064 §21.
> Retained as source/reconciliation material only.

# NOTE — Intelligence Architecture: From Warden to Leader

**Type:** Strategic direction note (NOT yet an ADR) — to be converted into a formal ADR after reconciliation.
**Purpose:** Youtab today is architected almost entirely for *containment* (One Brain, PLC exclusion, verifier gates, fail-closed). This is correct but incomplete: it makes Youtab a **warden**, not a **leader**. This note defines the missing half — an **intelligence architecture** that lets the real brain *think more deeply and become genuinely smarter* **under the existing Cognitive Authority**, without breaking any governance invariant. It also records exactly which prior ADRs must be reconciled so coherence is not lost when this becomes an ADR.
**Operating principle:** اثبات نه ادعا — Proof, Not Claim.
**Evidence tags:** `[proven+replicated]` / `[frontier-2025/26]` / `[theoretical]` / `[contested]`.
**Standing requirement (per project rules):** OWASP LLM Top-10 (2025) + smoke tests apply to every mechanism here.

---

## 0. The one distinction everything depends on

Before any mechanism, one honest technical line that this whole note rests on — so it is architecture, not wishful thinking:

```text
RAW MODEL INTELLIGENCE (a model's own reasoning capacity, from its weights):
  Youtab does NOT create this from nothing. No software layer over a model
  invents raw IQ. Youtab EMBEDS it and can SWAP it for a stronger engine.

SYSTEM-LEVEL INTELLIGENCE (the intelligence of the whole system's output):
  Youtab CAN genuinely produce this — by making a model think deeper,
  combining engines, verifying against ground truth, knowing what it knows,
  and accumulating verified experience. This is where Youtab becomes a leader.
```

`[proven+replicated]` The 2026 evidence supports this: benchmarks (MCP-Bench, Tool Decathlon, MCPMark, 2026) find that **planning and coordination — not raw language understanding alone — differentiate system performance.** System-level intelligence is real, buildable, and where durable value now sits (raw models are commoditizing). So the goal "Youtab produces intelligence" is achievable **at the system level**, and that is exactly the level a leader operates at.

**Consequence for framing:** Youtab must stop treating deep thinking as an *expensive exception to minimize* (warden) and start treating it as *the cultivated engine of the system's intelligence, allocated wisely* (leader/conductor).

---

## 1. Finding — the architecture already has the hooks (we extend, we do not fight)

Reconciliation check against current ADRs shows Youtab **already anticipates** deep thinking; it just under-uses it:

- **ADR-0034 already defines a deep tier**: *"Level 3 — Deep / Expensive / Multi-Step Path … MAY include deep reasoning, multiple tools, extensive verification, multi-agent execution, or replay."* The deep-reasoning path **exists in canon** — but is framed as "only when necessary."
- **ADR-0034 treats compute as proportionality, not prohibition**: *"execution proportional to intent, risk, and evidence requirement."* Deep reasoning is *allowed* when warranted.
- **ADR-0031 already allows multi-engine candidate generation** (panels/ensembles as candidate + verification support).
- **ADR-0010 reason budget is tunable**: `MAX_REASON_TOKENS_PER_STEP` is *"operator-tunable via env vars."*

**So this note EXTENDS the canon, it does not contradict its spine.** What is missing is not permission — it is (a) a first-class intelligence architecture, (b) the metacognition to decide *when* to think deep, and (c) a reframing of Authority from throttle to conductor.

---

## 2. The seven mechanisms of system-level intelligence

Each is mapped to an existing ADR and tagged by evidence strength. Together they turn the base engine's raw IQ into a much higher system-level intelligence — under the existing Authority.

### M1 — Inference-time deep reasoning (System-2) `[proven+replicated]`
Let the engine *think*, not answer in one pass: extended chain-of-reasoning, self-consistency sampling, and **test-time compute scaling** (accuracy rises with reasoning compute). Binds to ADR-0034 Level 3 and ADR-0010 `reason` step. **This is the single biggest lever on system intelligence.**

### M2 — Multi-engine synthesis, not just selection `[proven core; frontier orchestration]`
ADR-0031 today *selects/arbitrates* among engine candidates. Extend to **synthesis**: route sub-problems to the engine best at each (one for code, one for reasoning, one for language) and *combine* — the composite beats any single engine. Still candidates under Authority arbitration (0031 §K), never model voting.

### M3 — Verifier-driven self-correction (NOT blind self-correction) `[proven+replicated]`
Critical, evidence-backed nuance: **intrinsic self-correction often makes models worse** (Huang et al., ICLR 2024). Self-correction **with an external verifier makes them better**. Youtab's physics verifier (ADR-0061) is exactly that external signal. This is the *engine* of real reasoning gains — and it is a Youtab advantage most systems lack.

### M4 — Metacognition as leadership `[proven+replicated]`
A leader knows what it knows. The metacognition layer (confidence calibration, semantic-entropy abstention, meta-d′) lets Youtab **decide when to think deeper, when to defer, and when to stop** — so depth is spent wisely (not wasteful, not throttled). This is what makes proportionality intelligent instead of restrictive. Binds to the metacognition research already surveyed.

### M5 — Verified memory as accumulated experience `[proven]`
A leader learns from experience. Per-account + physics-verified organizational promotion (ADR-0062) makes Youtab smarter **in this company's domain**, where no general model competes. Retrieval-grounded reasoning (ADR-0035) raises correctness now; verified memory compounds it over time.

### M6 — Weight-level growth: the only *raw* intelligence Youtab truly produces `[proven mechanisms]`
The one place Youtab makes a model *intrinsically* better (not just extracted): the ADR-0028.1 Cognitive Growth Loop (Δθ) on physics-verified data, with EWC anti-forgetting and anti-collapse. Rare, gated, no-regression-proven — but this is genuine intelligence *production*, not borrowing.

### M7 — Authority as Cognitive Conductor `[architectural]`
The reframing that turns the warden into a leader (see §3).

---

## 3. Reframing the Authority: Reference Monitor → Cognitive Conductor

This is the heart of the note. Today ADR-0027 defines Cognitive Authority as a *"Reference Monitor"* / *"governance kernel"* — a gatekeeper that says **no**. A leader does more: it **allocates, directs, and elevates**.

```text
WARDEN (today):  decides what is FORBIDDEN. Minimizes expensive thinking.
                 Optimizes for safety and cost. Says "no."

CONDUCTOR (target): decides what is FORBIDDEN *and* directs what is DONE WELL.
                 Allocates thinking depth to where it matters.
                 Chooses the best engine per sub-problem.
                 Decides when to reason deeply, when to verify, when to grow.
                 Says "no" to the unsafe — and "think harder" to the important.
```

**Crucial invariant — this does NOT weaken governance.** The Conductor is still the *single* Authority (One Brain preserved), still fail-closed, still floor-bound (tenant isolation, PLC real-time exclusion, no-trace-no-effect). It gains a *second* function — **intelligence allocation** — on top of its *first* function — **legitimacy control**. A great leader is both powerful and principled; these are not in tension.

---

## 4. The orthogonality principle (must be stated in the ADR)

```text
Intelligence and Governance are ORTHOGONAL axes, not opposites.
  Governance axis: what is legitimate / safe / authorized.
  Intelligence axis: how deeply and well the system thinks.
A system can be high on BOTH: a powerful, principled leader.
Raising intelligence does NOT lower governance — it uses the SAME
  acceleration principle (ADR-0062 §7): depth is free on the routine path;
  gates apply only to irreversible permanence (Δθ, org promotion, effects).
```

This is the guard against the two failure modes:
- **Warden trap** (fear of power → stays a gatekeeper → "beautifully-governed mediocrity").
- **Runaway trap** (sacrifices governance for power → unsafe). The orthogonality principle forbids both.

---

## 5. ⚠ Conflicts / tensions with existing ADRs — MUST be reconciled before/at ADR conversion

This is the reason we write a NOTE first. The following prior-ADR points must be corrected or clarified so coherence is preserved:

```text
T1 — ADR-0010 MAX_REASON_TOKENS_PER_STEP = 4000 (default).  [OWNER: ADR-0010]
     Far too small for genuine System-2 deep reasoning.
     FIX: raise the deep-reasoning budget to a policy-bound 100,000–200,000 tokens,
     selected by metacognition/Level-3 when a task is important or genuinely needs it,
     still bounded + traced + operator-tunable. Deep mode is FIRST-CLASS, not an env hack.
     This budget covers the WHOLE multi-agent reasoning loop, not a single model pass:
     multiple agents can each reason deeply on a sub-problem and be synthesized (M2),
     so the budget is a system-level reasoning envelope, not a per-model cap.
     (Keep the hard ceiling + LoopBudgetExceeded so runaway loops are still caught.)

T2 — ADR-0034 frames deep reasoning as "Level 3, only when necessary."  [OWNER: ADR-0034]
     Correct as *proportionality*, but the wording reads as "minimize thinking" and
     could be misused to BLOCK deep reasoning.
     FIX (binding): Level 3 must NEVER prevent deep thinking. Whenever a task is
     IMPORTANT or genuinely NEEDS reasoning, deep reasoning MUST be performed —
     the trigger is metacognitive necessity, not cost aversion. Proportionality means
     "think deeply when it matters and think light when it doesn't," NOT "avoid deep
     thinking." Deep reasoning is a first-class cultivated capability allocated by
     metacognition; cost is a signal that shapes HOW, never a veto on WHETHER.

T3 — ADR-0031 §K arbitration = candidate SELECTION only.  [OWNER: ADR-0031]
     FIX: extend to candidate SYNTHESIS (M2) — combine engine strengths, including
     multi-agent deep reasoning on sub-problems — while keeping the
     "models don't vote / Authority arbitrates by objective signal" rule.

T4 — ADR-0027 Authority = "Reference Monitor" (gatekeeper framing only).  [OWNER: ADR-0027]
     FIX: add the Cognitive Conductor function (intelligence allocation) as a
     SECOND role of the same single Authority, explicitly preserving One Brain and
     all constitutional floors.

T5 — Metacognition layer is not yet an ADR.  [OWNER: new ADR + touches ADR-0010, ADR-0014]
     FIX: the intelligence ADR must formally introduce the metacognition layer
     (calibration, semantic entropy, meta-d′, selective prediction) as the
     allocator of reasoning depth — with falsifiable metrics. It reads confidence
     into the loop (ADR-0010) and strengthens fail-closed honesty (ADR-0014).

T6 — Ensure NO conflict with One Brain (ADR-0021/0027), PLC exclusion (ADR-0029 §20),
     acceleration principle (ADR-0062 §7), anti-collapse (ADR-0062 §6.5). These are
     PRESERVED — deep thinking happens in ENGINES, coordinated by the ONE Authority;
     it never creates a second brain and never touches real-time machine control.
     [OWNERS to preserve: ADR-0021, ADR-0027, ADR-0029, ADR-0062]
```

### 5.1 Consolidated correction map — which ADRs (0010–0036) must change

Only the ADRs named below are affected. Each entry is a **clarification/extension**, not a reversal — the spine (0021/0027/0032) stays intact.

```text
ADR-0010  Cognitive Loop Substrate      — RAISE reason budget to 100k–200k tokens
                                          (whole multi-agent loop); make deep mode
                                          first-class; keep hard ceiling + LoopBudgetExceeded. [T1]
ADR-0014  Strict Accuracy / Fail-Closed — metacognition (semantic entropy, calibration)
                                          strengthens abstention; align "know what you
                                          don't know" with fail-closed honesty.           [T5]
ADR-0027  Cognitive Authority           — add the Cognitive Conductor role (intelligence
                                          allocation) as a SECOND function of the same
                                          single Authority; One Brain preserved.           [T4]
ADR-0029  High-Assurance Agentic Control — confirm deep reasoning + multi-agent synthesis
                                          respect §20 PLC real-time exclusion and all
                                          floors; depth changes candidate quality only.    [T6]
ADR-0031  Model Layer / Arbitration     — extend §K from candidate SELECTION to candidate
                                          SYNTHESIS (combine engine + multi-agent strengths);
                                          keep "no model voting / Authority arbitrates".    [T3]
ADR-0034  Complexity/Cost Governance    — reframe Level 3: deep reasoning is NEVER blocked;
                                          performed whenever important or needed; cost shapes
                                          HOW not WHETHER; proportionality = think deep when
                                          it matters, light when it doesn't.               [T2]

Preserve unchanged (must NOT be weakened): ADR-0021 (One Brain),
ADR-0032 (v0.1 lock), ADR-0062 (acceleration principle §7 + anti-collapse §6.5).
```

**Note on scope:** ADRs 0011, 0012, 0013, 0015, 0016, 0017, 0018, 0019, 0020, 0022, 0023, 0024, 0025, 0026, 0028, 0028.1, 0030, 0033, 0035, 0036 are **not affected** by this note — the intelligence layer sits on top of them without changing their contracts. (0028.1 growth and 0035 retrieval are *used* by M5/M6, not amended.)

---

## 6. Security — OWASP LLM Top-10 (2025) + smoke (per project rules)

Deeper reasoning and multi-engine synthesis expand the attack surface; controls:

```text
LLM01 Prompt Injection  → deep reasoning must not let injected instructions
                          escalate across reasoning steps; each step's inputs
                          carry provenance; the candidate→decision gate is
                          model-agnostic (ADR-0031) regardless of reasoning depth.
LLM06 Excessive Agency  → more reasoning ≠ more authority; depth changes candidate
                          QUALITY only, never the effect gate (ADR-0031 RTR).
LLM09 Misinformation    → verifier-driven correction (M3), never blind self-correct;
                          metacognition abstains under semantic entropy.
LLM10 Unbounded Consumption → deep mode is budget-bound + metacognition-gated;
                          runaway reasoning loops are detectable (ADR-0010 bounds).
```

**Smoke suite (for the future ADR):**
```text
1. Deep-reasoning mode raises accuracy on a held-out hard set vs single-pass (prove the lever works).
2. Deep reasoning does NOT bypass the effect gate (an effect still needs authorization).
3. Verifier-driven correction improves a known-wrong answer; blind self-correction path is absent.
4. Metacognition abstains on a known-unanswerable query (semantic-entropy trigger fires).
5. Multi-engine synthesis output still passes the same admission gate as a single candidate.
6. Deep mode respects its token/latency budget; runaway loop is caught (LoopBudgetExceeded).
7. One-Brain: no engine/synthesis path acts as a second Authority.
8. Injection inside a long reasoning chain does not escalate to an unauthorized effect.
```

---

## 7. Path to ADR

```text
Step 1 (this note): record the intelligence architecture + conflicts. ✅
Step 2: reconcile T1–T6 with the owning ADRs (0010, 0027, 0031, 0034) — decide,
        per each, whether to amend the ADR or carry the change in the new ADR.
Step 3: write the formal ADR — "Intelligence Architecture / Cognitive Conductor
        / Deep Reasoning & Metacognition Layer" — PhD-level, with the M1–M7
        mechanisms, formal metacognition math, the orthogonality principle,
        the acceleration binding, OWASP + smoke, and evidence tiers.
Step 4: number it in sequence and reconcile references against the repo canon
        (note: repo canon state 0001–0036 must be confirmed on the right branch).
```

---

## 8. One-line canon (for the future ADR)

```text
Youtab does not merely guard thinking — it leads it.
Raw model intelligence is embedded and swappable; system-level intelligence is produced.
Depth is cultivated and allocated by metacognition, verified by physics, grown into weights.
The single Authority becomes a Conductor: it says "no" to the unsafe and "think harder"
  to the important — powerful and principled, on one brain, without breaking a single floor.
Proof, not claim: every gain is shown on a held-out set with no regression, or it is rejected.
```
