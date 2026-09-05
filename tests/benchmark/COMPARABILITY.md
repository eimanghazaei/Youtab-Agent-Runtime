# Dual-Track Benchmark Comparability Contract (WAVE-30C §2)

> **PRELIMINARY — NOT RELEASE EVIDENCE. No live provider call is part of this
> contract; it is validated entirely in the deterministic CI gate.**

The benchmark runs on **two comparable execution tracks**. This document — and
the machine-checkable spec beside it — define exactly what makes them comparable
so a Track A vs Track B comparison is *earned*, not asserted.

| | **Track A** | **Track B** |
| --- | --- | --- |
| Execution | Local inference (Ollama-served ECO engine) | One Owner-selected **non-Anthropic** cloud provider |
| Mode | `local_runtime` | `real_provider` |
| Provider | `ollama` | `OWNER_SELECTION_REQUIRED` |
| Model | Owner-supplied ECO tag (`${YOUTAB_ECO_MODEL}`) | `OWNER_SELECTION_REQUIRED` |
| Credential | none (local) | file-based `<PROVIDER_API_KEY_ENV>_FILE` (strict tier) |
| Cost | **€0 API** (electricity tracked separately) | **≤ €10.00** cumulative campaign ledger |
| Config | [`tracks/track_a_eco.v1.json`](tracks/track_a_eco.v1.json) | [`tracks/track_b_cloud.v1.json`](tracks/track_b_cloud.v1.json) |

The spec: [`tracks/comparability.v1.json`](tracks/comparability.v1.json). The
validator + record stamp: [`harness/tracks.py`](harness/tracks.py). The tests:
[`test_comparability_contract.py`](test_comparability_contract.py).

## What is SHARED (identical, byte-for-byte — the comparability invariants)

Both track configs carry an identical `shared` block (compared as **canonical
JSON** — order-independent and type-sensitive, so an `8` vs `8.0` drift is caught),
and the validator proves it equals the spec **and** the real task bank on disk:

- **Task bank** — `tests/benchmark/tasks/manifest.json`, pinned by
  `manifest_sha256`, `scenario_id_set_sha256` and `family_set_sha256`. Both tracks
  run the **byte-identical** 49 scenarios across 21 families.
- **Schema versions** — output `benchmark.v1`, taskbank `benchmark_tasks.v1`.
- **Scoring** — the same state-based oracles (`harness/oracles.py`) and the same
  `Runner.verdict_matches_expected` gate. Verdicts never read the agent's
  self-report.
- **Evidence / output schema** — the same `BenchmarkRecord` fields in
  `results.jsonl` + `summary.json`.
- **Technically-meaningful run limits** — identical for both tracks:
  `max_iterations`, `max_requests`, `max_retries`, `max_concurrency`,
  `max_total_tokens`, `max_runtime_seconds`, `failure_threshold`. (Cost is **not**
  a shared limit — it is per-track.)

If the manifest changes without the pinned hashes being updated, the contract
fails **loudly** (`test_manifest_drift_breaks_comparability`).

## What is PER-TRACK (the only sanctioned differences, and they are recorded)

Recorded in each track's `per_track` section and stamped into every record's
`provenance` (`track`, `provider_name`, `model_name`, `model_source`,
`credential_source`, `cost_model`, `execution`, `mode`):

- provider + model identity and provenance;
- credential source (none-local vs file);
- cost model (€0 local vs €10-budgeted cloud) and the campaign-budget binding;
- **separately measured** latency, throughput (tokens/sec), failure counts and
  cost — these are per-track measurements and are **never averaged across tracks**.

Anything that differs outside `per_track` invalidates the comparison and fails the
contract test.

## Track A — Local / ECO: exact, reproducible configuration

Track A is defined as the explicit **versioned** profile
`tracks/track_a_eco.v1.json` (not just a label). ECO is engine profile `eco.v01`;
a local model is addressed as `OLLAMA_BASE_URL` (loopback/private/tailnet only,
enforced by `engine_connection.endpoint_is_authorized`) + the model tag.

