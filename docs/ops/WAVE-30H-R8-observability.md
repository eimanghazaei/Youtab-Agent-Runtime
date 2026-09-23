# WAVE-30H R8 — Causally-valid per-stage latency observability

Status: instrumentation + analysis tooling landed on Draft PR #41 (local only,
not pushed). **Live p50/p95/p99 are NOT yet collected** — that requires the same
one Owner-authorized full-capability diagnostic run described in
[`WAVE-30F-root-cause-experiment.md`](./WAVE-30F-root-cause-experiment.md). This
document specifies what R8 measures, how it stays honest, and the evidence-based
bottleneck hypothesis the authorized run will confirm or refute.

R8 is the **observability layer** under the WAVE-30F attribution matrix. WAVE-30F
answered *"how do we attribute one run's wall-clock without blaming the wrong
component"*; R8 answers *"measure every stage causally, across many runs, and
produce distributions and cold-vs-warm splits"* — without ever logging a secret,
raw prompt, or raw response, and without changing the latency it measures.

Guiding principle (Owner, unchanged): optimise intelligently — do **not** shrink
the workload by hiding tools, prompts, memory, skills, or safeguards. R8 only
measures; it modifies no performance behaviour.

## What R8 records

`youtab_runtime/stage_trace.py` — a per-stage span recorder with five structural
guarantees (each enforced in code + tests, not by convention):

1. **No secret / raw-content leakage.** A span attribute may be
   `bool`/`int`/`float`/`None` freely; a `str` is accepted only for an
   allowlisted key (`SAFE_STR_KEYS`) and only up to 128 chars. Anything else
   raises `TraceSafetyError`. To identify a prompt/response without storing it,
   callers pass a size (int) and/or `content_fingerprint()` (a SHA-256 prefix).
2. **null != zero.** `duration_ns is None` means "occurred but not timed" and is
   counted apart from a measured zero; it never enters a percentile.
3. **Right clock per boundary.** Intra-process durations use `time.monotonic_ns()`
   (immune to wall-clock steps). A cross-process gap (queue wait, worker startup)
   cannot share a monotonic base, so it is measured from wall-clock epoch marks
   (`mark_epoch()`) and labelled `clock="epoch"` — never mistaken for a span.
4. **Immutable per-run context via `contextvars`** (correlation_id, run_id,
   root_run_id, attempt, agent_id, engine, provider, tenant, user) — thread-
   isolated, so concurrent runs never cross-contaminate. Same rule WAVE-30H
   correction 4 applied to memory namespacing.
5. **Observability never breaks or slows execution.** A sink failure is counted
   and swallowed; when tracing is disabled there are no clock reads and no emit —
   the wrapped body still runs. Instrumenting the system must not itself change
   the latency under study.

`youtab_runtime/stage_trace_report.py` — aggregation the codebase previously
lacked: linear-interpolation p50/p95/p99 (numpy-free), **model** cold-vs-warm
split (native `load_duration`), a distinct **process** cold-vs-warm split (first
model call in a fresh per-run subprocess), and honest null/measured/error counts.

## Where each requirement-listed stage is measured

| Stage | Source | Clock | Notes |
|---|---|---|---|
| request admission + HMAC verify | `web_routers/runtime.py` `_verify_signed_command` span `admission.verify_signature` | monotonic | failed (401) verify still emits `ok=False` + `reason_code` |
| grant verify + persist | `_admit_execution_grant` span `grant.verify` | monotonic | managed mode only |
| tool discovery / schema freeze | `_admit_execution_grant` span `tools.discover` | monotonic | `tool_count` from the built manifest, not guessed |
| model TTFT | observer `on_post_api_request` → `model.ttft` | monotonic | receive-time TTFT (WAVE-30F) |
| model generation | observer → `model.generate` | monotonic | native eval leg (`eval_duration`) |
| prompt prefill / input tokens | observer → `prompt.tokenize` (`reason_code=native_prompt_eval`, `input_tokens`) | monotonic | native prefill; labelled so it is not mistaken for client tokenization |
| model init warm/cold | observer → `model.init` (`cache_state` from native `load_duration`) | monotonic | model residency; omitted when unknown, never guessed |
| per-call wall | observer → `model.call` (`process_cold`) | monotonic | authoritative on the live OpenAI-compat path (no native legs) |
| each tool-call latency | observer `on_post_tool_call` → `tool.call` (`tool_name`, `ok`) | monotonic | covers memory tool + delegate tool (both invoked as tools) |
| worker startup / lifecycle | `phase_timing.py` `lifecycle_*_ms` + `t0_epoch` anchor | monotonic + epoch | rides the first usage event (WAVE-30F) |
| retries / backoff / throttle | `attempt` in trace context; `worker_incarnation` per attempt | — | retry lineage preserved (WAVE-30F) |

