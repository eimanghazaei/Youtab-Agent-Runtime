# Worker pool — production activation & operations (WAVE-30H)

The pre-warmed single-use worker pool (`youtab_agent_cli/worker_pool.py`) removes
the per-run interpreter/import startup cost from the managed dispatch critical
path. It is **integrated** into the runtime dispatcher and **OFF by default**. This
document is the activation contract: exact config, how it is selected, fail-closed
behaviour, rollout, rollback, health checks and metrics.

> Status labels used across the WAVE-30H evidence: **implemented in Runtime** (done),
> **enabled in production config** (via the env below), **proven on real managed E2E**
> (see the evidence bundle), **tested hermetically** (unit/integration), **pending
> Linux CI** (authoritative required checks — Owner-gated push).

## 1. Configuration

| env var | default | meaning |
|---|---|---|
| `YOUTAB_AGENT_RUNTIME_WORKER_POOL` | unset (OFF) | `1`/`true` enables the pool on the real-model spawn path |
| `YOUTAB_AGENT_RUNTIME_WORKER_POOL_SIZE` | `2` | number of warm workers; valid range **1–16** |
| `YOUTAB_ECO_RESOLVE_CACHE` | unset (OFF) | set by the pool worker itself; opt-in process-scoped engine-resolve cache |
| `YOUTAB_ECO_KEEP_ALIVE_MAX_SECONDS` | `1800` | keep_alive residency ceiling |
| `YOUTAB_ECO_KEEP_ALIVE_MEM_MIN_AVAIL` | `0.15` | available-memory fraction below which residency is capped |
| `YOUTAB_ECO_KEEP_ALIVE_PRESSURE_SECONDS` | `300` | residency used under memory pressure |

**Exact production activation (per host, staged):**
```
YOUTAB_AGENT_RUNTIME_WORKER_POOL=1
YOUTAB_AGENT_RUNTIME_WORKER_POOL_SIZE=2
# optional keep_alive residency (bounded/mem-aware policy applies):
# model.ollama_keep_alive: "5m"   (in the ECO profile config)
```

## 2. Selection path (how it is chosen)

The dispatcher spawn seam is `runtime._mode_aware_spawn`. Precedence:
`_spawn_override` (tests) → per-task `deterministic` mode (non-prod) → **worker pool
when enabled** → classic cold `_default_spawn`. The pool is used only when
`YOUTAB_AGENT_RUNTIME_WORKER_POOL` is set **and** the size is valid **and** the pool
started. Proven by `tests/youtab_runtime/test_worker_pool.py::test_seam_routes_real_model_spawn_to_pool_when_enabled`.

## 3. Fail-closed behaviour (never silently unsafe)

- **Invalid/unsafe size** (non-integer, `<1`, `>16`, empty, non-integer float): the
  pool is **disabled stickily** and every run uses the classic cold spawn. It is never
  enabled with a bad size and never spawns an unbounded fleet.
- **Pool creation failure**: disabled stickily → cold spawn. The board keeps running.
- **Saturation** (all warm workers busy past the backpressure window): the individual
  `spawn()` falls back to a fresh cold spawn for that run — the board never stalls.
Proven by `test_invalid_or_unsafe_pool_size_fails_closed` (6 cases),
`test_pool_creation_failure_fails_closed`, `test_saturated_pool_backpressure_falls_back_not_stall`.

## 4. Rollout

1. Deploy with the pool **OFF** (default). No behaviour change.
2. **Canary one host**: set `YOUTAB_AGENT_RUNTIME_WORKER_POOL=1`, `SIZE=2`. Watch the
   metrics in §6 for one dispatch cycle.
3. Widen host-by-host once the canary is healthy. Do **not** flip a global default —
   activation is per-host env, so a bad host is contained.

## 5. Rollback

- **Instant, no code change:** unset `YOUTAB_AGENT_RUNTIME_WORKER_POOL` (or set `0`).
  New runs immediately use the classic cold spawn; the pool is torn down (zero-survivor)
  when the dispatcher stops. No in-flight run is affected — each pooled worker is
  single-use and finishes its own run.
- **Code rollback:** the change is additive and behind the flag; reverting the
  worker-pool commit restores the exact prior spawn path.

## 6. Health checks & operational metrics

`WorkerPool.stats()` returns `{size, total, ready, assigned}` — expose it on a health
probe. Recommended signals to watch:

| signal | source | healthy |
|---|---|---|
| warm workers ready | `stats().ready` | ≈ `size` between dispatches |
| assigned (in-flight) | `stats().assigned` | ≤ `size` |
| warm-boot time | worker `ready.json` `warm_boot_ms` | steady (host-dependent, ~seconds) |
| saturation fallbacks | count of `spawn()` cold-spawn fallbacks | rare; a rising rate ⇒ raise `SIZE` |
| stale rejections | idle-TTL evictions | occasional (idle boards) |
| crash replacements | dead-worker refills | rare; a spike ⇒ investigate the worker |
| teardown survivors | `close()` `zero_survivors` | always `true` (0 survivors) |

## 7. Isolation & per-run binding (safety)

Every run gets a **fresh** single-use worker with the **identical** per-run binding a
fresh spawn would get (`kanban_db.build_worker_invocation`: tenant, user, grant, task,
workspace, board, correlation, profile, model/provider override). Because a worker
serves exactly one run then exits, no grant/tenant/memory/tool/secret/env state can
leak between runs. Proven by the two-tenant isolation + one-run-per-worker tests and
the managed-admission integration test.
