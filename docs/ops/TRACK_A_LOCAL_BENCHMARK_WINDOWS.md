# Track A (local ECO / Qwen) benchmark — Windows runbook

Exact, repository-local procedure to run the **Track A `local_runtime`** benchmark
on Windows: the harness drives the authenticated `/api/runtime/v1` plane, which
binds the **ECO engine (`eco.v01`)** to a **local Ollama** endpoint serving the
Owner-supplied Qwen model, at **exactly €0** provider cost.

This is an **implementation runbook**, not an authorization to run a live
benchmark. Running it requires the Owner to (1) install the service-secret file,
(2) supply the concrete ECO model tag, and (3) authorize an exact SHA.

> **Do not** use `C:\pr0` or a global Python. Do not put any secret in `.env`,
> argv, logs, Git, shell history, or a cloud-synced folder. See
> [SECRET_FILE_SUPPORT.md](SECRET_FILE_SUPPORT.md) and
> [LIVE_BENCHMARK_SECRET_INSTALL.md](LIVE_BENCHMARK_SECRET_INSTALL.md).

All commands run from the repository root in **PowerShell**:

```powershell
cd F:\Youtab_AI_COS_Platform\Youtab-Agent-Runtime
```

## 1. Python interpreter (repository-local)

Supported band: **`>=3.11,<3.14`** (`pyproject.toml`). CI runs **3.12**;
`.venv-qual` is the supported interpreter (Python 3.12 + fastapi/uvicorn).
**Python 3.14 is not supported** — `uv` refuses it, Rust-backed wheels have no
cp314 build, and `tools/daemon_pool.py` fails closed on 3.14. A global 3.14 with
no project install is exactly why the earlier attempt raised `ModuleNotFoundError`.

Create the repo-local venv with **uv** (preferred) …

```powershell
uv venv .venv-qual --python 3.12
uv pip install -e ".[dev,web]"
```

… or with plain pip:

```powershell
py -3.12 -m venv .venv-qual
.\.venv-qual\Scripts\python.exe -m pip install -e ".[dev,web]"
```

`pywin32` installs automatically on Windows (a `sys_platform == 'win32'` marker
in `pyproject.toml`); it is required by the strict-tier secret loader's DACL
check, which **fails closed** if pywin32 cannot verify a file. The `web` extra
provides fastapi + uvicorn (the runtime API server); `dev` provides pytest/tooling.

Use `.\.venv-qual\Scripts\python.exe` for **every** command below — never a
global `python`.

## 2. Owner-only service-secret file (no plaintext)

The runtime↔harness auth uses the **service** secret
`YOUTAB_AGENT_RUNTIME_SERVICE_SECRET` (distinct from any provider key — Track A
local Ollama needs **no** provider key; `credential_source: none_local`).

Install it as an owner-only file per
[LIVE_BENCHMARK_SECRET_INSTALL.md §2](LIVE_BENCHMARK_SECRET_INSTALL.md) (a folder
under `%LOCALAPPDATA%\Youtab\secrets`, owner-only DACL via `icacls`). Then point
the process at the **file**, never the value:

```powershell
$env:YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE = "$env:LOCALAPPDATA\Youtab\secrets\runtime_service_secret"
```

The runtime reads it via `_runtime_secret()` (`env_or_file`, file preferred); the
harness reads the **same** file via `--secret-file` with the hardened strict-tier
loader (owner-only, no symlink, not inside the repo or a cloud folder).

## 3. Non-secret environment (Track A)

```powershell
$env:OLLAMA_BASE_URL             = "http://127.0.0.1:11434"          # loopback (or a private/Tailnet host)
$env:YOUTAB_ECO_MODEL            = "youtab-qwen35-9b-agent-64k:latest"  # Owner-supplied concrete tag
$env:YOUTAB_AGENT_SERVE_HEADLESS = "1"
```

* `OLLAMA_BASE_URL` must resolve to a **verified-local** target — loopback,
  RFC1918-private, link-local, or Tailscale CGNAT. A public host is refused
  (fail closed) and would **not** qualify for the €0 local-zero policy.
