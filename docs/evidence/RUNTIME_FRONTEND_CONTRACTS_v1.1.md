# Runtime Frontend — Canonical Contracts v1.1 (supersedes v1.0)

Published by SESSION 2/4 (Frontend Master Integrator). This version records the
**real sibling contracts** discovered by inspection, their exact **unapproved
candidate SHAs**, and the precise BLOCKED status of each. No contract is invented.
Nothing is integrated until the sibling's implementation is **approved** (on
`main` / tagged) and passes integrated E2E; the current candidates are NOT approved
(`main` tip = `c7650a1b920224283ba3a59ca054d6625e07a5f3`), so all remain BLOCKED.

## 1. Gateway file-ingress — REAL candidate exists, NOT APPROVED → BLOCKED

- **Candidate branch/SHA (NOT on main, NOT approved):** `feat/gateway-file-ingress` @ `1be59d107d49513e900191b2789539a754768b02`.
- **Real contract (to integrate ONLY once approved):**
  - `POST /v1/files/upload`, multipart field `uploaded_file` (list, max 8).
  - Success JSON (**snake_case**): `{ "success": true, "file_id": "<uuid>", "filename", "size_bytes", "workspace_id" }`.
  - Lifecycle columns `state` / `scan_status`: `quarantined/unscanned` → `clean/clean` (synchronous ClamAV INSTREAM scan; there is no async "scanning" state).
  - Error codes: `413 file_too_large`, `413 storage_quota_exceeded`, `422 invalid_file_count | empty_file | malware_detected | <validation:unsupported>`, `503 file_scanner_unavailable`.
  - Also `GET /v1/files`, `GET/PATCH/DELETE /v1/files/{id}`, `POST /v1/files/{id}/reconcile`, `POST /v1/files/{id}/approve-rag`. Traversal/symlink hardening via `_require_under` + `commonpath`.
- **Frontend stance now:** the `file-ingress` consumer is **fail-closed** (transmits nothing, `scanner_unavailable`). It does NOT restore `file.ingress` or ArrayBuffer-over-JSON-RPC. It will be wired to `POST /v1/files/upload` (multipart, snake_case) only against the **approved** Gateway SHA. **BLOCKED** (SC-01..04, CX-04/05/07, E2E-04).

## 2. Workspace Authority — REAL candidate exists, NOT APPROVED → BLOCKED

- **Candidate branch/SHA (same as §1, NOT on main):** `feat/gateway-file-ingress` @ `1be59d10…`.
- **Real contract:** `require_workspace_identity` dependency → `GET /v1/agents/workspace/active` → `{ "workspace_id": "<canonical opaque>" }`; `POST /v1/agents/workspace/select` (validated through Office authority); `POST /v1/files/{id}/reconcile` uses `resolve_requested_workspace`.
- **Frontend stance now:** `lib/workspace-identity.ts` `resolveCanonicalWorkspaceId()` returns `null` (fail-closed; never a profile/cwd/free-text substitute). It will call `GET /v1/agents/workspace/active` only against the **approved** SHA. **BLOCKED** (CX-03 real id, SC-04).

## 3. Runtime/Electron folder-grant bridge — ABSENT → BLOCKED

- **No real implementation exists** on any branch. Only a TypeScript optional interface (`YoutabFolderGrantsBridge` in `apps/desktop/src/global.d.ts`) + the renderer consumer (`apps/desktop/src/lib/folder-grants.ts`). `apps/desktop/electron/**` exposes no `folderGrants` (0 matches across revisions). A TS-only interface is NOT completion.
- **Frontend stance now:** consumer fails closed (`FolderGrantsUnavailableError` → "Local Runtime required"). Folder authority must live in Runtime/Electron, never the renderer. **BLOCKED** (FR-04/05/06, FW-01/02, AP-01, E2E-03, CX-06).

## 4. Runtime dependency-vulnerability remediation — ABSENT as approved → BLOCKED

- Documented plan only, all OPEN: `RUNTIME_DEPENDENCY_CVE_LEDGER_v1.1.md` (17 packages / 40 GHSA; 8 high, 8 moderate, 1 low; `npm audit` exit 1 keeps Runtime NO-GO) + `RUNTIME_DEPENDENCY_CVE_REMEDIATION_ROADMAP_v1.1.md` (PR-01..05 proposed, not landed). Dependabot candidate branches exist (anyio, colord, fast-uri, joi, js-yaml, svgo…), none merged.
- **Frontend stance:** consume the **approved** result when it lands; do not duplicate/ignore. **BLOCKED** (mandatory for overall GO).

## 5. External / OWNER dependencies

- Live provider credentials for a real inference receipt (ME-05 / E2E-01).
- A reachable MCP server for a real tool-call receipt (MCP-03 / E2E-02).
- Exact-SHA review `APPROVED` — condition 10 of every DONE.
- OWNER authorization for any push / PR / merge / deploy.

## Integration definition of done

A capability flips from BLOCKED only when the sibling's **approved** endpoint/bridge
is connected and the `UI → API/Runtime → effect → receipt` E2E passes on the exact
frontend SHA, the independent verifier accepts, and the exact-SHA review returns
`APPROVED`. Until then: truthful capability-unavailable state, row BLOCKED, verdict
`NO-GO`. A NO-GO / unapproved candidate is never integrated.
