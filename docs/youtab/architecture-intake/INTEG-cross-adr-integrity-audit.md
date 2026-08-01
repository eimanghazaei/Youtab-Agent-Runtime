# INTEG — Cross-ADR Integrity Audit, Path-Disruption Register, and Convergence-Assurance Framework

- **Status:** Proposed / Ready for Human Review. Audit artifact (item B of PLAN-close-6-gaps).
- **Date:** 2026-07-06
- **Operating Principle:** اثبات نه ادعا — Proof, Not Claim. This audit does **not** guarantee success; it makes divergence **detectable early** and names every known path-disruption.
- **Standing rules honored:** README + roadmap is the first gate (I cannot read it here — repo not mounted); PhD-level; OWASP LLM Top-10 (2025) + smoke.
- **Honest scope (critical):** I have direct text for **ADR-0010** and **ADR-0027** (uploaded) and for the ADRs/addenda authored this session (**0061-AddA, 0064, 0065, 0010-T1, 0027-T4, 0063**). I do **NOT** have the live text of **0014, 0026, 0028/0028.1, 0029, 0030, 0031, 0032, 0034, 0035, 0060, 0062**. So this audit is **complete on dependencies and known conflicts**, and **explicitly gaps** the ADRs whose text I lack (§6). A full line-level audit needs those files.

---

## 0. Your two questions, answered directly

**Q1 — "Executing these files needs all ADRs to be traversed; will there be disruption in the path?"**
**Yes — there are exactly the disruptions listed in §3, and they are already known, not surprises.** They are: the 0061 number collision, the four open tensions (T2/T3/T5/T6) that *fight the intelligence layer at runtime* if left unclosed, the dependency-order requirement (0062 before 0063/0064), fake-green CI, unmeasured verifier false-accept, and the branch mismatch. **None is unknown; each has a fix and a blocking gate.** The path is safe **iff** they are closed in order (§4). Left open, they *will* cause real disruption.

**Q2 — "How can I be sure I'll converge to the goal in real execution?"**
**You cannot get a guarantee — and anyone who gives you one is violating Proof-not-Claim.** What you get instead (§5) is *early measured signal*: the goal is operationalized into measurable proxies, instrumented as leading indicators, and protected by phase gates. **You know you are converging when the proxies trend up and no gate fires; if a proxy stalls or regresses, a gate stops you in shadow — before real-execution cost.** Convergence is *measured*, not promised. That is the strongest honest assurance that exists.

---

## 1. The ADR dependency graph (what depends on what)

```text
0032 (v0.1 lock) ─────────────┐
0029 (effect gates,R0-R5,PLC) ├──── floors used by everything (never weakened)
0027 (authority) +T4 ─────────┤
0014 (fail-closed) +T5 ───────┘
                    │
0026 (knowledge) ───┤
0035 (RAG) ─────────┤
0031 (model layer) +T3 ─────────────┐
0034 (cost/perf) +T2 ───────────────┤
0010 (cognitive loop) +T1 ──────────┤
0030 (routing econ) ────────────────┤
0060 (security/guardian) ───────────┤
0061 (workforce | PHYSICS-VERIFIER) ─┤  ← COLLISION
0028/0028.1 (growth Δθ) ─────────────┤
0062 (verified promotion,K_org) ─────┤  ← must land BEFORE 0063/0064
        │                            │
        ▼                            ▼
   0063 (chat lifecycle)        0064 (intelligence arch: RM+Conductor, M-A..M-J)
                                     │  (needs T1..T6 closed to be internally consistent)
                                     ▼
                                0065 (heavy multi-agent)  ← child of 0064;
                                     depends on 0061-verifier / 0026 / 0035 / 0014 / 0029 /
                                     0010-T1 / 0062 / 0028.1 / 0060 / 0027-T4
```

---

## 2. Invariant-preservation check (do the addenda weaken anything?)