* `YOUTAB_ECO_MODEL` is the concrete Ollama tag. The committed roster carries
  only a provider-neutral placeholder; without this env the runtime **refuses**
  an ECO run (`eco_model_unconfigured`). Never commit the tag.
* Track A needs **no** campaign id and **no** FX snapshot — a €0 conversion
  requires neither, and none is fabricated. Budget enforcement is still reported
  armed via the verified local-zero cost policy (see §5).

Confirm Ollama is serving the model:

```powershell
ollama list        # the tag from YOUTAB_ECO_MODEL must appear
```

## 4. Start the runtime (from the repo root)

```powershell
.\.venv-qual\Scripts\python.exe -m youtab_agent_cli.main serve --host 127.0.0.1 --port 8081 --no-open
```

`serve` is the headless backend that mounts `/api/runtime/v1`. The `--base-url`
you pass the harness must match this origin (`http://127.0.0.1:8081`).

## 5. Signed health / preflight + engine attestation

The harness authenticates every call (service bearer + gateway identity headers;
mutating commands are HMAC-signed) and auto-runs preflight before the first task.
You can sanity-check the authenticated attestation yourself — pass the engine so
the runtime returns the **effective** binding:

`GET /api/runtime/v1/preflight?engine=eco.v01` should report:

* `engine_attestation.engine_profile = "eco.v01"`, `provider = "ollama"`,
  `model = <your YOUTAB_ECO_MODEL>`, `model_identifier_status = "resolved"`;
* `execution = "local"`, `endpoint_class` in {loopback, private, link_local, cgnat},
  `endpoint_authorized = true`;
* `provider_cost_policy = "local_zero_verified"`;
* `budget_enforcement_enabled = true`, `budget_enforcement_source = "local_zero_verified"`;
* `redaction_enabled = true`, `no_production_dataset = true`, and the full
  `build_sha`;
* when the local Ollama server answers, `ollama_model_digest` (64-hex) +
  `ollama_digest_status = "verified_present"`.

The harness refuses the run (fail closed) on any mismatch — wrong engine,
provider, model, endpoint class, cost policy, an unresolved model, or a digest
that does not match `--expected-model-digest`.

## 6. Run Track A

Pick an artifact directory **outside** the repo and not cloud-synced. The
`--expected-sha` is the authorized clean-HEAD SHA; `--expected-model-digest` is
the Owner-supplied full 64-hex Ollama manifest digest
(e.g. `b7b9afeaf023a549a9e6fc7960694f3c32fc490d415bbdf1751e2f390cf4ae48`).

```powershell
.\.venv-qual\Scripts\python.exe -m tests.benchmark.harness.cli run `
  --mode local_runtime --track A `
  --base-url http://127.0.0.1:8081 `
  --secret-file "$env:LOCALAPPDATA\Youtab\secrets\runtime_service_secret" `
  --expected-sha <authorized-clean-HEAD-sha> `
  --expected-model-digest <64-hex-ollama-manifest-digest> `
  --stage canary --scenario <one-scenario-id> --canary `
  --out "$env:LOCALAPPDATA\youtab-bench\trackA"
```

* `--mode local_runtime` requires `--track A` (the engine-bound track). Without a
  bound engine the harness refuses — it never dispatches on the worker default.
* Start with `--stage canary --canary` (exactly one scenario, one request, no
  retry) before a `--stage pilot` / `--stage full` run.
* Every record is stamped with the Track A provenance (engine profile, provider,
  model, local-zero cost model) and each model call records an audited **€0** cost.

## 7. Clean shutdown / restart by port

Stop with `Ctrl-C` in the runtime terminal (graceful uvicorn shutdown), or by
port:

```powershell
Get-NetTCPConnection -LocalPort 8081 | Select-Object -Expand OwningProcess | ForEach-Object { Stop-Process -Id $_ }
```

Rotating the service secret is a process restart (re-read on next start).

## 8. Artifacts

Results land under `--out`: `results.jsonl` (one redacted `benchmark.v1` record
per scenario), `summary.json`, and a `retention.json` marker. The directory is
created owner-only and must stay outside the repo and any cloud-sync folder.
