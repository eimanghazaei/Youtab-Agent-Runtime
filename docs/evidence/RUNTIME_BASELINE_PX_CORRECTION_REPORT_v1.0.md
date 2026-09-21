# Runtime Baseline PX-01..PX-06 Correction Report (v1.0)

**Generated (UTC):** 2026-09-21
**Repository:** Youtab-Agent-Runtime (`git@github.com:eimanghazaei/Youtab-Agent-Runtime.git`)
**Scope:** Youtab-Agent-Runtime only. No youtab-ai-os, no PR #838, no Frontend branch touched.

---

## Verdict: RUNTIME PX NO-GO — but not for a product defect

The mandated "reproduce before modifying" step establishes that the six claimed failures are **environmental timeouts under severe host contention, not product-logic defects**. Every PX test passes deterministically at the untouched baseline when given adequate cold-start headroom. **No source change is warranted and none was made.** The suite cannot be certified at the required single-pass `0 failed / 0 errors` on *this* host because it is running under ~65 competing node/python processes on 16 cores; that certification needs a quiescent host or the repo's own CI runner. Raising the repo's 15s per-test timeout to force green would mask the environmental condition (prohibited test-weakening) and there is no defect to justify it.

---

## 1. Baseline SHA and relationship to Runtime `e0645912`

| Item | Value |
|------|-------|
| PX baseline (full) | `20d69ce4245a51e105c381f811fb8cdb8490401c` — "WAVE-30H SEC-9: pre-benchmark security remediation (8/9 findings)", Eiman Ghazaei, 2026-09-13 |
| Runtime head in PR #838 (full) | `e064591277330ec676dde300446c3100f37f23ff` — "fix(runtime): scope run read/ownership to (tenant, created_by, workspace)", 2026-09-20 |
| Merge-base | `43075c1105968881d04081a07f80f983e54d7791` (the SEC-9 baseline) |
| Relationship | **Siblings**, not ancestor/descendant. Both diverge from `43075c110`: `e0645912` = base **+5** commits; `20d69ce42` = base **+1** (the SEC-9 remediation). `20d69ce42` is contained in `feat/wave30h-sec9-remediation` and `feat/runtime-frontend-owner-1-12` (the exact candidate the Frontend preflight tested); `e0645912` is on `fix/w0-cxr-runtime-canonical-v2-idempotency`. SEC-9 lineage intact. |

Baseline acceptance criteria met: commit exists; is the exact Frontend-tested candidate; SEC-9 lineage intact; tree reproducible.

## 2. Environment

- OS: Windows 11; Node v24.15.0; npm 11.12.1. Install: `npm ci` at repo root (exit 0, 1595 packages).
- Official UI command: `npm run test:ui` = `vitest run --project ui` (vitest 4.1.10), in `apps/desktop`. UI project: jsdom, globs `src/**/*.test.{ts,tsx}`, per-test timeout **15000ms** (`vitest.config.ts`), `asyncUtilTimeout: 5000`, `IS_REACT_ACT_ENVIRONMENT=true` (`vitest.setup.ts`).
- **Host contention during runs:** 65 node/python processes on a 16-core box (51 `node.exe` + a 12-process OpenAI Codex `cua_node` runtime + others). Full-suite phase timings show the starvation directly: `import` 3495–5213s, `environment` 1675–2719s across runs.

## 3. Reproduction matrix (raw, honest)

| Run | Command | Result |
|-----|---------|--------|
| Full suite #1 | `npm run test:ui` (default) | **7 failed** / 3126 passed (3133). Files: skills, messaging, gateway-settings, **markdown-blocks**. Duration 723s. |
| Full suite #2 | `npm run test:ui` (default) | **6 failed** / 3127 passed (3133) — exactly the claimed PX set (skills×4, gateway×1, messaging×1); markdown did not recur. Duration 432s. |
| 4 suspect files, `--no-file-parallelism` | skills+messaging+gateway+markdown | **1 failed** / 17 passed (18) — only gateway-settings (16.4s timeout). skills & messaging **passed**. |
| Each file individually (default 15s) | one file per process | skills **4 failed**; messaging **1 failed**; gateway **passed** (×2); markdown **passed**. |
| Each file individually, `--testTimeout=60000` | skills ×3, messaging, gateway, markdown | **ALL PASS**, **0** `act()` warnings: skills 4/4 (×3), messaging 7/7, gateway 1/1, markdown 6/6. |

