# Runtime Frontend — Wave 2.3 Report v1.3 (supersedes v1.2)

SESSION 2/4 · Frontend Master Integrator · OWNER Requirements 1–12.
Exact-SHA review CHANGES-REQUIRED correction pass + clean-attribution delivery.

## 1. Delivery branches & SHAs (full 40-character)

- **Base SHA:** `20d69ce4245a51e105c381f811fb8cdb8490401c`
- **Prior candidate (v1.2, exact-SHA reviewed → CHANGES REQUIRED):** `5d5e4f73b56cd5028c448b0a3e53695b365a180e` (frozen, not amended).
- **Historical correction branch (preserved):** `delivery/runtime-frontend-owner-1-12`, head `e362364fecad22ccffed216b28d6d2745d593e69` — carries the correct code but 3 commit MESSAGES contain the token "Codex" (an OWNER-forbidden attribution token). Preserved as historical; NOT the delivery.
- **Clean delivery branch:** `delivery/runtime-frontend-owner-1-12-v2` — the final tree replayed from base with sanitized commit messages (0 forbidden attribution). Pre-v1.3 head `9492d918b3fc857f74c4ee721fd215c89ad94894`; **final SHA including this v1.3 report** = the branch HEAD after this commit (a document cannot embed its own hash; the 40-char value is in the hand-off / `git rev-parse HEAD`).

## 2. Clean-attribution remediation (why v2 exists)

The v1.2 exact-SHA review returned CHANGES REQUIRED. The additive corrections were made on descendants of the frozen candidate, but three of those commit messages contained the word "Codex" — which the OWNER lists among forbidden attribution tokens (`co-authored | claude | anthropic | openai | codex | generated with`). History is not rewritten. As with the earlier `Co-Authored-By` remediation, the final tree was **replayed onto a fresh branch (`…-v2`) with sanitized messages**. 

- **Attribution scan** `git log 20d69ce4…..HEAD --format=%B | grep -icE "co-authored|claude|anthropic|openai|codex|generated with"` → **0**.
- **All authors and committers** = `Eiman Ghazaei <eiman.ghazaei@gmail.com>`.
- **Whole-tree diff** `git diff <historical e362364> <v2 HEAD>` → **EMPTY** (byte-identical code AND docs, minus this added v1.3 report).

## 3. Commit series (base → v2 HEAD), 0 attribution

| # | Full SHA | Commit |
|---|---|---|
| 1 | `09182127d…` (`09182127d`) | docs: binding execution checklist + canonical contracts |
| 2 | `7e913b158` | feat: local file/folder UI slice — real consumers, fail-closed ingest, truthful states |
| 3 | `034dbe24c` | i18n: local-files, scan-state and model summary keys (additive) |
| 4 | `0b293c59a` | feat: ME-05 Inference summary UI mounted on the Model surface (real fields only) |
| 5 | `08e8253b7` | fix: mount Local files panel in the shipped right-sidebar surface (reachable, not orphaned) |
| 6 | `e388bea00` | test: SE-02 branding-integrity lock |
| 7 | `9faa412c3` | test: API/MCP executable integration proofs via REFERENCE_EXTERNAL_SYSTEM (real consumers) |
| 8 | `9cbc68ab8` | test: integrate Runtime PX-01..06 harness correction (test-only, static imports) |
| 9 | `9492d918b` | docs: Wave 2 / 2.1 / 2.2 evidence reports |
| 10 | (this commit) | docs: Wave 2.3 report v1.3 |

## 4. Review CHANGES-REQUIRED corrections (mandatory items)

- **Item 1 — panel mounted (CX-01):** LocalFilesPanel was unmounted/orphaned (only direct-render tested). Now MOUNTED in the shipped right-sidebar (`RightSidebarPane`), ErrorBoundary-wrapped, with a reachability test asserting `data-slot=local-files-panel-mount` contains the real panel. This also **falsified and removed** the earlier AX-02 "0 disconnected controls" claim (downgraded).
- **Item 2 — invented ingress removed (CX-02):** the fabricated `file.ingress` method + ArrayBuffer-over-JSON-RPC payload were removed. `lib/file-ingress.ts` is now **fail-closed**: transmits nothing, returns `scanner_unavailable` (`gateway_ingress_contract_unavailable`) — no method/response shape/casing invented. `AbortSignal` threaded so cancel stays real. `isAttachable` requires `state==='clean'` AND a non-empty server-issued `fileId`.
- **Item 3 — canonical workspace authority (CX-03):** removed `workspaceId: activeProfile` (a profile substitution). New `lib/workspace-identity.ts` — `resolveCanonicalWorkspaceId()` returns `null` (no backend authority integrated; never substitutes profile/cwd/free text); the panel fails closed (`workspace_denied`). Tests prove a profile change is NOT a workspace-authority change.
- **Items 4/5/6/7 (CX-04..07):** real byte transport, clean-file invariant into the shared submit path, real cancel proof through the transport, and downstream server `fileId` — all **BLOCKED** on the integrated Gateway ingress contract + backend workspace authority. Not invented; existing chat `file.attach` flow untouched.
- **PX (CX-08):** integrated the Runtime session's PX-01..06 correction (test-only harness fix — heavy view modules moved out of the timed `it()` body; static/`vi.hoisted` imports; no timeout/skip/xfail/retry/weakening), from `fix/runtime-px-correction-v1` @ `0ca38740` (code `c92069a5a`). The PX correction awaits its own exact-SHA review.
- **Item 8 — evidence corrected:** unsupported statuses downgraded, the false "0 disconnected controls" claim removed, commands/artifacts bound to the exact final SHA, the db2ba509-vs-5d5e4f73 contradiction reconciled, and only clean-chain SHAs used.

