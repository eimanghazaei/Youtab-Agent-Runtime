# Runtime Frontend — Wave 2 Report v1.0

SESSION 2/4 · Frontend Master Integrator · OWNER Requirements 1–12.
Branch `feat/runtime-frontend-owner-1-12` · Base `20d69ce4245a51e105c381f811fb8cdb8490401c`.

## 1. Final full SHA

`bdb3053e0d746e869ebe12ba9dae807f57fb3d0f` (branch head after Wave 2).

## 2. Exact commits and changed files

| SHA | Attrib | Summary | Changed files |
|---|---|---|---|
| `eefaa1e5b` | ⚠ co-author trailer | checklist v1.0 | docs/roadmap/…CHECKLIST_v1.0.md |
| `4f95bdd23` | ⚠ co-author trailer | contracts v1.0 | docs/evidence/…CONTRACTS_v1.0.md |
| `fbeaa21c3` | ⚠ co-author trailer | baseline failures record | docs/roadmap/…CHECKLIST_v1.0.md |
| `194bf8485` | ⚠ co-author trailer | File/Folder slice | lib/folder-grants.ts(+test), lib/file-ingress.ts(+test), app/files/local-files-panel.tsx(+test), store/composer.ts, global.d.ts, app/chat/composer/attachments.tsx(+test), i18n en/types/zh |
| `6e1b823af` | ⚠ co-author trailer | checklist wave-1 | docs/roadmap/…CHECKLIST_v1.0.md |
| `cf2aedb58` | 0-attrib | ME-05 receipt UI | app/model-receipt/{use-inference-receipt.ts, inference-receipt.tsx, inference-receipt.test.tsx}, i18n en/types/zh |
| `a6825a932` | 0-attrib | SE-02 branding lock | components/__tests__/branding-integrity.test.ts |
| `7b600f60a` | 0-attrib | API/MCP integration proofs | app/settings/__tests__/api-connection.integration.test.ts, app/skills/__tests__/mcp-consumers.integration.test.ts |
| `bdb3053e0` | 0-attrib | checklist wave-2 | docs/roadmap/…CHECKLIST_v1.0.md |

