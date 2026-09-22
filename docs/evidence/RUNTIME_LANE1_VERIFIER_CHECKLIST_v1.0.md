# Runtime Lane 1 — Independent Exact-SHA Verifier Checklist v1.0

Read-only verification for the frozen Lane-1 delivery. Run every step against the
exact branch HEAD; no step mutates the repo, network, or any other worktree. Base
`c92069a5a3a9b83d80e0e0074fe05f0561d3b19c`. Set `export PYTHONPATH=$PWD` at the
worktree root. Where a supported interpreter is required, use Python 3.12
(`>=3.11,<3.14`); `py -3` here resolves to unsupported 3.14 (diagnostic only).

For each check: the claim, how to verify, and the expected result.

## A. Middleware-path authenticity (no parallel API)
- **Claim:** effectful ops are governed at the real `tool_executor` seam.
- **Verify:** `grep -n "route_managed_file_tool" agent/tool_executor.py` shows the call inside `_authorized_dispatch` *before* `enforce_managed_tool_authority`. The acceptance tests call `agent.tool_executor._run_agent_tool_execution_middleware(...)` with `function_name` in {`read_file`,`write_file`,`patch`} — not the router/gate directly (`grep -n "_run_agent_tool_execution_middleware\|route_managed_file_tool\|managed_fs_gate" tests/youtab_runtime/test_managed_fs_production_path.py tests/youtab_runtime/test_managed_fs_production_matrix.py`; the latter two symbols must be ABSENT as direct calls).
- **Expected:** router wired at the seam; acceptance tests drive the middleware only.

## B. No shell bypass
- **Claim:** a managed op never touches `ShellFileOperations._exec` or the registered handler.
- **Verify:** both acceptance suites monkeypatch `ShellFileOperations._exec` with a spy and assert `spy.called is False` (+ `execute` handler spy `.called is False`) on every managed op; the standalone control asserts both ARE called (proving the spies are wired).
- **Expected:** managed ops use grant-bound host-IO; standalone uses the shell.

## C. Approval / grant / workspace binding
- **Claim:** effect identity binds op + canonical path + workspace + content/patch digest + tenant/principal; authorization is Ed25519-verified, single-use, expiring, workspace/request-digest-bound.
- **Verify:** `youtab_runtime/approval.py::reserve_and_consume_authorization` (trust+signature+binding+digest+atomic single-use) and `authorization_transport.py::correlate_proposal`; run `tests/youtab_runtime/test_effect_authorization.py`, `test_authorization_transport.py`, `test_grant_fs_effect.py`.
- **Expected:** all green; negative cases fail closed.

## D. Pre-execution rejection → zero effect row
- **Claim:** every pre-execution rejection yields zero fs mutation and zero effect-ledger row; `UNKNOWN` only post-mutation.
- **Verify:** `test_managed_fs_production_matrix.py` — `test_move_destination_outside_grant_fails_closed` asserts `list_effects_in_workspace(...) == []`; the missing/expired/wrong-binding/changed/traversal/remote cases assert no committed effect and filesystem unchanged. Report §10.2 has the full state table.
- **Expected:** green; zero effect rows on rejection.

## E. Crash-after-effect → UNKNOWN
- **Claim:** a mutation-phase host-IO failure leaves exactly one non-terminal `unknown` effect (never committed, never blind-replayed).
- **Verify:** `test_crash_after_effect_left_unknown` (injects failure at `_host_write` after claim) asserts one `unknown` effect + zero committed.
- **Expected:** green.

## F. Replay does not mutate twice
- **Verify:** `test_replay_no_double_write` / `test_replayed_copied_authorization_refused` (matrix) and the in-process/cross-process replay tests: second use of a consumed authorization is refused, filesystem unchanged, exactly one committed effect.
- **Expected:** green.

## G. Junction / TOCTOU defenses
- **Verify:** `test_grant_fs_toctou.py`, `test_grant_fs_delete_move.py`, and the matrix junction test exercise real reparse swaps (parent→out-of-grant junction) and assert refusal + the out-of-grant target untouched. `grant_fs.open_within_grant` derives containment from the opened handle's final path (`_final_path_from_handle`), not a second `resolve()`.
- **Expected:** green; out-of-grant object never modified.

## H. Cleanup lifecycle
- **Verify:** `tests/tools/test_result_store_cleanup_lifecycle.py` — teardown (`_cleanup_inactive_envs`, `cleanup_vm`) invokes `cleanup_run_scope`; teardown survives cleanup raising / returning False; cleanup uses a bounded timeout, deletes only the current run scope, and issues no process kill. `test_tool_result_storage_containment.py` proves scope containment + private perms + bounded spill.
- **Expected:** green.

## I. Supported-Python tests
- **Verify:** on Python 3.12 (ideally `uv sync --frozen`; else a 3.12 venv with pytest/cryptography/pydantic + pyyaml/psutil/python-dotenv/requests), run the 22 Lane-1 files (§7 list). Also `py -3.12 -m py_compile` the runtime modules.
- **Expected:** 266 collected / 266 passed; py_compile OK (no 3.14-only syntax).

## J. Attribution & secrets
- **Verify:** `git log --format='%an <%ae>' c92069a5a..HEAD | sort -u` (single author Eiman); `git log --format='%B' c92069a5a..HEAD | grep -iE 'co-authored-by|generated with|claude'` (empty); secret/machine-path scan over changed files (`git diff --name-only c92069a5a..HEAD`) for `AKIA…`, `-----BEGIN … PRIVATE`, machine paths.
- **Expected:** single author; no attribution; no secrets/machine paths.

## K. Cross-lane conflict analysis (read-only)
- **Verify:** `git merge-base <Lane1> 1ed19f0914cce6d074aae6cf378d81f05bc6d283` = `ca89219ae…`; overlap Lane1∩Lane2 = 0; `git merge-tree ca89219ae <Lane1> 1ed19f091…` conflicts = 0; Lane1∩Lane3 (`d877b373c`) = 0 conflicts at base `c92069a5a`. `origin/main` (`c7650a1b9…`) had no local common ancestor — recompute against full-history main.
- **Expected:** Lane1 disjoint + conflict-free with Lane2 and Lane3; main pending.

## L. Static gates
- **Verify:** `ruff check <changed .py>`; `mypy --ignore-missing-imports --follow-imports=skip <11 runtime modules>`; `git diff --check c92069a5a..HEAD`.
- **Expected:** clean; 0 mypy issues on the delivered modules (full-repo mypy hits pre-existing unrelated debt, e.g. `tools/tts_tool.py` — out of scope).

## Verdict gate
`LANE 1 LOCAL MANAGED FILE EXECUTION VERIFIED_NOT_REVIEWED`; overall **NO-GO** until the §10.6 items (LIVE Simorgh issuance, remote authority, V4A ADD/UPDATE-hunks + multi-op, `search_files` grant-routing, integrated Lane1+2+3 exact-SHA suite, lockfile-frozen supported-Python run) are closed.
