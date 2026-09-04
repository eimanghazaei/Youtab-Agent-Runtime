"""Tests for the WAVE-26 egress audit boundary (contracts 3, 6, 8).

Focus:
  * authorize emits an authorize/deny decision BEFORE any network side effect;
  * the boundary fails CLOSED when policy state is unavailable;
  * the network-deny posture blocks every non-allowlisted destination;
  * attempt/outcome records carry byte counts + digests, never payload bytes.
"""

from __future__ import annotations

import pytest

from youtab_runtime import egress_audit as ea
from youtab_runtime.egress_audit import (
    AuditDecision,
    DestClass,
    EgressAuditError,
    EgressPolicy,
    authorize,
    record_attempt,
    record_outcome,
)
from youtab_runtime.run_journal import Principal, list_events
from youtab_runtime.run_states import EgressDecision

P = Principal("tenant-a", "user-1")
Q = Principal("tenant-a", "user-2")


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "run_journal.db"


@pytest.fixture(autouse=True)
def _clean_policy():
    """Each test controls policy explicitly; never touch real env/config."""
    ea.reset_policy_provider()
    yield
    ea.reset_policy_provider()


def _open_posture(allowlist=frozenset()):
    ea.set_policy_provider(lambda: EgressPolicy(network_deny=False, allowlist=allowlist))


def _deny_posture(allowlist=frozenset()):
    ea.set_policy_provider(lambda: EgressPolicy(network_deny=True, allowlist=allowlist))


def _egress(db, run_id="run1", principal=P):
    return list_events(run_id, principal, db_path=db, category="egress", limit=5000)


# --------------------------------------------------------------------------- #
# authorize decision + ordering
# --------------------------------------------------------------------------- #
def test_public_ip_authorized_and_recorded_before_side_effect(db):
    _open_posture()
    d = authorize("https://93.184.216.34/path", "test_adapter", "run1", P, db_path=db)
    assert d.allowed is True
    assert d.decision == EgressDecision.AUTHORIZED
    assert d.dest_class == DestClass.PUBLIC

    events = _egress(db)
    kinds = [e.kind for e in events]
    # requested is journalled first, then the authorize decision — both exist
    # BEFORE the caller performs any network side effect (no attempt yet).
    assert kinds == [
        EgressDecision.REQUESTED.value,
        EgressDecision.AUTHORIZED.value,
    ]
    assert EgressDecision.ATTEMPTED.value not in kinds


def test_metadata_ip_denied(db):
    _open_posture()
    d = authorize("http://169.254.169.254/latest/meta-data/", "aux", "run1", P, db_path=db)
    assert d.allowed is False
    assert d.dest_class == DestClass.METADATA
    kinds = [e.kind for e in _egress(db)]
    assert kinds[-1] == EgressDecision.DENIED.value


def test_loopback_denied_without_allow_private(db):
    _open_posture()
    d = authorize("http://127.0.0.1:8080/", "aux", "run1", P, db_path=db)
    assert d.dest_class == DestClass.LOOPBACK
    assert d.allowed is False


def test_unsupported_scheme_blocked(db):
    _open_posture()
    d = authorize("file:///etc/passwd", "aux", "run1", P, db_path=db)
    assert d.dest_class == DestClass.BLOCKED
    assert d.allowed is False


# --------------------------------------------------------------------------- #
# fail-closed on unavailable policy
# --------------------------------------------------------------------------- #
def test_fail_closed_when_policy_provider_raises(db):
    def _boom():
        raise RuntimeError("policy store offline")

    ea.set_policy_provider(_boom)
    d = authorize("https://93.184.216.34/", "aux", "run1", P, db_path=db)
    assert d.allowed is False
    assert d.decision == EgressDecision.DENIED
    assert d.reason == "policy_unavailable"
    # A denied decision is still durably recorded (audit trail never skipped).
    kinds = [e.kind for e in _egress(db)]
    assert kinds[-1] == EgressDecision.DENIED.value


def test_fail_closed_when_provider_returns_wrong_type(db):
    ea.set_policy_provider(lambda: "not-a-policy")
    d = authorize("https://93.184.216.34/", "aux", "run1", P, db_path=db)
    assert d.allowed is False
    assert d.reason == "policy_unavailable"


