# Document-Set Control Audit — Completeness, Clarity & Commit Plan

**Scope:** the six artifacts of the Intelligence-Architecture work stream.
**Question asked:** Are they complete? Do they cause reader confusion? What must be added? Which must be committed?
**Doctrine:** اثبات نه ادعا — Proof, Not Claim. Every verdict below is backed by a check.
**Governance note:** the repo (`docs/arch-canon-v1` on `G:\`) is not mounted in this session, so number/roadmap reconfirmation and the actual `git` commit are out of scope here — they are stated as preconditions.

---

## 0. Bottom line

**Physically all six files are clean** (no corruption, no NUL bytes). **The two ADRs are complete and canon-ready in substance.** There is **one real coherence hole** (the cross-link between the two ADRs is one-directional) and **four documents that need a one-line "superseded/folded/consumed" label** so a future reader is not misled. **Only two files are commit candidates (the two ADRs); the other four are the paper trail and must NOT enter `docs/adr/`.** Commit order is fixed by dependencies: **0062 → 0063 → 0064.**

---

## 1. Per-file verdict

| File | Integrity | Complete? | Reader clarity | Commit as canon? |
|---|---|---|---|---|
| **ADR-0064** Intelligence Architecture (949 lines) | clean, 0 NUL, 0 real TODO, no workforce number | ✅ full (30 sections, M-A…M-J + math, OWASP, 17+4 smoke, 21 failure modes, lineage, evidence tiers) | high — resolves voting/orthogonality/scope ambiguities explicitly | **YES** — after review + preconditions |
| **ADR-0063** Chat Lifecycle (349 lines) | clean, 0 NUL, 0 TODO, no workforce number | ✅ full (stages 1–3, pipeline, OWASP, 12 smoke, laws, canon) | high — but **missing the forward-link to 0064** (see F1) | **YES** — after review + preconditions |
| **Reconciliation report** (437 lines) | clean; self-labeled "RECONCILIATION ONLY — not an ADR" | ✅ as a report | **stale if read alone** — unaware of the 0064 number and the 0063 collision; uses M1–M7 naming | **NO** — analysis history |
| **NOTE 1** From Warden to Leader (246 lines) | clean; self-labeled "NOT yet an ADR" | ✅ as a note | source note; M1–M7 naming | **NO** — source, folded into 0064 |
| **NOTE 2** Latent-Power (inline; ~155 lines) | clean; self-labeled "to be folded into the Intelligence ADR" | ✅ as a note | **two confusions if read alone** — the concrete "10,000-user" number, and "{0010,0031,0034}" reads narrower than the ADR's full map | **NO** — source, folded into 0064 |
| **PROMPT rewrite** (447 lines) | clean; targets 0064 (22×) | ✅ as a build spec | build artifact; job done | **NO** — consumed |

---

## 2. Findings, ranked (what must be added)

### F1 — The ADR cross-link is one-directional (the one real coherence hole)
ADR-0064 references ADR-0063 ten times and builds the flywheel in its §3. **ADR-0063 references ADR-0064 zero times** (verified: `grep -c 0064` on the 0063 file = 0). Reason: ADR-0063 was authored 2026-07-05, before ADR-0064 existed. A reader of ADR-0063 alone is therefore left believing the memory story is complete in itself, with no pointer to the reasoning/conductor layer that consumes it.

**Add to ADR-0063 (drop-in text, before it is committed):**
```text
Header line (add to the metadata block):
- **Feeds Into:** ADR-0064 (Intelligence Architecture — Cognitive Conductor). This ADR
  builds the per-account memory + compaction substrate that ADR-0064's Cognitive Conductor
  reasons over. Together they form the ADR-0063 ↔ ADR-0064 cognitive flywheel:
  conversations → verified memory → deep reasoning → gated growth.

§9 (Alignment) — add one line:
  ADR-0064 (Intelligence Architecture): CONSUMES this memory substrate — the Conductor
  thinks deeply over the memory this ADR feeds, and grows from verified results.
  Bidirectional pair; see ADR-0064 §3 for the full flywheel.
```

### F2 — Four working docs need a "superseded / folded / consumed" label
Each is correctly self-labeled as *not an ADR*, but none points forward to ADR-0064. Read standalone, each gives a stale picture (unnumbered ADR, narrower scope, older M1–M7 naming, the concrete workforce number). Add a single banner line at the top of each **before archiving**:

```text
Reconciliation report:
> STATUS (2026-07-06): SUPERSEDED by ADR-0064. Its "one new ADR" recommendation was executed
  as ADR-0064 (numbered 0064 to avoid the collision with ADR-0063 Chat Lifecycle). M1–M7 map to
  ADR-0064 M-A…M-J (lineage table there). Retained as analysis history; not canon.

NOTE 1:
> FOLDED INTO ADR-0064 (2026-07-06). Mechanisms M1–M7 → ADR-0064 M-A…M-J; tensions T1–T6 →
  ADR-0064 §21. Source note; not canon.

NOTE 2:
> FOLDED INTO ADR-0064 (2026-07-06). Mechanisms M-A…M-E preserved as ADR-0064 M-A…M-E. The
  concrete "10,000-user" figures are GENERICIZED to enterprise-scale in the ADR. "{0010,0031,0034}"
  here is the CONFLICT subset only — the full reconciliation scope is ADR-0064 §21. Source note; not canon.

PROMPT rewrite:
> CONSUMED (2026-07-06). ADR-0064 was produced from this spec. Build artifact; not canon.
```

### F3 — NOTE 2 retains the concrete "10,000-user" number
ADR-0064 correctly genericized every workforce figure (verified: 0 matches in both ADRs). NOTE 2 still carries "10,000-user scale" / "uneconomic for 10,000 users" (§3). If NOTE 2 is retained or shared, genericize it there too (enterprise-scale / organization-scale), for the same canon-hygiene and sensitive-data reason the ADR applies. Covered by the F2 banner if the note is only archived internally.

### F4 — Micro-clarity in ADR-0064 (optional)
The combined header `## 9–18. The Ten Mechanisms (M-A … M-J)` followed by `### 9. M-A …` could momentarily read as "is §9–18 one section or ten?" It is unambiguous on a careful read (each mechanism is its own numbered subsection with the lineage table above them), but if you want zero friction, either promote each mechanism to a top-level `##` section or add one line under the combined header: *"Sections 9 through 18 below each define one mechanism."* Cosmetic; not blocking.

---

## 3. Completeness confirmation (the two commit candidates)

Both ADRs satisfy the project's standing rules and contain no gaps:

```text
ADR-0063: OWASP table ✅ | 12-test smoke suite ✅ | PLC exclusion ✅ | One Brain ✅ |
          0 TODO ✅ | 0 concrete workforce number ✅ | 0062 dependency declared ✅
ADR-0064: OWASP LLM Top-10 ✅ | 17 smoke + 4 flywheel tests ✅ | PLC ✅ | One Brain ✅ |
          M-A…M-J + math ✅ | evidence tiers ✅ | v0.1-lock no-breach check ✅ |
          0 real TODO ✅ | 0 concrete workforce number ✅ | 0062/0063 wired ✅
```

No core concept from NOTE 1, NOTE 2, the reconciliation report, or the ADR-0063 alignment is missing from ADR-0064 (confirmed via the lineage table and the earlier fidelity audit's eight fixes, all applied).

---

## 4. Commit plan — which, in what order, after what

### Commit as canon (to `docs/adr/`)
```text
✅ ADR-0062  (dependency of both; "authored but not committed") — MUST GO FIRST.
✅ ADR-0063  (Chat Lifecycle) — after the F1 forward-link is added.
✅ ADR-0064  (Intelligence Architecture) — last, because its §3 flywheel references 0063.
```

### Do NOT commit as canon (archive as paper trail, e.g. `analysis/` or `notes/`)
```text
✗ Reconciliation report   → archive, banner "SUPERSEDED by ADR-0064".
✗ NOTE 1                   → archive, banner "FOLDED INTO ADR-0064".
✗ NOTE 2                   → archive, banner "FOLDED INTO ADR-0064" (+ genericize the number).
✗ PROMPT rewrite           → archive or delete, banner "CONSUMED".
```

### Commit order (fixed by dependencies)
```text
ADR-0062  →  ADR-0063  →  ADR-0064
(memory topology)  (memory consumer + chat)  (reasoning layer + flywheel over both)
```

### Blocking preconditions before ANY commit (all currently open)
```text
P1. Reconfirm numbers 0062 / 0063 / 0064 against the live README + roadmap on
    docs/arch-canon-v1. (The 0063 collision proves the roadmap must be the source of truth.)
P2. Commit ADR-0062 first. Both 0063 and 0064 declare a hard dependency on it; committing
    either before 0062 creates a dangling dependency.
P3. Human review sign-off. Both ADRs are "Proposed / Ready for Human Review", NOT "Accepted".
    Do not flip status to Accepted without the reviewer.
P4. Apply the F1 forward-link to ADR-0063 so the ADR pair is bidirectionally coherent.
P5. Apply the F2 banners to the four working docs before archiving.
P6. Confirm the v0.1 lock (ADR-0032) no-breach check in ADR-0064 §20.7 against the actual
    0032 text once the branch is in hand.
```

---

## 5. One-paragraph verdict

The set is coherent and, in substance, complete. The two ADRs are canon-ready and clean; the four notes/reports/prompt are an honest paper trail that must stay out of `docs/adr/`. The only genuine defect is that ADR-0063 does not yet point forward to ADR-0064 — a one-line addendum fixes it. Add the F1 forward-link, put the F2 banners on the four working docs, then commit in the order 0062 → 0063 → 0064, each only after human review and roadmap/number reconfirmation. Nothing here weakens Youtab; the two ADRs together are a strict level-up, and this control pass exists only to make sure the reader of any single file lands on the same clear goal.
