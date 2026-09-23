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

## Server startup
The shipped server runs `gateway run` (docker-compose `command: ["gateway","run"]`);
the api_server constructs the run store via `create_run_store()` (env-driven) at
startup. To run the server on PostgreSQL: set the two env vars above before
`gateway run`.

## Scope / OPEN
- SQLite qualified single-node (local correctness); PostgreSQL qualified on a real
  PG16 instance (server durability). Multi-host distributed execution beyond a
  single PG is not claimed here.
- Lane-1 signed effect reconciliation remains BLOCKED (fail-closed) — no second
  effect/receipt authority is introduced.
- A pyproject `[server]` aggregate extra (bundling postgres + otlp + ...) may be
  added later as a convenience; not required for this closure.
