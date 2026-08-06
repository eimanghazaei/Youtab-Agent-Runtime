# Youtab Agent Runtime — Master Tracker

The single authoritative record of what is done, what is running, and what is
waiting. A row is only ever marked VERIFIED with evidence that can be re-run;
"it looked right" is not a status.

Queued work is never deleted to tidy this file. An item that stops being
relevant is closed with a reason, not removed — a tracker that forgets is worse
than no tracker, because it reads as complete.

## Position

| | |
|---|---|
| Repository | `eimanghazaei/Youtab-Agent-Runtime` |
| Branch | `feat/youtab-agent-runtime-on-main` |
| PR | **#10** (OPEN, draft) |
| Base | `main@cc4cab2f592e60a197e796506de9168f74baf3ea` |
| PR head | `e4dec29587f1581c90e639df3efb0a6f51a32797` |
| **Deployed SHA (protected pre-production)** | **`64b32afb68dc022fc463c72d6e024054fd816e4c`** |

The deployed SHA is tracked separately from the PR head on purpose. They are
not the same thing and have not been the same thing for the whole of this
branch; conflating them is how a "green PR" gets described as shipped.

**No merge and no deployment are authorized.**

## Governed runtime versions

Source of truth is `uv.lock`, which is what the image installs via
`uv sync --frozen` (`Dockerfile:270`). Nothing else may state a version.

| Package | Locked | Enforced by |
|---|---|---|
| fastapi | 0.133.1 | `tests/youtab_runtime/test_framework_version_matches_lock.py` |
| starlette | 1.3.1 | same |
| uvicorn | 0.41.0 | same |

Required CI installs `-e '.[dev,web]'`; the `web` extra carries the same pins.
`.[dev]` alone pins Starlette but not FastAPI, and the core dependency is the
range `fastapi>=0.104.0,<1`, so that path resolved freely and walked a
different router than production builds.

## Workstreams

| # | Workstream | Status | Evidence / return point |
|---|---|---|---|
| 1 | Truthful gateway lifecycle contract | **VERIFIED** | `tests/youtab_agent_cli/test_gateway_lifecycle.py` (77) |
| 2 | Six platform root-cause test fixes | **VERIFIED** | 706 passed on the previously-failing set |
| 3 | Secure completion gate (governed verification registry) | **VERIFIED** | `tests/youtab_runtime/test_completion_gate.py` (26) |
| 4 | Container control-plane isolation gate + Compose hardening | **VERIFIED** | `scripts/youtab/container_isolation_gate.py`; 9 controls; 48 tests |
| 5 | Cloudflare Access registered as auth provider, wired end to end | **VERIFIED** | 28 + 45 tests; one chain, no header trust |
| 6 | Event-stream scope + assertion-only external bind | **VERIFIED** | `events:read`; socket-level enforcement |
| 7 | CSRF + exact Origin/Referer on the external bind | **VERIFIED** | `tests/youtab_agent_cli/test_csrf_and_origin.py` (29) |
| 8 | Route inventory from the real router | **VERIFIED** | `scripts/youtab/route_inventory.py`; 294/255/7/1, 0 unrecognised |
| 9 | Dependency provenance & CI/local/production version parity | **VERIFIED** | this commit; 7 mutations RED, restored GREEN |
| 10 | **Route authorization default-deny** | **ACTIVE — flip landed, gate outstanding** | `authz.authorize` refuses unmapped; 294/294 HTTP + 6/7 sockets enforced; 76 tests. **Router-vs-policy CI gate NOT built.** |
| 11 | Purpose-bound WebSocket tickets | QUEUED | one ticket currently opens any socket |
| 12 | Tenant/user event filtering | QUEUED | scope gate exists; payload filtering does not |
| 13 | Governed CSRF secret-file contract | QUEUED | `YOUTAB_CSRF_SECRET_FILE`, ≥32 bytes, fail closed |
| 14 | Agent Intelligence Floor | QUEUED | needs a measured baseline before it can gate |
| 15 | Cognitive-growth & persistent-memory ADR | QUEUED | blocking prerequisite for Sandbox/Workspace |
| 16 | Secret Broker & Provider Proxy | QUEUED | ADR first |
| 17 | Capability engine & execution sandbox | QUEUED | ADR first |
| 18 | Safe snapshot/rollback service | QUEUED | kit engine rejected: deletes post-snapshot user files |
| 19 | Workspace ingress & Code-to-Agent connection | QUEUED | after sandbox + workspace |
| 20 | File Reader sub-agent | QUEUED | read-only, workspace-scoped |
| 21 | Governed dependency/search/debug egress | QUEUED | default-deny with task profiles |
| 22 | Branding · `USER_REACHABLE → 0` | QUEUED | classifier already at 0 blocking |
| 23 | Kanban → Orchestrator Board migration | QUEUED | product workstream |
| 24 | Frontend Agent launcher PR #72 | **PARKED** | separate repository |
| 25 | Runtime PR #8 | **EVIDENCE ONLY** | untouched; `972f48be7` |

## Known constraints

- **CSRF replay store is single-process.** In-memory nonces; single-use holds
  within one process only. Horizontal scaling stays unqualified until a shared
  atomic store exists.
- **Cookie-session binds have no CSRF coverage.** The gate is scoped to the
  external Access bind. They were never weakened — they were never covered.
- **Docker daemon unavailable locally.** Image build, digest, SBOM and
  container-escape tests are CI/VPS work.
