# Hermes → Agent Runtime — Capability-Parity Ledger

Purpose: prove that Hermes-derived capabilities are **not silently removed** as the
engine is productized behind the multi-tenant gateway and Web OS. No capability is
`REMOVED_WITH_OWNER_DECISION` unless an actual Owner decision is recorded (none is).
Removing Hermes branding and hiding physical provider/model/URL/IP details are
**not** capability removal.

## Key architectural finding — two HTTP planes

The engine exposes two surfaces:

- **Tenant-safe service plane** `/api/runtime/v1/*` — one file
  `youtab_agent_cli/web_routers/runtime.py`, gated by `require_service_identity`,
  and the ONLY surface the gateway connector calls
  (`services/gateway/app/agents/runtime_connector.py`, `_API_PREFIX="/api/runtime/v1"`).
- **Single-operator dashboard plane** `/api/*`
  (`web_routers/{mcp,profiles,sessions,skills,tools}.py`, `web_server.py`) — **not**
  proxied to tenants.

A capability that lives only on the dashboard plane is therefore *present but not
tenant-wired* — never proxied raw to multi-tenant users (that would be the
forbidden single-operator-dashboard-to-tenant proxy).

Classification vocabulary: `PRESERVED`, `EXTENDED`, `TENANT-SAFE_AND_WIRED`,
`PRESENT_BUT_NOT_TENANT_WIRED`, `INTENTIONALLY_HIDDEN_FROM_PRODUCT_UI`,
`REMOVED_WITH_OWNER_DECISION` (unused — no Owner decision recorded).

## Ledger

| # | Capability | Engine surface | Gateway surface | Web OS | Authz | Classification |
|---|---|---|---|---|---|---|
| 1 | Profiles / Agent config | `youtab_agent_cli/profiles.py` (file profiles); service `GET /api/runtime/v1/agents` (`runtime.py`) | `agents/definition_routes.py` `/v1/agents/definitions` CRUD; `db_models.py::AgentDefinition` | `web-os/agent-runtime.js` viewBuilder/viewAgents | read=member, write=operator+, delete=tenant_admin+, tenant-scoped | **TENANT-SAFE_AND_WIRED** (EXTENDED: DB-backed versioned tenant definitions over file profiles) |
| 2 | Sessions | `sessions_cmd.py`, TUI `tui_gateway/methods_session.py`, dashboard `/api/sessions*` | `agents/session_routes.py` `/v1/sessions`; `session_models.py::AgentSession` (project_id FK) | viewSessions/viewSession | user, scoped (tenant_id, owner_user_id) | **TENANT-SAFE_AND_WIRED** (EXTENDED: tenant session grouping) |
| 3 | Skills | registry `tools/skills_tool.py`; service `GET /api/runtime/v1/skills`; dashboard `web_routers/skills.py` | `agents/routes.py` `GET /v1/agents/skills` (read-only proxy) | viewTools ("Tools & Skills"), composer chips | user, tenant-scoped (read-only) | **TENANT-SAFE_AND_WIRED** (author/toggle stays dashboard-only by design) |
| 4 | Projects & folders | none on runtime plane (engine groups by *runs*) | `agents/project_routes.py` `/v1/projects`; `project_models.py::AgentProject` | viewProjects | user, scoped (tenant_id, owner_user_id) | **TENANT-SAFE_AND_WIRED** (net-new product concept) |
| 5 | MCP lifecycle | dashboard-only `web_routers/mcp.py` (`/api/mcp/servers` CRUD, `/test`, `/auth`, `/oauth/*`, `/catalog*`) | **none found** (no `/v1/agents/mcp*`; connector has no MCP method) | none | dashboard single-operator | **PRESENT_BUT_NOT_TENANT_WIRED** |
| 6 | Tools | `toolsets.py`; service `GET /api/runtime/v1/tools`; dashboard `web_routers/tools.py` | `agents/routes.py` `GET /v1/agents/tools` (read-only proxy) | viewTools (toolRowNodes) | user, tenant-scoped (read-only) | **TENANT-SAFE_AND_WIRED** (config/env stays dashboard-only) |
| 7 | Provider configuration | `providers/base.py::ProviderProfile`, `plugins/model-providers/*` (keys via env/config); dashboard `/api/tools/toolsets/{name}/provider` | not exposed as provider config; `AgentDefinition.model_preference` is an internal hint; connector never sends raw provider/model/endpoint | none (builder shows performance/memory, never provider/model/endpoint) | engine holds credentials; product plane strips them | **INTENTIONALLY_HIDDEN_FROM_PRODUCT_UI** |
| 8 | Sandboxes | service `GET /api/runtime/v1/sandboxes`; backends local/docker/singularity/modal/daytona/ssh; `tools/terminal_tool.py` | `agents/routes.py` `GET /v1/agents/sandboxes` (read-only proxy) | viewTools ("execution environments") | user, tenant-scoped (read-only) | **TENANT-SAFE_AND_WIRED** (no product-side provisioning) |
| 9 | Run lifecycle | `web_routers/runtime.py` — `POST /api/runtime/v1/runs` (signed+idempotent), `/runs`, `/events`, `/artifacts*`, `/logs`, `/cancel`, `/retry` (no resume; recovery=retry) | `agents/routes.py` `/v1/agents/run`, `/runs`, `/events`, `/artifacts*`, `/cancel`, `/retry`, `/logs`; `engine_delegate.py`+`runtime_connector.py` | viewRuns/viewRun, composer | run CRUD=user scoped; `/logs`+`/diagnostics`+`/capabilities`=super_admin; engine plane=service identity + signed | **TENANT-SAFE_AND_WIRED** |
| 10 | Memory | internal `agent/memory_manager.py`, `agent/memory_provider.py`, `agent/background_review.py`; **no runtime/dashboard memory route** | no dedicated memory route in agents plane; recall config only via `AgentDefinition.memory_recall_kinds`/`_limit` through create_run (platform memory lives in `app/memory`, outside agents plane) | builder exposes only the two recall fields; no memory browser | recall fields inherit definition gates; store/graph has no tenant route | **PRESENT_BUT_NOT_TENANT_WIRED** (recall *parameters* EXTENDED into tenant definitions+UI; the store/graph has no route/UI) |
| 11 | Multi-Agent | one worker/run via kanban dispatcher (`kanban_db.dispatch_once`/`_default_spawn`); no multi-agent HTTP surface | delegation machinery present — `agents/delegation.py` (`DelegationLease`, strict attenuation), `agents/child_runtime.py` (`spawn_child_run`, ADR-0045: `derive_child_lease` has no runtime caller); only route `POST /v1/agents/goal-runs/{id}/cancel` | none (builder: single-agent strategies only) | goal-run cancel=user scoped; child-lease attenuation enforced but not runtime-invoked | **PRESENT_BUT_NOT_TENANT_WIRED** (child-spawn deferred per ADR-0045) |

