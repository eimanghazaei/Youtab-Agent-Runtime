"""Adversarial coverage for grant-bound filesystem effects (grant_fs).

Ties Folder Grant authority to the effect ledger's exactly-once receipt spine.
Runnable under pytest or standalone (stdlib + a temp sqlite db only):
  python tests/youtab_runtime/test_grant_fs_effect.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from youtab_runtime import effect_ledger as ledger  # noqa: E402
from youtab_runtime.folder_grant import (  # noqa: E402
    FolderGrant,
    GrantExpiredError,
    GrantRevokedError,
    GrantScopeError,
)
from youtab_runtime.grant_fs import (  # noqa: E402
    claim_granted_fs_effect,
    settle_committed,
    settle_unknown,
)
from youtab_runtime.run_journal import Principal  # noqa: E402

RUN = "run-1"
TENANT = "tenant-A"
USER = "user-1"
WS = "ws-1"


def _principal(tenant=TENANT, user=USER):
    return Principal(tenant=tenant, user=user)


def _grant(root, *, tenant=TENANT, user=USER, ws=WS, perms=("read", "write", "create"), **kw):
    return FolderGrant(
        grant_id="g-1", tenant_id=tenant, principal_id=user, workspace_id=ws,
        canonical_root=root, permissions=frozenset(perms), **kw,
    )


def _claim(grant, path, *, op="write", db, principal=None, ws=WS, run=RUN):
    return claim_granted_fs_effect(
        grant, path, operation=op, run_id=run,
        principal=principal or _principal(), workspace_id=ws, db_path=db,
    )


def _expect(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception as e:
        raise AssertionError(f"expected {exc.__name__}, got {type(e).__name__}: {e}") from e
    raise AssertionError(f"expected {exc.__name__}, but call succeeded")


# ---- exactly-once / replay --------------------------------------------------

def test_first_claim_wins_then_commit(tmp_root, db):
    p = _principal()
    eff = _claim(_grant(tmp_root), "a/f.txt", db=db)
    assert eff.won is True and eff.state == "in_progress"
    assert settle_committed(eff, p, db_path=db) == "committed"


def test_replay_does_not_win_after_commit(tmp_root, db):
    p = _principal()
    e1 = _claim(_grant(tmp_root), "a/f.txt", db=db)
    settle_committed(e1, p, db_path=db)
    e2 = _claim(_grant(tmp_root), "a/f.txt", db=db)  # retry same op
    assert e2.won is False and e2.state == "committed" and e2.effect_id == e1.effect_id


def test_concurrent_second_claim_loses_while_in_progress(tmp_root, db):
    e1 = _claim(_grant(tmp_root), "a/f.txt", db=db)
    e2 = _claim(_grant(tmp_root), "a/f.txt", db=db)  # before settle
    assert e1.won is True and e2.won is False and e2.state == "in_progress"


# ---- crash-after-effect reconciliation --------------------------------------

def test_unknown_outcome_is_not_blind_retried(tmp_root, db):
    p = _principal()
    e1 = _claim(_grant(tmp_root), "a/f.txt", db=db)
    assert e1.won is True
    assert settle_unknown(e1, p, db_path=db) == "unknown"  # crash before proof
    e2 = _claim(_grant(tmp_root), "a/f.txt", db=db)
    # Stranded 'unknown' is NOT executable: caller must reconcile, not re-apply.
    assert e2.won is False and e2.state == "unknown"


# ---- isolation --------------------------------------------------------------

def test_cross_workspace_is_a_distinct_effect(tmp_root, db):
    p = _principal()
    a = _claim(_grant(tmp_root, ws="ws-A"), "f.txt", db=db, ws="ws-A")
    settle_committed(a, p, db_path=db)
    b = _claim(_grant(tmp_root, ws="ws-B"), "f.txt", db=db, ws="ws-B")
    # Same path, different workspace → different effect, still executable.
    assert b.effect_id != a.effect_id and b.won is True


def test_cross_principal_is_a_distinct_effect(tmp_root, db):
    a_grant = _grant(tmp_root, tenant="t-A", user="u-A")
    b_grant = _grant(tmp_root, tenant="t-B", user="u-B")
    a = _claim(a_grant, "f.txt", db=db, principal=_principal("t-A", "u-A"))
    settle_committed(a, _principal("t-A", "u-A"), db_path=db)
    b = _claim(b_grant, "f.txt", db=db, principal=_principal("t-B", "u-B"))
    assert b.effect_id != a.effect_id and b.won is True


# ---- fail-closed BEFORE any effect row exists -------------------------------

def test_traversal_rejected_before_registration(tmp_root, db):
    _expect(GrantScopeError, lambda: _claim(_grant(tmp_root), "../escape", db=db))
    assert _no_effects(db)


def test_revoked_grant_registers_nothing(tmp_root, db):
    _expect(GrantRevokedError, lambda: _claim(_grant(tmp_root, revoked=True), "f.txt", db=db))
    assert _no_effects(db)


def test_expired_grant_registers_nothing(tmp_root, db):
    import time as _t
    _expect(GrantExpiredError, lambda: _claim(_grant(tmp_root, expires_at=_t.time() - 1), "f.txt", db=db))
    assert _no_effects(db)


def test_missing_permission_registers_nothing(tmp_root, db):
    _expect(GrantScopeError, lambda: _claim(_grant(tmp_root, perms=("read",)), "f.txt", op="write", db=db))
    assert _no_effects(db)


# ---- helpers ----------------------------------------------------------------

def _no_effects(db) -> bool:
    import sqlite3
    if not os.path.exists(db):
        return True
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='effects'"
        ).fetchone()
        if row is None:
            return True
        return conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    finally:
        conn.close()


def _run_standalone() -> int:
    tests = sorted(
        (n, o) for n, o in globals().items() if n.startswith("test_") and callable(o)
    )
    passed = failed = 0
    for name, fn in tests:
        root = tempfile.mkdtemp(prefix="gfs-root-")
        dbfd, dbpath = tempfile.mkstemp(prefix="gfs-", suffix=".sqlite3")
        os.close(dbfd)
        os.unlink(dbpath)  # let the ledger create it fresh
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
