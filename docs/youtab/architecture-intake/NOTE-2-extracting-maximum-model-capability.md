> **SOURCE NOTE — NOT NORMATIVE CANON.** Normative owner: **ADR-0064**.
> **FOLDED INTO ADR-0064 (2026-07-06):** mechanisms M-A…M-E preserved as ADR-0064 M-A…M-E.
> Concrete workforce-size figures generalized to enterprise / organization scale (see §3).
> "{0010,0031,0034}" here is the CONFLICT subset only; the full reconciliation scope is ADR-0064 §21.
> Retained as source/reconciliation material only.

# NOTE 2 — Extracting Maximum Model Capability (Latent-Power Release)

**Type:** Strategic direction note (companion to NOTE 1 "From Warden to Leader") — to be folded into the Intelligence ADR.
**Purpose:** NOTE 1 established that Youtab must *lead* thinking, not just guard it, and that it produces **system-level** intelligence. This note answers the sharper question: **can Youtab make the brain use the FULL capability of the embedded model — higher power and performance — and if so, what is the real, proven method?** Answer: **yes.** This note gives the proven mechanisms, with mathematics, for releasing a model's *latent* reasoning power (not exceeding the model — fully unlocking what it already has).
**Operating principle:** اثبات نه ادعا — Proof, Not Claim.
**Evidence tags:** `[proven+replicated]` / `[frontier-2025/26]` / `[theoretical]` / `[contested]`.
**Standing requirement (project rules):** OWASP LLM Top-10 (2025) + smoke tests apply to every mechanism.

---

## 0. The one distinction that makes this real, not wishful

```text
IMPOSSIBLE: make a model understand BEYOND its trained capacity.
            Raw capacity is locked in the weights; no wrapper adds raw IQ.

PROVEN & POSSIBLE: extract the FULL capacity the model already has but does
            NOT reveal in a single fast pass. A model answering in one shot
            shows only a fraction of what it can do. Given the right process,
            the SAME model performs dramatically better.
```

**Key fact `[proven+replicated]`:** a single greedy forward pass is a *lower bound* on a model's ability. The gap between "one-pass answer" and "full-capability answer" is large and recoverable by test-time process. Youtab's job is to close that gap — this is releasing latent power, and it is exactly what a leader-brain does with its engines.

---

## 1. The five proven mechanisms (with mathematics)

Ordered by leverage. Every one is peer-reviewed and implementable on today's hardware.

### M-A — Test-Time Compute Scaling  `[proven+replicated]` — the biggest lever
Let a fixed model with weights $\theta$ produce accuracy $A$ as a function of test-time compute $C$ (reasoning tokens, samples, search width). Empirically $A(C)$ **increases** with $C$ up to saturation:

$$A(\theta, C_2) > A(\theta, C_1) \quad \text{for } C_2 > C_1 \ \ (\text{until saturation})$$

Snell et al. (2024, DeepMind) show a **compute-optimal** test-time strategy lets a smaller model, given more test-time compute, **match or beat a model ~14× larger** on hard reasoning — *without changing the model*. Pretraining compute and test-time compute are partially exchangeable:

$$\text{Performance} \approx f(C_{\text{pretrain}},\, C_{\text{test}}), \qquad \partial f/\partial C_{\text{test}} > 0$$

**Youtab binding:** this is precisely the 100k–200k reasoning-token budget in NOTE 1 (T1). Allocate more compute to important tasks → the same engine gets sharply smarter.

### M-B — Chain-of-Thought + Self-Consistency  `[proven+replicated]`
Instead of one answer, sample $m$ reasoning paths $r_1,\dots,r_m$ (temperature > 0), each yielding answer $a_i$. Take the **majority (mode)**:

$$\hat{a} = \arg\max_{a} \sum_{i=1}^{m} \mathbb{1}[a_i = a]$$

Works because correct reasoning tends to **converge** on the same answer while errors are **diffuse and uncorrelated**. Wei et al. (2022, chain-of-thought) + Wang et al. (2023, self-consistency) — large, replicated gains on reasoning benchmarks. Releases latent reasoning the greedy path hides.

### M-C — Best-of-N + Verifier  `[proven+replicated]` — Youtab's decisive form
Sample $N$ candidates $y_1,\dots,y_N$; a verifier $V$ scores each; select the best:

$$\hat{y} = \arg\max_{i \in \{1,\dots,N\}} V(y_i)$$

With a **perfect** verifier, achievable accuracy is the **pass@N** bound (probability at least one of $N$ samples is correct):

