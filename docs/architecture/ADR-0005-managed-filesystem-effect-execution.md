# ADR-0005 — Managed filesystem effect execution (Folder Grant + signed effect authorization)

**Status:** Accepted (2026-09-22) — governs the authorized-effect *execution* path for managed filesystem tools
**Scope:** Youtab-Agent-Runtime — how a managed run's file read/write/patch/delete/move tools are authorized, ledgered and receipted
**Supersedes:** nothing · **Related:** `ADR-0002-simorgh-admission-execution-contract.md` (DP3 dispatch seam, DP4 effect round-trip), `ADR-0003-extensible-tool-registry.md`

> This ADR does **not** change ADR-0002. It is the concrete realization, for the *filesystem* capability, of ADR-0002's DP4 (Option 4b — effect authorization arrives as fresh signed authority) and DP3 (the single tool-dispatch authority seam). It records one architectural decision — the enforcement boundary for a **shell-backed** filesystem — and the fail-closed rollout. No merge/deploy/benchmark is authorized by this document.

---

## Context

ADR-0002 established that `AuthorityBoundary.decide_tool` (`youtab_runtime/policy.py:207`) **refuses to self-authorize any external WRITE**: it returns `execute_in_runtime=False` and an `EffectProposal`, never a positive decision. That default-deny is retained and is **not** relaxed by this ADR. What was missing is the *other half* of DP4: once the Brain returns authorization for that proposal, there was **no execution path** — the fs primitives (`youtab_runtime/grant_fs.py`, `managed_fs_gate.py`) had zero production callsites, and a managed `write_file`/`patch` was simply blocked with the proposal discarded.

Two facts about the real production path shape the decision:

1. **The single managed authority seam is `agent/tool_executor.py::enforce_managed_tool_authority`** (`:354`), called immediately before every managed tool invocation, holding the sealed `AdmittedCommand`, already fail-closed when admission is absent. This is the DP3 chokepoint for the fs capability.
2. **Every production filesystem mutation is shell-backed.** `tools/file_tools.py` (`write_file_tool`, `patch_tool`, `read_file_tool`) → `_get_file_ops(task_id)` → `tools/file_operations.py::ShellFileOperations`, which performs the actual IO by `terminal_env.execute(<shell command>)` against the configured backend — **local host shell OR a remote docker/modal/ssh/daytona sandbox**. The runtime does **not** `os.open` the target file on the production path.

Fact (2) is decisive: the host-side `os.open` + `O_NOFOLLOW` + device/inode handle-identity + parent-reparse TOCTOU protections in `grant_fs.open_within_grant` only bind when *the runtime itself opens the file on the host filesystem*. For a remote sandbox they cannot apply — the file lives in another OS namespace the runtime cannot open.

## Decision

Adopt a **capability-governance layer** enforced at the `enforce_managed_tool_authority` seam for every managed filesystem tool, and a **local host-IO execution path** for the case where the runtime genuinely performs the write itself.

### 1. Retained default-deny (unchanged)

`decide_tool` continues to refuse WRITE/PATCH/DELETE/MOVE self-authorization and emit an `EffectProposal`. Nothing in this ADR flips it to allow. READ (`EffectClass.READ`/`NONE`) continues to be admissible by the task contract — but see (3): managed READ additionally requires a workspace-bound Folder Grant.

### 2. The two-stage state machine (ADR-0002 DP4-4b, concretized)

For a managed filesystem tool call:

1. admitted `AdmittedCommand` present (else fail closed — no standalone fallback);
2. `decide_tool` yields an `EffectProposal` for the effectful op;
3. Simorgh evaluates the proposal (out of band, over the run-events cursor);
4. Simorgh returns a **signed `EffectAuthorization`** (`youtab_runtime/effect_authorization.py`) — Ed25519, `key_id`-selected, binding issuer, tenant, principal, canonical workspace, the grant `command_id`, capability + operation, the normalized effect digest (operation + canonical path + workspace + content digest), expiry and a single-use nonce;
5. the Runtime **verifies** issuer/key/tenant/principal/workspace/command/capability/operation/effect-digest/expiry/nonce and **consumes** it exactly once (`approval.reserve_and_consume_authorization`);
6. the Runtime **claims the canonical effect** (idempotency identity + worker lease) in the effect ledger *before* the filesystem is touched;
7. the worker performs the operation;
8. the Runtime **settles** committed / unknown / reconciliation-required;
9. the Runtime persists a **workspace-bound receipt**.

Unsigned, expired, mismatched, cross-workspace, cross-principal, replayed or unsolicited authorization fails closed. **The Runtime never mints or self-approves production authority** (`resolve_authority_public_key` refuses a test-prefixed key when `production=True`).

