# Engine file-backed service-boundary secrets (`<NAME>_FILE`)

The Agent Runtime engine reads its **service-boundary** secrets from a file when a
`<NAME>_FILE` environment variable is set, instead of taking the value inline from
`<NAME>`. The secret **value** never enters the process environment, so it cannot
appear in `os.environ`, `/proc/<pid>/environ`, process arguments, `docker inspect`,
or logs. Only the **path** is configuration.

Resolution lives in one place — `youtab_agent_cli/secret_file.py`
(`env_or_file` / `read_secret_file`).

## Covered secrets

| Inline var | File var | Read site |
|---|---|---|
| `YOUTAB_AGENT_RUNTIME_SERVICE_SECRET` | `YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE` | `web_routers/runtime.py::_runtime_secret` (gateway↔engine auth/HMAC on the `/api/runtime/v1` plane; read per request — no import-time read) |
| `YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN` | `YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN_FILE` | `web_server.py::_resolve_session_token` (single-operator dashboard session token) |

Provider/model credentials (ANTHROPIC_API_KEY, OPENAI_API_KEY, the DeepSeek/Amour
key, …) are **out of scope here** — they resolve through the profile-scoped
credential pool / `config.yaml` `custom_providers` path, which is already
file-backed (see the Amour resolver proof).

## Contract (fail-closed)

- `<NAME>_FILE` set ⇒ read the file; the inline `<NAME>` must **not** also be set
  (dual source refused — no precedence contract).
- File must be present, **regular** (not dir/device), **not a symlink**,
  **non-empty**, ≤ 64 KiB, and — on POSIX — **not** group/other-writable. Any
  violation raises `SecretFileError` and the caller fails closed.
- Bounded read; exactly **one** trailing newline (`\n`/`\r\n`) trimmed, nothing
  else. Contents are never included in an exception message or logged.
- Neither file nor inline set ⇒ prior default behaviour (runtime secret → empty →
  surface reports `runtime_disabled`; dashboard token → fresh ephemeral token).

## Deployment (Docker Compose)

```yaml
services:
  engine:
    environment:
      YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE: /run/secrets/agent_runtime_service_secret
    secrets: [agent_runtime_service_secret]
secrets:
  agent_runtime_service_secret: { file: ./secrets/agent_runtime_service_secret }
```

Existing deployments that set the inline variables keep working unchanged;
migrating to files is opt-in per secret.

## Rotation / revocation

1. Write the new value to the file (0400, owned by the service user).
2. Replace the Docker/K8s secret object, or replace a bind-mounted file atomically
   (`install -m 0400 new /run/secrets/<name>`).
3. Restart the engine process — the runtime secret is read per request and the
   dashboard token at server start, so a restart is the rotation boundary. To
   **revoke**, remove the file/secret and restart; the runtime surface then reports
   `runtime_disabled` (503) rather than serving with a stale key.
4. Never place the value in shell history, an env var, or a log line.

## Tests

`tests/test_engine_secret_file.py` proves, per boundary secret: file consumption
via the real `_runtime_secret()` / `_resolve_session_token()`; absence of the value
from `os.environ`, `/proc/self/environ`, a spawned child env, argv; and every
fail-closed condition (missing/empty/oversized/non-regular/symlink/world-writable/
dual-source). 11 pass + 2 POSIX-only (run on the Linux CI runner).
