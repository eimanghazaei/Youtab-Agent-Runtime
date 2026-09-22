"""Worker-lease (item 3) and evidence-bound reconciliation (item 4) coverage.

Atomic claim CAS, lease renewal invalidating the previous token, expired-lease
reacquire with real attempt progression, dead-letter at the ceiling, sweep-once,
and reconciliation that requires verifiable evidence. pytest-collected; also
runnable standalone.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from youtab_runtime import effect_ledger as ledger  # noqa: E402
from youtab_runtime.approval import compute_effect_digest, content_digest  # noqa: E402
from youtab_runtime.effect_authorization import TestEffectAuthority  # noqa: E402
from youtab_runtime.effect_evidence import (  # noqa: E402
    ReconciliationEvidence,
    ReferenceEvidenceSigner,
)
from youtab_runtime.folder_grant import FolderGrant, resolve_within_grant  # noqa: E402
from youtab_runtime.grant_fs import claim_granted_fs_effect, settle_committed, settle_unknown  # noqa: E402
from youtab_runtime.run_journal import Principal  # noqa: E402
from youtab_runtime.run_states import EffectState  # noqa: E402
from youtab_runtime.worker_lease import (  # noqa: E402
    LeaseError,
    ReconciliationError,
    WorkspaceMismatchError,
    assert_lease_holder,
    reacquire_expired_lease,
    reconcile_to_terminal,
    renew_lease,
    sweep_expired_leases,
)

# A deterministic 64-hex sha256-shaped result digest used across evidence cases.
RESULT_DIGEST = "a" * 64
# Reference provenance is recomputation-backed: this recompute reproduces the
# evidence's own digest, which is the trusted independent check.
_RECOMPUTE_OK = lambda ev: ev.result_digest  # noqa: E731

RUN, TENANT, USER, WS, NOW = "run-1", "t-A", "u-A", "ws-1", 1_000_000.0
CMD = "cmd-00000001"
AUTH = TestEffectAuthority()


def _dt(ts):
    return datetime.fromtimestamp(ts, UTC)


def _p():
    return Principal(tenant=TENANT, user=USER)


def _grant(root):
    return FolderGrant(grant_id="g-1", tenant_id=TENANT, principal_id=USER,
                       workspace_id=WS, canonical_root=root,
                       permissions=frozenset({"read", "write", "create"}))


def _claim(grant, path, *, db, owner="w1", now=NOW, auth_id="az-0000000000000001",
           content=b"data", lease_ttl=300.0):
    p = _p()
    safe = resolve_within_grant(grant, path, operation="write", tenant_id=p.tenant,
                                principal_id=p.user, workspace_id=WS, now=now)
    ed = compute_effect_digest("write", safe, WS, content_digest(content))
    az = AUTH.mint(authorization_id=auth_id, tenant_id=TENANT, user_id=USER,
                   workspace_id=WS, command_id=CMD, capability="fs", operation="write",
                   effect_digest=ed, issued_at=_dt(now), expires_at=_dt(now + 100))
    return claim_granted_fs_effect(
        grant, path, operation="write", run_id=RUN, principal=p, workspace_id=WS,
        authorization=az, owner_token=owner, content=content, production=False,
        test_authority_keys=AUTH.keyring(), lease_ttl_seconds=lease_ttl,
        db_path=db, now=now,
    )


def _expect(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception as e:
        raise AssertionError(f"expected {exc.__name__}, got {type(e).__name__}: {e}") from e
    raise AssertionError(f"expected {exc.__name__}, but call succeeded")


# ---- atomic claim CAS -------------------------------------------------------

def test_ledger_try_claim_is_atomic_single_winner(tmp_root, db):
    # Register one AUTHORIZED effect; race N threads on try_claim -> exactly one wins.
    p = _p()
    rec = ledger.begin_effect("run-x", p, "fs.write", {"k": "v"}, db_path=db)
    eid = rec.effect_id
    barrier = threading.Barrier(8)
    wins: list[bool] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        won, _ = ledger.try_claim(eid, p, db_path=db)
        with lock:
            wins.append(won)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1 and len(wins) == 8


def test_duplicate_dispatch_second_claim_loses(tmp_root, db):
    e1 = _claim(_grant(tmp_root), "f.txt", db=db)
    e2 = _claim(_grant(tmp_root), "f.txt", db=db, auth_id="az-0000000000000002")
    assert e1.won is True and e2.won is False


# ---- lease renewal / stale holder ------------------------------------------

def test_renew_invalidates_previous_token(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db, owner="w1")
    renew_lease(eff.effect_id, p, "w1", "w2", new_expires_at=NOW + 600, db_path=db)
    _expect(LeaseError, lambda: assert_lease_holder(eff.effect_id, p, "w1", db_path=db))
    assert_lease_holder(eff.effect_id, p, "w2", db_path=db)  # new token holds


def test_stale_holder_cannot_settle_after_renew(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db, owner="w1")
    renew_lease(eff.effect_id, p, "w1", "w2", new_expires_at=NOW + 600, db_path=db)
    _expect(LeaseError, lambda: settle_committed(eff, p, workspace_id=WS, db_path=db))  # eff carries w1


def test_non_holder_cannot_renew(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db, owner="w1")
    _expect(LeaseError, lambda: renew_lease(eff.effect_id, p, "wX", "w2",
                                            new_expires_at=NOW + 600, db_path=db))


# ---- reacquire with real attempt progression -------------------------------

def test_valid_lease_cannot_be_reacquired(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db, owner="w1")  # lease exp NOW+300
    _expect(LeaseError, lambda: reacquire_expired_lease(
        eff.effect_id, p, "w2", now=NOW + 10, new_expires_at=NOW + 310, db_path=db))


def test_expired_lease_reacquire_increments_attempt(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db, owner="w1")
    r = reacquire_expired_lease(eff.effect_id, p, "w2", now=NOW + 1000,
                                new_expires_at=NOW + 1300, max_attempts=3, db_path=db)
    assert r == {"reacquired": True, "attempt": 2, "dead_lettered": False}
    assert_lease_holder(eff.effect_id, p, "w2", db_path=db)


def test_reacquire_dead_letters_after_real_increments(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db, owner="w1")  # attempt 1
    # attempt 1 -> 2 -> 3, then ceiling of 3 dead-letters (real increments).
    reacquire_expired_lease(eff.effect_id, p, "w2", now=NOW + 1000, new_expires_at=NOW + 1300, max_attempts=3, db_path=db)
    reacquire_expired_lease(eff.effect_id, p, "w3", now=NOW + 2000, new_expires_at=NOW + 2300, max_attempts=3, db_path=db)
    r = reacquire_expired_lease(eff.effect_id, p, "w4", now=NOW + 3000, new_expires_at=NOW + 3300, max_attempts=3, db_path=db)
    assert r["dead_lettered"] is True and r["reacquired"] is False
    assert ledger.get_effect(eff.effect_id, p, db_path=db).state == EffectState.FAILED


# ---- sweep-once -------------------------------------------------------------

def test_sweep_expired_lease_transitions_once(tmp_root, db):
    p = _p()
    _claim(_grant(tmp_root), "f.txt", db=db, owner="w1")
    r1 = sweep_expired_leases(RUN, p, now=NOW + 1000, max_attempts=3, db_path=db)
    r2 = sweep_expired_leases(RUN, p, now=NOW + 2000, max_attempts=3, db_path=db)
    assert r1 == {"reconciled": 1, "dead_lettered": 0}
    assert r2 == {"reconciled": 0, "dead_lettered": 0}  # already left in_progress


def test_valid_lease_not_swept(tmp_root, db):
    p = _p()
    _claim(_grant(tmp_root), "f.txt", db=db, owner="w1")
    assert sweep_expired_leases(RUN, p, now=NOW + 10, db_path=db) == {"reconciled": 0, "dead_lettered": 0}


# ---- evidence-bound reconciliation -----------------------------------------

def _digest_of(effect_id, p, db):
    return ledger.get_effect(effect_id, p, db_path=db).target_scope_digest


def _ref_evidence(effect_id, p, db, *, outcome="succeeded", observed_at=NOW + 5, ws=WS,
                  op_digest=None, result_digest=RESULT_DIGEST):
    """A REFERENCE-provenance evidence (recomputation-backed, unsigned)."""
    return ReconciliationEvidence(
        provider="reference-provider", capability="fs", capability_version="v1",
        effect_id=effect_id,
        operation_digest=op_digest if op_digest is not None else _digest_of(effect_id, p, db),
        workspace_id=ws, provider_txn_id=None, observed_terminal_state=outcome,
        observed_at=_dt(observed_at), result_digest=result_digest, provenance="reference",
        signature=None,
    )


def test_reconcile_with_valid_reference_evidence_commits(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db)
    settle_unknown(eff, p, workspace_id=WS, db_path=db)  # crash before proof
    ev = _ref_evidence(eff.effect_id, p, db, outcome="succeeded")
    assert reconcile_to_terminal(
        eff.effect_id, p, ev, workspace_id=WS, now=NOW + 10,
        recompute_reference=_RECOMPUTE_OK, db_path=db) == "committed"


def test_reconcile_with_valid_signed_live_evidence_commits(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db)
    settle_unknown(eff, p, workspace_id=WS, db_path=db)
    signer = ReferenceEvidenceSigner(provider="billing")
    ev = signer.mint(
        capability="fs", capability_version="v1", effect_id=eff.effect_id,
        workspace_id=WS, operation_digest=_digest_of(eff.effect_id, p, db),
        provider_txn_id="txn-123", observed_terminal_state="succeeded",
        observed_at=_dt(NOW + 5), result_digest=RESULT_DIGEST,
    )
    assert reconcile_to_terminal(
        eff.effect_id, p, ev, workspace_id=WS, now=NOW + 10,
        live_provider_keys=signer.keyring(), db_path=db) == "committed"


def test_reconcile_failed_outcome_is_terminal_failed(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db)
    settle_unknown(eff, p, workspace_id=WS, db_path=db)
    ev = _ref_evidence(eff.effect_id, p, db, outcome="failed")
    assert reconcile_to_terminal(
        eff.effect_id, p, ev, workspace_id=WS, now=NOW + 10,
        recompute_reference=_RECOMPUTE_OK, db_path=db) == "failed"


def test_reconcile_forged_matching_fields_rejected(tmp_root, db):
    # A reference evidence with correct-looking fields but NO recompute check
    # supplied must NOT be trusted (field equality alone is forgeable).
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db)
    settle_unknown(eff, p, workspace_id=WS, db_path=db)
    ev = _ref_evidence(eff.effect_id, p, db, outcome="succeeded")
    _expect(ReconciliationError,
            lambda: reconcile_to_terminal(eff.effect_id, p, ev, workspace_id=WS,
                                          now=NOW + 10, db_path=db))
    assert ledger.get_effect(eff.effect_id, p, db_path=db).state == EffectState.UNKNOWN


def test_reconcile_evidence_for_another_effect_rejected(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db)
    settle_unknown(eff, p, workspace_id=WS, db_path=db)
    ev = _ref_evidence("deadbeef" * 4, p, db, outcome="succeeded",
                       op_digest=_digest_of(eff.effect_id, p, db))
    _expect(ReconciliationError,
            lambda: reconcile_to_terminal(eff.effect_id, p, ev, workspace_id=WS,
                                          now=NOW + 10, recompute_reference=_RECOMPUTE_OK,
                                          db_path=db))
    assert ledger.get_effect(eff.effect_id, p, db_path=db).state == EffectState.UNKNOWN


def test_reconcile_mismatched_operation_digest_rejected(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db)
    settle_unknown(eff, p, workspace_id=WS, db_path=db)
    ev = _ref_evidence(eff.effect_id, p, db, op_digest="0" * 64)
    _expect(ReconciliationError,
            lambda: reconcile_to_terminal(eff.effect_id, p, ev, workspace_id=WS,
                                          now=NOW + 10, recompute_reference=_RECOMPUTE_OK,
                                          db_path=db))


def test_reconcile_stale_evidence_rejected(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db)
    settle_unknown(eff, p, workspace_id=WS, db_path=db)
    ev = _ref_evidence(eff.effect_id, p, db, observed_at=NOW - 10_000)  # long ago
    _expect(ReconciliationError,
            lambda: reconcile_to_terminal(eff.effect_id, p, ev, workspace_id=WS,
                                          now=NOW + 10, max_age_seconds=3600,
                                          recompute_reference=_RECOMPUTE_OK, db_path=db))


def test_reconcile_wrong_workspace_rejected(tmp_root, db):
    p = _p()
    eff = _claim(_grant(tmp_root), "f.txt", db=db)
    settle_unknown(eff, p, workspace_id=WS, db_path=db)
    ev = _ref_evidence(eff.effect_id, p, db, ws="ws-OTHER")
    _expect(WorkspaceMismatchError,
            lambda: reconcile_to_terminal(eff.effect_id, p, ev, workspace_id="ws-OTHER",
                                          now=NOW + 10, recompute_reference=_RECOMPUTE_OK,
                                          db_path=db))


def _run_standalone() -> int:
    tests = sorted((n, o) for n, o in globals().items()
                   if n.startswith("test_") and callable(o))
    passed = failed = 0
    for name, fn in tests:
        root = tempfile.mkdtemp(prefix="lease-root-")
        dbfd, dbpath = tempfile.mkstemp(prefix="lease-", suffix=".sqlite3")
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
