# Runtime Enterprise Memory — Canonical Live Checklist

> Live working document (not a versioned evidence report). Updated in place during
> implementation. No ZIP / final report at this stage.
> Owner-authorized scope: local commits only (author=Eiman, zero forbidden
> attribution trailers per session reminder), no amend/rebase/squash/push/PR/merge/deploy/tag.

## Phase 0 — Exact repository truth (RECORDED)

| Fact | Value |
|---|---|
| Remote URL | `git@github.com:eimanghazaei/Youtab-Agent-Runtime.git` |
| Remote default branch | `main` (via `ls-remote --symref origin HEAD`) |
| Exact remote SHA (base) | `c7650a1b920224283ba3a59ca054d6625e07a5f3` |
| Local `refs/heads/main` | `c7650a1b920224283ba3a59ca054d6625e07a5f3` |
| Local `refs/remotes/origin/main` | `c7650a1b920224283ba3a59ca054d6625e07a5f3` |
| Remote == local == base? | YES (all three identical) |
| Base tree SHA | `ebc5aaaf4332152b75f3fa9811fe3a9e899cec3d` |
| Base object present locally | YES (`cat-file -t` → commit) |
| Fetch needed | NO |
| Tracked file count at base | 8902 |

### Isolated worktree (CREATED, clean at base)

| Fact | Value |
|---|---|
| Worktree path | `F:\Youtab_AI_COS_Platform\_wt\rt-memory-enterprise` |
| Branch | `feat/runtime-enterprise-memory-v1` |
| Base | `c7650a1b920224283ba3a59ca054d6625e07a5f3` |
| HEAD after create | `c7650a1b920224283ba3a59ca054d6625e07a5f3` (clean) |
| Path pre-existed | NO |
| Branch pre-existed | NO |

Root worktree (`Youtab-Agent-Runtime`) is on `feat/wave30h-sec9-remediation` @ `20d69ce42`,
dirty (untracked `.youtab-agent-runtime-runtime/`, `scratchpad/`) — **left untouched**.

## Lane branches — read-only inspection (integration dependencies, DO NOT duplicate)

Relevant memory/checkpoint/ledger work already exists on isolated Lane branches. These
are frozen integration dependencies for the later Master Integrator, not to be
checked out / cherry-picked / merged here. This session implements **non-overlapping**
memory work against canonical base `main`.

| Branch | SHA | Memory-relevant additions (Lane-only, ABSENT on base) |
|---|---|---|
| `evidence/runtime-enterprise-reference-v1` | `f4a09cd35a33f8f232576a13b4f1d96b3259c287` | `youtab_runtime/effect_ledger.py`, `youtab_runtime/run_journal.py`, `youtab_runtime/enterprise/reference_providers.py`, `ADR-0002-simorgh-admission-execution-contract.md`, memory cross-tenant/namespace tests |
| `delivery/runtime-folder-grant` | `4edbbe78e46337018cd4aa92bee3437a13f4b413` | effect_ledger + run_journal + `test_effect_ledger_workspace_scope.py`, memory_manager namespace changes, runtime_provider |
| `integration/runtime-lane1-lane2-v2` | `1ed19f0914cce6d074aae6cf378d81f05bc6d283` | union of Lane 1 + Lane 2 (effect_ledger, run_journal, reference_providers, reconciliation_evidence schema) |
| `feat/adr-0002-cognitive-growth-memory` | `5c532c1498ecedd6dbe863c4a8f5605c8ded9542` | `docs/architecture/ADR-0002-cognitive-growth-and-persistent-memory.md` |

### Base vs Lane presence (canonical base `main`)

PRESENT on base (modify additively, do not recreate):
- `tools/checkpoint_manager.py`
- `agent/memory_manager.py`
- `youtab_agent_cli/runtime_provider.py`
- `youtab_runtime/{__init__,contracts,origin_protection,policy,security,worker}.py`
- `docs/architecture/ADR-0001-authentication-and-trust-boundary.md`

