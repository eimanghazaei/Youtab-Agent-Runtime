# Repository Ownership and Boundaries

## Decision

Youtab uses three repositories with one Cognitive Authority:

| Repository | Owns | Must not own |
|---|---|---|
| `youtab-ai-os` | One Brain, Cognitive Authority, Agent Control Plane, tenant/auth, Context Mesh, Memory Fabric, shared resource ledger, autonomy leases, policy/effects, evidence, durable task state, final authorization | Forked runtime implementation or browser UI |
| `youtab-agent-runtime` | Agent execution loop, workers, tools, terminal/code/browser/MCP execution, runtime workspaces, Brain-authorized subagents, ephemeral execution caches, checkpoint adapters | A second Brain, final authorization, durable tenant authority, independent policy/memory authority, direct public UI API |
| `youtab-frontend` | Chat and Agent UI, Simorgh, Projects/Files/Terminal views, Agent tree, approvals, usage, pause/cancel/resume, rollback controls | Credentials, code execution, policy enforcement, Cognitive Authority, direct Runtime access |

## Allowed request flow

```text
youtab-frontend
  -> youtab-ai-os Gateway / One Brain / Agent Control Plane
  -> internal authenticated Runtime Adapter
  -> youtab-agent-runtime worker
  -> tools and isolated execution environments
```

Events and results return through the reverse controlled path. The frontend
does not receive Runtime credentials and does not bypass `youtab-ai-os`.

## Required control-plane contract

Before Runtime integration, `youtab-ai-os` must define versioned contracts for:

- task identity, tenant, actor, repository, environment, and objective;
- autonomy mode (`manual`, `ask_important`, `auto_decide`) and lease scope;
- allowed tools, effects, credentials, egress, and resource envelope;
- context references and memory references without copying authority;
- progress events, tool events, evidence, usage, and completion status;
- checkpoint, pause, cancel, resume, timeout, kill, and rollback;
- idempotency, retry, replay, ordering, and trace correlation;
- version negotiation and fail-closed behavior.

Until those contracts are approved, this runtime may be qualified in
isolation but must not be wired directly to production or to the frontend.

## Runtime state rule

The Runtime may hold isolated, task-scoped workspaces and caches required for
execution. Durable Youtab memory, identity, authority, and audit ownership stay
in `youtab-ai-os`. Runtime state must be traceable, expirable, tenant-bound,
and exportable through the Control Plane.
