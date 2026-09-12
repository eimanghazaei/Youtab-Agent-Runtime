# Reproduction — WAVE-30H local/tunnel baseline

This baseline was produced with the managed E2E matrix harness
`scratchpad/pool_e2e_matrix.py` (a benchmark harness, NOT product code) driving the real
managed dispatch path. It is **environment-specific** (a live Ollama model over an SSH
tunnel) and cannot be reproduced byte-for-byte without that exact backend.

## Preconditions
- A reachable Ollama serving `youtab-qwen35-9b-agent-64k:latest` on loopback `127.0.0.1:11435`
  (here via an SSH tunnel to a macOS host).
- `.venv-qual` (Python 3.12) with the runtime installed.
- Managed trust mode with a valid Ed25519 Simorgh grant signer (test keys) and the runtime
  service secret supplied via a mode-0400 file (never an env var; never committed).

## Steps (as run)
1. `seed` — create a managed run, capture the exact env + `youtab --cli chat` command.
2. `matrix --config cold --reps N --timeout 700` — sequential cold-spawn runs.
3. `matrix --config pool --reps N --timeout 700` — sequential pooled runs (single-use warm workers).
4. `merge` — aggregate per-rep records; compute p50/p95 and stage separation from `run_journal`.

## Notes on comparability
- Cold and pool used identical model / prompt / tools / host / profile / config; only the
  spawn path differed.
- The short-timeout (420 s) cold run is retained separately (`raw/matrix_cold_t420.json`);
  its extra timeouts are a timeout artifact, not a cold-vs-pool effect.
- Sequential only — never concurrent against the single Mac Ollama.
- The harness grant used `max_total_tokens` sized to the ~7.4 K-token prompt (an 8 K value
  caused a single-turn budget halt; not a product limit).
