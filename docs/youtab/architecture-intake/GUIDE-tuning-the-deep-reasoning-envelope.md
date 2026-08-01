# Guide — Tuning the Deep-Reasoning Envelope (ADR-0010 Addendum T1 / ADR-0064)

**Audience:** operator / architecture reviewer tuning the reasoning-budget constants on Youtab.
**What this governs:** `MAX_REASON_TOKENS_PER_STEP_DEEP`, `REASON_TOKENS_SESSION_ROUTINE / STANDARD_DEEP / HIGH_VALUE`, `REASON_TOKENS_SESSION_HARD_CEILING`, `MIN_MARGINAL_VALUE_RATIO`, and the metacognition allocator thresholds.
**Operating principle:** اثبات نه ادعا — Proof, Not Claim. You tune against **held-out ground truth**, never against a claim or a proxy.
**Standing rules:** check README + roadmap before touching anything; keep OWASP LLM Top-10 (2025) + smoke discipline; PhD-level rigor throughout.

> **The one sentence that governs everything below:**
> You are not choosing a token number. You are finding the point on the *accuracy-vs-compute curve* where the **next token stops paying for itself**, and setting the guards around it. ROI is the driver; the tier ceilings are guardrails; the hard ceiling is a runaway backstop.

---

## 0. Preconditions — do these BEFORE you tune (or your results are noise)

```text
P1. README + roadmap check: confirm the current phase, that the deep path exists, whether it is
    flag-enabled, and the LIVE values in services/gateway/app/cognitive/loop/limits.py. Tune the
    real file, not the documented defaults.
P2. Verifier trust FIRST (the make-or-break precondition). Pull the Verifier Quality Registry
    (ADR-0064 §20A.2). If false_accept_rate is high, STOP — your accuracy signal is corrupted and
    every tuning number will be a lie. Fix the verifier before tuning the budget.
P3. Shared-pool invariant holds. Run smoke ST-C: best-of-N × self-consistency × multi-agent must
    draw from ONE pool, not N×/m×/k× separate pools. If not shared, cost numbers are meaningless.
P4. Held-out benchmark sets built, WITH GROUND TRUTH, one per tier:
      routine set · standard-deep set (hard, high-uncertainty) · high-value set (critical, physics-
      verifiable) · an UNANSWERABLE set (for abstention calibration).
P5. Telemetry on: Reasoning ROI / Marginal-Value Telemetry (§20A.5) and the Metacognition
    Calibration Suite (§20A.4) must be emitting. No telemetry → no tuning.
P6. Safety rails armed: dormant-by-flag ON, shadow mode available, per-tenant accounting verified,
    auto-revert to cheap defaults on a cost-SLA breach.
```

**If any of P1–P6 fails, you are not ready to tune. Fix it first.**

---

## 1. What you are tuning, in plain terms

| Constant | What it controls | The question it answers |
|---|---|---|
| `MAX_REASON_TOKENS_PER_STEP_DEEP` | max generated tokens in **one** `reason` call on the deep path | "how big can a single deep step be?" |
| `REASON_TOKENS_SESSION_ROUTINE` | shared pool for a routine task | "how much for an everyday task?" |
| `REASON_TOKENS_SESSION_STANDARD_DEEP` | shared pool for a typical hard task (the old "100k–200k") | "how much for a normal hard task?" |
| `REASON_TOKENS_SESSION_HIGH_VALUE` | shared pool for a critical / high-ROI task | "how much for a task worth real money/safety?" |
| `REASON_TOKENS_SESSION_HARD_CEILING` | absolute runaway backstop | "when do we force-stop no matter what?" |
| `MIN_MARGINAL_VALUE_RATIO` | the ROI stop threshold | "when has the next token stopped paying for itself?" |
| allocator thresholds | tier selection + epistemic/aleatoric routing + abstention | "which tasks get depth at all?" |

**Reading order of the guards (per session):** ROI stop (primary) → tier ceiling (guard) → hard ceiling (backstop). In a well-tuned system, **ROI ends most sessions**; the ceilings are rarely reached.

