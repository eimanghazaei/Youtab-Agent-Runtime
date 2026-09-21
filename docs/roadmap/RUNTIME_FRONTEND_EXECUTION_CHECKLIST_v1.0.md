# Runtime Frontend — Execution Checklist v1.0 (OWNER Requirements 1–12)

> **Binding execution record.** This file governs the Agent Runtime frontend work
> (SESSION 2/4 — Frontend Master Integrator). Read this checklist before starting
> **every** task. Update it after **every** completed task. Never bulk-check. Never
> remove/rename/silently-defer an unfinished row. New discovered work = a new
> numbered row. No `FRONTEND GO` unless every mandatory row is `[x] DONE`.

## Baseline (immutable for this branch)

| Field | Value |
|---|---|
| Repo | `eimanghazaei/Youtab-Agent-Runtime` |
| Base commit (full SHA) | `20d69ce4245a51e105c381f811fb8cdb8490401c` |
| Base subject | WAVE-30H SEC-9: pre-benchmark security remediation (8/9 findings) |
| Base author / date | Eiman Ghazaei — 2026-09-13 |
| Working branch | `feat/runtime-frontend-owner-1-12` |
| Working worktree | `F:\Youtab_AI_COS_Platform\_wt\ar-frontend-1-12` (isolated) |
| Active Runtime session worktree (DO NOT TOUCH) | main checkout on `feat/wave30h-sec9-remediation` @ same SHA |
| SEC-9 lineage proof | ancestry contains `43075c110`, `ac3fce0ec`, `04d6ebaf7`, `812750e38` ✓ |
| Recorded divergence | `origin/main` `c7650a1b920224283ba3a59ca054d6625e07a5f3` is **NOT** an ancestor (base sits on older main; OWNER-chosen) |
| Scope | `apps/desktop/src/**` only |
| Started | 2026-09-21 |

## Allowed statuses (only these)

`[ ] NOT_STARTED` · `[ ] IN_PROGRESS` · `[ ] BLOCKED` · `[ ] IMPLEMENTED_NOT_VERIFIED` · `[ ] VERIFIED_NOT_REVIEWED` · `[x] DONE`

## `[x] DONE` gate — all 10 required, never inferred from implementation

1. production implementation exists · 2. UI control visible & user-friendly · 3. connected to real Gateway/Runtime · 4. no mock/stub/fake-success path · 5. targeted tests pass · 6. relevant full suite `0 failed / 0 errors` · 7. real `UI→backend/Runtime→effect→receipt` E2E passes · 8. evidence bound to exact full SHA · 9. independent verifier accepts · 10. Codex returns `APPROVED` for that exact SHA.

## Toolchain commands (run in `apps/desktop`)

| Gate | Command |
|---|---|
| Targeted UI tests | `vitest run --project ui <path>` |
| Full desktop unit suite | `npm run test:ui` (`vitest run --project ui`) |
| Type-check | `npm run typecheck` |
| Lint | `npm run lint` (`eslint src/ electron/`) |
| Format check | `npx prettier --check 'src/**/*.{ts,tsx}'` |
| Platform/component tests | `npm run test:desktop:platforms` |
| Whitespace/diff | `git diff --check` |
| E2E (real backend) | `npm run test:e2e` (Playwright) |

## Agent ownership (non-overlapping; Master edits shared files only)

- **Master (me):** `youtab.ts`, `lib/model-options.ts`, `app/gateway/hooks/use-gateway-request.ts`, `types/youtab.ts`, `global.d.ts` (type only), `store/notifications.ts`, `i18n/types.ts`+locales, `app/settings/index.tsx`, `app/skills/index.tsx`, shared primitives — plus this checklist, contracts, reports, integration.
- **Agent 1 — Model/Engine:** `app/settings/model-settings.tsx`(+test), `fallback-models-field.tsx`, `components/model-visibility-dialog.tsx`, `app/model-visibility-overlay.tsx`, `store/model-presets.ts`, `store/model-visibility.ts`, `store/provider-collapse.ts`, `lib/model-status-label.ts`, `lib/reasoning-effort.ts`, `lib/model-search-text.ts`.
- **Agent 2 — API/MCP:** `app/skills/mcp-tab.tsx`(+new siblings), `lib/mcp-tool-filter.ts`, `lib/mcp-dashboard-oauth.ts`, `app/settings/providers-settings.tsx`, `credential-key-ui.tsx`, `env-credentials.tsx`, `keys-settings.tsx`, `custom-endpoints-settings.tsx`, `components/onboarding/*`, `store/onboarding.ts`, new API-connection files.
- **Agent 3 — File/Folder:** new `app/files/*`, new composer siblings under `app/chat/composer/`, `store/composer.ts` (uploadState union), new `lib/desktop-fs.ts` folder-grant wrappers, new file-ingress consumer.
- **Agent 4 — Verifier:** read-only until each writer commits; a11y/branding/truthful-state review; browser E2E; branding-integrity test (new).

