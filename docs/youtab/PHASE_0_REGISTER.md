# Phase 0 Ground-Truth Register

**Status:** IN PROGRESS

**Gate:** G0 OPEN

**Branch:** `agent/supply-chain-sovereignty`

**Date opened:** 2026-08-01

Gate G0 closes only when there is no unknown authoritative source,
instruction, required capability/control, license obligation, repository
boundary, or asset dependency.

## Verified

- [x] Official upstream repository identified.
- [x] Stable upstream release pinned to an immutable peeled commit.
- [x] Upstream root `AGENTS.md` read before downstream edits.
- [x] MIT license identified and preserved without modification.
- [x] Baseline Git tree and selected artifact hashes recorded.
- [x] Local downstream branch created from the exact pinned commit.
- [x] Upstream remote separated from the future Youtab `origin`.
- [x] No upstream core file changed during bootstrap.
- [x] Three-repository ownership boundary recorded.
- [x] Simorgh source archive re-hashed as
  `d41faffa2ea26a3eaa9fc089755e3f4722e94b6f8029210a6c87722f0956d225`.
- [x] Locked development environment created from `uv.lock` without changing
  the lockfile.
- [x] First canonical credential-free smoke slice completed: 84 passed and 0
  failed across constants, toolsets, and backend identity.
- [x] Every currently advertised upstream branch and tag fetched into an
  isolated local mirror namespace; exact remote/local ref proof required after
  every subsequent sync.
- [x] Upstream push disabled locally; automated intake is mirror-only and may
  not merge into a Youtab product branch.
- [x] Source-level supply-chain policy, offline installers, SBOM/checksum/
  provenance generators, and fail-closed release proof gate added.
- [x] Private downstream `main` published at the exact pinned upstream commit
  `cc4cab2f592e60a197e796506de9168f74baf3ea`.
- [x] Downstream working branch published at
  `89cea8fd61ebe5afe2e6c9c9147753f0ff284c20` and Draft PR #6 opened.
- [x] All 16 owner-supplied architecture-intake documents verified
  byte-for-byte against their source copies and a SHA-256 manifest.
- [x] Authenticated checkouts of all three repositories established; the
  2026-08-01 intake SHAs are Agent Runtime `89cea8fd61eb`,
  backend `77393aeab7f8`, and frontend `07e452d53c6a`.

## Open requirements

- [ ] Read the current authoritative `youtab-ai-os` default branch at its
  exact SHA, including all repository instructions, ADR index/roadmap,
  Agent/Chat/File APIs, auth/tenant contracts, Engine Registry, memory,
  streaming, CI/CD, deployment, and rollback controls.
- [ ] Read the current authoritative `youtab-frontend` default branch at its
  exact SHA, including all repository instructions, Chat/Agent surfaces,
  upload behavior, Simorgh states, streaming, auth routes, tests, and CI/CD.
- [ ] Resolve existing PRs and work-in-progress constraints in both Youtab
  repositories before proposing overlapping changes.
- [ ] Complete upstream feature and control inventory from executable code,
  manifests, and tests.
- [ ] Run the pinned upstream unmodified in an isolated environment.
- [ ] Complete dependency, SBOM, vulnerability, secret, and license scans.
- [ ] Provision authenticated internal Python, npm, OCI, and binary stores;
  populate and verify the exact artifacts recorded by the manifests.
- [ ] Build the production image with no external artifact egress, generate a
  full image/OS SBOM, sign image and provenance, and prove offline rebuild.
- [x] Store and restore-test a verified all-ref Git bundle in a durable
  location outside GitHub.
- [ ] Verify the staged Simorgh implementation contents; current hash proves
  archive identity only, not frontend compatibility or Dutch-language parity.
- [ ] Freeze the versioned Agent Control Plane contract in `youtab-ai-os`.
- [x] Create the private GitHub repository
  `eimanghazaei/Youtab-Agent-Runtime` and configure it as `origin`.
- [x] Push this branch and open a Draft PR without merging it.

## Current blockers

| ID | Blocker | Impact | Required resolution |
|---|---|---|---|
| B0-04 | Docker is not installed in this Work runtime | Container and sandbox baseline cannot run here | Run Phase 1 on the approved isolated Runtime host with Docker/container support |
| B0-05 | Work runtime denies the Unix-domain socket required by `execute_code` tests | Two timezone/code-execution tests are environment-blocked | Rerun the exact canonical test file on the approved Runtime host; do not weaken the code or test |
| B0-06 | Internal PyPI/npm/OCI/binary stores and their credentials are not available in this runtime | Artifact copies, signatures, and a real offline rebuild cannot be completed | Provision Youtab-owned stores and run the release gate with immutable proof files |
| B0-07 | Docker/Syft/Cosign are unavailable here | Production image, complete image SBOM, vulnerability scan, and signed provenance cannot be produced | Run the exact release workflow on the approved build runner |
| B0-08 | The current fine-grained PAT lacks Workflow write permission; 19 of 1406 quarantined branch refs and 0 of 29 quarantined tag refs are materialized remotely | Manual mirror publication is incomplete; local mirror and off-GitHub bundle remain complete | Merge Draft PR #6 only after review/authorization so the repository-scoped workflow can publish all refs with its bounded `GITHUB_TOKEN`, or grant the PAT Workflow write permission |

## Resolved access blockers

- B0-01: portable authenticated GitHub CLI plus GitHub App write path proven.
- B0-02: `youtab-ai-os` cloned and pinned at `77393aeab7f848e3332826c2a463e22f5079f0f9`.
- B0-03: `youtab-frontend` cloned and pinned at `07e452d53c6a09f3d29601184174b21606f6daf8`.

## Gate verdict

G0 is **not passed**. The pinned upstream downstream is ready for Phase 0
analysis, but implementation integration, public rebranding, backend wiring,
frontend wiring, merge, deployment, and production claims remain prohibited
until the open requirements are proven.
