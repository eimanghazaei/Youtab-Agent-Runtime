# Runtime Lane 1 — Managed Filesystem Effects — Delivery Report v1.0

**Verdict:** `LANE 1 READY FOR MASTER INTEGRATION` (local managed file execution),
with the explicit, bounded limitations recorded in §8. Nothing pushed.

## 1. Exact-SHA manifest

| | SHA |
|---|---|
| Repository | `github.com/eimanghazaei/Youtab-Agent-Runtime` (PRIVATE) |
| Branch | `delivery/runtime-folder-grant` (worktree `C:\Users\eiman\worktrees\rt-lane1-delivery`) |
| Base (frozen PX product SHA) | `c92069a5a3a9b83d80e0e0074fe05f0561d3b19c` |
| Code-final SHA | `dd21b04f9d9f02a3a835178a7c0ba38030adb7be` |
| Final SHA (this report commit) | *the commit that adds this file — the branch HEAD after it lands* |
| Diff range | `c92069a5a..HEAD` |
| Toolchain | Python 3.14, pytest 9.1.1, ruff, mypy; Windows 11 host |
| Authorship | all commits `Eiman Ghazaei <eiman.ghazaei@gmail.com>`, **zero attribution trailers** |

## 2. Lane-1 commits (base → HEAD, newest first)

Foundation (earlier rounds): `c014f4abf` folder-grant path safety · `f3e7a6f26` grant→effect-ledger spine · `dab1b2b23` approval + worker-lease · `5213440d8` approval/lease/TOCTOU boundary · `ddb9bf75a` parent-junction TOCTOU · `0b571de22` signed authority + workspace receipts + lease/evidence · `d11269d00` managed_fs_gate · `f0fd316c3` LF normalize · `ca89219ae` mypy Literal.

Security-foundation round: `a493664a7` ADR-0005 · `afae22df2` non-forgeable evidence verifier · `4886496cf` opened-handle final-path containment · `bc37f58ce` classify fs/shell/code tools as effects (bypass close) · `8b68e59a5` workspace-scoped receipt lookup · `2792a1897` cross-process gate proof · `24f59a292` authorization transport · `907e5aa0f` grant-bound delete/move host-IO · `9597f7ea3` ADR §7 writer disposition.

Production-positive-path round: `b1033da12` remote fail-closed contract · `dafd78fa3` transport correlate/consume split · `2609353e8` **router wired into the tool_executor seam** · `1feac7dcc` production write_file proof.

Storage hardening: `ca850b056` containment invariant · `db2ee8765` per-run-scoped/private/bounded spill.

Operation-matrix round: `81a873cf8` cleanup bound to env teardown + public scope accessor · `c37ad47ef` **patch/delete/move governed through the router** · `519ee1d5c` full operation matrix + adversarial tests · `dd21b04f9` scoped-mypy narrowing.

**Changed files (38):** 11 runtime modules (`youtab_runtime/`: approval, authorization_transport, effect_authorization, effect_evidence, effect_ledger, folder_grant, grant_fs, managed_fs_gate, managed_fs_router, managed_remote_executor, worker_lease), the seam (`agent/tool_executor.py`), 6 tool/env files (`tools/`: file_tools, terminal_tool, code_execution_tool, process_registry, tool_result_storage, environments/base), ADR-0005, and 19 test files.

## 3. Actual production call graph (verified)

```
model tool call ("write_file" | "patch" [ | "read_file"])
  → agent.tool_executor._run_agent_tool_execution_middleware
    → _authorized_dispatch(final_args)
      → youtab_runtime.managed_fs_router.route_managed_file_tool   ← wired here (before enforce_managed_tool_authority)
        → identity from the sealed AdmittedCommand (verify_proof; tenant/user/workspace)
        → backend dispatch: local → host-IO ; remote → MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE (fail closed)
        → resolve_within_grant (workspace-bound Folder Grant)  + compute_effect_digest
        → READ: grant read-perm → grant-bound handle-verified host-IO read
        → EFFECT: no signed authorization → record EffectProposal, ZERO side effect
                  signed EffectAuthorization → authorization_transport.correlate_proposal
                    → grant_fs.claim_granted_fs_effect (verify+single-use-consume + ledger claim + worker lease)
                    → grant-bound TOCTOU-safe host-IO (open_within_grant / unlink_within_grant / move_within_grant)
                    → grant_fs.settle_committed → workspace-bound canonical receipt (effect ledger)
```

Proven by `test_managed_fs_production_path.py` (write/read) and `test_managed_fs_production_matrix.py` (patch/delete/move + adversarial), both driving the **real** `_run_agent_tool_execution_middleware` — not a helper. The unrestricted shell backend (`ShellFileOperations._exec`) and the registered tool handler are asserted **never** called for a managed op; the standalone control proves those spies are wired.

## 4. Operation matrix (governed through the real seam)

| Op | Tool surface | Governed execution | Positive proof |
|---|---|---|---|
| read | `read_file` | grant read-perm + host-IO read | `test_managed_read_through_tool_executor` |
| write | `write_file` | signed-auth → claim/lease → host-IO write → receipt | `test_managed_write_through_tool_executor_authorized` (production-mode key) |
| patch | `patch` (replace) | signed-auth → grant read+write → apply → receipt | `test_patch_replace_authorized_matrix` |
| delete | `patch` V4A `Delete File` | signed-auth → `unlink_within_grant` → receipt | `test_v4a_delete_authorized_matrix` |
| move | `patch` V4A `Move File` | signed-auth → `move_within_grant` → receipt | `test_v4a_move_authorized_matrix` |

