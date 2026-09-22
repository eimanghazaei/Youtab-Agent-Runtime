"""Adversarial coverage for grant-bound filesystem effects (grant_fs).

Ties Folder Grant authority + single-use approval to the effect-ledger
exactly-once receipt spine and worker lease. pytest-collected; also runnable
standalone (stdlib + a temp sqlite db per test):
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
from youtab_runtime.approval import (  # noqa: E402
    ApprovalBindingError,
    ApprovalConsumedError,
    ApprovalDigestMismatchError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    compute_effect_digest,
    content_digest,
    issue_approval,
)
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
from youtab_runtime.worker_lease import (  # noqa: E402
    LeaseError,
    reconcile_to_terminal,
    sweep_expired_leases,
)

RUN = "run-1"
TENANT = "tenant-A"
USER = "user-1"
WS = "ws-1"
NOW = 1000.0


def _principal(tenant=TENANT, user=USER):
    return Principal(tenant=tenant, user=user)


def _grant(root, *, tenant=TENANT, user=USER, ws=WS, perms=("read", "write", "create"), **kw):
    return FolderGrant(
        grant_id="g-1", tenant_id=tenant, principal_id=user, workspace_id=ws,
        canonical_root=root, permissions=frozenset(perms), **kw,
    )


def _issue_for(grant, path, *, op, db, principal, ws, content, now, approval_id, expires_at=None):
    """Issue an approval bound to the exact effect digest the claim will compute."""
    safe = resolve_within_grant(
        grant, path, operation=op, tenant_id=principal.tenant,
        principal_id=principal.user, workspace_id=ws, now=now,
    )
    ed = compute_effect_digest(op, safe, ws, content_digest(content))
    issue_approval(approval_id, ed, principal, ws,
                   expires_at=expires_at if expires_at is not None else now + 100.0,
                   db_path=db)
    return approval_id


def _claim(grant, path, *, op="write", db, principal=None, ws=WS, run=RUN,
           content=b"data", owner="w1", now=NOW, approval_id="ap-1",
           revocation_check=None):
    principal = principal or _principal()
    return claim_granted_fs_effect(
        grant, path, operation=op, run_id=run, principal=principal,
        workspace_id=ws, approval_id=approval_id, owner_token=owner,
        content=content, revocation_check=revocation_check, db_path=db, now=now,
    )


def _issue_and_claim(grant, path, *, op="write", db, principal=None, ws=WS, run=RUN,
                     content=b"data", owner="w1", now=NOW, approval_id="ap-1",
                     revocation_check=None, expires_at=None):
    principal = principal or _principal()
    _issue_for(grant, path, op=op, db=db, principal=principal, ws=ws,
               content=content, now=now, approval_id=approval_id, expires_at=expires_at)
    return _claim(grant, path, op=op, db=db, principal=principal, ws=ws, run=run,
                  content=content, owner=owner, now=now, approval_id=approval_id,
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
    assert settle_committed(eff, p, db_path=db) == "committed"


def test_replay_does_not_win_after_commit(tmp_root, db):
    p = _principal()
    e1 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db)
    settle_committed(e1, p, db_path=db)
    # Retry reuses the SAME approval id; effect already exists -> not re-spent.
    e2 = _claim(_grant(tmp_root), "a/f.txt", db=db)
    assert e2.won is False and e2.state == "committed" and e2.effect_id == e1.effect_id


def test_concurrent_second_claim_loses_while_in_progress(tmp_root, db):
    e1 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db)
    e2 = _claim(_grant(tmp_root), "a/f.txt", db=db)  # before settle
    assert e1.won is True and e2.won is False and e2.state == "in_progress"


# ---- crash-after-effect reconciliation --------------------------------------

def test_unknown_outcome_is_not_blind_retried(tmp_root, db):
    p = _principal()
    e1 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db)
    assert settle_unknown(e1, p, db_path=db) == "unknown"  # crash before proof
    e2 = _claim(_grant(tmp_root), "a/f.txt", db=db)
    assert e2.won is False and e2.state == "unknown"


def test_reconcile_unknown_to_terminal(tmp_root, db):
    p = _principal()
    e1 = _issue_and_claim(_grant(tmp_root), "a/f.txt", db=db)
    settle_unknown(e1, p, db_path=db)
    # Out-of-band proof the effect took hold -> terminal known state.
    assert reconcile_to_terminal(e1.effect_id, p, committed=True, db_path=db) == "committed"


# ---- isolation + receipt ownership ------------------------------------------

def test_cross_workspace_is_a_distinct_effect(tmp_root, db):
    p = _principal()
    a = _issue_and_claim(_grant(tmp_root, ws="ws-A"), "f.txt", db=db, ws="ws-A", approval_id="ap-A")
    settle_committed(a, p, db_path=db)
    b = _issue_and_claim(_grant(tmp_root, ws="ws-B"), "f.txt", db=db, ws="ws-B", approval_id="ap-B")
    assert b.effect_id != a.effect_id and b.won is True


def test_cross_principal_is_a_distinct_effect(tmp_root, db):
    pa, pb = _principal("t-A", "u-A"), _principal("t-B", "u-B")
    a = _issue_and_claim(_grant(tmp_root, tenant="t-A", user="u-A"), "f.txt",
                         db=db, principal=pa, approval_id="ap-A")
    settle_committed(a, pa, db_path=db)
    b = _issue_and_claim(_grant(tmp_root, tenant="t-B", user="u-B"), "f.txt",
                         db=db, principal=pb, approval_id="ap-B")
    assert b.effect_id != a.effect_id and b.won is True


def test_foreign_receipt_rejected(tmp_root, db):
    pa, pb = _principal("t-A", "u-A"), _principal("t-B", "u-B")
    a = _issue_and_claim(_grant(tmp_root, tenant="t-A", user="u-A"), "f.txt",
                         db=db, principal=pa, approval_id="ap-A")
    settle_committed(a, pa, db_path=db)
    # Another principal can neither read nor settle the receipt.
    assert ledger.get_effect(a.effect_id, pb, db_path=db) is None
    _expect(LeaseError, lambda: settle_committed(a, pb, db_path=db))


# ---- fail-closed BEFORE any effect row / approval spend ----------------------

def test_traversal_rejected_before_registration(tmp_root, db):
    _expect(GrantScopeError, lambda: _claim(_grant(tmp_root), "../escape", db=db, approval_id="none"))
    assert _no_effects(db)


def test_revoked_grant_registers_nothing(tmp_root, db):
    _expect(GrantRevokedError, lambda: _claim(_grant(tmp_root, revoked=True), "f.txt", db=db, approval_id="none"))
    assert _no_effects(db)


def test_expired_grant_registers_nothing(tmp_root, db):
    _expect(GrantExpiredError,
            lambda: _claim(_grant(tmp_root, expires_at=NOW - 1), "f.txt", db=db, approval_id="none"))
    assert _no_effects(db)


def test_missing_permission_registers_nothing(tmp_root, db):
    _expect(GrantScopeError,
            lambda: _claim(_grant(tmp_root, perms=("read",)), "f.txt", op="write", db=db, approval_id="none"))
    assert _no_effects(db)


# ---- approval binding (single-use / expiry / digest / binding) --------------

def test_missing_approval_rejected(tmp_root, db):
    _expect(ApprovalNotFoundError, lambda: _claim(_grant(tmp_root), "f.txt", db=db, approval_id="never"))
    assert _no_effects(db)


def test_expired_approval_rejected(tmp_root, db):
    _expect(ApprovalExpiredError,
            lambda: _issue_and_claim(_grant(tmp_root), "f.txt", db=db, expires_at=NOW - 1))
    assert _no_effects(db)


def test_single_use_approval_replay_rejected(tmp_root, db):
    g = _grant(tmp_root)
    _issue_and_claim(g, "f.txt", db=db, content=b"AAA", approval_id="ap-x")
    # Reusing the spent approval for different content (a different effect) fails closed.
    _expect(ApprovalConsumedError,
            lambda: _claim(g, "f.txt", db=db, content=b"BBB", approval_id="ap-x"))


def test_same_path_different_content_needs_distinct_authority(tmp_root, db):
    g = _grant(tmp_root)
    a = _issue_and_claim(g, "f.txt", db=db, content=b"AAA", approval_id="ap-A")
    b = _issue_and_claim(g, "f.txt", db=db, content=b"BBB", approval_id="ap-B")
    assert a.effect_id != b.effect_id  # same path, different content = distinct effect


def test_approval_for_wrong_content_rejected(tmp_root, db):
    g = _grant(tmp_root)
    p = _principal()
    _issue_for(g, "f.txt", op="write", db=db, principal=p, ws=WS,
               content=b"AAA", now=NOW, approval_id="ap-A")
    _expect(ApprovalDigestMismatchError,
            lambda: _claim(g, "f.txt", db=db, content=b"BBB", approval_id="ap-A"))


def test_approval_cross_principal_rejected(tmp_root, db):
    g = _grant(tmp_root, tenant="t-A", user="u-A")
    pa = _principal("t-A", "u-A")
    _issue_for(g, "f.txt", op="write", db=db, principal=pa, ws=WS,
               content=b"data", now=NOW, approval_id="ap-A")
    gb = _grant(tmp_root, tenant="t-B", user="u-B")
    _expect(ApprovalBindingError,
            lambda: _claim(gb, "f.txt", db=db, principal=_principal("t-B", "u-B"), approval_id="ap-A"))


# ---- worker lease -----------------------------------------------------------

def test_settle_requires_lease_holder(tmp_root, db):
    eff = _issue_and_claim(_grant(tmp_root), "f.txt", db=db, owner="worker-1")
    forged = eff.__class__(eff.effect_id, eff.safe_path, eff.won, eff.state, "worker-2")
    _expect(LeaseError, lambda: settle_committed(forged, _principal(), db_path=db))


def test_expired_lease_swept_to_reconciliation(tmp_root, db):
    p = _principal()
    _issue_and_claim(_grant(tmp_root), "f.txt", db=db, owner="w1", now=NOW)  # lease ~ NOW+300
    res = sweep_expired_leases(RUN, p, now=NOW + 1000, max_attempts=3, db_path=db)
    assert res == {"reconciled": 1, "dead_lettered": 0}


def test_valid_lease_not_swept(tmp_root, db):
    p = _principal()
    _issue_and_claim(_grant(tmp_root), "f.txt", db=db, owner="w1", now=NOW)
    res = sweep_expired_leases(RUN, p, now=NOW + 10, max_attempts=3, db_path=db)
    assert res == {"reconciled": 0, "dead_lettered": 0}


def test_expired_lease_dead_lettered_at_max_attempts(tmp_root, db):
    p = _principal()
    _issue_and_claim(_grant(tmp_root), "f.txt", db=db, owner="w1", now=NOW)
    # attempts==1 after the first claim; a ceiling of 0 forces the dead-letter branch.
    res = sweep_expired_leases(RUN, p, now=NOW + 1000, max_attempts=0, db_path=db)
    assert res == {"reconciled": 0, "dead_lettered": 1}


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
    tests = sorted(
        (n, o) for n, o in globals().items() if n.startswith("test_") and callable(o)
    )
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
