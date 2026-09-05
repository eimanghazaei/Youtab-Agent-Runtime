# Engine file-backed secrets (`<NAME>_FILE`)

The Agent Runtime engine reads its secrets from a file when a `<NAME>_FILE`
environment variable is set, instead of taking the value inline from `<NAME>`.
This covers the two **service-boundary** secrets and, since WAVE-30B, **every
provider API key** (`<PROVIDER_API_KEY_ENV>_FILE`). The secret **value** never
enters the process environment, so it cannot appear in `os.environ`,
`/proc/<pid>/environ`, process arguments, `docker inspect`, or logs. Only the
**path** is configuration.

Resolution lives in one place — `youtab_agent_cli/secret_file.py`
(`env_or_file` / `read_secret_file`, with the strict tier for provider keys). The
two key-read chokepoints (`config.get_env_value_prefer_dotenv` for registry
providers, `runtime_provider._getenv` for custom providers) both route through it.

## Covered secrets

| Inline var | File var | Read site |
|---|---|---|
| `YOUTAB_AGENT_RUNTIME_SERVICE_SECRET` | `YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE` | `web_routers/runtime.py::_runtime_secret` (gateway↔engine auth/HMAC on the `/api/runtime/v1` plane; read per request — no import-time read) |
| `YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN` | `YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN_FILE` | `web_server.py::_resolve_session_token` (single-operator dashboard session token) |

## Provider API keys (WAVE-30B, generic `<ENV>_FILE`)

Every credential-bearing provider now supports file-based delivery through the
**same** loader, keyed on the provider's canonical API-key environment-variable
name — `<ENV>_FILE`. Adding a provider requires no change to the loader or to
benchmark core.

| Provider | Inline var | File var |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | `OPENAI_API_KEY_FILE` |
| Anthropic | `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY_FILE` |
| Google (Gemini) | `GOOGLE_API_KEY` / `GEMINI_API_KEY` | `GOOGLE_API_KEY_FILE` / `GEMINI_API_KEY_FILE` |
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_API_KEY_FILE` |
| Z.AI / GLM | `GLM_API_KEY` / `ZAI_API_KEY` | `GLM_API_KEY_FILE` / `ZAI_API_KEY_FILE` |
| xAI | `XAI_API_KEY` | `XAI_API_KEY_FILE` |
| OpenRouter | `OPENROUTER_API_KEY` | `OPENROUTER_API_KEY_FILE` |
| Alibaba / Qwen | `DASHSCOPE_API_KEY` | `DASHSCOPE_API_KEY_FILE` |
| …any registry provider | `<ITS_API_KEY_ENV>` | `<ITS_API_KEY_ENV>_FILE` |

Custom / local OpenAI-compatible profiles (`config.yaml` `providers:` /
`custom_providers:`) may declare, in addition to `base_url` / `model`:

- `api_key_env` (alias `key_env`) — an env var holding the inline key; it also
  gains `<api_key_env>_FILE` support automatically; **or**
- `api_key_file_env` — an env var whose value is the **path** to a strict-tier
  key file (highest precedence).

The provider key is loaded in-process and handed **directly to the SDK
constructor** (`api_key=`); it is never written back to `os.environ`, `.env`,
GitHub Secrets, command arguments, logs, journals, or benchmark artifacts.

Bedrock / Vertex / Azure use their cloud SDK's own token-file mechanism
(`AWS_WEB_IDENTITY_TOKEN_FILE`, `AZURE_FEDERATED_TOKEN_FILE`, service-account
JSON) and are unaffected.

## Contract (fail-closed)

- `<NAME>_FILE` set ⇒ read the file; the inline `<NAME>` must **not** also be set
  (dual source refused — no precedence contract). Applies to service-boundary and
  provider secrets alike.
- **Boundary tier** (service secret, dashboard token — default): file must be
  present, **regular** (not dir/device), **not a symlink**, **non-empty**,
  ≤ 64 KiB, and — on POSIX — **not** group/other-writable. This tolerates the
  world-readable `0444` root-owned mounts that Docker/Kubernetes secrets create
  by default, so existing service-secret deployments are unchanged.
- **Strict tier** (provider API keys, live-benchmark credentials): all boundary
  checks **plus**, verified *before any bytes are returned*: POSIX `O_NOFOLLOW`
  read validated from the same descriptor with **owner == the runtime account**
  and **mode no broader than `0400`** (no group/world bits, no owner
  write/execute); Windows **protected owner-only DACL** (fail-closed if pywin32
  cannot verify); **no UTF-8 BOM**; an **absolute, non-`..`** path; and, for local
  files, refusal of a **git working tree or cloud-sync** location. This is why a
  Docker-secret-delivered provider key must be mounted `mode: 0400` owned by the
  runtime service account (see below).
- **Live-benchmark mode** (`YOUTAB_AGENT_LIVE_BENCHMARK=1`): file-based delivery
  is **mandatory** for provider keys — a plaintext-environment provider
  credential is refused. Outside benchmark mode a plaintext env credential still
  works but emits a one-time non-secret warning (naming only the variable).
- Bounded read; exactly **one** trailing newline (`\n`/`\r\n`) trimmed, nothing
  else. Contents are never included in an exception message or logged.
- Neither file nor inline set ⇒ prior default behaviour (runtime secret → empty →
  surface reports `runtime_disabled`; dashboard token → fresh ephemeral token;
  provider key → provider reports missing credential).

## Deployment (Docker Compose)

```yaml
services:
  engine:
    environment:
      YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE: /run/secrets/agent_runtime_service_secret
      # Provider key (strict tier) — MUST be mounted 0400 owned by the runtime uid.
      OPENAI_API_KEY_FILE: /run/secrets/openai_api_key
    secrets:
      - agent_runtime_service_secret          # boundary tier: default 0444 is fine
      - source: openai_api_key                # strict tier: force owner-only 0400
        target: openai_api_key
        uid: "10001"                          # the runtime service account uid
        mode: 0400
secrets:
  agent_runtime_service_secret: { file: ./secrets/agent_runtime_service_secret }
  openai_api_key: { file: ./secrets/openai_api_key }
```

The default Docker/K8s secret mount (`0444`, root-owned) satisfies the boundary
tier but **fails** the strict tier — provider keys must therefore use the long
secret syntax with `uid:` + `mode: 0400` so the file is owner-only for the
runtime account. Existing deployments that set the inline variables keep working
unchanged; migrating to files is opt-in per secret.

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
boundary-tier fail-closed condition (missing/empty/oversized/non-regular/symlink/
world-writable/dual-source).

`tests/test_secret_file_strict.py` proves the strict tier: POSIX owner + mode
(`0400` accepted; `0600`/`0440`/`0444`/… refused; wrong owner refused), Windows
protected owner-only DACL (accepted; broad DACL refused; pywin32-missing
fail-closed), BOM rejection, absolute/`..` traversal rejection, and repo/cloud
location refusal.

`tests/test_provider_key_file.py` proves provider-neutral `<ENV>_FILE` resolution
through both key-read chokepoints (`config.get_env_value_prefer_dotenv`,
`runtime_provider._getenv`), dual-source refusal, the plaintext-env warning, and
the live-benchmark file-mandatory enforcement — with the value never entering
`os.environ`.
