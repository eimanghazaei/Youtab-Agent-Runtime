# Upstream Feature Parity Matrix

This is a proof register, not a marketing checklist. `Inventory found` means a
surface exists in source or upstream instructions. Only `Qualified` means the
pinned unmodified baseline has passed the required tests in Youtab's isolation
environment.

| Capability | Inventory found | Qualified | Youtab ownership/route | Required evidence |
|---|---:|---:|---|---|
| Core Agent loop and model calls | Yes | No | Runtime under Brain task contract | Deterministic conversation/tool-loop tests, cache and role-alternation proof |
| Terminal execution | Yes | No | Runtime isolated workspace | Backend matrix, cwd isolation, cancellation, timeout, egress and secret tests |
| File read/write/edit | Yes | No | Runtime isolated workspace | Traversal, symlink, binary, large-file, rollback and exact-scope tests |
| Code execution | Yes | No | Runtime sandbox | Language/runtime matrix, limits, isolation, result/evidence tests |
| Browser automation | Yes | No | Runtime sandbox and egress policy | Navigation, download/upload, credential boundary and SSRF tests |
| MCP client/toolsets | Yes | No | Runtime; authorization from Control Plane | Discovery, versioning, credential isolation and tool-availability tests |
| Skills | Yes | No | Runtime execution; governed install policy | Install/use/update/rollback and supply-chain tests |
| Memory providers | Yes | No | Adapter only; Youtab Memory Fabric remains authoritative | Isolation, provenance, injection, sync and no-second-memory-authority tests |
| Context engine/compression | Yes | No | Runtime adapter to Context Mesh | Cache stability, compaction fidelity, long-context and resume tests |
| Subagents/delegation | Yes | No | Brain-authorized Runtime execution | Inheritance, scope, concurrency, depth, budget, cancellation and handoff tests |
| Kanban/multi-worker queue | Yes | No | Candidate Runtime mechanism, not tenant authority | Durability, tenancy, stale claims, restart and Brain-command tests |
| Cron/scheduled jobs | Yes | No | Runtime execution under durable Control Plane ownership | Restart, duplicate suppression, lease, cancellation and delivery tests |
| Checkpoints | Yes | No | Runtime adapter; durable references in Control Plane | Create/resume/expire/rollback and compatibility tests |
| Pause/cancel/interrupt | Yes | No | Frontend -> Control Plane -> Runtime | Race, blocked-tool, subtree kill and final-state tests |
| Messaging gateways/channels | Yes | No | Reuse subject to Youtab identity and tenant contracts | Per-channel auth, delivery, replay and provider-brand-leak tests |
| TUI | Yes | No | Internal/engineering surface only unless separately admitted | Build, streaming, approvals and terminal behavior tests |
| Desktop application | Yes | No | Not the Youtab frontend; upstream baseline reference only | Build/E2E and parity inventory; no iframe or duplicate public chat decision |
| Dashboard/web UI | Yes | No | Not the Youtab frontend; baseline reference only | Inventory only, then map controls to real `youtab-frontend` UI |
| Profiles | Yes | No | Insufficient for enterprise tenancy | Explicit negative proof plus tenant-isolation design/tests |
| Plugin system | Yes | No | Runtime extension surface | Lifecycle, isolation, signature/provenance and failure-containment tests |
| Provider/model routing | Yes | No | Must defer to Youtab Engine Registry and Brain | Adapter contract, no hardcoded authority, fail-closed and streaming tests |

No row may be marked `Qualified` from documentation or source inspection
alone. Qualification requires executable evidence against the pinned commit.