Correlation ids (correlation_id, run_id, root_run_id, attempt, agent_id,
engine/provider, tool identity, cache state) are preserved on every span via the
immutable trace context bound once at worker startup (`conversation_loop.py`) and
per request at ingress.

### Stages deliberately NOT wrapped, and why

* **Vector/graph memory retrieval inside context assembly.** The file-backed
  memory tool is already covered as a `tool.call`. The canonical vector/graph
  store lives in `app/memory`, whose per-namespace shape is governed by an ADR
  (a Phase-B schema decision). Instrumenting internal (non-tool) retrieval there
  is a governed change, not a casual one — flagged, not silently done.
* **Sub-millisecond crypto (HMAC/Ed25519).** Now measurable (spans above), so
  their magnitude is *proven small* rather than assumed — but they were never the
  suspected cost.

## Persistence & analysis

* Production sink: `RunJournalSink` writes spans into the existing principal-bound
  `run_journal` under a new `timing` category (deduped, redacted — numeric leaves
  survive, per-run `seq`). One durable store, not a parallel file.
* Experiment sink: a per-pid JSONL under `<agent home>/runtime/stage_traces/`,
  env-gated by `YOUTAB_STAGE_TRACE` (zero prod overhead when off).
* Report: `python -m youtab_runtime.stage_trace_cli --journal --tenant … --user …`
  or `--traces-dir …` → p50/p95/p99 + both cold/warm splits, bottleneck-ordered.

## Controlled experiments (one variable at a time)

`youtab_runtime/latency_experiment.py` enforces the discipline structurally:
`one_variable_matrix(baseline, variations)` emits, per axis
(`num_ctx` / `tool_count` / `prompt_tokens` / `process_cold`), cells that differ
from the baseline in **exactly one** axis, so any delta is attributable to that
single variable. `run_experiment` runs each cell N times and aggregates it
independently.

It is provider-agnostic:
* `native_call_fn(base_url, model)` drives the real loopback Ollama `/api/chat`
  diagnostic client — for the Owner-authorized run on a host with a reachable
  model.
* `synthetic_call_fn` is a deterministic, network-free model (illustrative
  coefficients, **NOT** measured latencies) used only to prove the analysis
  pipeline attributes a delta to the right axis without a provider.

## Evidence-based bottleneck hypothesis (to be confirmed at the authorized run)

Nothing below is a live p50/p95/p99 measured in this environment — there is no
provider/VPS/canary here, and none was run. It is the hypothesis the R8 tooling
exists to confirm, grounded in WAVE-30F's actual measurements and the
architecture:

1. **Primary suspect: cold per-run subprocess start (~13.3 s), model-cold-load
   secondary.** Each run is a freshly spawned `youtab chat -q` subprocess that
   handles one run and exits (worker pools are ADR-gated, out of scope), so the
   pre-first-token overhead is dominated by cold Python start + one-shot agent
   construction — nothing amortises across runs. This is measured by
   `phase_timing`; R8 adds the always-known **process cold/warm** split so the
   first-call-in-a-fresh-process penalty is visible distributionally.
2. **Not the prompt/tool-schema size.** The ~38-tool array is ~16K of near-pure
   English prose with zero JSON-Schema keyword bloat and no per-request
   reserialization defect (memoized once per run). It is a modest, inherent
   per-run cost — not the dominant term — and the safe F1 compaction already
   landed (~594 tokens, no capability removed).
3. **Not admission / HMAC / grant / manifest.** Now instrumented; expected
   sub-millisecond-to-low-millisecond — measurable proof they are noise relative
   to (1), not an assumption.

The authorized run collects the WAVE-30F A/B/C/D matrix **and** feeds the same
calls through R8 so the verdict carries distributions (p50/p95/p99) and both
cold/warm splits, not a single sample.

## Boundaries honoured

No live run, no full 49-scenario benchmark, no cloud provider, no OpenRouter key,
no secret read, no VPS/production access, no merge/deploy. No tool hidden, no
registry cap, no prompt shortened, no security check removed — R8 measures only.
PR #41 stays Draft. Any live run is a separate exact-SHA Owner authorization for
one diagnostic canary.
