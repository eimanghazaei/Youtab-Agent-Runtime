# Phase 1 Preflight Evidence — 2026-08-01

## Scope

This preflight exercised the pinned upstream runtime without modifying its
core source. It proves that the locked development environment can be created
and that a small credential-free smoke slice runs through the repository's
canonical test runner. It does not qualify the complete Runtime.

## Source identity

| Field | Value |
|---|---|
| Upstream baseline | `cc4cab2f592e60a197e796506de9168f74baf3ea` |
| Bootstrap commit | `b7ad3b89eb98401df62996563e28be2ed855d145` |
| Branch | `bootstrap/phase-0-ground-truth` |
| Core changes | None |

## Toolchain

| Tool | Result |
|---|---|
| Python | `3.12.13` |
| uv | `0.11.33` |
| Node.js | `v24.14.0` |
| npm | `11.9.0` |
| Docker | BLOCKED — executable not installed in this runtime |

## Locked environment creation

Command:

```bash
UV_CACHE_DIR=/tmp/youtab-agent-runtime-uv-cache uv sync --extra dev --frozen
```

Result: PASS. The project built from the local pinned checkout and 78 locked
packages were installed into the ignored local `.venv`. The cache was moved
to `/tmp` because the runtime-owned `/root/.cache` path is read-only; this did
not alter dependency resolution or the repository lockfile.

## Credential-free smoke

Canonical command:

```bash
scripts/run_tests.sh -j 2 \
  tests/test_hermes_constants.py \
  tests/test_toolsets.py \
  tests/agent/test_backend_identity.py -q
```

Result: **PASS — 84 passed, 0 failed**, 3 test files, canonical hermetic
runner, two workers.

Breakdown:

| Test file | Result |
|---|---:|
| `tests/test_hermes_constants.py` | 47 passed |
| `tests/test_toolsets.py` | 22 passed |
| `tests/agent/test_backend_identity.py` | 15 passed |

## Sandbox-blocked timezone/code-execution probe

The first wider command also included `tests/test_timezone.py`. Its result was
93 passed and 2 failed overall. The two failures both stopped before executing
the timezone assertion because this Work runtime denied creation of the Unix
domain socket used by `execute_code`:

```text
PermissionError: [Errno 1] Operation not permitted
socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
```

Classification: **ENVIRONMENT BLOCKED**, not PASS and not yet a confirmed
product defect. The Runtime code and tests were not weakened or bypassed to
force a green result. These two tests must be rerun in the planned isolated
Runtime host/container where Unix-domain sockets are permitted.

## Observed warning

The wider run reported that the linked SQLite `3.50.4` is affected by the
upstream WAL-reset warning. The runtime automatically selected
`journal_mode=DELETE` instead of WAL. This is a mitigation, not closure. Phase
1 must verify the production Runtime image links SQLite `3.51.3+` or an
upstream-listed fixed backport before production admission.

## Verdict

- Locked development setup: PASS.
- Small credential-free smoke slice: PASS.
- Code-execution timezone slice: BLOCKED by current sandbox.
- Docker/container baseline: BLOCKED because Docker is unavailable here.
- Full Phase 1 qualification: NOT STARTED / NOT CLAIMED.
