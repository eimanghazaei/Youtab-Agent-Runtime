# Wave 2.5 Corrected Addendum v1.0 (additive; supersedes the Wave 2.4 addendum)

Status: **PENDING CODEX REVIEW — NOT APPROVED. Verdict: FRONTEND NO-GO.**
Additive successor commits on the same clean branch
`delivery/runtime-frontend-owner-1-12-v2`. The Wave 2.4 candidate
`c3cfa92b00b285e71777a718e5b4e395cabe8290` is preserved as historical evidence; no
amend/rebase/squash/replay was performed.

## 1. Fail-open exception removed (review item 1)

`scanState === undefined` no longer becomes attachable because of a client-side
`attachedSessionId`. `isAttachmentAttachable`
(`app/chat/composer/attachment-invariant.ts`) now accepts a legacy attachment ONLY
when it carries an **authenticated server-issued binding**:
- a non-empty `refText` — issued by the `file.attach` response (which requires
  `attached === true` + `ref_text`), or a gateway-resolvable in-app `@file:` ref; OR
- an inline image (`kind === 'image'` + `data:` `previewUrl` — bytes staged via
  `image.attach[_bytes]`).

A **client-controlled id is never authority**: a forged `attachedSessionId`, a
local-path-only attachment, a client-mutated clean/file/workspace state, a
mismatched/absent workspace, and every non-clean lifecycle state are rejected
(adversarial tests in `attachment-invariant.test.ts`). Enforced on the single
shared submit seam (`use-prompt-actions/submit.ts`) for Chat and Agent Run.

## 2. Static capability flags replaced with a real runtime signal (items 2/4)

The static `isFileIngestAvailable()`/`isWorkspaceAuthorityAvailable()` constants
were **removed**. Capability state now comes from a REAL authenticated Gateway
runtime signal via `app/files/use-file-capability.ts` (`useFileCapability`), which
reuses the smallest existing route:
- gated on the live `$gatewayState` handshake (`'open'` = real authenticated socket);
- probes `GET /api/status` (`getStatus`) via react-query;
- maps: idle → `loading`; not connected → `unavailable`; query loading → `loading`;
  query error → `error`; status advertises the capability → `available`; otherwise
  → `unavailable` — **all derived from the real response, never a constant.**

Its own test (`use-file-capability.test.ts`) proves the derivation for idle,
disconnected, advertised (`features.file_ingress && features.workspace_authority`),
absent, single-flag, and error.

## 3. Control stays visible + minimal Gateway capability contract (items 3/4)

The Add-files control and drop zone stay **visible at all times**; they are
**disabled** with the exact dependency status + remediation while
loading/unavailable/error (`data-slot="file-ingest-status"`,
`data-capability-state`), and **activate in place** (same DOM) once available — the
UI is never replaced.

No capability endpoint is fabricated. The **minimal Gateway response required** (to
be delivered by the Gateway owner) is: `GET /api/status` should include a
`features` object advertising booleans:

```jsonc
// StatusResponse addition:
"features": { "file_ingress": true, "workspace_authority": true }
```

Until the approved Gateway advertises these, `useFileCapability` truthfully
resolves to `unavailable` from the real status response.

## 4. Corrected Gateway dependency mapping (item 6) — do NOT integrate (unapproved)

- **Gateway source candidate:** `5064b54a913f428bd61917adcf63830b9f92577d`
- **Gateway evidence HEAD:** `34a3e72624c6adf7a66c34c937a9d16021958147`
- The earlier `1be59d10…` is stale and superseded. The Gateway file-ingress
  (`POST /v1/files/upload`, snake_case `file_id`/`workspace_id`) and Workspace
  Authority (`GET /v1/agents/workspace/active`) contracts still apply but bind to
  the candidate above. **NOT approved (not on `main` `c7650a1b…`) → BLOCKED, not
  integrated.**

## 5. Scope boundary (item 7)

Runtime-wide act-warning / lint / dependency-CVE remediation is owned by the Agent
Runtime session and is **not** touched. The frontend records `npm audit` = 17
vulnerabilities (mandatory blocker) but does not duplicate/remediate it.

## 6. Remaining blockers → FRONTEND NO-GO

Approved Gateway file-ingress + Workspace Authority SHA (`5064b54a9`, unapproved),
Runtime/Electron folder-grant implementation (absent), approved Runtime dep-CVE
remediation (17 open vulns), live cross-repository browser E2E + live creds, and the
exact-SHA review APPROVED gate. No File/Folder security closure or product readiness
is claimed without live cross-repo E2E. No row DONE.
