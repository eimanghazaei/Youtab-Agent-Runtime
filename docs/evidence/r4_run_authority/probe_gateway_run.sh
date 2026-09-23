#!/bin/sh
# usage: probe_gateway_run.sh <container-name> <host-port>   (secrets REDACTED)
docker run -d --name "$1" --network r4-net -p 127.0.0.1:$2:8090 \
  -e API_SERVER_KEY=<REDACTED> -e API_SERVER_HOST=0.0.0.0 -e API_SERVER_PORT=8090 \
  -e YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND=postgres \
  -e 'YOUTAB_AGENT_DURABLE_PG_DSN=host=r4-pg port=5432 dbname=durable user=youtab password=<REDACTED>' \
  -v "<PROBE_DIR>/probe_config.yaml:/opt/data/config.yaml:ro" \
  youtab-agent-runtime:r4-415a1734a gateway run
