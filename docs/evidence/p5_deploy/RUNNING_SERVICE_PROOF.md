# P5 — running-service proof (real image, s6 `gateway run`, HTTP /v1/runs on PostgreSQL)

Source SHA: `9f44a980c23436de49696b4609a8edf6cefa444b` (P5 code `cc5c4f7d`).
Image: `youtab-agent-runtime:p5-cc5c4f7d`
Image Id: `sha256:abee28872c4aff7c8dd92b36a82f160517d4db98bd77fa24c93daf613edbde08` (4.49 GB, debian:13.4).
PostgreSQL: `postgres:16-alpine` (16.15). psycopg 3.3.6 in image.
Build log: `docs/evidence/p5_deploy/p5_image_build.log` (build exit 0).

## Topology (all on a docker network; no host port collision)
- `de-p5-pg2` — PostgreSQL (durable RunStore backend).
- `de-p5-mock` — OpenAI-compatible mock provider (fixed chat completion).
- `de-p5-gw` — the built image, booted via its normal s6 `/init` entrypoint + `gateway run`.

## Redacted run command (secret = the DSN password `devpass`, shown REDACTED)
```
docker run -d --name de-p5-gw --network de-p5-net -p 18642:8090 \
  -e API_SERVER_KEY=<REDACTED> -e API_SERVER_HOST=0.0.0.0 -e API_SERVER_PORT=8090 \
  -e YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND=postgres \
  -e 'YOUTAB_AGENT_DURABLE_PG_DSN=host=de-p5-pg2 port=5432 dbname=durable user=youtab password=<REDACTED>' \
  -e OPENAI_API_KEY=<REDACTED> -e OPENAI_BASE_URL=http://de-p5-mock:8080/v1 \
  youtab-agent-runtime:p5-cc5c4f7d gateway run
```
The api_server (`/v1/runs`) binds aiohttp on container :8090 (verified `Server: Python/3.13 aiohttp/3.14.1`). It is OFF unless `API_SERVER_KEY`/`API_SERVER_HOST` are set — the existing product contract.

## 1. Admission over HTTP against the running service — PASS
`POST /v1/runs {"input":"hello","model":"main"}` (Bearer API_SERVER_KEY):
→ `HTTP 202`, `Server: aiohttp/3.14.1`, body `{"run_id":"run_d5854cc6347548a0aa0ac507efcff305","status":"started"}`.

## 2. Persisted status/result in PostgreSQL — PASS
State progression (durable), via `GET /v1/runs/{id}/events?from_seq=0`:
`seq1 accepted(QUEUED) → seq2 status.running(RUNNING) → seq3 status.failed(FAILED)`.
`GET /v1/runs/{id}/result` → `{"terminal":true,"status":"failed","error":"HTTP 401: Missing Authentication header"}`.
Physical PG row (`psql`): `run_d5854cc6347548a0aa0ac507efcff305|FAILED|`.
NOTE: the FAILED terminal is because the agent's provider call did not reach the
mock (the `main` route did not use OPENAI_BASE_URL — provider-config, not a
durability fault). The durable STATUS/terminal was persisted correctly through the
running service. The "completed-with-output" result-payload path is proven at the
component level (test_p5_http_on_postgres.py, completing mock agent → SUCCEEDED +
result_ref in PG); wiring a completing provider through the running service needs a
config-file provider (OPEN, provider-side).

## 3. Recovery after a service restart — PASS
`docker restart de-p5-gw` (fresh process; in-memory dicts empty) → `GET /v1/runs/{id}`:
`{"status":"failed","recovered_from_store":true,...}`; events replay = 3 durable events.
The run was recovered from PostgreSQL by the running service.

## 4. PostgreSQL unavailable → fail-closed (OBSERVED, not inferred) — PASS
`docker stop de-p5-pg2` then `docker restart de-p5-gw`:
- gw logs (raw): `api_server.py:1247 __init__` → `create_run_store` → `_postgres_run_store`
  → `PostgresRunStore.__init__` →
  `youtab_runtime.durable_run_store.DurableRunError: PostgreSQL backend unavailable at startup (OperationalError); server durability fail-closed (no SQLite fallback)`.
- api_server does NOT bind: `GET /v1/runs` → `http_code=000` (connection refused);
  `POST /v1/runs` → `000`. The running service CANNOT accept a run with PostgreSQL
  down, and does NOT fall back to SQLite.
- (s6 keeps the container process up for other supervised services, but the
  durable-execution ingress — the api_server /v1/runs endpoint — is down.)

## Verdict
Running-service admission + durable status persistence + restart recovery +
PG-down fail-closed: PASS on the real image. OPEN: a completing provider through
the running service (config-file provider) and the Master-adopted deployment
contract. Product NO-GO.
