# Owner-managed live-benchmark secret installation (WAVE-30B §5)

**The Owner — not the agent — installs the real provider test key.** This runbook
gives exact, copy-pasteable commands. It contains **no secret value**, and no
step here is ever run by the agent. The provider is chosen by the Owner from
`docs/benchmark/PROVIDER_SELECTION_MATRIX.md` (this repo recommends none).

There are **two** distinct secrets (see `docs/ops/SECRET_FILE_SUPPORT.md`):

| Secret | Delivered to | File var |
|---|---|---|
| Runtime **service** secret (harness → runtime auth/HMAC) | the runtime service **and** the harness (`--secret-file`) | `YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE` |
| **Provider** API key (runtime → provider) | the runtime service only | `<PROVIDER_API_KEY_ENV>_FILE`, e.g. `OPENAI_API_KEY_FILE` |

The agent never receives, displays, copies, or persists either value.

---

## 1. Linux / dedicated VPS (production target)

Required contract for **both** secret files (strict tier):

```
Location:            /run/secrets/<name>       (tmpfs, not on disk-at-rest)
Owner:               the runtime service account (its uid)
Mode:                0400   (owner read only — no group/world, no owner write/exec)
Regular file:        required            Symlink: forbidden
Max size:            64 KiB              Encoding: UTF-8, no BOM
Format:              raw secret, one line, at most one trailing newline
```

### 1a. Docker Compose (Docker secrets)

The default secret mount is world-readable `0444` root-owned — that satisfies the
**service** secret (boundary tier) but **fails** the strict tier used for
**provider** keys. Provider keys must be mounted `mode: 0400` owned by the runtime
uid:

```yaml
services:
  engine:
    environment:
      YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE: /run/secrets/agent_runtime_service_secret
      OPENAI_API_KEY_FILE: /run/secrets/openai_api_key   # example provider
      YOUTAB_AGENT_LIVE_BENCHMARK: "1"                    # refuse plaintext provider creds
    secrets:
      - agent_runtime_service_secret
      - source: openai_api_key
        target: openai_api_key
        uid: "10001"        # the runtime service account uid inside the container
        mode: 0400
secrets:
  agent_runtime_service_secret: { file: ./secrets/agent_runtime_service_secret }
  openai_api_key:              { file: ./secrets/openai_api_key }
```

Create the on-host source files owner-only, insert the value with an editor (never
argv/history), then bring the stack up:

```bash
umask 077
mkdir -p ./secrets
: > ./secrets/openai_api_key && chmod 0400 ./secrets/openai_api_key
# paste the key into the file with an editor; ensure UTF-8, no BOM, one line:
${EDITOR:-nano} ./secrets/openai_api_key
```

### 1b. Bare systemd / non-container runtime

```bash
sudo install -o youtab-runtime -g youtab-runtime -m 0400 /dev/null /run/secrets/openai_api_key
sudo -u youtab-runtime ${EDITOR:-nano} /run/secrets/openai_api_key   # paste; UTF-8, no BOM, one line
export OPENAI_API_KEY_FILE=/run/secrets/openai_api_key
export YOUTAB_AGENT_LIVE_BENCHMARK=1
```

### 1c. Encrypted backup with SOPS + age (B-15; Vault NOT required this phase)

Back the secret up **encrypted at rest**; decrypt only into the `0400` runtime
file. Do **not** commit the decrypted value.

```bash
age-keygen -o ~/.config/sops/age/keys.txt          # once; keep this key offline
export SOPS_AGE_RECIPIENTS=age1....                  # the public recipient
sops --encrypt --input-type binary --output-type binary \
     ./secrets/openai_api_key > openai_api_key.enc   # commit ONLY the .enc, never the plaintext
# restore on the VPS:
sops --decrypt --input-type binary --output-type binary openai_api_key.enc \
  | install -o youtab-runtime -m 0400 /dev/stdin /run/secrets/openai_api_key
```

---

## 2. Windows (local integration testing only)

Use a directory **outside** the repo and outside any cloud-sync root
(OneDrive/Dropbox/…); `%LOCALAPPDATA%` is not cloud-synced by default.

```powershell
New-Item -ItemType Directory -Force -Path "$env:LOCALAPPDATA\Youtab\secrets" | Out-Null
$sid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
icacls "$env:LOCALAPPDATA\Youtab\secrets" /setowner "*$sid" /inheritance:r `
  /grant:r "*${sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F"
$f = "$env:LOCALAPPDATA\Youtab\secrets\openai_api_key.txt"
New-Item -ItemType File -Path $f | Out-Null          # born owner-only by inheritance
icacls $f /setowner "*$sid" /inheritance:r /grant:r "*${sid}:F" "*S-1-5-18:F"
notepad $f                                             # paste the key; save UTF-8 (no BOM), one line
#   do NOT re-run New-Item -Force after pasting — it truncates the file
$env:OPENAI_API_KEY_FILE = $f
$env:YOUTAB_AGENT_LIVE_BENCHMARK = "1"
```

Use the **current-token SID**, not `$env:USERNAME` (name→SID is ambiguous on
domain machines). Run non-elevated so the owner is you, not `Administrators`. The
strict loader requires this protected owner-only DACL and refuses a broad one.

---

## 3. Verify WITHOUT revealing the value

```bash
# Linux
stat -c '%U %a %s' /run/secrets/openai_api_key        # owner, mode (want 0400), size
python -c "from youtab_agent_cli.secret_file import read_secret_file as r; \
print('OK len=', len(r('/run/secrets/openai_api_key', var='OPENAI_API_KEY', require_secure_perms=True)))"
```

```powershell
# Windows
Get-Acl $f | Format-List Owner, AccessToString ; icacls $f
python -c "from youtab_agent_cli.secret_file import read_secret_file as r; import os; \
print('OK len=', len(r(os.environ['OPENAI_API_KEY_FILE'], var='OPENAI_API_KEY', require_secure_perms=True)))"
```

Both print **only the length** — `read_secret_file` never prints or logs the
value, and its errors carry only the variable name and safe path metadata.

## 4. Revocation

`rm -f` / `Remove-Item -Force` is an ordinary unlink, **not** a secure wipe on
SSD/tmpfs. To truly revoke, **rotate the key at the provider** and replace the
file. Then restart the runtime (the runtime secret is read per request; provider
keys are re-read on process start).

## 5. Confirm the runtime is ready (no secret handled)

Against a running runtime, the authenticated preflight reports the safety posture
(build SHA, provider/model NAMES, `provider_credential_source: file`,
`redaction_enabled`, `budget_enforcement_enabled`) with **no secret**:

```
GET /api/runtime/v1/preflight   (service-bearer + HMAC)
```

The harness verifies this automatically before any live call
(`--expected-sha <authorized SHA>`); it refuses to run against a mismatched or
unsafe runtime.