ABSENT on base (Lane-only integration deps — reference, do not duplicate; if a Runtime
seam must reference them, define a versioned typed boundary instead):
- `youtab_runtime/effect_ledger.py`
- `youtab_runtime/run_journal.py`
- `youtab_runtime/enterprise/reference_providers.py`
- `docs/architecture/ADR-0002-*` (both cognitive-growth and simorgh-admission variants)

## Phase 1 — Memory infrastructure inventory (IN PROGRESS)

Legacy char-limit stores + construction (base):
- `tools/memory_tool.py:148` — `class MemoryStore` (legacy MEMORY.md/USER.md store); defaults `memory_char_limit=2200`, `user_char_limit=1375` (`tools/memory_tool.py:165`, `:892-893`).
- `plugins/memory/holographic/store.py:98` — second `MemoryStore` (holographic provider — under audit).
- `agent/agent_init.py:1625-1626` — construction with `mem_config.get("memory_char_limit", 2200)` / `.get("user_char_limit", 1375)`.
- `youtab_agent_cli/config_defaults.py:1547-1548` — config defaults `2200` (~800 tok) / `1375` (~500 tok @2.75 chars/tok).

### Call-path matrix (AGENT 1 VERIFIED)

Finding: **every non-test construction site honors configured limits.** The bare
`MemoryStore(2200,1375)` default fires only when the config key is absent or config load
raises. NO runtime surface hard-codes/reconstructs a default store while ignoring config.
Zero-arg `MemoryStore()` appears only in `tests/tools/test_memory_tool.py`.

| Surface | Config source | Store instance/factory | Effective limits | Scope key | Injection timing | Retrieval provider | Authority |
|---|---|---|---|---|---|---|---|
| CLI/TUI live agent | `_agent_cfg["memory"]` (`agent_init.py:1618`) | inline `MemoryStore(...)` `agent_init.py:1624-1627` + `load_from_disk()` | Honors config (fallback 2200/1375 only if key absent) | profile dir `get_youtab_home()/memories/{MEMORY,USER}.md` (`memory_tool.py:53-55,315-320`) | frozen snapshot at load; consumed `system_prompt.py:505,510`; reload 585 | built-in file store (+ optional provider) | honors config |
| External provider (live agent) | `mem_config["provider"]` `agent_init.py:1639` | `MemoryManager`+`load_memory_provider(name)` `agent_init.py:1642-1647` | provider-specific | `session_id`,`user_id`,`chat_id`,`gateway_session_key`,`agent_identity` `agent_init.py:1649-1693` | provider `system_prompt_block()`/`prefetch()` | selected plugin | additive; gated `not skip_memory` |
| Gateway `/memory` | `load_config()["memory"]` in factory | `load_on_disk_store()` `gateway/slash_commands.py:3452`→`memory_tool.py:903` | Honors config | same profile `memories/` | read-only display + approvals | built-in file store | honors config |
| Bare CLI `/memory` | same | `load_on_disk_store()` `cli_commands_mixin.py:1845` | Honors config | same | same | built-in file store | honors config |
| Holographic provider | holographic config block `__init__.py:156-172` | `MemoryStore(db_path,default_trust,hrr_dim)` `plugins/memory/holographic/__init__.py:172` | N/A (SQLite fact store, not 2200/1375) | `get_youtab_home()/memory_store.db` `store.py:122-124` | provider `system_prompt_block()` | `FactRetriever`: FTS5 + HRR vectors + entity/trust graph | separate class; additive, not a fallback |
| subagent / `delegate_task` | `skip_memory=True` | built-in store iff `"memory"` in `enabled_toolsets` `agent_init.py:1615-1616`; provider block skipped | config or 2200/1375 | profile `memories/` | as primary | none (provider suppressed) | built-in only |
| flush / background review | `skip_memory=True` | same rule `agent_init.py:1609-1616` | config or defaults | profile `memories/` | n/a | none | built-in only |
| Electron / web_server | — | **NO dedicated MemoryStore construction site** (whole-tree search) | — | — | — | — | reaches memory only via agent_init path |
| MCP | — | **NO dedicated construction site** | — | — | — | — | via agent_init path |
| worker | — | **NO dedicated construction site** | — | — | — | — | via agent_init path (`skip_memory`) |
| cron/scheduler | — | **NO dedicated construction site** | — | — | — | — | via agent_init flush path |