---

## 2. The core method — the accuracy-vs-compute curve

For each tier's benchmark, plot accuracy `A` against cumulative reasoning tokens `C`. On real workloads the shape is almost always **concave-increasing to a knee, then flat, sometimes drooping**:

```text
accuracy A
  │           ___________  ← plateau (spending here is WASTE)
  │        __/
  │      _/  ← the KNEE  (set the tier ceiling near here)
  │    _/
  │  _/  ← steep region (each token buys a lot — never strangle here)
  │_/
  └────────────────────────► cumulative reasoning tokens C
        C_knee
```

**The knee `C*`** is where the marginal accuracy per token stops justifying its cost. Formally (value-of-computation):

```text
keep spending while   (dA/dC) · V  >  unit_cost
stop at C* where      (dA/dC) · V  =  unit_cost
   dA/dC   ≈  ΔA/ΔC  (finite difference between two compute levels — read from telemetry)
   V       =  value of a correct answer for this tier (money / safety / rework avoided)
```

- **Set each tier ceiling ≈ `C*` + a small margin.** Not at the plateau top (waste), not below the knee (strangling).
- **Set `MIN_MARGINAL_VALUE_RATIO` so the ROI stop fires at the knee.** `ratio = 1.0` means "stop when the next increment's expected value equals its cost."

**Critical detail:** the curve is **per tier, per workload, per engine.** An engineering task with a physics verifier has a different (often much later) knee than an open-ended language task. **Never set one ceiling for all.** And when you swap or grow the engine (ADR-0031 / ADR-0028.1 M-I), the curve moves — **re-sweep** (see §8).

---

## 3. Step-by-step procedure

```text
STEP 1 — Baseline (cheap path).
  Measure cheap-path accuracy + cost on every benchmark set. This is your floor and your
  control group. Record it; every deep gain is measured AGAINST this.

STEP 2 — Sweep compute (offline, shadow).
  For each tier's set, run the deep path at increasing session budgets, e.g.:
      8k → 32k → 100k → 200k → 500k → 1M → 2M → 4M
  with best-of-N / self-consistency / multi-agent configured as in production.
  Record at each level: accuracy A(C), marginal_gain (ΔA/ΔC), verifier_delta, latency, cost.

STEP 3 — Find the knee per tier.
  Locate C* where (ΔA/ΔC)·V ≈ unit_cost. Set:
      tier ceiling  ≈ C* + margin
      MIN_MARGINAL_VALUE_RATIO so the ROI stop fires at C*.
  If accuracy is STILL rising steeply at your top swept level → the knee is beyond it → sweep higher
  before setting the ceiling (do NOT set the ceiling below an unfound knee).

STEP 4 — Calibrate the allocator (which tasks get depth).
  Tune tier-selection thresholds (importance / uncertainty / error-cost features) so the deep path
  fires on tasks that SHOW lift on the curve and NOT on flat ones. Tune epistemic-vs-aleatoric
  routing so aleatoric tasks abstain instead of burning compute. Validate on the Metacognition
  Calibration Suite (§20A.4).

STEP 5 — Set the runaway backstop.
  REASON_TOKENS_SESSION_HARD_CEILING = well above the high-value knee (e.g. 2–4×), so it never
  interferes with legitimate high-value work but still catches pathology → ReasoningEnvelopeExceeded.

STEP 6 — Shadow-validate on real traffic (no prod spend).
  Run the tuned config in shadow. Compare live telemetry to the green/red signals in §4 and §5.

STEP 7 — Canary rollout, per tenant.
  Enable for a small canary tenant behind the flag. Watch cost, accuracy lift, calibration, and
  governance no-regression. Expand gradually. Auto-revert on cost-SLA breach.

STEP 8 — Steady-state monitoring + re-tune triggers.
  Dashboard the signals in §4/§5. Re-tune when a trigger in §8 fires.
```

---

## 4. What you SHOULD see (green signals — this is "tuned")