### 3. Managed READ requires a Folder Grant

A managed READ is admitted by the task contract only if the requested path resolves **within a workspace-bound Folder Grant** with read permission (`folder_grant.resolve_within_grant` — pure path algebra, fail-closed on escape/no-grant). READ needs no signed effect authorization (it is not an external effect) but it is no longer ungoverned: a path outside the grant is refused.

### 4. Enforcement boundary — shell-backed vs. runtime host-IO (the crux)

Because production IO is shell-backed (Context fact 2), enforcement is layered:

- **Governance layer (all backends, always):** grant authorization + signed-authorization verify/consume + canonical effect claim (idempotency) + worker lease + workspace-bound receipt + settle/reconcile, applied at the `enforce_managed_tool_authority` seam with the requested path **grant-validated before** the tool's shell command runs. This layer is backend-independent and is what makes a managed fs effect non-bypassable, at-most-once and auditable.
- **Local host-IO execution path (`grant_fs.open_within_grant`):** when the effect is executed by the runtime itself against the **host** filesystem, the strong TOCTOU guarantees apply — re-resolve, `O_NOFOLLOW`, parent-reparse (symlink/junction) validation pre+post open, and device/inode handle-identity confirmation (fail closed if unverifiable). On Windows the final containment is proven from the **opened handle's** final path, not a second path-string `resolve()`.
- **Remote-sandbox containment (delegated, documented):** for a docker/modal/ssh/daytona backend the runtime cannot `os.open` the target; containment of the final byte-write is the sandbox's namespace boundary. The governance layer still binds the effect (grant path-authorization + ledger + receipt); the host-handle TOCTOU guarantee is explicitly **not claimed** for remote backends and is recorded as a delegated-trust boundary, not a silent gap.

### 5. Shell / terminal bypass policy

The terminal / arbitrary-shell tool carries an effectful `side_effect_class`; `decide_tool` therefore returns a proposal and `enforce_managed_tool_authority` **blocks it for managed runs**. There is no command-level filesystem authority contract yet, so **effectful shell execution is denied for managed runs — fail closed.** A managed run cannot mutate files by shelling around the fs tools. Lifting this requires a separate command-level signed authority + constrained-workspace sandbox policy (future ADR), not a relaxation here.

### 6. Rollback / production readiness

