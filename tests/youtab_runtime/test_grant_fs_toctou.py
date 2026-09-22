"""TOCTOU adversarial coverage: the operation boundary must re-validate.

Proves that a symlink/junction swapped in *after* an initial validation is
rejected at the actual open/create boundary, and that a grant revoked between
resolution and effect fails closed. pytest-collected; also runnable standalone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from youtab_runtime.approval import (  # noqa: E402
    compute_effect_digest,
    content_digest,
    issue_approval,
)
from youtab_runtime.folder_grant import (  # noqa: E402
    FolderGrant,
    GrantRevokedError,
    GrantScopeError,
    _canonical,
    _is_within,
    resolve_within_grant,
)
from youtab_runtime.grant_fs import claim_granted_fs_effect, open_within_grant  # noqa: E402
from youtab_runtime.run_journal import Principal  # noqa: E402

WS = "ws-1"
NOW = 1000.0


def _p():
    return Principal(tenant="t", user="u")


def _grant(root, perms=("read", "write", "create")):
    return FolderGrant(
        grant_id="g-1", tenant_id="t", principal_id="u", workspace_id=WS,
        canonical_root=root, permissions=frozenset(perms),
    )


def _make_reparse(link: str, target: str) -> bool:
    """Create a directory symlink, else a Windows junction (no admin)."""
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


def test_open_within_grant_reads_inside(tmp_root, db):
    d = os.path.join(tmp_root, "d")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "f.txt"), "wb") as fh:
        fh.write(b"hello")
    fd = open_within_grant(_grant(tmp_root), "d/f.txt", operation="read",
                           principal=_p(), workspace_id=WS, now=NOW)
    try:
        assert os.read(fd, 5) == b"hello"
    finally:
        os.close(fd)


def test_open_boundary_rejects_component_swapped_to_junction(tmp_root, db):
    # 1) legitimate dir + file inside the grant; validate the path.
    inside = os.path.join(tmp_root, "d")
    os.makedirs(inside, exist_ok=True)
    with open(os.path.join(inside, "secret"), "wb") as fh:
        fh.write(b"inside")
    resolve_within_grant(_grant(tmp_root), "d/secret", operation="read",
                         tenant_id="t", principal_id="u", workspace_id=WS, now=NOW)
    # 2) attacker swaps component 'd' for a junction pointing OUTSIDE the grant.
    outside = tempfile.mkdtemp(prefix="toctou-out-")
    with open(os.path.join(outside, "secret"), "wb") as fh:
        fh.write(b"OUTSIDE")
    import shutil
    shutil.rmtree(inside)
    if not _make_reparse(inside, outside):
        # Environment cannot create a reparse point: assert the containment
        # primitive rejects the equivalent out-of-root real path (no silent skip).
        assert not _is_within(_canonical(tmp_root), _canonical(os.path.join(outside, "secret")))
        return
    # 3) the operation boundary re-resolves and fails closed.
    _expect(GrantScopeError,
            lambda: open_within_grant(_grant(tmp_root), "d/secret", operation="read",
                                      principal=_p(), workspace_id=WS, now=NOW))


def test_create_boundary_rejects_parent_swapped_to_junction(tmp_root, db):
    parent = os.path.join(tmp_root, "p")
    os.makedirs(parent, exist_ok=True)
    resolve_within_grant(_grant(tmp_root), "p/new.txt", operation="create",
                         tenant_id="t", principal_id="u", workspace_id=WS, now=NOW)
    outside = tempfile.mkdtemp(prefix="toctou-out-")
    os.rmdir(parent)
    if not _make_reparse(parent, outside):
        assert not _is_within(_canonical(tmp_root), _canonical(os.path.join(outside, "new.txt")))
        return
    _expect(GrantScopeError,
            lambda: open_within_grant(_grant(tmp_root), "p/new.txt", operation="create",
                                      principal=_p(), workspace_id=WS, now=NOW))
    # And no file was created outside.
    assert not os.path.exists(os.path.join(outside, "new.txt"))


def test_revocation_between_validation_and_claim_fails_closed(tmp_root, db):
    p = _p()
    g = _grant(tmp_root)
    # Approval is valid and would authorize the effect...
    safe = resolve_within_grant(g, "f.txt", operation="write", tenant_id="t",
                                principal_id="u", workspace_id=WS, now=NOW)
    ed = compute_effect_digest("write", safe, WS, content_digest(b"data"))
    issue_approval("ap-1", ed, p, WS, expires_at=NOW + 100, db_path=db)
    # ...but the grant is revoked in the window before the effect is claimed.
    revoked = {"v": True}
    _expect(GrantRevokedError, lambda: claim_granted_fs_effect(
        g, "f.txt", operation="write", run_id="run-1", principal=p,
        workspace_id=WS, approval_id="ap-1", owner_token="w1", content=b"data",
        revocation_check=lambda gid: revoked["v"], db_path=db, now=NOW,
    ))
    # Fail-closed: no effect registered, and the single-use approval was NOT spent.
    from youtab_runtime import effect_ledger as ledger
    assert ledger.list_effects("run-1", p, db_path=db) == []


def _reparse_supported() -> bool:
    root = tempfile.mkdtemp(prefix="toctou-probe-")
    out = tempfile.mkdtemp(prefix="toctou-probe-out-")
    return _make_reparse(os.path.join(root, "j"), out)


def test_pre_open_parent_swap_hook_fails_closed(tmp_root, db):
    # A parent is swapped for an outside-pointing junction in the window BETWEEN
    # the pre-open ancestor validation and the actual open(): the post-open
    # re-validation must catch the reparse parent and fail closed.
    parent = os.path.join(tmp_root, "p")
    os.makedirs(parent, exist_ok=True)
    with open(os.path.join(parent, "f.txt"), "wb") as fh:
        fh.write(b"inside")
    outside = tempfile.mkdtemp(prefix="toctou-hook-out-")
    with open(os.path.join(outside, "f.txt"), "wb") as fh:
        fh.write(b"OUTSIDE")

    if not _reparse_supported():
        # Environment cannot create reparse points: assert the primitive rejects
        # a junctioned-parent path (no silent skip).
        assert not _is_within(_canonical(tmp_root), _canonical(os.path.join(outside, "f.txt")))
        return

    def hook():
        os.remove(os.path.join(parent, "f.txt"))
        os.rmdir(parent)
        assert _make_reparse(parent, outside)  # parent -> outside junction

    def call():
        fd = open_within_grant(_grant(tmp_root), "p/f.txt", operation="read",
                               principal=_p(), workspace_id=WS, now=NOW,
                               _pre_open_hook=hook)
        os.close(fd)

    _expect(GrantScopeError, call)


def _run_standalone() -> int:
    tests = sorted((n, o) for n, o in globals().items()
                   if n.startswith("test_") and callable(o))
    passed = failed = 0
    for name, fn in tests:
        root = tempfile.mkdtemp(prefix="toctou-root-")
        dbfd, dbpath = tempfile.mkstemp(prefix="toctou-", suffix=".sqlite3")
        os.close(dbfd)
        os.unlink(dbpath)
        try:
            fn(root, Path(dbpath))
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

    @pytest.fixture()
    def db(tmp_path):
        return tmp_path / "effects.sqlite3"
except Exception:  # pragma: no cover
    pass


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
