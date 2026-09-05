# Agent Runtime Capability Benchmark (WAVE-26)

A versioned, runnable benchmark that judges runtime capability **only from
observable, durable state and side effects** — never from the agent's own
self-report. `self_reported_success` is recorded for every run but is never an
input to the verdict; the gap between what the agent claimed and what the state
proves is the headline `honesty_divergences[]` aggregate.

> All artifacts are labelled `PRELIMINARY_NOT_RELEASE_EVIDENCE`. Promotion to
> release evidence is a separate, explicit Owner step.

## What the verdict is derived from

Every verdict comes from state-based oracles reading:

- the durable **run journal** events, ordered by `seq` (`youtab_runtime.run_journal`);
- the **effect ledger** (`youtab_runtime.effect_ledger`);
- the **egress audit** trail (`youtab_runtime.egress_audit`);
- **harness process** evidence (`youtab_runtime.harness_process`);
- files / artifacts on disk (newline-normalized hashing for Win/Linux parity).

`pass | fail | unknown` are all first-class outcomes (`unknown` is never coerced
to pass or 0).

## Modes

| Mode | Provider | Network | Home | Use |
|------|----------|---------|------|-----|
| `deterministic` | none | none | isolated temp | **CI-required.** Drives the REAL WAVE-26 substrate + controlled fault injection; gates the runtime plumbing. |
| `local_runtime` | none | in-process/local | isolated | Drives `/api/runtime/v1` via the real signing client against a local runtime. |
| `real_provider` | live (Owner secret file) | yes | isolated | **Default OFF.** Model-quality dimensions. Credentials are read from an Owner file and never logged/displayed/copied/persisted. |

## Deterministic vs real-provider dimensions

Deterministically testable (runtime plumbing — the CI gate):
completion plumbing, forbidden-tool rejection, prompt/tool-output injection &
exfil (under the network-deny posture, definitive by construction), provider /
tool failure recovery, timeout/cancel, restart/state recovery, effect
idempotency, duplicate-run dedup, concurrency isolation, cross-principal
isolation, usage-unknown honesty, tool-call budget, and truthful-incomplete
detection.

Require a live provider (defined with oracles, reported `unknown` +
`OWNER_LIVE_PROVIDER_ACTION_REQUIRED` in deterministic mode): task-completion
quality, tool-selection / argument accuracy, real token counts & cost, and
model-driven injection resistance.

### Scope of the runtime-plumbing green

The deterministic suite proves the WAVE-26 substrate *modules* (effect ledger,
run journal, egress audit, harness process control) behave correctly —
`duplicate_effect_rate == 0`, `cross_principal_leakage_rate == 0`,
`unauthorized_egress_rate == 0` reflect the ledger/boundary the harness itself
drives. They are NOT a claim that every production side effect is guarded:
effect-level idempotency is wired into the durable run-retry endpoint (and
`utils.atomic_write_text` accepts an opt-in `effect=` guard), but broad
production write/network callers are not yet routed through the ledger, and the
egress boundary is not yet wired into production adapters (see
`tests/tools/test_egress_boundary_enumeration.py` → `UNIVERSAL_EGRESS_COVERAGE`).
That production wiring is Owner-gated follow-on work.

## Running

From the repo root:

```bash
# Deterministic offline (no provider, no network). Self-verifying gate.
python -m tests.benchmark.harness.cli run --mode deterministic --out .bench

# Validate the task bank only (>=40 scenarios, all 20 families).
python -m tests.benchmark.harness.cli validate

# Via the canonical runner (per-file isolation, no retries):
scripts/run_tests.sh tests/benchmark -m benchmark --file-retries 0

# Real provider (Owner-only; secret read from a file, never logged):
python -m tests.benchmark.harness.cli run --mode real_provider \
  --base-url https://runtime.example --secret-file /path/to/secret --out .bench
```

Outputs: `results.jsonl` (one record per scenario×repetition, contract 7) and
`summary.json` (contract-9 metrics + `honesty_divergences[]` + parity + coverage).

## Layout

```
tests/benchmark/
  harness/
    schema.py      # record (contract 7), Observation, Verdict, helpers, git HEAD
    oracles.py     # state-based verdict functions (registry: ORACLES)
    executors.py   # deterministic drivers of the REAL substrate (registry: EXECUTORS)
    seam.py        # DeterministicSubstrateSeam + HttpRuntimeSeam (real signing)
    metrics.py     # all contract-9 metrics (unsupported => unknown + provenance)
    recorder.py    # results.jsonl + summary.json + honesty_divergences
    runner.py      # isolated fixture per run, concurrency pool, bounded waits
    taskbank.py    # load + validate the JSON manifest
    cli.py         # entry point
    auth_client.py # real-principal /api/runtime/v1 client (WAVE-26 Agent 1)
  tasks/manifest.json  # >=40 versioned scenarios across the 20 families
  test_task_bank.py            # manifest integrity (marked benchmark)
  test_benchmark_deterministic.py  # deterministic gate + smoke (marked benchmark)
```

The `benchmark` pytest marker keeps this suite out of default collection; the
dedicated `benchmark-deterministic` CI job re-selects it and also runs the CLI as
an authoritative backstop.
