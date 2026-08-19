# Youtab Code–Agent Workspace Architecture Contract

**Status:** CANON — LIVING, VERSIONED, AND EVOLVABLE  
**Version:** 1.0  
**Owner ratification:** 2026-08-19  
**Scope:** Product Youtab Agent, persistent Workspace, execution Sandbox, Code application, staged/direct change handling, live activity transport, reviewed egress, and implementation order.  
**Implementation status:** This document defines architecture. It does not by itself claim that any endpoint, UI, Sandbox, transport, or security control is implemented or production-ready.

## Canon interpretation

This is the current authoritative architecture for the Youtab Code–Agent Workspace path. It is a **Living Canon**, not a frozen design:

- The architecture may be amended, extended, replaced, or versioned at any implementation stage when product evidence, security findings, performance measurements, platform evolution, user experience, or Simorgh evolution justify a better design.
- A change requires an explicit reviewed PR or ADR amendment that records the reason, affected contracts, threat-model impact, compatibility, migration, rollback, tests, and executable acceptance evidence appropriate to the change.
- The newest Owner-ratified version is authoritative. Older versions remain traceable in Git history; there is no silent drift.
- Canon status must never be interpreted as a hard-coded ceiling on Agent intelligence, memory, learning, specialization, operational strength, multi-agent collaboration, or future architecture.
- Evolution must not silently weaken tenant isolation, data ownership, evidence, rollback, capability boundaries, or the rule that Simorgh remains Youtab's unified system-level brain and orchestrator.
- Security boundaries restrict unauthorized authority and blast radius; they are not cognitive limits.
- Implementation progress is tracked separately. A Canon decision is not evidence that the corresponding code is complete.

## Actor boundary

- **Simorgh** is the unified brain and system-level orchestrator.
- **Product Youtab Agent** is an autonomous, growing cognitive execution component operating under Simorgh's coordination and governed authority.
- **Code** is a distinct user-facing application and workspace surface.
- **Chat** is a distinct conversational surface that may invoke Agent through governed contracts.
- Agents may maintain memory, learn, specialize, delegate, use sub-agents, and improve over time without hard-coded cognitive ceilings.
- Agent Runtime is not a competing second brain; useful governed learning and memory can be shared with Simorgh for system-wide growth.
- The implementation agent developing this repository is not the Product Agent and is governed by repository contribution rules rather than Product-Agent runtime restrictions.

## Source and applicability

This contract canonizes the architecture-bearing sections of the Owner document **“Single Owner Mandate — V4 Kit Integration, Capability-Preserving Isolation, Workspace and Code Architecture”** (Library source: `Pasted markdown (2)(3).md`).

Historical execution instructions, obsolete PR/SHA state, one-time archive-review steps, and temporary deployment prohibitions from that mandate are intentionally not Canon. The architecture-bearing provisions follow.

---

## Corrected persistent Workspace architecture

Adopt the V4 direction:

```text
Ingress adapters
→ one governed persistent workspace contract
→ Agent execution
→ reviewed egress
```

Supported general Runtime ingress adapters may include:

```text
empty
upload
git clone
SFTP pull
explicit local-folder connector
```

The Product Agent sees only the workspace contract, not ingress-specific authority.

Required properties:

* immutable `v0` baseline;
* exact directory structure preserved;
* explicit size/file-count limits;
* skipped files reported before completion;
* tenant/user/workspace ownership;
* persistent workspace independent of chat lifetime;
* retention policy;
* user-initiated deletion;
* encrypted storage;
* audit trail;
* no shared writable workspace between tenants.

### Code app scope

For a workspace created from the Web OS Code app:

* upload-only by default;
* user disk is never opened, mounted, watched or written;
* workspace exists server-side;
* Agent receives only workspace-scoped capability;
* replacing original local files is a separate user action;
* Code may export an archive, changed files or patch;
* Code may prepare a branch and PR description;
* the authenticated user opens the PR.

### General local-folder connector

A local-folder connector is separate from the Code upload flow.

It requires:

* an explicit folder selected by the user;
* only that exact project root authorized;
* no parent-directory access;
* no unrelated project access;
* canonical-path and symlink-escape protection;
* project-specific capability envelope;
* macOS VirtioFS or equivalent governed sharing;
* dependency directories in named volumes;
* host UID/GID mapping;
* no root-owned output.

---

## Resolve the staged-mode versus bind-mount conflict

V4 currently contains a contradiction:

* `apply_policy: staged` is declared the default.
* `RUNTIME_MODES.md` says a local-folder bind mount lets Agent writes immediately change the same inode the user has open.

Both cannot be true simultaneously.

The Owner decision is:

### Staged mode — default

In staged mode:

* Agent writes must not directly modify the user’s original folder.
* The local-folder adapter creates or synchronizes an isolated workspace/staging layer.
* User originals remain untouched.
* User reviews per-file diffs.
* Only approved files are exported/applied through a user-authorized action.
* External changes in the original folder are detected before approval/apply.
* Drift causes conflict, re-read and re-plan—not overwrite.

### Direct mode — explicit exception

Direct mode is available only when:

* the user explicitly enables it for that workspace;
* the target is git-backed or has an independently proven rollback mechanism;
* the exact selected project root is mounted;
* a snapshot is proven before writing;
* hash revalidation occurs before every write;
* no other host path is visible;
* the decision is recorded and auditable.

Never infer Direct mode. Never make it the default.

Update V4 documentation/specifications to remove this contradiction before treating them as locked implementation contracts.

---

## Active Sandbox versus persistent Workspace

Do not interpret “one container per workspace” as “one permanently running container for every stored workspace.”

The Owner decision is:

* workspace storage persists;
* execution containers are disposable;
* one active isolated sandbox per active workspace/task/session;
* no writable sandbox sharing between workspaces or tenants;
* an idle workspace does not require an indefinitely running container;
* warm containers may be pooled only before tenant/workspace assignment;
* after assignment, a warm sandbox belongs only to that tenant/user/workspace until destroyed;
* no container is created for every shell command.

This preserves isolation without making Sandbox cold-start and resource usage a bottleneck.

---

## Capability-based autonomy

Security restricts authority and blast radius, not intelligence.

At task start, create a bounded capability envelope containing:

* authorized workspace/repository roots;
* tenant and user identity;
* read roots;
* write roots;
* execution profile;
* network profile;
* dependency policy;
* deletion policy;
* external-write policy;
* resource limits;
* expiry/TTL.

Ordinary capabilities are authorized once per task/session. Do not prompt for every read, test, documentation lookup or package download.

If additional capability is necessary, extend only that capability.

### Broad authorized-project reading

Inside authorized roots, Product Agent may read:

* all project source;
* tests;
* documentation;
* configuration schemas;
* `.env.example`;
* dependency source;
* project package caches;
* build/test output;
* sanitized project logs;
* git history where present;
* generated artefacts required for debugging;
* imports, callers and related modules.

Deny:

* unrelated repositories;
* another customer’s project;
* another tenant;
* real secret files;
* `/proc/*/environ`;
* host credentials;
* host home directories outside authorized roots;
* unrelated host configuration.

For an explicitly cross-repository Youtab task, multiple roots may be granted and recorded.

### Normal engineering writes

Inside the authorized workspace/task branch, Agent may:

* edit and create files;
* add/update tests;
* refactor;
* format;
* run codemods;
* repair build configuration;
* generate migration drafts;
* build and test;
* create commits where the product contract permits.

Do not require approval for each routine edit.

---

## Intent-aware deletion

Do not prohibit all deletion.

Product Agent may delete:

* its own sandbox temporary files;
* reproducible generated artefacts;
* obsolete internal files replaced in the same change;
* explicitly requested files;
* dead code proven unreferenced and covered by tests.

It may not delete, disable, hide or weaken these to manufacture success:

* tests;
* CI checks;
* security gates;
* product functionality;
* user-facing options;
* Owner/Superadmin capabilities;
* APIs;
* migrations;
* legal attribution;
* tenant isolation;
* monitoring;
* backups;
* rollback;
* evidence;
* documentation exposing an unresolved defect.

Deletion of user data, workspace, snapshots, database state, public APIs, migrations, security controls, legal files or user-facing functionality requires explicit authenticated user/Owner action as applicable.

