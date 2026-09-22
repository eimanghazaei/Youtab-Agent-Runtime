# Runtime Lane 1 — Managed Filesystem Effects — Delivery Report v1.0

> **v1.1 correction (this revision):** the classification below is corrected to
> `LANE 1 LOCAL MANAGED FILE EXECUTION VERIFIED_NOT_REVIEWED`; the overall product
> is **NO-GO**. See §10 (v1.1 corrections) for the supported-Python (3.12)
> qualification, the corrected pre-execution-rejection vs UNKNOWN state table, the
> Lane-1/Lane-2/Lane-3 delta + conflict analysis, and the honest limitation list.
> The independent verifier checklist is `docs/evidence/RUNTIME_LANE1_VERIFIER_CHECKLIST_v1.0.md`.

**Verdict:** `LANE 1 LOCAL MANAGED FILE EXECUTION VERIFIED_NOT_REVIEWED` for the
local backend. **Overall product: NO-GO** (LIVE Simorgh/Gateway issuance absent;
remote/container authority absent; V4A ADD/UPDATE-hunks + multi-op unsupported;
`search_files` not grant-routed; no integrated Lane1+Lane2+Lane3 exact-SHA suite).
Nothing pushed.

## 1. Exact-SHA manifest

| | SHA |
|---|---|
| Repository | `github.com/eimanghazaei/Youtab-Agent-Runtime` (PRIVATE) |
| Branch | `delivery/runtime-folder-grant` (worktree `C:\Users\<user>\worktrees\rt-lane1-delivery`) |
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

**`LANE 1 LOCAL MANAGED FILE EXECUTION VERIFIED_NOT_REVIEWED`** for the local backend: the registered `read_file`/`write_file`/`patch` tools are governed through the real `tool_executor` seam with signed-authorization, grant, workspace, idempotency, lease, receipt and reconciliation enforcement, proven end-to-end (in-process, real-middleware and cross-process) with a full adversarial matrix. **Overall product remains NO-GO** — see §8 and §10. Remote backends and LIVE Simorgh issuance remain explicitly fail-closed pending an approved cross-repo SHA; no integrated Lane1+Lane2+Lane3 exact-SHA suite exists yet.

---

## 10. v1.1 corrections (independent-review preparation)

### 10.1 Supported-Python (3.12) qualification

Environment facts (mechanically recorded):
- `py -3 --version` → **Python 3.14.5**, executable `C:\Users\<user>\AppData\Local\Python\pythoncore-3.14-64\python.exe` — **outside** `requires-python = ">=3.11,<3.14"`, so the 3.14 run in §7 is **diagnostic only**.
- `uv` → **not available** in this environment (`command not found`), so the exact `uv sync --frozen` env **cannot be created here**.
- `uv.lock` present, sha256 `0bad12260bbd349f7e640bc8c9911ec52049f135491d0c7a3c747327e4b10d27`.
- **`py -3.12` → Python 3.12.10** (within `requires-python`).

Supported-Python run (best available, honestly labelled — a pip-installed 3.12 venv, **not** `uv sync --frozen`, because uv is unavailable): interpreter 3.12.10; pytest 9.1.1, cryptography 50.0.1, pydantic 2.13.5 (+ pyyaml/psutil/python-dotenv/requests/ruff/mypy for the tool-importing tests).
- `py -3.12 -m pytest <22 Lane-1 files> --collect-only -q` → **266 collected**.
- `py -3.12 -m pytest <22 Lane-1 files> -p no:cacheprovider -q` → **266 passed / 0 failed / 0 skipped** (exit 0).
- `py -3.12 -m py_compile <13 runtime modules>` → OK (proves no 3.14-only syntax).
- `py -3.12 -m ruff check …` → clean · `py -3.12 -m mypy --ignore-missing-imports --follow-imports=skip …` → 0 issues.

Residual: the **lockfile-frozen** (`uv sync --frozen`) supported-Python run must be executed by Master/CI in an environment with `uv`; this environment could not create it.

### 10.2 Corrected pre-execution rejection vs UNKNOWN state table

Pre-execution validation (operation shape, source+destination grant containment, tenant/principal/workspace binding, authorization signature/expiry/digest, content/patch/src/dst digest, backend support, traversal/junction at the boundary) now happens **before** any `begin_effect`/claim/host-IO. Every pre-execution rejection produces **zero filesystem mutation and zero effect-ledger row** (a proposal/rejection audit row may exist separately in `pending_proposals`; it is never an effect). `UNKNOWN` is reserved for a failure **during the mutation phase** (after claim + single-use consume), where the external outcome is indeterminate.