```text
G1. ROI is the dominant stopper.  stop_reason distribution: the large majority is
    roi_below_threshold — NOT tier_ceiling, NOT hard_ceiling. (You are stopping at the knee.)
G2. Deep fires selectively.  Deep-path activation is a MINORITY of tasks, concentrated on
    high user_value_class / high-uncertainty tasks. Cheap path carries the volume.
G3. Real accuracy lift.  Deep-path accuracy on held-out hard sets is significantly above the
    cheap-path baseline (pre-registered margin, with confidence intervals).
G4. Diminishing returns visible.  Within a session, marginal_gain decreases across increments —
    and the session stops shortly after it crosses the ROI threshold.
G5. Healthy metacognition.  confident-wrong on the UNANSWERABLE set ≈ 0; epistemic-vs-aleatoric
    routing accuracy high; selective accuracy at fixed coverage beats the no-metacognition baseline.
G6. Cost has a THIN tail.  Most sessions cheap; a few expensive; and every expensive session
    correlates with high value_class AND shows an accuracy payoff (verifier_delta > 0).
G7. Verifier honest.  Verifier Quality Registry false_accept_rate stable/low; accuracy gains hold
    up against GROUND TRUTH, not just the in-loop verifier.
G8. Per-tenant fairness.  No single tenant monopolizes the pool; per-tenant accounting holds.
G9. Governance intact.  All existing governance smoke tests still pass (no-regression).
```

---

## 5. What you should NOT see (red flags — this is "NOT tuned")

Each red flag → its likely cause → the fix.

```text
R1. Ceiling hit often (stop_reason = tier_ceiling / hard_ceiling common).
    → Either the ceiling is BELOW the knee (accuracy still rising at the ceiling: marginal_gain>0
      there) → RAISE the tier; OR the ROI stop is not firing → fix MIN_MARGINAL_VALUE_RATIO / the
      ROI estimator. Diagnose by checking marginal_gain AT the ceiling.

R2. Deep fires on trivial tasks (deep activation on low value_class / low uncertainty).
    → Allocator tier-selection too loose → tighten thresholds. This is pure over-spend.

R3. Deep does NOT fire on hard tasks that would benefit.
    → Allocator too conservative → loosen. This is the Warden Trap (under-thinking).

R4. Accuracy FLAT or DOWN as compute rises (on some tasks).
    → Over-thinking / unguided verbosity, OR aleatoric uncertainty being fed deep reasoning (deep
      can't help irreducible ambiguity), OR a weak verifier selecting wrong candidates.
    → Lower that tier's ceiling to before the droop; fix uncertainty routing; check verifier quality.

R5. marginal_gain noisy/negative but the session keeps spending.
    → ROI stop broken or MIN_MARGINAL_VALUE_RATIO too low → raise the ratio / fix the estimator.

R6. Cost FAT tail / many sessions near the hard ceiling.
    → Allocator over-promoting tiers, OR the shared-pool invariant is broken (N×/m×/k× leaking).
    → Re-verify ST-C (shared pool); tighten tier promotion.

R7. confident-wrong on the unanswerable set RISING.
    → Deep reasoning is manufacturing confident misinformation (OWASP LLM09). Fail-closed harder,
      raise abstention, route aleatoric to human. Do NOT ship until this is ≈ 0.

R8. Verifier false-accept rate RISING (from the Registry).
    → Best-of-N is selecting wrong candidates; your "gains" are illusory. STOP tuning the budget;
      fix the verifier first. (This is the most dangerous confound — see §6.1.)

R9. One tenant draining the pool.
    → Per-tenant accounting broken → fix isolation (ADR-0010 §Decision 9 / ADR-0062 §4).

R10. Results swing run-to-run.
    → Too few samples / unstable base → get statistical power; fix instability before trusting numbers.
```

---

## 6. Important techniques & precise details (the ones people get wrong)

