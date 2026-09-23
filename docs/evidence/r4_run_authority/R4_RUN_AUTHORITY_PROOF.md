# R4: durable run-owner fencing (proof)

**Verdict:** the R4 product fix and its live evidence are ready for owner review.
R4 stays **OPEN** until the owner accepts ADR-0002 and integrates it. Nothing
has been pushed, merged, deployed or installed.

| Item | Value |
|---|---|
| Base (partial fail-closed correction) | `87b1deff51b3406c861a874b1daf1e8ffe4708a6` |
| Fix commit | `415a1734a49f04ceced1801f5ddaffd7ff36ee55`, tree `0d2dcb51b1e431be2274960a2e075cc77b30877a` |
| Branch / worktree | `release/r4-startup-failclosed` · `_wt/runtime-r4-regressions` (local only) |
| Image | `youtab-agent-runtime:r4-415a1734a`, Id `sha256:ccfe1bc5e1843c5e489b60bcb28706efcc9231d7b83c23b687622ebedcce747e` |
| Embedded SHA | `/opt/youtab/.youtab_agent_build_sha` = `415a1734a49f…` (provenance match) |
| Changed-file parity | sha256 of `api_server.py`, `durable_run_authority.py`, `durable_run_store.py`, `durable_run_store_pg.py` in the image equals the commit |
| `docker save` tarball | sha256 `9e705330b25ed65b1927133f6bdd897aef5e40491cf43bdb3e05c4a5bad908c4` (1,084,438,016 bytes) |
| Build | `docker build --build-arg YOUTAB_AGENT_GIT_SHA=$(git rev-parse HEAD) -t youtab-agent-runtime:r4-415a1734a .` from a clean tree (`r4_image_build.log`) |
| Decision | `docs/architecture/ADR-0002-durable-run-authority-singleton.md` (PostgreSQL-enforced singleton, Proposed) |

## Design chosen

**Singleton, enforced by PostgreSQL.** A replica count alone does not enforce
it. The chosen design meets the decision document's conditions:

- **Before readiness:** the gateway acquires a session advisory lock with
  `pg_try_advisory_lock` on a dedicated supervised connection.
- **For its whole lifetime:** every durable write is fenced. The write checks
  that the epoch row still names this instance and that `pg_locks` shows the
  lock still held.
- **Competing startup:** a second instance is refused with `AuthorityHeld`.
- **Lock loss:** the gateway fail-stops. It refuses admission, the 202 ack,
  dispatch, readiness and terminal commits, then interrupts agents and exits
  with code 75.
- **Recovery:** only once exclusivity is proven, all prior owner-stamped
  nonterminal `/v1/runs` rows move to `UNKNOWN`, regardless of PID.
- **Ownership stamp:** runs are stamped `inst:<uuid>:<epoch>`. The supervisor
  checks the lock every 2 s. The advisory-lock key is `(0x59544152,
  crc32('v1_runs'))` and the collision domain is one database.

The multi-instance protocol was not needed. The singleton could be made safe
through startup, active execution, lock loss and restart.

## Focused and regression tests (canonical `scripts/run_tests.sh`)

PostgreSQL tests ran against a real `postgres:16-alpine` container on
`127.0.0.1:55432`. The canonical runner scrubs `YOUTAB_TEST_PG_DSN`, so an
unreachable default port silently skips those tests. Here they executed and
were not skipped.

| Command | Result |
|---|---|
| `scripts/run_tests.sh tests/durable_execution/test_p5_terminal_durability.py -q` (the two formerly red regressions and fail-closed cases) | exit 0: **10 passed** |
| `scripts/run_tests.sh tests/durable_execution/test_r4_run_authority.py -v` | exit 0: **7 passed** (3 SQLite, 4 real PostgreSQL, 0 skipped) |
| `YOUTAB_AGENT_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/durable_execution/ -q` (×2) | exit 0: **86 passed** each run |
| Durable suite plus the 14 `tests/gateway/test_api_server*`, reconnect and readiness files | 280 passed; see load-flake note |
| `ruff check` on the changed files; `git diff --check` | exit 0 / exit 0 |

What the behavioral tests cover:

- **Same-PID orphan:** owners `pid:<os.getpid()>`, `pid:999999` and
  `inst:…:7` all end `UNKNOWN`, on both SQLite and PostgreSQL.
- **Live peer:** a real subprocess holds the authority. The second start fails
  with `AuthorityHeld` and the peer's run stays `RUNNING`. After the peer dies
  abruptly, the next start recovers the run to `UNKNOWN`.
