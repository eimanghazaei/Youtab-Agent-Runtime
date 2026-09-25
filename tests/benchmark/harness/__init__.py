"""Benchmark harness package (WAVE-26 contract 7).

Modules:
  * :mod:`schema`   — the per-run record (contract 7) + observation/verdict types.
  * :mod:`oracles`  — state-based verdict functions (read journal/effects/egress/files).
  * :mod:`executors`— deterministic drivers that produce REAL substrate state.
  * :mod:`seam`     — durable HTTP seam (real signing) + deterministic substrate seam.
  * :mod:`runner`   — isolated-fixture-per-run driver + concurrency pool.
  * :mod:`metrics`  — all contract-9 metrics (unsupported => "unknown" + provenance).
  * :mod:`recorder` — results.jsonl + summary.json + honesty_divergences.
  * :mod:`taskbank` — load + validate the versioned JSON task manifest.
  * :mod:`auth_client` — real-principal client for ``/api/runtime/v1`` (Agent 1).
"""