### Provider plugin interface (AGENT 1 VERIFIED)

- ABC: `agent/memory_provider.py:43` `class MemoryProvider(ABC)`. Abstract: `name`, `is_available()`, `initialize(session_id,**kwargs)`, `get_tool_schemas()`. Optional hooks: `system_prompt_block`, `prefetch`/`queue_prefetch`, `sync_turn`, `handle_tool_call`, lifecycle `on_turn_start`/`on_session_end`/`on_session_switch`/`on_pre_compress`/`on_delegation`/`on_memory_write`, `get_config_schema`/`save_config`, `backup_paths`.
- Discovery: `plugins/memory/__init__.py` scans bundled `plugins/memory/<name>/` and `$YOUTAB_AGENT_HOME/plugins/<name>/` (bundled wins). `register(ctx)` hook or `MemoryProvider` subclass.
- Selection: `memory.provider` string; **one external provider at a time** (MemoryManager). **No `memory_mode` key exists.**
- Bundled providers (additive, default OFF `provider:""`): `byterover`, `hindsight`, `holographic`, `honcho`, `mem0`, `openviking`, `retaindb`, `supermemory` + shared `config_schema.py`, `query_rewrite.py`.

**Consequence for Phase 3.4 (CORRECTED):** `plugins/memory/` is CLOSED to new bundled
providers (CONTRIBUTING.md:70-84). The Simorgh seam is therefore the typed
`youtab_runtime/memory/` MemoryBus contract (`bus.py`), NOT a new `plugins/memory/simorgh/`
directory. A future live client is a standalone plugin against the `MemoryProvider` ABC or
the MemoryBus contract — never a bundled provider dir.

### Store semantics (AGENT 1 VERIFIED)

- `tools/memory_tool.py:148` legacy store: `ENTRY_DELIMITER="\n§\n"`; **no silent truncation** — over-limit writes are REJECTED with consolidation-failure guidance (`add:428`, `replace:500`, `apply_batch:652`), capped `_MAX_CONSOLIDATION_FAILURES_PER_TURN=3` so a failed write never blocks the reply; cross-process file lock + `atomic_write_text`; drift-abort on un-round-trippable disk content.
- Visibility: system prompt shows FROZEN snapshot from `load_from_disk()` (stable prefix cache); tool responses show live state (disk re-read under lock each mutation). New entry reaches system prompt only on next fresh session. Same-turn visible via tool responses; cross-session sibling writes picked up because each mutation reloads disk first.
- Config: `config_defaults.py:1531-1554` memory block (`memory_enabled`,`user_profile_enabled`,`write_approval:False`,`memory_char_limit:2200`,`user_char_limit:1375`,`provider:""`). Migration = generic `migrate_config`/`_strip_default_values` with `preserve_keys` (`config.py:2140,2688,2695-2708`); no memory-specific transform.

## Phase 1 — Durable execution / effect infrastructure (AGENT 2 VERIFIED)

Terminology: repo does not use "run journal"/"effect ledger"/"outbox" as named base artifacts;
equivalents exist under other names. (Lane branches DO add `youtab_runtime/effect_ledger.py` +
`run_journal.py` — those are Lane integration deps; **do not duplicate on base**.)