- **Startup failures:** failure to acquire fails the start. Failure to recover
  releases the authority, then fails the start. A store without authority
  support is unsupported.
- **Competing instance on PostgreSQL:** the second start is refused. The
  holder's run and readiness are unaffected.
- **Authority loss:** covered by a superseded SQLite epoch and by
  `pg_terminate_backend` on PostgreSQL. After loss there is no terminal commit,
  admission returns 503 with no dispatch, readiness returns 503, and the
  fail-stop fires once. The next holder uses epoch+1 and sets `UNKNOWN`. A late
  write from the old holder raises `AuthorityLost`.
- **HTTP/SSE:** `run.completed` is sent only after `SUCCEEDED` is committed. On
  lock loss mid-run, the stream carries `run.reconciliation_required` and no
  `run.completed`, and `/result` returns `terminal:false` with `output:null`.
  After restart the status is `unknown`, the committed result is unchanged,
  and the agent was called exactly once.
- **Unchanged rows:** ownerless `QUEUED` rows, terminal rows and `delegate` rows
  are left alone.

**Load flake note.** Two tests failed once under 32-way parallel load and
passed on rerun and in isolation (18/18):
`test_p3a…long_task_survives_short_wait` (a 2 s worker-claim race) and
`test_p0g`/`test_p4` (Windows job-object process supervision). Neither imports
any changed code: `grep` finds no `api_server` or authority reference in those
files. They are pre-existing timing sensitivities, not R4 regressions.

## Live probe on the fix-bearing image (raw: `R4_LIVE_RAW.txt`)

**Setup:** docker network `r4-net`, a fresh `postgres:16-alpine`, a streaming
OpenAI-compatible mock (`mock_provider.py`), and gateways booted through the
normal s6 `/init` with `gateway run` (`probe_gateway_run.sh`,
`probe_config.yaml`). Every request went over real HTTP with bearer auth.

| # | Case (decision doc acceptance) | Observed |
|---|---|---|
| 5a | Success: terminal SSE after durable commit | `status.completed/SUCCEEDED` committed at `…603.0535`, then SSE `run.completed` at `…603.0576`. Owner `inst:fd3d…:1`. Row `SUCCEEDED\|CUSTOMER_TASK_OK_42` |
| 5b | Cancellation | `/stop` produced events `CANCELLING`, `cancel_requested`, `status.cancelled` (commit `…620.3196`), then live SSE `run.cancelled` (`…620.3234`). Row `CANCELLED` |
| 5c | Failure | `status.failed` committed at `…648.1314`, then SSE `run.failed` at `…648.1373`. Row `FAILED`, error `HTTP 400: mock provider rejected request` |
| 2 | Competing instance on the same DB during an in-flight run | gw2 raised `AuthorityHeld … (epoch=1 host=0e69d869cb15 pid=123)`. gw2 `/health/ready` and `POST /v1/runs` returned `000` (no bind) and gw2 crash-looped as a standby under s6. gw1 stayed ready and its run finished `SUCCEEDED` under epoch 1 |
| 1 | Same numeric PID after an abrupt kill | Fresh container A: gateway PID **123**, epoch 3, SLOW run `RUNNING` owned by `inst:abea…:3`, plus a seeded legacy row `pid:123`. After SIGKILL and recreate, container B's gateway was also PID **123**, at epoch 4. Both rows ended `UNKNOWN`. The pre-R4 logic would have kept them `RUNNING` |
| 4 | Abrupt kill and restart | Advisory lock released on kill (`pg_locks` empty). The restarted instance moved the run to `UNKNOWN` (`reason=prior_instance_superseded`, `authority_epoch`). `/result`: `status:unknown, terminal:false, output:null`. The committed success result was unchanged |
| 3b | Lock lost after startup | `pg_terminate_backend(<lock backend>)` logged `CRITICAL … authority LOST … AdminShutdown; fail-stopping`. Within 1 s PID 123 exited and s6 started PID 182; `/health/ready` and `POST` returned `000` meanwhile. The live SSE was cut (curl exit 18) with **no** `run.completed`. The new process took epoch 5 and set the run `UNKNOWN`. It was still `UNKNOWN` after the mock's 20 s response window, so there was no late success |
| 3a/3c | PostgreSQL lost mid-run; startup with the store down | Stopping PG caused the fail-stop. Restarts then failed closed (`PostgreSQL backend unavailable at startup`) with no bind or readiness. Once PG was back (≈16 s) the gateway took epoch 6 and set the in-flight run `UNKNOWN` |
| — | No blind retry | The mock saw exactly 6 SLOW requests for 6 SLOW submissions. There were 0 `SUCCEEDED` rows without a committed `status.completed` event |

