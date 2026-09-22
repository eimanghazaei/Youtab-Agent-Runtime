# Durable Execution — Canonical Checklist (live)

One live checklist for this stream. Updated in place; no versioned reports/bundles during implementation.

**Verdict:** `PHASE 0/1 AUDIT COMPLETE_NOT_REVIEWED` · `P0-A…H DEFECTS REPRODUCED` · `DURABLE STORE COMPONENT VERIFIED_NOT_INTEGRATED` · `DURABLE EXECUTION PRODUCT NO-GO`.

Storage decision + inventory + deadline matrix: see DURABLE_EXECUTION_STORAGE_DECISION.md. Idempotency now SCOPED (tenant/workspace/principal/operation/key) — commit a40fb392.

## Environment status
- ⚠ Embedded SQLite 3.49.1 hits the WAL-reset bug advisory; kanban falls back to journal_mode=DELETE (environmental, non-blocking; `youtab update` repairs managed installs).
- Authoritative env: `.venv` in worktree, Python 3.12.10, `uv 0.8.17` + `uv sync --frozen --extra dev` (CI-pinned). ✅ 19/19 on async_delegation test. Root worktree venv untouched. Disk delta ≈1 GB (13 GB free).
- Run recipe: `TZ=UTC PYTHONHASHSEED=0 LANG=C.UTF-8 ./.venv/Scripts/python.exe -m pytest <file> -p no:cacheprovider`.

## Claim-ID inventory (30) → see ledger R2.2 for full classification
- (R3, post P0-A…H) REPRODUCED_CONFIRMED 8 · PRESENT_NOT_YET_REPRODUCED 3 · ALREADY_FIXED_ON_BASE 6 · REPORT_CLAIM_DISPROVED 4 · UPSTREAM_ONLY 0 · NOT_APPLICABLE_TO_RUNTIME_REPO 4 · INCONCLUSIVE_ENV_UNAVAILABLE 1 · DEFERRED_TO_NAMED_OWNER 4 = 30.

## Phase 2 reproduction status (deterministic; failing repro REQUIRED before any prod edit)
| id | target claim | status | repro path | result |
|---|---|---|---|---|
| P0-A | interactive task loss `/v1/runs` in-memory [C-WEB-1] | DONE | tests/durable_execution/test_p0a_interactive_run_loss.py | 3/3 pass — fresh adapter loses run on restart; /v1/responses (SQLite) is durable contrast |
| P0-B | async delegation restart/resume [C-2.1d] | DONE | tests/durable_execution/test_p0b_async_delegation_restart.py | 2/2 pass — dead-owner 'running'->'unknown' (status recovered, NOT resumed); live owner untouched |
| P0-C | configured child timeout → summary=None [C-2.1c] | DONE | tests/durable_execution/test_p0c_configured_child_timeout.py | 1/1 pass — injected 0.2s timeout -> status timeout, summary None, partial discarded, no recoverable state (only diagnostic log) |
| P0-D | Kanban false health (liveness≠progress) [C-2.2b] + PID/breaker [C-2.2c] | DONE | tests/durable_execution/test_p0d_kanban_false_health.py | 2/2 pass — heartbeat advances last_heartbeat_at only (no step/checkpoint marker); breaker trips to 'blocked' at limit 2 |
| P0-E | effect-then-die [C-2.2d] | DONE (characterization) | tests/durable_execution/test_p0e_effect_then_die.py | 2 pass — effect ledger ABSENT on base; retry re-execution window; typed boundary IR-1 |
| P0-F | multiplex hooks [C-3.4] | DONE (characterization) | tests/durable_execution/test_p0f_multiplex_hooks.py | 1 pass — secondary-profile security hook inert & silent; SEPARATE commit pending fix |
| P0-G | Windows MCP lifecycle [C-3.2b] | DONE (characterization) | tests/durable_execution/test_p0g_windows_mcp_orphan.py | 3 pass — real Windows child→grandchild orphan; killpg/watchdog POSIX-only. C-3.2c shared-loop still PRESENT_NOT_YET_REPRODUCED |
| P0-H | Web/Electron reconnect [C-WEB-1/2] | DONE (real HTTP E2E) | tests/durable_execution/test_p0h_web_reconnect.py | 1 pass — reconnect 404 (no seq cursor); restart 404 (lost); consumer contract IR-2 |

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

