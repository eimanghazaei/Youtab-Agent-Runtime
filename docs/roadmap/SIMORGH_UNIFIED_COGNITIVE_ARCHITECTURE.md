# Simorgh Unified Cognitive Architecture

**Status:** BINDING OWNER ARCHITECTURE  
**Scope:** Youtab platform, Simorgh, Agent Runtime, Chat, Code, Voice, workflows, tools, engines, memory, and all future cognitive components  
**Rule:** This document supersedes any roadmap, prompt, ADR, implementation note, or test assumption that conflicts with it.

## Canonical principle

> **One central brain, many growing cognitive agents.**
>
> Simorgh is the unified brain and system-level orchestrator. Agents may
> maintain memory, learn, evolve, specialize, and become stronger without
> hard-coded cognitive ceilings, while sharing governed knowledge with Simorgh
> and remaining part of one coherent system under Simorgh's supervision.

Simorgh is the central brain and integrator of Youtab. Agent Runtime, Chat,
Code, Voice, workflows, tools, replaceable AI engines, and future subsystems
operate as parts of the unified Simorgh architecture. They must not create a
second platform brain or a competing system-level orchestrator.

## Simorgh responsibilities

Simorgh owns system-level coherence:

- unified identity and user context;
- global goals, intent continuity, and cross-surface coordination;
- orchestration of agents, engines, tools, workflows, and runtimes;
- shared cognitive state and system-wide memory synthesis;
- conflict resolution between agent conclusions and memories;
- routing work to the best available engine or agent without coupling memory
  to one model provider;
- integrating useful agent learning so the whole Youtab system improves;
- maintaining continuity across Chat, Agent Runtime, Code, Voice, desktop,
  mobile, web, cloud, local, and offline operation.

Simorgh is not a thin router and must not be reduced to a renamed workflow
engine. It is the primary cognitive architecture of Youtab.

## Agent cognition, memory, learning, and growth

Agents are not stateless tools. Each agent may maintain and develop:

- working memory;
- long-term memory;
- episodic and experiential memory;
- semantic knowledge;
- procedural and skill memory;
- task history and outcome memory;
- learned strategies, preferences, and domain specialization;
- reflection, evaluation, feedback, and self-improvement state.

Agents may learn from experience, improve their methods, specialize, and become
stronger over time. Agent Runtime must support this growth rather than erase,
reset, flatten, or artificially cap it.

There must be no hard-coded ceiling on agent intelligence, learning, memory,
operational capability, specialization, collaboration, or long-term growth.
Resource policy may be dynamically governed by the Owner and deployment
environment, but an implementation must not silently convert a resource or
security policy into a cognitive limitation.

## Relationship between agents and Simorgh

Agent-local cognition does not make an agent a competing Youtab brain.

Agents have operational autonomy within their assigned goals and authority.
They may plan, execute, use tools, collaborate, create sub-agents, remember,
learn, and improve. They share useful memories, discoveries, skills, outcomes,
and provenance with Simorgh through governed memory exchange.

Simorgh coordinates and integrates that knowledge at system level, makes it
available where appropriate, and preserves one coherent Youtab identity and
direction. Agent memory may remain local, shared, promoted, summarized, or
federated according to provenance, relevance, user ownership, privacy, and
tenant boundaries.

Sharing with Simorgh must not mean deleting the agent's own memory, weakening
its expertise, or forcing all cognition through a single synchronous store.

## Memory architecture requirements

The architecture must support:

- persistent agent identity and agent-local memory across restarts;
- persistent Simorgh system memory;
- explicit memory provenance, timestamps, ownership, confidence, and source;
- governed agent-to-Simorgh and Simorgh-to-agent memory exchange;
- conflict detection and reconciliation without silently overwriting history;
- engine-independent memory so changing GPT, DeepSeek, Qwen, Llama, Mistral,
  or another engine does not erase or require rebuilding cognitive state;
- an embedding abstraction so embedding-engine replacement does not destroy
  memory compatibility;
