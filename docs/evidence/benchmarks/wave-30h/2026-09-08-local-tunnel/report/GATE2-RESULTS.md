# WAVE-30H Gate 2 — managed E2E: defect fixed, real completion proven, before/after benchmark

Owner authorized Option 1 (diagnose + fix the managed admission → worker → tool-gate
path, current Draft worktree only). This is the result. **No commit / push / merge /
deploy. HEAD unchanged `40b3d69aa`. Gate 5 (Linux CI) remains Owner-gated.**

## 1. Executive summary

The managed path that previously blocked **every** run from completing now reaches a
**real terminal completion** through the genuine chain:

```
create_run → HMAC transport + Ed25519 Simorgh grant → ingress admit + FROZEN manifest
  + persist → dispatcher → REAL youtab --cli chat -q worker → establish admitted context
    → authorized tool calls (kanban_show → kanban_complete) → completed event (task=done)
```

Two independent defects were pinned and fixed; both are proven live (ECO/Ollama) and
hermetically (150 existing + 10 new tests). Cold-spawn and pooled workers both complete
(parity). The pool's only real saving is worker startup/import (~2.2 s); total E2E is
dominated by the model and the pool does not change it.

## 2. Root causes (pinned before any change)

**Phase A — the real worker path never established admission.** The dispatcher spawns a
non-goal kanban worker as `youtab … --cli chat -q "work kanban task <id>"`. In the CLI
argument parser `-q`/`--query` is the *query text* and `-Q`/`--quiet` is the quiet flag
(`youtab_agent_cli/_parser.py:275,327`). So `quiet=False`, and `main()` takes its
human-facing single-query branch (`cli.py:17962` → `cli.chat(query)` → `YoutabCLI.chat`
→ `run_conversation` at `cli.py:13658`). That method **never called**
`establish_managed_admission`; the call existed only in the `if quiet:` branch
(`cli.py:17875`, used by the `-Q` goal-mode worker). Result: the agent reached a tool with
`_admitted_command is None` and the authority gate (correctly) fail-closed on **100 % of
tool calls** — verified in the run_journal: every `tool_result` was
`error_type=authority_block` "managed execution reached a tool invocation without a
Simorgh execution grant (admission is mandatory; no standalone fallback)". The gate was
correct; the worker side was unwired.

**Phase B — the frozen manifest excluded the completion tool.** The ingress freezes the
per-run capability manifest from the live registry. The kanban lifecycle tools
(`kanban_complete`/`kanban_block`/`kanban_heartbeat`) are gated by `_check_kanban_mode`,
whose availability is keyed on the **worker-only** `YOUTAB_AGENT_KANBAN_TASK` env — absent
in the ingress process. So even under a `"*"` grant the freeze produced 28 tools with **no
`kanban_*`** (proven deterministically). The worker re-admits that frozen manifest, so the
gate denied its own completion tool — meaning no managed kanban run could ever complete,
even once admission was established.

## 3. Fixes (within all 10 security constraints)

**Phase A** — `cli.py` `YoutabCLI.chat`, immediately after `_init_agent`, calls
`establish_managed_admission(agent)` on the exact `AIAgent` object `run_conversation`
runs (object identity verified: attach id == gate id). Fail-closed (`exit(3)` on
`ManagedWorkerAdmissionError`); a no-op in local-standalone / non-kanban. Covers the cold
**and** pooled workers (both run this same CLI entry). Re-admits ONLY the exact persisted,
signed, ingress-verified grant + frozen manifest — it never mints/broadens authority.

**Phase B** — new `context_available_toolsets` parameter on
`registry.capability_manifest_pairs` (threaded through `capability_manifest.build_ingress_binding`,
passed by `runtime.py` as `_WORKER_EXECUTION_CONTEXT_TOOLSETS = frozenset({"kanban"})`).
It defers the **execution-context** gate (kanban-mode) — which the dispatched worker
satisfies but the ingress process cannot — to the invocation-time strict gate
(`available_strict`, C9), which re-checks in the worker and fails closed elsewhere. This
is **not** an availability bypass (operational availability is still enforced at
invocation) and is **fully grant/ACL/forbidden gated**.

### Security-constraint compliance

