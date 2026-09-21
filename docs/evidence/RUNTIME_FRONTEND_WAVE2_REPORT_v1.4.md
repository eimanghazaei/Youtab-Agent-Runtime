# Runtime Frontend — Wave 2.4 Report v1.4 (supersedes v1.3)

SESSION 2/4 · Frontend Master Integrator. Additive corrections on the existing
clean delivery branch `delivery/runtime-frontend-owner-1-12-v2` (base
`20d69ce4245a51e105c381f811fb8cdb8490401c`) in response to the exact-SHA review
CHANGES REQUIRED on `bb0d8c9130b45cacee1ae3c89d4a2763ea20b6a8`. That candidate and
its bundle are preserved unchanged; no amend/rebase/squash/replay was performed.

## 1. SHAs

- **Base:** `20d69ce4245a51e105c381f811fb8cdb8490401c`
- **Reviewed candidate (preserved):** `bb0d8c9130b45cacee1ae3c89d4a2763ea20b6a8`
- **Pre-v1.4 head:** `fc92f2681cd68ab23aec472d43c0f96774e2489f`
- **Final SHA (incl. this v1.4 report):** delivery branch HEAD after this commit (provided in the hand-off; a document cannot embed its own hash). All prior commit SHAs are on the clean chain (see checklist v1.1 mapping).

## 2. Additive commits this pass (0 attribution, author+committer = Eiman)

| SHA | Commit |
|---|---|
| `93b0c3e0f60170e2cb03d17d64187d7a753fab29` | fix: strict attachability invariant + enforce in submit + truthful file-ingest gate |
| `fc92f2681cd68ab23aec472d43c0f96774e2489f` | fix: normalize CRLF→LF in right-sidebar test + strip trailing whitespace (range diff-check) |
| (this commit) | docs: Wave 2.4 report v1.4 + contracts v1.1 + checklist v1.1 |

## 3. Mandatory corrections

- **Item 1 — fail-open bypass CLOSED (CX-10):** `isAttachmentAttachable` moved to a pure module `app/chat/composer/attachment-invariant.ts`. Legacy (undefined state) is accepted ONLY with a completed `attachedSessionId`, OR as a pure in-app context ref (resolvable `refText`, no local `path`); a local-path-only unstaged attachment is rejected. Scan-managed is accepted ONLY when `clean` + non-empty server `fileId` + non-empty issued `workspaceId` + canonical-workspace match (fail-closed today, since the authority is unapproved). Adversarial tests: undefined-no-session, forged-clean, empty-fileId, no/empty/mismatched workspace, local-path-only, path-leak ref, every non-clean lifecycle state, payload mutation.
- **Item 2 — enforced in the REAL submit path (CX-11):** `use-prompt-actions/submit.ts` filters synced attachments through the invariant before building `attachmentRefs` and the context text — the single seam shared by Chat and Agent-Run tiles, so neither can bypass the other. Rejected/pending/quarantined and local-path-only never enter the payload; only server-issued refs/fileIds and completed legacy refs pass. Existing `file.attach` flow preserved (verified: `use-prompt-actions/index.test.tsx` green incl. the path-less `@file:` ref pass-through).
- **Item 3 — workspace stub (CX-03):** unchanged fail-closed; the REAL authority contract (`GET /v1/agents/workspace/active` → `{workspace_id}`) is an **unapproved** candidate (`feat/gateway-file-ingress` @ `1be59d10`). Not integrated. BLOCKED.
- **Item 4 — Gateway ingress:** NOT restored to `file.ingress`. Real contract recorded in contracts v1.1 (`POST /v1/files/upload`, multipart, snake_case `file_id`/`workspace_id`, error codes) from the **unapproved** candidate `1be59d10`. Not integrated. BLOCKED (CX-04/05/07, E2E-04).
- **Item 5 — panel connection + no doomed control (CX-12):** the file Add/drop affordance is gated behind `isFileIngestAvailable() && isWorkspaceAuthorityAvailable()`; when unavailable (today) it shows a truthful "Secure file scanning unavailable" state instead of a clickable Add that always returns `workspace_denied`. Full connection so a *clean* file flows into Chat/Run is BLOCKED on the approved ingress + authority (the submit-side invariant guard is already in place, CX-11).
- **Item 6 — folder-grant bridge:** ABSENT (TS interface only; no `apps/desktop/electron/**` impl on any branch). BLOCKED. Folder authority must live in Runtime/Electron, never the renderer.
- **Item 7 — evidence:** contracts v1.1 + checklist v1.1 + this v1.4 created as successors (v1.0/v1.1–v1.3 preserved). Historical SHAs (`194bf8485`, `6b16fa5f2`, `fedc0c926`, …) appear only in the checklist-v1.1 mapping table; active rows use clean-chain SHAs. Stale cwd/profile-workspace claim removed; "clean+fileId already enforced" corrected to VERIFIED_NOT_REVIEWED; cancellation described as local-only.
- **Item 8 — quality debt:** classified (see §5).

