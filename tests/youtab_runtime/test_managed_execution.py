"""Managed-execution trust mode + Simorgh grant admission (WAVE-30H R3).

Proves the mandatory managed-run grant admission fails CLOSED on every bad
grant (missing / malformed / untrusted key / bad signature / expired / replayed
/ identity or workspace mismatch / store unavailable), admits a valid grant into
a sealed AdmittedCommand, and that the local-standalone mode refuses managed
grants. Test Ed25519 keys only.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from youtab_runtime.contracts import BrainCommandEnvelopeV2
from youtab_runtime.managed_execution import (
    AdmissionIdentity,
    ManagedAdmissionError,
    TrustMode,
    admit_managed_run,
    current_trust_mode,
    decode_grant_header,
    load_brain_public_keys,
    re_admit_worker_grant,
    reject_grant_in_standalone,
)
from youtab_runtime.policy import AuthorityBoundary

KEY_ID = "brain-ed25519-test"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_OTHER_PUB = base64.b64encode(
    Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32).public_key().public_bytes_raw()
).decode()

_NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


def _mint(*, signer=_SIGNER, nonce="grant-abcdef0123456789abcdef01234567", **overrides):
    fields = dict(
        schema_version="youtab.agent-command.v2",
        issuer="youtab-one-brain",
        audience="youtab-agent-runtime",
        protocol_version="youtab.runtime-sig.v2",
        command_id="cmd-abcdef012345",
        task_id="task-abcdef012345",
        root_run_id="run-abcdef012345",
        parent_task_id=None,
        attempt=1,
        tenant_id="tenant-alpha",
        workspace_id="-",
        user_id="user-eiman",
        membership_generation=1,
        authorization_epoch=1,
        agent_id="agent-default",
        engine_id="engine-local",
        trace_id="trace-abcdef012345",
        nonce=nonce,
        objective="do the thing",
        allowed_toolsets=("web_search",),
        allowed_memory_scopes=(),
        allowed_artifact_scopes=(),
        effect_proposal_scopes=(),
        reasoning={
            "max_iterations": 5,
            "max_spawn_depth": 1,
            "max_concurrent_agents": 1,
            "max_total_tokens": 1000,
            "max_cost_micros": 0,
            "max_retries": 0,
            "deadline_at": _NOW + timedelta(minutes=20),
        },
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=30),
        key_id=KEY_ID,
        signature="0" * 88,
    )
    fields.update(overrides)
    env = BrainCommandEnvelopeV2(**fields)
    sig = base64.b64encode(signer.sign(env.canonical_payload())).decode()
    grant = env.model_dump(mode="json")
    grant["signature"] = sig
    header = base64.b64encode(
        json.dumps(grant, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    return header


def _keys():
    return {KEY_ID: _PUB}


def _identity():
    return AdmissionIdentity(tenant="tenant-alpha", user="user-eiman", workspace="-")


def _boundary():
    return AuthorityBoundary()  # in-process nonce claim is fine for unit tests


# --- trust mode -----------------------------------------------------------

def test_trust_mode_defaults_to_standalone():
    assert current_trust_mode({}) is TrustMode.LOCAL_STANDALONE


def test_trust_mode_managed_opt_in():
    assert current_trust_mode({"YOUTAB_RUNTIME_TRUST_MODE": "managed"}) is TrustMode.MANAGED


def test_unknown_trust_mode_fails_closed():
    with pytest.raises(ManagedAdmissionError) as ei:
        current_trust_mode({"YOUTAB_RUNTIME_TRUST_MODE": "loose"})
    assert ei.value.http_status == 500


# --- keyring --------------------------------------------------------------

def test_public_keyring_parses():
    ring = load_brain_public_keys({"YOUTAB_BRAIN_PUBLIC_KEYS": json.dumps(_keys())})
    assert ring[KEY_ID] == _PUB


def test_public_keyring_empty_is_empty():
    assert load_brain_public_keys({}) == {}


def test_public_keyring_malformed_fails_closed():
    with pytest.raises(ManagedAdmissionError) as ei:
        load_brain_public_keys({"YOUTAB_BRAIN_PUBLIC_KEYS": "not json"})
    assert ei.value.http_status == 503


# --- happy path -----------------------------------------------------------

def test_valid_grant_admits_to_sealed_command():
    admitted = admit_managed_run(
        grant_header=_mint(),
        identity=_identity(),
        boundary=_boundary(),
        public_keys=_keys(),
        now=_NOW,
    )
    assert admitted.tenant_id == "tenant-alpha"
    # The sealed context is usable for a tool decision (allowed toolset).
    from youtab_runtime.policy import EffectClass, ToolIntent

    decision = _boundary().decide_tool(
        admitted,
        ToolIntent(
            tool_name="web_search", toolset="web_search",
            effect_class=EffectClass.READ, arguments={},
        ),
    )
    assert decision.execute_in_runtime is True


# --- fail-closed proofs (Owner-required) ----------------------------------

def test_missing_grant_fails_closed():
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=None, identity=_identity(),
                          boundary=_boundary(), public_keys=_keys(), now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("grant_required", 401)


def test_no_public_keys_fails_closed():
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=_mint(), identity=_identity(),
                          boundary=_boundary(), public_keys={}, now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("authority_unavailable", 503)


def test_unauthorized_grant_wrong_key_fails_closed():
    # Grant signed by the real signer, but the engine trusts a DIFFERENT key.
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=_mint(), identity=_identity(),
                          boundary=_boundary(), public_keys={KEY_ID: _OTHER_PUB}, now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("grant_rejected", 401)


def test_forged_grant_signed_by_untrusted_signer_fails_closed():
    forged = _mint(signer=Ed25519PrivateKey.from_private_bytes(bytes([42]) * 32))
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=forged, identity=_identity(),
                          boundary=_boundary(), public_keys=_keys(), now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("grant_rejected", 401)


def test_expired_grant_fails_closed():
    later = _NOW + timedelta(hours=1)  # past expires_at (_NOW + 30m)
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=_mint(), identity=_identity(),
                          boundary=_boundary(), public_keys=_keys(), now=later)
    assert (ei.value.code, ei.value.http_status) == ("grant_rejected", 401)


def test_unknown_key_id_fails_closed():
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=_mint(key_id="rotated-out"),
                          identity=_identity(), boundary=_boundary(),
                          public_keys=_keys(), now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("unknown_grant_key", 401)


def test_identity_mismatch_fails_closed():
    ident = AdmissionIdentity(tenant="tenant-beta", user="user-eiman")
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=_mint(), identity=ident,
                          boundary=_boundary(), public_keys=_keys(), now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("grant_identity_mismatch", 403)


def test_workspace_mismatch_fails_closed():
    ident = AdmissionIdentity(tenant="tenant-alpha", user="user-eiman", workspace="ws-x")
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=_mint(), identity=ident,
                          boundary=_boundary(), public_keys=_keys(), now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("grant_workspace_mismatch", 403)


def test_malformed_grant_header_fails_closed():
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header="!!!not-base64!!!", identity=_identity(),
                          boundary=_boundary(), public_keys=_keys(), now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("grant_malformed", 400)


def test_replayed_grant_fails_closed():
    boundary = _boundary()  # SAME boundary -> shared nonce store
    header = _mint()
    admit_managed_run(grant_header=header, identity=_identity(),
                      boundary=boundary, public_keys=_keys(), now=_NOW)
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=header, identity=_identity(),
                          boundary=boundary, public_keys=_keys(), now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("grant_rejected", 401)


def test_store_unavailable_fails_closed_503():
    class _BrokenStore:
        def claim(self, key, ts):
            raise RuntimeError("db down")

    boundary = AuthorityBoundary(nonce_store=_BrokenStore())
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=_mint(), identity=_identity(),
                          boundary=boundary, public_keys=_keys(), now=_NOW)
    assert (ei.value.code, ei.value.http_status) == ("authority_unavailable", 503)


def test_forbidden_toolset_in_grant_is_rejected():
    header = _mint(allowed_toolsets=("cognitive_authority",))
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=header, identity=_identity(),
                          boundary=_boundary(), public_keys=_keys(), now=_NOW)
    assert ei.value.code == "grant_rejected"


# --- standalone -----------------------------------------------------------

def test_standalone_refuses_managed_grant():
    with pytest.raises(ManagedAdmissionError) as ei:
        reject_grant_in_standalone(_mint())
    assert (ei.value.code, ei.value.http_status) == ("grant_not_accepted_in_standalone", 400)


def test_standalone_allows_absent_grant():
    reject_grant_in_standalone(None)  # must not raise
    reject_grant_in_standalone("")


# --- worker re-admission --------------------------------------------------

def test_worker_re_admits_without_double_burning_nonce():
    boundary = _boundary()
    header = _mint()
    # Ingress admission consumes the nonce.
    admit_managed_run(grant_header=header, identity=_identity(),
                      boundary=boundary, public_keys=_keys(), now=_NOW)
    # Worker re-admits the SAME grant in-process: must succeed (no replay error).
    admitted = re_admit_worker_grant(grant_header=header, boundary=boundary,
                                     public_keys=_keys(), now=_NOW)
    assert isinstance(admitted, type(admitted))
    admitted.verify_proof()  # sealed + bound to this process


def test_worker_re_admission_rejects_tampered_persisted_grant():
    boundary = _boundary()
    # A grant whose signature no longer matches its (tampered) payload.
    env = decode_grant_header(_mint())
    tampered = env.model_dump(mode="json")
    tampered["objective"] = "exfiltrate"  # signature no longer valid
    header = base64.b64encode(
        json.dumps(tampered, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    with pytest.raises(ManagedAdmissionError) as ei:
        re_admit_worker_grant(grant_header=header, boundary=boundary,
                              public_keys=_keys(), now=_NOW)
    assert ei.value.code == "grant_rejected"