| Floor | Guarded by | Any addendum weaken it? |
|---|---|---|
| Effect authorization / complete mediation | 0027 §7, 0029 | **No** — T1/T4/T2/T3/0065 all keep effects gated; allocation authorizes nothing |
| PLC real-time red line | 0029 §20, 0061 §5 | **No** — untouched everywhere |
| One Brain / one sovereign | 0027 C1/C2 | **No** — 0064/0065 forbid second brain / agent authority |
| Identity kernel K1–K9 | 0027 §11 | **Strengthened** (K8 growth obligation is now discharged, not braked) |
| Tenant isolation | 0010 §9, 0062 | **No** — envelope + agent trees are tenant-scoped |
| Gated growth Δθ | 0028.1, 0062 | **No** — promotion/growth stays gated |
| Fail-closed abstention | 0014 | **Clarified, not weakened** (T5: fail-closed ≠ shallow) |
**Result (on what I can see): no floor weakened; two are strengthened.** The 0014/0029 rows must be re-confirmed against their live text (§6).

---

## 3. Path-Disruption Register (every known disruption, with fix + gate)

| # | Disruption | Sev | Status | Fix | Blocking gate |
|---|---|---|---|---|---|
| D1 | ADR-0061 number collision (Workforce vs Physics-Verifier) → ambiguous citations, promotion-gate may bind wrong verifier | **H** | proposal ready, unresolved | extract verifier to own ADR (Option 1) | P0 read + P1 apply |
| D2 | ADR-0034 (T2) still frames cost as *minimize/veto* → **fights the 0010-T1 deep-reasoning envelope at runtime** (Warden Trap re-enters) | **H** | addendum pending | write T2 (cost = proportionality, not veto) | P1 |
| D3 | ADR-0031 (T3) still *selection-only* → **constrains multi-engine synthesis** the Conductor needs | **H** | addendum pending | write T3 (selection → synthesis, no voting) | P1 |
| D4 | ADR-0014 (T5) fail-closed read as *shallow* → **fights warranted depth** | **M** | clarification pending | write T5 (fail-closed ≠ shallow) | P1 |
| D5 | ADR-0029 (T6) read as *deeper reasoning ⇒ more authority* → **breaks intelligence/authority orthogonality** | **M** | clarification pending | write T6 (deeper reasoning = candidate quality, not authority) | P1 |
| D6 | Dependency order: 0062 (promotion/K_org) must be accepted **before** 0063/0064, or their memory/promotion wiring dangles | **H** | known | topological accept order (§4) | P1a |
| D7 | Fake-green CI (continue-on-error) → "passing" tests prove nothing → **the whole measurement chain is invalid** | **H** | known (prior session) | P2: real CI first | P2 before P4 |
| D8 | Verifier `false_accept_rate` unmeasured → Evidence-Strength is claim, not proof → **best-of-N may select wrong candidates ("gains" illusory)** | **H** | known | G4: register + measure against ground truth | before P4 |
| D9 | No held-out benchmark + ground truth → **nothing is measurable** → no trust claim valid | **H** | known | G3: build benchmark | before P4 |
| D10 | Branch mismatch (analysis: 0001–0029; canon 0030+ on docs/arch-canon-v1) → **commit lands on wrong branch** | **M** | known | reconcile branch in P0 | P0 |
| D11 | All ADRs Proposed, none Accepted → status ambiguity | **L** | known | P1a acceptance after INTEG | P1 |
| D12 | Claude lacks live text of 0014/0026/0028.1/0029/0030/0031/0032/0034/0035/0060/0062 → **audit incomplete** | **M** | this session | upload files → complete line-level audit | to finalize INTEG |

**Reading of the table:** D2–D5 are the answer to "will the intelligence layer be disrupted?" — **yes, unless the four tension addenda are written.** They are the runtime frictions that would drag the system back toward the Warden Trap. Closing T2/T3/T5/T6 is not paperwork; it removes real runtime conflict.

---

## 4. Safe traversal order (topological — prevents D6/D1 disruption)