The authorized-execution path is **fail-closed until an approved Simorgh SHA issues signed `EffectAuthorization`s** cross-repo. The Runtime publishes the exact typed `EffectAuthorization` contract for Simorgh to implement; until then, and whenever no valid signed authorization is present, the managed WRITE/PATCH/DELETE/MOVE path denies (identical to today's behaviour), so shipping the wiring changes no production behaviour. A Runtime signer is **never** inserted as a substitute. Rollback = the path stays denied.

### 7. Disposition of the remaining direct-Python writer tools

A callsite inventory found that, besides the arbitrary external-fs / shell / code tools closed in (1)+(5) (`write_file`, `patch`, `terminal`, `execute_code`, `process` — now effect-classified and fail-closed in managed mode), several tools mutate the filesystem via direct Python (`Path.write_text`/`mkdir`/`shutil`) while registered `side_effect_class="none"`: `todo`, `kanban`, `skill`, `project`, `cron`, plus `image`/`checkpoint`. These split into two classes:

- **Agent control-plane state** (`todo`, `kanban`, `skill`, `project`, `cron`): the run's own orchestration/state, analogous to memory. Classifying them as an *external* `write` is semantically wrong and would break a managed run's own self-management. Like `memory_write`, they warrant their **own scoped-authority class governed by a grant scope** — a follow-up decision, not an external-effect block. They are recorded here as a deliberate scoping boundary, **not** left silently unclassified.
- **External artifacts** (`image` file output, `checkpoint` disk writes): candidates for the external-effect path; deferred pending confirmation of whether each targets a managed artifact store vs. an arbitrary path.

This ADR's scope is the arbitrary-external-fs / shell / code bypass (closed). The control-plane class is a named follow-up, so no managed writer is both unclassified and unbounded.

#### 7a. Allow-listed internal-plane writer: the tool-result overflow store

`tools/tool_result_storage.py` (`maybe_persist_tool_result` → `_write_to_sandbox`) writes to the backend filesystem during a managed run via `env.execute("mkdir -p <root> && cat > <path>", stdin_data=…)`, outside the `managed_fs_router` / grant / signed-authorization / effect-ledger / receipt path. This is a **deliberate allow-listed exception**, not an ungoverned bypass, because it is **internal runtime infrastructure**, not a model-directed external effect:

- **What it writes:** a tool's *own* oversized output, spilled to disk so it does not overflow the context window (the model then reads it back through the governed `read_file`). It is result-caching, analogous to logging — the content originates from the runtime, not from a model-issued write request against user/workspace data.
- **Where — per-run scoped:** `{env.get_temp_dir()}/youtab-results/<run-scope>/`, where `<run-scope>` is an **opaque, Runtime-generated** id (the environment's own `_session_id`, bound to the run lifecycle; a generated fallback otherwise). It is never derived from tenant/user/model input, so no caller can select another run's scope, and two runs using the same `tool_use_id` cannot overwrite or read each other's results. The store is never placed under a user workspace grant.
- **Containment invariant (enforced + tested):** the only attacker-influenceable input to the path is the system `tool_use_id`, sanitized to a single filename component by `_safe_result_filename` (`[^A-Za-z0-9_.-]+`→`_`, strips leading/trailing `._-`, hash-suffix when altered). `_write_to_sandbox` **fails closed** (no write, caller inline-truncates) if the resolved leaf is empty, `.`/`..`, or contains a separator, so a crafted id can never escape the scope, nest, or traverse.
- **Shell-safe + private + symlink-hardened:** every infrastructure-controlled path is quoted with the canonical POSIX quoter and content travels only through stdin (never argv/interpolation); the write runs under a restrictive `umask 077` with an explicit `0700` spill dir and `0600` file, and the store root, spill dir and leaf are each rejected if they are a pre-existing symlink. This is shell-level protection for the sandbox filesystem — host-handle-level containment is **not** claimed for a remote backend; on a non-POSIX backend the command fails and the caller inline-truncates (fail closed).
- **Bounded + cleaned up:** a single result spill is capped at `MAX_SPILL_BYTES` (no unbounded write; `cat >` truncates, never appends); `cleanup_run_scope(env)` removes **only** `{store-root}/{run-scope}` — guarded (direct child of the store root, not a symlink) so a cleanup failure or hostile symlink can never delete user/workspace files — bound to the environment teardown, with the sandbox/OS temp reclamation as the abnormal-termination fallback.

Routing this through the signed-`EffectAuthorization` external-effect path is explicitly rejected: requiring Brain authorization to persist a tool's own output would break large-result handling for no security gain (the content is already in hand and the destination is a run-scoped runtime cache, outside every workspace grant — so the governed `read_file` cannot reach it and the effectful shell tools that could are already fail-closed for managed runs).

## Consequences

- **Positive:** the fs primitives gain a real, single, non-bypassable execution seam; managed READ becomes grant-scoped; at-most-once + workspace-bound receipts + evidence-bound reconciliation become the contract for fs effects; the shell/terminal bypass is closed fail-closed; nothing is weakened and no runtime authority is minted.
- **Negative / honest limits:** the strong host-handle TOCTOU guarantee binds only the local host-IO path; remote-sandbox containment is a delegated-trust boundary. Production remains inert until Simorgh signs — this ADR wires the Runtime side and the reference/test authority proves it end-to-end in-process and cross-process, but LIVE production issuance is a cross-repo dependency (ADR-0002 gating dependency, unchanged).
- **Risk:** mapping drift (a new fs tool with no effect-class entry) fails closed by the `EffectClass.WRITE` default in `enforce_managed_tool_authority`.

## Test strategy

- Runtime-side authorization transport: verify/consume matrix (unsigned, unknown issuer, wrong signer/key, changed workspace/operation/content-digest, expired, previously consumed, unsolicited, production-using-test-authority) — all fail closed.
- Real cross-process proof: `tests/youtab_runtime/test_managed_execution_subprocess.py` promoted to route a real fs tool through admission → gate → grant → signed authorization → ledger → receipt, with the negative matrix (missing/forged/revoked/expired grant or authorization, wrong workspace/tenant/principal, replay same/modified content, concurrent, crash-after-effect, foreign receipt, junction swap, shell-bypass attempt) asserting **both** filesystem state and ledger/receipt state, and zero side-effect + zero ledger row on a pre-admission rejection.
- Mutation checks: removing the grant check, the authorization verify/consume, the ledger claim, the workspace binding on receipt read, or the shell-deny must each turn the suite red.

---

*Grounded in: `youtab_runtime/policy.py:207-296`, `agent/tool_executor.py:354-426`, `tools/file_tools.py:961-1745`, `tools/file_operations.py:793-1394`, `youtab_runtime/grant_fs.py`, `youtab_runtime/managed_fs_gate.py`, `youtab_runtime/effect_authorization.py`, `youtab_runtime/approval.py`, `youtab_runtime/worker_lease.py`, `docs/architecture/ADR-0002-simorgh-admission-execution-contract.md`.*
