# Phase 1 Unmodified Baseline Plan

## Objective

Run the exact pinned upstream commit without Youtab core modifications and
measure what it actually supports. Phase 1 discovers capabilities and limits;
it does not rebrand, integrate, merge, or deploy.

## Isolation

- Use a dedicated unprivileged service identity and task-scoped temporary home.
- Keep Youtab production credentials, user data, VPS, and networks out of the
  baseline environment.
- Use synthetic repositories and fixtures first; add immutable approved real
  repository fixtures only after their authorization and hashes are recorded.
- Deny external effects by default; explicitly allow only the dependency and
  test endpoints required for each case.
- Capture versions, commands, exit codes, resource usage, logs, and artifacts.

## Qualification groups

1. Installation, dependency lock, doctor, startup, and clean shutdown.
2. Core chat loop, native streaming, prompt caching, role alternation, context
   compaction, resume, and long-session behavior.
3. File, terminal, code, browser, MCP, skills, plugins, and provider routing.
4. Memory, context engines, checkpoints, project ingestion, and recovery.
5. Delegation, subagent nesting, concurrency, shared budgets, cancellation,
   handoff, completion, and orphan detection.
6. Cron, background work, Kanban/worker durability, retry, and restart.
7. CLI, TUI, desktop, dashboard, messaging gateways, and control parity.
8. Security: auth, secrets, traversal, symlinks, injection, SSRF, egress,
   sandbox escape, dependency/SBOM/license, and denial behavior.
9. Performance: cold/warm latency, streaming time-to-first-event, throughput,
   memory/CPU/disk, context cost, and agent-storm behavior.
10. Failure injection: model/provider loss, tool crash, process restart,
    corrupted checkpoint, stale worker, network partition, timeout, and kill.

## Required outputs

- Versioned Evidence Pack linked to the upstream commit.
- Updated Feature Parity Matrix with executable proof references.
- Limitations and risk register.
- Candidate patch list, with no patch applied yet.
- Go/no-go verdict for freezing the Agent Control Plane contract.

Phase 1 passes only when failures and unsupported behavior are recorded as
clearly as successes. Missing credentials or unavailable external services are
`BLOCKED`, never silently converted into a pass.