## 4. Verification (direct on the final SHA chain)

| Gate | Exit | Result |
|---|---|---|
| targeted adversarial + unit (attachment-invariant, submit-path, panel, right-sidebar, model-receipt, API/MCP, branding, folder/ingress) | 0 | green (adversarial invariant matrix + submit enforcement + truthful-unavailable) |
| typecheck (`tsc ×3`) | 0 | clean |
| ESLint — changed files | 0 | **0 errors, 0 warnings in this program's changed files** |
| Prettier | 0 | clean |
| range-bound `git diff --check 20d69ce4..HEAD` | 0 | **CLEAN** (after CRLF/whitespace fix) |
| official Gitleaks v8.21.2 over base..final range + evidence | 0 | 0 findings (see bundle) |
| attribution scan (base..HEAD `%B`) | — | **0 forbidden** |
| **`npm run test:ui` serial run 1** | 0 | **363 files / 3222 passed / 0 failed / 0 errors** |
| **`npm run test:ui` serial run 2** | 0 | **363 files / 3222 passed / 0 failed / 0 errors** (identical collection) |
| `npm audit` (dependency audit) | **1** | **17 vulnerabilities (8 high, 8 moderate, 1 low)** — pre-existing; the Runtime dep-CVE remediation chain is unapproved/OPEN. **Mandatory blocker.** Not duplicated/fixed in the frontend. |
| headless-browser E2E (real Gateway/Runtime/bridge) | — | **NOT RUN — BLOCKED** (no approved Gateway ingress / workspace authority / folder-grant bridge / live creds). No mock/reference-service is reported as live E2E. |

## 5. Item 8 — quality-debt classification

- **75 ESLint warnings** = 68 `no-restricted-globals` + 6 `react-hooks/exhaustive-deps` + 1, **all pre-existing in files that are NOT part of this local-file feature; 0 in this program's changed files.** Resolving them is a separate broad refactor across ~20 non-feature files (violates "keep unrelated changes out of a change"). The `0-warnings` final gate is therefore a **repo-wide pre-existing-debt blocker (QD-01)**.
- **~41 stderr sections** in the passing suite = intentional negative-path diagnostics from EXISTING components (`[submit-drift-abort]` console.warn, RPC not-connected fallback, React key warnings) exercised by pre-existing tests, not by this program's new tests. A full per-node "expected stderr" assertion audit spans pre-existing tests outside this feature (QD-03).
- **Range-bound whitespace gate (QD-02):** CLEAN after normalizing a CRLF test file + one doc line (`fc92f2681`).

## 6. Checklist totals (checklist v1.1)

DONE **0** · VERIFIED_NOT_REVIEWED **24** · IMPLEMENTED_NOT_VERIFIED **5** · BLOCKED **19**. No row DONE.

## 7. Remaining blockers (exact)

1. Approved Gateway **file-ingress** SHA (candidate `feat/gateway-file-ingress` @ `1be59d10`, NOT on main) — CX-04/05/07, SC-01..04, E2E-04.
2. Approved **Workspace Authority** SHA (same candidate `1be59d10`) — CX-03 real id, SC-04.
3. Runtime/Electron **folder-grant** implementation — ABSENT — FR-04/05/06, FW-01/02, AP-01, CX-06, E2E-03.
4. Approved **Runtime dependency-CVE remediation** — ABSENT/OPEN — `npm audit` 17 vulns, mandatory for GO.
5. **Live browser E2E** against real Gateway/Runtime + live provider creds / reachable MCP — CX-09, E2E-01..04, ME-05, MCP-03.
6. Repo-wide **pre-existing ESLint/stderr debt** (QD-01/QD-03) for the 0-warnings gate.
7. **Exact-SHA review APPROVED** on the final candidate — condition 10 of every DONE.

## 8. Verdict

**FRONTEND NO-GO.** The strict attachability invariant is closed and enforced on the real submit path, the doomed Add control is replaced with a truthful state, the range-bound whitespace gate is clean, the full suite is 0-failed serial ×2, attribution is clean, and secret-scan is clean — but multiple mandatory dependencies (approved Gateway ingress + Workspace Authority, Runtime folder-grant impl, approved dep-CVE remediation, live E2E) are undelivered/unapproved and the repo-wide lint/stderr debt is unresolved. No push, PR, merge, deploy, or unapproved-candidate integration performed. No row DONE.
