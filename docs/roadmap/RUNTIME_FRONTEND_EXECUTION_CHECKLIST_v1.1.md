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