$$\text{pass@}N = 1 - (1-p)^N, \qquad p = P(\text{single sample correct})$$

So even a modest per-sample $p$ becomes high accuracy as $N$ grows — **bounded by verifier quality**. This is where Youtab wins: **in engineering, physics IS the verifier** (FEA convergence, tolerance closure, ST compile+sim, ADR-0061). Physics / solver / compiler verifiers are high-quality and often low-marginal-cost domain verifiers when their validity conditions hold → Youtab can approach the pass@N bound on physically checkable results. Generate N designs, let physics rank them — well above a single pass. Verifier quality still bounds the gain.

### M-D — Multi-Agent Deep Reasoning + Synthesis  `[proven core; frontier orchestration]`
Decompose task $T$ into separable sub-tasks $\{T_1,\dots,T_k\}$; each solved by an agent (each itself allowed deep reasoning under M-A); synthesize:

$$\hat{S} = \Phi\big(a_1, a_2, \dots, a_k\big), \quad a_j = \text{Agent}_j(T_j)$$

When sub-tasks are separable and the synthesis operator $\Phi$ is sound, the composite exceeds any single agent. 2026 benchmarks (MCP-Bench, Tool Decathlon) confirm **coordination**, not raw language alone, differentiates performance. This is the operator's own point: **agents help deep thinking.** The 100k–200k budget (T1) is the envelope for the *whole* multi-agent reasoning loop, not one model.

### M-E — Process Reward Models (step-level guidance)  `[proven+replicated]`
Instead of scoring only the final answer (outcome reward), score **each reasoning step** $s_t$ and guide search over reasoning (beam/tree):

$$R_{\text{PRM}}(r) = \operatorname{agg}_t\, \rho(s_t) \quad (\operatorname{agg} = \min \text{ or } \textstyle\sum), \qquad \hat{r} = \arg\max_r R_{\text{PRM}}(r)$$

Lightman et al. (2023, "Let's Verify Step by Step"): **step-level supervision outperforms outcome supervision** and keeps the model on correct reasoning trajectories — extracting its full step-by-step competence rather than gambling on the final token.

---

## 2. The allocation problem — when to spend maximum capability (metacognition leads)

Maximum capability is **not free** (see §3). The leader-brain must decide *when* it is worth it. Formalize as **rational metareasoning / value of computation** (Russell & Wefald):

$$C^{*} = \arg\max_{C}\ \Big[\, \mathbb{E}\big[\text{value}(A(\theta, C))\big] \;-\; \text{cost}(C) \,\Big]$$

The **metacognition layer** (NOTE 1 M4: calibration, semantic entropy, meta-d′) estimates the marginal accuracy gain $\partial A/\partial C$ and the task's importance, then allocates compute where the gain justifies the cost. This is what makes proportionality (ADR-0034) *intelligent*: deep capability is spent **when it matters**, never blocked when needed (T2), never wasted on the trivial.

---

## 3. Honest cost analysis (keeps the method real at enterprise / organization scale)

```text
Test-time compute is REAL compute: more tokens, more samples, more wall-clock, more $.
  best-of-N  ≈ N× inference cost.
  self-consistency(m) ≈ m× inference cost.
  deep reasoning ≈ (long chain) × cost.
  multi-agent(k) ≈ ~k× (plus synthesis).
```

So "maximum capability on EVERY query" is uneconomic at enterprise / factory-scale deployment. The resolution is the same layered/acceleration discipline already in canon:

- **Tier by importance (ADR-0034):** trivial queries → single pass; important/hard → M-A…M-E.
- **Metacognition gates depth (§2):** spend N, m, chain-length only where marginal value clears cost.
- **Physics verifier is often low-marginal-cost for Youtab:** M-C can reuse the FEA/sim the engineer already runs → low marginal verifier cost when validity conditions hold (a Youtab advantage most lack).
- **Cost shapes HOW, never WHETHER (T2):** cost may lower $N$ or chain length, but never blocks deep reasoning on an important task.

**Canon:** maximum capability is a *dial the leader turns up when it matters*, not a constant tax.

---

## 4. Binding to existing canon — NO new conflicts beyond NOTE 1

Reconciliation check: these mechanisms are the **technical HOW** of hooks the canon already has.