Every tracked-file deletion requires:

* reason;
* replacement if any;
* reference evidence;
* tests proving behavior was preserved.

The Product Agent may never delete a workspace or snapshot. It may only propose it. Workspace deletion is an authenticated user action with confirmation.

---

## Governed search, dependency and debugging access

Network default-deny does not mean permanently offline.

Provide task/session profiles:

```text
offline
docs-search
dependency-fetch
browser-test
approved-api
```

### docs-search

Allow governed read-only access to:

* official documentation;
* standards;
* research;
* public source;
* security advisories;
* package documentation;
* search/retrieval proxy.

### dependency-fetch

Allow approved package registries and required CDN endpoints.

For persistent dependencies:

1. check existing dependencies;
2. verify package identity;
3. check provenance, licence and vulnerabilities;
4. pin compatible version;
5. update lockfile;
6. update SBOM;
7. scan;
8. run relevant tests and mutation controls.

Downloads enter sandbox quarantine/cache. Package lifecycle scripts execute only inside Sandbox and receive no secrets.

Never use:

```text
curl ... | sh
curl ... | bash
wget ... | sh
```

### debugging

Inside Sandbox Agent may:

* run application/tests;
* add temporary diagnostics;
* inspect sanitized logs and stack traces;
* use debugger/profiler/tracing;
* bind sandbox-local ports;
* run browser automation;
* inspect task-related network traffic;
* perform static/dynamic analysis.

It may not:

* attach to unrelated host processes;
* inspect another container or tenant;
* dump secrets;
* disable authentication;
* expose debug ports publicly.

Search pages, repository files, README content, issues, dependencies and tool output are untrusted data. They cannot grant capability or override authenticated Owner/user intent.

Outbound DLP must prevent secrets or complete proprietary files being transmitted to arbitrary destinations.

---

## Product instruction channels

V4’s separation between human intent and file content is accepted and must be enforced mechanically.

Trusted instruction channels:

* authenticated Owner/user typed input;
* authenticated Code referral `user_reason`;
* governed system/tenant policy;
* signed/committed Runtime policy.

Untrusted data channels:

* project files;
* README files;
* comments;
* commit messages;
* issue text;
* package metadata;
* web content;
* tool output;
* model memory;
* `file_excerpt`;
* diffs.

`user_reason` must:

* originate from authenticated user keystrokes;
* be bound server-side to user, tenant and workspace;
* be protected against CSRF/replay;
* be stored separately from file content;
* be preserved verbatim;
* be audited.

File content can never populate `user_reason`.

---

## File Reader sub-agent

The V4 File Reader direction is accepted with these controls:

* read-only workspace mount;
* no write capability;
* no shell/exec;
* no package installation;
* no network by default;
* no secret access;
* tenant/workspace-scoped token;
* bounded input/output;
* same injection separation as the main Agent.

It may read broadly inside the authorized workspace to understand imports, callers, tests and configuration.

Its output contains:

```text
paths
user_reason
what_changed
why_it_matters
conflicts
injection_flags
notes
```

Only `user_reason` is authoritative user intent. All other fields remain data.

The main Agent must acknowledge delivered referrals and record how it adapted.

Do not create an independent unrestricted Agent authority. The File Reader is a constrained helper inside the same workspace governance and budget.

---

## Project lock, Stop and hash-revalidated writes

Adopt V4 D3/D4 with mechanical enforcement.

Run states:

```text
idle → running → completed | stopped
```

During a run:

* Code panel is read-only;
* whole workspace lock is held by run service;
* Stop remains visible and reachable;
* external changes must still be detected.

Stop must:

1. stop scheduling new operations immediately;
2. safely finish or atomically abort current bounded write;
3. cancel running subprocesses;
4. take partial-state snapshot;
5. flush evidence;
6. release lock;
7. report incomplete work;
8. complete within a measured bounded SLO.

Do not wait for an unbounded file operation or full task to finish.

Before every write:

* tool layer validates the last-read content hash;
* changed target rejects with conflict;
* Agent re-reads and re-plans;
* atomic write/rename is used where supported.

