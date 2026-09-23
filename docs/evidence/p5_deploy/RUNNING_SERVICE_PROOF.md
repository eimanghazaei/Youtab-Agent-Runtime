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

## 2. SUCCEEDED with nonempty result, persisted in PostgreSQL — PASS
A completing provider is now wired through the RUNNING service via a config file at
`/opt/data/config.yaml` (the service's `YOUTAB_AGENT_HOME=/opt/data`), pointing at an
OpenAI-compatible mock that returns a valid streaming SSE completion
(`text/event-stream`, `chat.completion.chunk` frames + `data: [DONE]`, content
`CUSTOMER_TASK_OK_42`). Raw evidence: `RUNNING_SERVICE_SUCCEEDED_RAW.txt`.
- `POST /v1/runs {"input":"do the customer task"}` → run_id `run_795e0e80d51b4f0d8d5b17375958ceb3`.
- `GET /v1/runs/{id}/result` → `{"terminal":true,"status":"completed","output":"CUSTOMER_TASK_OK_42","error":null}`.
- Durable events `GET /v1/runs/{id}/events?from_seq=0`:
  `seq1 accepted(QUEUED) → seq2 status.running(RUNNING) → seq3 progress → seq4 status.completed(SUCCEEDED)`.
- Physical PG row (`psql`): `run_795e0e80d51b4f0d8d5b17375958ceb3|SUCCEEDED|CUSTOMER_TASK_OK_42`
  (state SUCCEEDED, `result_ref` carries the nonempty output).
This is one synthetic task against a mock provider — it proves the durable
execution + result-payload path end-to-end through the live service, not a real
customer workload. Product remains NO-GO.

(Earlier FAILED-terminal observation retained for the record: before the config
file was mounted, the `main` route ignored `OPENAI_BASE_URL` env and the provider
call failed 401 — durable STATUS/terminal was still persisted correctly; that was
provider-config, not a durability fault.)

## 3. Recovery after a service restart — PASS
`docker restart de-p5-gw` (fresh process; in-memory dicts empty) → `GET /v1/runs/{id}`:
`{"status":"completed","output":"CUSTOMER_TASK_OK_42","recovered_from_store":true,...}`;
`GET /v1/runs/{id}/result` → SUCCEEDED with output intact; events replay = 4 durable
events (seq1..seq4 ending status.completed/SUCCEEDED). The SUCCEEDED run with its
nonempty result was recovered from PostgreSQL by the restarted service.

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

## Provider config (how the running service reached the mock)
The service reads `YOUTAB_AGENT_HOME=/opt/data`, so its config is `/opt/data/config.yaml`
(NOT `.youtab-agent-runtime/config.yaml`, and `OPENAI_BASE_URL` env alone is NOT
honored by the `main` route). Config mounted (secrets redacted):
`model.default: mock-model`, `custom_providers.mock.{base_url: http://de-p5-mock:8080/v1,
api_key: <REDACTED>}`. The mock MUST return streaming SSE (a plain JSON completion is
rejected with "Provider returned an empty stream ... malformed SSE").

## 5. Secure external route through the merged installation config — PASS
RC image `youtab-agent-runtime:p5-rc-cad5ec26d` (embedded source SHA
`cad5ec26d210fa3e8ea7b4b9d528a21e59773571`, Id `sha256:6ee971b2b8bc…`). Merged
`docker-compose.yml + docker-compose.postgres.yml + docker-compose.edge.yml`:
- The api_server binds 0.0.0.0 INSIDE the container and is only `expose`d — it is
  NOT host-published (`docker ps`: gw `ports=[]`). A Caddy TLS `edge` is the only
  host-published entrypoint (`127.0.0.1:8443`). The unsandboxed local terminal
  backend is never exposed to an untrusted network.
- An EXTERNAL client (separate container) over the TLS edge: TLS1.3 handshake
  verified against the mounted cert (subject/issuer `CN=youtab-durable-edge`, SAN
  match, HTTP/2 200); `POST /v1/runs` WITHOUT the bearer → 401 (api_server
  enforces end-to-end); WITH the bearer → SUCCEEDED run, output
  `CUSTOMER_TASK_OK_42`, PG row SUCCEEDED. Raw: `SECURE_INGRESS_AND_PGLOSS_RAW.txt`.

## 6. PostgreSQL loss WHILE the API is running (readiness + fail-closed admission) — PASS
Readiness contract: `/health` is STATIC liveness only; `/health/ready`
(added, store-aware) round-trips the RunStore. Observed with the API already up:
- baseline: `/health/ready` 200 `{"ready":true,"durable_store":"ok"}`; a run SUCCEEDS.
- `docker stop` PostgreSQL (mid-flight): `/health/ready` → **503**
  `{"ready":false,"durable_store":"unavailable"}`; `/health` STAYS **200** (proving
  it must NOT be used as store readiness); `POST /v1/runs` → **503
  `durable_store_unavailable`** (persist-before-ack; NOT a 202 in-memory-only run).
- `docker start` PostgreSQL: `/health/ready` recovers to 200; a new run SUCCEEDS.
- PG holds only SUCCEEDED rows — no orphaned/false-completed row from the PG-down
  window.

NOTE (defect found & fixed here): before the fix, `POST /v1/runs` returned 202 and
ran IN-MEMORY ONLY when PG was down, because the write-through mirror is
best-effort. Fixed by a persist-before-ack admission barrier in server durable
mode (commit cad5ec26d; test_p5_admission_failclosed.py). The best-effort mirror
for LATER state changes is unchanged (desktop/sqlite never blocked).

## Verdict
Running-service admission + durable status persistence + **SUCCEEDED run with
nonempty result** + restart recovery + PG-down fail-closed (startup AND
mid-flight) + **secure external TLS route that does not expose the unsandboxed
backend** + **store-aware readiness**: PASS on the real RC image, synthetic mock
task. OPEN: the Master-adopted deployment contract and a real customer workload.
Product NO-GO.
