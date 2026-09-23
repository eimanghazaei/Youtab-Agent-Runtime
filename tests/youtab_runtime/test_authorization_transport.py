"""Owner item 5 — the Runtime-side authorization transport correlates an inbound
signed authorization to the proposal it answers, and fails closed on an
unsolicited authorization, a changed request, or a superseded/consumed proposal.

The Runtime never mints authority; it only records the proposals it emitted and
accepts an authorization that a trusted issuer signed for a still-pending one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from youtab_runtime.authorization_transport import (
    ProposalChangedError,
    ProposalSupersededError,
    UnsolicitedAuthorizationError,
    accept_authorization,
    record_proposal,
)
from youtab_runtime.approval import (
    ApprovalBindingError,
    ApprovalConsumedError,
    ApprovalDigestMismatchError,
)
from youtab_runtime.contracts import EffectProposal
from youtab_runtime.effect_authorization import TestEffectAuthority
from youtab_runtime.run_journal import Principal

TENANT = "tenant-alpha"
USER = "user-eiman"
WS = "-"
COMMAND = "cmd-transport01"
EDIGEST = "a" * 64
NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
_EPOCH = NOW.timestamp()


def _proposal(*, proposal_id, tool_name="write_file", request_digest="b" * 64):
    return EffectProposal(
        proposal_id=proposal_id,
        command_id=COMMAND,
        task_id="task-transport1",
        tenant_id=TENANT,
        trace_id="trace-transport1",
        effect_class="write",
        tool_name=tool_name,
        arguments_digest=request_digest,
        reason="external effect requires Brain authorization",
    )


def _auth(
    signer,
    *,
    proposal_id,
    request_digest="b" * 64,
    effect_digest=EDIGEST,
    tenant_id=TENANT,
    workspace_id=WS,
    authorization_id="authz-transport-000001",
    ttl=timedelta(hours=1),
):
    return signer.mint(
        authorization_id=authorization_id,
        tenant_id=tenant_id,
        user_id=USER,
        workspace_id=workspace_id,
        command_id=COMMAND,
        capability="fs.effect",
        operation="write",
        effect_digest=effect_digest,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + ttl,
        proposal_id=proposal_id,
        request_digest=request_digest,
    )


def _accept(auth, db, **over):
    kwargs = dict(
        principal=Principal(TENANT, USER),
        workspace_id=WS,
        expected_effect_digest=EDIGEST,
        production=False,
        now=_EPOCH,
        test_keys=over.pop("test_keys"),
        db_path=db,
    )
    kwargs.update(over)
    return accept_authorization(auth, **kwargs)


def test_valid_authorization_is_accepted_and_returns_provenance(tmp_path):
    db = tmp_path / "transport.db"
    signer = TestEffectAuthority()
    record_proposal(_proposal(proposal_id="proposal-valid01"), authorization_epoch=1, db_path=db)
    auth = _auth(signer, proposal_id="proposal-valid01")
    prov = _accept(auth, db, test_keys=signer.keyring())
    assert prov.authorization_id == "authz-transport-000001"
    assert prov.proposal_id == "proposal-valid01"
    assert prov.key_id == signer.key_id
    assert prov.signature == auth.signature


def test_authorization_without_proposal_reference_is_unsolicited(tmp_path):
    db = tmp_path / "transport.db"
    signer = TestEffectAuthority()
    # No proposal_id/request_digest on the authorization -> unsolicited.
    auth = signer.mint(
        authorization_id="authz-noref-00000001", tenant_id=TENANT, user_id=USER,
        workspace_id=WS, command_id=COMMAND, capability="fs.effect", operation="write",
        effect_digest=EDIGEST, issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    with pytest.raises(UnsolicitedAuthorizationError):
        _accept(auth, db, test_keys=signer.keyring())


def test_authorization_for_never_emitted_proposal_is_unsolicited(tmp_path):
    db = tmp_path / "transport.db"
    signer = TestEffectAuthority()
    # Nothing recorded for this proposal_id.
    auth = _auth(signer, proposal_id="proposal-ghost01")
    with pytest.raises(UnsolicitedAuthorizationError):
        _accept(auth, db, test_keys=signer.keyring())


def test_changed_request_digest_is_rejected(tmp_path):
    db = tmp_path / "transport.db"
    signer = TestEffectAuthority()
    record_proposal(
        _proposal(proposal_id="proposal-chg01", request_digest="1" * 64),
        authorization_epoch=1, db_path=db,
    )
    # Same proposal_id but the authorization answers a DIFFERENT request digest.
    auth = _auth(signer, proposal_id="proposal-chg01", request_digest="2" * 64)
    with pytest.raises(ProposalChangedError):
        _accept(auth, db, test_keys=signer.keyring())


def test_superseded_proposal_is_rejected(tmp_path):
    db = tmp_path / "transport.db"
    signer = TestEffectAuthority()
    # PA then PB for the SAME (command, tool): PA is superseded by PB.
    record_proposal(
        _proposal(proposal_id="proposal-old01", request_digest="1" * 64),
        authorization_epoch=1, db_path=db,
    )
    record_proposal(
        _proposal(proposal_id="proposal-new01", request_digest="2" * 64),
        authorization_epoch=2, db_path=db,
    )
    auth_old = _auth(signer, proposal_id="proposal-old01", request_digest="1" * 64)
    with pytest.raises(ProposalSupersededError):
        _accept(auth_old, db, test_keys=signer.keyring())
    # The newer proposal is still acceptable.
    auth_new = _auth(
        signer, proposal_id="proposal-new01", request_digest="2" * 64,
        authorization_id="authz-transport-000002",
    )
    prov = _accept(auth_new, db, test_keys=signer.keyring())
    assert prov.proposal_id == "proposal-new01"


def test_binding_mismatch_is_delegated_and_fails_closed(tmp_path):
    db = tmp_path / "transport.db"
    signer = TestEffectAuthority()
    record_proposal(_proposal(proposal_id="proposal-bind01"), authorization_epoch=1, db_path=db)
    # Authorization bound to a different workspace than the caller.
    auth = _auth(signer, proposal_id="proposal-bind01", workspace_id="ws-other")
    with pytest.raises(ApprovalBindingError):
        _accept(auth, db, test_keys=signer.keyring())


def test_effect_digest_mismatch_is_delegated_and_fails_closed(tmp_path):
    db = tmp_path / "transport.db"
    signer = TestEffectAuthority()
    record_proposal(_proposal(proposal_id="proposal-dig01"), authorization_epoch=1, db_path=db)
    auth = _auth(signer, proposal_id="proposal-dig01", effect_digest="c" * 64)
    with pytest.raises(ApprovalDigestMismatchError):
        _accept(auth, db, test_keys=signer.keyring())


def test_replay_of_same_authorization_is_refused(tmp_path):
    db = tmp_path / "transport.db"
    signer = TestEffectAuthority()
    record_proposal(_proposal(proposal_id="proposal-rep01"), authorization_epoch=1, db_path=db)
    auth = _auth(signer, proposal_id="proposal-rep01")
    _accept(auth, db, test_keys=signer.keyring())
    # Second acceptance: the proposal is now consumed -> superseded/consumed refusal,
    # and even if that were bypassed the single-use authorization id is spent.
    with pytest.raises((ProposalSupersededError, ApprovalConsumedError)):
        _accept(auth, db, test_keys=signer.keyring())
