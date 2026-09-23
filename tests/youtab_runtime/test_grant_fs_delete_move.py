"""Item 8 — host-IO delete/move within a Folder Grant with handle-based
containment and destination-parent TOCTOU coverage.

Delete and move re-validate every ancestor for reparse points (symlink/junction)
and, for delete, confirm the opened handle's final path is inside the grant. A
parent swapped for an out-of-grant junction between validation and the operation
fails closed with no deletion / no move; a source escape is refused; and a
cross-grant move validates BOTH grants' roots.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from youtab_runtime.folder_grant import (  # noqa: E402
    FolderGrant,
    GrantScopeError,
    _canonical,
    _is_within,
)
from youtab_runtime.grant_fs import move_within_grant, unlink_within_grant  # noqa: E402
from youtab_runtime.run_journal import Principal  # noqa: E402

WS = "ws-1"
NOW = 1000.0


def _p():
    return Principal(tenant="t", user="u")


def _grant(root, perms):
    return FolderGrant(
        grant_id="g-1", tenant_id="t", principal_id="u", workspace_id=WS,
        canonical_root=root, permissions=frozenset(perms),
    )


def _make_reparse(link: str, target: str) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if sys.platform == "win32":
        try:
            subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           check=True, capture_output=True, timeout=15)
            return os.path.isdir(link)
        except Exception:
            return False
    return False


def _expect(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception as e:
        raise AssertionError(f"expected {exc.__name__}, got {type(e).__name__}: {e}") from e
    raise AssertionError(f"expected {exc.__name__}, but call succeeded")


# ── delete ────────────────────────────────────────────────────────────────────


def test_unlink_within_grant_deletes_inside(tmp_root):
    target = os.path.join(tmp_root, "d", "f.txt")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(b"bye")
    unlink_within_grant(_grant(tmp_root, ("delete",)), "d/f.txt",
                        principal=_p(), workspace_id=WS, now=NOW)
    assert not os.path.exists(target)


def test_unlink_refuses_directory(tmp_root):
    os.makedirs(os.path.join(tmp_root, "adir"), exist_ok=True)
    _expect(GrantScopeError, lambda: unlink_within_grant(
        _grant(tmp_root, ("delete",)), "adir",
        principal=_p(), workspace_id=WS, now=NOW))
    assert os.path.isdir(os.path.join(tmp_root, "adir"))


def test_unlink_parent_swapped_to_junction_fails_closed(tmp_root):
    parent = os.path.join(tmp_root, "p")
    os.makedirs(parent, exist_ok=True)
    with open(os.path.join(parent, "f.txt"), "wb") as fh:
        fh.write(b"inside")
    outside = tempfile.mkdtemp(prefix="del-out-")
    with open(os.path.join(outside, "f.txt"), "wb") as fh:
        fh.write(b"OUTSIDE")

    def hook():
        os.remove(os.path.join(parent, "f.txt"))
        os.rmdir(parent)
        assert _make_reparse(parent, outside)

    if not _make_reparse(os.path.join(tmp_root, "_probe"), tempfile.mkdtemp(prefix="del-probe-")):
        assert not _is_within(_canonical(tmp_root), _canonical(os.path.join(outside, "f.txt")))
        return

    _expect(GrantScopeError, lambda: unlink_within_grant(
        _grant(tmp_root, ("delete",)), "p/f.txt",
        principal=_p(), workspace_id=WS, now=NOW, _pre_op_hook=hook))
    # The out-of-grant file was NOT deleted through the swapped junction.
    assert os.path.exists(os.path.join(outside, "f.txt"))


# ── move ────────────────────────────────────────────────────────────────────


def test_move_within_grant_relocates(tmp_root):
    src = os.path.join(tmp_root, "a.txt")
    with open(src, "wb") as fh:
        fh.write(b"payload")
    g = _grant(tmp_root, ("move",))
    move_within_grant(g, g, "a.txt", "b.txt", principal=_p(), workspace_id=WS, now=NOW)
    assert not os.path.exists(src)
    assert Path(os.path.join(tmp_root, "b.txt")).read_bytes() == b"payload"


def test_move_across_two_grants_same_principal(tmp_path):
    root1 = os.path.join(str(tmp_path), "r1")
    root2 = os.path.join(str(tmp_path), "r2")
    os.makedirs(root1)
    os.makedirs(root2)
    with open(os.path.join(root1, "a.txt"), "wb") as fh:
        fh.write(b"cross")
    g1 = _grant(root1, ("move",))
    g2 = _grant(root2, ("move",))
    move_within_grant(g1, g2, "a.txt", "b.txt", principal=_p(), workspace_id=WS, now=NOW)
    assert not os.path.exists(os.path.join(root1, "a.txt"))
    assert Path(os.path.join(root2, "b.txt")).read_bytes() == b"cross"


def test_move_source_escape_fails_closed(tmp_root):
    g = _grant(tmp_root, ("move",))
    _expect(GrantScopeError, lambda: move_within_grant(
        g, g, "../escape.txt", "b.txt", principal=_p(), workspace_id=WS, now=NOW))


def test_move_destination_parent_swap_fails_closed(tmp_root):
    src = os.path.join(tmp_root, "a.txt")
    with open(src, "wb") as fh:
        fh.write(b"payload")
    dst_parent = os.path.join(tmp_root, "dp")
    os.makedirs(dst_parent, exist_ok=True)
    outside = tempfile.mkdtemp(prefix="mv-out-")

    def hook():
        os.rmdir(dst_parent)
        assert _make_reparse(dst_parent, outside)

    if not _make_reparse(os.path.join(tmp_root, "_probe2"), tempfile.mkdtemp(prefix="mv-probe-")):
        assert not _is_within(_canonical(tmp_root), _canonical(os.path.join(outside, "b.txt")))
        return

    g = _grant(tmp_root, ("move",))
    _expect(GrantScopeError, lambda: move_within_grant(
        g, g, "a.txt", "dp/b.txt", principal=_p(), workspace_id=WS, now=NOW,
        _pre_op_hook=hook))
    # Source intact; nothing moved out through the swapped junction.
    assert os.path.exists(src)
    assert not os.path.exists(os.path.join(outside, "b.txt"))


def _run_standalone() -> int:
    tests = sorted((n, o) for n, o in globals().items()
                   if n.startswith("test_") and callable(o))
    passed = failed = 0
    for name, fn in tests:
        root = tempfile.mkdtemp(prefix="delmove-root-")
        try:
            import inspect
            if "tmp_path" in inspect.signature(fn).parameters:
                fn(Path(tempfile.mkdtemp(prefix="delmove-tp-")))
            else:
                fn(root)
            print(f"PASS {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


try:
    import pytest

    @pytest.fixture()
    def tmp_root(tmp_path):
        return str(tmp_path)
except Exception:  # pragma: no cover
    pass


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