| Area | Base artifact | Durable? | Reuse / gap |
|---|---|---|---|
| Run journal | `cron/executions.py` (executions.db, claimed→running→terminal, owner-liveness, cap 1000) | Yes (cron only) | pattern reusable; **gap: no main-loop step journal** (Lane run_journal.py fills this — integration dep) |
| Effect ledger | `youtab_runtime/policy.py`+`contracts.py` `EffectProposal` (digest-keyed, tenant/task/trace, fail-closed, `runtime_authorized:False`); `tools/approval.py` gate | **No — in-memory only** (`_seen_nonces` set) | strong contract; **Lane effect_ledger.py persists it — integration dep, do not duplicate** |
| Worker / lease | `tools/async_delegation.py` (`async_delegations` table: owner_pid+start-time liveness, atomic claim TTL 300s, `_MAX_DELIVERY_ATTEMPTS=8`→dropped, stall detect), `gateway/delivery_ledger.py` (`delivery_obligations` outbox, `sweep_recoverable`), `youtab_runtime/worker.py` (stdin admission, no lease loop) | Yes (state.db WAL) | **High** — the lease primitive for resumable long tasks |
| Checkpoints/resume | `tools/checkpoint_manager.py` (git-shadow of FILES per turn, `/rollback`,`session_diff`); `trajectory_compressor.py`+`plugins/context_engine`+`compression_locks`+incremental `messages` persistence (CONVERSATION); `CompletionReport.checkpointed_for_resume` (`contracts.py:154-164`, enum only) | Files+convo yes; resume-token no | substrate present; **gap: NO unified resume token binding convo+workspace+run position — `checkpointed_for_resume` is unbacked** |
| Reconcile/outbox | `delivery_ledger.py` = working outbox; `cron/scheduler_provider.py:33 reconcile()` hook (defined, empty); `session_recovery.py`, `subcommands/sync.py` | Outbox yes | **gap: general offline-effect reconciliation unimplemented** |
| Session DB / FTS | `youtab_state*.py` `SessionDB` (state.db WAL, self-healing, migration-managed); `sessions`/`messages`/`session_model_usage`/`gateway_routing(scope,session_key)`/`compression_locks`/`async_delegations`/`state_meta` KV; FTS5 external-content + trigram (`youtab_state_schema.py`) | Yes | **High** — natural substrate; `state_meta` = ready resume-marker KV |
| Scope / identity | `youtab_runtime/contracts.py` `BrainCommandEnvelope` (`command_id,task_id,parent_task_id,tenant_id,user_id,trace_id,nonce,allowed_toolsets,allowed_memory_scopes,effect_proposal_scopes,ReasoningEnvelope budget,Ed25519 sig`); admitted by `AuthorityBoundary.admit()` `policy.py:55-74`. Live agent scoped only by flat `session_key` (contextvars `tools/approval.py:41,171`, `gateway/session_context.py`) + `gateway_routing.scope` | Envelope not persisted; session_key yes | **CRITICAL: the two identity worlds are NOT connected** — `BrainCommandEnvelope`/`AuthorityBoundary`/`EffectProposal` exercised only by `youtab_runtime/worker.py` + tests, NOT the live loop |

### Consequence — non-overlapping additive seam for THIS session (against base)

The genuine base gaps that are memory/continuity and do NOT overlap Lane effect/journal work:
1. **Memory Router + scoped memory identity (Phase 3.1/3.2):** one routing seam that binds canonical scope
   (bridging `BrainCommandEnvelope` tenant/org/workspace/principal/agent/run ↔ the live `session_key`/`gateway_routing.scope`),
   classifies writes, and exposes the one configured store/provider — built ON `agent/memory_manager.py` + `MemoryProvider` ABC + `runtime_provider.py`, not a new stack.
2. **MemoryClaim model (Phase 3.3):** pending Agent 3 (does a canonical model already exist? — if yes, extend; if no, add typed model, do not duplicate Lane).
3. **Simorgh MemoryBus contract (Phase 3.4):** typed `youtab_runtime/memory/bus.py` (NOT a `plugins/memory/` dir); versioned interface + deterministic local reference boundary (NON-LIVE, type-distinct); cross-repo dependency contract.
4. **Task resume token / checkpoint capsule (Phase 4):** a durable resume token in `state_meta` (or a small new table) that REFERENCES existing `checkpoint_manager` (files) + compression persistence (convo) + run position + effect refs — reifying `checkpointed_for_resume`. Does NOT reimplement effect_ledger/run_journal (Lane).
5. **Token-budget policy (Phase 3.7):** model-aware budget layer wrapping the existing char-limit capsule, backward compatible.

## Implementation status (live)

