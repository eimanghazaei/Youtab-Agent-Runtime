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
| PR head | `464f8672b69190ad2a6758481ef9438325832bf7` |
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
| 10 | **Route authorization registry → default-deny → Router-vs-policy CI gate** | **ACTIVE — registry in progress, 101/302** | `youtab_agent_cli/route_authz_registry.py`; 34 tests, 11 mutations RED→GREEN. **Default-deny is NOT active.** |
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

The registry is **data only**. Nothing enforces it, so nothing can break; that
ordering is deliberate and must be kept. Flipping `authorize()` to default-deny
against a partial registry refuses every unclassified route, and the fastest
way back to a working dashboard would be to bulk-assign a permissive class —
the exact failure this control exists to prevent.

| | |
|---|---|
| HTTP route+method pairs classified | **99 of 294** (SPA built) / 99 of 293 (unbuilt) |
| WebSocket routes classified | **2 of 7** |
| Conditional mounts classified | **0 of 1** (`/assets`) |
| Registry entries total | 101 |
| Stale entries | 0 |
| `authorize()` default-deny | **NOT ACTIVE** — still returns `True` for unmapped |
| Router-vs-policy CI gate | **NOT BUILT** |

**Clusters complete** (every endpoint body opened): `/api/plugins` (47 + 1
socket), `/api/profiles` (15), the `ROUTE_SCOPES` privileged surface (30 + 1
socket), the `PUBLIC_API_PATHS` allowlist (7).

**Clusters untouched**: `/api/git` 19, `/api/dashboard` 15, `/api/ops` 15,
`/api/sessions` 14, `/api/cron` 13 (less `/fire`), `/api/providers` remainder,
`/api/skills` 12, `/api/tools` 12, `/api/mcp` 11, `/api/messaging` 11,
`/api/memory` 8, `/api/files` 7, `/api/fs` 6, `/api/config` remainder,
`/api/webhooks` 5, `/api/auth` 4, `/api/learning` 4, `/api/pairing` 4,
`/api/audio` 3, `/api/curator` 3, `/auth/native` 3, `/api/analytics` 2,
`/api/youtab` 2, and the single-route clusters. Sockets still unclassified:
`/api/audio/speak-stream`, `/api/console`, `/api/pty`, `/api/pub`, `/api/ws`.

### Two authorization defects found by reading bodies

Both were reachable by anyone merely signed in, and both sit under a path
prefix whose neighbours are ordinary user data — a prefix-derived or
name-derived classification would have missed them.

1. `GET /api/plugins/kanban/model-options` and `GET /api/plugins/kanban/profiles`
   return provider slugs, model lists and per-profile `provider`/`model`
   bindings — the private engine catalogue `/api/model/options` is already held
   at `provider:read` for.
2. `GET /api/profiles` returns `model`, `provider`, the profile's absolute path
   and `has_env` per profile. `PUT /api/profiles/{name}/model` **writes** the
   same binding `POST /api/model/set` is held at `engine:select` for — so while
   it is unmapped, that scope is bypassable through a different path.

They are classified in the registry. They are **not yet enforced** — enforcement
arrives with the default-deny flip.

### Build-state delta, measured

| | built | unbuilt |
|---|---|---|
| HTTP route+method pairs | 294 | 293 |
| distinct paths | 255 | 254 |
| mounts | 1 (`/assets`) | 0 |

The only difference is `GET /assets/{}.css` plus the `/assets` StaticFiles
mount. `GET /{}` exists in both states but is a *different endpoint* in each —
`serve_spa` when built, `no_frontend` when not — so one registry key covers two
bodies and the classification has to hold for both. The registry must be
authored against the built superset and the gate must accept the unbuilt state
as a subset, or CI goes red on any runner that has not built the frontend.

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
