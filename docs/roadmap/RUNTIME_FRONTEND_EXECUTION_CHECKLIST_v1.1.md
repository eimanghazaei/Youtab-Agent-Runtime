# Runtime Frontend — Execution Checklist v1.1 (supersedes v1.0)

> Successor to `RUNTIME_FRONTEND_EXECUTION_CHECKLIST_v1.0.md` (not overwritten).
> This version binds every ACTIVE row to a full 40-character SHA on the **current
> clean delivery chain** `delivery/runtime-frontend-owner-1-12-v2` (base
> `20d69ce4245a51e105c381f811fb8cdb8490401c`), removes stale claims, and adds the
> review CHANGES-REQUIRED corrections. No row is `[x] DONE`.

## Historical → clean-chain SHA mapping (labelled historical; do NOT use historical SHAs in active rows)

| Historical SHA (abandoned branches) | Meaning | Clean-chain (v2) equivalent |
|---|---|---|
| `194bf8485` (branch `feat/runtime-frontend-owner-1-12`) | file/folder slice | `7e913b15800ffc13540348ad3ad1a662b5e84046` |
| `cf2aedb58` | ME-05 receipt UI | `0b293c59a8efd2c17a5fb277b29f2d9fa1b9ae4d` |
| `6b16fa5f2` | mount + fail-closed ingest + workspace | `08e8253b703b1ad3d7fbae2a742fbdd1b554b03b` (mount) + `7e913b158…` (ingest/workspace) |
| `fedc0c926` | PX-01..06 integration | `9cbc68ab86b35fc9664d23dcdde004d46c1e7f7f` |
| `e362364…` (branch `delivery/runtime-frontend-owner-1-12`) | had "codex" in 3 messages | superseded by v2 chain (0 attribution) |

Active clean-chain commits (base→HEAD): `09182127d` docs · `7e913b158` file/folder slice · `034dbe24c` i18n · `0b293c59a` ME-05 UI · `08e8253b7` panel mount · `e388bea00` branding · `9faa412c3` API/MCP proofs · `9cbc68ab8` PX integration · `9492d918b` reports · `bb0d8c913` v1.3 report · `93b0c3e0f` strict invariant + submit-enforce + file-ingest gate · `fc92f2681` CRLF/whitespace hygiene.

## Corrections applied (stale claims removed)

- **Removed:** the statement that cwd/profile is the current workspace identity. Truth: there is **no** canonical workspace identity in the frontend; `resolveCanonicalWorkspaceId()` returns `null` and the real authority (`GET /v1/agents/workspace/active`) is an **unapproved** candidate (`1be59d10`).
- **Corrected:** "clean+fileId already enforced" — the strict invariant is now implemented AND enforced on the real shared submit path with adversarial tests (CX-10/CX-11), so it is `VERIFIED_NOT_REVIEWED` (targeted tests), NOT claimed DONE.
- **Corrected:** cancellation is **local-only** today (the ingest is fail-closed; `AbortSignal` is threaded but no real transport exists). Real transport-cancellation is BLOCKED (CX-06).
- **Removed:** "only clean-chain SHAs" is now literally true — no historical SHA appears in an active row (they live only in the mapping table above).

## Review CHANGES-REQUIRED rows (Section K, updated + new)