### Model identity — STOP-AND-REPORT (Owner action required)

**"Qwen 3.5 9B" is not a canonical open-weight identifier.** The real open-weight
Qwen families are **Qwen2.5** and **Qwen3**. The ECO binding in the *generated*
`youtab_agent_cli/agent_identity.v1.json` carries the **provider-neutral
placeholder** `ollama/qwen3.5:9b`; the comment there states the concrete tag is
deployment-specific, injected server-side via **`YOUTAB_ECO_MODEL`** or the
`youtab-ai-os` governed registry, and must never be committed. This config
therefore **does not substitute a different model** — `model_name` is the
`${YOUTAB_ECO_MODEL}` placeholder and the track is flagged
`OWNER_MODEL_IDENTIFIER_REQUIRED`. **Before a Track A run the Owner must supply the
exact canonical model repository/tag and revision.**

### Determinism fields recorded (per `track_a_eco.v1.json`)

canonical model name · artifact/revision · model checksum · quantization
format + level · inference runtime + version · configured context limit ·
input/output token limits · sampling (temperature/top-p) · tool-calling mode ·
structured-output support · hardware (CPU/RAM/GPU/VRAM) · runtime HEAD SHA ·
clean worktree · cold-start vs warm-start.

### Resource + cost

Local API spend is **€0**. Recorded separately: wall-clock, tokens/sec, peak
RAM/VRAM, GPU/CPU utilization, energy (Wh) where measurable, and an **estimated
electricity cost** that is **never mixed into the cloud €10 API budget**. Track A
still enforces max requests/tokens/iterations/runtime, retry/failover policy,
failure threshold, concurrency, safe output dir, exact SHA, clean worktree,
redaction, and deterministic config recording — it is excluded only from the
€10 *cloud* campaign ledger.

## Track B — Cloud: Owner-gated, non-Anthropic, €10-bounded

Provider selection is **`OWNER_SELECTION_REQUIRED`** and must **not** default to
Anthropic. The Owner selects one provider *after* comparing current official
pricing, privacy, retention, residency and API compatibility — see the ranked
shortlist and full matrix in
[`docs/benchmark/PROVIDER_SELECTION_MATRIX.md`](../../docs/benchmark/PROVIDER_SELECTION_MATRIX.md).

> **Pre-selection note:** by design the contract is satisfiable **only while Track
> B is unselected** — `validate_comparability` requires `provider_name` /
> `model_name` / `selection_status` to be `OWNER_SELECTION_REQUIRED`. When the
> Owner selects a provider, the Track B config is updated with the concrete
> provider/model and this guard is relaxed to accept the selected values; that
> selection is the Owner's authorized next step, not part of WAVE-30C.

The credential is delivered as a file (`<PROVIDER_API_KEY_ENV>_FILE`, strict
tier — owner-only `0400` / protected DACL, read through the WAVE-30C same-handle
reader). The run is bounded by the durable **€10.00 cumulative** campaign ledger,
allocated Canary €0.10 / Pilot €2.00 / Full €7.90 (= €10.00), covering retries,
failover and resumed/aborted-billable runs. A provider-side spend cap is set as
defense-in-depth. Estimated vs provider-billed cost are recorded separately.

## How comparability stays CI-deterministic (no live calls)

The contract is validated in the `benchmark-deterministic` job: `tracks.py` is
pure stdlib, so `validate_comparability()` runs offline, and a `--track A`
deterministic run is exercised end-to-end with **no provider and no network**
(the `real_provider` rows short-circuit to an honest `unknown`). Track B's real
latency/throughput/cost measurements happen only under an Owner-authorized
`--mode real_provider` run behind the `assert_live_safety` / `verify_runtime_sha`
preflight — never in CI.