```text
P0  read README+roadmap; reconcile branch (D10); confirm numbers + next free slot.
P1  1) fix D1 (0061 renumber + citations)          ← first, so later refs are correct
    2) write T2,T3,T5,T6 (close D2–D5)
    3) INTEG line-level audit on the now-complete file set (close D12)
    4) accept in dependency order (close D6/D11):
         0062 → 0063 → {0010-T1, 0027-T4, 0034-T2, 0031-T3, 0014-T5, 0029-T6} → 0064
              → 0061-verifier-extract → 0065
P2  code integrity: real CI (D7) → dedup endpoints → Phase-AUTH.
P3  benchmark+ground truth (D9) → verifier registry + measured false_accept (D8).
P4  V-1…V-17 in shadow (needs P1-accept + P2-green CI + P3-data).
P5  activate per-tenant behind flags only after V-suite green.
```

---

## 5. Convergence-Assurance Framework (how you KNOW you're approaching the goal)

**Step 1 — operationalize "the goal" into measured proxies (no vague "powerful/trusted"):**
```text
POWER      = held-out heavy-task CLOSURE RATE + quality vs single-agent baseline        (V-1)
INTELLIGENCE = verified-accuracy LIFT on hard held-out sets; smarter-not-voting          (ST-1, V-1, V-2)
TRUST      = confident-wrong on unanswerable ≈ 0; verifier false_accept LOW; no-regression (V-4, V-10, G4, V-9)
LEADERSHIP = Brain-initiated escalation IMPROVES results WITHOUT unauthorized effects     (V-7)
```

**Step 2 — instrument leading indicators (in-flight, from the tuning guide):**
```text
GREEN (converging):  ROI is the dominant reasoning stopper · cost tail thin · deep fires selectively ·
                     ceiling-hit rare · confident-wrong ≈ 0 · verifier false_accept stable-low.
RED (diverging):     ceiling hit often · cost fat tail · accuracy flat/down with compute ·
                     confident-wrong rising · false_accept rising.
```

**Step 3 — gate every phase (bounded divergence):** each phase has a Definition-of-Done + smoke that must pass before the next phase. **You cannot drift far without a gate firing** — and the gates run in **shadow**, so divergence is caught before real-execution cost.

**The honest assurance statement:**
```text
You do NOT get a guarantee of reaching the goal.
You DO get: the goal expressed as measured proxies, instrumented as leading indicators, protected by
shadow-mode gates. If the proxies trend up and no gate fires, you ARE converging — with evidence.
If a proxy stalls or regresses, a gate stops you early. That is proof-based convergence, not a promise.
```

---

## 6. What I need to finalize a FULL line-level integrity audit

Upload the live text of: **0014, 0026, 0028/0028.1, 0029, 0030, 0031, 0032, 0034, 0035, 0060, 0062.** Then I close D12 and confirm the D2–D5/D6 rows against real text (not references), plus the 0014/0029 invariant rows in §2.

---

## 7. Security (OWASP-aligned) + integrity smoke

```text
LLM04 (integrity spirit) — ambiguous verifier reference (D1) could bind a promotion gate to the wrong
   verifier; resolving D1 + measuring false_accept (D8) protects the trust chain.
LLM06/LLM10 — the T2/T3/T6 closures keep intelligence escalation ≠ authority escalation and keep the
   shared envelope bounded; closing them removes runtime conflict WITHOUT weakening gates.
```

**Integrity smoke tests (run in a mounted session):**
```text
I-1  Link-check: no dangling ADR cross-reference after D1 fix (every ref resolves to one ADR).
I-2  Dependency-order: 0062 accepted before 0063/0064; 0064 before 0065 (build a topological check).
I-3  Invariant-preservation: each addendum keeps effect gates + PLC + One Brain (diff vs floor list §2).
I-4  Tension-closed: T1..T6 each have a closing artifact; no ADR still contradicts the intelligence layer.
I-5  No-regression: all pre-existing 0027/0029/0064 smoke tests pass after the addenda land.
```

*This audit changes no ADR and commits nothing. It names every known path-disruption, orders the safe traversal, and defines proof-based convergence. It is complete on what is in hand and explicitly gaps the ADRs whose live text is not available here.*
