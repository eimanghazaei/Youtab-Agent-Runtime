# Youtab Agent Runtime / Hermes Intake Execution Report

**Date:** 2026-08-01

**Owner:** Eiman Ghazaei

**Status:** source downstream and controlled intake established; integration
and Production admission not complete

**Rule:** proof, not claim

## 1. Executive verdict

The complete upstream Git source advertised at verification time is present
locally: 1406 branches and 29 tags matched the official upstream with zero
missing, extra, or changed refs. The 16 owner-supplied architecture files are
also present in the downstream branch byte-for-byte and pass their SHA-256
manifest.

This does **not** mean that every upstream change automatically becomes active
Youtab product code. Upstream changes enter a quarantine namespace first.
Promotion into the Youtab Agent product branch requires version pinning,
contract and patch-drift review, capability/security/regression qualification,
a reviewed PR, and the applicable owner/release gate. No upstream update may
silently modify Youtab One Brain, Canon, durable Memory authority, frontend,
VPS, or Production.

## 2. Exact repository ground truth

| Repository | Role | Verified branch/SHA | State |
|---|---|---|---|
| `eimanghazaei/Youtab-Agent-Runtime` | Hermes-derived execution runtime | `main@cc4cab2f592e60a197e796506de9168f74baf3ea`; working branch initially published at `89cea8fd61ebe5afe2e6c9c9147753f0ff284c20` | Private origin, Draft PR #6 |
| `eimanghazaei/youtab-ai-os` | One Brain, authority, tenant, memory, engine and Agent Control Plane | `main@77393aeab7f848e3332826c2a463e22f5079f0f9` | Authenticated clean checkout |
| `eimanghazaei/youtab-frontend` | Web OS and user-facing Chat/Agent controls | `main@07e452d53c6a09f3d29601184174b21606f6daf8` | Authenticated clean checkout |

The pinned upstream release is `v2026.7.30` /
`cc4cab2f592e60a197e796506de9168f74baf3ea`. Its MIT license and Nous
Research copyright notice remain preserved. The latest upstream `main` seen
during the final mirror sync was `66ba36ec81f3923cb285441d528158ab232bcfec`;
it is quarantined and was not merged into the pinned product baseline.

## 3. Source and file completeness

### Official upstream Git source

- local mirrored branches: 1406
- official remote branches at the same check: 1406
- local mirrored tags: 29
- official remote tags at the same check: 29
- missing/extra/changed refs: 0 / 0 / 0
- upstream push URL: disabled
- all-ref backup: generated, checksum-verified, restored into a new bare
  repository, and checked against its ref manifest

This is a 100% ref-completeness statement for the upstream state observed at
the verification timestamp. It is not a promise that upstream cannot publish a
new ref after the check; the scheduled intake workflow exists for that.

### Owner-supplied architecture intake

- supplied documents: 16
- tracked documents: 16
- SHA-256 matches: 16/16
- authority: reference intake, not automatically accepted Canon
- PR location: `docs/youtab/architecture-intake/`

### Remote mirror publication

At report preparation, the private origin contained the exact product
`main`, the exact downstream working branch, 19 quarantined upstream branch
refs, and 0 quarantined upstream tags. The complete 1406/29 mirror exists
locally and in the verified off-GitHub bundle. Remote materialization remains
open because the fine-grained PAT has Code/Pull Request access but not Workflow
write access. The repository-scoped mirror workflow can finish this after the
Draft PR is reviewed and separately authorized for merge.

## 4. What was implemented

1. Established the private downstream origin without rewriting upstream
   history.
2. Published `main` at the exact pinned upstream SHA.
3. Published the Youtab branch and opened Draft PR #6.
4. Preserved all 16 architecture artifacts and their exact bytes.
5. Added provenance, repository-boundary, feature-parity, Phase 0, baseline,
   patch-registry, and supply-chain control documents.
6. Added source verification, source SBOM/provenance generation, dependency
   locks/integrity accounting, offline installer paths, and bundle/evidence
   tooling.
7. Registered `YAR-PATCH-0001`, which blocks lazy executable downloads and
   self-update when the admitted Youtab runtime policy is enabled.
8. Added scheduled upstream intake that writes only
   `upstream-mirror/*` quarantine refs and never merges the product branch.
9. Obtained authenticated exact-SHA checkouts of backend and frontend and
   inspected their current Agent/Chat/File/streaming seams.

No merge, VPS mutation, deployment, Production activation, Canon acceptance,
or public rebranding was performed.

## 5. Update admission behavior

```text
official upstream update
  -> exact fetch and ref/hash proof
  -> private quarantine refs
  -> diff against pinned admitted version
  -> downstream patch-drift and license review
  -> capability, contract, security, tenant, memory and performance tests
  -> candidate Youtab branch and Draft PR
  -> CI/evidence/review/owner gate
  -> merge/release/deploy only when separately authorized
```

Therefore:

- **Does every upstream file enter Youtab storage?** It enters the quarantined
  source mirror and backup when the mirror job completes.