```text
ADR-0031 already provides:
  • "panels/ensembles as candidate generation + verification support"  → M-B, M-C, M-D
  • "Tests and strong evidence are objective signals the Authority USES" (EVD-4) → M-C verifier
  • "low-risk answers → best candidate may be selected"                → best-of-N selection
  → EXTEND (per NOTE 1 T3): formalize selection→synthesis + best-of-N + verifier scoring.
    Keep the floor: models don't vote; Authority arbitrates by objective signal.

ADR-0034 already provides:
  • Level 3 "MAY include deep reasoning, extensive verification, multi-agent execution"
  → REFRAME (per NOTE 1 T2): Level 3 allocates M-A…M-E when important; never blocks.

ADR-0010 reason budget:
  → RAISE (per NOTE 1 T1): 100k–200k for the whole multi-agent reasoning loop;
    keep hard ceiling + LoopBudgetExceeded.
```

**Result:** NOTE 2 introduces **no new ADR conflicts** — it deepens the SAME corrections already listed in NOTE 1 (T1, T2, T3). The affected (conflict) ADRs remain exactly {0010, 0031, 0034}; One Brain (0021), v0.1 lock (0032), and acceleration/anti-collapse (0062) are preserved. Deep capability runs in the ENGINES, arbitrated by the ONE Authority — no second brain, PLC §20 untouched. (Full reconciliation scope, including the clarification-only ADRs 0014/0027/0029, is owned by ADR-0064 §21.)

---

## 5. Security — OWASP LLM Top-10 (2025) + smoke

```text
LLM06 Excessive Agency  → more compute/samples changes candidate QUALITY only,
                          never the effect gate; N candidates enter the SAME
                          admission path (ADR-0031). Depth ≠ authority.
LLM01 Prompt Injection  → longer reasoning chains must not let an injected step
                          escalate to an effect; per-step provenance; verifier gate.
LLM09 Misinformation    → M-C/M-E use an EXTERNAL verifier (physics/PRM); never rely
                          on blind self-agreement; abstain under semantic entropy.
LLM10 Unbounded Consumption → N, m, chain-length, agent-count are budget-bound and
                          metacognition-gated; runaway is caught (LoopBudgetExceeded).
LLM04 Data/Model Poisoning → a best-of-N winner promoted to memory/weights still
                          passes the ADR-0062 verified-promotion gate.
```

**Smoke suite (for the ADR):**
```text
1. M-A: raising the reasoning budget raises accuracy on a held-out hard set (lever proven).
2. M-B: self-consistency(m) beats single greedy pass on the same set.
3. M-C: best-of-N + verifier beats best-of-1; with the physics verifier, approaches pass@N.
4. Verifier quality bounds gain: a deliberately weak verifier does NOT inflate accuracy (honesty).
5. M-D: multi-agent synthesis beats the best single agent on a separable task.
6. M-E: step-level PRM guidance beats outcome-only selection.
7. Budget: N/m/chain respect the token+latency ceiling; runaway caught (LoopBudgetExceeded).
8. Governance: N candidates do NOT bypass the effect gate; no engine becomes a second Authority.
9. Allocation: metacognition spends deep compute on the hard query and NOT on the trivial one.
```

---

## 6. Relationship to NOTE 1 and path to ADR

```text
NOTE 1  = WHY and the FRAMING: warden → leader; system-level intelligence;
          Cognitive Conductor; orthogonality; conflicts T1–T6.
NOTE 2  = HOW to release maximum model capability: M-A…M-E with math + cost + security.
Together → one Intelligence ADR (ADR-0064):
  • Conductor Authority (NOTE 1 M7/T4)
  • Deep-reasoning + latent-power mechanisms M-A…M-E (NOTE 2)
  • Metacognition allocator (NOTE 1 M4 / NOTE 2 §2)
  • Verified memory/growth binding (NOTE 1 M5/M6; ADR-0062/0028.1)
  • Corrections to {0010, 0031, 0034}; preserve {0021, 0027*, 0029, 0032, 0062}
    (*0027 gains the Conductor role)
  • OWASP + smoke; evidence tiers; consciousness out of scope.
Reconcile references against the repo canon once the branch/state of 0030–0036 is confirmed.
```

---

## 7. One-line canon (for the ADR)

```text
A single fast answer is a floor, not the ceiling, of a model's ability.
Youtab releases the model's full latent power — deep reasoning, self-consistency,
  best-of-N chosen by physics, multi-agent synthesis, step-level guidance —
  and the leader-brain spends this power WHEN IT MATTERS, verified and bounded.
It does not exceed the model; it stops wasting it.
Proof, not claim: every gain is shown on a held-out set, bounded by verifier quality, or rejected.
```