| ID | Capability | Status | Evidence (clean-chain SHA) | Disposition |
|---|---|---|---|---|
| CX-01 | LocalFilesPanel mounted in shipped right-sidebar | `[ ] VERIFIED_NOT_REVIEWED` | `08e8253b7` | live browser-nav E2E still in blocked E2E gate |
| CX-02 | Invented ingress removed → fail-closed | `[ ] VERIFIED_NOT_REVIEWED` | `7e913b158` | real transport BLOCKED on approved Gateway SHA |
| CX-03 | Canonical workspace authority; no profile substitution | `[ ] VERIFIED_NOT_REVIEWED` | `7e913b158` | real id BLOCKED on approved authority `1be59d10` |
| CX-04 | Real byte-for-byte transport to Gateway | `[ ] BLOCKED` | — | approved Gateway ingress SHA |
| CX-05 | Clean file delivered end-to-end into Chat/Run | `[ ] BLOCKED` | — | needs approved ingress + workspace authority (invariant guard is in place, CX-11) |
| CX-06 | Real cancel through actual transport | `[ ] BLOCKED` | `93b0c3e0f` (AbortSignal threaded; local-only) | prove against real transport |
| CX-07 | Server-issued fileId in downstream request | `[ ] BLOCKED` | — | approved Gateway fileId |
| CX-08 | Full official suite 0 failed / 0 errors / exit 0 | `[ ] VERIFIED_NOT_REVIEWED` | `fc92f2681` | serial ×2 identical 0-failed (see v1.4 report) |
| CX-09 | Live headless-browser E2E (UI→Gateway→scanner→Chat/Run) | `[ ] BLOCKED` | — | approved Gateway+Runtime SHAs + live creds |
| CX-10 | **Strict attachability invariant + adversarial tests** | `[ ] VERIFIED_NOT_REVIEWED` | `93b0c3e0f` | closes the fail-open bypass; targeted adversarial suite green |
| CX-11 | **Invariant enforced in the real shared submit path (Chat + Run, single seam)** | `[ ] VERIFIED_NOT_REVIEWED` | `93b0c3e0f` | submit.ts filters non-attachable before payload; existing flow preserved |
| CX-12 | **No doomed Add-Files control (truthful capability-unavailable state)** | `[ ] VERIFIED_NOT_REVIEWED` | `93b0c3e0f` | file Add/drop gated behind ingest+authority; truthful unavailable state tested |

## Item 8 — quality-debt classification

| ID | Debt | Classification | Status | Disposition |
|---|---|---|---|---|
| QD-01 | 75 ESLint warnings | **68 `no-restricted-globals` + 6 `react-hooks/exhaustive-deps` + 1 — ALL pre-existing in files NOT part of this feature; 0 in this program's changed files** | `[ ] BLOCKED` | resolving requires a separate broad refactor across ~20 non-feature files (out of the local-file scope; "keep unrelated changes out of a change"); the `0-warnings` final gate is a repo-wide pre-existing-debt blocker |
| QD-02 | Range-bound `git diff --check 20d69ce4..HEAD` | whitespace/CRLF | `[ ] VERIFIED_NOT_REVIEWED` | **CLEAN** after normalizing a CRLF test file + one doc line (`fc92f2681`) |
| QD-03 | ~41 stderr sections in the passing suite | intentional negative-path diagnostics from EXISTING components (e.g. `[submit-drift-abort]` console.warn, RPC not-connected fallback, React key warnings) exercised by pre-existing tests; not emitted by this program's new tests | `[ ] BLOCKED` | a full per-node "expected stderr" assertion audit spans pre-existing tests outside this feature; classified, not individually asserted here |

## Verdict

**FRONTEND NO-GO.** Totals (48 rows: 45 from v1.0 + CX-10/11/12): DONE **0** · VERIFIED_NOT_REVIEWED **24** · IMPLEMENTED_NOT_VERIFIED **5** · BLOCKED **19** (incl. QD-01/QD-03 as debt blockers to the 0-warnings/stderr gate). Blockers: approved Gateway file-ingress SHA, approved Workspace Authority SHA, Runtime/Electron folder-grant impl, approved Runtime dep-CVE remediation, live provider creds / reachable MCP, live browser E2E, exact-SHA review APPROVED, and the repo-wide pre-existing ESLint/stderr debt (QD-01/QD-03). No row DONE.

## Wave 2.6 — Six-day delivery mode (in progress; frozen review checkpoint 9b1843005)

New rows (frontend-owned, buildable now):