| Case | Classification | Effect-ledger row |
|---|---|---|
| destination outside grant (move) | **pre-execution reject** | none |
| traversal | pre-execution reject | none |
| real junction/reparse escape | pre-execution reject | none |
| missing / expired / wrong-workspace / wrong-tenant / wrong-principal auth | pre-execution reject | none (claim's consume precedes `begin_effect`) |
| changed content / patch not applicable | pre-execution reject | none |
| changed source/destination after auth | pre-execution reject | none |
| unsupported remote backend | pre-execution reject | none |
| unsupported V4A shape (ADD/UPDATE-hunks/multi-op) | pre-execution reject | none |
| **crash during the actual mutation (post-claim host-IO raises)** | **UNKNOWN → reconciliation** | one, non-terminal `unknown` |

Proven in `test_managed_fs_production_matrix.py` (`test_move_destination_outside_grant_fails_closed` now asserts **zero** effect rows; `test_crash_after_effect_left_unknown` injects a mutation-phase host-IO failure via `_host_write` and asserts exactly one `unknown` effect, never committed, never blind-replayed).

### 10.3 Positive filesystem-result + receipt assertions

Through the real middleware: write → exact bytes persisted; patch replace → exact resulting bytes; delete → target absent; move → source absent + destination present with exact bytes; receipt carries operation, canonical target path, workspace, authorization id/key/issuer and final state `committed`; the canonical ledger record has the correct state + a bound target-scope digest and is **unreadable to a foreign principal or a foreign workspace** (`test_receipt_fields_and_foreign_principal_unreadable`); replay performs no second mutation.

**Precise idempotency claim:** ledger/idempotency-enforced **at-most-once execution** for the supported local backend (single-use authorization id + canonical effect claim), with **reconciliation** for indeterminate (`unknown`) outcomes — not a global exactly-once guarantee.

### 10.4 Lane-1 / Lane-2 / Lane-3 / main delta + read-only conflict analysis

All SHAs full 40-char. Lane-1 HEAD at analysis time = `f5895a799a1f42c0c9f768cba9af682a79779463` (the report commit is the branch HEAD after this file lands).

- Lane-2 final `1ed19f0914cce6d074aae6cf378d81f05bc6d283`; Lane-2-consumed Lane-1 base `ca89219ae0925767f306ac9874c540813e0fd34e`.
- `git merge-base(Lane1, Lane2)` = `ca89219ae0925767f306ac9874c540813e0fd34e`.
- Lane-1 delta `ca89219ae..Lane1` = **32 files**; Lane-2 delta `ca89219ae..Lane2` = **31 files**.
- **Overlapping files (Lane1 ∩ Lane2) = 0.** `git merge-tree ca89219ae Lane1 Lane2` conflict markers = **0** → disjoint, conflict-free.
- Lane-3 `d877b373c`: `git merge-base(Lane1, Lane3)` = `c92069a5a` (shared PX base); overlap Lane1 ∩ Lane3 = **0**; `git merge-tree c92069a5a Lane1 Lane3` conflicts = **0**.
- `origin/main` `c7650a1b920224283ba3a59ca054d6625e07a5f3`: `git merge-base(Lane1, main)` = **NONE** (no common ancestor reachable in this local object DB) → main conflict analysis **could not be computed here**; Master must run it against a full-history main.
- **Recommended additive integration order:** Lane-1 → Lane-2 → Lane-3. Rationale: file-disjoint and conflict-free at the shared base, so order is not conflict-driven; Lane-1 first because it establishes the managed-fs governance the generic connector (Lane-2) can reuse, then Lane-3 packaging. No merge/cherry-pick performed — read-only analysis only.

### 10.5 Full-SHA changed-file inventory

38 files changed `c92069a5a..HEAD` (see §2 for the module breakdown): 11 `youtab_runtime/` modules, `agent/tool_executor.py`, 6 `tools/`+`tools/environments/` files, `docs/architecture/ADR-0005…`, `docs/contracts/EFFECT_AUTHORIZATION_COMPAT_v1.json`, this report, and 19 test files.

### 10.6 Remaining unsupported / NO-GO matrix (not hidden)

1. LIVE Simorgh/Gateway signed-`EffectAuthorization` issuance — **absent, no approved SHA**; production fail-closed. Runtime receiver done + proven with an ephemeral production-mode key; no Runtime signer.
2. Remote/container/SSH/modal filesystem authority — **absent**; fail-closed (`MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE`); host-handle containment claimed local-only.
3. `patch` V4A `ADD`/`UPDATE`-hunks and multi-op — **unsupported** in managed mode (fail-closed; use `write_file`/replace-mode `patch`).
4. `search_files` — **not grant-routed** (read at the authority gate).
5. **No integrated Lane1+Lane2+Lane3 exact-SHA suite** exists.
6. Lockfile-frozen (`uv sync --frozen`) supported-Python run pending an env with `uv`.

---

## 11. v1.2 corrections (authoritative closure)

### 11.1 Authoritative lockfile-derived qualification
- `uv==0.8.17` installed; `uv sync --frozen --extra dev` (from committed `uv.lock`, sha256 `0bad12260bbd349f7e640bc8c9911ec52049f135491d0c7a3c747327e4b10d27`) into a Python **3.12.10** `.venv` (project `youtab-agent-runtime==0.20.0`). Dev-pinned tools: **pytest 9.0.3**, **ruff 0.15.10**, **ty 0.0.21** (the project's configured type checker — not mypy).
- `python -m pytest <22 Lane-1 files>` → **collected & passed** (exit 0). `ruff check` clean · `py_compile` (3.12) OK · `ty check <runtime modules>` clean · `git diff --check` clean. This supersedes the §10.1 pip-venv run as the authoritative environment. Exact final count in §11.6.

### 11.2 Corrected Lane-3 SHA
- The Lane-3 SHA in §10.4 (`d877b373c48103d24fea94be4d32cf2390c544b0`) was a **stale earlier packaging-round HEAD**. The **real frozen Lane-3** delivery is `474eac31f8c83c9939d1d2d6f5450260dcf43dec` (subject: bounded Electron E2E teardown; a later commit). Neither is an ancestor of the other; their common base is `8ac1c5e72cf31f0190ce5e7d5676cca7239637d7`.
- Re-analysis against the correct SHA: `git merge-base(Lane1, 474eac31f…)` = `c92069a5a…` (shared PX base); **Lane1 ∩ Lane3 overlap = 0 files; `git merge-tree` conflicts = 0** — disjoint, conflict-free.

### 11.3 Disconnected-main history — root cause + resolution
- Root cause: the local clone was **shallow** (`git rev-parse --is-shallow-repository` = true), so the commits connecting Lane-1 to `origin/main` were absent → the earlier "no common ancestor" was a **shallow-clone artifact, not a genuine history split**. No grafts, no replace-refs.
- Resolution: `git fetch --unshallow` (bounded) → repo full (`is-shallow` = false). Then `git merge-base(Lane1, origin/main c7650a1b9…)` = `13f79aa6caae907af16c6ce87012021671319952`. **Lane1 ∩ main overlap = 0 files; merge-tree conflicts = 0** (main moved only 3 files since the base — website logo image uploads).
- Migration note: the frozen PX base `c92069a5a…` is **not** an ancestor of `origin/main`; Lane-1's base line has not been integrated to main. Lane-1 is textually conflict-free with main, but the Master Integrator must bring the PX baseline forward (a separate track) rather than merge Lane-1 directly onto main. No `--allow-unrelated-histories`, rebase, merge or rewrite was performed.

### 11.4 Lane-1 ↔ Lane-2 semantic compatibility (beyond file overlap)
Full matrix: `docs/evidence/RUNTIME_LANE1_LANE2_COMPAT_MATRIX_v1.0.md`. Lane-2's `enterprise/*` connector consumes the shared contracts Lane-1 changed. **9 of 10 surfaces COMPATIBLE** (additive `operation` widening, unchanged approval/ledger/lease signatures, new-alongside-old workspace lookups). **1 surface ADAPTER_REQUIRED/BLOCKING:** Lane-2 reconciliation uses the **removed** `worker_lease.EffectEvidence` + the old `reconcile_to_terminal`; Lane-1's non-forgeable-evidence correction requires `effect_evidence.ReconciliationEvidence`. Master must adapt the Lane-2 callsite (bounded, Lane-2-only, no Lane-1 relaxation). This is the concrete integration gate a zero-conflict check misses.

### 11.5 UNKNOWN semantics at the mutation boundary (A vs B)
- **A — deterministic failure BEFORE mutation** (e.g. unsigned write, destination outside grant, not-applicable patch): zero mutation, zero receipt, **zero effect-ledger row** — never an ambiguous UNKNOWN.
- **B — mutation SUCCEEDS then failure before settlement**: the router now downgrades a settlement failure to `settle_unknown` (never reports committed without a recorded receipt). Proven for **write** (`test_write_mutation_then_settle_crash_is_unknown`) and **delete** (`test_delete_mutation_then_settle_crash_is_unknown`): the bytes/absence prove the mutation occurred, the effect is `unknown`, a retry with the same single-use authorization does not repeat, reconciliation remains required.

### 11.6 `search_files` security decision + final counts
- `search_files` **was exposed** in managed mode (registered `side_effect_class="read"` → admitted ungoverned by the authority gate) — a filename/content disclosure path. It is now **fail-closed in managed mode** at the router (`test_managed_search_files_fails_closed`), inert in standalone (`test_standalone_search_files_passthrough`). Recorded as explicitly unsupported until grant/workspace-scoped.
- Final authoritative counts (uv-frozen 3.12): see §11.1 command; exact `collected`/`passed` recorded in the handoff.
