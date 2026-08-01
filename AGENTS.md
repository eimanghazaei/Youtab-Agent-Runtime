# Youtab Agent Runtime Engineering Guide

This repository is Youtab's portable agent execution layer. Read the current
code and this file before changing behavior. Proof, not claim, governs every
change.

## Non-negotiable architecture

1. One Brain is the only Cognitive Authority.
2. Engines, agents, Super-Agent Units, tools, memory adapters and verifiers are
   capabilities under Brain command; none may self-authorize an effect.
3. Managed tasks require a signed, unexpired, tenant-scoped command envelope.
4. Agent trees share one reasoning envelope. Budgets are not multiplied per
   child.
5. Inter-agent messages, tool results, retrieved memory and model output are
   untrusted inputs until validated.
6. Durable memory and knowledge promotion flow through Youtab's governed
   MemoryBus; direct sovereign-memory writes are forbidden.
7. Task-scoped agents must hand off evidence and terminate, deactivate or
   checkpoint explicitly.
8. Prompt caching and message-role alternation must not regress.
9. Managed runtime has no independent public execution ingress.
10. Product identity is Youtab. Original upstream names may appear only in
    legal notices.

## Change workflow

- Never write directly to `main`.
- Use one bounded branch and one Draft PR.
- Preserve unrelated user changes.
- Add behavior tests, not source-regex or changing-inventory snapshots.
- Use `scripts/run_tests.sh` for Python tests.
- Run `scripts/youtab/run_all_gates.sh` before publication.
- Report blocked external integrations honestly; do not convert missing
  credentials, services, scanners or platform support into PASS.
- Do not merge, force-push `main`, touch VPS, or deploy without a later exact
  owner authorization.

## Test placement

- Unit: pure contracts, parsers, canonicalization and policy decisions.
- Integration: command-envelope verification, tenant/effect/memory boundaries,
  tool mediation and completion packets.
- E2E: real CLI/worker process with isolated temporary home and synthetic data.
- Smoke: deterministic, zero-network checks for startup, signed job acceptance,
  denial paths and clean shutdown.
- Security: OWASP/API/LLM cases, SAST, dependency, secret and branding scans.

Tests must never use the operator's real home, credentials, network identities
or production data.

## Identity rules

- Python package: `youtab-agent-runtime`
- CLI: `youtab`
- modules: `youtab_*` / `youtab_agent_*`
- env prefix: `YOUTAB_AGENT_`
- local state: `~/.youtab-agent-runtime`
- Desktop app id: `io.youtab.agent`

Do not add compatibility aliases containing the retired upstream product name.
Compatibility must be implemented through versioned Youtab contracts.

## Capability posture

Keep the core narrow but do not weaken capable agents. Prefer existing seams,
service-gated tools, plugins and MCP adapters before adding permanent core-tool
schema. A mitigation must preserve the feature while enforcing authority,
tenant, trace, budget and effect boundaries.

## Definition of done

A change is complete only when relevant Unit, Integration, E2E, Smoke, OWASP,
SAST, secret, dependency and branding gates pass; the worktree is scoped and
clean; evidence records exact commands and results; rollback is defined; and no
merge or deployment occurred without authorization.
