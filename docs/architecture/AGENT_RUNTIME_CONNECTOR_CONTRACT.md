# Agent Runtime Connector Contract (v1)

**Status:** DRAFT — AR-PROD-01 end-to-end connect. Not Canon; not merged.
**Owner:** authorized 2026-08-19 (see the Master Tracker AR-PROD-01 block).
**Purpose:** the versioned service contract between the **youtab-ai-os `/v1/agents`
control plane** and the **Youtab-Agent-Runtime engine**, consumed by a
`AgentRuntimeConnector` in the gateway. The browser never sees this surface.

```text
Web OS / Windows / iOS  →  youtab-ai-os /v1/agents  →  AgentRuntimeConnector  →  Youtab-Agent-Runtime /api/runtime/v1  →  kanban engine (real runs, sandboxes, tools, skills)
```

## Why a new engine surface is required (evidence)

The engine has **no networked run-lifecycle API today** and its signed-command
boundary is not wired to execution:

- `youtab_runtime/worker.py` — signed `BrainCommandEnvelope` admission is
  **stdin/stdout only**, "exposes no public listener", and **dead-ends at
  `{"accepted":true}`** — nothing dispatches an admitted command into kanban
  (no importer of `AuthorityBoundary`/`BrainCommandEnvelope` in `kanban*`).
- The real run engine (`kanban_db.create_task` + `dispatch_once`) is
  **subprocess/CLI-driven**, not networked.
- `web_server.py` exposes agents/tools/skills/sessions read APIs but **no
  create/start-run, run status, run-events stream, or cancel** endpoint.
- The only run-over-network path (`/api/ws` → `tui_gateway`) is an interactive
  `sid`-based chat protocol, not a run lifecycle with run-ids/status/cancel.

Therefore AR-PROD-01 adds a **thin, service-authenticated `/api/runtime/v1`
router that wraps the existing kanban engine** — the single authoritative run
store — introducing **no new state machine and no duplicate agent/run store**.
It also wires the `youtab_runtime` signed-command path so an admitted command
dispatches a real kanban task (closing the dead-end), proving the full command
path per the directive.

## Transport & auth

- **Transport:** HTTP/JSON (REST) to the engine's FastAPI app, plus an
  incremental **run-events** endpoint (cursor poll now; upgradeable to
  streaming HTTP/WS without a contract change — see Events).
- **Service auth (engine side):** the router registers its exact paths via
  `dashboard_auth/token_auth.register_token_route()` and authenticates the
  gateway with a **service bearer secret** (`YOUTAB_AGENT_RUNTIME_SERVICE_SECRET`,
  ≥256-bit, constant-time compare — the drain-secret pattern), so it works on
  non-loopback binds too. On a co-located **loopback** bind the existing
  `X-Youtab-Session-Token` seam also applies. No browser credential ever reaches
  this surface.
- **Identity propagation:** the gateway is the identity authority. Every request
  carries gateway-verified context in headers — `X-Youtab-Tenant-Id`,
  `X-Youtab-User-Id`, `X-Youtab-Roles`, `X-Youtab-Correlation-Id`,
  `Idempotency-Key` — **never** trusted from a browser. The engine records
  `tenant`/`created_by` on the kanban task from these. Fail-closed: if the
  service secret or tenant context is absent/invalid, the engine refuses
  (401/403), never executes.
- **Signed commands (execution path):** run creation may additionally carry a
  `youtab.agent-command.v1` Ed25519-signed `BrainCommandEnvelope`; the engine
  verifies it via `AuthorityBoundary.admit()` (signature, allowed toolsets/memory
  scopes, replayed-nonce) **and then dispatches** the admitted command into
  kanban — closing today's dead-end.

## Versioning / negotiation

- Path is version-pinned: `/api/runtime/v1/*`. `GET /api/runtime/v1/capabilities`
  returns `{ contract_version, engine_version, supported: [...], sandbox_backends,
  reasoning_strategies }`. The connector performs a **compatibility handshake** on
  connect; a version/contract mismatch surfaces as `runtime_incompatible`
  (never a silent empty success).

## Endpoints (engine `/api/runtime/v1`, wrapping real kanban)

