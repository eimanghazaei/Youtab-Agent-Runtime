# Runtime Frontend — Canonical Contracts to Sibling Sessions v1.0

> Published by SESSION 2/4 (Frontend Master Integrator) at base
> `20d69ce4245a51e105c381f811fb8cdb8490401c`. These are the **exact** typed
> consumers the frontend implements now and will integrate against the **real**
> endpoints/bridge delivered by the Runtime and Gateway sessions. No mocks or
> fake-success handlers ship in production; until a dependency lands, the
> corresponding UI shows a truthful "setup required / capability unavailable"
> state and its checklist row stays `BLOCKED`.

## 1. Runtime / Desktop folder-grant bridge — owner: Runtime/Electron session

Frontend declares this on `window.youtabDesktop` (**type only** in
`apps/desktop/src/global.d.ts`; the implementation lives in
`apps/desktop/electron/**`, which is out of the frontend scope). The renderer
consumes it through new wrappers in `apps/desktop/src/lib/desktop-fs.ts`.

```ts
interface YoutabFolderGrant {
  grantId: string            // opaque, stable per grant
  safeLabel: string          // user-facing display label — NEVER the absolute path
  permission: 'read-only' | 'read-write'
  status: 'active' | 'revoked' | 'expired'
  workspaceId: string        // canonical workspace this grant is bound to (see §2)
  createdAt: string          // ISO-8601
}

interface YoutabFolderWriteOptions {
  requireApproval?: boolean  // high-risk write → renderer shows approval dialog first
}

interface YoutabFolderGrantsBridge {
  request(options: { readOnly: boolean }): Promise<YoutabFolderGrant>   // MUST be user-gesture initiated; MUST NOT enumerate before approval
  list(): Promise<YoutabFolderGrant[]>
  revoke(grantId: string): Promise<{ revoked: true }>                    // access must be rejected immediately after
  read(grantId: string, relPath: string): Promise<{ bytes: ArrayBuffer }>
  write(grantId: string, relPath: string, bytes: ArrayBuffer, options?: YoutabFolderWriteOptions): Promise<{ written: true; receiptId: string }>
}
// on window.youtabDesktop:  folderGrants?: YoutabFolderGrantsBridge
```

**Runtime obligations (enforced server-side, NOT in the renderer):**
- reject path traversal (`..`) and symlink escape outside the granted root;
- never return or log the absolute path to the renderer beyond `safeLabel`;
- bind every grant to a `workspaceId`; reject use from another workspace;
- `read`/`write` operate strictly inside the granted root;
- `write` with `requireApproval` must fail closed if approval was not obtained.

**Frontend consumer state when `window.youtabDesktop.folderGrants` is absent:**
canonical "Local Runtime required" unavailable state (visible, not hidden).

## 2. Gateway file-ingress + scan — owner: Codex Gateway session

The frontend needs a **server-issued `file_id`** and a truthful scan lifecycle.
Preferred transport: gateway RPC via `requestGateway` (mirrors existing
`file.attach`); REST `window.youtabDesktop.api({ path:'/api/files/ingress', upload })`
is acceptable if the ingress is REST.

```ts
type FileScanState =
  | 'selected' | 'uploading' | 'scanning'
  | 'clean'        // == ready; ONLY this state may attach to Chat/Run
  | 'quarantined' | 'rejected'
  | 'oversized' | 'quota_exceeded' | 'unsupported_type'
  | 'scanner_unavailable' | 'workspace_denied' | 'interrupted'

interface FileIngressResult {
  fileId: string            // server-issued; the ONLY reference sent onward
  workspaceId: string       // canonical workspace binding
  state: FileScanState
  safeName: string          // display name; no absolute path
  sizeBytes: number
  reasonCode?: string       // machine code for the non-clean states below
}
```

**Canonical Gateway error/reason codes** (surfaced through
`apps/desktop/src/store/notifications.ts` `ERROR_SUMMARIES`, mapped to i18n):
`file_too_large`, `quota_exceeded`, `unsupported_type`, `scanner_unavailable`,
`workspace_denied`, `quarantined`.

**Canonical workspace identifier:** today the frontend uses `$currentCwd` (a
path) + `$activeGatewayProfile`. The Gateway must issue a real, opaque
`workspaceId` (never free-text) that scopes files and grants; the frontend will
send that identifier and refuse cross-workspace reuse.

**Frontend guarantees regardless of transport:**
- only `state === 'clean'` files are attachable to Chat and Agent Run;
- only `fileId` (never a local path) is sent in the submit payload;
- a workspace/identity change clears pending attachments and renders
  foreign-workspace files/grants as unavailable.

## 3. External / OWNER dependencies

- Live provider credentials for a real inference receipt (ME-05 / E2E-01).
- A reachable MCP server for a real tool-call receipt (MCP-03 / E2E-02).
- Codex exact-SHA `APPROVED` review — condition (10) of the DONE gate.
- OWNER authorization for any push / PR / merge / deploy.

## Integration definition of done

A capability flips from `BLOCKED` only when the sibling's real endpoint/bridge
is connected and the `UI → API/Runtime → effect → receipt` E2E passes on the
exact frontend SHA, the independent verifier accepts, and Codex returns
`APPROVED`. Until then: truthful setup state, row `BLOCKED`, verdict `NO-GO`.
