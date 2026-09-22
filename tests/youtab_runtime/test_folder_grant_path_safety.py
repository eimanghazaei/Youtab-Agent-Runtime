"""Adversarial coverage for Folder Grant path-safety enforcement.

Runnable two ways so it stays lightweight during the disk-constrained period:
  * as pytest   :  python -m pytest tests/youtab_runtime/test_folder_grant_path_safety.py
  * standalone  :  python tests/youtab_runtime/test_folder_grant_path_safety.py
The standalone path avoids the heavy repo conftest and needs only stdlib.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

# Allow standalone execution from anywhere in the worktree.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from youtab_runtime.folder_grant import (  # noqa: E402
    FolderGrant,
    GrantBindingError,
    GrantExpiredError,
    GrantRevokedError,
    GrantScopeError,
    resolve_within_grant,
)

TENANT = "tenant-A"
PRINCIPAL = "user-1"
WORKSPACE = "ws-canonical"


def _grant(root: str, *, perms=("read", "write", "create"), **kw) -> FolderGrant:
    return FolderGrant(
        grant_id="g-1",
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        workspace_id=WORKSPACE,
        canonical_root=root,
        permissions=frozenset(perms),
        **kw,
    )


def _resolve(grant: FolderGrant, path: str, *, operation="read", **kw) -> str:
    return resolve_within_grant(
        grant,
        path,
        operation=operation,
        tenant_id=kw.pop("tenant_id", TENANT),
        principal_id=kw.pop("principal_id", PRINCIPAL),
        workspace_id=kw.pop("workspace_id", WORKSPACE),
        **kw,
    )


def _expect(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # wrong error type — still a failure to surface
        raise AssertionError(f"expected {exc.__name__}, got {type(e).__name__}: {e}") from e
    raise AssertionError(f"expected {exc.__name__}, but call succeeded")


# ---- happy path -------------------------------------------------------------

def test_valid_relative_within_root(tmp_root):
    real = _resolve(_grant(tmp_root), "sub/data.txt", operation="write")
    assert os.path.normcase(real).startswith(os.path.normcase(os.path.realpath(tmp_root)))


def test_dot_and_nested_normalize_inside(tmp_root):
    real = _resolve(_grant(tmp_root), "a/./b/c.txt")
    assert "b" in real and "c.txt" in real


# ---- traversal / substitution escapes --------------------------------------

def test_parent_traversal_rejected(tmp_root):
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), "../outside.txt"))


def test_deep_traversal_rejected(tmp_root):
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), "a/b/../../../etc/passwd"))


def test_absolute_posix_rejected(tmp_root):
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), "/etc/passwd"))


def test_absolute_windows_rejected(tmp_root):
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), r"C:\Windows\System32\drivers\etc\hosts"))


def test_backslash_root_rejected(tmp_root):
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), r"\Windows\notepad.exe"))


def test_unc_rejected(tmp_root):
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), r"\\attacker\share\x"))
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), "//attacker/share/x"))


def test_alternate_drive_rejected(tmp_root):
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), r"D:\secret"))


def test_drive_relative_rejected(tmp_root):
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), "C:evil"))


def test_sibling_prefix_not_contained(tmp_root):
    # A sibling dir sharing a name prefix must not be treated as inside.
    sib = tmp_root + "X"
    os.makedirs(sib, exist_ok=True)
    # request that resolves (via join+..) toward the sibling is blocked by '..' guard,
    # and even a crafted absolute is blocked; assert containment logic directly too.
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), "../" + os.path.basename(sib) + "/f"))


# ---- symlink / junction / reparse escape -----------------------------------

def test_symlink_or_junction_escape_rejected(tmp_root):
    outside = tempfile.mkdtemp(prefix="fg-outside-")
    link = os.path.join(tmp_root, "link")
    created = _make_reparse(link, outside)
    if not created:
        # No privilege/support for symlink AND junction creation — do not skip
        # silently; assert the containment primitive rejects an equivalent
        # out-of-root real path so the escape class is still covered.
        from youtab_runtime.folder_grant import _canonical, _is_within
        assert not _is_within(_canonical(tmp_root), _canonical(os.path.join(outside, "f")))
        return
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), "link/secret.txt"))


# ---- binding / lifecycle / permission --------------------------------------

def test_cross_tenant_binding_rejected(tmp_root):
    _expect(GrantBindingError, lambda: _resolve(_grant(tmp_root), "f.txt", tenant_id="tenant-B"))


def test_cross_principal_binding_rejected(tmp_root):
    _expect(GrantBindingError, lambda: _resolve(_grant(tmp_root), "f.txt", principal_id="user-2"))


def test_cross_workspace_binding_rejected(tmp_root):
    _expect(GrantBindingError, lambda: _resolve(_grant(tmp_root), "f.txt", workspace_id="ws-other"))


def test_revoked_grant_rejected(tmp_root):
    _expect(GrantRevokedError, lambda: _resolve(_grant(tmp_root, revoked=True), "f.txt"))


def test_expired_grant_rejected(tmp_root):
    g = _grant(tmp_root, expires_at=time.time() - 1)
    _expect(GrantExpiredError, lambda: _resolve(g, "f.txt"))


def test_missing_permission_rejected(tmp_root):
    g = _grant(tmp_root, perms=("read",))
    _expect(GrantScopeError, lambda: _resolve(g, "f.txt", operation="write"))


def test_unknown_operation_rejected(tmp_root):
    _expect(GrantScopeError, lambda: _resolve(_grant(tmp_root), "f.txt", operation="delete"))


# ---- helpers ---------------------------------------------------------------

def _make_reparse(link: str, target: str) -> bool:
    """Best-effort create a symlink; fall back to a Windows junction (no admin).

    Returns True if a reparse point was created at ``link`` pointing to ``target``.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", link, target],
                check=True, capture_output=True, timeout=15,
            )
            return os.path.exists(link)
        except Exception:
            return False
    return False


# ---- standalone runner (no pytest / conftest) ------------------------------

def _run_standalone() -> int:
    tests = sorted(
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    passed = failed = 0
    for name, fn in tests:
        root = tempfile.mkdtemp(prefix="fg-root-")
        try:
            fn(root)
            print(f"PASS {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001 — surface every failure
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


# pytest fixture shim (only imported/used under pytest) ------------------------
try:
    import pytest

    @pytest.fixture()
    def tmp_root(tmp_path):
        return str(tmp_path)
except Exception:  # pragma: no cover - pytest absent in standalone mode
    pass


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
