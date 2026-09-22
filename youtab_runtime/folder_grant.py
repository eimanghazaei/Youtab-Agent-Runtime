"""Folder Grant — server-issued, immutable, workspace-bound filesystem authority.

A grant is the ONLY authority for a filesystem operation: the client never
supplies a path that is trusted as authority. Every operation resolves the
client-relative request against the grant's canonical root and is re-validated
immediately before the operation, rejecting every escape vector (traversal,
absolute substitution, UNC, alternate drive, symlink / junction / reparse-point).

This module is pure-stdlib (no runtime deps) so it can be enforced anywhere in
the Runtime and unit-tested without the heavy toolchain. Grant issuance and the
effect-ledger receipt linkage build on top of the primitives here.
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from typing import FrozenSet, Optional

__all__ = [
    "FolderGrantError",
    "GrantExpiredError",
    "GrantRevokedError",
    "GrantScopeError",
    "GrantBindingError",
    "FolderGrant",
    "resolve_within_grant",
]


class FolderGrantError(Exception):
    """Fail-closed base error. The message never leaks the resolved outside path."""


class GrantExpiredError(FolderGrantError):
    pass


class GrantRevokedError(FolderGrantError):
    pass


class GrantScopeError(FolderGrantError):
    """Requested path escapes the grant root, or the permission is not held."""


class GrantBindingError(FolderGrantError):
    """Tenant / principal / workspace does not match the grant's immutable binding."""


def _canonical(path: str) -> str:
    """Fully resolve a path (symlinks/junctions/reparse points) and normalize case.

    ``os.path.realpath`` resolves reparse points on Windows (3.8+); ``normcase``
    makes the containment check case- and separator-insensitive on Windows.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _is_within(root_real: str, target_real: str) -> bool:
    """True iff ``target_real`` is ``root_real`` or a descendant of it.

    Uses ``os.path.commonpath`` on already-realpath'd, normcased paths so a
    symlink/junction whose real target sits outside the root is rejected, and a
    sibling like ``rootX`` cannot masquerade as being under ``root``.
    """
    if target_real == root_real:
        return True
    try:
        return os.path.commonpath([root_real, target_real]) == root_real
    except ValueError:
        # Different drives / UNC vs local — never contained.
        return False


@dataclass(frozen=True)
class FolderGrant:
    """Immutable server-issued grant. All fields are set by the server, never the client."""

    grant_id: str
    tenant_id: str
    principal_id: str
    workspace_id: str  # canonical workspace id
    canonical_root: str  # server-resolved absolute root
    permissions: FrozenSet[str] = field(default_factory=frozenset)  # {"read","write","create"}
    expires_at: Optional[float] = None  # epoch seconds; None = no expiry
    revoked: bool = False
    audit_ref: Optional[str] = None  # immutable receipt/audit reference

    def root_real(self) -> str:
        return _canonical(self.canonical_root)


def _is_reparse(path: str) -> bool:
    """True if ``path`` itself is a symlink/junction/reparse point (not followed).

    On Windows checks the reparse-point file attribute (catches junctions, which
    ``S_ISLNK`` does not); on POSIX checks for a symlink. A missing component is
    not a reparse point.
    """
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse)


def assert_no_reparse_ancestors(root_real: str, target_abs: str) -> None:
    """Fail closed if any component from ``root_real`` down to ``target_abs`` is a
    reparse point (symlink/junction). Every parent is validated, not just the
    final component — an ``O_NOFOLLOW`` on the leaf does not cover a junctioned
    parent on Windows.
    """
    root_real = os.path.normcase(root_real)
    target_norm = os.path.normcase(os.path.abspath(target_abs))
    if target_norm != root_real and not target_norm.startswith(root_real + os.sep):
        raise GrantScopeError("target is not lexically within the grant root")
    rel = os.path.relpath(target_norm, root_real)
    if rel == os.curdir:
        return
    cur = root_real
    for part in rel.split(os.sep):
        if part in ("", os.curdir, os.pardir):
            raise GrantScopeError("illegal path component")
        cur = os.path.join(cur, part)
        if _is_reparse(cur):
            raise GrantScopeError("reparse point in path is not permitted")


def _validate_grant(grant: FolderGrant, *, now: float) -> None:
    if grant.revoked:
        raise GrantRevokedError(f"grant {grant.grant_id} is revoked")
    if grant.expires_at is not None and now >= grant.expires_at:
        raise GrantExpiredError(f"grant {grant.grant_id} is expired")


def _reject_untrusted_request(requested_path: str) -> None:
    """Reject request shapes that attempt to be authoritative rather than
    grant-relative: absolute paths, drive-qualified paths, and UNC paths.

    The only trusted authority is the grant root; the client request must be a
    relative path *inside* it.
    """
    if requested_path is None:
        raise GrantScopeError("no path supplied")
    p = str(requested_path)
    # UNC: \\server\share or //server/share
    if p.startswith("\\\\") or p.startswith("//"):
        raise GrantScopeError("UNC paths are not permitted")
    # Absolute (POSIX "/...", or Windows "\..." / "C:\...") — client may not
    # supply an absolute path as authority.
    if os.path.isabs(p):
        raise GrantScopeError("absolute paths are not permitted; request must be grant-relative")
    # Drive-qualified without separator, e.g. "C:foo" (drive-relative) or "D:\x".
    drive, _ = os.path.splitdrive(p)
    if drive:
        raise GrantScopeError("drive-qualified paths are not permitted")


def resolve_within_grant(
    grant: FolderGrant,
    requested_path: str,
    *,
    operation: str,
    tenant_id: str,
    principal_id: str,
    workspace_id: str,
    now: Optional[float] = None,
) -> str:
    """Re-validate the grant and resolve ``requested_path`` to a safe real path.

    Raises a ``FolderGrantError`` subclass (fail-closed) on any binding mismatch,
    expiry/revocation, missing permission, or escape attempt. On success returns
    the canonical (realpath'd) absolute path guaranteed to be inside the grant.

    ``operation`` is one of ``read`` / ``write`` / ``create`` and must be held in
    ``grant.permissions``.
    """
    now = time.time() if now is None else now

    # 1) immutable binding — the caller's identity must match the grant exactly.
    if (
        tenant_id != grant.tenant_id
        or principal_id != grant.principal_id
        or workspace_id != grant.workspace_id
    ):
        raise GrantBindingError("tenant/principal/workspace does not match the grant binding")

    # 2) permission check.
    if operation not in ("read", "write", "create"):
        raise GrantScopeError(f"unknown operation {operation!r}")
    if operation not in grant.permissions:
        raise GrantScopeError(f"operation {operation!r} not permitted by grant")

    # 3) re-validate grant lifecycle immediately before the filesystem op.
    _validate_grant(grant, now=now)

    # 4) reject untrusted request shapes (absolute / drive / UNC).
    _reject_untrusted_request(requested_path)

    # 5) reject traversal tokens up-front (defense in depth; the containment
    #    check below is authoritative).
    parts = str(requested_path).replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise GrantScopeError("path traversal ('..') is not permitted")

    # 6) resolve the candidate and enforce containment against the REAL root,
    #    following symlinks/junctions/reparse points — the final real target
    #    must still be inside the grant root.
    root_real = grant.root_real()
    candidate = os.path.join(grant.canonical_root, str(requested_path))
    target_real = _canonical(candidate)
    if not _is_within(root_real, target_real):
        raise GrantScopeError("resolved path escapes the grant root")

    return target_real