---

## Section A — Model/Engine (OWNER 1–3)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ME-01 | Add/edit/remove provider & engine via visible UI | A1 | model-settings.tsx, providers-settings.tsx | real `/api/model/*`, `/api/providers/*` (in-repo) | `[ ] IMPLEMENTED_NOT_VERIFIED` | `vitest run --project ui src/app/settings/model-settings.test.tsx` | CRUD renders + persists | Agent 4 static REAL-BACKED: setModelAssignment→POST /api/model/set (model-settings:622); no fakes | — | pre-existing | pending | needs live E2E + Codex |
| ME-02 | Persist model/engine config through real API | A1 | model-settings.tsx | `/api/model/set`, `/api/config` | `[ ] IMPLEMENTED_NOT_VERIFIED` | (targeted) | set persists across reload | Agent 4: saveYoutabConfig→PUT /api/config (:529); REAL | — | pre-existing | pending | needs live E2E + Codex |
| ME-03 | Validate provider credential/endpoint | A1/C | custom-endpoints-settings.tsx, youtab.ts | `/api/providers/validate`, `validateCustomEndpoint` | `[ ] VERIFIED_NOT_REVIEWED` | `vitest run --project ui src/app/settings/__tests__/api-connection.integration.test.ts` | valid/invalid states truthful | Wave-2 integration test: validateCustomEndpoint emits POST /api/providers/custom-endpoints/validate, reachable=false surfaced truthfully; provider-credential validate still static | 7b600f60a | 7b600f60a | pending | needs live E2E + Codex |
| ME-04 | Discover/select models (catalogue) | A1 | model-settings.tsx, lib/model-options.ts | `model.options` RPC / `/api/model/options` | `[ ] IMPLEMENTED_NOT_VERIFIED` | (targeted) | catalogue lists real providers | Agent 4: requestModelOptions gateway-first REST-fallback; REAL | — | pre-existing | pending | needs live E2E + Codex |
| ME-05 | Execute & prove a real inference request (durable receipt) | A1+Master | app/settings/model-settings.tsx (mount), app/model-receipt/* , i18n | live gateway + provider creds + **canonical durable receipt contract** | `[ ] BLOCKED` | `vitest run --project ui src/app/settings/model-settings.test.tsx src/app/model-receipt/inference-receipt.test.tsx` | live prompt + durable receipt returned | Wave-2.2: backend exposes **NO canonical durable receipt** (no receipt_id/effect_id; no server request-id on prompt.submit / message.complete). Visible UI truthfully renamed **"Inference summary"** (not a durable receipt); mounted on Settings→Models, reachable + truthful-empty + no-fabrication tested (32 tests). **Durable-receipt requirement + live inference stay BLOCKED** pending Backend receipt contract + ephemeral OWNER creds | c05f481… (rename descendant) | see v1.2 | pending | blocker: Backend durable-receipt contract + live provider creds (OWNER) |

## Section B — API connections (OWNER 4)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| API-01 | User-friendly connection setup + create/edit/test/delete | A2/C | gateway-settings.tsx, custom-endpoints, youtab.ts | real bridge + `/api/providers/custom-endpoints*` | `[ ] VERIFIED_NOT_REVIEWED` | `vitest run --project ui src/app/settings/__tests__/api-connection.integration.test.ts` | create/edit/test/delete real; invalid fails visibly | Wave-2 integration test (10): save/validate/activate/delete emit correct routes; blank base_url + unknown id reject visibly, nothing persisted | 7b600f60a | 7b600f60a | pending | needs live E2E + Codex (⚠ PX-05 pre-existing test fail in gateway-settings.test.tsx → Runtime lane) |
| API-02 | Secret-safe credential entry | A2 | credential-key-ui.tsx, env-credentials.tsx | `/api/env` (masked, server-side) | `[ ] IMPLEMENTED_NOT_VERIFIED` | (targeted) | never in localStorage | Agent 4 REAL: setEnvVar→/api/env, masked; only localStorage touch is removeItem cleanup; no secret persisted | — | pre-existing | pending | needs live E2E + Codex |
| API-03 | Test connection / connectivity proof | A2/C | custom-endpoints, youtab.ts | `validateCustomEndpoint`, `testConnectionConfig` | `[ ] VERIFIED_NOT_REVIEWED` | (Wave-2 integration test) | probe returns real status | Wave-2: validateCustomEndpoint reachable=false surfaced truthfully for unreachable endpoint | 7b600f60a | 7b600f60a | pending | needs live E2E + Codex |

## Section C — MCP (OWNER 5)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| MCP-01 | Add/edit/remove MCP server via UI | A2/C | app/skills/mcp-tab.tsx, youtab.ts | `/api/mcp/servers*` | `[ ] VERIFIED_NOT_REVIEWED` | `vitest run --project ui src/app/skills/__tests__/mcp-consumers.integration.test.ts` | CRUD persists to config | Wave-2 integration test (12): saveMcpServers emits PUT /api/mcp/servers; stdio-without-command + remote-without-url reject visibly, nothing persisted | 7b600f60a | 7b600f60a | pending | needs live E2E + Codex (⚠ PX-01..04 pre-existing fails in skills/index.test.tsx → Runtime lane) |
| MCP-02 | Test MCP server (probe) | A2/C | mcp-tab.tsx, youtab.ts | `/api/mcp/servers/{n}/test` | `[ ] VERIFIED_NOT_REVIEWED` | (Wave-2 integration test) | status `ok/needs-auth/error` truthful | Wave-2: testMcpServer emits POST .../test; unknown server → truthful ok:false+error (not a transport crash) | 7b600f60a | 7b600f60a | pending | needs live E2E + Codex |
| MCP-03 | Activate & use MCP (tool call receipt) | A2+Master | mcp-tab.tsx, reload.mcp | live gateway + reachable server | `[ ] BLOCKED` | E2E | real tool invocation receipt | reload.mcp RPC wired (real); needs a reachable server for a live tool receipt | — | — | — | blocker: reachable MCP server (OWNER) |
| MCP-04 | MCP OAuth flow | A2/C | lib/mcp-dashboard-oauth.ts, youtab.ts | `/api/mcp/servers/{n}/auth` | `[ ] VERIFIED_NOT_REVIEWED` | (Wave-2 integration test) | flow completes truthfully | Wave-2: authMcpServer + getMcpOAuthFlow emit correct routes; unknown server rejects | 7b600f60a | 7b600f60a | pending | needs live E2E + Codex |

## Section D — File/Folder Read (OWNER 6, 7-read)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| FR-01 | File select (single/multi) via UI | A3 | app/files/local-files-panel.tsx | existing `selectPaths` + `file.attach` | `[ ] VERIFIED_NOT_REVIEWED` | `vitest run --project ui src/app/files/local-files-panel.test.tsx` | files selectable + chips | 31/31 pass + Agent V review PASS (item 5) | 194bf8485 | 194bf8485 | pending | needs live E2E + Codex |
| FR-02 | Drag-and-drop files | A3 | local-files-panel.tsx (useFileDropZone) | existing pipeline | `[ ] VERIFIED_NOT_REVIEWED` | (targeted) | OS drop → attach | pass + V review | 194bf8485 | 194bf8485 | pending | needs live E2E + Codex |
| FR-03 | Progress / cancel / retry / remove | A3 | store/composer.ts, attachments.tsx | existing eager-upload | `[ ] VERIFIED_NOT_REVIEWED` | (targeted) | each state truthful | pass + V review | 194bf8485 | 194bf8485 | pending | needs live E2E + Codex |
| FR-04 | Explicit folder grant (read-only) | A3 | app/files/*, lib/folder-grants.ts | **Runtime folder-grant bridge (SIBLING)** | `[ ] BLOCKED` | (targeted, mocked bridge) | grant shows safe label, read-only | consumer impl + truthful-unavailable tested (mocked-available grant/revoke tested) | 194bf8485 | 194bf8485 | pending | blocker: Runtime/Electron bridge (real grant) |
| FR-05 | Real read inside granted root | A3 | lib/folder-grants.ts `readInGrant` | **Runtime bridge `.read`** | `[ ] BLOCKED` | E2E | real file bytes returned | consumer impl; throws when bridge absent | 194bf8485 | 194bf8485 | pending | blocker: Runtime/Electron bridge |
| FR-06 | Grant status + revoke; post-revoke reject | A3 | app/files/*, lib/folder-grants.ts | **Runtime bridge `.list/.revoke`** | `[ ] BLOCKED` | (targeted) | revoke removes access immediately | revoke wired + tested (mocked); real backing pending | 194bf8485 | 194bf8485 | pending | blocker: Runtime/Electron bridge |
| FR-07 | Bridge-unavailable → truthful "Local Runtime required" | A3 | app/files/*, lib/folder-grants.ts | none (frontend truthful state) | `[ ] VERIFIED_NOT_REVIEWED` | (targeted) | canonical unavailable state | tested + V review PASS (visible role=status, not hidden/disabled) | 194bf8485 | 194bf8485 | pending | needs Codex |
| FR-08 | No auto directory enumeration/upload | A3 | app/files/* | none | `[ ] VERIFIED_NOT_REVIEWED` | (targeted) | never bulk-uploads folder | tested + V review PASS (pickContextPaths file-only, no dir enumeration) | 194bf8485 | 194bf8485 | pending | needs Codex |

## Section E — File/Folder Write (OWNER 7-write)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| FW-01 | Real write inside granted root | A3 | app/files/* | **Runtime bridge `.write`** | `[ ] BLOCKED` | E2E | file written + receipt | — | — | — | — | blocker: Runtime/Electron bridge |
| FW-02 | Traversal / symlink-escape prevention | A3+Runtime | consumer asserts rejection | **Runtime enforcement (SIBLING)** | `[ ] BLOCKED` | (targeted) | escape attempts rejected | — | — | — | — | blocker: Runtime enforcement |

## Section F — Approvals (OWNER 8)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AP-01 | Visible approval for destructive/high-risk writes | A3 | app/files/* approval dialog | Runtime `.write requireApproval` | `[ ] BLOCKED` | (targeted) | approval required pre-write | — | — | — | — | blocker: Runtime bridge |

## Section G — Attachment scan states + Chat/Run integration (OWNER 6/10/11)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SC-01 | Truthful states: selected/uploading/scanning/clean/rejected/oversized/quota/unsupported/scanner-unavailable/workspace-denied/revoked/interrupted/capability-unavailable | A3 | store/composer.ts, attachments.tsx, lib/file-ingress.ts | **Gateway file-ingress+scan (SIBLING)** | `[ ] BLOCKED` | (targeted, mocked) | each state renders truthfully | state model + pill rendering impl + tested; real data pending | 194bf8485 | 194bf8485 | pending | blocker: Gateway scan API (real verdicts) |
| SC-02 | Only `clean/ready` reaches Chat | A3 | attachments.tsx `isAttachmentAttachable`, file-ingress `isAttachable` | Gateway `file_id` | `[ ] BLOCKED` | (targeted) | rejected/quarantined never attach | guard impl (clean+fileId) + tested; real clean pending | 194bf8485 | 194bf8485 | pending | blocker: Gateway scan API |
| SC-03 | Only `clean/ready` reaches Agent Run | A3 | shared ChatBar/ComposerScope guard | Gateway `file_id` | `[ ] BLOCKED` | (targeted) | same guarantee on run tile | same guard (shared composer path); real clean pending | 194bf8485 | 194bf8485 | pending | blocker: Gateway scan API |
| SC-04 | Workspace switch clears pending; foreign file/grant unavailable | A3 | app/files/local-files-panel.tsx | canonical workspace id (SIBLING) | `[ ] BLOCKED` | (targeted) | no cross-workspace reuse | clear-on-cwd/profile-change impl + tested; foreign-workspace needs workspaceId | 194bf8485 | 194bf8485 | pending | blocker: canonical workspace id |

## Section H — Accessibility (OWNER 10/11)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AX-01 | Keyboard operation + screen-reader labels on all new controls | A3/A4 | app/files/local-files-panel.tsx | none | `[ ] VERIFIED_NOT_REVIEWED` | (targeted) | roles/labels/focus correct | aria labels + keyboard tested + V review | 194bf8485 | 194bf8485 | pending | needs Codex |
| AX-02 | Every planned option visible; missing setup shows exact requirement (nothing hidden) | A4/Master | all incl. app/files panel mount | none | `[ ] IMPLEMENTED_NOT_VERIFIED` | verifier review | no hidden/inert/disconnected controls | **CORRECTED (Codex Wave 2.3):** the earlier "0 disconnected controls" claim was FALSE — LocalFilesPanel was unmounted (disconnected). Now mounted in the shipped right-sidebar (CX-01). Claim removed; re-verification of the full surface pending independent verifier + Codex | 6b16fa5f2 | 6b16fa5f2 | changes_required | needs verifier re-review + Codex |

## Section I — Security (OWNER 9/11)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SE-01 | No local absolute path in API payload or logs | A3/B | local-files-panel.tsx, file-ingress.ts | none | `[ ] VERIFIED_NOT_REVIEWED` | (targeted) | payload/log has safe label only | Master + Agent B + Agent V audits: `.path` only for local byte-read; gateway gets `safeName`+bytes; grants `safeLabel`; 0 console/localStorage/URL/receipt path leaks; tested | 194bf8485 | 194bf8485 | pending | needs Codex |
| SE-02 | Branding integrity unchanged | B | components/__tests__/branding-integrity.test.ts | none | `[ ] VERIFIED_NOT_REVIEWED` | `vitest run --project ui src/components/__tests__/branding-integrity.test.ts` | brand assets/name unchanged | Wave-2: 5-test lock (youtab-girl.jpg mark + white tile, public asset exists, <title>Youtab</title>, 'Youtab Agent Runtime' string); fails on rebrand | a6825a932 | a6825a932 | pending | needs Codex |
| SE-03 | Secret scan clean; no secret in diff | B/Master | delivery range base..HEAD + evidence/reports | none | `[ ] VERIFIED_NOT_REVIEWED` | official **Gitleaks** `detect --no-git` on `git log -p 20d69ce4..HEAD` + docs/evidence + docs/roadmap; + `git diff --check` | 0 findings | Wave-2.1: **Gitleaks v8.21.2** (ghcr.io/gitleaks/gitleaks:v8.21.2, digest sha256:0e99e88…) scanned every commit diff in the delivery range + generated evidence/reports → **"no leaks found", 0 findings, exit 0**; git diff --check clean. (Supplemental regex scan of 1411 added lines also 0; NOT a dependency scan.) | e42f05845 | e42f05845 | pending | needs Codex |

## Section J — E2E (OWNER 12)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| E2E-01 | Model/Engine UI→API→effect→receipt | A4 | e2e/ | live gateway+creds | `[ ] BLOCKED` | `npm run test:e2e` | real receipt captured | — | — | — | — | blocker: live backend |
| E2E-02 | MCP UI→Runtime→tool receipt | A4 | e2e/ | reachable MCP | `[ ] BLOCKED` | `npm run test:e2e` | real tool receipt | — | — | — | — | blocker: reachable MCP |
| E2E-03 | File/Folder UI→Runtime→read/write receipt | A4 | e2e/ | Runtime bridge | `[ ] BLOCKED` | `npm run test:e2e` | real fs effect receipt | — | — | — | — | blocker: Runtime bridge |
| E2E-04 | Attachment scan UI→Gateway→clean/quarantine receipt | A4 | e2e/ | Gateway scan API | `[ ] BLOCKED` | `npm run test:e2e` | real scan verdict receipt | — | — | — | — | blocker: Gateway scan API |

## Section K — Codex Wave 2.3 corrections (CHANGES REQUIRED response)

| ID | Capability | Agent | Owned files | Dependency | Status | Test cmd | Expected | Actual | Evidence | Commit SHA | Codex | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| CX-01 | Mount LocalFilesPanel in a shipped, visible, navigable surface | Master | app/right-sidebar/index.tsx(+test) | none | `[ ] VERIFIED_NOT_REVIEWED` | `vitest run --project ui src/app/right-sidebar/index.test.tsx` | reachable in real RightSidebarPane, not orphaned | mounted (ErrorBoundary-wrapped) + reachability test asserts `data-slot=local-files-panel-mount` contains the real panel | 6b16fa5f2 | 6b16fa5f2 | pending | live browser-nav E2E still part of blocked E2E gate |
| CX-02 | Remove invented ingress method / ArrayBuffer-over-JSON-RPC → fail-closed | Master | lib/file-ingress.ts(+test), local-files-panel.tsx | **integrated Gateway ingress contract (SIBLING)** | `[ ] VERIFIED_NOT_REVIEWED` | `vitest run --project ui src/lib/file-ingress.test.ts` | no invented method; transmits nothing; scanner_unavailable | removed `file.ingress`+ArrayBuffer JSON-RPC; ingest fail-closed (scanner_unavailable/workspace_denied); AbortSignal threaded; isAttachable=clean+fileId | 6b16fa5f2 | 6b16fa5f2 | pending | real transport/bytes proof BLOCKED on Gateway ingress SHA |
| CX-03 | Canonical workspace authority; no profile/cwd substitution (fail-closed) | Master | lib/workspace-identity.ts(+test), local-files-panel.tsx | **backend workspace-authority (SIBLING)** | `[ ] VERIFIED_NOT_REVIEWED` | `vitest run --project ui src/lib/workspace-identity.test.ts` | resolve returns null; profile change ≠ workspace authority | removed `workspaceId: activeProfile`; resolveCanonicalWorkspaceId()→null; workspaceIdMatches fail-closed; tests prove no substitution | 6b16fa5f2 | 6b16fa5f2 | pending | obtaining the REAL canonical workspace id BLOCKED on backend authority |
| CX-04 | Real byte-for-byte transport to Gateway boundary | Master | file-ingress transport | **integrated Gateway ingress SHA** | `[ ] BLOCKED` | E2E | exact original bytes arrive at real Gateway | fail-closed today; no invention | — | — | — | blocker: Gateway ingress SHA (transport-safe encoding/upload) |
| CX-05 | Clean-file invariant into shared Chat/Run submit path | Master | shared submit seam | **server fileId + canonical workspace (SIBLINGS)** | `[ ] BLOCKED` | (targeted) | only clean+non-empty fileId+matching workspace+no path submits | isAttachable invariant present; wiring into shared submit needs real fileId/workspace (must not break existing file.attach flow) | — | — | — | blocker: Gateway fileId + workspace authority |
| CX-06 | Real cancel through actual transport | Master | ingest transport | **integrated Gateway ingress SHA** | `[ ] BLOCKED` | E2E | cancel leaves no clean artifact/effect | AbortSignal threaded; nothing transmitted today → trivially no artifact | 6b16fa5f2 | — | — | blocker: prove against real transport |
| CX-07 | Server-issued fileId used in real downstream Chat/Run request | Master | downstream request | **Gateway fileId (SIBLING)** | `[ ] BLOCKED` | E2E | downstream carries server fileId | field exists; no disconnected retention beyond truthful state | — | — | — | blocker: Gateway fileId |
| CX-08 | Full official suite 0 failed / 0 errors / exit 0 | Runtime+Master | full suite | Runtime PX correction (integrated) | `[ ] VERIFIED_NOT_REVIEWED` | `npm run test:ui` | 0 failed | **MET:** Runtime PX correction integrated (fedc0c926, test-only from fix/runtime-px-correction-v1 @ 0ca38740); serial ×2 IDENTICAL = **362 files / 3201 passed / 0 failed / exit 0** | fedc0c926 | fedc0c926 | pending | PX correction awaits its OWN Codex; overall gate 0-failed satisfied |
| CX-09 | Live browser E2E (shipped UI→Gateway→scanner→clean/quarantine→Chat/Run) | A4 | e2e/ | **Gateway+Runtime SHAs + OWNER creds** | `[ ] BLOCKED` | `npm run test:e2e` | real end-to-end receipt | not run; no mock/reference-service reported as live E2E | — | — | — | blocker: sibling SHAs + live creds |

---

## Blocking external dependencies (owners named)

1. **Runtime/Electron session** — folder-grant bridge on `window.youtabDesktop` (`folderGrants.request/list/revoke/read/write`) + traversal/symlink enforcement. Blocks FR-04..06, FW-01/02, AP-01, E2E-03.
2. **Codex Gateway session** — file-ingress returning server-issued `file_id`, scan lifecycle (`scanning→clean/quarantined/rejected`), error codes, canonical workspace id. Blocks SC-01..04, E2E-04.
3. **OWNER** — live provider credentials for real inference (ME-05, E2E-01); reachable MCP server (MCP-03, E2E-02); push/PR/merge authorization.
4. **Codex exact-SHA review** — `APPROVED` gate on every row (condition 10). Blocks all `[x] DONE`.

## Verdict

**Current verdict: `NO-GO`.** No row is `[x] DONE`. Final `FRONTEND GO` requires every mandatory row `[x] DONE`.

**Totals after Codex Wave 2.3 + PX integration (45 rows):** DONE **0** · VERIFIED_NOT_REVIEWED **19** · IMPLEMENTED_NOT_VERIFIED **5** (ME-01/02/04, API-02, AX-02) · BLOCKED **21** · NOT_STARTED **0** · IN_PROGRESS 0.
(CX-08 met: Runtime PX correction integrated → full suite serial ×2 = 362 files / 3201 passed / 0 failed / exit 0. Verdict still NO-GO: Gateway ingress, folder-grant bridge, live creds, live E2E, and Codex remain blocking.)
(Codex Wave 2.3 CHANGES REQUIRED added Section K: CX-01/02/03 VERIFIED_NOT_REVIEWED, CX-04..09 BLOCKED; AX-02 downgraded — its "0 disconnected controls" claim was false because LocalFilesPanel was unmounted, now mounted (CX-01). ME-05 stays BLOCKED (durable receipt absent + live inference).)
(Ceilings enforced: static-only ≤ IMPLEMENTED_NOT_VERIFIED; targeted-local-tests ≤ VERIFIED_NOT_REVIEWED; BLOCKED stays until the exact sister SHA is integrated; no row DONE this pass.)

## Pre-existing baseline failures (at untouched base SHA — NOT caused by this work)

Desktop UI suite at base `20d69ce42` (only 2 docs files added, no code touched):
**6 failed / 3127 passed (3133)**, **3 failed / 351 passed (354)** files.

| ID | Failing test file | Failing test | Owner-of-file | Note |
|---|---|---|---|---|
| PX-01 | `src/app/skills/index.test.tsx` | toolset mgmt: renders a switch for each toolset and toggles it off | A2 | pre-existing on SEC-9 base |
| PX-02 | `src/app/skills/index.test.tsx` | toolset mgmt: renders the provider config panel inline for the selected toolset | A2 | pre-existing |
| PX-03 | `src/app/skills/index.test.tsx` | toolset mgmt: renders toolset titles without leading emoji | A2 | pre-existing |
| PX-04 | `src/app/skills/index.test.tsx` | vision explainer deep-links to Settings → Models | A1/A2 | pre-existing |
| PX-05 | `src/app/settings/gateway-settings.test.tsx` | labels local mode as default inheritance for a named profile | A2 | pre-existing |
| PX-06 | `src/app/messaging/index.test.tsx` | hides setup-guide button for a plugin platform with no docs URL | A2 | pre-existing |

**Consequence:** the literal DONE gate condition 6 ("relevant full suite `0 failed / 0 errors`") is **not satisfiable at base** for rows whose relevant suite includes these files, until PX-01..06 are resolved. Measured gate for new work meanwhile = **no NEW failures vs this baseline (6)**.

**OWNER DECISION (2026-09-21):** PX-01..06 **assigned to the active Runtime session** as a mandatory parallel baseline-correction lane. Runtime must register them, preserve exact IDs/output, and fix root causes with **no** delete/weaken/skip/xfail. **Frontend must NOT touch these backend-owned PX files.**

**Accepted Runtime handoff (either is sufficient) — Wave 2.1 correction:** the PX dependency does **not** unconditionally require a code-change "correction SHA". The accepted handoff is EITHER
- `NO_CODE_CHANGE_VERIFIED` — supported by **two official quiescent 0/0 qualification runs** proving the six nodes pass under the official harness with no code change; OR
- a real **correction SHA**, if the official quiescent qualification still fails.

Until one of these is delivered, **PX-01..PX-06 and the complete frontend `0 failed / exit 0` suite gate remain BLOCKED**. The frontend full-suite gate stays NOT-DONE until the accepted handoff is integrated non-destructively and the suite is rerun serially on the **combined exact SHA**. Frontend continues non-overlapping work in parallel.

## Checkpoint log

- 2026-09-21 — checklist created at base `20d69ce4245a51e105c381f811fb8cdb8490401c`; worktree isolated & verified `HEAD == base`; all rows opened. Totals: DONE 0 · BLOCKED 15 · NOT_DONE(other) 14.
- 2026-09-21 — commits `eefaa1e5b` (checklist), `4f95bdd23` (contracts v1.0). Deps installed (npm, exit 0, 1595 pkgs). Baseline desktop UI suite captured: **6 pre-existing failures** (PX-01..06) recorded above. No frontend code changed yet. Verdict: NO-GO.
- 2026-09-21 — **⚠ ATTRIBUTION FLAG (for later additive correction; no history rewrite).** All 5 pre-Wave-2 commits (`eefaa1e5b`, `4f95bdd23`, `fbeaa21c3`, `194bf8485`, `6e1b823af`) carry a `Co-Authored-By: Claude Opus 4.8` trailer (author is correctly Eiman). This conflicts with the repo's 0-attribution convention. History is NOT rewritten; Wave-2 commits (`cf2aedb58`, `a6825a932`, `7b600f60a`, and this doc commit) are **0-attribution**. OWNER to decide any additive correction.
- 2026-09-21 — **Parallel wave 1 delivered.** Agent 3 (File/Folder slice) committed at **`194bf8485`**: new `lib/folder-grants.ts`, `lib/file-ingress.ts`, `app/files/local-files-panel.tsx` (+3 tests); modified `store/composer.ts` (additive uploadState union + fileId), `global.d.ts` (optional folderGrants type), `app/chat/composer/attachments.tsx` (+test), i18n en/zh/types. Real consumers, no mocks/fake-success. **Master-verified gates:** 31/31 targeted tests pass; typecheck exit 0; eslint 0; prettier clean; `git diff --check` clean; full `test:ui` = **6 failed / 3152 passed** = baseline PX only, **0 new failures** (⚠ a concurrent-load run flaked to 11 — re-run isolated = 6; do NOT run the suite alongside other heavy commands). Master audit: no absolute path in payload/log (SE-01). Agent 4 (verifier, read-only) confirmed Model/Engine, providers/connections, MCP, file-attach are **REAL-BACKED with 0 fake/disconnected controls**; folder-grants, server file_id+scan, canonical workspaceId, in-UI inference receipt, and a brand-lock test are genuinely **ABSENT** (BLOCKED-justified, not hidden). Status transitions applied above. **Totals: DONE 0 · VERIFIED_NOT_REVIEWED 1 · IMPLEMENTED_NOT_VERIFIED 17 · BLOCKED 16 · NOT_STARTED 2.** Verdict: **NO-GO**. Next non-blocked: SE-02 branding-lock test, ME-05 inference-receipt UI; then integrate sibling SHAs (folder-grant bridge, Gateway ingress) for the BLOCKED rows.
- 2026-09-21 — **Parallel wave 2 delivered.** 4 non-overlapping agents (A Model/Engine receipt, B branding+secret-scan, C API/MCP integration, V verifier). Commits (0-attrib): `cf2aedb58` ME-05 receipt UI (11 tests), `a6825a932` SE-02 branding lock (5 tests), `7b600f60a` API/MCP integration proofs (22 tests). **Master-verified combined gates:** 38 Wave-2 targeted tests pass; typecheck exit 0; eslint 0; prettier clean; `git diff --check` clean; secret scan 0 findings (regex method, changed range, NOT a dep scan). **Serial-run flakiness resolution (per Agent V):** full `test:ui` run twice SERIALLY, single-process, full output — **Run 1 = 6 failed / 3190 passed (3196); Run 2 = 6 failed / 3190 passed (3196)** — identical collected counts, identical failing-node set = ONLY the 3 PX files (skills/index, gateway-settings, messaging), zero non-PX failures. The earlier "11-failure" run is thereby confirmed a concurrent-load artifact, NOT reproducible serially; its 5 extra nodes were unrecoverable (summary-filtered) so "flaky" is now backed by repeatable serial evidence rather than assertion. Agent V review: no regression/hidden/disabled option, folder+file consumers fail closed, no static-as-live-E2E overclaim (all PASS). **Totals: DONE 0 · VERIFIED_NOT_REVIEWED 16 · IMPLEMENTED_NOT_VERIFIED 5 · BLOCKED 15.** Final `0 failed / exit 0` suite gate remains unmet until the Runtime PX-01..06 correction SHA is integrated. Verdict: **FRONTEND NO-GO** — exact blockers: Runtime folder-grant bridge, Gateway file-ingress SHA, Runtime PX SHA, live provider creds / reachable MCP, Codex exact-SHA APPROVED.
- 2026-09-21 — **Codex Wave 2.3 (CHANGES REQUIRED) additive correction.** Candidate `5d5e4f73b56cd5028c448b0a3e53695b365a180e` preserved (no amend/rewrite). Corrections on descendant `6b16fa5f2…`: (item 1) LocalFilesPanel MOUNTED in the shipped right-sidebar (CX-01) — it was previously unmounted/orphaned, which falsified the AX-02 "0 disconnected controls" claim (now removed/downgraded); (item 2) removed the INVENTED `file.ingress` method + ArrayBuffer-over-JSON-RPC → fail-closed consumer, no invented method/shape/casing (CX-02); (item 3) new `lib/workspace-identity.ts` — canonical workspace id from the real backend authority only, `resolveCanonicalWorkspaceId()`→null, no profile/cwd substitution (CX-03). **SHA reconciliation (db2ba509 vs 5d5e4f73):** `db2ba509a` was the rename commit; `5d5e4f73b` is its descendant adding the v1.2 report (the qualified candidate). Wave 2.2 read-only bundle ran its gates directly on `5d5e4f73b` (targeted 90/0, serial 6-PX/3191, Gitleaks 0). This Codex pass binds every new command/artifact to the NEW final SHA (the descendant of `6b16fa5f2` that adds the v1.3 report). **All SHAs are on the clean delivery chain** `delivery/runtime-frontend-owner-1-12` (base `20d69ce4…`), 0 forbidden attribution. Verdict: **FRONTEND NO-GO** — CX-04..09, integration gates, 0-failed suite (PX handoff), and live browser E2E remain BLOCKED on undelivered Gateway/Runtime SHAs + OWNER creds + Codex.