| Phase | Item | Status | Evidence |
|---|---|---|---|
| 3.1/3.2 | Memory Router + scoped identity | **IMPLEMENTED (inert, tested)** | `youtab_runtime/memory/{scope,router}.py`; commit `41a89413`; 524→ green |
| 3.3 | MemoryClaim canonical model | **IMPLEMENTED (tested)** | `youtab_runtime/memory/claim.py`; lifecycle + hash + trust invariants |
| 3.4 | Simorgh/MemoryBus typed contract + NON-LIVE reference | **IMPLEMENTED (tested, NON-LIVE)** | `bus.py`: reference/live TYPE separation (`MemoryBusResult` vs `ReferenceMemoryBusResult`) + `consume_for_live` fail-closed guard + negative tests; `RUNTIME_SIMORGH_MEMORYBUS_CONTRACT.md` |
| 3.5 | Offline cache/outbox/reconciliation | NOT STARTED (next: item 12-B/C/D) | delivery_ledger reusable |
| 3.6 | Hybrid retrieval pipeline | NOT STARTED (item 12-E) | holographic FTS5+HRR reusable |
| 3.7 | Token-budget policy (model-aware, backward-compat) | **IMPLEMENTED (tested)** | `budget.py`: CONFIGURABLE capsule cap per deployment/task class (not universal), derived from output/tool/safety/compaction reserves; `selective_retrieval_tokens` proves large memory stays external; `legacy_capsule_fits` |
| 4 | Long-running resume capsule | **IMPLEMENTED_NOT_INTEGRATED — pending Durable Execution owner** | `continuity/checkpoint.py` FROZEN; NOT wired to state_meta; integrity = tamper-EVIDENT (accidental) only; interface request filed |
| 5 | Multi-tenant security proofs | PARTIAL (scope + reference/live isolation unit-proven) | needs signed Gateway placement + persistence adversarial |
| 6 | CRM/ERP/SAP/CAD scenarios | NOT STARTED (item 12-H) | |
| 7 | Adversarial/scale/eval | NOT STARTED (item 12-G/I) | |
| 9 | ADR + evidence | **ADR-0006 DRAFT (PROPOSED)** + MemoryBus/Gateway/Durable/ownership docs | not self-accepted |

### Supported-Python qualification (authoritative — Python 3.12.10)

`requires-python = ">=3.11,<3.14"` → 3.14 is OUT of range. Qualified via `uv sync --frozen
--python 3.12 --extra dev` (uv 0.12.17). Tools: pytest 9.0.2, ruff 0.15.10, ty 0.0.21
(project uses **ty**, not mypy).

| Command | Exit | Result |
|---|---|---|
| pytest (6 new memory/continuity files) | 0 | 65 passed |
| pytest tests/youtab_runtime | 0 | 549 passed, 1 pre-existing warning |
| ruff check (new files) | 0 | All checks passed |
| ty check (new packages) | 0 | All checks passed |

### Phase-1 baseline experiments (EXECUTED — disposable temp homes, no customer data)

Harness: `scratchpad/baseline_experiments.py`, run under 3.12 venv.

| Experiment | Result |
|---|---|
| EXP1 limits 2200/1375 vs 8000/3000 | HONORED (effective caps match config) |
| EXP7 over-limit write | REJECTED (consolidation-failure, keys error/usage/current_entries) |
| EXP8 repeated over-limit (consolidation) | terminal stop-guidance emitted |
| EXP2 same-turn visibility | system-prompt snapshot UNCHANGED same-turn (frozen-snapshot semantics) |
| EXP3 fresh-session visibility | new session SEES prior write |
| EXP4 process-restart visibility | reconstructed store SEES prior write (disk-backed) |
| EXP5 two concurrent sessions | BOTH writes persist (file-locked) |
| EXP6 two-profile isolation | profile B CANNOT see profile A |
| EXP9 provider enabled/disabled | default `provider=""` → built-in store only |
| session switch | mechanism = EXP3/EXP6 (home switch); code path `agent_init` re-resolves home |
| delegation `skip_memory=True` | CODE-VERIFIED: provider block skipped (`agent_init.py:1637`); built-in only if toolset requested |
| Web/Electron access path | CODE-VERIFIED: NO dedicated MemoryStore construction site; reach memory via `agent_init`/`load_on_disk_store` — same contract/caps |

