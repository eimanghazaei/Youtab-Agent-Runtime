# Durable Execution — Server (PostgreSQL) install + backend-selection contract

## Backend selection (single mechanism, env prefix `YOUTAB_AGENT_`)
| Env | Meaning |
|---|---|
| `YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND` | `sqlite` (default; desktop/local single-node durable) or `postgres` (server/enterprise) |
| `YOUTAB_AGENT_DURABLE_PG_DSN` | PostgreSQL DSN when backend=postgres (e.g. `postgresql://user:pass@host:5432/db`) |

`create_run_store()` (and the api_server startup) read these; there is **one**
selection mechanism, no second config path. Desktop/local default is **SQLite**
(intentionally supported single-node durable backend). An enterprise that
**requires** PostgreSQL sets `...BACKEND=postgres`; then a missing/malformed/
unreachable DSN **refuses startup** (fail-closed) — never a silent SQLite fallback.
The api_server swallows a store-init failure ONLY for the default sqlite path (so
local startup is never blocked); when a server backend is explicitly requested it
**propagates** the failure.

## Fail-closed matrix (proved: tests/durable_execution/test_p5_backend_selection.py)
| Case | Env | Result |
|---|---|---|
| a | backend unset | SQLite backend (documented default) |
| b | backend=postgres, DSN unset | refuse at construction (fail-closed) |
| c | backend=postgres, malformed DSN | refuse; credentials NOT in error |
| d | backend=postgres, unreachable PG | refuse before accepting a run; no cred leak |
| e | backend=postgres, reachable PG | PostgresRunStore selected |

Failures happen at store construction — before any run is accepted or the server
can report healthy. Error messages never include the DSN/credentials.

## Install (reproducible, lockfile-based)
- Dependency: `postgres` extra = `psycopg[binary]==3.3.6` (declared in
  `pyproject.toml`; present in `uv.lock`). `psycopg[binary]` ships self-contained
  manylinux (debian:13.4 server image, glibc) and Windows wheels, so
  `uv lock` / `uv sync --locked` stays cross-platform and no system libpq/apt
  package is required.
- Server image (`Dockerfile`): the server sync line now includes `--extra postgres`
  alongside the existing `--extra all --extra messaging ...`.
- Clean install command (server): `uv sync --frozen --extra postgres`
  (in addition to the image's other extras). Verified from a CLEAN environment
  (fresh `UV_PROJECT_ENVIRONMENT`, not the manually-seeded dev venv):
  `uv sync --frozen --no-install-project --extra postgres` → exit 0 → `import psycopg` == 3.3.6.
- Desktop/local: unchanged — the SQLite backend needs no extra; PostgreSQL is
  never imposed on the desktop installer.

## Passwords with URI-special characters (@ : / ? # [ ])
`YOUTAB_AGENT_DURABLE_PG_DSN` is passed to psycopg, which accepts BOTH forms:
- **libpq keyword conninfo** (the compose default): `host=postgres port=5432
  dbname=durable user=youtab password=<pw>` — the password is verbatim, no
  percent-encoding. Caveat: a password with a space or single-quote must be
  libpq-quoted (`password='pa ss'`).
- **URL DSN**: `postgresql://youtab:<pw>@postgres:5432/durable` — `<pw>` MUST be
  percent-encoded (a raw `@`/`:`/`/`/`#` mis-parses; verified in
  `tests/durable_execution/test_p5_dsn_special_chars.py`).
Live-verified: a password `p@ss:w/rd#1` connects via the keyword form and via a
percent-encoded URL, and FAILS raw-unencoded in a URL DSN.

## Server startup
The shipped server runs `gateway run` (docker-compose `command: ["gateway","run"]`);
the api_server constructs the run store via `create_run_store()` (env-driven) at
startup. To run the server on PostgreSQL: set the two env vars above before
`gateway run`. The api_server (`/v1/runs`) is off unless `API_SERVER_KEY` and
`API_SERVER_HOST` are set (existing product contract).

## API health behavior (OBSERVED on the running image)
The api_server exposes health at **`GET /health`**, `GET /health/detailed` and the
alias `GET /v1/health` (registered in `gateway/platforms/api_server.py`). There is
**no** `/api/health`, `/healthz`, `/readyz` or `/livez` route (all 404).
- `GET /health` → `200 {"status":"ok","platform":"youtab-agent-runtime","version":"0.19.1"}`,
  no auth required. Use this as the container liveness/readiness probe.
- `GET /health/detailed` requires `Authorization: Bearer <API_SERVER_KEY>`
  (401 `gateway_auth_failed` without it).
- Fail-closed signal: with `BACKEND=postgres` and PostgreSQL unreachable, the run
  store raises at api_server startup, so **the aiohttp listener never binds** —
  `/health` itself is unreachable (connection refused / HTTP 000), not a 200 with a
  degraded body. The absence of a bound `/health` is the health-probe failure. The
  container's other s6-supervised processes may stay up, but the durable-execution
  ingress is down (proved in RUNNING_SERVICE_PROOF.md §4).

## Verdict
**PostgreSQL component VERIFIED; production deployment path OPEN.** The dependency
is locked, the backend is implemented and tested on real PG16, and backend
selection is fail-closed. What is NOT yet proven end-to-end: a built server IMAGE
booted against a fresh PostgreSQL with `/v1/runs` exercised through the running
service. That requires the shared deployment contract (below) to be adopted with
Master and a provider configured — deployment/pre-prod is Owner/Master-side.

## Production deployment contract (PROPOSED — pending Master coordination)
- `docker-compose.postgres.yml` (additive override; the shared `docker-compose.yml`
  is NOT modified). Adds a `postgres` service and sets, on the gateway service,
  `YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND=postgres` +
  `YOUTAB_AGENT_DURABLE_PG_DSN=postgresql://youtab:${POSTGRES_PASSWORD}@postgres:5432/durable`
  with `depends_on: postgres: service_healthy`. No secret embedded.
- Boot: `POSTGRES_PASSWORD=… docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d`.
- Fail-closed at the service level: with backend=postgres and PostgreSQL
  missing/unreachable, the run-store construction raises at api_server startup —
  the gateway service cannot become healthy (health probe fails) or accept a
  `/v1/runs` run. The default `docker-compose.yml` (no override) keeps SQLite for
  explicitly local/single-node use.
- The api_server (`/v1/runs`) itself is off unless `API_SERVER_KEY`/`API_SERVER_HOST`
  are set (existing contract) and a provider is configured — those are the
  pre-deploy prerequisites the deployment contract must pin with Master.

## Scope / OPEN
- SQLite qualified single-node (local correctness); PostgreSQL qualified on a real
  PG16 instance (server durability). Multi-host distributed execution beyond a
  single PG is not claimed here.
- Lane-1 signed effect reconciliation remains BLOCKED (fail-closed) — no second
  effect/receipt authority is introduced.
- A pyproject `[server]` aggregate extra (bundling postgres + otlp + ...) may be
  added later as a convenience; not required for this closure.
