"""Managed filesystem router — the production positive path at the tool_executor
authority seam.

When a managed agent invokes a registered file tool (``read_file``,
``write_file``, ``patch``), this router OWNS the outcome, implementing ADR-0005's
two-stage state machine on the actual production callsite (not a parallel API):

  1. verify the admitted command identity (tenant / principal / workspace from the
     sealed grant, never from caller args);
  2. classify the tool + normalized operation;
  3. resolve the request within a workspace-bound Folder Grant and compute the
     canonical effect digest;
  4. **read** → grant read permission + a grant-bound, handle-verified host-IO read;
  5. **effectful** (write / patch-replace / V4A add / delete / move) → if no signed
     authorization is attached, record the ``EffectProposal`` and perform ZERO side
     effect; if a signed ``EffectAuthorization`` is attached, correlate it to the
     proposal, verify + single-use-consume it, claim the canonical effect under a
     worker lease, perform the TOCTOU-safe host-IO operation, and settle a
     workspace-bound receipt.

Backend dispatch is explicit: the strong host-``os.open`` guarantees apply to the
**local** host filesystem only. For a remote / container / SSH / modal backend the
runtime cannot enforce containment host-side, so managed file operations fail
closed with ``MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE`` until a
sandbox-side enforcement adapter exists.

Scope notes: ``delete`` and ``move`` are not registered tools — they are issued
through ``patch`` V4A headers, so they are governed here as single V4A operations.
V4A ``UPDATE`` (hunk application) and multi-operation V4A patches are fail-closed in
managed mode (the model can use ``write_file`` or a replace-mode ``patch``); a
managed operation is NEVER allowed to fall back to the ungated shell backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

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

# Registered file tools this router owns, and their normalized operation.
# ``search_files`` is intentionally NOT routed yet (a read at the gate); ``patch``
# decomposes into governed write/delete/move effects.
_FILE_TOOL_OP = {"read_file": "read", "write_file": "write", "patch": "patch"}


class _PatchError(Exception):
    """A managed patch could not be applied within the governed host-IO path."""


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


@dataclass
class _Ctx:
    agent: Any
    admitted: AdmittedCommand
    principal: Principal
    workspace_id: str
    workspace_root: str
    db_path: Optional[Path]
    production: bool
    prod_keys: Optional[Mapping[str, str]]
    test_keys: Optional[Mapping[str, str]]
    now: float
    task_id: str


def _blocked(reason: str) -> RouteOutcome:
    return RouteOutcome(blocked=True, reason=reason)


def _handled(result: dict, effect_id: Optional[str] = None) -> RouteOutcome:
    return RouteOutcome(
        blocked=False, result=json.dumps(result, ensure_ascii=False), effect_id=effect_id
    )


def _is_local_backend(agent, task_id: str) -> bool:
    override = getattr(agent, "_managed_fs_backend", None)
    if override is not None:
        return str(override) == "local"
    try:
        from tools.file_tools import _terminal_env_type_for_task

        return _terminal_env_type_for_task(task_id) == "local"
    except Exception:
        return False  # unknown backend → not provably local → fail closed


def _grant(ctx: _Ctx, permissions: set) -> FolderGrant:
    return FolderGrant(
        grant_id=ctx.admitted.envelope.command_id,
        tenant_id=ctx.admitted.envelope.tenant_id,
        principal_id=ctx.admitted.envelope.user_id,
        workspace_id=ctx.workspace_id,
        canonical_root=str(ctx.workspace_root),
        permissions=frozenset(permissions),
    )


def _find_authorization(authorizations: Optional[Any], effect_digest: str) -> Optional[EffectAuthorization]:
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


def _proposal_ref(descriptor: dict) -> Tuple[str, str]:
    args = json.dumps(descriptor, sort_keys=True, separators=(",", ":"), default=str).encode()
    rd = hashlib.sha256(args).hexdigest()
    return f"proposal-{rd[:24]}", rd


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
        "status": "ok", "operation": "read", "resolved_path": safe_path,
        "content": numbered, "backend": "local-managed",
    }


def _govern(
    ctx: _Ctx, *, grant: FolderGrant, requested_path: str, operation: str,
    digest_bytes: bytes, descriptor: dict,
) -> Tuple[Optional[RouteOutcome], Optional[grant_fs.GrantedFsEffect], Optional[EffectAuthorization]]:
    """Compute effect identity, require + correlate + single-use-consume a signed
    authorization, and atomically claim the canonical effect under a lease.

    Returns ``(outcome, None, None)`` when the caller must stop (proposal emitted /
    blocked / idempotent no-op), or ``(None, effect, auth)`` when the caller must
    perform the host-IO and then settle. No side effect occurs here.
    """
    try:
        safe_path = resolve_within_grant(
            grant, requested_path, operation=operation,
            tenant_id=ctx.principal.tenant, principal_id=ctx.principal.user,
            workspace_id=ctx.workspace_id, now=ctx.now,
        )
    except FolderGrantError as exc:
        return _blocked(f"managed {operation} refused: {type(exc).__name__}"), None, None

    effect_digest = compute_effect_digest(
        operation, safe_path, ctx.workspace_id, content_digest(digest_bytes)
    )
    auth = _find_authorization(getattr(ctx.agent, "_effect_authorizations", None), effect_digest)
    if auth is None:
        proposal_id, request_digest = _proposal_ref(descriptor)
        env = ctx.admitted.envelope
        proposal = EffectProposal(
            proposal_id=proposal_id, command_id=env.command_id, task_id=env.task_id,
            tenant_id=ctx.principal.tenant, trace_id=env.trace_id, effect_class="write",
            tool_name=f"fs:{operation}:{safe_path}"[:128], arguments_digest=request_digest,
            reason="external filesystem effect requires Youtab Brain authorization",
        )
        _tx.record_proposal(
            proposal, authorization_epoch=int(getattr(env, "authorization_epoch", 1)),
            db_path=ctx.db_path,
        )
        return _blocked(
            "external filesystem effect requires a Brain-signed EffectAuthorization "
            "(proposal emitted; zero side effect)"
        ), None, None

    try:
        _tx.correlate_proposal(auth, principal=ctx.principal, db_path=ctx.db_path)
    except _tx.AuthorizationTransportError as exc:
        return _blocked(f"authorization correlation failed: {type(exc).__name__}"), None, None

    owner_token = str(
        getattr(ctx.agent, "_managed_lease_token", None)
        or f"lease-{ctx.admitted.envelope.command_id}"
    )
    try:
        effect = grant_fs.claim_granted_fs_effect(
            grant, requested_path, operation=operation, run_id=ctx.task_id,
            principal=ctx.principal, workspace_id=ctx.workspace_id, authorization=auth,
            owner_token=owner_token, content=digest_bytes, production=ctx.production,
            authority_public_keys=ctx.prod_keys, test_authority_keys=ctx.test_keys,
            db_path=ctx.db_path, now=ctx.now,
        )
    except (ApprovalError, EffectAuthorizationError, FolderGrantError) as exc:
        return _blocked(f"authorization rejected: {type(exc).__name__}"), None, None

    if not effect.won:
        return _handled({
            "status": "noop", "operation": operation, "resolved_path": effect.safe_path,
            "state": effect.state, "detail": "effect already claimed; idempotent no-op (no re-execute)",
        }, effect_id=effect.effect_id), None, None

    return None, effect, auth


def _finish(ctx: _Ctx, effect, auth, extra: dict) -> RouteOutcome:
    state = grant_fs.settle_committed(
        effect, ctx.principal, workspace_id=ctx.workspace_id, db_path=ctx.db_path
    )
    if auth.proposal_id:
        _tx.mark_proposal_consumed(auth.proposal_id, db_path=ctx.db_path)
    body = {
        "status": "ok", "resolved_path": effect.safe_path, "backend": "local-managed",
        "receipt": {
            "effect_id": effect.effect_id, "state": state, "workspace_id": ctx.workspace_id,
            "authorization_id": auth.authorization_id, "key_id": auth.key_id,
            "issuer": auth.issuer,
        },
    }
    body.update(extra)
    return _handled(body, effect_id=effect.effect_id)


def _unknown(ctx: _Ctx, effect, exc: Exception, operation: str) -> RouteOutcome:
    grant_fs.settle_unknown(effect, ctx.principal, workspace_id=ctx.workspace_id, db_path=ctx.db_path)
    return _blocked(
        f"managed {operation} failed after claim (left for reconciliation): {type(exc).__name__}"
    )


# ── operation routers ────────────────────────────────────────────────────────


def _route_read(ctx: _Ctx, final_args: dict) -> RouteOutcome:
    requested_path = str(final_args["path"])
    grant = _grant(ctx, {"read"})
    try:
        fd = grant_fs.open_within_grant(
            grant, requested_path, operation="read",
            principal=ctx.principal, workspace_id=ctx.workspace_id, now=ctx.now,
        )
    except FolderGrantError as exc:
        return _blocked(f"managed read refused: {type(exc).__name__}")
    try:
        data = os.read(fd, _MAX_READ_BYTES)
    finally:
        os.close(fd)
    return _handled(_read_result(requested_path, data, final_args))


def _route_write(ctx: _Ctx, final_args: dict) -> RouteOutcome:
    requested_path = str(final_args["path"])
    content = str(final_args.get("content", "")).encode("utf-8")
    grant = _grant(ctx, {"write"})
    outcome, effect, auth = _govern(
        ctx, grant=grant, requested_path=requested_path, operation="write",
        digest_bytes=content, descriptor=dict(final_args),
    )
    if outcome is not None:
        return outcome
    written = _host_write(ctx, grant, requested_path, content)
    if isinstance(written, RouteOutcome):
        return _unknown(ctx, effect, OSError(written.reason or ""), "write")
    return _finish(ctx, effect, auth, {"operation": "write", "bytes_written": written})


def _host_write(ctx: _Ctx, grant, requested_path, content) -> Any:
    try:
        fd = grant_fs.open_within_grant(
            grant, requested_path, operation="write",
            principal=ctx.principal, workspace_id=ctx.workspace_id, now=ctx.now,
        )
        try:
            os.ftruncate(fd, 0)
            return os.write(fd, content)
        finally:
            os.close(fd)
    except OSError as exc:
        return _blocked(str(exc))


def _route_patch(ctx: _Ctx, final_args: dict) -> RouteOutcome:
    mode = str(final_args.get("mode", "replace"))
    if mode == "replace":
        return _route_patch_replace(ctx, final_args)
    if mode == "patch":
        return _route_patch_v4a(ctx, final_args)
    return _blocked(f"unsupported managed patch mode {mode!r}")


def _route_patch_replace(ctx: _Ctx, final_args: dict) -> RouteOutcome:
    requested_path = str(final_args.get("path") or "")
    if not requested_path:
        return _blocked("managed patch requires a 'path'")
    old = str(final_args.get("old_string", ""))
    new = str(final_args.get("new_string", ""))
    replace_all = bool(final_args.get("replace_all", False))
    # The effect is bound to the PATCH request digest (ADR-0005 §2 "patch digest"),
    # not the resulting content (which is derived at execution from the file + patch).
    patch_payload = json.dumps(
        {"mode": "replace", "old": old, "new": new, "replace_all": replace_all},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    grant = _grant(ctx, {"read", "write"})
    # PRE-EXECUTION validation (before any effect is claimed): the target must be
    # grant-contained and readable, and the patch must be applicable. A failure
    # here is a clean rejection with ZERO effect row — never an UNKNOWN effect.
    try:
        fd = grant_fs.open_within_grant(
            grant, requested_path, operation="read",
            principal=ctx.principal, workspace_id=ctx.workspace_id, now=ctx.now,
        )
        try:
            current = os.read(fd, _MAX_READ_BYTES).decode("utf-8", errors="replace")
        finally:
            os.close(fd)
    except FolderGrantError as exc:
        return _blocked(f"managed patch target refused: {type(exc).__name__}")
    except OSError as exc:
        return _blocked(f"managed patch target unreadable: {type(exc).__name__}")
    try:
        new_text = _apply_replace(current, old, new, replace_all)
    except _PatchError as exc:
        return _blocked(f"managed patch not applicable: {exc}")
    # Now claim the write effect and perform the actual mutation.
    outcome, effect, auth = _govern(
        ctx, grant=grant, requested_path=requested_path, operation="write",
        digest_bytes=patch_payload, descriptor=dict(final_args),
    )
    if outcome is not None:
        return outcome
    written = _host_write(ctx, grant, requested_path, new_text.encode("utf-8"))
    if isinstance(written, RouteOutcome):  # mutation-phase host-IO failure
        return _unknown(ctx, effect, OSError(written.reason or ""), "patch")
    return _finish(ctx, effect, auth, {"operation": "patch", "bytes_written": written})


def _apply_replace(text: str, old: str, new: str, replace_all: bool) -> str:
    if old == "":
        raise _PatchError("empty old_string")
    count = text.count(old)
    if count == 0:
        raise _PatchError("old_string not found")
    if count > 1 and not replace_all:
        raise _PatchError("old_string is ambiguous (multiple matches); set replace_all")
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def _route_patch_v4a(ctx: _Ctx, final_args: dict) -> RouteOutcome:
    from tools.patch_parser import OperationType, parse_v4a_patch

    patch_text = str(final_args.get("patch") or "")
    ops, err = parse_v4a_patch(patch_text)
    if err:
        return _blocked(f"managed V4A patch parse error: {err}")
    if len(ops) != 1:
        return _blocked(
            "managed V4A patch must contain exactly one ADD / DELETE / MOVE "
            "operation (multi-operation patches are not governed in managed mode)"
        )
    op = ops[0]
    if op.operation == OperationType.DELETE:
        grant = _grant(ctx, {"delete"})
        descriptor = {"tool": "patch", "op": "delete", "path": op.file_path}
        outcome, effect, auth = _govern(
            ctx, grant=grant, requested_path=op.file_path, operation="delete",
            digest_bytes=b"", descriptor=descriptor,
        )
        if outcome is not None:
            return outcome
        try:
            grant_fs.unlink_within_grant(
                grant, op.file_path, principal=ctx.principal,
                workspace_id=ctx.workspace_id, now=ctx.now,
            )
        except (FolderGrantError, OSError) as exc:
            return _unknown(ctx, effect, exc, "delete")
        return _finish(ctx, effect, auth, {"operation": "delete"})

    if op.operation == OperationType.MOVE:
        if not op.new_path:
            return _blocked("managed V4A move requires a destination path")
        grant = _grant(ctx, {"move"})
        # PRE-EXECUTION: BOTH source and destination must be grant-contained before
        # any effect is claimed. A destination outside the grant is a clean
        # rejection with ZERO effect row — never an UNKNOWN effect.
        try:
            resolve_within_grant(
                grant, op.file_path, operation="move", tenant_id=ctx.principal.tenant,
                principal_id=ctx.principal.user, workspace_id=ctx.workspace_id, now=ctx.now,
            )
            resolve_within_grant(
                grant, op.new_path, operation="move", tenant_id=ctx.principal.tenant,
                principal_id=ctx.principal.user, workspace_id=ctx.workspace_id, now=ctx.now,
            )
        except FolderGrantError as exc:
            return _blocked(f"managed move refused: {type(exc).__name__}")
        descriptor = {"tool": "patch", "op": "move", "src": op.file_path, "dst": op.new_path}
        move_ident = json.dumps(
            {"src": op.file_path, "dst": op.new_path}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        outcome, effect, auth = _govern(
            ctx, grant=grant, requested_path=op.file_path, operation="move",
            digest_bytes=move_ident, descriptor=descriptor,
        )
        if outcome is not None:
            return outcome
        try:
            grant_fs.move_within_grant(
                grant, grant, op.file_path, op.new_path, principal=ctx.principal,
                workspace_id=ctx.workspace_id, now=ctx.now,
            )
        except (FolderGrantError, OSError) as exc:
            # Post-claim boundary failure (e.g. a TOCTOU swap): outcome indeterminate.
            return _unknown(ctx, effect, exc, "move")
        return _finish(ctx, effect, auth, {"operation": "move", "destination": op.new_path})

    # ADD (content lives in hunks and duplicates write_file) and UPDATE (hunk
    # application) are not reproduced in the governed host-IO path.
    return _blocked(
        "managed V4A ADD/UPDATE patches are not supported; use write_file or a "
        "replace-mode patch"
    )


def route_managed_file_tool(
    agent, function_name: str, final_args: Any, *, task_id: str, now: Optional[float] = None,
) -> Optional[RouteOutcome]:
    """Route a managed file-tool call. Returns ``None`` when it does not apply
    (non-file tool, or not a managed run) so the normal authority gate runs;
    otherwise the router owns the outcome (handled or blocked)."""
    operation = _FILE_TOOL_OP.get(function_name)
    if operation is None:
        return None
    try:
        from youtab_runtime import managed_execution as mx
    except Exception:
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

    if not _is_local_backend(agent, task_id):
        return _blocked(MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE)

    workspace_root = getattr(agent, "_managed_workspace_root", None)
    if not workspace_root:
        return _blocked(
            "managed workspace root is not bound for this run; refusing local "
            "filesystem operation (fail closed)"
        )
    if not isinstance(final_args, dict) or (operation != "patch" and not final_args.get("path")):
        return _blocked("managed filesystem operation requires a 'path' argument")

    db_path = getattr(agent, "_managed_effects_db", None)
    ctx = _Ctx(
        agent=agent, admitted=admitted, principal=principal, workspace_id=workspace_id,
        workspace_root=workspace_root, db_path=Path(db_path) if db_path is not None else None,
        production=bool(getattr(agent, "_managed_authority_production", True)),
        prod_keys=getattr(agent, "_managed_authority_public_keys", None),
        test_keys=getattr(agent, "_managed_test_authority_keys", None),
        now=now, task_id=task_id,
    )

    if operation == "read":
        return _route_read(ctx, final_args)
    if operation == "write":
        return _route_write(ctx, final_args)
    return _route_patch(ctx, final_args)