| ID | Capability | Status | Evidence (clean-chain SHA) | Disposition |
|---|---|---|---|---|
| CX-13 | Bounded inline-image policy (MIME allowlist + byte cap; SVG excluded; fail-closed) — **defense-in-depth only** | `[ ] IMPLEMENTED_NOT_VERIFIED` | `f6d81464a` | authoritative inline-image validation/scanning is Gateway's (DR-GW-3); needs live E2E |
| CX-14 | Capability requires explicit feature fields + workspace context + compatible schema (not just $gatewayState open) | `[ ] IMPLEMENTED_NOT_VERIFIED` | `f6d81464a` | real availability requires the approved Gateway status contract + live E2E |
| CX-15 | refText treated as a server-RETURNED reference (not authority/immutable); Gateway MUST revalidate at submit — **required contract (DR-GW-2), not a proven fact** | `[ ] IMPLEMENTED_NOT_VERIFIED` | `f6d81464a` + `b3de32bf6` | Gateway/Runtime revalidation unproven until live cross-repo E2E |

## Dependency requests sent to the owning sibling sessions (frontend BLOCKED until delivered)

- **DR-GW-1 (Gateway):** approve + advertise file-ingress capability in `GET /api/status` as `features:{file_ingress:true,workspace_authority:true}` + `workspace:{id}` + `file_capability_schema:1`; deliver `POST /v1/files/upload` (snake_case `file_id`/`workspace_id`) + scan lifecycle + error codes. Source candidate `5064b54a913f428bd61917adcf63830b9f92577d` / evidence `34a3e72624c6adf7a66c34c937a9d16021958147` — NOT approved. Blocks F3 live, SC-*, CX-04/05/07, E2E-04.
- **DR-GW-2 (Gateway):** approved Workspace Authority `GET /v1/agents/workspace/active` → `{workspace_id}`; revalidation of refText/fileId at submit. Blocks CX-03/05, SC-04.
- **DR-GW-3 (Gateway):** approved server-side inline-image validation/scanning policy (bounded MIME/size + malware scan). Blocks CX-13 full closure.
- **DR-RT-1 (Runtime/Electron):** real `window.youtabDesktop.folderGrants` implementation (request/list/revoke/read/write + traversal/symlink enforcement + approval-before-write). Blocks FR-04/05/06, FW-01/02, AP-01, E2E-03.
- **DR-RT-2 (Runtime):** consume approved dep-CVE remediation `e5edea91df1cac36db0417f5d0a2e95123e57bb1` (npm audit 0) at final integration — NOT duplicated in frontend. Blocks the npm-audit gate.
- **DR-RT-3 (Runtime):** real CRM / ERP / SAP / CAD-DFM-FEA typed operation contracts + approval/effect/receipt/reconciliation backend for F4/F5. No vendor platform built in frontend; UI consumes typed manifests + real Runtime operations when delivered.
- ~~**DR-OWNER:** live provider creds / MCP / E2E authorization~~ — **REMOVED as a blocker.** The Owner authorized ephemeral test-only secrets, deterministic local reference services, and teardown. F1/F2 live proof + F6 E2E proceed against ephemeral LOCAL reference services (real Frontend consumers + config UI + persistence; no mocked Gateway/UI handlers); live vendor credentials are NOT required.

## Scope status (six-day mode)

- **F1 Model/Engine, F2 API/MCP:** already REAL/backed in-repo (verified prior waves); live inference / live MCP-op / real tool-listing E2E are OWNER/live-dependency-blocked.
- **F3 File/Folder:** security hardening done (CX-10..15); ingest/workspace/folder-grant BLOCKED on DR-GW-1/2, DR-RT-1.
- **F4 Enterprise (CRM/ERP/SAP/CAD), F5 Approval/Receipts, F6 Browser E2E:** BLOCKED on DR-RT-3, DR-GW-*, DR-RT-1, DR-OWNER — no fake/disconnected surfaces built.

## Wave 2.6 — F1–F6 buildability audit (truthful, no fabrication)

Substrate audit (read-only) results + this-session execution feasibility:

| Agent | Substrate | This-session execution | Status / action |
|---|---|---|---|
| **F1 Model/Engine** | REAL config UI + consumers (`app/settings/*`) + routes `/api/config`, `/api/env`, `/api/providers/custom-endpoints*`, `/api/providers/validate`, OAuth. create/test/use/revoke proven at the reference-service boundary (`api-connection.integration.test.ts`, Wave-2.2, `7e913b158`/`9faa412c3`). | Full-app inference E2E needs electron+backend (see F6). | `[ ] IMPLEMENTED_NOT_VERIFIED` — reference-proven; live app E2E BLOCKED on F6 env |
| **F2 API/MCP** | REAL MCP UI (`app/skills/mcp-tab.tsx`) + routes `/api/mcp/servers*`, catalog, auth, oauth flows. add/auth/test/catalog/remove proven (`mcp-consumers.integration.test.ts`). | Same as F1. | `[ ] IMPLEMENTED_NOT_VERIFIED` — reference-proven; live op E2E BLOCKED on F6 env |
| **F3 File/Folder** | Security hardened this session (CX-10..15). | Ingest/folder-grant fail-closed. | live BLOCKED on DR-GW-1/2/3, DR-RT-1 |
| **F4 Enterprise (CRM/ERP/SAP/CAD)** | **BUILT (reference-backed, real consumer)** `3d187115b` — `app/enterprise/`: typed `OperationManifest` consumer + generic surface for CRM/ERP/SAP/CAD (params/risk/approval/preview/execute/receipt/reconcile phases), driven by a deterministic **reference** provider with a persistent `source: reference` badge. Runtime-swappable via injected `execute`/`reconcile`. 18 targeted tests. | reference-backed only. | `[ ] IMPLEMENTED_NOT_VERIFIED` — consumer+UI real; **real backend still BLOCKED DR-RT-3a**. Owner-authorized reference until Runtime exact-SHA ops land. |
| **F5 Approval/Receipts** | **BUILT (reference-backed, real consumer)** `3d187115b` — `app/governance/`: request→approve/deny→effect→receipt→reconcile consumer over a deterministic **reference** approval+effect-ledger service incl. adversarial terminals (deny/expiry/replay/payload_modified/revoked_delegation/workspace_mismatch); reference receipts never carry a fabricated runtime effectId/receiptId. 23 targeted tests. | reference-backed only. | `[ ] IMPLEMENTED_NOT_VERIFIED` — consumer+UI real; **real backend still BLOCKED DR-RT-3b**. Owner-authorized reference until Runtime exact-SHA API lands. |
| **F6 Browser E2E** | **EXISTS + NOW RUN HERE** — Playwright `@playwright/test 1.58.2`, `apps/desktop/e2e/`. **CORRECTION: the earlier "NOT RUNNABLE / CI-only" claim was WRONG.** electron.exe IS present at the monorepo-root `node_modules/electron/dist/electron.exe`; `npm run build` produces `dist/`; the REAL youtab backend boots with `YOUTAB_AGENT_DESKTOP_PYTHON=<system python that imports youtab_agent_cli>` (this worktree contains the `youtab_agent_cli` package). A Windows portability fix landed in `e2e/fixtures.ts` (`findElectron` prefers `electron.exe`). | **RUNNABLE + RUN:** `boot.spec.ts` 4/4; **`e2e/enterprise-governance.spec.ts` 9/9 passed exit 0** on the real chain electron→real `youtab serve`→real gateway→renderer (only external LLM vendor mocked). | `[ ] IMPLEMENTED_NOT_VERIFIED` — real E2E green for enterprise/governance (`1a73a72f5`); still NOT DONE (real Gateway ingress/folder-grant/enterprise+governance backends absent; Codex APPROVED pending) |

**Real Browser E2E landed `1a73a72f5` — scenarios a–l all collected + passing (9/9):** a nav entries visible; b CRM/ERP/SAP/CAD truthful `source: reference`; c preview → no effect; d denial → no execution; e approval executes exactly once (2nd execute needs fresh approval); f–i governance adversarial rejections (replay/payload_modified/workspace_mismatch/revoked_delegation) via GENUINE reference-service calls (real visible controls added to the governance panel — no fabricated receipts, `source:reference`/`effectId null`); j receipt + reconciliation visible; k missing backend → visible fail-closed BootFailureOverlay; l File/Folder Add control visible-but-disabled fail-closed until real capability. Transports real; only the external LLM vendor is reference/mock (Owner rule satisfied). No skip/xfail/weakening.