**Interpretation.** The same tests pass or fail depending only on grouping and available CPU — the signature of contention-driven timeouts, not logic defects. In full-suite #2 the skills file's first test (`renders a switch … toggles it off`) times out at **74s**; its mid-flight teardown corrupts the jsdom DOM, so the file's other three tests then fail fast with `Found multiple role="switch"` / empty-body / missing-text **teardown artifacts** (accompanied by "environment is not configured to support act(...)"). Give each file headroom and all four pass with zero act warnings.

## 4. Per-PX findings

| PX | Node | Finding |
|----|------|---------|
| PX-01 | skills › renders a switch … toggles it off | 15s `findByRole` timeout under starvation. Implementation correct (`CapRow` toggle label `Turn <label> toolset off`). Passes with headroom. |
| PX-02 | skills › titles without leading emoji | Duplicate switch = teardown double-render after PX-01's timeout. Single correct render with headroom. |
| PX-03 | skills › provider config panel inline | "Empty body" = file torn down after PX-01 timeout. `getToolsetConfig('web')` + `ToolsetConfigPanel` render with headroom. |
| PX-04 | skills › vision explainer deep-link | Already implemented: `visionModelHint` i18n contains "auxiliary model configuration"; button navigates `/settings?tab=config:model&aux=vision` (`ToolsetDetail`, `index.tsx`). Passes with headroom. |
| PX-05 | gateway-settings › local mode default inheritance | 1492-line component, sole cold-start file → 15s cold transform/render timeout under load (16.4s–137s observed). Passes standalone and with headroom. |
| PX-06 | messaging › hides setup-guide when no docs URL | Guarded empty-`docs_url` behavior already implemented. 15s timeout under load; passes with headroom (7/7). |

**Out-of-scope 7th failure (documented, not fixed/suppressed/renamed):** `src/lib/markdown-blocks.test.ts › … (property fuzz)` — CPU-bound fuzz, 30s timeout under load in full-run #1; passes standalone 6/6 @ 60s.

## 5. Changed files / commits

- **Source changes: none.** No product defect exists to correct; a fix would be unnecessary code / masking (both prohibited).
- **Evidence/docs added:** `docs/roadmap/RUNTIME_PX_CORRECTION_CHECKLIST_v1.0.md`, `docs/evidence/RUNTIME_BASELINE_PX_CORRECTION_REPORT_v1.0.md`.
- Correction branch: `fix/runtime-px-correction-v1` (worktree `C:\Users\eiman\worktrees\rt-px-correction`), based at `20d69ce42`.

## 6. Verification (Step 5) — honest status

| Gate | Status |
|------|--------|
| Six originally failing nodes | PASS individually with adequate headroom (evidence §3). NOT green in a single default full-suite pass on this contended host. |
| Affected modules | skills/messaging/gateway/markdown all PASS per-file with headroom. |
| Complete official suite `npm run test:ui` (default) | **6 failed / 3127 passed (raw exit 1)** on this host — environmental timeouts. **NOT certifiable at 0/0 here.** |
| Lint / format / typecheck / dep-scan / secret-scan / SAST / SEC-9 signing | Not re-run — **no code change**; baseline is the repo's CI-covered state. Re-running would only characterize untouched baseline. |
| `git diff --check` | Clean (docs only). |
| Attribution / branding | Docs author = Eiman; no retired-upstream product name introduced; no Claude attribution added (repo branding norm). |

No retry-to-green was used; no test was deleted, skipped, xfailed, weakened, or renamed; no timeout was altered.

## 7. Recommendation
Run `npm run test:ui` on a **quiescent host or the repo's CI runner** (its own `youtab-ci.yml` JS gate: `npm ci` + `npm run check` on ubuntu). Expectation, per §3: **0 failed** — the six components are correct at `20d69ce42`. If a durable robustness improvement is desired for constrained hosts, that is a separate, Owner-approved change to `vitest.config.ts` cold-start headroom (not made here, as it would mask rather than fix).

## 8. Blockers
1. Cannot obtain a trustworthy single-pass official-suite `0/0` on this contended host (65 procs / 16 cores).
2. No product defect found to "correct"; the round's premise (six real failures to fix) does not hold at this baseline.
3. Codex exact-SHA review pending (no final corrected SHA exists because no correction was warranted).