| Method & path | Wraps | Returns |
|---|---|---|
| `GET /health` | `web_server /api/health` + readiness | `{ ok, engine_version, contract_version, auth_required }` |
| `GET /capabilities` | registries | `{ contract_version, engine_version, supported[], sandbox_backends[], reasoning_strategies[] }` |
| `GET /agents` | `profiles.list_profiles()` + `agent_identity.v1.json` | `{ agents: [AgentProjection] }` |
| `GET /agents/{id}` | profile + identity | `AgentProjection` |
| `POST /runs` | `create_task(...)` + trigger `dispatch_once` | `RunSummary` (`status: queued|running`) |
| `GET /runs` | `list_tasks(tenant=...)` | `{ runs: [RunSummary], total, limit, offset }` |
| `GET /runs/{id}` | `get_task` + latest `get_run` | `RunDetail` |
| `GET /runs/{id}/events?after=<cursor>` | `task_events WHERE id > ?` | `{ events: [Event], cursor, terminal }` |
| `POST /runs/{id}/cancel` | interrupt + status transition + worker signal | `{ run_id, status }` |
| `GET /tools` | `tools/registry` (`/api/tools/toolsets`) | `{ tools: [...] }` |
| `GET /skills` | `tools/skills_tool` (`/api/skills`) | `{ skills: [...] }` |
| `GET /sandboxes` | `tools/environments` (`/api/tools/terminal/backends`) | `{ backends: [...], default }` |
| `GET /runs/{id}/artifacts` | task attachments/outputs | `{ artifacts: [ArtifactRef] }` |

### Schemas (v1)

```text
AgentProjection = {
  id, display_name, description, status, version,
  capabilities[], tools[], skills[], sandbox_profile,
  supported_modes[], scope, created_at, updated_at, runtime_available
}
RunSummary = { run_id, agent_id, agent_name, status, created_at, tenant_id }
RunDetail  = RunSummary + { task, result, error, started_at, finished_at,
                            tokens?, retries, current_run_id, checkpoints? }
Event      = { id (cursor), run_id, kind, payload(redacted), created_at }
ArtifactRef= { id, name, kind, size, download_url(gateway-issued) }
```

## Error taxonomy (honest — never a fake empty success)

The connector translates engine failures into these product error classes,
distinct from an **authenticated-but-empty** result:

`runtime_unavailable` · `runtime_incompatible` · `unauthorized` · `forbidden` ·
`entitlement_denied` · `invalid_request` · `conflict` · `timeout` ·
`run_partially_completed` · `run_cancelled` · `retryable_failure` ·
`terminal_failure`.

An empty agent/run list is returned **only** as an authenticated empty result,
with `runtime_available: true`; it is never used to mask an unavailable or
incompatible runtime.

## Events / streaming

`GET /runs/{id}/events?after=<cursor>` returns events with `id > after`, a new
`cursor`, and a `terminal` flag when the run reached a terminal state. This is
the reliable, resumable substrate (the gateway may expose it to the UI as SSE/WS
and reconnect from the last acknowledged `cursor`). Ordering is by `(created_at,
id)`; duplicate suppression is by monotonic `id`. Polling here is a real
resumable cursor stream — the gateway/UI must not describe any fallback poll as
"live" unless it is cursor-ordered and terminal-correct.

## Isolation invariants (tested)

- Cross-tenant / cross-user reads of runs, events, logs, artifacts, and sandbox
  state are impossible at every boundary (query filter on `tenant`/`created_by`).
- The engine never trusts client-supplied tenant/user/role; only the
  gateway-verified headers (behind the service secret) set them.
- Replayed/expired/tampered signed commands are rejected by `AuthorityBoundary`.
- No existing engine capability is removed or weakened; this surface is additive.

## Local integration boot (for E2E proof)

```
YOUTAB_AGENT_RUNTIME_SERVICE_SECRET=<≥32-byte test secret> \
YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN=<known> \
YOUTAB_AGENT_SERVE_HEADLESS=1 \
.venv-qual/Scripts/python.exe -m youtab_agent_cli.main dashboard \
  --host 127.0.0.1 --port 8081 --no-open --skip-build
```

Loopback keeps `auth_required=False`; the connector authenticates with the
service bearer. `.venv-qual` (Python 3.12 + fastapi/uvicorn) is the supported
interpreter. This proves the engine + connector legs; the full gateway chain
additionally requires Postgres+pgvector/JWT (heavier; may be environment-gated).
```