## Gates (`scripts/youtab/run_all_gates.sh`, venv Python, retries 0)

Command: `YOUTAB_AGENT_PYTHON=.venv/Scripts/python.exe YOUTAB_AGENT_TEST_FILE_RETRIES=0 scripts/youtab/run_all_gates.sh <evidence-dir>` on Windows 11. **Result: 9 of 12 gates PASS. R4 introduces no gate regression.**

| Gate | Result |
|---|---|
| secrets, dependencies, sast, container-control-plane-isolation, router-policy, owasp-smoke, compileall, ruff, whitespace | **PASS** |
| branding | **BLOCKED (environment):** `--ocr` requested but neither tesseract nor rapidocr is installed (`GATE_COULD_NOT_RUN`). The text-only scan (`branding_gate.py --root .`) exits 0, `passed: true`, 8057 files, no retired-brand hits |
| uv-lock | **BLOCKED (environment):** `uv: command not found` (exit 127). The fix changes neither `pyproject.toml` nor `uv.lock` |
| unit-integration-e2e | **FAIL; pre-existing on this Windows host:** 224 failed in 84 files. The unmodified base `87b1deff5` fails **226 in 86 files** on the same file list. 222 failing IDs are identical. 4 fail only on base and 2 only on the fix run. Of those 2, `test_import_failure_diagnostics::…in_test_import_failure…` passed in isolation, and `test_cmd_update::…on_fork_checks_upstream…` mocks `subprocess.run` in a file that fails 5 other tests on base too. No failing file references `api_server`, `durable_run` or `run_authority`. The failures are mostly POSIX-only behavior (symlinks, `execvp`, s6, curses, `/proc`) in `tests/tools` and `tests/youtab_agent_cli` |

The gate run left an empty untracked `$tmp` file in the repo root (a test artifact, which `test_repo_root_is_not_a_scratch_dir` flags). It was removed. The rerun of the fix side of the baseline comparison was stopped by the host for low memory and was not restarted.

## Remaining risk (explicit)

1. **Effect window after loss.** A tool call already running when the lock is
   lost can finish an external effect in the detection window. That window is
   at most the 2 s supervisor interval, which was about 1 s live via
   `AdminShutdown`, or the keepalive/`tcp_user_timeout` time on a silent
   network partition, which can reach about 10 to 15 s. Such a run can never
   commit success, and the next holder marks it `UNKNOWN` with no retry. Closing
   the window needs effect fencing through the Lane-1 effect ledger, which is
   outside R4.
2. **Topology is a product contract.** The deployment must have one effectful
   `/v1/runs` instance per database, use a `Recreate` rollout (a pre-R4 binary
   has no fencing), and give the authority a direct or session-mode PostgreSQL
   connection (not PgBouncer transaction mode). Multiplexed API-server profiles
   sharing one store fail closed.
3. **A standby crash-loops until it takes over,** because gateway startup raises
   `AuthorityHeld`. That is correct fail-closed behavior, but it produces noisy
   logs. Operators should expect it, or run a single replica.
4. **Legacy rows.** The first R4 start marks pre-R4 `pid:` nonterminal
   `/v1/runs` rows `UNKNOWN`, which is intended. After a rollback, `inst:` rows
   need manual resolution (see the ADR).
5. **Environment gaps.** The branding OCR engine is absent locally, and the
   live probe ran on one Docker Desktop host with a synthetic mock task, not a
   customer workload or a multi-host network partition.

## Owner integration steps

1. Review and accept ADR-0002 (or reject it and choose the multi-instance
   protocol).
2. Bring `2f71295d2`, `87b1deff5` and `415a1734a` onto the Timeout owner branch
   without rewriting them (cherry-pick or merge as the owner decides). Resolve
   conflicts in `api_server.py` around `_admit_durable_or_fail`,
   `_reconcile_orphaned_runs_on_startup`, `_handle_ready`, `connect()` and
   `disconnect()`.
3. Rebuild the RC image from the integrated SHA with
   `--build-arg YOUTAB_AGENT_GIT_SHA`. Rerun
   `tests/durable_execution/` with PostgreSQL on `127.0.0.1:55432`, then repeat
   this live probe (cases 1 to 5) on that exact image.
4. Update the admin install procedure: set replicas to 1, use the `Recreate`
   strategy, keep the direct PG DSN, treat exit code 75 as restartable, and
   explain what `UNKNOWN` means to operators.
