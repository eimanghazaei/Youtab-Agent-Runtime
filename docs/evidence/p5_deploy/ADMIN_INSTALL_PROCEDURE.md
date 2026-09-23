# Durable Execution — customer admin install (prebuilt, digest-pinned image)

Self-hosted install of the durable-execution API (`/v1/runs`) on PostgreSQL,
behind a TLS edge, fail-closed. The customer admin consumes a **prebuilt,
digest-pinned** image (they do NOT build from source). Product is NO-GO; this is
the install path, not a release authorization. No push/deploy is performed by
this document.

## Image (pin by digest)
| Field | Value |
|---|---|
| RC source SHA (embedded in image) | `77b2b2c4b740ba256596cb4e4c7514dfcd5b42fe` |
| Local image Id (config digest) | `sha256:8eefb02c101fcef5654cded0e3c3c285376c191ac93d50c10f4ce0e041bfe343` |
| Distribution tarball sha256 (`docker save`) | `c0d1ceee98c61ce3cee6540d422cc966233f5bf6ce8da72e20b150b775b189ac` |
| Registry repo digest | `<PUBLISHED_REPO_DIGEST>` — filled by Master at publish (`docker push` → the `repo@sha256:` digest). Pin THIS in compose. |

Provenance: the image is self-describing — `docker run --rm --entrypoint sh
<image> -c 'cat /opt/youtab/.youtab_agent_build_sha'` prints the RC source SHA.

### Obtaining the image (one of)
- Registry (preferred): `docker pull <repo>@sha256:<PUBLISHED_REPO_DIGEST>`.
- Offline tarball: verify then load.
  - Linux/macOS: `sha256sum youtab-agent-runtime.tar` → must equal the tarball
    sha256 above; then `docker load -i youtab-agent-runtime.tar`.
  - Windows PowerShell: `Get-FileHash youtab-agent-runtime.tar -Algorithm SHA256`
    → compare; then `docker load -i youtab-agent-runtime.tar`.

Pin the image in your compose by digest (never a floating tag):
```
services:
  youtab-agent-runtime:
    image: <repo>@sha256:<PUBLISHED_REPO_DIGEST>
```

## 0. Prerequisites
- Docker + Docker Compose v2.
- The three additive compose files + `deploy/edge/`, from the release bundle
  (the shared `docker-compose.yml` is NOT modified):
  `docker-compose.postgres.yml`, `docker-compose.edge.yml`,
  `deploy/edge/Caddyfile`, `deploy/edge/tls/` (admin-supplied cert+key).