# --------------------------------------------------------------------------- #
# network-deny posture
# --------------------------------------------------------------------------- #
def test_deny_posture_blocks_non_allowlisted(db):
    _deny_posture(allowlist=frozenset({"api.allowed.example"}))
    d = authorize("https://evil.attacker.example/exfil", "aux", "run1", P, db_path=db)
    assert d.allowed is False
    assert d.reason == "network_deny_posture"


def test_deny_posture_allows_allowlisted_host_offline(db):
    # Allowlisted match short-circuits classification: no DNS required, so the
    # oracle is definitive by construction even in an offline CI sandbox.
    _deny_posture(allowlist=frozenset({"api.allowed.example"}))
    d = authorize("https://api.allowed.example/v1/x", "aux", "run1", P, db_path=db)
    assert d.allowed is True
    assert d.dest_class == DestClass.ALLOWLISTED


def test_metadata_floor_cannot_be_allowlisted(db):
    # Even if an operator/attacker allowlists the metadata IP, the metadata floor
    # is checked first, so it is classified METADATA and denied — the allowlist
    # can never re-enable a cloud-metadata endpoint.
    _deny_posture(allowlist=frozenset({"169.254.169.254"}))
    d = authorize("http://169.254.169.254/", "aux", "run1", P, db_path=db)
    assert d.dest_class == DestClass.METADATA
    assert d.allowed is False
    # Same guarantee under the open posture.
    _open_posture(allowlist=frozenset({"169.254.169.254"}))
    d2 = authorize("http://169.254.169.254/", "aux", "run2", P, db_path=db)
    assert d2.dest_class == DestClass.METADATA
    assert d2.allowed is False


# --------------------------------------------------------------------------- #
# attempt/outcome carry no payload bytes
# --------------------------------------------------------------------------- #
def test_attempt_and_outcome_record_only_metadata(db):
    _open_posture()
    d = authorize("https://93.184.216.34/submit", "aux", "run1", P, db_path=db)
    assert d.allowed
    record_attempt(d, bytes_out=1234, digest="deadbeef")
    record_outcome(d, EgressDecision.SUCCEEDED, bytes_out=1234, digest="deadbeef",
                   http_status=200)

    events = _egress(db)
    kinds = [e.kind for e in events]
    assert kinds == [
        EgressDecision.REQUESTED.value,
        EgressDecision.AUTHORIZED.value,
        EgressDecision.ATTEMPTED.value,
        EgressDecision.SUCCEEDED.value,
    ]
    attempt = next(e for e in events if e.kind == EgressDecision.ATTEMPTED.value)
    assert attempt.payload["bytes_out"] == 1234
    assert attempt.payload["digest"] == "deadbeef"
    # No raw body key anywhere in the payload.
    assert "body" not in attempt.payload
    assert "content" not in attempt.payload


def test_record_attempt_refused_for_denied_decision(db):
    _deny_posture()
    d = authorize("https://evil.attacker.example/", "aux", "run1", P, db_path=db)
    assert not d.allowed
    with pytest.raises(EgressAuditError):
        record_attempt(d, bytes_out=1)


def test_record_outcome_rejects_invalid_status(db):
    _open_posture()
    d = authorize("https://93.184.216.34/", "aux", "run1", P, db_path=db)
    with pytest.raises(EgressAuditError):
        record_outcome(d, EgressDecision.AUTHORIZED)


def test_digest_bytes_never_returns_bytes():
    n, hexd = ea.digest_bytes(b"super-secret-body")
    assert n == len(b"super-secret-body")
    assert isinstance(hexd, str) and len(hexd) == 64
    assert "super-secret-body" not in hexd
    assert ea.digest_bytes(None) == (None, None)


def test_events_are_principal_scoped(db):
    _open_posture()
    authorize("https://93.184.216.34/", "aux", "run1", P, db_path=db)
    # A different user in the same tenant cannot read run1's egress events.
    assert _egress(db, principal=Q) == []


def test_authorize_requires_principal_and_adapter(db):
    _open_posture()
    with pytest.raises(EgressAuditError):
        authorize("https://x.example/", "aux", "run1", object(), db_path=db)  # type: ignore[arg-type]
    with pytest.raises(EgressAuditError):
        authorize("https://x.example/", "  ", "run1", P, db_path=db)
