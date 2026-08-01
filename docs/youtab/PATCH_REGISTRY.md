# Downstream Core Patch Registry

Every downstream change to upstream core must be registered before merge. A
documentation-only bootstrap entry does not count as a core patch.

## Current patches

No upstream core patches have been applied.

## Required entry schema

| Field | Requirement |
|---|---|
| Patch ID | Stable `YAR-PATCH-NNNN` identifier |
| Base | Exact upstream commit and downstream parent commit |
| Scope | Exact files, symbols, behavior, and public surfaces |
| Reason | Proven limitation or approved Youtab contract requirement |
| Owner | Responsible implementation/review owner |
| Capability impact | Preserved, strengthened, replaced, or intentionally unavailable with reason |
| Security impact | Threats, trust-boundary changes, and mitigations |
| Cache/context impact | Prompt-cache and context-lifecycle analysis |
| Tests | Focused, regression, integration, security, and conformance evidence |
| Rollback | Revert path, data compatibility, and recovery evidence |
| Upstream disposition | Candidate contribution, downstream-only, or superseded upstream |
| Sync policy | Conflict/rebase plan for future upstream versions |
| Status | Proposed, implemented, verified, merged, superseded, or retired |

Unregistered core drift fails downstream conformance and blocks release.
