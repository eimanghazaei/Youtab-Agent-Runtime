# PROMPT for Claude Code — Youtab PHASE 3: Measurement Infrastructure (Benchmarks + Verifier Registry)

> Run only AFTER Phase 2 (real CI) is reviewed by Eiman — measurement on a fake-green pipeline is worthless. Paste this whole file to Claude Code inside the Youtab repo. Written to be executed **without mistakes**. When in doubt, **STOP and report**.

---

## 0. ROLE, MISSION, NON-NEGOTIABLE RULES

**You are** a measurement/benchmark engineer for the Youtab AI OS. **Mission this run:** make the system MEASURABLE — build held-out benchmark sets **with ground truth** per workforce group, and populate the **Verifier Quality Registry** (ADR-0064 §20A.2) with **measured** `false_accept_rate` per verifier. Without this, "powerful/trusted/intelligent" is a claim, not a fact — and the V-suite (Phase 4) cannot run.

**Operating principle:** اثبات نه ادعا — Proof, Not Claim. **Ground truth is sacred: it must come from humans / physics / experiment / deterministic checks — NEVER from the model.** Fabricating a label corrupts the entire evidence chain.

**HARD RULES — violating any is a failure:**
```text
R1. Regime A git discipline: NO git operations. Eiman does all git. You deliver files/patches to `outputs/`.
R2. GROUND TRUTH IS NEVER MODEL-GENERATED. You may help ASSEMBLE candidate items and build the harness, but
    every ground-truth label must be sourced from a human expert, a physics/solver/measurement result, a
    deterministic check, or an authoritative dataset — and independently spot-audited. If you cannot obtain a
    real label, mark the item UNLABELED and exclude it — do NOT invent a label.
R3. README + roadmap + ADR-0064 §20A + ADR-0061(+Addendum A) FIRST. Confirm the workforce groups, the
    verifier list, and the §20A Registry schema on real text. If reality contradicts this prompt, STOP and report.
R4. Highest technical rigor: pre-register metrics + margins BEFORE measuring; report confidence intervals;
    tenant-isolate all benchmark data.
R5. OWASP LLM Top-10 (2025) + smoke tests on every artifact.
R6. FUSE handling: if a file reads empty/garbled, re-read from disk; if still empty, STOP and report — never fabricate.
R7. STOP-and-report at each task gate.
```

**Output location & naming (all in `outputs/`):**
```text
outputs/BENCHMARK-DESIGN.md                     (per-group sets, metrics, margins, labeling protocol)
outputs/benchmark/<group>/manifest.json         (item ids, provenance, label source — NO fabricated labels)
outputs/VERIFIER-REGISTRY-PLAN.md               (schema population plan per ADR-0064 §20A.2)
outputs/verifier-registry/measurements.md       (MEASURED false_accept_rate + validity conditions per verifier)
outputs/MEASUREMENT-REPORT.md                   (final report + honesty-control evidence)
```

---

## 1. CONTEXT — the groups and verifiers to cover

Per ADR-0061 (+ Addendum A), build a held-out set (WITH ground truth) + an UNANSWERABLE set for abstention:
```text
G1  Mechanical/Industrial design   → verifier: physics/FEA, tolerance, CAD checks.
G8  Software/Web/App               → verifier: compiler/typecheck, tests, CI, static analysis.
G9  Simulation & Scientific Comp   → verifier: V&V (software verification AND physics validation, must converge).
G10 Applied-Physics & Measurement  → verifier: physics solver AND experimental measurement (independent chains).
G7  Media Visual Intelligence      → verifier: ground-truth labels for recognition; provenance for restoration.
+   Analyzer/Data, Admin, Sales, Management → domain checks where a ground truth exists.
+   UNANSWERABLE set                → items with no correct answer (for abstention calibration).
```

---

## 2. PHASE 3 — TASKS (each: design → assemble → measure → STOP)

### Task 3.1 — Benchmark design + labeling protocol (design FIRST, pre-register)
```text
DESIGN: for each group, define the task format, the accuracy metric, the pre-registered pass margin, the
  sample size for significance, and the LABELING PROTOCOL (who/what produces ground truth, how it is
  spot-audited). Write BENCHMARK-DESIGN.md.
GROUND-TRUTH SOURCING (R2): specify, per group, the real label source — human expert sign-off, physics/solver
  result, experimental measurement, deterministic check, or authoritative dataset. NO model-generated labels.
=== STOP. Report BENCHMARK-DESIGN.md to Eiman for approval of the protocol BEFORE assembling items. ===
```

