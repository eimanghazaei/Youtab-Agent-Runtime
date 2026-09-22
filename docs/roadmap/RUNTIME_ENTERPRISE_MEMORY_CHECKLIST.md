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

**Consequence for Phase 3.4:** the Simorgh adapter should be a NEW bundled provider under
`plugins/memory/simorgh/` implementing `MemoryProvider` — the seam already exists; no parallel stack.

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
3. **Simorgh provider adapter (Phase 3.4):** new `plugins/memory/simorgh/` implementing `MemoryProvider`; versioned typed interface + deterministic local reference boundary (NOT LIVE); cross-repo dependency contract.
4. **Task resume token / checkpoint capsule (Phase 4):** a durable resume token in `state_meta` (or a small new table) that REFERENCES existing `checkpoint_manager` (files) + compression persistence (convo) + run position + effect refs — reifying `checkpointed_for_resume`. Does NOT reimplement effect_ledger/run_journal (Lane).
5. **Token-budget policy (Phase 3.7):** model-aware budget layer wrapping the existing char-limit capsule, backward compatible.

## Owner authorization (recorded)

- Base = current remote default (`main` @ `c7650a1b9`). Lane/Frontend/evidence branches NOT used as base and untouched.
- Local commits: author/committer = Owner/Eiman; per active session reminder the commit trailer `Co-Authored-By: Claude Opus 4.8` applies (reminder overrides earlier 0-attrib default; Owner may direct otherwise).
- No amend/rebase/squash/push/PR/merge/deploy/tag. Any future push needs separate Owner auth naming exact full SHA + exact remote branch.
