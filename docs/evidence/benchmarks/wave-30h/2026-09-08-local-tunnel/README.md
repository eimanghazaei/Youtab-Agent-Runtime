# WAVE-30H — Historical local/tunnel benchmark baseline (2026-09-08)

Immutable historical evidence. **New benchmark generations are additive and MUST NOT
overwrite, amend, or replace this directory.**

## Mandated labels

| Label | Value |
|---|---|
| Environment | Windows client → SSH tunnel → macOS / Ollama (loopback `127.0.0.1:11435`) |
| Evidence type | **Historical local/tunnel baseline** |
| Production evidence | **NO** |
| Cloud benchmark | **NO** |
| Linux production-equivalent benchmark | **NO** |
| Tunnel latency present | **YES** |
| Date | 2026-09-08 |
| Source | branch `feat/wave30d-track-a-local-binding`, committed HEAD `40b3d69aa` **plus** the then-uncommitted step-3 working-tree changes (see PROVENANCE) |
| Model / digest | `youtab-qwen35-9b-agent-64k:latest` (digest `b7b9afeaf023a549…` as recorded) |
| Worker-pool result | **Real production CLI worker path** (`runner="cli"`, the genuine agent — NOT a deterministic prototype). See caveat below. |

## What this evidence proves

1. The managed chain reaches a **real terminal completion** through the genuine production path:
   `create_run → HMAC + Ed25519 Simorgh grant → ingress admit + frozen manifest + persist →
   dispatcher → real `youtab --cli chat` worker → establish admitted context →
   authorized `kanban_show` → `kanban_complete` → `completed` (task `done`)`.
2. Cold-spawn and pooled workers both complete (parity), one-run-per-worker, zero-survivor teardown.
3. The WorkerPool's **only** measured benefit is worker startup/import (~2.2 s p50: cold ~3.1 s → pool ~0.84 s).
   Total end-to-end is **model-dominated** (157–385 s per turn) and the pool does **not** change it;
   the E2E/TTFT deltas between cold and pool are within model-latency noise, **not** a pool effect.

## What this evidence does NOT prove (read before citing)

- It is **not** production readiness, **not** Linux-production-equivalent, and **not** a cloud benchmark.
- **Tunnel + pathological Mac model latency** (trivial warm request ~47 s; degrading under load)
  caps statistical power: small completed-n, large variance. This is infrastructure, not code.
- **WorkerPool caveat:** the pooled-worker figures were measured on the WorkerPool implementation
  **as it stood on 2026-09-08**, which pre-dates the 2026-09-09 correctness fixes (`env_drop`
  env-subtraction pruning; dead-worker `_all` pruning; control-plane idempotency ordering). Those
  fixes do **not** affect the startup-latency measurement (they add only microsecond-scale env-diff
  work), but the exact pool code is not byte-identical to the current tree. A Linux production-path
  re-benchmark (separate, pending) supersedes these pool numbers as production evidence.
- The hermetic **unit/regression** WorkerPool tests use a `runner="deterministic"` prototype; those
  are correctness proofs, not latency evidence. The **latency** figures here are from the real
  `runner="cli"` path only.

## Contents

- `report/GATE2-RESULTS.md` — primary report (methodology, raw per-rep records, p50/p95, stage separation).
- `report/GATE2-FINDING.md` — root-cause finding.
- `raw/matrix_{cold,cold_t420,pool,merged}.json` — raw per-run benchmark records (all reps, incl. timeouts).
- `raw/{validate_cold,smoke_cold,smoke_pool}.json` — validation + smoke records.
- `driver-logs/matrix_{cold,pool,smoke}.out` — sanitized benchmark driver logs (dir named `driver-logs` because `.gitignore` excludes any `logs/`).
- `PROVENANCE.md`, `REPRODUCE.md`, `MISSING-ARTIFACTS.md`, `MANIFEST.sha256`.

## Integrity

`MANIFEST.sha256` lists the SHA-256 of every file. All files sanitized (no username, host,
private IP, path, secret, or bearer token); residual-secret re-scan reported CLEAN at assembly.
No PDF was ever produced (see `MISSING-ARTIFACTS.md`) — none is fabricated.
