"""Non-forgeable reconciliation evidence (Owner item 7).

Proves that :func:`effect_evidence.verify_evidence` fails CLOSED unless a trust
root is satisfied — a verifying Ed25519 signature from a pre-trusted LIVE
provider, or an independent recomputation for a REFERENCE provider — and that
field equality alone can never drive an ambiguous effect to a terminal state.
Also exercises the end-to-end guard through
:func:`worker_lease.reconcile_to_terminal`.

pytest-collected; each db-backed case uses a UNIQUE temp sqlite db under
``tmp_path``. Also runnable standalone.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from youtab_runtime import effect_ledger as ledger  # noqa: E402
from youtab_runtime.effect_evidence import (  # noqa: E402
    EffectIdentityMismatchError,
    EvidenceWorkspaceMismatchError,
    InvalidEvidenceSignatureError,
    MissingProviderTransactionError,
    MissingSignatureError,
    OperationDigestMismatchError,
    ReconciliationEvidence,
    ReferenceDigestMismatchError,
    ReferenceEvidenceSigner,
    ReferenceRecomputeUnavailableError,
    StaleEvidenceError,
    UnknownSignerError,
    verify_evidence,
)
from youtab_runtime.run_journal import Principal  # noqa: E402
from youtab_runtime.run_states import EffectState  # noqa: E402
from youtab_runtime.worker_lease import (  # noqa: E402
    ReconciliationError,
    reconcile_to_terminal,
)

RUN, TENANT, USER, WS = "run-ev", "t-A", "u-A", "ws-1"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
OP_DIGEST = "ab" * 32  # 64-hex, shaped like a scope digest
RESULT_DIGEST = "c" * 64
CAP, CAP_V = "fs", "v1"


def _p():
    return Principal(tenant=TENANT, user=USER)


def _expect(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"expected {exc.__name__}, got {type(e).__name__}: {e}") from e
    raise AssertionError(f"expected {exc.__name__}, but call succeeded")


def _ref_evidence(*, effect_id="deadbeef" * 4, ws=WS, op_digest=OP_DIGEST,
                  outcome="succeeded", observed_at=None, result_digest=RESULT_DIGEST):
    return ReconciliationEvidence(
        provider="ref", capability=CAP, capability_version=CAP_V, effect_id=effect_id,
        workspace_id=ws, operation_digest=op_digest, provider_txn_id=None,
        observed_terminal_state=outcome, observed_at=observed_at or NOW,
        result_digest=result_digest, provenance="reference", signature=None,
    )


def _verify_ref(ev, *, effect_id=None, ws=WS, op_digest=OP_DIGEST, now=None,
                recompute=lambda ev: ev.result_digest):
    return verify_evidence(
        ev, expected_effect_id=effect_id or ev.effect_id, expected_workspace_id=ws,
        expected_operation_digest=op_digest, now=now or (NOW + timedelta(seconds=5)),
        max_age_seconds=3600.0, live_provider_keys=None, recompute_reference=recompute,
    )


# ---- pure verifier: fail-closed rejections ---------------------------------

def test_fabricated_matching_fields_live_without_signature_rejected():
    # All fields line up with the effect but the LIVE evidence is unsigned:
    # field equality alone must NOT be accepted.
    ev = ReconciliationEvidence(
        provider="billing", capability=CAP, capability_version=CAP_V,
        effect_id="deadbeef" * 4, workspace_id=WS, operation_digest=OP_DIGEST,
        provider_txn_id="txn-1", observed_terminal_state="succeeded",
        observed_at=NOW, result_digest=RESULT_DIGEST, provenance="live", signature=None,
    )
    _expect(MissingSignatureError, lambda: verify_evidence(
        ev, expected_effect_id=ev.effect_id, expected_workspace_id=WS,
        expected_operation_digest=OP_DIGEST, now=NOW + timedelta(seconds=5),
        max_age_seconds=3600.0, live_provider_keys={"billing": "x"}, recompute_reference=None))


def test_reference_without_recompute_rejected():
    ev = _ref_evidence()
    _expect(ReferenceRecomputeUnavailableError, lambda: verify_evidence(
        ev, expected_effect_id=ev.effect_id, expected_workspace_id=WS,
        expected_operation_digest=OP_DIGEST, now=NOW + timedelta(seconds=5),
        max_age_seconds=3600.0, live_provider_keys=None, recompute_reference=None))


def test_unknown_signer_rejected():
    signer = ReferenceEvidenceSigner(provider="billing")
    ev = signer.mint(capability=CAP, capability_version=CAP_V, effect_id="deadbeef" * 4,
                     workspace_id=WS, operation_digest=OP_DIGEST, provider_txn_id="txn-1",
                     observed_terminal_state="succeeded", observed_at=NOW,
                     result_digest=RESULT_DIGEST)
    # No trusted key registered for provider "billing" -> unknown signer.
    _expect(UnknownSignerError, lambda: verify_evidence(
        ev, expected_effect_id=ev.effect_id, expected_workspace_id=WS,
        expected_operation_digest=OP_DIGEST, now=NOW + timedelta(seconds=5),
        max_age_seconds=3600.0, live_provider_keys=None, recompute_reference=None))


def test_wrong_signer_key_rejected():
    signer = ReferenceEvidenceSigner(provider="billing")
    other = ReferenceEvidenceSigner(provider="billing")
    ev = signer.mint(capability=CAP, capability_version=CAP_V, effect_id="deadbeef" * 4,
                     workspace_id=WS, operation_digest=OP_DIGEST, provider_txn_id="txn-1",
                     observed_terminal_state="succeeded", observed_at=NOW,
                     result_digest=RESULT_DIGEST)
    # A key IS registered for the provider, but it belongs to a different signer.
    _expect(InvalidEvidenceSignatureError, lambda: verify_evidence(
        ev, expected_effect_id=ev.effect_id, expected_workspace_id=WS,
        expected_operation_digest=OP_DIGEST, now=NOW + timedelta(seconds=5),
        max_age_seconds=3600.0, live_provider_keys=other.keyring(), recompute_reference=None))


def test_tampered_signed_evidence_rejected():
    signer = ReferenceEvidenceSigner(provider="billing")
    ev = signer.mint(capability=CAP, capability_version=CAP_V, effect_id="deadbeef" * 4,
                     workspace_id=WS, operation_digest=OP_DIGEST, provider_txn_id="txn-1",
                     observed_terminal_state="succeeded", observed_at=NOW,
                     result_digest=RESULT_DIGEST)
    # Flip the terminal state after signing -> signature no longer covers payload.
    tampered = ev.model_copy(update={"observed_terminal_state": "failed"})
    _expect(InvalidEvidenceSignatureError, lambda: verify_evidence(
        tampered, expected_effect_id=ev.effect_id, expected_workspace_id=WS,
        expected_operation_digest=OP_DIGEST, now=NOW + timedelta(seconds=5),
        max_age_seconds=3600.0, live_provider_keys=signer.keyring(), recompute_reference=None))


def test_missing_provider_txn_id_for_live_rejected():
    # Construct a LIVE evidence with no provider_txn_id (and a placeholder sig);
    # the txn check fails closed before the signature is even examined.
    ev = ReconciliationEvidence(
        provider="billing", capability=CAP, capability_version=CAP_V,
        effect_id="deadbeef" * 4, workspace_id=WS, operation_digest=OP_DIGEST,
        provider_txn_id=None, observed_terminal_state="succeeded", observed_at=NOW,
        result_digest=RESULT_DIGEST, provenance="live", signature="0" * 64,
    )
    _expect(MissingProviderTransactionError, lambda: verify_evidence(
        ev, expected_effect_id=ev.effect_id, expected_workspace_id=WS,
        expected_operation_digest=OP_DIGEST, now=NOW + timedelta(seconds=5),
        max_age_seconds=3600.0, live_provider_keys={"billing": "x"}, recompute_reference=None))


def test_stale_evidence_rejected():
    ev = _ref_evidence(observed_at=NOW - timedelta(hours=10))
    _expect(StaleEvidenceError, lambda: _verify_ref(ev, now=NOW))


def test_future_dated_evidence_rejected():
    ev = _ref_evidence(observed_at=NOW + timedelta(hours=1))
    _expect(StaleEvidenceError, lambda: _verify_ref(ev, now=NOW))


def test_result_digest_mismatch_rejected():
    ev = _ref_evidence()
    # Recompute returns a DIFFERENT digest than the evidence claims.
    _expect(ReferenceDigestMismatchError,
            lambda: _verify_ref(ev, recompute=lambda ev: "d" * 64))


def test_evidence_replayed_for_another_effect_rejected():
    ev = _ref_evidence(effect_id="deadbeef" * 4)
    _expect(EffectIdentityMismatchError,
            lambda: _verify_ref(ev, effect_id="feedface" * 4))


def test_wrong_workspace_rejected():
    ev = _ref_evidence(ws="ws-OTHER")
    _expect(EvidenceWorkspaceMismatchError, lambda: _verify_ref(ev, ws=WS))


def test_wrong_operation_digest_rejected():
    ev = _ref_evidence(op_digest="0" * 64)
    _expect(OperationDigestMismatchError, lambda: _verify_ref(ev, op_digest=OP_DIGEST))


# ---- pure verifier: accepted outcomes --------------------------------------

def test_valid_signed_live_evidence_verifies():
    signer = ReferenceEvidenceSigner(provider="billing")
    ev = signer.mint(capability=CAP, capability_version=CAP_V, effect_id="deadbeef" * 4,
                     workspace_id=WS, operation_digest=OP_DIGEST, provider_txn_id="txn-1",
                     observed_terminal_state="succeeded", observed_at=NOW,
                     result_digest=RESULT_DIGEST)
    out = verify_evidence(
        ev, expected_effect_id=ev.effect_id, expected_workspace_id=WS,
        expected_operation_digest=OP_DIGEST, now=NOW + timedelta(seconds=5),
        max_age_seconds=3600.0, live_provider_keys=signer.keyring(), recompute_reference=None)
    assert out.terminal_state == "succeeded"
    assert out.kind == "live"
    assert out.provider == "billing"
    assert out.result_digest == RESULT_DIGEST


def test_valid_reference_evidence_verifies_and_labelled_reference():
    ev = _ref_evidence(outcome="succeeded")
    out = _verify_ref(ev)
    assert out.terminal_state == "succeeded"
    # Reference outcome is permanently labelled reference — never promotable.
    assert out.kind == "reference"
    assert out.provider == "ref"


# ---- end-to-end reconcile guard --------------------------------------------

def _unknown_effect(db, p, *, ws=WS):
    rec = ledger.begin_effect(RUN, p, "fs.write", {"ws": ws, "path": "/x"},
                              detail={"workspace": ws}, db_path=db)
    _won, rec = ledger.try_claim(rec.effect_id, p, db_path=db)
    ledger.mark_unknown(rec.effect_id, p, db_path=db)
    return rec.effect_id, rec.target_scope_digest


def test_reconcile_valid_signed_live_commits(tmp_path):
    db = tmp_path / "ev-live.sqlite3"
    p = _p()
    eid, digest = _unknown_effect(db, p)
    signer = ReferenceEvidenceSigner(provider="billing")
    ev = signer.mint(capability=CAP, capability_version=CAP_V, effect_id=eid,
                     workspace_id=WS, operation_digest=digest, provider_txn_id="txn-1",
                     observed_terminal_state="succeeded", observed_at=NOW,
                     result_digest=RESULT_DIGEST)
    assert reconcile_to_terminal(
        eid, p, ev, workspace_id=WS, now=NOW + timedelta(seconds=10),
        live_provider_keys=signer.keyring(), db_path=db) == "committed"
    assert ledger.get_effect(eid, p, db_path=db).state == EffectState.COMMITTED


def test_reconcile_valid_reference_commits_labelled_reference(tmp_path):
    db = tmp_path / "ev-ref.sqlite3"
    p = _p()
    eid, digest = _unknown_effect(db, p)
    ev = ReconciliationEvidence(
        provider="ref", capability=CAP, capability_version=CAP_V, effect_id=eid,
        workspace_id=WS, operation_digest=digest, provider_txn_id=None,
        observed_terminal_state="succeeded", observed_at=NOW,
        result_digest=RESULT_DIGEST, provenance="reference", signature=None)
    assert reconcile_to_terminal(
        eid, p, ev, workspace_id=WS, now=NOW + timedelta(seconds=10),
        recompute_reference=lambda ev: ev.result_digest, db_path=db) == "committed"
    rec = ledger.get_effect(eid, p, db_path=db)
    assert rec.state == EffectState.COMMITTED
    assert rec.detail.get("provenance") == "reference"


def test_reconcile_unverifiable_evidence_leaves_effect_unknown(tmp_path):
    db = tmp_path / "ev-bad.sqlite3"
    p = _p()
    eid, digest = _unknown_effect(db, p)
    # Live evidence signed by an untrusted signer -> cannot be verified.
    rogue = ReferenceEvidenceSigner(provider="billing")
    ev = rogue.mint(capability=CAP, capability_version=CAP_V, effect_id=eid,
                    workspace_id=WS, operation_digest=digest, provider_txn_id="txn-1",
                    observed_terminal_state="succeeded", observed_at=NOW,
                    result_digest=RESULT_DIGEST)
    _expect(ReconciliationError, lambda: reconcile_to_terminal(
        eid, p, ev, workspace_id=WS, now=NOW + timedelta(seconds=10),
        live_provider_keys=None, db_path=db))
    assert ledger.get_effect(eid, p, db_path=db).state == EffectState.UNKNOWN


def test_reconcile_replayed_for_another_effect_leaves_unknown(tmp_path):
    db = tmp_path / "ev-replay.sqlite3"
    p = _p()
    eid_a, digest_a = _unknown_effect(db, p)
    # Genuinely-signed evidence, but bound to a DIFFERENT effect id.
    signer = ReferenceEvidenceSigner(provider="billing")
    ev = signer.mint(capability=CAP, capability_version=CAP_V, effect_id="deadbeef" * 4,
                     workspace_id=WS, operation_digest=digest_a, provider_txn_id="txn-1",
                     observed_terminal_state="succeeded", observed_at=NOW,
                     result_digest=RESULT_DIGEST)
    _expect(ReconciliationError, lambda: reconcile_to_terminal(
        eid_a, p, ev, workspace_id=WS, now=NOW + timedelta(seconds=10),
        live_provider_keys=signer.keyring(), db_path=db))
    assert ledger.get_effect(eid_a, p, db_path=db).state == EffectState.UNKNOWN


def _run_standalone() -> int:
    import inspect

    tests = sorted((n, o) for n, o in globals().items()
                   if n.startswith("test_") and callable(o))
    passed = failed = 0
    for name, fn in tests:
        needs_tmp = "tmp_path" in inspect.signature(fn).parameters
        tmp = Path(tempfile.mkdtemp(prefix="ev-")) if needs_tmp else None
        try:
            fn(tmp) if needs_tmp else fn()
            print(f"PASS {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