- **`/assets` is conditional.** It exists only when the SPA is built, so route
  qualification must handle both states rather than assume one.

## Workstream 10 — exact position

Authorization is **fail-closed**. `authorize()` refuses any route no table
describes, for every principal including the Owner. Enforcement is
`authz.required_scope`, consulted most-specific first: exact path, then
parameterised pattern, then longest prefix, then the SPA catch-all for paths
outside the application's own roots.

| | |
|---|---|
| HTTP route+method pairs resolving to a decision | **294 of 294** (built) / 293 of 293 (unbuilt) |
| Unmapped HTTP routes | **0** |
| WebSockets enforcing a scope at the upgrade | **7 of 7** |
| Public routes | 16 — 14 login/shell, 2 credential-bearing |
| `authorize()` default-deny | **ACTIVE** |
| Router-vs-policy CI gate | **NOT BUILT** — the remaining work on this row |

Classification is **by cluster**, on Owner direction, not by reading each
endpoint body. The trade was explicit: reading 250 functions first would have
left `/api/pty`, arbitrary file write and the system-prompt endpoint open for
as long as the reading took, and a wrong scope surfaces as a 403 in seconds.
Longest prefix wins, so any cluster can be narrowed without reordering.

Three routes are **not** on the cluster rule, because their bodies were read
first and the cluster scope would have been wrong:
`/api/plugins/kanban/model-options` and `.../profiles` (provider slugs and
per-profile engine bindings → `provider:read`, matched exactly so the
description-editing routes beneath them stay `plugin:use`), and
`PUT /api/profiles/{name}/model` (writes the binding `POST /api/model/set` is
held at `engine:select` for → `engine:select`, or the scope stays bypassable
through a different path).

### Fail-closed must not mean fail-empty

`NORMAL_USER` held no scopes and an unrostered identity resolved to nothing.
Harmless while unguarded meant reachable; after the flip it would have locked
every ordinary customer out of their own installation. `USER_CAPABILITIES` now
names what they already had and is granted to the normal-user baseline. It
contains no provider, credential, engine, tenant, deployment or ops scope.

### WebSocket enforcement, per socket

| socket | scope | before |
|---|---|---|
| `/api/pty` | `session:write` | **no check** |
| `/api/console` | `session:write` | **no check** |
| `/api/ws` | `session:write` | **no check** |
| `/api/audio/speak-stream` | `session:read` | **no check** |
| `/api/events` | `events:read` | **no check** — see below |
| `/api/plugins/kanban/events` | `plugin:use` | credential only |
| `/api/pub` | `events:read` | already enforced, unchanged |

`/api/events` carried an `events:read` entry in `ROUTE_SCOPES` and had no check
at its socket. Starlette's HTTP middleware does not run on an upgrade, so that
scope governed only the transport nobody reads the stream over. The
`_ws_scope_ok(ws, EVENTS_READ)` call that existed was in `pub_ws`.

### Open items, listed rather than guessed

1. **`/api/status` is no longer public, and that breaks a named consumer.**
   NAS `fly-provider.ts` `getInstanceRuntimeStatus` fetches it without a cookie
   as its sole liveness probe; it now gets 403 and healthy agents will surface
   as down in the portal UI. Point the probe at `/api/health` (still public) or
   give it a credential. One line to revert if that is the wrong call.
2. **Two tables disagree about `/api/status`.** It is still in
   `dashboard_auth.public_paths.PUBLIC_API_PATHS`, so the cookie gate admits it
   and the authorization gate then refuses it. Reconciling them means deciding
   whether the probe is coming back.
3. **`GET /api/profiles` still discloses** model, provider, on-disk path and
   `has_env` per profile. Left at `profile:read` deliberately — it is the route
   the profile picker lists from. The fix belongs in the payload: mask those
   fields for a caller without `provider:read`.
4. **Cluster scopes are unverified against endpoint bodies** for ~250 routes.
   That is the accepted trade, not an oversight. `route_authz_registry.py`
   remains the body-verified subset: 101 entries across `/api/plugins`,
   `/api/profiles` and the pre-existing privileged surface.
5. **`/docs`, `/redoc`, `/openapi.json`** are held at `ops:manage` rather than
   disabled in production. Disabling them outright was the stated preference
   and was not done.

### Test evidence

`tests/youtab_runtime/test_authorization_is_fail_closed.py` — 76 assertions,
green in **both** build states. Full-suite delta measured against the pre-flip
commit `b6cab1057`: **184 failed before, 183 after, zero new**. The 183 are
pre-existing cross-test pollution — they pass in isolation and fail identically
at `b6cab1057`. One was fixed by this change
(`test_path_traversal_still_blocked`), because the `/dashboard-plugins/{}/{}`
pattern is anchored to exactly two non-slash segments.

## Next permitted slice

**Complete route authorization registry → enforce explicit default-deny → add
Router-vs-policy CI gate.**

Classification must come from reading endpoint bodies and their data and
mutation authority. Not from path prefixes, route names, UI visibility or
comments. No broad authenticated baseline. Unknown, stale, duplicate or
malformed entries fail closed, and a newly added route makes CI red until it is
classified.

Start with `python scripts/youtab/route_inventory.py --json inventory.json
--by-prefix`. The privileged clusters already carry scopes and are the anchor;
`/api/plugins` at 47 route+methods is the largest unclassified cluster.