1. Gate not weakened/bypassed — unchanged; still the last check before every tool. ✓
2. No standalone fallback added — fail-closed preserved. ✓
3. Worker never mints/broadens/self-authorizes a grant — it re-admits the exact persisted one. ✓
4. Worker re-admits only the persisted, signed, ingress-verified grant + frozen manifest. ✓
5. Signature / expiry / nonce / tenant / user / tool-hash / manifest binding preserved. ✓
6. Fail-closed on missing/expired/forged/replayed/cross-tenant/tampered — tests green. ✓
7. No **global** kanban authorization — inclusion is gated by the grant's `allowed_toolsets`
   (a grant not authorizing `kanban`/`"*"` still excludes; proven: `file`/`browser`/`web`
   grants exclude `kanban_complete`). ✓
8. Completion tool executes only if explicitly in the frozen contract — `decide_tool`
   authorizes it only when present in the frozen manifest. ✓
9. No `dynamic_inclusion`, no loose `"*"`, no availability bypass, no env injection. ✓
10. No commit/push/merge/deploy. ✓

## 4. Proof the fix works (before → after, same run)

| | before fix | after fix |
|---|---|---|
| first tool (`kanban_show`) | `authority_block` | **`[ok]`** |
| `kanban_complete` | never reached / not in manifest | **`[ok]`** |
| task terminal state | stuck `running` → timeout/violation | **`done` + `completed` event** |

## 5. Real live E2E benchmark (ECO/Ollama, sequential, all reps retained)

Model `youtab-qwen35-9b-agent-64k:latest` (digest `b7b9afeaf023a549…`) over the loopback
tunnel (127.0.0.1:11435). No deterministic runner — `runner="cli"`, the genuine agent.
Every rep (including timeouts) retained.

**Raw per-rep records** (worker startup = process start → first real work; TTFT per model turn):

COLD (timeout 700 s, matched): 2/3 completed
- cold-1 `completed` e2e 253.8 s, startup 3317 ms, TTFT [157.6 s, 37.8 s], tools [show, complete]
- cold-2 `timeout`   e2e 701.0 s, startup 3006 ms, TTFT [182.5, 373.5, 5.7 s], tools [show, complete]
- cold-3 `completed` e2e 634.0 s, startup 2825 ms, TTFT [225.5 s, 385.0 s], tools [show, complete]

POOL (timeout 700 s, matched): 2/2 completed
- pool-1 `completed` e2e 212.7 s, startup 880 ms, TTFT [157.3 s, 31.3 s], tools [show, complete]
- pool-2 `completed` e2e 577.0 s, startup 807 ms, TTFT [179.1 s, 373.2 s], tools [show, complete]

(An earlier cold run at timeout 420 s gave 1/5 completed — 4 timed out purely because 420 s
was shorter than the model's per-run latency, NOT a cold-vs-pool effect. Retained in
`matrix_cold_t420.json`. Cold startup there: 2690/2751/3565/3603/4023 ms.)

**Aggregate p50 / p95 and before→after delta:**

| stage (p50) | cold | pool | delta | interpretation |
|---|---|---|---|---|
| **worker startup / import** | **3071 ms** | **844 ms** | **−2227 ms** | REAL, consistent — the pool pre-pays the import |
| total end-to-end | 443.9 s | 394.9 s | −49 s | model-variance NOISE (n small; same backend) |
| model TTFT | 191.6 s | 168.2 s | −23 s | model-variance NOISE |
| model call | 196.7 s | 174.9 s | −22 s | model-variance NOISE |

Worker-startup raw: cold {3317, 2825} (t700) + {2690, 2751, 3565, 3603, 4023} (t420) ≈ 2.7–4.0 s;
pool {880, 807} ≈ 0.8 s.

### Stage separation (Owner requirement)

- **Import / startup saving — the pool's only real benefit:** ~2.2 s p50 (cold ~3.1 s →
  pool ~0.84 s). Confirmed by a direct cold import measurement: `model_tools` + CLI import
  ≈ 1.7 s (warm OS cache); the pool pre-pays this once in its resident warm worker.
- **Model load / prefill / TTFT / generation:** 157–385 s **per turn**, identical backend
  for cold and pool, and the dominant term by ~100×. Highly variable (e.g. 209 s vs 634 s
  for the same 2-turn task).
- **Complete end-to-end saving:** ≈ the import/startup saving (~2.2 s), i.e. **~0.5–1 % of
  total**. The larger E2E/TTFT p50 deltas above are within model-latency noise, **not** a
  pool effect — the pool does not touch the model. This is the required clear distinction
  between the measured import reduction and total-E2E improvement.