## Phase 3 implementation status
Verdict: `DURABLE EXECUTION IMPLEMENTED_NOT_VERIFIED` (until restart/crash/reconnect + independent exact-SHA verification pass).

| step | scope | status | evidence |
|---|---|---|---|
| 3-A(1) | durable schema/store + migration | DONE `687f33ca` | youtab_runtime/durable_run_store.py; 10 desired-invariant tests + real cross-process acceptance test (wait 2s→RUNNING+task_id, worker survives, reconnect, restart, exactly-once, 0 orphans) |
| 3-B(core) | progress vs liveness + STALLED | DONE (in store) | heartbeat=liveness-only; record_progress advances progress_seq; detect_stalled separate windows → visible STALLED, no kill |
| 3-A(2) | wire `/v1/runs` create/get/result/events to store | TODO | consumer contract IR-2 |
| wait/lifetime | delegate_tool: parent-wait returns typed non-terminal, child continues | TODO | store.wait_for_terminal ready |
| 3-D | profile hook isolation (separate commit) | TODO | baseline P0-F green |
| 3-E | Windows Job Object supervision | TODO | baseline P0-G green; reuse enterprise-lane Job Object primitives |
| 3-C | Lane-1 effect/lease adapter + contract tests | TODO (IMPLEMENTED_NOT_VERIFIED) | EffectLedger Protocol + AbsentEffectLedger fail-closed shipped |

Gates on `687f33ca`: ruff clean · ty clean · git diff --check clean · 27/27 durable_execution tests pass. Author Eiman, 0-attribution. No push.

## Phase 3 progress (Codex #5/#6/#7 cycle)
| item | status | commit | evidence |
|---|---|---|---|
| P0-A storage decision + inventory | DONE | 07b8f083 | DURABLE_EXECUTION_STORAGE_DECISION.md |
| P0-B scoped idempotency + typed RunStore interface | DONE | a40fb392 | 13 p3a tests |
| v1->v2 migration on POPULATED db | DONE | 50a420e7 | test_p3a2_migration.py 3/3 |
| real API route map | DONE | 50a420e7 | DURABLE_EXECUTION_ROUTE_MAP.md |
| durable /v1/runs coordinator + HTTP->RunStore->worker proof | DONE | e8e4feb9 | test_p3c_http_wiring.py 5/5 (short-wait RUNNING, reconnect, once, restart, x-workspace deny, cancel, from_seq) |
| api_server in-place handler swap (delegate existing routes) | OPEN | — | next commit; shipped route still in-memory until then |
| delegate_tool parent-wait decoupling (real path) | OPEN | — | coordinator.wait ready |
| 30-minute wall-clock proof on wired path | OPEN | — | starts after api_server swap |
| Web/Electron continuity (tui_gateway/PTY bridge, IR-2) | OPEN | — | route map documents current transports |
| Lane-1 effect-ledger adapter | OPEN (fail-closed) | — | AbsentEffectLedger |
| crash/adversarial matrix on wired path (P0-F) | OPEN | — | store-level covered; HTTP-level partial |
| Windows Job Object supervision (P0-G) | OPEN | — | orphan baseline P0-G green |
| PostgreSQL backend | OPEN (stub) | — | PostgresRunStore raises |

Suite on e8e4feb9: 38/38 durable_execution; ruff/ty/whitespace clean. Author Eiman, 0-attribution. No push.

## 30-minute wall-clock acceptance (shipped /v1/runs path) — PASS
Evidence: docs/evidence/DURABLE_EXECUTION_30MIN_ACCEPTANCE.json (real aiohttp over the
shipped api_server handlers; long mock agent, 20s progress cadence).
- T+0.6s accepted: HTTP 202 RUNNING + task_id (<10s).
- T+601s: status=running, alive, retrievable, progress_seq=32 (32 durable events) —
  NOT killed by any 600s limit; task recoverable mid-flight.
- T+1823s (~30.4 min): SUCCEEDED, exactly-once (completions=1), durable result_ref,
  93 durable events, /result terminal. verdict PASS.
- Worker in-process (no subprocess) + acceptance process exit 0 -> zero orphan workers.
Note: a store-only or substitute-handler run would NOT satisfy this; this used the
real shipped handlers.