**Attribution flag (per Wave-2 instruction):** the 5 pre-Wave-2 commits carry `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` (author correctly Eiman). This conflicts with the repo 0-attribution convention. History is **not** rewritten; Wave-2 commits are 0-attribution. Recommended additive correction (OWNER's call): a documented note or `.mailmap`, not a rebase.

## 3. All 36 checklist statuses (after Wave 2)

**Totals: DONE 0 · VERIFIED_NOT_REVIEWED 16 · IMPLEMENTED_NOT_VERIFIED 5 · BLOCKED 15 · NOT_STARTED 0.**

- **VERIFIED_NOT_REVIEWED (16):** ME-03, API-01, API-03, MCP-01, MCP-02, MCP-04, FR-01, FR-02, FR-03, FR-07, FR-08, AX-01, AX-02, SE-01, SE-02, SE-03.
- **IMPLEMENTED_NOT_VERIFIED (5):** ME-01, ME-02, ME-04 (static REAL-BACKED only), ME-05 (receipt UI built+tested; mount + live creds pending), API-02 (static).
- **BLOCKED (15):** MCP-03 (reachable server), FR-04/05/06 (Runtime folder-grant bridge), FW-01/02 (Runtime write + traversal/symlink enforcement), AP-01 (Runtime approval), SC-01/02/03/04 (Gateway ingress + scan + canonical workspaceId), E2E-01/02/03/04 (live sisters).
- **DONE (0):** none — no row may be DONE this pass; DONE needs all 10 conditions incl. real E2E + Codex APPROVED.

Ceilings enforced: static-only ≤ IMPLEMENTED_NOT_VERIFIED; targeted-local-tests ≤ VERIFIED_NOT_REVIEWED; BLOCKED holds until the exact sister SHA is integrated.

## 4. Model/Engine / API / MCP proof

**Model/Engine.** ME-01/02/04 statically REAL-BACKED (Agent 4 citations: setModelAssignment→POST /api/model/set; saveYoutabConfig→PUT /api/config; requestModelOptions gateway-first). ME-03 now executable: `validateCustomEndpoint` emits `POST /api/providers/custom-endpoints/validate`, `reachable=false` surfaced truthfully. ME-05: `InferenceReceiptCard` + `useInferenceReceipt` built from REAL stores (`$currentModel`, `$currentProvider`, `$activeSessionId`, `$currentUsage` incl. input/output/total/cost_usd from real `message.complete`); server request-id / custom endpoint / latency are **absent from the real response** and shown as "not reported by backend" — never fabricated. 11 tests (success / invalid-credential / unavailable-provider / failed / pending / empty). **Remaining:** mount into a visible surface + live provider creds for a live receipt.

**API connections.** Executable integration (10 tests) drive the real `youtab.ts` consumers through a recording reference service bound only to `window.youtabDesktop.api` (consumers run unmodified): `getCustomEndpoints`/`saveCustomEndpoint`/`validateCustomEndpoint`/`activateCustomEndpoint`/`deleteCustomEndpoint` → correct `/api/providers/custom-endpoints*` shapes; blank base_url + unknown id reject visibly, nothing persisted.

**MCP.** Executable integration (12 tests): `saveMcpServers`→PUT `/api/mcp/servers` (stdio-without-command + remote-without-url reject, nothing persisted); `listMcpServers`, `testMcpServer`→POST `.../test` (unknown → truthful `ok:false`+error), `authMcpServer`+`getMcpOAuthFlow`, `getMcpCatalog`, `installMcpCatalogEntry` (missing required env rejects; disconnected `enable:true` creates no live server); tool gating `isToolEnabled`/`toggleToolInServer`.

**MCP-03 live tool receipt** stays BLOCKED (needs a reachable MCP server) — the reference-service proof does not substitute for a live handshake.

## 5. Security and secret-scan results

- **Secret scan (SE-03):** `git diff 20d69ce4245a51e105c381f811fb8cdb8490401c..HEAD -- apps/desktop/src`, 1411 added lines scanned for `sk-`, `ghp_/gho_/github_pat_`, `AKIA…`+secret_access_key, `AIza…`, PEM private keys, `bearer …`, Slack `xox…`, JWT, credentialed `password/api_key/secret/token`, and credentialed connection strings. **0 findings.** Method: rigorous regex scan (gitleaks/trufflehog not on PATH — stated explicitly; NOT a dependency/CVE scan). `git diff --check` clean.
- **Path/credential leakage (SE-01):** Master + Agent B + Agent V audits agree — `attachment.path` is used ONLY for a local byte read (`readDesktopFileDataUrl`); the gateway payload carries `safeName` + bytes + `workspace_id` only; folder grants expose `safeLabel` only; 0 `console.*`/`localStorage`/`sessionStorage`/URL/receipt path leaks in the Wave-1 files.
- **Branding integrity (SE-02):** 5-test static lock — BrandMark → canonical `youtab-girl.jpg` on the white brand tile, `public/youtab-girl.jpg` exists, `<title>Youtab</title>`, `Youtab Agent Runtime` product string. Fails if the brand name/asset is altered. No branding changed.
- **Fail-closed (Agent V):** folder-grants throws `FolderGrantsUnavailableError` when the bridge is absent (never fabricates a grant); file-ingress → `scanner_unavailable` on missing method, `rejected` for clean-without-fileId; `isAttachable` requires `clean` + server `fileId`.

## 6. Analysis of the 11-failure run

The earlier concurrent-load run (`b38eb6fmm.output`) reported **11 failed / 8 files** but was summary-filtered — it contains only aggregate counts, so the identity of the 5 nodes beyond the 6 PX nodes is **unrecoverable** from disk (confirmed by Agent V). Rather than accept "flaky" on faith, Wave-2 produced repeatable serial evidence (Agent V's procedure): the full suite was run **twice, serially, single-process, full output**:

- **Run 1:** `Test Files 3 failed | 358 passed (361)` · `Tests 6 failed | 3190 passed (3196)` · failing files = ONLY `skills/index.test.tsx`, `settings/gateway-settings.test.tsx`, `messaging/index.test.tsx`; zero non-PX failures.
- **Run 2:** identical — `6 failed | 3190 passed (3196)`, same 3 PX files, zero non-PX failures.

Identical collected counts and identical failing-node sets across two serial runs confirm the suite is **stable at exactly the 6 PX failures**; the 11-failure run was a concurrent-load artifact (I had run `test:ui` alongside `typecheck`/`eslint`). Lesson recorded: never run the full suite alongside other heavy commands. Exit code was 1 in both serial runs solely because of the 6 pre-existing PX failures.

## 7. Sister SHA integration status

None integrated yet (all pending):
- **Runtime PX-01..06 correction SHA** — not received. Frontend `0 failed / exit 0` final suite gate cannot be met until integrated on a combined SHA. On arrival: verify ancestry + changed files, integrate non-destructively, rerun the 6 PX nodes, rerun the full suite serially twice.
- **Gateway file-ingress SHA** — not received; its current candidate is NO-GO and MUST NOT be integrated. On a corrected SHA: verify `file_id`, scan/quarantine lifecycle, workspace binding, receipts; connect the typed `file-ingress` consumer; run real Gateway integration tests. Blocks SC-01..04, E2E-04.
- **Runtime/Electron folder-grant SHA** — not received. On arrival: connect grant/revoke/read/write; prove workspace ownership, traversal + symlink-escape rejection, revoked-grant read/write rejection. Blocks FR-04/05/06, FW-01/02, AP-01, E2E-03.
- **OWNER** — live provider creds (ME-05/E2E-01), reachable MCP server (MCP-03/E2E-02), push/PR authorization.
- **Codex** — exact-SHA APPROVED (condition 10 of every DONE).

## 8. Exact test commands, exit codes and counts

| Command (in `apps/desktop`) | Result |
|---|---|
| `npx vitest run --project ui <4 Wave-2 test files>` | 4 files, **38 passed / 0 failed**, exit 0 |
| `npm run typecheck` (tsc ×3) | exit **0** |
| `npx eslint <Wave-2 changed files>` | **0 errors**, exit 0 |
| `npx prettier --check <Wave-2 changed files>` | clean, exit 0 |
| secret scan (regex, base..HEAD) | **0 findings** |
| `git diff --check` | clean |
| `npm run test:ui` serial Run 1 | **6 failed / 3190 passed (3196)**, exit 1 (PX only) |
| `npm run test:ui` serial Run 2 | **6 failed / 3190 passed (3196)**, exit 1 (PX only) |

Wave-1 targeted (prior): 31 passed. No retry used to manufacture green. No new skip/xfail introduced.

## 9. Final clean-worktree proof

After all Wave-2 commits: `git status --porcelain=v1` → no tracked changes; worktree clean. Branch head `bdb3053e0`. Active Runtime session worktree untouched.

## 10. Verdict

**FRONTEND NO-GO.**

Exact blockers preventing `FRONTEND READY FOR CODEX EXACT-SHA REVIEW`:
1. Runtime/Electron folder-grant bridge SHA (FR-04/05/06, FW-01/02, AP-01, E2E-03).
2. Corrected Gateway file-ingress SHA — server `file_id` + scan/quarantine + canonical `workspaceId` (SC-01..04, E2E-04). Current Gateway candidate is NO-GO; do not integrate.
3. Runtime PX-01..06 correction SHA — required for the `0 failed / exit 0` full-suite gate.
4. OWNER: live provider credentials (ME-05/E2E-01), reachable MCP server (MCP-03/E2E-02).
5. Codex exact-SHA `APPROVED` (condition 10 of every DONE row).
6. ME-05 mount into a visible surface (Master integration step).

All independent, non-blocked frontend work for Wave 2 is complete, real, and verified to VERIFIED_NOT_REVIEWED where targeted tests + independent review allow; no row is DONE; no mocks, fake-success, disconnected-enabled, or hidden controls were introduced. No push, PR, merge, deployment, or production activation performed.