## 5. Direct-on-v2-SHA qualification (not tree-equivalence)

Run in `apps/desktop` of the v2 worktree (its own node_modules), directly on the v2 head:

| Gate | Exit | Result |
|---|---|---|
| targeted (11 files: file/folder + mounted-UI + API/MCP + branding + workspace-identity + right-sidebar mount) | 0 | **96 passed / 0 failed** |
| `npm run typecheck` (tsc ×3) | 0 | clean |
| `npx eslint src/` | 0 | 0 errors (pre-existing warnings only) |
| `npx prettier --check 'src/**/*.{ts,tsx}'` | 0 | clean |
| `git diff --check` | 0 | clean |
| attribution scan (base..HEAD) | — | **0 forbidden** |
| official Gitleaks v8.21.2 (`ghcr.io/gitleaks/gitleaks:v8.21.2`, digest `sha256:0e99e88…`) over the v2 delivery range + evidence/reports | 0 | **0 findings ("no leaks found")** |
| **`npm run test:ui` (serial, full output + JUnit)** | 0 | **362 files / 3201 passed / 0 failed / 0 errors** |

The 0-failed suite is achieved after integrating the PX correction. Prior to integration, the full suite was `6 failed (PX only) / 3195 passed`; the PX correction resolves exactly those 6 nodes with no new failure. No retry manufactured green; no new skip/xfail; no mock/reference-service reported as live E2E.

## 6. Inference summary decision (unchanged from v1.2)

Backend exposes NO canonical durable inference receipt. The visible UI is "Inference summary" (not a durable receipt); absent fields render "not reported by backend"; the durable-receipt requirement + live inference stay BLOCKED (ME-05).

## 7. All 45 checklist statuses

**Totals: DONE 0 · VERIFIED_NOT_REVIEWED 19 · IMPLEMENTED_NOT_VERIFIED 5 · BLOCKED 21 · NOT_STARTED 0.**

- **VERIFIED_NOT_REVIEWED (19):** ME-03, API-01, API-03, MCP-01, MCP-02, MCP-04, FR-01, FR-02, FR-03, FR-07, FR-08, AX-01, SE-01, SE-02, SE-03, CX-01, CX-02, CX-03, CX-08.
- **IMPLEMENTED_NOT_VERIFIED (5):** ME-01, ME-02, ME-04, API-02, AX-02.
- **BLOCKED (21):** ME-05, MCP-03, FR-04/05/06, FW-01/02, AP-01, SC-01/02/03/04, CX-04/05/06/07/09, E2E-01/02/03/04.
- **DONE (0):** none — no row DONE this pass.

## 8. The six PX nodes (now passing via integrated correction)

PX-01..04 `src/app/skills/index.test.tsx`, PX-05 `src/app/settings/gateway-settings.test.tsx`, PX-06 `src/app/messaging/index.test.tsx` — all pass after the integrated harness correction (3 files / 12 tests). The full suite is 0 failed. The PX correction itself awaits its own exact-SHA review.

## 9. Sister dependency status (none of the runtime/gateway bridges integrated)

- Runtime/Electron folder-grant bridge SHA — not received (FR-04/05/06, FW-01/02, AP-01, E2E-03, CX-04/06).
- Corrected Gateway file-ingress SHA — not received; current candidate is NO-GO, not integrated (SC-01..04, E2E-04, CX-04/05/07).
- Backend canonical workspace authority — does not exist (CX-03 real id, SC-04).
- Backend durable inference-receipt contract — does not exist (ME-05).
- Runtime PX correction — integrated (test-only); awaits its own exact-SHA review.
- OWNER — live provider creds (ME-05/E2E-01), reachable MCP server (MCP-03/E2E-02), push/PR authorization.
- Exact-SHA review APPROVED — pending on the v2 final SHA (condition 10 of every DONE).

## 10. Verdict

**FRONTEND NO-GO.** The full official suite is 0 failed / 0 errors / exit 0 and attribution is clean, but the real Gateway ingress + Runtime folder-grant bridges, the canonical workspace authority, the durable receipt contract, live provider/MCP credentials, and the live browser E2E remain undelivered/blocked, and the exact-SHA review APPROVED gate is pending. No row is DONE. No push, PR, merge, deploy, or Gateway-candidate integration performed. Final worktree clean. Stop for exact-SHA review of the v2 final candidate.
