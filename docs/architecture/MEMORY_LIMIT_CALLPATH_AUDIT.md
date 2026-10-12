# Production Limit Call-Path Audit (item 1)

Every production path imposing a size limit / truncation / capsule / context assembly
for memory & context. Purpose: correct the "just raise 2200" instinct.

`memory_store_char_limit`: **NOT FOUND** (only `memory_char_limit` / `user_char_limit` exist).

## A. Built-in file store (MEMORY.md / USER.md) — CHARACTER-based

| Path | Surface | Value source | Unit | Storage vs Injection |
|---|---|---|---|---|
| `tools/memory_tool.py:165-169` `MemoryStore.__init__` | all | ctor args | chars | defines both caps |
| `tools/memory_tool.py:385-388` `_char_limit` | all | `memory_char_limit`/`user_char_limit` | chars | **STORAGE + injection (single value governs both)** |
| `tools/memory_tool.py:418-441` `add()` over-limit reject | Web/CLI/worker/gateway | `_char_limit` | chars | **DURABLE-STORAGE** (write refused) |
| `tools/memory_tool.py:493-512` `replace()` | all | `_char_limit` | chars | **DURABLE-STORAGE** |
| `tools/memory_tool.py:597,674-680` `apply_batch()` | all | `_char_limit` | chars | **DURABLE-STORAGE** (all-or-nothing) |
| `tools/memory_tool.py:682-693` `format_for_system_prompt` | all | frozen snapshot | chars | MODEL-INJECTION (already storage-bounded; NO re-truncation) |
| `tools/memory_tool.py:845-848` flush drift guard | all | `_char_limit` | chars | **DURABLE-STORAGE** |
| `agent/agent_init.py:1624-1628` construction+load | all | `mem_config.*` (2200/1375) | chars | STORAGE + injection |
| `gateway/slash_commands.py:3432-3452` `load_on_disk_store()` | gateway | config caps | chars | **DURABLE-STORAGE** (approvals) |
| `youtab_agent_cli/cli_commands_mixin.py:1842-1845` `load_on_disk_store()` | CLI | config caps | chars | **DURABLE-STORAGE** |
| `youtab_agent_cli/config_defaults.py:1547-1548` | all | constants 2200/1375 | chars | source of truth |

## B. External provider (Honcho) + capsule router

| Path | Surface | Value | Unit | Storage vs Injection |
|---|---|---|---|---|
| `youtab_runtime/memory/router.py:38` `_CAPSULE_MAX_CHARS=3000` | runtime | constant | chars | **STORAGE-admission for capsule** |
| `plugins/memory/honcho/session.py:125` `_dialectic_max_chars=600` | gateway/worker | config | chars | MODEL-INJECTION (truncates retrieval) |
| `plugins/memory/honcho/client.py:419` `message_max_chars=25000` | gateway/worker | config | chars | **DURABLE-STORAGE** (to provider) |
| `agent/context_engine.py:34-53` `MEMORY_CONTEXT_MAX_CHARS=6000` | all | constants | chars | MODEL-INJECTION (LLM egress) |

## C. Context / prompt assembly & compaction

| Path | Surface | Value | Unit | Storage vs Injection |
|---|---|---|---|---|
| `agent/system_prompt.py:503-517` memory+user+provider inject | all | store/manager | chars | MODEL-INJECTION |
| `agent/system_prompt.py:579-585` reload on compaction | all | load_from_disk | chars | injection refresh |
| `agent/prompt_builder.py:1263-1311` `CONTEXT_FILE_MAX_CHARS=20000` + dynamic | all | config/dynamic | chars | MODEL-INJECTION |
| `agent/context_compressor.py:379` `_SUMMARY_INPUT_MAX_CHARS=160000` | all | constant | chars | MODEL-INJECTION |
| `agent/context_compressor.py:616-622` fallback caps (8000/3000/700/260/1400) | all | constants | chars | MODEL-INJECTION |
| `agent/context_compressor.py:1357-1490,1843-1888` `threshold_tokens`/`tail_token_budget`/`max_tokens` | all | config/model | **TOKENS** | MODEL-INJECTION (already token-based) |
| `agent/agent_init.py:820,2004-2027` `agent.max_tokens` | all | config | tokens | output cap |

## Architectural conclusion (drives items 2 & 12-F)

The built-in memory `memory_char_limit`/`user_char_limit` (2200/1375) is a **single value
gating BOTH durable-storage admission AND system-prompt injection** — `format_for_system_prompt`
emits exactly what storage admitted, with no re-truncation. Therefore **raising 2200 enlarges the
persisted file**, coupling storage to injection. That coupling is what the target architecture breaks:

- durable memory stays external and is NOT enlarged by an injection change;
- a **token-budgeted injection wrapper** (`youtab_runtime/memory/budget.py` + `tokenizer.py`)
  sizes what is *injected* for the selected model, independent of the storage-admission cap;
- large artifacts stay referenced, not inlined; compaction preserves goals/decisions/open-tasks/
  evidence-refs/risks (`tokenizer.CompactionSummary`, `needs_compaction`).

Character-based INJECTION-ONLY paths (safe to wrap with a token budget): `context_engine.py:34`,
`honcho session.py:125`, `prompt_builder.py:1263`, `context_compressor.py:379,616-622`,
`memory_tool.py:682/731`.
Character-based STORAGE-affecting paths (must NOT be raised as an injection fix):
`memory_tool.py:418/493/597/845`, `router.py:38 _CAPSULE_MAX_CHARS`, `honcho client.py:419`.
Already token-based (extend, don't wrap): `context_compressor.py` threshold/tail/max_tokens; `agent.max_tokens`.
