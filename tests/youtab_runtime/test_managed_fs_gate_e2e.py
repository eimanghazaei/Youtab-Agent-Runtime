"""In-process integration: managed authority chain -> filesystem effect gate.

Drives a REAL Ed25519 Simorgh grant -> admit -> sealed AdmittedCommand ->
managed_fs_gate.enforce_managed_fs_effect against a temp workspace + temp ledger
db, with the full positive/negative matrix. Identity flows only from the verified
grant. No dispatcher/HTTP/model/subprocess. pytest-collected; standalone-runnable.

Promotes the folder-grant/authorization/lease/receipt spine from unit-only to
"exercised through the admitted-grant chokepoint". The remaining production wiring
(routing the file/terminal tools through this gate) is ADR-gated and reported as
IMPLEMENTED_NOT_WIRED — see the Lane-1 report.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from youtab_runtime import effect_ledger as ledger  # noqa: E402
from youtab_runtime.approval import compute_effect_digest, content_digest  # noqa: E402
from youtab_runtime.contracts import BrainCommandEnvelopeV2  # noqa: E402
from youtab_runtime.effect_authorization import (  # noqa: E402
    InvalidAuthorizationSignatureError,
    TestEffectAuthority,
    UntrustedIssuerError,
)
from youtab_runtime.approval import ApprovalBindingError, ApprovalDigestMismatchError  # noqa: E402
from youtab_runtime.folder_grant import GrantScopeError, _canonical, _is_within, resolve_within_grant  # noqa: E402
from youtab_runtime.managed_execution import (  # noqa: E402
    ManagedAdmissionError,
    re_admit_worker_grant,
)
from youtab_runtime.managed_fs_gate import (  # noqa: E402
    ManagedFsGateError,
    enforce_managed_fs_effect,
    settle,
)
from youtab_runtime.policy import AuthorityBoundary  # noqa: E402
from youtab_runtime.run_journal import Principal  # noqa: E402
from youtab_runtime.worker_lease import LeaseError  # noqa: E402

KEY_ID = "brain-ed25519-gate"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([13]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_KEYS = {KEY_ID: _PUB}
_NOW_DT = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
NOW = _NOW_DT.timestamp()
TENANT, USER, WS = "tenant-alpha", "user-eiman", "-"
RUN = "run-gate01234"
AUTH = TestEffectAuthority()


def _grant_header(*, signer=_SIGNER, tenant=TENANT, user=USER, ws=WS,
                  nonce="grant-gate-0123456789abcdef0123", **over):
    fields = dict(
        schema_version="youtab.agent-command.v2", issuer="youtab-one-brain",
        audience="youtab-agent-runtime", protocol_version="youtab.runtime-sig.v2",
        command_id="cmd-gate01234", task_id="task-gate0123", root_run_id=RUN,
        parent_task_id=None, attempt=1, tenant_id=tenant, workspace_id=ws, user_id=user,
        membership_generation=1, authorization_epoch=1, agent_id="agent-default",
        engine_id="engine-local", trace_id="trace-gate01", nonce=nonce,
        objective="do the thing", allowed_toolsets=("*",), allowed_memory_scopes=(),
        allowed_artifact_scopes=(), effect_proposal_scopes=(),
        reasoning={"max_iterations": 5, "max_spawn_depth": 1, "max_concurrent_agents": 1,
                   "max_total_tokens": 1000, "max_cost_micros": 0, "max_retries": 0,
                   "deadline_at": _NOW_DT + timedelta(minutes=20)},
        issued_at=_NOW_DT, expires_at=_NOW_DT + timedelta(minutes=30),
        key_id=KEY_ID, signature="0" * 88,
    )
    fields.update(over)
    env = BrainCommandEnvelopeV2(**fields)
    sig = base64.b64encode(signer.sign(env.canonical_payload())).decode()
    grant = env.model_dump(mode="json")
    grant["signature"] = sig
    return base64.b64encode(json.dumps(grant, separators=(",", ":")).encode()).decode()


def _admit(**over):
    return re_admit_worker_grant(
        grant_header=_grant_header(**over), boundary=AuthorityBoundary(),
        public_keys=_KEYS, now=_NOW_DT,
    )


def _safe(workspace_root, path, *, op="write", ws=WS, tenant=TENANT, user=USER):
    from youtab_runtime.folder_grant import FolderGrant

    g = FolderGrant(grant_id="cmd-gate01234", tenant_id=tenant, principal_id=user,
                    workspace_id=ws, canonical_root=str(workspace_root),
                    permissions=frozenset({op}))
    return resolve_within_grant(g, path, operation=op, tenant_id=tenant,
                                principal_id=user, workspace_id=ws, now=NOW)


def _authz(workspace_root, path, *, op="write", content=b"data", ws=WS, tenant=TENANT,
           user=USER, auth_id="az-gate-0000000001", expires_in=1000.0, authority=AUTH):
    safe = _safe(workspace_root, path, op=op, ws=ws, tenant=tenant, user=user)
    ed = compute_effect_digest(op, safe, ws, content_digest(content))
    return authority.mint(
        authorization_id=auth_id, tenant_id=tenant, user_id=user, workspace_id=ws,
        command_id="cmd-gate01234", capability="fs", operation=op, effect_digest=ed,
        issued_at=_NOW_DT, expires_at=_NOW_DT + timedelta(seconds=expires_in),
    )


def _enforce(admitted, workspace_root, path, *, op="write", content=b"data", az, db,
             owner="w1", now=NOW):
    return enforce_managed_fs_effect(
        admitted, operation=op, requested_path=path, workspace_root=workspace_root,
        run_id=RUN, authorization=az, owner_token=owner, content=content,
        production=False, test_authority_keys=AUTH.keyring(), db_path=db, now=now,
    )


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


def _make_reparse(link, target):
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


# ---- positive ---------------------------------------------------------------

def test_governed_write_then_read(tmp_root, db):
    admitted = _admit()
    az = _authz(tmp_root, "data.txt", content=b"hello-governed")
    fd, eff = _enforce(admitted, tmp_root, "data.txt", content=b"hello-governed", az=az, db=db)
    assert fd is not None and eff.won is True
    try:
        os.write(fd, b"hello-governed")
    finally:
        os.close(fd)
    assert settle(eff, admitted, committed=True, db_path=db) == "committed"
    on_disk = Path(tmp_root, "data.txt").read_bytes()
    assert on_disk == b"hello-governed"
    # governed read of the same file
    raz = _authz(tmp_root, "data.txt", op="read", content=b"", auth_id="az-gate-read-0001")
    rfd, reff = _enforce(admitted, tmp_root, "data.txt", op="read", content=b"", az=raz, db=db)
    assert rfd is not None
    try:
        assert os.read(rfd, 64) == b"hello-governed"
    finally:
        os.close(rfd)
    settle(reff, admitted, committed=True, db_path=db)


# ---- admission / authority negatives (no side effect) -----------------------

def test_forged_grant_rejected_at_admission(tmp_root, db):
    wrong = Ed25519PrivateKey.from_private_bytes(bytes([99]) * 32)
    _expect(ManagedAdmissionError, lambda: _admit(signer=wrong))


def test_missing_admitted_context_fails_closed(tmp_root, db):
    az = _authz(tmp_root, "f.txt")
    _expect(ManagedFsGateError, lambda: _enforce(None, tmp_root, "f.txt", az=az, db=db))
    assert not Path(tmp_root, "f.txt").exists() and _no_effects(db)


def test_untrusted_authority_no_side_effect(tmp_root, db):
    admitted = _admit()
    rogue = TestEffectAuthority(key_id="test-authority:rogue")
    az = _authz(tmp_root, "f.txt", authority=rogue)  # signed by an unregistered key
    _expect(UntrustedIssuerError, lambda: _enforce(admitted, tmp_root, "f.txt", az=az, db=db))
    assert not Path(tmp_root, "f.txt").exists() and _no_effects(db)


def test_expired_authorization_no_side_effect(tmp_root, db):
    admitted = _admit()
    az = _authz(tmp_root, "f.txt", expires_in=-1.0)
    _expect(InvalidAuthorizationSignatureError, lambda: _enforce(admitted, tmp_root, "f.txt", az=az, db=db))
    assert not Path(tmp_root, "f.txt").exists() and _no_effects(db)


def test_wrong_workspace_authorization_rejected(tmp_root, db):
    admitted = _admit()  # grant workspace "-"
    az = _authz(tmp_root, "f.txt", ws="ws-OTHER")
    _expect(ApprovalBindingError, lambda: _enforce(admitted, tmp_root, "f.txt", az=az, db=db))
    assert _no_effects(db)


def test_wrong_tenant_authorization_rejected(tmp_root, db):
    admitted = _admit()  # tenant-alpha
    az = _authz(tmp_root, "f.txt", tenant="tenant-beta")
    _expect(ApprovalBindingError, lambda: _enforce(admitted, tmp_root, "f.txt", az=az, db=db))
    assert _no_effects(db)


def test_wrong_content_digest_rejected(tmp_root, db):
    admitted = _admit()
    az = _authz(tmp_root, "f.txt", content=b"AAA")
    _expect(ApprovalDigestMismatchError,
            lambda: _enforce(admitted, tmp_root, "f.txt", content=b"BBB", az=az, db=db))
    assert not Path(tmp_root, "f.txt").exists() and _no_effects(db)


# ---- idempotency / lifecycle / isolation -----------------------------------

def test_replay_same_request_no_double_write(tmp_root, db):
    admitted = _admit()
    az = _authz(tmp_root, "f.txt", content=b"once")
    fd, eff = _enforce(admitted, tmp_root, "f.txt", content=b"once", az=az, db=db)
    os.write(fd, b"once")
    os.close(fd)
    settle(eff, admitted, committed=True, db_path=db)
    az2 = _authz(tmp_root, "f.txt", content=b"once", auth_id="az-gate-replay-2")
    fd2, eff2 = _enforce(admitted, tmp_root, "f.txt", content=b"once", az=az2, db=db)
    assert fd2 is None and eff2.won is False and eff2.state == "committed"


def test_crash_after_effect_not_blind_retried(tmp_root, db):
    admitted = _admit()
    az = _authz(tmp_root, "f.txt", content=b"x")
    fd, eff = _enforce(admitted, tmp_root, "f.txt", content=b"x", az=az, db=db)
    os.close(fd)
    settle(eff, admitted, committed=False, db_path=db)  # crash before proof -> unknown
    az2 = _authz(tmp_root, "f.txt", content=b"x", auth_id="az-gate-crash-002")
    fd2, eff2 = _enforce(admitted, tmp_root, "f.txt", content=b"x", az=az2, db=db)
    assert fd2 is None and eff2.state == "unknown"


def test_foreign_principal_cannot_settle(tmp_root, db):
    admitted = _admit()
    az = _authz(tmp_root, "f.txt", content=b"y")
    fd, eff = _enforce(admitted, tmp_root, "f.txt", content=b"y", az=az, db=db)
    os.close(fd)
    other = _admit(tenant="tenant-beta", user="user-x", nonce="grant-gate-beta-000000000000")
    _expect(LeaseError, lambda: settle(eff, other, committed=True, db_path=db))


def test_symlink_swap_rejected_at_gate(tmp_root, db):
    admitted = _admit()
    inside = os.path.join(tmp_root, "d")
    os.makedirs(inside, exist_ok=True)
    with open(os.path.join(inside, "f.txt"), "wb") as fh:
        fh.write(b"inside")
    # validate + mint authority against the legitimate path BEFORE the swap
    _safe(tmp_root, "d/f.txt", op="read")
    az = _authz(tmp_root, "d/f.txt", op="read", content=b"", auth_id="az-gate-symlink-01")
    outside = tempfile.mkdtemp(prefix="gate-out-")
    with open(os.path.join(outside, "f.txt"), "wb") as fh:
        fh.write(b"OUT")
    import shutil
    shutil.rmtree(inside)
    if not _make_reparse(inside, outside):
        assert not _is_within(_canonical(tmp_root), _canonical(os.path.join(outside, "f.txt")))
        return
    # the gate re-resolves at the op boundary and fails closed on the junction
    _expect(GrantScopeError,
            lambda: _enforce(admitted, tmp_root, "d/f.txt", op="read", content=b"", az=az, db=db))


def _run_standalone() -> int:
    tests = sorted((n, o) for n, o in globals().items()
                   if n.startswith("test_") and callable(o))
    passed = failed = 0
    for name, fn in tests:
        root = tempfile.mkdtemp(prefix="gate-root-")
        dbfd, dbpath = tempfile.mkstemp(prefix="gate-", suffix=".sqlite3")
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