Detect file additions, modifications, deletions and moves.

User/external edits are authoritative. Agent may object in words but may not silently revert them.

---

## Correct the Snapshot Engine before integration

Do not integrate `scripts/snapshot_engine.py` as trusted rollback code in its current form.

The current V4 implementation and self-test do not prove all claimed guarantees.

### Required defect corrections

#### Post-snapshot user-file conflict

The self-test creates `keep.txt` before the baseline snapshot, so it is actually snapshotted. It does not test a file created by the user after the target snapshot.

Current restore behavior takes an emergency snapshot, classifies later files as added, then removes them. This can remove a post-snapshot user-created file from the active workspace, even though it remains recoverable in the emergency snapshot.

That violates D4.

Add a real test where:

1. baseline snapshot is taken;
2. user creates a new file afterward;
3. restore is requested;
4. restore refuses or preserves the user file unless explicit override is supplied;
5. no silent deletion occurs.

#### Modified user-file conflict

If the user modifies a file after a snapshot, restore must not silently overwrite it.

Track actor/provenance or authoritative hash state and fail with an explicit conflict.

#### Trusted service boundary

Snapshot/restore is a trusted Workspace service operation, not an arbitrary command available to Agent.

Agent may request snapshot/restore. The trusted service validates:

* authenticated tenant/user;
* workspace ID;
* authorized workspace root;
* lock state;
* retention policy;
* ref ownership.

#### Safe refs and paths

Require:

* full immutable object IDs internally;
* validation that refs belong to that workspace store;
* no option-like refs;
* canonical workspace roots;
* no path escape;
* `-z`/safe parsing for unusual filenames;
* no shell invocation;
* sanitized git environment;
* hooks and filters disabled;
* concurrency lock;
* size/file-count limits.

#### Multi-tenant secure storage

Snapshot storage must be:

* scoped by tenant/user/workspace ID;
* encrypted at rest;
* inaccessible across tenants;
* outside user-visible project tree;
* covered by retention/deletion policy;
* auditable;
* included in user deletion;
* protected against unbounded growth.

Ingress must detect and quarantine real secret files. Do not silently snapshot raw `.env`, credentials or private keys into an ungoverned bare repository.

#### Excludes

Do not blindly exclude directories such as `vendor`, `dist` or `build` if they are part of the user’s required project state.

Exclusion policy must be workspace/project-specific and disclosed. A rollback that silently omits required files is not complete.

### Rollback drill correction

Never deliberately break:

* production;
* the user’s live original project;
* another tenant’s workspace;
* an irreplaceable project.

The release rollback drill must use:

* a disposable exact clone;
* a dedicated test workspace;
* or a controlled fixture.

The drill must prove restore behavior without risking user or production data.

### Required snapshot tests

At minimum:

* modified Agent file restored;
* Agent-deleted file restored;
* Agent-added file removed;
* post-snapshot user-created file preserved/refused;
* post-snapshot user-modified file conflict;
* emergency snapshot restorable;
* no `.git` inside project;
* tenant isolation;
* unauthorized workspace refused;
* malicious ref refused;
* traversal/symlink escape refused;
* concurrent snapshot/restore serialized;
* unusual filenames handled;
* secrets excluded/quarantined or encrypted by policy;
* retention/user deletion removes snapshot state;
* mutation controls turn tests red.

Do not claim rollback proven merely because the archive’s current 14 checks pass.

---

## Staged review, diff and egress

Default:

```text
apply_policy: staged
```

Agent changes remain in workspace staging.

User reviews:

* per-file diff;
* full content for new files;
* removed content for deletions;
* select-all or subset approval;
* user edit before approval;
* unread state.

Agent has no capability to apply staged work to external targets.

### Direct mode

Only explicit, recorded, reversible and scoped.

### Download

User chooses:

* full archive;
* changed files;
* patch.

### PR

Product Agent may:

* prepare branch;
* commit;
* prepare PR title/body.

Product Agent may not call the API that opens or merges the PR.

The Code panel, using the authenticated user’s authority, opens the PR.

Never push default branch. Never auto-merge.

### SFTP/storage

Requires:

* explicit authenticated user action;
* remote snapshot first;
* drift detection;
* exact output file report;
* governed short-lived credentials;
* no Agent access to raw remote credentials.

---

## Code app contract

Adopt V4 C0–C7 after applying this mandate’s corrections.

The Code app remains separate from Chat. Do not modify or replace Chat.

The existing Web OS Agent launcher remains a separate Agent product launcher.

Code may invoke Agent through a governed workspace/run action, but Agent, Code and Chat remain distinct surfaces.

### Required backend contracts

Workspace-scoped, tenant-validated and server-authorized:

```text
GET    /workspaces/{id}/tree
GET    /workspaces/{id}/file
PUT    /workspaces/{id}/file
POST   /workspaces/{id}/stop
POST   /workspaces/{id}/queue
POST   /workspaces/{id}/read
POST   /workspaces/{id}/run
POST   /workspaces/{id}/export
POST   /workspaces/{id}/pr
DELETE /workspaces/{id}
```

Require:

* server-side tenant isolation;
* RBAC;
* CSRF protection where applicable;
* exact workspace authorization;
* canonical path validation;
* symlink escape rejection;
* file/zip bomb limits;
* rate limits;
* audit events;
* idempotency for state-changing actions where needed;
* no provider/model leakage;
* no secret response fields.

### Transport

* authenticated WebSocket for live events;
* monotonic sequence numbers;
* bounded replay/resume;
* heartbeat;
* SSE fallback;
* REST for state-changing actions;
* Stop over REST and WebSocket, idempotently.

### Live activity

Events derive from actual tool/filesystem/process activity, not model narration.

### Unread marks

Stored server-side per:

```text
user + workspace + path
```

### Multi-tab

One workspace lock and broadcast group across tabs.

Never discard unsaved buffers silently.

### Editors

* Monaco for desktop/large tablet;
* CodeMirror 6 for phone/touch;
* one editor abstraction;
* real phone verification;
* Stop always reachable.

Do not ship live editing before hash-revalidated writes are mechanically enforced.

---

---

## Future ADRs and implementation order

After the immediate control-plane isolation workstream is genuinely green, create ADRs for:

1. trusted control plane versus untrusted Agent sandbox;
2. capability envelope and task network profiles;
3. Secret Manager, Secret Broker and provider proxy;
4. governed egress and DLP;
5. persistent Workspace and ingress adapters;
6. Snapshot/rollback service and conflict semantics;
7. Code app transport, lock and live-event contract;
8. staged/direct apply and egress authority.

ADRs must contain threat model, alternatives, chosen design, performance impact, migration, rollback and executable acceptance criteria.

After each ADR slice is green, continue implementation in this order:

1. secret broker/provider proxy;
2. capability and policy engine;
3. active-workspace Sandbox lifecycle;
4. snapshot service corrections;
5. hash-revalidated Workspace writes;
6. persistent Workspace and ingress adapters;
7. staged review/approval;
8. Code backend transport and events;
9. Code frontend/editor;
10. reader sub-agent/referrals;
11. egress;
12. retention/deletion;
13. performance and adversarial qualification.

Do not start an unrelated phase while the current PR is red.

---

## Sandbox performance

Do not create a container per command.

Use:

* one active sandbox per task/session/workspace;
* strict tenant/user/workspace binding;
* bounded idle TTL;
* pre-pulled immutable images;
* bounded warm pool before assignment;
* named dependency caches;
* incremental builds;
* persistent provider-proxy connections;
* resource profiles.

Profiles:

```text
light
code
build
browser
high-memory
```

Measure before and after:

* cold-start p50/p95;
* warm command p50/p95;
* test/build duration;
* filesystem I/O;
* provider-proxy latency;
* time to first token;
* CPU/memory;
* concurrency;
* failure/timeout rates.

Initial budgets:

* warm execution overhead ≤15% p95;
* non-model task regression ≤20% p95;
* provider-proxy overhead ≤10% p95;
* no repeated dependency installation per warm task;
* no capability/test reduction to improve performance.

If budgets fail, optimize pooling, images, caching, filesystem and connections. Do not restore host networking or expose secrets.

---
