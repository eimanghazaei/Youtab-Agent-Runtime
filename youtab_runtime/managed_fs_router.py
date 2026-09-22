"""Managed filesystem router — the production positive path at the tool_executor
authority seam.

When a managed agent invokes a registered file tool (`read_file`, `write_file`),
this router OWNS the outcome, implementing ADR-0005's two-stage state machine on
the actual production callsite (not a parallel test API):

  1. verify the admitted command identity (tenant / principal / workspace from the
     sealed grant, never from caller args);
  2. classify the tool + normalized operation;
  3. resolve the request within a workspace-bound Folder Grant and compute the
     canonical effect digest;
  4. **read** → require grant read permission and perform a grant-bound,
     handle-verified host-IO read;
  5. **write** (external effect) → if no signed authorization is attached, record
     the `EffectProposal` and perform ZERO side effect (the runtime never
     self-authorizes); if a signed `EffectAuthorization` is attached, correlate it
     to the proposal, verify + single-use-consume it, claim the canonical effect
     under a worker lease, perform the TOCTOU-safe host-IO write, and settle a
     workspace-bound receipt.

Backend dispatch is explicit: the strong host-`os.open` guarantees apply to the
**local** host filesystem only. For a remote / container / SSH / modal backend the
runtime cannot enforce containment host-side, so managed file operations fail
closed with `MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE` until a sandbox-side
enforcement adapter exists (`youtab_runtime.managed_remote_executor`).

Everything is read from per-run state the worker attaches to the agent
(`_admitted_command`, `_managed_workspace_root`, `_effect_authorizations`, …).
Absent state fails closed, so this changes no production behaviour until the
managed workspace root + signed authorizations are supplied — a Runtime signer is
never introduced.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from youtab_runtime import authorization_transport as _tx
from youtab_runtime import grant_fs
from youtab_runtime.admission import AdmittedCommand
from youtab_runtime.approval import (
    ApprovalError,
    compute_effect_digest,
    content_digest,
)
from youtab_runtime.contracts import EffectProposal
from youtab_runtime.effect_authorization import (
    EffectAuthorization,
    EffectAuthorizationError,
)
from youtab_runtime.folder_grant import FolderGrant, FolderGrantError, resolve_within_grant
from youtab_runtime.managed_remote_executor import (
    MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE,
)
from youtab_runtime.run_journal import Principal

__all__ = ["RouteOutcome", "route_managed_file_tool"]

WORKSPACE_UNSCOPED = "-"
_MAX_READ_BYTES = 8 * 1024 * 1024

# The registered file tools this router owns, and their normalized operation.
# delete/move are not registered tools (they occur only inside `patch` V4A), and
# `patch`/`search_files` are intentionally NOT routed yet — a managed `patch`
# therefore stays fail-closed at the authority gate (classified "write"), and
# `search_files` stays a read at the gate; both are recorded follow-ups.
_FILE_TOOL_OP = {"read_file": "read", "write_file": "write"}


@dataclass(frozen=True)
class RouteOutcome:
    """The router's decision for a managed file-tool invocation.

    ``blocked=True`` → the seam refuses the call with ``reason`` (zero side
    effect). ``blocked=False`` → the router performed the operation and ``result``
    is the JSON string to return in place of the real tool handler.
    """

    blocked: bool
    result: Optional[str] = None
    reason: Optional[str] = None
    effect_id: Optional[str] = None


def _blocked(reason: str) -> RouteOutcome:
    return RouteOutcome(blocked=True, reason=reason)


def _handled(result: dict, effect_id: Optional[str] = None) -> RouteOutcome:
    return RouteOutcome(
        blocked=False,
        result=json.dumps(result, ensure_ascii=False),
        effect_id=effect_id,
    )


def _is_local_backend(agent, task_id: str) -> bool:
    override = getattr(agent, "_managed_fs_backend", None)
    if override is not None:
        return str(override) == "local"
    try:
        from tools.file_tools import _terminal_env_type_for_task

        return _terminal_env_type_for_task(task_id) == "local"
    except Exception:
        # Unknown backend → not provably local → fail closed (treat as remote).
        return False


def _grant_for(admitted: AdmittedCommand, workspace_root, workspace_id: str,
               operation: str) -> FolderGrant:
    return FolderGrant(
        grant_id=admitted.envelope.command_id,
        tenant_id=admitted.envelope.tenant_id,
        principal_id=admitted.envelope.user_id,
        workspace_id=workspace_id,
        canonical_root=str(workspace_root),
        permissions=frozenset({operation}),
    )


def _find_authorization(
    authorizations: Optional[Any], effect_digest: str
) -> Optional[EffectAuthorization]:
    """Find an attached signed authorization whose effect digest matches. Accepts a
    mapping keyed by effect_digest or a sequence of EffectAuthorization."""
    if authorizations is None:
        return None
    if isinstance(authorizations, Mapping):
        candidate = authorizations.get(effect_digest)
        return candidate if isinstance(candidate, EffectAuthorization) else None
    if isinstance(authorizations, Sequence):
        for a in authorizations:
            if isinstance(a, EffectAuthorization) and a.effect_digest == effect_digest:
                return a
    return None


def _build_proposal(env, function_name: str, final_args: dict) -> EffectProposal:
    import hashlib

    args = json.dumps(
        final_args if isinstance(final_args, dict) else {},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(args).hexdigest()
    return EffectProposal(
        proposal_id=f"proposal-{digest[:24]}",
        command_id=env.command_id,
        task_id=env.task_id,
        tenant_id=env.tenant_id,
        trace_id=env.trace_id,
        effect_class="write",
        tool_name=function_name,
        arguments_digest=digest,
        reason="external filesystem effect requires Youtab Brain authorization",
    )


def _read_result(safe_path: str, data: bytes, final_args: dict) -> dict:
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    try:
        offset = max(1, int(final_args.get("offset", 1)))
    except (TypeError, ValueError):
        offset = 1
    try:
        limit = int(final_args.get("limit", len(lines)))
    except (TypeError, ValueError):
        limit = len(lines)
    window = lines[offset - 1: offset - 1 + max(0, limit)]
    numbered = "\n".join(f"{offset + i}|{ln}" for i, ln in enumerate(window))
    return {
        "status": "ok",
        "operation": "read",
        "resolved_path": safe_path,
        "content": numbered,
        "backend": "local-managed",
    }


def route_managed_file_tool(
    agent, function_name: str, final_args: Any, *, task_id: str,
    now: Optional[float] = None,
) -> Optional[RouteOutcome]:
    """Route a managed file-tool call. Returns ``None`` when it does not apply
    (non-file tool, or not a managed run) so the normal authority gate runs;
    otherwise the router owns the outcome (handled or blocked)."""
    operation = _FILE_TOOL_OP.get(function_name)
    if operation is None:
        return None
    try:
        from youtab_runtime import managed_execution as mx
    except Exception:  # authority core unavailable → do not gate standalone
        return None
    if mx.current_trust_mode() is not mx.TrustMode.MANAGED:
        return None

    admitted = getattr(agent, "_admitted_command", None)
    if not isinstance(admitted, AdmittedCommand):
        return _blocked(
            "managed filesystem operation reached the router without a Simorgh "
            "admission (admission is mandatory; no standalone fallback)"
        )
    admitted.verify_proof()
    env = admitted.envelope
    principal = Principal(env.tenant_id, env.user_id)
    workspace_id = getattr(env, "workspace_id", WORKSPACE_UNSCOPED) or WORKSPACE_UNSCOPED
    now = time.time() if now is None else now

    # Backend dispatch: remote/container backends fail closed (no host-side
    # containment) until a sandbox-side enforcement adapter exists.
    if not _is_local_backend(agent, task_id):
        return _blocked(MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE)

    workspace_root = getattr(agent, "_managed_workspace_root", None)
    if not workspace_root:
        return _blocked(
            "managed workspace root is not bound for this run; refusing local "
            "filesystem operation (fail closed)"
        )

    if not isinstance(final_args, dict) or not final_args.get("path"):
        return _blocked("managed filesystem operation requires a 'path' argument")
    requested_path = str(final_args["path"])

    db_path = getattr(agent, "_managed_effects_db", None)
    if db_path is not None:
        db_path = Path(db_path)

    # ── READ: grant read permission + grant-bound host-IO read ────────────────
    if operation == "read":
        grant = _grant_for(admitted, workspace_root, workspace_id, "read")
        try:
            fd = grant_fs.open_within_grant(
                grant, requested_path, operation="read",
                principal=principal, workspace_id=workspace_id, now=now,
            )
        except FolderGrantError as exc:
            return _blocked(f"managed read refused: {type(exc).__name__}")
        try:
            data = os.read(fd, _MAX_READ_BYTES)
        finally:
            os.close(fd)
        return _handled(_read_result(requested_path, data, final_args))

    # ── WRITE: external effect — proposal unless a signed authorization exists ─
    content = str(final_args.get("content", "")).encode("utf-8")
    grant = _grant_for(admitted, workspace_root, workspace_id, "write")
    try:
        safe_path = resolve_within_grant(
            grant, requested_path, operation="write",
            tenant_id=principal.tenant, principal_id=principal.user,
            workspace_id=workspace_id, now=now,
        )
    except FolderGrantError as exc:
        return _blocked(f"managed write refused: {type(exc).__name__}")

    effect_digest = compute_effect_digest(
        "write", safe_path, workspace_id, content_digest(content)
    )
    authorization = _find_authorization(
        getattr(agent, "_effect_authorizations", None), effect_digest
    )
    if authorization is None:
        # Runtime never self-authorizes: record the proposal, do nothing else.
        proposal = _build_proposal(env, function_name, final_args)
        _tx.record_proposal(
            proposal, authorization_epoch=int(getattr(env, "authorization_epoch", 1)),
            db_path=db_path,
        )
        return _blocked(
            "external filesystem effect requires a Brain-signed EffectAuthorization "
            "(proposal emitted; zero side effect)"
        )

    production = bool(getattr(agent, "_managed_authority_production", True))
    prod_keys = getattr(agent, "_managed_authority_public_keys", None)
    test_keys = getattr(agent, "_managed_test_authority_keys", None)

    # 1) correlate the authorization to its pending proposal (no consume here).
    try:
        _tx.correlate_proposal(authorization, principal=principal, db_path=db_path)
    except _tx.AuthorizationTransportError as exc:
        return _blocked(f"authorization correlation failed: {type(exc).__name__}")

    # 2) verify + single-use-consume + claim the canonical effect under a lease.
    owner_token = str(getattr(agent, "_managed_lease_token", None) or f"lease-{env.command_id}")
    try:
        effect = grant_fs.claim_granted_fs_effect(
            grant, requested_path, operation="write", run_id=task_id,
            principal=principal, workspace_id=workspace_id, authorization=authorization,
            owner_token=owner_token, content=content, production=production,
            authority_public_keys=prod_keys, test_authority_keys=test_keys,
            db_path=db_path, now=now,
        )
    except (ApprovalError, EffectAuthorizationError, FolderGrantError) as exc:
        return _blocked(f"authorization rejected: {type(exc).__name__}")

    if not effect.won:
        # Replay / already-in-progress: at-most-once — no second write.
        return _handled({
            "status": "noop",
            "operation": "write",
            "resolved_path": effect.safe_path,
            "state": effect.state,
            "detail": "effect already claimed; idempotent no-op (no re-write)",
        }, effect_id=effect.effect_id)

    # 3) perform the TOCTOU-safe host-IO write, then settle a workspace-bound receipt.
    try:
        fd = grant_fs.open_within_grant(
            grant, requested_path, operation="write",
            principal=principal, workspace_id=workspace_id, now=now,
        )
        try:
            os.ftruncate(fd, 0)
            written = os.write(fd, content)
        finally:
            os.close(fd)
    except OSError as exc:
        # The effect was claimed but the write did not prove out → mark UNKNOWN
        # for reconciliation rather than committing an unproven side effect.
        grant_fs.settle_unknown(effect, principal, workspace_id=workspace_id, db_path=db_path)
        return _blocked(f"managed write failed after claim (left for reconciliation): {type(exc).__name__}")

    state = grant_fs.settle_committed(
        effect, principal, workspace_id=workspace_id, db_path=db_path
    )
    _tx.mark_proposal_consumed(authorization.proposal_id, db_path=db_path) if authorization.proposal_id else None
    return _handled({
        "status": "ok",
        "operation": "write",
        "resolved_path": effect.safe_path,
        "bytes_written": written,
        "backend": "local-managed",
        "receipt": {
            "effect_id": effect.effect_id,
            "state": state,
            "workspace_id": workspace_id,
            "authorization_id": authorization.authorization_id,
            "key_id": authorization.key_id,
            "issuer": authorization.issuer,
        },
    }, effect_id=effect.effect_id)