### 6.1 Trust the verifier before you trust any number (the #1 pitfall)
You must tune against **held-out GROUND TRUTH**, never against the in-loop verifier. If you tune to the verifier score, you optimize for *verifier-pleasing but wrong* answers (Goodhart's law). A best-of-N with a lying verifier will happily "improve accuracy" that isn't real. Validate the in-loop verifier's `false_accept_rate` (§20A.2) *first*, and confirm the final accuracy against ground truth.

### 6.2 The knee, not the plateau
Set ceilings where marginal value dies (the knee), not where accuracy stops moving entirely (the plateau). The tokens between knee and plateau buy almost nothing and cost a lot.

### 6.3 ROI is primary; ceilings are guards
A correctly tuned system stops **most** sessions via ROI, well below the tier ceiling. If ceilings are your main stopper, you haven't tuned the ROI stop — fix that before touching the ceilings.

### 6.4 Tune per tier, per workload, per engine — never to the global average
Physics-verifiable engineering tasks, open-ended language tasks, and interactive chat have *different* curves. One global ceiling is wrong for all of them. Segment.

### 6.5 Cost is tokens AND latency AND money
For interactive chat, a 2M-token session may be unacceptable even if accurate. Tier by **interaction mode**: interactive (tight latency ceiling) vs batch/async (can go deep). A high-value design review can take minutes; a live chat reply cannot.

### 6.6 Aleatoric tasks get NO deep budget
Deep reasoning cannot resolve irreducible ambiguity. Spending there is pure waste and, worse, breeds confident-wrong answers. Route aleatoric uncertainty to abstain / clarify / human. Verify routing accuracy in the Calibration Suite.

### 6.7 Engine swap / growth invalidates the curve
Swapping the embedded model (ADR-0031) or growing it (ADR-0028.1 / M-I) moves the accuracy-vs-compute curve — often a stronger engine needs LESS compute for the same accuracy. **Re-sweep after any engine change.** A stale curve gives stale ceilings.

### 6.8 The physics-verifier advantage is real but conditional
Where a valid physics/solver verifier exists, deep best-of-N pays off far later (the knee is deeper) because selection is cheap and accurate — so the high-value tier is genuinely worth raising there. But only when the verifier's validity conditions hold (§20A.2). Do not generalize the engineering curve to non-verifiable domains.

### 6.9 Statistical rigor
Pre-register thresholds and margins before the sweep. Use enough samples for significance; report confidence intervals; treat single-session `marginal_gain` as noisy — smooth over a window.

### 6.10 Never tune on an unstable base
Fix verifier bugs, allocator bugs, and shared-pool leaks first. Tuning on top of a bug bakes the bug into your constants.

---

## 7. When it IS tuned vs when it is NOT — the explicit checklist

**IT IS TUNED (exit criteria — all must hold):**
```text
✓ ROI is the dominant stop_reason (≈ ≥80% of deep sessions).
✓ Ceiling-hit rate is low (a few %), and every ceiling-hit is a genuine high-value task still
  paying off at the ceiling (marginal_gain > 0 there).
✓ Deep-path accuracy lift is significant and STABLE across re-runs on held-out ground truth.
✓ confident-wrong on the unanswerable set ≈ 0; routing + calibration metrics within target.
✓ Cost-per-unit-accuracy-gain has PLATEAUED (raising budgets further buys no accuracy).
✓ Cost tail is thin; per-tenant fairness holds.
✓ All governance smoke tests pass (no regression).
```

**IT IS NOT TUNED (keep going if ANY holds):**
```text
✗ Ceiling frequently hit / cost fat tail.
✗ Accuracy still rising steeply at the ceiling (knee not found → sweep higher).
✗ Deep firing on trivial tasks, or NOT firing on hard ones.
✗ Accuracy flat/down with more compute (over-thinking / bad verifier / aleatoric misroute).
✗ confident-wrong rising, or calibration metrics off.
✗ High run-to-run variance.
✗ Verifier false-accept rate rising.
```

---

## 8. Re-tune triggers (steady state)

Re-run the relevant part of §3 when any of these fire — each moves the curve or the economics:
```text
• Engine swap or Cognitive Growth (Δθ) — curve moves; re-sweep (§6.7).
• Workload distribution shift (new task types, new tenants, seasonal mix).
• Verifier change or degradation (Registry false_accept_rate drift).
• Cost or latency SLA change.
• A red flag from §5 appearing in steady-state dashboards.
```

---

## 9. Security — OWASP LLM Top-10 (2025) for the TUNING PROCESS itself

```text
LLM10 Unbounded Consumption — the tuning process must NEVER remove the runaway backstop. Shadow and
   canary runs are cost-capped; a mis-tune must FAIL SAFE (auto-revert to cheap defaults). Keep the
   dormant-by-flag kill switch. A raised high-value tier is still bounded + backstopped, never open.
LLM09 Misinformation — a "tuning win" that raises accuracy against the in-loop verifier but not
   against ground truth is a Goodhart artifact; reject it. confident-wrong on unanswerable must stay ≈0.
LLM06 Excessive Agency — tuning changes candidate QUALITY budgets only; it never touches effect gates
   (ADR-0029), the R0–R5 ladder, or the PLC §20 exclusion.
LLM02 Sensitive Info — sweep/benchmark data and telemetry inherit tenant isolation + redaction; do
   not pool cross-tenant reasoning traces into a shared tuning corpus without governance.
```

**Smoke tests for the tuning artifacts (run before promoting any tuned config):**
```text
TS-1  The tuned constants pass ST-A…ST-H of the ADR-0010 Addendum T1 (cheap unchanged, deep fires
      only when warranted, shared pool not multiplied, per-step cap holds, dormant-by-flag, tenancy,
      ROI telemetry emitted, no regression).
TS-2  A deliberately weak verifier does NOT inflate the tuned accuracy (honesty control).
TS-3  A cost-SLA breach in canary triggers AUTO-REVERT to cheap defaults (fail-safe).
TS-4  An aleatoric/unanswerable task abstains under the tuned allocator (does not burn budget).
TS-5  Governance no-regression: all pre-existing governance smoke tests pass with the tuned config.
```

---

## 10. Governance — when tuning is free vs when it needs an ADR addendum

```text
• Tuning WITHIN the addendum-authorized bounds (via env vars) — FREE, no ADR needed.
• Raising a tier by >2× beyond the ADR-0010 Addendum T1 values — needs a NEW addendum
  (ADR-0010 Compliance: ">2× default requires an ADR addendum").
• Adding a NEW budget scope or a 10th loop step — needs a superseding ADR (not just an addendum).
• Every tuned config is a reviewable artifact: record the sweep, the chosen constants, the held-out
  evidence, and the smoke/OWASP results. Proof, not claim.
```

---

## 11. Quick-reference — the whole method on one screen

```text
1. Check README/roadmap + live limits.py.          (don't tune blind)
2. Trust the verifier (Registry false_accept).     (or the signal is a lie)
3. Baseline cheap path.                             (your control)
4. Sweep compute per tier; build A(C) curve.        (offline/shadow)
5. Find the knee C*; set tier ceiling ≈ C*+margin.  (knee, not plateau)
6. Set MIN_MARGINAL_VALUE_RATIO so ROI stops at C*. (ROI is primary)
7. Calibrate the allocator (which tasks get depth). (epistemic→deep, aleatoric→abstain)
8. Set HARD_CEILING far above high-value knee.      (runaway backstop only)
9. Shadow → canary (per tenant) → expand.           (fail-safe, auto-revert)
10. Watch: ROI dominant stop? thin cost tail?       (green = tuned)
    ceiling-hit rare? confident-wrong≈0? no regression?
11. Re-tune on engine swap / workload shift / verifier drift.
```

*This guide is an operational runbook, not an ADR. It tunes the constants authorized by ADR-0010 Addendum T1 under ADR-0064; it grants no runtime authority, changes no effect gate, and never removes the runaway backstop. Tune against held-out ground truth. Proof, not claim.*
