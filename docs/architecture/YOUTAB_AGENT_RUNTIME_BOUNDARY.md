# Youtab Agent Runtime authority boundary

Youtab has one cognitive authority: One Brain. This runtime is an execution plane for task-scoped agents and Super-Agent Units; it is not another brain, policy sovereign, memory authority, or effect authorizer.

## Admission contract

Every managed task enters through a signed `youtab.agent-command.v1` envelope. The envelope binds issuer, audience, command, task, parent task, tenant, user, trace, nonce, objective, allowed toolsets, allowed memory scopes, effect-proposal scopes, shared reasoning limits, expiry and signing key. Unknown fields, invalid signatures, expired commands, replayed tenant nonces, authority-bearing toolsets and sovereign memory scopes fail closed.

The objective is data. Instructions embedded inside it cannot expand the signed contract.

## Effects and memory

Read-only or effect-free work may run only within the signed allowlist. Writes, network calls, processes, credential access and memory writes are never self-authorized. They become `youtab.effect-proposal.v1` records for the Brain Effect Gate. The proposal carries an arguments digest and `runtime_authorized=false`.

The runtime cannot write or promote sovereign memory. Memory use must remain inside the command's allowed read scopes and the system-owned MemoryBus.

## Lifecycle

Every task-scoped agent returns `youtab.agent-completion.v1` with actions, tools, memory, evidence, uncertainty, conflicts, verifier results, outputs, unresolved items, trace references and an explicit deactivation state. Cross-task and cross-tenant completion is rejected.

The managed worker uses stdin/stdout and does not open a public listener. Public ingress belongs to the owning Youtab system boundary.
