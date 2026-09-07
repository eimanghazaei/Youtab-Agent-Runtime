# WAVE-30F — Local ECO/Qwen root-cause experiment & remediation

Status: instrumentation + safe schema compaction landed on Draft PR #41.
**The dominant bottleneck is not yet PROVEN** — that requires one Owner-authorized
full-capability diagnostic canary that runs the controlled matrix below. This
document is the runbook and the evidence contract.

Guiding principle (Owner): optimise Agent Runtime intelligently — do **not** make
the workload smaller by hiding tools, prompts, guidance, memory, skills, or
safeguards. Every removed/deferred/summarised/cached element must have a measured
reason, a capability-preservation proof, and a rollback path.

## What changed the analysis

The runtime's local ECO traffic uses Ollama's **OpenAI-compatible** endpoint
(`/v1/chat/completions`). That endpoint does **not** return Ollama's native timing
block (`total_duration`, `load_duration`, `prompt_eval_count`,
`prompt_eval_duration`, `eval_count`, `eval_duration`). Those fields exist only on
the **native** endpoints (`/api/chat`, `/api/generate`). Therefore the earlier
`extract_native_ollama_timings(response)` returns `{}` on every live call and
`native_timings_available` is always `False` on the live path. Unit tests that fed
fabricated response objects proved the extractor, not the live path.