**Machine-readable sibling-contract fixtures published `b0a295aa1`** — `src/contracts/fixtures/{dr-gw-1,dr-gw-2,dr-gw-3,dr-rt-1,dr-rt-3a,dr-rt-3b}.json` + `contract-manifest.ts` + `contract-conformance.test.ts` (23 tests). Frontend consumers proven shape-compatible (compile-time-exhaustive union/field guards + runtime key checks catch drift); every fixture pinned `status:PENDING_INTEGRATION` and a test asserts passing implies NO backend is verified. `ContractStatus` has no VERIFIED member.

**Mount (no hidden controls):** both panels are reachable + visible — mounted `86a34fdcf` as `/enterprise` + `/governance` ROUTES_AREA pages with SIDEBAR_NAV_AREA nav rows via the existing plugin contribution registry (no core AppView/AppRouteId/APP_ROUTES/shell edits). Sidebar/route-tile/contrib suites 122 pass.

**DR-RT-3 refined:** split into DR-RT-3a (operation-manifest consumer contract) and DR-RT-3b (approval + effect-ledger API). The frontend now ships the REAL typed consumers + UI (`3d187115b`) driven by deterministic, clearly-labelled **reference** providers (Owner-authorized). These are NOT fake success handlers: each surface is always truthful that the real Runtime backend is a pending dependency (`source: reference` badge + DR-RT-3a/3b note), and swaps to real Runtime ops by injecting the real `execute`/`reconcile`/decision service with no state-machine change. F4/F5 stay `IMPLEMENTED_NOT_VERIFIED` (never DONE) until the exact-SHA Runtime contracts land + live cross-repo E2E + Codex approve.

**Exact typed contract the Runtime session must satisfy — DR-RT-3a (enterprise operation-manifest):**
```ts
type OperationDomain = 'crm' | 'erp' | 'sap' | 'cad'
type RiskLevel = 'low' | 'medium' | 'high'
interface OperationParam { key: string; label: string; type: 'string'|'number'|'boolean'|'enum'; required: boolean; options?: string[] }
interface OperationManifest { id: string; domain: OperationDomain; title: string; description: string; params: OperationParam[]; risk: RiskLevel; requiresApproval: boolean }
type OperationPhase = 'idle'|'preview'|'approval_required'|'approved'|'executing'|'effect_complete'|'reconciled'|'reconcile_failed'|'denied'|'failed'
interface OperationReceipt { effectId: string | null; status: OperationPhase; detail: string | null; source: 'reference' | 'runtime' }
// runtime source ⇒ authoritative effectId/status from the exact-SHA effect ledger; reference source ⇒ deterministic ref-id, effectId null.
```

**Exact typed contract the Runtime session must satisfy — DR-RT-3b (approval + effect-ledger):**
```ts
type GovernancePhase = 'approval_required'|'approved'|'denied'|'expired'|'payload_modified'|'replay_rejected'|'revoked_delegation'|'workspace_mismatch'|'effect_complete'|'reconcile_succeeded'|'reconcile_failed'
interface GovernanceRequest { id: string; action: string; workspaceId: string; payloadHash: string; requiresApproval: boolean; delegationId: string | null }
interface GovernanceReceipt { requestId: string; phase: GovernancePhase; effectId: string | null; receiptId: string | null; source: 'reference' | 'runtime'; detail: string | null }
// service surface: submitRequest(req); decide(id,'approve'|'deny',currentPayloadHash?); completeEffect(id); reconcile(id); getReceipt(id).
// effectId/receiptId non-null ONLY when source==='runtime'; payloadHash MUST be secret/local-path free and re-checked at decision.
```

**F6 execution environment note:** the E2E harness is real and boots without live vendors, but this session lacks the electron binary + a built `dist/` + a provisioned youtab backend runtime. Executing the full browser path (including a new F1/F2 provider-config → inference spec) requires that environment (available in CI). A speculative, unexecutable e2e spec is intentionally NOT committed (violates the targeted-test-green rule).
