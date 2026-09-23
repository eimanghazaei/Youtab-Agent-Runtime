"""Signed effect-authority negatives (item 1).

Runtime verifies + single-use-consumes a signed EffectAuthorization; it never
mints production authority. Proves fail-closed on unknown issuer, wrong signer,
test authority in production, changed workspace/operation/content, expiry, and
replay of a consumed authorization. pytest-collected; also runnable standalone.
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
from youtab_runtime.approval import (  # noqa: E402
    ApprovalBindingError,
    ApprovalConsumedError,
    ApprovalDigestMismatchError,
    compute_effect_digest,
    content_digest,
)
from youtab_runtime.effect_authorization import (  # noqa: E402
    InvalidAuthorizationSignatureError,
    TestAuthorityInProductionError,
    TestEffectAuthority,
    UntrustedIssuerError,
)
from youtab_runtime.folder_grant import FolderGrant, resolve_within_grant  # noqa: E402
from youtab_runtime.grant_fs import claim_granted_fs_effect  # noqa: E402
from youtab_runtime.run_journal import Principal  # noqa: E402

RUN, TENANT, USER, WS, NOW = "run-1", "t-A", "u-A", "ws-1", 1_000_000.0
CMD = "cmd-00000001"
AUTH = TestEffectAuthority(key_id="test-authority:local")


def _dt(ts):
    return datetime.fromtimestamp(ts, UTC)


def _p():
    return Principal(tenant=TENANT, user=USER)


def _grant(root):
    return FolderGrant(grant_id="g-1", tenant_id=TENANT, principal_id=USER,
                       workspace_id=WS, canonical_root=root,
                       permissions=frozenset({"read", "write", "create"}))


def _mint(authority, safe_path, *, op="write", ws=WS, content=b"data",
          tenant=TENANT, user=USER, now=NOW, auth_id="az-0000000000000001", expires_in=100.0):
    ed = compute_effect_digest(op, safe_path, ws, content_digest(content))
    return authority.mint(
        authorization_id=auth_id, tenant_id=tenant, user_id=user, workspace_id=ws,
        command_id=CMD, capability="fs", operation=op, effect_digest=ed,
        issued_at=_dt(now), expires_at=_dt(now + expires_in),
    )


def _claim(grant, path, *, az, op="write", ws=WS, content=b"data", principal=None,
           now=NOW, db, production=False, keys=None):
    principal = principal or _p()
    return claim_granted_fs_effect(
        grant, path, operation=op, run_id=RUN, principal=principal, workspace_id=ws,
        authorization=az, owner_token="w1", content=content, production=production,
        test_authority_keys=AUTH.keyring() if keys is None else keys,
        authority_public_keys=keys if production else None, db_path=db, now=now,
    )


def _safe(grant, path, *, op="write", ws=WS, principal=None, now=NOW):
    principal = principal or _p()
    return resolve_within_grant(grant, path, operation=op, tenant_id=principal.tenant,
                                principal_id=principal.user, workspace_id=ws, now=now)


def _expect(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception as e:
        raise AssertionError(f"expected {exc.__name__}, got {type(e).__name__}: {e}") from e
    raise AssertionError(f"expected {exc.__name__}, but call succeeded")


def _no_effects(db) -> bool:
    import sqlite3
    if not os.path.exists(str(db)):
        return True
    conn = sqlite3.connect(str(db))
    try:
        r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='effects'").fetchone()
        return r is None or conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    finally:
        conn.close()


def test_valid_signed_authorization_admits(tmp_root, db):
    g = _grant(tmp_root)
    az = _mint(AUTH, _safe(g, "f.txt"))
    eff = _claim(g, "f.txt", az=az, db=db)
    assert eff.won is True


def test_unknown_issuer_rejected(tmp_root, db):
    g = _grant(tmp_root)
    other = TestEffectAuthority(key_id="test-authority:unregistered")
    az = _mint(other, _safe(g, "f.txt"))
    # keyring passed is AUTH's only -> other's key id is not trusted.
    _expect(UntrustedIssuerError, lambda: _claim(g, "f.txt", az=az, db=db))
    assert _no_effects(db)


def test_wrong_signer_same_key_id_rejected(tmp_root, db):
    g = _grant(tmp_root)
    impostor = TestEffectAuthority(key_id="test-authority:local")  # same id, different key
    az = _mint(impostor, _safe(g, "f.txt"))
    _expect(InvalidAuthorizationSignatureError, lambda: _claim(g, "f.txt", az=az, db=db))
    assert _no_effects(db)


def test_test_authority_refused_in_production(tmp_root, db):
    g = _grant(tmp_root)
    az = _mint(AUTH, _safe(g, "f.txt"))
    _expect(TestAuthorityInProductionError,
            lambda: _claim(g, "f.txt", az=az, db=db, production=True, keys={}))
    assert _no_effects(db)


def test_changed_workspace_rejected(tmp_root, db):
    g = _grant(tmp_root)
    safe = _safe(g, "f.txt")
    az = _mint(AUTH, safe, ws="ws-OTHER")  # authorization bound to a different workspace
    _expect(ApprovalBindingError, lambda: _claim(g, "f.txt", az=az, ws=WS, db=db))
    assert _no_effects(db)


def test_changed_operation_rejected(tmp_root, db):
    g = _grant(tmp_root)
    safe = _safe(g, "f.txt", op="read")
    az = _mint(AUTH, safe, op="read")  # authorized for read...
    _expect(ApprovalDigestMismatchError,
            lambda: _claim(g, "f.txt", az=az, op="write", db=db))  # ...used for write
    assert _no_effects(db)


def test_changed_content_rejected(tmp_root, db):
    g = _grant(tmp_root)
    safe = _safe(g, "f.txt")
    az = _mint(AUTH, safe, content=b"AAA")
    _expect(ApprovalDigestMismatchError,
            lambda: _claim(g, "f.txt", az=az, content=b"BBB", db=db))
    assert _no_effects(db)


def test_expired_authorization_rejected(tmp_root, db):
    g = _grant(tmp_root)
    az = _mint(AUTH, _safe(g, "f.txt"), expires_in=-1.0)
    _expect(InvalidAuthorizationSignatureError, lambda: _claim(g, "f.txt", az=az, db=db))
    assert _no_effects(db)


def test_consumed_authorization_replay_rejected(tmp_root, db):
    g = _grant(tmp_root)
    az1 = _mint(AUTH, _safe(g, "a.txt"), auth_id="az-single-use-000001")
    _claim(g, "a.txt", az=az1, db=db)  # consumes az-single-use-000001
    # Re-use the SAME authorization id for a different effect (b.txt): the
    # signature verifies but the single-use id is already spent -> fail closed.
    az2 = _mint(AUTH, _safe(g, "b.txt"), content=b"data", auth_id="az-single-use-000001")
    _expect(ApprovalConsumedError, lambda: _claim(g, "b.txt", az=az2, db=db))


def _run_standalone() -> int:
    tests = sorted((n, o) for n, o in globals().items()
                   if n.startswith("test_") and callable(o))
    passed = failed = 0
    for name, fn in tests:
        root = tempfile.mkdtemp(prefix="auth-root-")
        dbfd, dbpath = tempfile.mkstemp(prefix="auth-", suffix=".sqlite3")
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