Mechanism (requirement #3): `youtab_runtime/ollama_native.py` obtains authoritative
native timings out-of-band by calling Ollama's native `/api/chat` with the SAME
messages/tools/options, **without** changing the production transport's model
semantics or breaking tool calling. It powers matrix paths A and B; its raw ns
timing dict is fed through the one shared ns→ms authority (`model_timings`).

## Controlled bottleneck-attribution matrix (requirement #2)

Same model + digest + task + generation options + prompt payload across four paths:

| Path | What it runs | Purpose | Native timings |
|---|---|---|---|
| **A** | native `/api/chat`, minimal 1-line task | model load + baseline gen, tiny prompt | yes (A/B via `ollama_native`) |
| **B** | native `/api/chat`, EXACT runtime messages+tools+options | model prompt-eval + gen cost of the real ~21K prompt, no orchestration | yes |
| **C** | Runtime assembly + orchestration, **no inference** | Runtime lifecycle/assembly overhead | phase timings |
| **D** | Full Agent Runtime execution | the total the canary experienced | wall + phase timings |

Attribution (`youtab_runtime/bottleneck_matrix.py`, pure + deterministic):
components `model_cold_load`, `model_prompt_evaluation`, `model_generation`,
`runtime_overhead` (= path C wall), `transport_residual` (= D − model total(B) −
runtime(C)). Each is scored `PROVEN_PRIMARY` (≥50% of D wall), `PROVEN_SECONDARY`
(≥20%), `DISPROVEN` (measured, below), or `UNRESOLVED` (inputs absent — never
blamed). No component (Mac / SSH / Ollama / Qwen / Runtime) is blamed without this
comparison.

### Running the matrix at an authorized canary
1. Capture the exact runtime-assembled `messages`, `agent.tools`, and options
   (`num_ctx`, `keep_alive`, temperature, `num_predict`) at dispatch (path D).
2. `run_ab_paths(base_url, model, runtime_messages, runtime_tools, options)` →
   paths A and B (local Ollama native; never a cloud provider).
3. Path C: run the worker with inference stubbed (assembly + loop, no send).
4. `classify_bottlenecks({A,B,C,D})` → ranked findings.
5. Record cold vs warm separately (below). One exact-SHA canary only — it must not
   silently become the 49-scenario benchmark.

## Runtime lifecycle overhead (~13.3s) (requirement #4)

Architectural fact: each run executes in a **freshly spawned `youtab chat -q`
subprocess** that handles one run and exits — so the pre-first-token overhead is
dominated by cold Python process start + one-shot agent construction; nothing is
amortised across runs (worker pools are ADR-gated, out of scope).

Instrumentation: `youtab_runtime/phase_timing.py` records monotonic marks
(`agent_stack_imported`, `agent_init_start`, `first_model_send`) emitted as
all-numeric `lifecycle_*_ms` on the first per-call timing event. The import-head
slice (process start + heavy imports, before the phase clock's T0) is derived from
the dispatcher's timestamped `spawned` journal event minus the first-event marks.

Static top suspects (ranked by code shape; the canary confirms the split), each
with a SAFE, isolation-preserving fix (no shared pools/caches):
1. **Eager import of 30+ subcommand parsers** (`youtab_agent_cli/main.py:433-477`)
   + full agent/tools stack — a worker that only runs `chat` pays for all of them.
   Fix: lazy subcommand parser construction.
2. **`tirith ensure_installed()` before the enabled-gate** (`cli.py:7013`) can
   attempt GitHub-release downloads (10s timeouts) when the binary isn't cached.
   Fix: check `security.tirith_enabled` config before any network install; never
   block a dispatcher-spawned worker on a download.
3. **env-probe wait** (`tools/env_probe.py:295`, up to 10s). Fix: disable
   `environment_probe` for kanban workers, or start the warm probe earlier.
4. **Ollama `num_ctx` `/api/show` round-trip** (`agent_init.py:2591-2602`). Fix:
   set `model.ollama_num_ctx` in config so the probe is skipped.
5. Skills-prompt disk snapshot vs full scan; repeated `load_config_readonly`
   deepcopies. Fixes: ensure a fresh snapshot; frozen readonly view.

Confirmed NOT recomputed per call: the system prompt is cached and `agent.tools`
is assembled once per run — so the ~21K is a per-run assembly cost, not per-call.

## Tool-schema size — why ~16K, and the capability-preserving fix (requirements #6/#7)

The ~38-tool array is **almost pure English prose**: ~52% `parameters` (of which
~36% is property-description prose), ~43% tool-level descriptions, <2% enums+names,
and **zero** JSON-Schema keyword bloat (no `title`/`examples`/`additionalProperties`).
There is no per-request reserialization defect (the array is memoized); tokens are
inherent to sending `tools=` each turn — the only levers are real de-duplication,
prose compaction, or provider prefix-caching (ADR follow-up).

**Landed (F1, safe, all tools visible):** the board-resolution rule
(`_DESC_BOARD`, 350 chars) was serialized on the wire for all 12 board-bearing
kanban tools even though it is server-side behaviour the model never computes.
Now it appears once (on `kanban_show`); the rest carry a short stub with identical
actionable semantics. **~594 est tokens saved, no parameter/optionality/validation
removed, no tool hidden.**

**Measured but NOT applied (review-gated):** F2 (kanban schema prose that restates
the always-injected `KANBAN_GUIDANCE`, ~1.0–1.2K tok) and F3 (verbose behavioural
prose in `session_search`/`delegate_task`/`terminal`, ~0.7–1.2K tok). These carry
**behavioural guidance** — which for a 9B local model is part of capability
(requirement #6) — so they are held for a behaviour A/B at the canary rather than
trimmed blind. Must-keep in all cases: `required` sets, enum values, and the
load-bearing constraints (phantom-id rejection, absolute-path/exists-at-completion,
FTS5 syntax, notify/watch mutual exclusion, delegate self-report verification).

## Deferred-tool loadout = EXPERIMENTAL, default-off (Owner correction)

`tools.tool_search` `defer_core`/`always_on` remains **default-off**. It is NOT the
Track A baseline and must not be enabled for the root-cause canary without separate
Owner authorization. Direct visibility, discovery reliability, behavioural guidance,
and round-trip count are all part of capability; "technically reachable via a
bridge" is not a capability-preservation proof. `always_on` now supports glob
patterns (`kanban_*`) as well as literal names (previously `kanban_*` silently
matched nothing).

## Context budgeting — adaptive target, 64K always retained (requirement #8)

`context_budget.plan_context` reserves output + tool-loop tokens, refuses silent
truncation (escalates instead), and always retains the authorized 64K capability.
The prompt-token TARGET (simple 4–8K … document 32–64K) is an engineering aim on
the tool-schema/initial-prompt category, independent of the `num_ctx` capability.
Boundary tests cover 8K/16K/32K/64K classes.

## Cold / warm / cache classification rule (requirement #9)

Rule (explicit, tested): a call is **cold** iff the native `load_duration` exceeds
`COLD_LOAD_THRESHOLD_MS` (1000 ms) — Ollama's own model-load timer, never
wall-clock; **warm** at/below it; **omitted** (never guessed) when no native
`load_duration` is available and the caller supplied no explicit fact. An explicit
caller fact always overrides the derivation.

At the authorized canary, record separately: cold model load; warm resident model
(`/api/ps` residency); prompt-cache hit/miss; Runtime cold vs warm start; worker
cold/warm; `keep_alive` config; GPU/CPU placement; effective `num_ctx`;
`OLLAMA_NUM_PARALLEL`; Flash Attention state when observable. `keep_alive`/preload
improve the WARM path only and must not be presented as fixing cold-start. No
KV-cache/model quantization change without a separate measured quality A/B + Owner
approval.

## Boundaries honoured

No live canary, no full 49-scenario benchmark, no Track B, no cloud provider, no
OpenRouter key, no oracle/task-bank/timeout/comparability change, no secret read,
no VPS/production access, no merge/deploy. PR #41 stays Draft. Any live run is a
separate exact-SHA Owner authorization for ONE diagnostic canary.
