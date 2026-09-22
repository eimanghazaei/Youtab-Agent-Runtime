# Durable Execution — Canonical Checklist (live)

One live checklist for this stream. Updated in place; no versioned reports/bundles during implementation.

**Verdict:** `PHASE 0/1 AUDIT COMPLETE_NOT_REVIEWED` · `PHASE 2 REPRODUCTION IN PROGRESS` · `DURABLE EXECUTION PRODUCT NO-GO`.

## Environment status
- ⚠ Embedded SQLite 3.49.1 hits the WAL-reset bug advisory; kanban falls back to journal_mode=DELETE (environmental, non-blocking; `youtab update` repairs managed installs).
- Authoritative env: `.venv` in worktree, Python 3.12.10, `uv 0.8.17` + `uv sync --frozen --extra dev` (CI-pinned). ✅ 19/19 on async_delegation test. Root worktree venv untouched. Disk delta ≈1 GB (13 GB free).
- Run recipe: `TZ=UTC PYTHONHASHSEED=0 LANG=C.UTF-8 ./.venv/Scripts/python.exe -m pytest <file> -p no:cacheprovider`.

## Claim-ID inventory (30) → see ledger R2.2 for full classification
- REPRODUCED_CONFIRMED 0 · PRESENT_NOT_YET_REPRODUCED 11 · ALREADY_FIXED_ON_BASE 6 · REPORT_CLAIM_DISPROVED 4 · UPSTREAM_ONLY 0 · NOT_APPLICABLE_TO_RUNTIME_REPO 4 · INCONCLUSIVE_ENV_UNAVAILABLE 1 · DEFERRED_TO_NAMED_OWNER 4.

## Phase 2 reproduction status (deterministic; failing repro REQUIRED before any prod edit)
| id | target claim | status | repro path | result |
|---|---|---|---|---|
| P0-A | interactive task loss `/v1/runs` in-memory [C-WEB-1] | DONE | tests/durable_execution/test_p0a_interactive_run_loss.py | 3/3 pass — fresh adapter loses run on restart; /v1/responses (SQLite) is durable contrast |
| P0-B | async delegation restart/resume [C-2.1d] | DONE | tests/durable_execution/test_p0b_async_delegation_restart.py | 2/2 pass — dead-owner 'running'->'unknown' (status recovered, NOT resumed); live owner untouched |
| P0-C | configured child timeout → summary=None [C-2.1c] | DONE | tests/durable_execution/test_p0c_configured_child_timeout.py | 1/1 pass — injected 0.2s timeout -> status timeout, summary None, partial discarded, no recoverable state (only diagnostic log) |
| P0-D | Kanban false health (liveness≠progress) [C-2.2b] + PID/breaker [C-2.2c] | DONE | tests/durable_execution/test_p0d_kanban_false_health.py | 2/2 pass — heartbeat advances last_heartbeat_at only (no step/checkpoint marker); breaker trips to 'blocked' at limit 2 |
| P0-E | effect-then-die (via read-only Lane effect-ledger boundary) [C-2.2d] | TODO | — | — |
| P0-F | multiplex hooks: secondary security hook not invoked [C-3.4] | TODO | — | — |
| P0-G | Windows MCP lifecycle: orphan/shared-loop [C-3.2b/c] | TODO | — | — |
| P0-H | Web/Electron reconnect from last acked sequence [C-WEB-2] | TODO | — | — |

## Ownership mapping (no duplicate ledgers)
- Durable Execution OWNS: durable task/run identity, task event sequence, execution checkpoints, restart/resume, parent/child lifecycle, progress watchdog, cancellation, reconnect + late-result delivery.
- Lane 1/2 OWN (read-only here): effect ledger, approvals, worker lease, UNKNOWN/reconciliation, receipts, governed execution. Lane1 `4edbbe78`, Lane2 `1ed19f09`, Lane3 `474eac31`.
- Where Lane APIs absent on base → emit typed interface request; do NOT build second ledger/approval/reconciliation.

## Design grounding (bind before implementing; documented vs inferred)
- Failure detector (Chandra–Toueg): timeout = suspicion, not proof of failure.
- Leases/fencing (Gray–Cheriton): ownership + safe takeover via epoch/fencing token.
- RPC ambiguity (Birrell–Nelson): unknown outcome ≠ failure; reconcile.
- Sagas (Garcia-Molina–Salem): long effects need compensations/reconciliation.
- Durable checkpoint / Continue-As-New (Temporal), checkpointers (LangGraph).
- Claude background-task identity/output retrieval; Codex async long-running + compaction (documented behavior only; internals not claimed).

## Commits
- `533b12e7` docs(durable-exec): Phase 0/1 claim ledger and call-path matrix.
- (pending) docs(durable-exec): Revision 2 corrections + checklist.

## Integration dependencies / blockers
- Lane 1/2 canonical effect-ledger/approval/receipt APIs on the eventual integrated base (typed requests to be drafted per P0-E).
- Packaged desktop artifact for C-LIC-2 final verdict (no build exists yet).
- Enterprise Memory typed interface (C-2.5) — read-only coordination.

## Hook fix isolation (C-3.4)
Separate small commit ONLY after: failing two-profile test; explicit hook-ownership contract; proof default-profile unchanged; failure visible (never silently ignored). Do NOT mix into task-journal changes.