### Task 3.2 — Assemble held-out sets (with real labels or exclude)
```text
ASSEMBLE: gather candidate items per group into outputs/benchmark/<group>/manifest.json with, per item:
  id · prompt/task · label_source · label · validity_scope · provenance · tenant_isolation_tag.
RULE: an item WITHOUT a real, sourced label is marked UNLABELED and EXCLUDED from scoring — never invent it.
INCLUDE the UNANSWERABLE set (items whose correct behavior is ABSTAIN).
TENANT ISOLATION: no cross-tenant data pooling; tag every item.
```

### Task 3.3 — Register verifiers + MEASURE false-accept
```text
REGISTER: for each verifier, create a Registry entry per ADR-0064 §20A.2 schema: verifier_id · type ·
  independence_from_generator · hierarchy_rank · validity_conditions · scope.
MEASURE (this is the point): run each verifier against the group's GROUND-TRUTH set and compute the real
  false_accept_rate (verifier says PASS while ground truth says FAIL) and false_reject_rate, with confidence
  intervals. Record validity conditions under which each holds.
HONESTY CONTROL: include a deliberately WEAK verifier; it MUST show a HIGH measured false_accept_rate. If it
  doesn't, the measurement harness is rigged — STOP and fix the harness before trusting any number.
=== STOP. Report registry measurements + honesty-control result to Eiman. Do NOT proceed to Phase 4. ===
```

---

## 3. SECURITY — OWASP LLM Top-10 (2025) mapping

```text
LLM04 Data/Model Poisoning — poisoned or fabricated ground truth corrupts every downstream trust decision.
   R2 (no model-generated labels) + spot-audit + honesty control are the direct defense.
LLM02 Sensitive Info — benchmark items may contain tenant data: tenant-isolate, no cross-tenant pooling,
   redact where required.
LLM03 Supply Chain — benchmark + label provenance recorded; datasets come from authoritative sources.
LLM09 Misinformation — the honesty control proves the harness cannot be gamed into inflating accuracy; a weak
   verifier must read as weak.
LLM10 Unbounded Consumption — measurement runs are budget-bound; large sweeps run async, capped.
```

---

## 4. SMOKE SUITE

```text
S3-1  Honesty control: a deliberately weak verifier shows a HIGH measured false_accept_rate (harness not rigged).
S3-2  No fabricated labels: every scored item has a non-model label_source; UNLABELED items are excluded.
S3-3  Ground-truth spot-audit: a random sample of labels is independently re-checked and matches.
S3-4  Tenant isolation: no benchmark item leaks across tenants; every item is isolation-tagged.
S3-5  Abstention set: the UNANSWERABLE set correctly elicits ABSTAIN (feeds V-4/V-10 later).
S3-6  Reproducibility: re-running a verifier on the same set yields the same false_accept_rate within CI.
S3-7  Registry completeness: every verifier used by any workforce group has a Registry entry with MEASURED
      false_accept_rate + validity conditions (no "assumed" values).
```

---

## 5. DEFINITION OF DONE + FINAL REPORT

```text
DONE when:
  ✓ BENCHMARK-DESIGN.md approved; metrics + margins + labeling protocol pre-registered.
  ✓ Each group has a held-out set with REAL, sourced ground truth + an UNANSWERABLE set; unlabeled items excluded.
  ✓ Every verifier has a Registry entry with a MEASURED false_accept_rate + validity conditions (ADR-0064 §20A.2).
  ✓ S3-1…S3-7 demonstrated with evidence (esp. S3-1 honesty control).
  ✓ The GUIDE-tuning preconditions P1–P6 are now satisfiable.
  ✓ No fabricated labels; no git op; tenant isolation intact.
FINAL REPORT: per group — set size, label sources, measured verifier false_accept + CI; per verifier —
validity conditions. Flag any group where a real ground truth could NOT be obtained (so Eiman decides whether
to defer that group). If any file was empty/corrupt (R6), name it and STOP.
```

---

## 6. EXPLICIT "DO NOT" LIST
```text
✗ Do NOT git add/commit/push (Regime A — Eiman does git).
✗ Do NOT generate ground-truth labels with the model — labels are human/physics/experiment/deterministic only.
✗ Do NOT score an item that lacks a real label — mark UNLABELED and exclude.
✗ Do NOT report an "assumed" false_accept_rate — only MEASURED values against ground truth.
✗ Do NOT pool benchmark data across tenants.
✗ Do NOT skip the honesty control (S3-1) — without it the numbers are untrustworthy.
✗ Do NOT proceed to Phase 4 (V-suite) without explicit human go.
✗ Do NOT fabricate content if a file is empty/corrupt — re-read from disk, else STOP and report.
```

*End of Phase 3 prompt. Execute Task 3.1 (design), then STOP for protocol approval before assembling items.*
