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
