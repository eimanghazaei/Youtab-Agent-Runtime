# Durable Execution — Real API Route Map (before wiring)

Codex directive: map the actual existing API and reuse ONE canonical task_id +
route contract; do not introduce a parallel `/v1/runs` product API.

## Existing routes (mechanical, `gateway/platforms/api_server.py`)

Registered at `:1843-1848` (and mirrored `:2898-2903`):

| Method | Route | Handler | Today | Durable wiring plan |
|---|---|---|---|---|
| POST | `/v1/runs` | `_handle_runs` | starts a run; state in RAM `_run_statuses` | create_run in `RunStore` BEFORE work; return existing `run_id` |
| GET | `/v1/runs/{run_id}` | `_handle_get_run` | reads RAM status | `RunStore.get_run` |
| GET | `/v1/runs/{run_id}/events` | `_handle_run_events` | single-consumer SSE, queue popped on disconnect | `RunStore.get_events(from_seq)` + `Last-Event-ID`; multi-consumer |
| POST | `/v1/runs/{run_id}/approval` | `_handle_run_approval` | approval signal | approval event + `WAITING_APPROVAL` state |
| POST | `/v1/runs/{run_id}/stop` | `_handle_stop_run` | interrupt | `RunStore.request_cancel` (explicit authorized cancel) |
| — | `/v1/runs/{run_id}/result` | (none) | result folded into status | add: `RunStore` result_ref lookup, durable after restart |
| GET/POST | `/v1/responses`, `/v1/responses/{id}` | ResponseStore (SQLite) | durable response snapshots | unchanged (different surface) |

**Decision:** the canonical API is the EXISTING `/v1/runs` — wire its handlers to
`RunStore`, preserving the same `run_id`/`task_id`. No parallel product API is
created. Add only `/v1/runs/{run_id}/result` for durable result retrieval.

## Runtime / delegation paths (same canonical id)
- `tools/delegate_tool.py` (sync) + `tools/async_delegation.py` (background,
  `state.db`): parent-wait must return RUNNING + the existing task_id via
  `RunStore.wait_for_terminal`; the child continues under its own lease. The
  delegation `delegation_id` is carried in `RunIdentity.delegation_id` — one id
  space, no duplicate identity.

## Web / Electron consumers (honest current state)
- Desktop chat uses `tui_gateway` JSON-RPC over `/api/ws` (session.resume, 20s
  orphan grace) — NOT `/v1/runs`.
- Browser `/chat` uses the PTY keep-alive (`/api/pty`, 30-min ring buffer).
- `/v1/runs` is the OpenAI-compatible external API surface.
So "one canonical task_id across Gateway + Runtime + Web/Electron" spans two
consumer transports today. P0-D (Web/Electron continuity on the durable contract,
IR-2) is a follow-on that binds the desktop/browser consumers to the same
`RunStore` task identity; it is OPEN, not done. No second Web/Desktop task store
will be created.

## `/v1/agents/run`
Not present in this Runtime repo (that route family belongs to the separate
youtab-ai-os gateway). Cross-gateway id unification is an integration item for
Master Integrator, tracked via IR-2.