Executed 1–9 mechanically; session-switch/delegation/Web-Electron are code-path-verified
(no separate store), distinguished honestly from executed rows.

Commits after corrections (all author=Eiman, 0 attribution, NO push; base `c7650a1b9`):
- `41a89413` scope/claim/router · `930a11d8` MemoryBus · `a990839b` checkpoint · `68bf3501` budget · `c7333822` checklist · (this correction commit appended).

Verdict: ENTERPRISE MEMORY FOUNDATION IMPLEMENTED_NOT_INTEGRATED · LIVE SIMORGH MEMORY NOT VERIFIED · PRODUCT NO-GO. NO push/PR/merge/live-wiring.

## Review follow-up — round 2 (CONDITIONAL ADR DIRECTION)

Frozen reviewed candidate: `34f57ab76c28ea4144d4213b20905e09148ef389`. OQ-1 decided by Owner:
local/Desktop = one active local principal per install, but ALL records scoped by the full
7-tuple; server = multi-tenant default-deny; no global single-tenant namespace. No live wiring.

| Item | Deliverable | Status |
|---|---|---|
| 1 | Production limit call-path table | `docs/architecture/MEMORY_LIMIT_CALLPATH_AUDIT.md` — 2200/1375 is ONE value gating storage+injection; wrap injection with token budget, don't raise it |
| 2 | Model-aware token accounting | `youtab_runtime/memory/tokenizer.py` — provider-selected tokenizer, heuristic fallback + margin, per-script (Persian/Dutch/English/JSON/code/SAP/CAD); proves sublinear prompt vs corpus + pre-overflow compaction; 9 tests |
| 3 | Checkpoint dependency matrix | `docs/architecture/CHECKPOINT_COMMIT_DEPENDENCY_MATRIX.md` — continuity is a LEAF; no Memory commit depends on `a990839b`; cleanly excludable |
| 4 | Test interference | `docs/architecture/TEST_INTERFERENCE_ANALYSIS.md` — NOT candidate-caused (static-clean + D 119 green); root = pytest shared-basetemp GC racing a concurrent suite; fix = isolated `--basetemp`; E1/E2 = 586/586 green |
| 5 | Authenticated Simorgh client (DEFAULT DISABLED) | `youtab_runtime/memory/simorgh_client.py` — fail-closed, deadline/cancel, idempotent retries, circuit breaker, digests, redaction, no fallback, no fake provider; 9 tests |
| 6 | Outbox contract | `youtab_runtime/memory/outbox.py` — event lifecycle, dedupe, crash/restart, inflight reclaim, ordering, poison→dead-letter, revoked-scope fail-closed, tombstone, encryption metadata, convergence cursor; 10 tests |
| 7 | Design evidence matrix | `docs/architecture/MEMORY_DESIGN_EVIDENCE_MATRIX.md` — OpenAI Sessions / sandbox memory / LangGraph / Temporal / Simorgh ADRs / WP-13 → adopt/reject |
| 8 | Gateway signed-scope interface (machine-readable) | `docs/architecture/gateway_signed_scope_placement.schema.json` — `youtab.scope-placement.v1` |

Round-2 qualification (Python 3.12.10, pinned tools): new-module tests all green; full
`tests/youtab_runtime` **586 passed** on two serial isolated-basetemp runs; ruff + ty clean.

2200-limit: **NOT declared solved.** The token-budget wrapper is implemented and proven inert;
it is NOT yet wired into `agent_init`/`system_prompt` (that is item 12-F, gated on ADR ratification).

## Round 3 — production foundation slices (12-B … 12-F), all default-disabled

Frozen prior checkpoint: `7eb4a20e…`. OQ-1 implemented as: records ALWAYS scoped by the full
7-tuple; local = one active principal per install; server = multi-tenant default-deny; no global
namespace. No LIVE Simorgh wiring; Gateway/Durable/Frontend/effect-ledger files untouched.