Fail-closed by design (documented, ADR-0005 §7 / router): V4A `ADD`/`UPDATE`(hunks) and multi-operation V4A in managed mode (use `write_file` or replace-mode `patch`); a managed op never falls back to the ungated shell.

## 5. Negative / adversarial matrix (each: refused, filesystem untouched, no committed receipt)

`test_managed_fs_production_matrix.py` + `test_managed_fs_production_path.py`, through the real middleware: missing authorization · expired authorization · replayed/copied (consumed) authorization · wrong workspace · wrong tenant · wrong principal · changed patch/content after authorization · changed move destination after authorization · path traversal (`../escape.txt`) · move source-inside/destination-outside · **real junction parent-swap escape** (reparse supported on this host; the out-of-grant file was not modified) · unsupported remote backend (`MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE`) · **crash-after-effect → effect left `unknown`** (non-terminal, never committed, never blind-replayed) · foreign-workspace receipt lookup returns not-found. Cross-process negatives (forged/expired/replayed/cross-tenant/missing grant, junction swap) also in `test_managed_fs_subprocess.py`.

## 6. Receipt / idempotency / reconciliation evidence

- **Receipt:** every committed effect yields a workspace-bound canonical ledger record (`get_effect_in_workspace` → state `committed`) carrying `effect_id`, `workspace_id`, `authorization_id`, `key_id`, `issuer`.
- **Idempotency / at-most-once:** replay of the same authorization executes exactly once — the single-use authorization id (`consumed_authorizations`) + the canonical effect claim make the second attempt a no-op; asserted in `test_replay_*` (in-process, production-path, matrix, and cross-process).
- **Workspace binding:** `get_effect_in_workspace`/`list_effects_in_workspace` return not-found for a foreign workspace even for the same tenant+user.
- **Reconciliation:** a host-IO failure after the claim settles the effect `unknown` (non-terminal) — proven by `test_crash_after_effect_left_unknown` and `test_move_destination_outside_grant_fails_closed`. Evidence-bound `reconcile_to_terminal` (non-forgeable verifier, LIVE=signed+txn / REFERENCE=recompute, never promotable) in `effect_evidence.py` + `worker_lease.py`.
- **Internal spill store (ADR-0005 §7a):** the tool-result overflow writer is an allow-listed internal-plane writer — per-run-scoped (opaque `session_scope`), private (`umask 077`, 0700/0600), symlink-hardened, bounded (`MAX_SPILL_BYTES`), containment-enforced, cleaned up on env teardown (`test_result_store_cleanup_lifecycle.py`).

## 7. Qualification (Lane-1 targeted; Master runs the heavy suite)

| Gate | Command | Result |
|---|---|---|
| pytest collect | `py -3 -m pytest <22 Lane-1 files> --collect-only -q` | **266 collected** |
| pytest run | `py -3 -m pytest <22 Lane-1 files> -p no:cacheprovider -q` | **266 passed / 0 failed / 0 skipped** (exit 0) |
| ruff | `py -3 -m ruff check <changed .py>` | All checks passed |
| py_compile | `py -3 -m py_compile <changed modules>` | OK |
| scoped mypy | `py -3 -m mypy --ignore-missing-imports --follow-imports=skip <11 runtime modules>` | **0 issues** |
| git diff --check | `git diff --check c92069a5a..HEAD` | clean |
| secret / machine-path scan | grep over changed files | clean |
| attribution scan | grep over commit messages | `NO_ATTRIBUTION_CLEAN`, all author=Eiman |
| orphan-process check | Win32_Process filter for worker scripts | **0** |

Note: full-repo mypy chases imports into pre-existing unrelated files (e.g. a syntax error in `tools/tts_tool.py`) and is out of Lane-1 scope; the scoped run above is authoritative for the delivered modules.

## 8. Explicit limitations & cross-repository dependencies

1. **LIVE Simorgh / Gateway issuance is NOT claimed.** The Runtime *receiver* (verify / correlate / single-use-consume) is complete and proven with an ephemeral, production-mode (`production=True`) Ed25519 key. Actual LIVE issuance of signed `EffectAuthorization`s is a cross-repository dependency with **no approved Simorgh SHA**; the production path remains fail-closed until one exists. No Runtime signer is introduced.
2. **Remote / container / SSH / modal backends** fail closed (`MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE`); the typed `ManagedRemoteFileExecutor` contract exists but a sandbox-side enforcement adapter does not. Host-handle containment is claimed for the **local** backend only.
3. **`patch` scope:** replace-mode + single-operation V4A `Delete`/`Move` are governed; V4A `ADD`/`UPDATE`(hunks) and multi-op V4A are fail-closed in managed mode (documented).
4. **`search_files`** is not yet routed (treated as a read at the authority gate); grant-scoping managed multi-file search is a follow-up.
5. Windows-only host for this run; junction-based reparse tests executed the real path.

## 9. Verdict

**`LANE 1 READY FOR MASTER INTEGRATION`** for local managed filesystem execution: the registered `read_file`/`write_file`/`patch` tools are governed through the real `tool_executor` seam with signed-authorization, grant, workspace, idempotency, lease, receipt and reconciliation enforcement, proven end-to-end (in-process, real-middleware and cross-process) with a full adversarial matrix. Remote backends and LIVE Simorgh issuance remain explicitly fail-closed pending an approved cross-repo SHA.