## 1. Provider config (preserved at /opt/data/config.yaml)
The service uses `YOUTAB_AGENT_HOME=/opt/data`, so its config is
`/opt/data/config.yaml`. Provide `config.yaml` and mount it there (this is what
makes a real run reach a model). The provider MUST return streaming SSE chat
completions (a plain-JSON completion is rejected with "empty stream ...
malformed SSE").
```
# config.yaml (redact real keys)
model:
  default: "<your-model>"
  provider: "custom"
  base_url: "https://<your-openai-compatible-endpoint>/v1"
  api_key: "<REDACTED>"
```
Add on the gateway service: `volumes: ["./config.yaml:/opt/data/config.yaml:ro"]`.

## 2. TLS cert for the edge
Place `cert.pem` + `key.pem` in `deploy/edge/tls/` (see its README).
- Linux/macOS self-signed test cert:
  `openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout deploy/edge/tls/key.pem -out deploy/edge/tls/cert.pem -subj "/CN=<edge-host>" -addext "subjectAltName=DNS:<edge-host>,IP:<ip>"`
- Windows PowerShell: run the same `openssl` (Git for Windows/WSL), or drop in a
  CA-issued cert. `.pem` files are gitignored.

## 3. Boot the merged stack (secrets from the host env, never embedded)
Linux/macOS (bash):
```
export POSTGRES_PASSWORD='<db-password>'      # URI-special chars OK (keyword DSN)
export API_SERVER_KEY='<long-random-secret>'  # the /v1/runs bearer
export EDGE_BIND=127.0.0.1 EDGE_PORT=8443     # LOCAL admin (see network cases)
docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.edge.yml up -d
```
Windows PowerShell:
```
$env:POSTGRES_PASSWORD='<db-password>'
$env:API_SERVER_KEY='<long-random-secret>'
$env:EDGE_BIND='127.0.0.1'; $env:EDGE_PORT='8443'
docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.edge.yml up -d
```
Validate first (optional): replace `up -d` with `config` (exit 0 expected).

## 4. Network cases — NOT the same qualification
- **(1) Local admin (default, `EDGE_BIND=127.0.0.1`)** — edge reachable only from
  the host loopback. Tested/qualified.
- **(2) Organization workstation on another host (`EDGE_BIND=0.0.0.0`)** — NOT
  qualified until BOTH: (a) the route is tested from that other host, AND (b) the
  agent terminal backend is sandboxed (`terminal.backend: docker`) OR the
  exposure is explicitly constrained (firewall/allowlist/mTLS) and accepted in
  writing by customer IT. TLS + Bearer authenticate and encrypt `/v1/runs` but do
  NOT sandbox the agent execution it dispatches. Keep the loopback bind until
  this is satisfied.

## 5. Verify (admin acceptance)
```
BASE=https://<edge-host>:${EDGE_PORT}     # --cacert deploy/edge/tls/cert.pem (self-signed) or your CA
curl --cacert deploy/edge/tls/cert.pem "$BASE/health/ready"                 # {"ready":true,"durable_store":"ok"}
curl -o /dev/null -w '%{http_code}\n' -X POST "$BASE/v1/runs" -H 'Content-Type: application/json' -d '{"input":"x"}'   # 401
curl -X POST "$BASE/v1/runs" -H "Authorization: Bearer $API_SERVER_KEY" -H 'Content-Type: application/json' -d '{"input":"hello"}'
curl "$BASE/v1/runs/<run_id>/result" -H "Authorization: Bearer $API_SERVER_KEY"
```

## 6. Readiness / liveness contract
- `GET /health` — STATIC liveness only. Do NOT use as durable-store readiness.
- `GET /health/ready` (+ `/v1/health/ready`) — durable-store readiness (round-trips
  the RunStore): 200 ready when PG is reachable, 503 when not. Wire the LB /
  orchestrator readiness probe here (the postgres override healthcheck does).
- Fail-closed under PostgreSQL loss:
  - startup: api_server never binds (connection refused);
  - mid-flight (PG lost while up): `/health/ready` → 503 AND `POST /v1/runs` →
    503 `durable_store_unavailable` (persist-before-ack; no in-memory-only 202);
  - terminal write lost (PG lost after admission, before result persist): the run
    is reported `reconciliation_required` (terminal=false, durable=false, no
    output), never a durable `completed`; terminal SSE/result are emitted only
    after the durable commit. After a restart the run is durable `unknown`
    (discoverable, `recovered_from_store`), NOT silently `running`.
- Unresolved-state recovery: `reconciliation_required` (in-flight commit failed)
  and `unknown` (recovered after process death) are non-terminal and carry no
  uncommitted result. An in-process run is NOT auto-resumed across process death —
  verify side effects and re-submit if needed.

## 7. Rollback (ordinary — DATA PRESERVED)
Stops and removes the containers/networks; the PostgreSQL volume (run history) is
KEPT.
```
docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.edge.yml down
```
The default `docker-compose.yml` alone keeps the SQLite single-node backend.

## 8. RESET (DESTRUCTIVE — separate, explicit, deletes durable data)
Only when you intend to erase all run history. This deletes the PostgreSQL volume
and the edge TLS/data volume. IRREVERSIBLE.
```
# Confirm the volumes first:
docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.edge.yml config --volumes
# Then, deliberately:
docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.edge.yml down -v
```
