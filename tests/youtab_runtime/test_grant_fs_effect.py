"""Core adversarial coverage for grant-bound filesystem effects (grant_fs).

Exactly-once, isolation, workspace-bound receipts, fail-closed-before-registration,
and lease-holder-only settle. Signed-authority negatives live in
test_effect_authorization.py; lease/reconcile in test_worker_lease.py.
pytest-collected; also runnable standalone.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from youtab_runtime import effect_ledger as ledger  # noqa: E402
from youtab_runtime.approval import compute_effect_digest, content_digest  # noqa: E402
from youtab_runtime.effect_authorization import TestEffectAuthority  # noqa: E402
from youtab_runtime.folder_grant import (  # noqa: E402
    FolderGrant,
    GrantExpiredError,
    GrantRevokedError,
    GrantScopeError,
    resolve_within_grant,
)
from youtab_runtime.grant_fs import (  # noqa: E402
    claim_granted_fs_effect,
    settle_committed,
    settle_unknown,
)
from youtab_runtime.run_journal import Principal  # noqa: E402
from youtab_runtime.worker_lease import LeaseError, WorkspaceMismatchError  # noqa: E402

RUN = "run-1"
TENANT = "tenant-A"
USER = "user-1"
WS = "ws-1"
NOW = 1_000_000.0
AUTH = TestEffectAuthority()
CMD = "cmd-00000001"


def _dt(ts):
    return datetime.fromtimestamp(ts, UTC)


def _principal(tenant=TENANT, user=USER):
    return Principal(tenant=tenant, user=user)


def _grant(root, *, tenant=TENANT, user=USER, ws=WS, perms=("read", "write", "create"), **kw):
    return FolderGrant(
        grant_id="g-1", tenant_id=tenant, principal_id=user, workspace_id=ws,
        canonical_root=root, permissions=frozenset(perms), **kw,
    )


def _mint(safe_path, *, op, ws, content, principal, now, auth_id, expires_in=100.0):
    ed = compute_effect_digest(op, safe_path, ws, content_digest(content))
    return AUTH.mint(
        authorization_id=auth_id, tenant_id=principal.tenant, user_id=principal.user,
        workspace_id=ws, command_id=CMD, capability="fs", operation=op,
        effect_digest=ed, issued_at=_dt(now), expires_at=_dt(now + expires_in),
    )


def _claim(grant, path, *, op="write", db, principal=None, ws=WS, run=RUN,
           content=b"data", owner="w1", now=NOW, authorization=None, revocation_check=None):
    principal = principal or _principal()
    return claim_granted_fs_effect(
        grant, path, operation=op, run_id=run, principal=principal, workspace_id=ws,
        authorization=authorization, owner_token=owner, content=content,
        production=False, test_authority_keys=AUTH.keyring(),
        revocation_check=revocation_check, db_path=db, now=now,
    )


def _issue_and_claim(grant, path, *, op="write", db, principal=None, ws=WS, run=RUN,
                     content=b"data", owner="w1", now=NOW, auth_id="az-0000000000000001",
                     revocation_check=None):
    principal = principal or _principal()
    safe = resolve_within_grant(grant, path, operation=op, tenant_id=principal.tenant,
                                principal_id=principal.user, workspace_id=ws, now=now)
    az = _mint(safe, op=op, ws=ws, content=content, principal=principal, now=now, auth_id=auth_id)
    return _claim(grant, path, op=op, db=db, principal=principal, ws=ws, run=run,
                  content=content, owner=owner, now=now, authorization=az,
                  revocation_check=revocation_check)


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
    eff = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db)
    assert eff.won is True and eff.state == "in_progress"
    assert settle_committed(eff, p, workspace_id=WS, db_path=db) == "committed"


def test_replay_does_not_win_after_commit(tmp_root, db):
    p = _principal()
    e1 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db)
    settle_committed(e1, p, workspace_id=WS, db_path=db)
    e2 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db, auth_id="az-0000000000000002")
    assert e2.won is False and e2.state == "committed" and e2.effect_id == e1.effect_id


def test_concurrent_second_claim_loses_while_in_progress(tmp_root, db):
    e1 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db)
    e2 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db, auth_id="az-0000000000000002")
    assert e1.won is True and e2.won is False and e2.state == "in_progress"


def test_unknown_outcome_is_not_blind_retried(tmp_root, db):
    p = _principal()
    e1 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db)
    assert settle_unknown(e1, p, workspace_id=WS, db_path=db) == "unknown"
    e2 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db, auth_id="az-0000000000000002")
    assert e2.won is False and e2.state == "unknown"


# ---- isolation + receipt ownership + workspace binding ----------------------

def test_cross_workspace_is_a_distinct_effect(tmp_root, db):
    p = _principal()
    a = _issue_and_claim(_grant(tmp_root, ws="ws-A"), "f.txt", db=db, ws="ws-A", auth_id="az-A0000000000000")
    settle_committed(a, p, workspace_id="ws-A", db_path=db)
    b = _issue_and_claim(_grant(tmp_root, ws="ws-B"), "f.txt", db=db, ws="ws-B", auth_id="az-B0000000000000")
    assert b.effect_id != a.effect_id and b.won is True


def test_cross_principal_is_a_distinct_effect(tmp_root, db):
    pa, pb = _principal("t-A", "u-A"), _principal("t-B", "u-B")
    a = _issue_and_claim(_grant(tmp_root, tenant="t-A", user="u-A"), "f.txt",
                         db=db, principal=pa, auth_id="az-A0000000000000")
    settle_committed(a, pa, workspace_id=WS, db_path=db)
    b = _issue_and_claim(_grant(tmp_root, tenant="t-B", user="u-B"), "f.txt",
                         db=db, principal=pb, auth_id="az-B0000000000000")
    assert b.effect_id != a.effect_id and b.won is True


def test_foreign_principal_receipt_rejected(tmp_root, db):
    pa, pb = _principal("t-A", "u-A"), _principal("t-B", "u-B")
    a = _issue_and_claim(_grant(tmp_root, tenant="t-A", user="u-A"), "f.txt",
                         db=db, principal=pa, auth_id="az-A0000000000000")
    settle_committed(a, pa, workspace_id=WS, db_path=db)
    assert ledger.get_effect(a.effect_id, pb, db_path=db) is None
    _expect(LeaseError, lambda: settle_committed(a, pb, workspace_id=WS, db_path=db))


def test_cross_workspace_receipt_settle_rejected(tmp_root, db):
    # Same tenant+user, effect in workspace A; settling as workspace B fails closed.
    p = _principal()
    a = _issue_and_claim(_grant(tmp_root, ws="ws-A"), "f.txt", db=db, ws="ws-A", auth_id="az-A0000000000000")
    _expect(WorkspaceMismatchError, lambda: settle_committed(a, p, workspace_id="ws-B", db_path=db))


# ---- fail-closed BEFORE any effect row -------------------------------------

def test_traversal_rejected_before_registration(tmp_root, db):
    _expect(GrantScopeError, lambda: _issue_and_claim(_grant(tmp_root), "../escape", db=db))
    assert _no_effects(db)


def test_revoked_grant_registers_nothing(tmp_root, db):
    _expect(GrantRevokedError, lambda: _issue_and_claim(_grant(tmp_root, revoked=True), "f.txt", db=db))
    assert _no_effects(db)


def test_expired_grant_registers_nothing(tmp_root, db):
    _expect(GrantExpiredError,
            lambda: _issue_and_claim(_grant(tmp_root, expires_at=NOW - 1), "f.txt", db=db))
    assert _no_effects(db)


def test_missing_permission_registers_nothing(tmp_root, db):
    _expect(GrantScopeError,
            lambda: _issue_and_claim(_grant(tmp_root, perms=("read",)), "f.txt", op="write", db=db))
    assert _no_effects(db)


# ---- lease-holder-only settle ----------------------------------------------

def test_settle_requires_lease_holder(tmp_root, db):
    eff = _issue_and_claim(_grant(tmp_root), "f.txt", db=db, owner="worker-1")
    forged = eff.__class__(eff.effect_id, eff.safe_path, eff.won, eff.state, "worker-2")
    _expect(LeaseError, lambda: settle_committed(forged, _principal(), workspace_id=WS, db_path=db))


def test_same_path_different_content_is_distinct_effect(tmp_root, db):
    g = _grant(tmp_root)
    a = _issue_and_claim(g, "f.txt", db=db, content=b"AAA", auth_id="az-A0000000000000")
    b = _issue_and_claim(g, "f.txt", db=db, content=b"BBB", auth_id="az-B0000000000000")
    assert a.effect_id != b.effect_id


# ---- helpers ----------------------------------------------------------------

def _no_effects(db) -> bool:
    import sqlite3
    if not os.path.exists(str(db)):
        return True
    conn = sqlite3.connect(str(db))
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
    tests = sorted((n, o) for n, o in globals().items()
                   if n.startswith("test_") and callable(o))
    passed = failed = 0
    for name, fn in tests:
        root = tempfile.mkdtemp(prefix="gfs-root-")
        dbfd, dbpath = tempfile.mkstemp(prefix="gfs-", suffix=".sqlite3")
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