- local or VPS-controlled storage for state, embeddings, and vector data;
- portable synchronization across local, cloud, desktop, mobile, and offline
  modes;
- tenant and user isolation with no cross-tenant memory disclosure;
- auditable promotion of learned material into shared system knowledge;
- rollback and correction without deleting valid user or agent history.

## Agent Runtime's role

Agent Runtime is the powerful execution and growth substrate for cognitive
agents. It is not the central brain.

It must provide agents with the tools, isolation, project-wide understanding,
search, dependency access, debugging, code execution, file operations,
computer use, collaboration, memory, and learning facilities required to
complete their assigned work at full capability.

Agent Runtime may contain local planning and decision loops required by an
agent's task. Those loops remain subordinate to Simorgh's system-level goals,
identity, coordination, and shared cognitive architecture; they must not become
a duplicate global orchestrator.

## Security is not cognitive weakening

Security boundaries, data ownership, tenant isolation, sandboxing, and explicit
authority exist to prevent unauthorized effects and disclosure. They are not
permission to weaken agent intelligence, memory, learning, project
understanding, or legitimate execution capability.

In particular:

- project-scoped reading must remain broad enough to understand relationships
  across the whole authorized project;
- agents must not read unrelated projects or another tenant's data;
- destructive operations must follow the user's request and granted authority;
- legitimate dependency installation, research, search, debugging, tool use,
  and file editing must remain available when required by the task;
- isolation must contain impact without making the agent less capable;
- no platform-name check, arbitrary static quota, or hidden fallback may
  silently reduce cognitive or operational capability.

## Prohibited architectural regressions

The following interpretations are forbidden:

- making agents stateless or memoryless;
- treating Agent Runtime as a mere command runner;
- building a second independent platform brain beside Simorgh;
- duplicating global identity, global memory authority, or system-level
  orchestration inside Agent Runtime, Chat, or a provider adapter;
- tying memory or learning to one replaceable model engine;
- deleting agent-local memory after sharing it with Simorgh;
- imposing hard-coded cognitive ceilings or fixed growth limits;
- describing security isolation as a reason to remove legitimate capability;
- allowing Chat, Code, Voice, workflows, or tools to bypass Simorgh and create
  a fragmented parallel cognitive system.

## Integration order

1. Complete and verify the Simorgh brain and its durable cognitive contracts.
2. Establish persistent, engine-independent memory and governed memory exchange.
3. Wire Agent Runtime to Simorgh as the execution and growing-agent substrate.
4. Wire Chat and other user interfaces to Simorgh, not to a competing brain.
5. Connect Code, Voice, workflows, tools, computer use, and future surfaces
   through the same unified architecture.
6. Verify cross-surface continuity, agent growth, memory sharing, tenant
   isolation, and engine replacement end to end.

Subsystem work may proceed in parallel when it does not invent a competing
brain or lock in a contract that conflicts with this order.

## Required acceptance evidence

Future implementation and review must prove that:

- an agent retains authorized memory and learned skill across restart;
- an agent can improve from outcomes and reuse that learning;
- useful agent learning can be shared with Simorgh with provenance intact;
- Simorgh can integrate knowledge from multiple agents without erasing their
  specialization;
- changing the underlying model engine does not erase identity, memory, goals,
  or learned capability;
- Chat and Agent Runtime operate under the same Simorgh context;
- agents preserve operational autonomy without creating a second global brain;
- tenant and user isolation prevent unauthorized memory access;
- no hard-coded cognitive ceiling or silent capability downgrade exists;
- security controls restrict unauthorized effects, not legitimate cognition;
- the whole system remains coherent under Simorgh's supervision.

## Roadmap governance

Every roadmap item, ADR, prompt, code review, and implementation slice touching
Simorgh, agents, memory, Chat, Agent Runtime, engines, tools, or orchestration
must be checked against this document.

If an existing implementation or document conflicts with this architecture,
the conflict must be recorded and corrected at its root cause. It must not be
silently reinterpreted, ignored, or used to build a second brain.