## Summary

- **TENANT-SAFE_AND_WIRED (7):** Profiles, Sessions, Skills, Projects, Tools, Sandboxes, Run lifecycle.
- **INTENTIONALLY_HIDDEN_FROM_PRODUCT_UI (1):** Provider configuration (provider/model/endpoint/keys deliberately never leave the engine).
- **PRESENT_BUT_NOT_TENANT_WIRED (3):** MCP lifecycle, Memory (store/graph), Multi-Agent (child-spawn).
- **REMOVED_WITH_OWNER_DECISION (0):** none — no capability was removed; none has an Owner decision.

## Remaining work (per capability)

- MCP lifecycle → design a tenant-scoped `/v1/agents/mcp*` contract + permission engine + web-os UI (do NOT proxy the dashboard plane raw).
- Memory → decide the tenant-scoped memory surface (recall is wired; store/graph browsing is not).
- Multi-Agent → wire `derive_child_lease`/`spawn_child_run` to a runtime caller behind a tenant-safe contract (ADR-0045), then a product UI.
- Tools/Skills/Sandboxes → the read-only projections are wired; enable/author/provision remain dashboard-only pending tenant-safe contracts.

## Confidence / gaps (honest)

Gateway and Web OS evidence is first-hand and complete. Engine evidence is
first-hand for all areas except a deeper internal audit of `agent/memory_manager.py`
(recall/graph capabilities) and any latent multi-agent orchestration; areas 10–11
rest on the confirmed *absence* of any `/api/runtime/v1` or `/v1/agents`
memory/multi-agent route plus the gateway-side delegation code. A deeper
engine-internal audit would harden rows 10–11.
