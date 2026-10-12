# TLS material for the durable-execution edge

The `edge` service (docker-compose.edge.yml) mounts this directory read-only at
`/etc/caddy/tls`. Place an admin-supplied **`cert.pem`** and **`key.pem`** here.
They are intentionally NOT committed (see `.gitignore`).

- Public domain: use your CA-issued cert/key (or switch the Caddyfile site
  address to your hostname and drop the `tls` line to use automatic ACME).
- Internal / self-hosted / test: generate a self-signed pair:

```
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/edge/tls/key.pem -out deploy/edge/tls/cert.pem \
  -subj "/CN=youtab-durable-edge" \
  -addext "subjectAltName=DNS:localhost,DNS:youtab-durable-edge,IP:127.0.0.1"
```

Clients that don't trust the self-signed CA must opt in (curl `-k`,
`--no-check-certificate`, etc.). Bearer `API_SERVER_KEY` auth is still enforced
end-to-end by the api_server regardless of TLS trust.