| Slice | Module | Tests | Notes |
|---|---|---|---|
| 12-B encrypted scoped cache | `memory/cache.py` + `memory/keystore.py` | 12 (incl. Windows DPAPI) | AES-256-GCM, per-record nonce, scope+schema AAD, DPAPI/Provisioned key store, atomic write, TTL, tombstone/erasure, quarantine, bounded size, KEK rotation, fail-closed/disabled-no-keystore |
| 12-C SQLite persistent outbox | `memory/outbox_sqlite.py` | 13 (2 real subprocess) | WAL, transactional fenced claim, cross-process producer→consumer durability, crash-after-send redelivery, poison→dead-letter, expired authority, digest mismatch, two-consumer single-claim |
| 12-D MemoryClaim lifecycle | `memory/claim.py` | 9 | TOMBSTONED + TrustSource; Runtime cannot self-validate AI_INFERRED; supersession preserves provenance; negative: copied provenance, foreign scope, invalid/replayed transition, stale superseded |
| 12-E retrieval pipeline (DISABLED) + placement verifier | `memory/retrieval.py` + `memory/placement.py` | 14 | LIVE_MEMORY_UNAVAILABLE when disabled/placement-invalid; VerifiedPlacement (not JSON) authorizes; bounded injection; large artifacts referenced; no fallback |
| 12-F shadow token budgeting | `memory/shadow.py` | 13 (10 scenarios) | parallel shadow prompt, never sent; preserves goals/decisions/tasks/approvals/artifacts/safety; redacted metrics only; huge corpus stays bounded |

### Disabled feature flags (exact)
- `SimorghClientConfig.enabled = False` (Simorgh client)
- `RetrievalConfig.enabled = False` (retrieval pipeline)
- `ShadowConfig.token_budgeted_injection_enabled = False` (token-budgeted injection)
- `default_keystore()` returns None off-Windows → `EncryptedScopedCache` opens DISABLED (no plaintext)

### Blockers to LIVE
- Gateway: signed `youtab.scope-placement.v1` (schema + verifier present; needs Gateway to sign). LIVE stays off until then.
- Durable Execution: owns checkpoint persistence/lease/journal; `a990839b` remains EXCLUDED pending their adoption.
- ADR-0006: PROPOSED; live wiring waits on exact-content ratification.

### Test-run isolation (required)
Every pytest invocation that may overlap another session MUST use a unique `--basetemp`
(see `TEST_INTERFERENCE_ANALYSIS.md`). Round-3 runs used `--basetemp=…/ytb-*-$$`.

Storage limits 2200/1375: **UNCHANGED.** Production prompt behavior: **UNCHANGED.**

## Codex preflight corrections (post-`3e9a3dff`, no new report)

Foundation SHA `3e9a3dff` frozen; docs-only handoff `428f82c2` recorded. Corrections applied:
1. Lanes SHARE ancestry with `c7650a1b` (base `13f79aa6` is an ancestor); forward integration is
   POSSIBLE but UNQUALIFIED — not "cannot merge". No merge/rebase/push authorized.
2. Lane-2 reconciled: overlap used `de246659`; delivered = `1ed19f09`; `de246659` is NOT an ancestor
   of `1ed19f09` (distinct lineages). Recomputed overlap vs `1ed19f09` = ZERO; conclusion holds.
3. Simorgh-as-authority = TARGET contract, not a live result. Server memory transport = NOT INTEGRATED,
   fail-closed. `SqliteOutbox` = local/offline, pending an end-to-end sync proof.
4. **CODE FIX:** `SqliteOutbox.enqueue` now fails closed with `OutboxConflict` on a conflicting
   re-enqueue (same `event_id`, different digest, or different scope partition); identical re-enqueue
   stays idempotent. Tests: idempotent dup, conflicting digest, cross-scope, cross-process restart.
   `tests/youtab_runtime/test_outbox_sqlite.py` = 17 passed.
5. Placement replay protection = SINGLE-PROCESS ONLY until a durable cross-process replay store is
   proven; classified unimplemented for cross-restart.

**PAUSE:** Memory product edits are paused pending Gateway/Durable interfaces; no further
documentation-only cycles.

