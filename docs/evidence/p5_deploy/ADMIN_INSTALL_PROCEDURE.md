# Durable Execution — admin-run reproducible install procedure (PostgreSQL + secure TLS ingress)

Self-hosted install of the durable-execution API (`/v1/runs`) on PostgreSQL,
behind a TLS edge, fail-closed. Every step is a command an admin runs; no step
depends on this session. Product is NO-GO; this is the install path, not a
release authorization. No push/deploy is performed by this document.

## 0. Prerequisites
- Docker + Docker Compose v2.
- This repo checked out. Files used (all additive; shared `docker-compose.yml`
  is NOT modified):
  - `docker-compose.postgres.yml` — PostgreSQL backend + API ingress env + readiness healthcheck.
  - `docker-compose.edge.yml` + `deploy/edge/Caddyfile` — TLS reverse-proxy edge.
  - `deploy/edge/tls/` — admin-supplied cert+key (gitignored).

## 1. Build the server image WITH embedded source provenance
```
GIT_SHA=$(git rev-parse HEAD)
docker build --build-arg YOUTAB_AGENT_GIT_SHA="$GIT_SHA" \
  -t youtab-agent-runtime:$(git rev-parse --short HEAD) .
# verify the image is self-describing:
docker run --rm --entrypoint sh youtab-agent-runtime:$(git rev-parse --short HEAD) \
  -c 'cat /opt/youtab/.youtab_agent_build_sha'   # must print $GIT_SHA
```
Tag this image as `youtab-agent-runtime` (or set `image:` in your compose) so the
compose services use it.

## 2. Provide the provider config (preserved at /opt/data/config.yaml)
The service uses `YOUTAB_AGENT_HOME=/opt/data`, so its config is
`/opt/data/config.yaml`. Mount your provider config there (this is what makes a
real run reach a model). Example (redact real keys):
```
# config.yaml
model:
  default: "<your-model>"
  provider: "custom"           # or your provider
  base_url: "https://<your-openai-compatible-endpoint>/v1"
  api_key: "<REDACTED>"
```
Add to the gateway service (e.g. in an override or your compose):
`volumes: ["./config.yaml:/opt/data/config.yaml:ro"]`.
The provider MUST return streaming SSE chat completions (the agent rejects a
plain-JSON completion with "empty stream ... malformed SSE").

## 3. Provide the TLS cert for the edge
```
# self-signed for internal/test (or drop in your CA-issued cert):
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/edge/tls/key.pem -out deploy/edge/tls/cert.pem \
  -subj "/CN=<your-edge-hostname>" \
  -addext "subjectAltName=DNS:<your-edge-hostname>,IP:<your-ip>"
```
(See `deploy/edge/tls/README.md`. The `.pem` files are gitignored.)

## 4. Boot the merged stack (secrets from the host env, never embedded)
```
export POSTGRES_PASSWORD='<db-password>'      # URI-special chars OK (keyword DSN)
export API_SERVER_KEY='<long-random-secret>'  # the /v1/runs bearer
export EDGE_BIND=127.0.0.1                     # 0.0.0.0 ONLY behind a firewall
export EDGE_PORT=8443
docker compose \
  -f docker-compose.yml \
  -f docker-compose.postgres.yml \
  -f docker-compose.edge.yml \
  up -d
```
Validate the merged config first (optional): append `config` instead of `up -d`
(exit 0 expected).

Security posture produced by this merge:
- The api_server binds 0.0.0.0 INSIDE the container and is only `expose`d on the
  internal docker network — it is NOT published to the host. The unsandboxed
  local terminal backend is therefore not reachable from an untrusted network.
- The Caddy `edge` is the ONLY host-published entrypoint (TLS), reverse-proxying
  to the api_server; the Bearer `API_SERVER_KEY` is enforced end-to-end.
- Consider `terminal.backend: docker` (sandboxed) for the agent backend.

## 5. Verify (admin acceptance)
```
BASE=https://<edge-host>:${EDGE_PORT}     # use --cacert <your cert> or -k for self-signed
# readiness (store-aware): 200 when PostgreSQL is reachable
curl --cacert deploy/edge/tls/cert.pem "$BASE/health/ready"        # {"ready":true,"durable_store":"ok",...}
# admission requires the bearer:
curl -o /dev/null -w '%{http_code}\n' -X POST "$BASE/v1/runs" \
  -H 'Content-Type: application/json' -d '{"input":"x"}'           # 401
# a real run:
curl -X POST "$BASE/v1/runs" -H "Authorization: Bearer $API_SERVER_KEY" \
  -H 'Content-Type: application/json' -d '{"input":"hello"}'       # {"run_id":...,"status":"started"}
curl "$BASE/v1/runs/<run_id>/result" -H "Authorization: Bearer $API_SERVER_KEY"
```

## 6. Readiness / liveness contract the admin wires to
- `GET /health` — STATIC liveness only (`{"status":"ok",...}`). Do NOT use it as
  durable-store readiness.
- `GET /health/ready` (and `/v1/health/ready`) — durable-store readiness:
  round-trips the RunStore. `200 {"ready":true,"durable_store":"ok"}` when
  PostgreSQL is reachable; `503 {"ready":false,"durable_store":"unavailable"}`
  when it is not. Wire your load-balancer / orchestrator readiness probe to this.
  (The `docker-compose.postgres.yml` healthcheck already probes `/health/ready`.)
- Fail-closed under PostgreSQL loss:
  - at startup: the api_server never binds (connection refused);
  - mid-flight (PG lost while the API is up): `/health/ready` → 503 AND
    `POST /v1/runs` → `503 durable_store_unavailable` (persist-before-ack; no
    202 for an in-memory-only run). No run is admitted against a dead store.

## 7. Rollback
`docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.edge.yml down`
(add `-v` to also drop the PostgreSQL + edge volumes). The default
`docker-compose.yml` alone keeps the SQLite single-node backend.
