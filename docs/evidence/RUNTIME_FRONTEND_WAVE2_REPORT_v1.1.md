# Runtime Frontend — Wave 2.1 Report v1.1 (supersedes v1.0)

SESSION 2/4 · Frontend Master Integrator · OWNER Requirements 1–12.
This report supersedes `RUNTIME_FRONTEND_WAVE2_REPORT_v1.0.md`. It records the
Wave-2.1 bounded correction pass: SHA reconciliation, visible ME-05 mount, official
Gitleaks, the zero-attribution clean delivery branch, REFERENCE_EXTERNAL_SYSTEM
language, and the corrected PX dependency.

## 1. SHA reconciliation (resolves the v1.0 contradiction)

- **Exact base SHA:** `20d69ce4245a51e105c381f811fb8cdb8490401c`.
- **Original (historical) working branch:** `feat/runtime-frontend-owner-1-12`, head `c869fe2f2362da1ed2275e4b4489d41967013df5` (preserved unchanged — not rewritten/amended/force-pushed).
- **v1.0 defect:** its "Final full SHA" listed `bdb3053e0` (the report's own PARENT). The actual working head that INCLUDES the v1.0 report commit is `129784d96`. A document cannot contain its own commit hash; this v1.1 report instead names the delivery branch head explicitly (below) and the hand-off records the 40-char value that includes this report commit.
- **Clean delivery branch:** `delivery/runtime-frontend-owner-1-12`, replayed from the exact base. Its head **including this v1.1 report commit** is the final delivery SHA (see the hand-off / `git rev-parse HEAD`).

## 2. Clean delivery branch — zero attribution (OWNER item 4)

`feat/runtime-frontend-owner-1-12` is preserved as historical local work; its five pre-Wave-2 commits carry a prohibited `Co-Authored-By: Claude Opus 4.8` trailer that a `.mailmap`/note cannot remove. Therefore a **new clean branch was created from the exact base and the final intended tree was replayed** as a small logical commit series.

Clean-branch commit series (base `20d69ce4…` → head), author **and** committer = `Eiman Ghazaei <eiman.ghazaei@gmail.com>`, **no** Co-Authored-By/Claude/Anthropic/OpenAI/Codex/AI attribution:

| # | SHA | Commit |
|---|---|---|
| 1 | `2d2b554c5` | docs(frontend): binding execution checklist + canonical contracts (OWNER 1-12) |
| 2 | `293c468c5` | feat(desktop): local file/folder UI slice — real consumers, truthful states |
| 3 | `48dd1ee13` | i18n(desktop): local-files, scan-state and model-receipt keys (additive) |
| 4 | `540aa4063` | feat(desktop): ME-05 inference-receipt UI mounted on the Model surface (real fields only) |
| 5 | `2c1123977` | test(desktop): SE-02 branding-integrity lock |
| 6 | `1c2dd1287` | test(desktop): API/MCP executable integration proofs via REFERENCE_EXTERNAL_SYSTEM (real consumers) |
| 7 | `6b5f54bc3` | docs(frontend): Wave 2 report v1.0 |
| 8 | (this commit) | docs(frontend): Wave 2.1 report v1.1 |

**Required attribution proof:** `git log 20d69ce4245a51e105c381f811fb8cdb8490401c..HEAD --format=%B | grep -iE "co-authored|claude|anthropic|openai|codex|generated with"` → **empty**. All authors and committers in range = Eiman Ghazaei.

## 3. Tree-equivalence (no intended file or feature lost)

`git diff feat/runtime-frontend-owner-1-12 <clean-branch, pre-v1.1>` (whole tree) → **EMPTY**; `git diff … -- apps/desktop/src` → **EMPTY**. The clean delivery **code/test tree (`apps/desktop/src`) is byte-identical** to the corrected source branch (all 21 source files + 3 docs reproduced). The only intended differences on delivery are documentation: this added `RUNTIME_FRONTEND_WAVE2_REPORT_v1.1.md` and a one-line checklist **totals correction** (ME-05 moved to BLOCKED → IMPLEMENTED_NOT_VERIFIED 4 / BLOCKED 16). No code or feature differs.

## 4. Exact commits and changed files (24 files, base→delivery)

Code/tests (`apps/desktop/src`): `lib/folder-grants.ts`(+test), `lib/file-ingress.ts`(+test), `app/files/local-files-panel.tsx`(+test), `store/composer.ts`, `global.d.ts`, `app/chat/composer/attachments.tsx`(+test), `app/model-receipt/{inference-receipt.tsx,inference-receipt.test.tsx,use-inference-receipt.ts}`, `app/settings/model-settings.tsx`, `app/settings/model-settings.test.tsx`, `app/settings/__tests__/api-connection.integration.test.ts`, `app/skills/__tests__/mcp-consumers.integration.test.ts`, `components/__tests__/branding-integrity.test.ts`, `i18n/{types,en,zh}.ts`. Docs: `docs/evidence/RUNTIME_FRONTEND_CONTRACTS_v1.0.md`, `docs/evidence/RUNTIME_FRONTEND_WAVE2_REPORT_v1.0.md`, `docs/roadmap/RUNTIME_FRONTEND_EXECUTION_CHECKLIST_v1.0.md`, plus this v1.1 report.

## 5. Official Gitleaks result (SE-03)

- **Scanner:** Gitleaks **v8.21.2** — `ghcr.io/gitleaks/gitleaks:v8.21.2` (digest `sha256:0e99e8821643ea5b235718642b93bb32486af9c8162c8b8731f7cbdc951a7f46`), run via Docker (ephemeral, pinned).
- **Command:** `gitleaks detect --no-git --source /scan -v --report-format json` over a staging dir containing `git log -p 20d69ce4…..HEAD` (every commit diff in the delivery range) **plus** the generated `docs/evidence` and `docs/roadmap` reports.
- **Exit code:** 0 · **Findings:** **0 ("no leaks found").**
- Supplemental regex scan of the 1411 added lines also 0. This is a SECRET scan, not a dependency/CVE scan.

## 6. Visible ME-05 proof (OWNER item 2)

`InferenceReceiptCard` is **mounted on the shipped Model surface** (`ModelSettings`, reachable via Settings → Models) — an additive `<section data-slot="inference-receipt-section">` at the end of the page, reusing existing navigation and the session-view state; **no duplicate settings page**. It shows the real available fields (model, provider, run id, tokens/cost from real `$currentUsage`) and displays **"not reported by backend"** for genuinely absent fields (server request-id, custom endpoint, latency) — never fabricated. Tests: a new mounted-reachability test (reachable through shipped UI; truthful empty state; **no fabricated run-id/status/endpoint/latency** before a run) plus the component's success / failed / invalid-credential / unavailable-provider / no-fabrication tests. The **live "execute real inference" proof stays BLOCKED** pending an ephemeral OWNER test credential.

## 7. Test results (targeted + full suite)

| Gate | Result |
|---|---|
| Targeted: model-settings.test.tsx + inference-receipt.test.tsx | **32 passed / 0 failed** |
| Targeted: API + MCP integration | **22 passed / 0 failed** |
| Targeted: branding-integrity | **5 passed / 0 failed** |
| Wave-1 targeted (file/folder) | 31 passed |
| Type-check (`tsc ×3`) | exit **0** |
| ESLint (`src/`) | **0 errors** (75 pre-existing warnings), exit 0 |
| Prettier (`src/**`) | clean |
| `git diff --check` | clean |
| Official Gitleaks | 0 findings, exit 0 |
| **Full serial `npm run test:ui`** | **6 failed / 3191 passed (3197 collected)**, exit 1 — failing = ONLY the 3 PX files, **zero non-PX failures** |

Serial-run stability (from Wave 2, per the verifier's procedure): two prior serial runs were identical (6/3190/3196); the "11-failure" run was a concurrent-load artifact (never run the full suite alongside other heavy commands). No retry used to manufacture green; no new skip/xfail.

## 8. All 36 checklist states

**Totals: DONE 0 · VERIFIED_NOT_REVIEWED 16 · IMPLEMENTED_NOT_VERIFIED 4 · BLOCKED 16 · NOT_STARTED 0.**

- **VERIFIED_NOT_REVIEWED (16):** ME-03, API-01, API-03, MCP-01, MCP-02, MCP-04, FR-01, FR-02, FR-03, FR-07, FR-08, AX-01, AX-02, SE-01, SE-02, SE-03.
- **IMPLEMENTED_NOT_VERIFIED (4):** ME-01, ME-02, ME-04 (static REAL-BACKED), API-02 (static).
- **BLOCKED (16):** ME-05 (live inference — receipt UI mounted+tested, live proof pends OWNER creds), MCP-03, FR-04/05/06, FW-01/02, AP-01, SC-01/02/03/04, E2E-01/02/03/04.
- **DONE (0):** none — no row DONE this pass; DONE needs all 10 conditions incl. real E2E + Codex APPROVED.

(ME-05 moved from IMPLEMENTED_NOT_VERIFIED → BLOCKED to truthfully reflect that its remaining gap is the LIVE inference proof, not the now-mounted UI.)

## 9. Sister dependency status (none integrated)

- **Runtime/Electron folder-grant SHA** — not received (FR-04/05/06, FW-01/02, AP-01, E2E-03).
- **Corrected Gateway file-ingress SHA** — not received; the current Gateway candidate is NO-GO and MUST NOT be integrated (SC-01..04, E2E-04).
- **Runtime PX handoff** — not received. Accepted forms: `NO_CODE_CHANGE_VERIFIED` (two official quiescent 0/0 runs) OR a real correction SHA. Until then PX-01..06 and the `0 failed / exit 0` full-suite gate stay BLOCKED.
- **OWNER** — live provider creds (ME-05/E2E-01), reachable MCP server (MCP-03/E2E-02), push/PR authorization.
- **Codex** — exact-SHA APPROVED (condition 10 of every DONE).

## 10. Exact remaining blockers & verdict

**FRONTEND NO-GO.** Blockers: (1) Runtime folder-grant bridge SHA; (2) corrected Gateway file-ingress SHA (current candidate NO-GO, not integrated); (3) Runtime PX handoff (`NO_CODE_CHANGE_VERIFIED` ×2 quiescent 0/0, or correction SHA) for the `0 failed` gate; (4) OWNER live provider creds / reachable MCP; (5) Codex exact-SHA APPROVED. No push, PR, merge, deploy, or Gateway-candidate integration performed. Final worktree clean.
