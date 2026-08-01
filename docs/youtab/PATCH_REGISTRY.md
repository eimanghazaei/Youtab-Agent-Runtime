# Downstream Core Patch Registry

Every downstream change to upstream core must be registered before merge. A
documentation-only bootstrap entry does not count as a core patch.

## Current patches

### `YAR-PATCH-0001` — immutable runtime artifact policy

- **Base:** upstream `cc4cab2f592e60a197e796506de9168f74baf3ea`;
  downstream parent `42e83dda45baec34229eeac75b94515fbf93f0df`.
- **Scope:** `hermes_cli/youtab_runtime_policy.py`, `tools/lazy_deps.py`,
  `hermes_cli/main.py`, and behavioural tests under `tests/youtab/`.
- **Reason:** an admitted Youtab image must not retrieve executable artifacts
  or self-update at runtime. The upstream durable-target mode still permits
  downloads, so its existing seal is insufficient for this contract.
- **Owner:** Youtab Runtime / Supply-chain Security.
- **Capability impact:** strengthened deployment integrity. Developer defaults
  and already-installed features are preserved; admitted images must pre-bake
  dependencies from approved internal mirrors.
- **Security impact:** blocks lazy Python/browser dependency retrieval and the
  built-in source updater when `YOUTAB_RUNTIME_DOWNLOAD_POLICY=deny`.
  Network egress admission remains a required independent control.
- **Cache/context impact:** none.
- **Tests:** `tests/youtab/test_runtime_download_policy.py`, source supply-chain
  verifier, and release admission workflow.
- **Rollback:** revert this patch and remove the production environment flag;
  no data migration is involved.
- **Upstream disposition:** downstream-only deployment policy.
- **Sync policy:** preserve the environment guard across upstream rebases;
  conflict or behavioural removal blocks promotion from quarantine.
- **Status:** implemented; release verification remains blocked until internal
  artifact services and the production image exist.

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
