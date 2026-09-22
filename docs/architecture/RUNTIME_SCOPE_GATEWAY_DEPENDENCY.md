# Runtime Scope — Gateway/Workspace Authority Dependency Request (v1, DRAFT)

Status: **DRAFT / dependency request.** Fail-closed until satisfied.

## Problem

`youtab_runtime.memory.MemoryScope` binds a memory item to
`(tenant_id, organization_id, workspace_id, principal_id, agent_id, run_id, purpose)`.
Mechanical inspection of the canonical base envelope
(`youtab_runtime/contracts.py`, `BrainCommandEnvelope`) shows the signed envelope
cryptographically binds only:

```
command_id, task_id, parent_task_id, tenant_id, user_id (principal),
trace_id, nonce, allowed_toolsets, allowed_memory_scopes, effect_proposal_scopes,
issued_at, expires_at, key_id, signature
```

It does **not** contain `organization_id`, `workspace_id`, `agent_id`, or `run_id`.

## Consequence (current, honest)

- Envelope-bound scope identity today = `tenant_id` + `principal_id` (+ `task_id`, `trace_id`).
- `organization_id`, `workspace_id`, `agent_id`, `run_id`, `purpose` are supplied via
  `ScopeAdmission` and are **NOT cryptographically bound**. The Runtime treats them as
  trusted-input-from-admission and never infers them from cwd, filename, profile, or
  session key. A scope cannot be built without all of them (fail-closed).

## Requested from Gateway / Workspace Authority (versioned interface)

Provide, at admission, a **signed placement** that binds the missing dimensions to the
same trust root as the command envelope. Options (Gateway to choose, then we pin a
`schema_version`):

1. Extend `BrainCommandEnvelope` (v2) with signed `organization_id`, `workspace_id`,
   `agent_id`, `run_id` fields; or
2. Issue a separate signed `youtab.scope-placement.v1` token bound by `command_id` +
   `trace_id`, carrying those four fields + `purpose`, verifiable by the Runtime with a
   Gateway public key.

Until one is delivered, memory scope beyond tenant+principal remains admission-trusted,
and no LIVE multi-tenant/workspace memory behavior may be enabled.

## Acceptance

- Runtime `MemoryScope.from_admission` consumes the signed placement and verifies it.
- Negative tests: forged/mismatched placement, cross-tenant, cross-workspace,
  cross-principal, cross-agent, cross-run, and stale/expired placement all fail closed.
