# Youtab Agent Runtime Downstream

This directory records the controlled Youtab downstream of the upstream Agent
runtime. It is the runtime execution layer in Youtab's three-repository
architecture; it is not a second Brain and it is not a user-facing frontend.

## Repository role

- Execute Agent jobs, tools, code, terminals, browser actions, MCP calls,
  skills, checkpoints, and Brain-authorized subagent work.
- Receive task contracts and authority from `youtab-ai-os`.
- Return structured events, evidence, resource usage, completion reports, and
  checkpoints to `youtab-ai-os`.
- Never become the owner of durable Youtab identity, tenant authority,
  Cognitive Authority, Memory Fabric, policy, or final effect authorization.
- Never expose a direct browser-to-runtime control path.

The user interface, including Simorgh and Agent controls, belongs in
`youtab-frontend`. The frontend communicates with `youtab-ai-os`; it does not
connect directly to this runtime.

## Current state

- Bootstrap branch: `bootstrap/phase-0-ground-truth`
- Upstream source is pinned and recorded in `UPSTREAM_PROVENANCE.md`.
- No upstream core patch has been made.
- Phase 0 is in progress; Gate G0 is not closed.
- Phase 1 unmodified runtime qualification has not started.
- Private origin: `eimanghazaei/Youtab-Agent-Runtime`; product changes remain
  isolated on a non-`main` branch until review and explicit merge approval.
- No merge, deployment, VPS change, or production admission is authorized by
  this bootstrap.

## Control documents

- `UPSTREAM_PROVENANCE.md` — immutable upstream identity and license evidence.
- `REPOSITORY_BOUNDARIES.md` — ownership boundaries and allowed data flow.
- `PHASE_0_REGISTER.md` — gate status, evidence, and blockers.
- `FEATURE_PARITY_MATRIX.md` — retained-capability inventory and proof status.
- `PATCH_REGISTRY.md` — downstream core-patch ledger.
- `BASELINE_PLAN.md` — Phase 1 isolation and qualification plan.
- `PHASE_1_PREFLIGHT_EVIDENCE_2026-08-01.md` — first executable preflight
  evidence and environment blockers.
- `SUPPLY_CHAIN_SOVEREIGNTY.md` — source mirroring, quarantined updates,
  offline dependencies, SBOM/provenance, branding, and release gates.
- `architecture-intake/README.md` — owner-supplied architecture reference
  set with explicit non-canon authority boundaries and a 16-file inventory.

The operating doctrine is **Proof, Not Claim**. An unchecked item is not an
implemented or supported capability.
