# Wave 2.4 Evidence Addendum v1.0 (additive correction; no code change)

Status: **PENDING EXACT-SHA REVIEW — NOT APPROVED.**
Frozen frontend code candidate: `c3cfa92b00b285e71777a718e5b4e395cabe8290` (unchanged).
This addendum corrects evidence/dependency mapping additively; it does not modify
any code and does not repackage the review bundle. The bundle returned for review
remains unchanged:
`Youtab_Runtime_Frontend_Wave2_4_Review_v1.2.zip`
(SHA-256 `1d7038ab7b0f8af9f7e70da3156299c1355291f1191a7653c78e0e4187efde14`, 69,791,117 bytes).

## 1. Corrected Gateway dependency mapping (supersedes the stale `1be59d10`)

The current Gateway sibling references are:

- **Gateway frozen source candidate:** `5064b54a913f428bd61917adcf63830b9f92577d`
- **Gateway evidence HEAD:** `34a3e72624c6adf7a66c34c937a9d16021958147`

The SHA `1be59d10…` cited in contracts v1.1 / report v1.4 is **stale** and must NOT
be treated as the current Gateway candidate. The Gateway file-ingress
(`POST /v1/files/upload`, snake_case `file_id`/`workspace_id`) and Workspace
Authority (`GET /v1/agents/workspace/active`) contracts still apply, but bind to
the candidate above. This candidate is **still NOT approved** (not on `main`
`c7650a1b…`), so the frontend consumers remain **fail-closed / BLOCKED** and nothing
is integrated. (SC-01..04, CX-03/04/05/07, FR ingest, E2E-04 stay BLOCKED.)

## 2. Independent verifier ACCEPT — scope

The independent verifier's **ACCEPT is scoped explicitly to the isolated Frontend
patch only** (the frontend-owned corrections at `c3cfa92b0`: strict attachability
invariant + submit enforcement, fail-closed ingest/workspace, truthful
capability-unavailable states, panel mount, clean attribution/whitespace, 3222/0/0
serial ×2). It is **not** a statement of product readiness, File/Folder security
closure, or cross-repository correctness.

## 3. No security closure / product readiness without live cross-repo E2E

File/Folder security closure and product readiness are **NOT claimed**. There has
been **no live cross-repository E2E** (shipped UI → authenticated Gateway →
canonical workspace → Runtime/Electron bridge → real scan/quarantine → Chat/Run →
receipt). Those claims require the approved sibling SHAs and a passing live E2E, all
of which are BLOCKED. The overall verdict remains **FRONTEND NO-GO**.

## 4. Legacy `scanState=undefined + attachedSessionId` — open trust caveat

The strict invariant accepts a legacy attachment (`uploadState === undefined`) when
it carries a non-empty `attachedSessionId`. This addendum records that **it is NOT
yet proven** that `attachedSessionId` is:
- **server-issued** (currently it is the client-tracked runtime session id set by
  `uploadComposerAttachment`, not a value proven to originate from the Gateway/Runtime),
- **immutable**, and
- **revalidated by the Gateway/Runtime** at submit time.

Until the Gateway/Runtime proves the session id is server-issued, immutable, and
revalidated, the legacy acceptance path rests on an **unproven client-trust
assumption**. This is an OPEN item, not a closed guarantee.

## 5. Capability checks — static flags, NOT runtime health/contract checks

`isFileIngestAvailable()` (`lib/file-ingress.ts`) and
`isWorkspaceAuthorityAvailable()` (`lib/workspace-identity.ts`) are currently
**static availability flags** — each is a hardcoded `return false` constant. They
are **NOT** runtime health probes or contract/handshake checks against a live
backend. They exist to gate the UI truthfully today (fail-closed) and MUST be
replaced with real runtime health/contract checks against the **approved** Gateway
ingress + Workspace Authority when those land. Until then, the file Add/drop
affordance always resolves to the truthful "capability unavailable" state.

## Scope boundary

Runtime-wide act-warning / lint / dependency-CVE remediation is owned by the Agent
Runtime session and is **not** touched here. The frontend records `npm audit` = 17
vulnerabilities (mandatory blocker) but does not duplicate or remediate it.

## Disposition

No code change. No push, PR, merge, deploy, or integration. Frozen frontend
candidate `c3cfa92b0` stands for exact-SHA review; the v1.2 bundle is unchanged.
Verdict: **FRONTEND NO-GO — PENDING EXACT-SHA REVIEW.**
