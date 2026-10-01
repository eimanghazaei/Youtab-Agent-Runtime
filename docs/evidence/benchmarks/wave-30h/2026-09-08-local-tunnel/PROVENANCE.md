# Provenance — WAVE-30H local/tunnel baseline

| Field | Value |
|---|---|
| Repository | `github.com/eimanghazaei/Youtab-Agent-Runtime` |
| Branch | `feat/wave30d-track-a-local-binding` |
| Committed HEAD at benchmark time | `40b3d69aa08c8c3df177f3495c0474a26cc110ed` |
| Uncommitted context | The step-3 last-mile working-tree changes (managed admission on the CLI path, capability-manifest freeze, WorkerPool, keepalive) were UNCOMMITTED at benchmark time. The pool code then pre-dated the 2026-09-09 `env_drop`/`_all`/idempotency fixes. |
| Runner | `runner="cli"` — the real production agent worker (no deterministic runner) |
| Model | `youtab-qwen35-9b-agent-64k:latest` |
| Model digest | `b7b9afeaf023a549…` (as recorded in the run) |
| Engine transport | Ollama over SSH tunnel, loopback `127.0.0.1:11435` |
| Host environment | Windows 11 client; model served on a macOS host via SSH tunnel |
| Execution | Sequential (never concurrent against the single Mac Ollama); all repetitions retained incl. timeouts |
| Qualification venv | `.venv-qual` (Python 3.12) |

## Headline-claim → raw-record traceability

| Headline claim | Raw source |
|---|---|
| worker startup cold ~3.1 s p50 | `raw/matrix_cold.json` (+ `raw/matrix_cold_t420.json`) `startup_ms` |
| worker startup pool ~0.84 s p50 | `raw/matrix_pool.json` `startup_ms` |
| cold + pool both reach terminal `completed` | `raw/matrix_cold.json`, `raw/matrix_pool.json` per-rep `status` |
| one-run-per-worker / zero-survivor | `report/GATE2-RESULTS.md` §6 (`pool_proof`: total=3 for 2 runs; survivors=[]) |
| E2E model-dominated, cold≈pool | `raw/matrix_merged.json` aggregate + per-rep TTFT |

Values not derivable from a retained raw record are NOT asserted here.
