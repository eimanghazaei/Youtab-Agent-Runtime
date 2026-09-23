# Gate 2 — real managed E2E: BLOCKED by a managed-authority defect (not the tunnel)

Date: 2026-09-09. Tunnel LIVE and verified in-process every run
(`/api/tags` → `youtab-qwen35-9b-agent-64k:latest`, digest `b7b9afeaf023a549…`).

## What was run

~8 real managed runs through the **real dispatch path** (NO `_spawn_override`, NO
deterministic runner): `create_run → signed HMAC transport + Ed25519 Simorgh
grant → ingress admit + freeze manifest + persist → dispatcher → cold `youtab …
--cli chat -q` worker (runner="cli") → ECO/Ollama (127.0.0.1:11435)`.
Harness: `scratchpad/pool_e2e_matrix.py` (isolated kanban DB; real `default`
profile; `engine=eco.v01`; `YOUTAB_ECO_MODEL` set; `OLLAMA_BASE_URL=11435`).

## What is PROVEN working (the first ~90% of the chain)

Every run reached, in order, the real events:
`created → runtime_execution_mode(model) → runtime_engine_selection(eco.v01) →
runtime_limits → runtime_execution_grant (signed) → runtime_capability_manifest
(frozen) → claimed → spawned (real PID + incarnation) → heartbeat…`

So admission (HMAC + Ed25519 grant + frozen manifest + persistence), dispatch,
the **real cold CLI/agent worker**, and **correct ECO endpoint resolution to
11435** all work. The worker runs the genuine `youtab` agent (model calls +
tool calls recorded in the run_journal).

## What is BLOCKED: terminal completion (0 / N runs completed)

**Root cause — the managed authority gate blocks 100% of the worker's tool
calls.** The product's own `run_journal` (`…/youtab/runtime/run_journal.db`,
category `timing`/`tool_result`) shows, across **every** recent managed run,
`tool_result = [error]`:

> "managed execution reached a tool invocation without a Simorgh execution grant
> (admission is mandatory; no standalone fallback)"  — `error_type: authority_block`

(The only non-error `tool_result` in the store is 2.4 days old, from a
pre-WAVE-30H run.) This is the `admitted is None` branch of
`agent/tool_executor.py::enforce_managed_tool_authority`: the worker's agent has
**no `_admitted_command`** at tool-execution time.

The agent's own transcript (board worker log) confirms it: it calls
`kanban_show`, `search_files`, `terminal`, `browser_navigate` — all blocked with
"Simorgh execution grant" errors — narrates that it "cannot run commands or
access the kanban tools," and gives up without calling `kanban_complete`. So the
run never self-completes → times out / protocol-violates.

## Security reading (important, positive)

The gate is behaving **correctly and fail-closed**: a managed tool invocation
without an established admitted context is refused, with no standalone fallback.
That is exactly the WAVE-30H R3 contract. The defect is NOT a security hole — it
is that the worker side never **establishes** the context, so the (correct) gate
denies everything.

## Localization

- `youtab_agent_cli/worker_admission.py::establish_managed_admission` works
  correctly **in isolation**: given the worker's env (trust=managed,
  `YOUTAB_AGENT_KANBAN_TASK`, persisted grant, brain keys) it loads the grant,
  `re_admit_worker_grant` succeeds, and sets `agent._admitted_command` (verified
  directly; it only fails once the grant's 30-min expiry passes → `grant_rejected`).
- `cli.py::main` (reached via `youtab_agent_cli.main.cmd_chat → cli_main(quiet=True)`)
  calls `establish_managed_admission(cli.agent)` at line ~17875, immediately
  before `cli.agent.run_conversation(...)` at ~17883.
- Yet at tool time the gate sees `_admitted_command is None`. So on the **real
  worker path** the admitted context is not present on the agent the gate checks
  — either `establish` is not reached on that path, or it attaches to a different
  agent object than the one `run_conversation`/`tool_executor` uses, or the
  attribute is reset between establish and the tool call.

Pinning the exact line needs an in-worker debugger/trace; a gated `YOUTAB_DIAG_*`
probe was attempted but the env did not propagate into the spawned worker through
the harness, and further live runs are slow (see below). The **blocker itself is
not in doubt** — the run_journal `authority_block` evidence is authoritative and
reproduced on every run.

## Secondary observation — model latency over the tunnel

The 9B model over the SSH tunnel is **slow**: ~1 tool-enabling turn per tens of
seconds (e.g. 1 `tool_result` in 120 s; 2–15 heartbeats over 70–300 s). Even if
completion worked, a single managed E2E is minutes, not the ~15 ms prototype or
the ~3.4 s import delta. The before/after timing matrix cannot be produced until
completion works.

## Consequence for the Gate-2 deliverable

The requested "successful terminal completion" + before/after stage-timing matrix
**cannot be produced** on the current code: no managed run completes because the
real worker does not present the admitted context to the (correct) authority gate.
This is the step-3 last-mile requirement #4 ("the worker must … establish the
sealed admitted execution context") not taking effect on the real
`youtab … chat -q` dispatch path, while #5 (the gate consuming it) works and
fails closed.

## Recommendation (needs Owner direction — security-critical authority path)

Fix the worker-side establishment so the admitted context is present on the agent
the `tool_executor` gate checks for the real cold `youtab … --cli chat -q` kanban
worker (and, by extension, the pooled worker, which runs the same CLI entry).
Then re-run this harness for the full before/after matrix. This is a change to the
managed admission→execution authority contract, so it should be made deliberately
with Owner authorization, not silently inside a benchmark harness.

Tree remains clean: only the authorized WAVE-30H change set is modified; all
diagnostic edits were reverted. No commit/push/merge/deploy.