- **Does every upstream file enter the active Youtab system?** No. Only a
  qualified, pinned version enters the product branch.
- **Can upstream updates be used?** Yes. The mirror and patch registry make
  updates importable without surrendering Youtab authority or silently losing
  downstream changes.

## 6. Three-repository integration finding

The current backend already has a Youtab-owned Agent substrate:
`/v1/agents`, `/v1/agents/run`, tenant registry/run inspection, budgets,
replay, MemoryBus use, permission checks, and language resolution. It does not
yet have a Hermes Runtime Adapter or a frozen Agent Control Plane contract for
the new downstream.

The current frontend has canonical `youtab_chat` integration and a real
Agent Call telephony adapter. It does not yet expose the full Youtab
Agent/Super-Agent capability matrix, Agent Tree, Context Mesh, shared ledger,
approvals/Auto Decide, checkpoints, evidence, runtime tools, or the complete
Simorgh phase protocol.

The required direction remains:

```text
youtab-frontend -> youtab-ai-os / One Brain -> Agent Control Plane
                -> Youtab-Agent-Runtime -> tools/subagents/evidence
                -> Agent Control Plane -> One Brain synthesis -> frontend
```

Direct browser-to-Agent-Runtime control and direct runtime ownership of
durable Youtab memory/authority are forbidden.

## 7. Validation evidence

| Check | Result |
|---|---|
| Upstream ref verifier after final sync | PASS, 1406/1406 branches and 29/29 tags |
| Architecture-intake SHA-256 manifest | PASS, 16/16 |
| Source supply-chain verifier | PASS |
| Runtime download-policy focused tests | PASS, 4/4 |
| Non-intake `git diff --check` | PASS |
| Youtab shell-script syntax | PASS |
| Remote base commit retrieval | PASS, exact `cc4cab2f592e...` |
| Remote branch commit retrieval | PASS, exact `89cea8fd61eb...` before this report commit |
| Draft PR | PASS, #6 |

The source verifier still correctly reports release blockers: Debian runtime
base is not digest-pinned, internal OCI/binary stores are not provisioned, and
the final Production image is not built, scanned, signed, or admitted.

## 8. Roadmap status

| Roadmap phase | Status | Evidence / remaining gate |
|---|---|---|
| Phase 0 — ground truth | **In progress, substantially advanced** | Three repos accessible and pinned; upstream/license/intake/source boundaries proven. G0 remains open for exhaustive repo/WIP/control inventory, full scans, Simorgh compatibility and Control Plane contract. |
| Phase 1 — unmodified baseline | **Not started as a full qualification** | Preflight smoke and focused tests exist, but the complete isolated capability/security/performance matrix has not run. |
| Phase 2 — contracts/threat model | **Not frozen** | Direction is documented; versioned cross-repo Agent Control Plane, event, effect, Context Mesh, Memory Fabric and ledger contracts remain to be approved. |
| Phase 3 — controlled fork/conformance | **Partially implemented** | Exact downstream, patch registry, quarantine sync, runtime download policy and conformance source gates exist. G3 is not passed until full baseline/conformance/rebase proof. |
| Phase 4 — Agent Control Plane | **Not implemented for Hermes downstream** | Existing backend Agent runtime is not yet connected through an adapter. |
| Phase 5 — Context/Memory/shared ledger | **Not implemented** | Design/reference intake only. |
| Phase 6 — elastic Super Agent/autonomy | **Not implemented** | Design/reference intake only. |
| Phase 7 — complete project ingestion | **Not implemented** | Existing Chat files are not the full manifest/indexing pipeline. |
| Phase 8 — Coding Closure | **Not implemented** | E0-E13 is roadmap doctrine, not runtime proof. |
| Phase 9 — UI parity/Simorgh | **Not implemented** | Current Chat/Agent Call are real but incomplete relative to the parity matrix. |
| Phase 10 — full qualification | **Not started** | Requires full regression/security/chaos/load/release/rollback suite. |
| Phase 11 — pilot/Production | **Not authorized and not ready** | Requires immutable artifacts, canary, SLO and separate owner authorization. |
| Phase 12 — Youtab-native agents | **Future phase** | Not part of the current downstream bootstrap. |

## 9. Immediate next controlled work

1. Finish G0 exhaustive backend/frontend/Runtime inventory and resolve current
   PR/WIP overlap.
2. Execute Phase 1 against the pinned unmodified baseline on an isolated runner
   with Docker and required sandbox support.
3. Freeze versioned cross-repository Agent Control Plane contracts in
   `youtab-ai-os`; do not create a second Brain or a parallel public API.
4. Implement the adapter and contract tests in backend and Runtime branches.
5. Wire frontend controls only after real backend/runtime endpoints and events
   exist.
6. Keep every upstream update quarantined until its exact candidate passes the
   same gates.

This report deliberately separates downloaded source, committed downstream
work, integrated runtime behavior, merged code, deployment, and Production
readiness. Those states are not interchangeable.