## 6. Pooled-worker correctness

- **Parity:** the pooled worker re-admits the same sealed context and completes identically
  to cold (pool-1/pool-2 both did `kanban_show → kanban_complete` → `done`).
- **One run per worker:** `pool_proof` shows `total=3` warm workers for 2 runs (each run a
  fresh worker + refill), each serving exactly one run then exiting.
- **Zero-survivor teardown:** `close()` → `{"terminated": [29644,49620,12224],
  "survivors": [], "zero_survivors": true}`.
- **Fresh per-run binding / no leakage:** each run gets its own grant + manifest + env via
  `build_worker_invocation` (identical to a fresh spawn); the warm worker applies that env
  and exits single-use — no state/grant/memory/secret carried between runs.

## 7. Negative controls (remain blocked after the fix)

All green in the hermetic regression (the fix did not weaken any):
- no grant / forged / expired / replayed / cross-tenant — `test_managed_lifecycle_http_e2e`
  + `test_managed_execution_subprocess` (ingress + cross-process).
- tampered/modified manifest — `test_worker_refuses_tampered_manifest`.
- unauthorized tool / tool absent from frozen manifest — `test_capability_binding` +
  new `test_gate2_managed_completion` (decide_tool denies; grant not authorizing kanban
  excludes the completion tool).
- dependency unavailable at invocation — `test_invocation_availability_failclosed` (C9).

## 8. Regression + new tests

- **150 hermetic tests green** (managed execution/lifecycle/subprocess, capability binding,
  policy, router policy gate, worker pool, run control, keepalive, invocation-availability).
- **10 new** `tests/youtab_runtime/test_gate2_managed_completion.py` (manifest grant-gating
  incl. no-global-authorization negative controls; decide_tool completion-only-if-in-manifest;
  Phase-A + Phase-B wiring guards). All green.
- Temporary diagnostics fully removed (`worker_admission.py` reverted clean; `tool_executor`/`cli` diag removed).

## 9. Change set (this Gate-2 work, on top of the WAVE-30H set)

- `cli.py` (+27) — Phase A: establish admission in `YoutabCLI.chat`.
- `tools/registry.py` (+~30) — Phase B: `context_available_toolsets` on `capability_manifest_pairs`.
- `youtab_agent_cli/capability_manifest.py` (+14) — Phase B: thread the parameter.
- `youtab_agent_cli/web_routers/runtime.py` — Phase B: `_WORKER_EXECUTION_CONTEXT_TOOLSETS`
  + pass it at the ingress freeze.
- `tests/youtab_runtime/test_gate2_managed_completion.py` (NEW, 10 tests).

Rollback: `git checkout -- cli.py tools/registry.py youtab_agent_cli/capability_manifest.py
youtab_agent_cli/web_routers/runtime.py` and delete the new test. All additive + fail-safe:
Phase A is a no-op outside managed+kanban; Phase B only adds grant-authorized, entitled
tools the worker legitimately has, with C9 enforcing operational availability at invocation.

## 10. Evidence status matrix

| item | status |
|---|---|
| Phase A + B implemented in Runtime | ✅ done |
| negative controls remain fail-closed | ✅ tested (150 hermetic) |
| real managed E2E reaches terminal completion (cold) | ✅ proven (ECO/Ollama) |
| pooled worker parity completion | ✅ proven (pool-1, pool-2) |
| one-run-per-worker / zero-survivor teardown | ✅ proven (`pool_proof`) |
| import/startup before→after (~2.2 s saving) | ✅ measured |
| total E2E / TTFT / generation | ✅ measured; model-dominated, identical cold/pool |
| Linux CI required checks (Gate 5) | ⏳ Owner-gated (push not authorized) |

## 11. Honest limitations

- **Model latency is pathological on this Mac right now** (trivial warm request ~47 s; real
  7440-token prompts 157–385 s per turn; degrading under repeated load). This is
  infrastructure, not code, and must not be touched. It caps the statistical power of the
  E2E percentiles (small completed-n; large variance) and is why a short 420 s timeout
  produced mostly timeouts. It does **not** affect the correctness fixes or the
  worker-startup measurement (captured on every run regardless of completion).
- The pool's benefit is the ~2.2 s import/startup saving; it cannot and does not reduce the
  model-dominated remainder.