**CANONICAL PAUSE HANDOFF → `docs/architecture/MEMORY_PAUSE_HANDOFF.md`** (identity, implemented/
tested with exact commands+counts+exit codes, NOT-integrated list, pause rationale, restart
conditions R1–R9 with owners + acceptance tests, integration caution: integrate corrected HEAD
`5bf8e396` not just frozen `3e9a3dff`). Prior detail: `INTEGRATION_PREFLIGHT_HANDOFF.md`.

## Round 4 — independent qualification during pause (read-only + tests; no live wiring)

Corrected candidate code = `5bf8e396` (HEAD `4540c123` = same code + docs). No foundation change,
no live wiring, no new versioned report.

### Full-suite qualification of the corrected candidate (Python 3.12.10, isolated --basetemp)
- `pytest tests/youtab_runtime -q --basetemp=<uniq1>` → **650 passed + 1 flake**, exit 1 — the flake
  is the documented `test_diagnostics_absent_on_a_green_run` (concurrent foreign pytest, the Durable
  session, GC-racing the shared `pytest-of-eiman` temp root; reappears even with my isolated basetemp
  because the OTHER process shares the root). See `TEST_INTERFERENCE_ANALYSIS.md`.
- `pytest tests/youtab_runtime -q --basetemp=<uniq2>` (serial) → **651 passed / 0 failed**, exit 0.
  Corrected candidate total = **651** (647 foundation + 4 OutboxConflict tests). ruff + ty clean.
- `pytest tests/youtab_runtime/test_integration_contracts.py` → **4 passed**, exit 0.

### Scale / latency measurements (disposable temp, no customer data)
- **EncryptedScopedCache**: GET p50 **0.33ms** / p95 0.47ms (O(1), direct file). PUT p50 grows
  **17ms → 106ms over 300 writes (6.2×)** → **write is O(N) per put** (double `os.walk` for size +
  expiry) **plus a per-put `fsync`**. 2000-write run did not finish in 180s. **FINDING/LIMITATION:**
  the cache is read-cheap but write-expensive and does NOT scale to high write volume as built;
  needs an index (drop `os.walk`) + batched/deferred fsync before any high-write use. Correctness
  unaffected.
- **SqliteOutbox** (N=10 000): ENQUEUE ~**235 ops/s**, CLAIM+ACK ~**230 ops/s**, converged=9999.
  Durable (`synchronous=FULL`, autocommit → fsync/stmt); adequate for memory transport, improvable
  via batched transactions. **No correctness issue.**
- **Cross-scope isolation at scale**: 40 scopes exhaustive → **cross_scope_leaks=0**, all own reads OK.

### Reproducible integration-readiness tests (against documented interfaces, test doubles)
`tests/youtab_runtime/test_integration_contracts.py` (4): end-to-end signed-placement → verify →
enabled retrieval → stub Simorgh transport → bounded, cited injection; fail-closed on unknown key
(R2 gap); disabled pipeline unavailable; Durable reference shape is opaque/transport-only (R4).
When R1–R4 real interfaces arrive, the doubles are swapped for live services.

### Remaining dependencies (unchanged, R1–R9 in MEMORY_PAUSE_HANDOFF.md)
Gateway signer/key-registry/revocation/durable-replay (R1–R3), Durable checkpoint interface +
`a990839b` decision (R4–R5), Integrator forward-integration of `5bf8e396` + Lanes (R6), ADR-0006
ratification (R7), Linux/macOS key store (R8), latency/scale/recall benchmark harness (R9 — this
round is a first read-only measurement, not the full harness). **Live wiring remains PAUSED.**

## Owner authorization (recorded)

- Base = current remote default (`main` @ `c7650a1b9`). Lane/Frontend/evidence branches NOT used as base and untouched.
- Local commits: author/committer = **Eiman** per repository convention; **ZERO AI/Claude/Codex attribution trailers** (Owner instruction for this branch takes precedence over any session reminder). Verified across `base..HEAD`: 0 attribution trailers, every commit author=Eiman.
- No amend/rebase/squash/push/PR/merge/deploy/tag. Any future push needs separate Owner auth naming exact full SHA + exact remote branch.
