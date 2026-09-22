"""In-process unit coverage for the managed filesystem router (ADR-0005 positive
path). The real production entrypoint (registered tool → tool_executor → router)
is proven cross-process in test_managed_fs_router_subprocess; this suite exercises
the router's state machine directly and fast: proposal-on-no-authorization,
authorized local write + canonical receipt, grant-bound read, remote fail-closed,
and replay at-most-once.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from youtab_runtime import managed_execution as mx
from youtab_runtime import managed_fs_router as router
from youtab_runtime.approval import compute_effect_digest, content_digest
from youtab_runtime.effect_authorization import TestEffectAuthority
from youtab_runtime.effect_ledger import get_effect_in_workspace
from youtab_runtime.folder_grant import FolderGrant, resolve_within_grant
from youtab_runtime.managed_remote_executor import (
    MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE,
)
from youtab_runtime.policy import AuthorityBoundary
from youtab_runtime.run_journal import Principal

from .helpers import keypair, signed_envelope

WS = "-"


class _Agent:
    pass


def _admitted(nonce):
    private, public = keypair()
    env = signed_envelope(private, allowed_toolsets=("*",), nonce=nonce)
    return AuthorityBoundary().admit(env, public)


def _agent(tmp_path, admitted, *, backend="local"):
    a = _Agent()
    a._admitted_command = admitted
    a._managed_workspace_root = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    a._managed_effects_db = tmp_path / "effects.db"
    a._managed_fs_backend = backend
    a._managed_authority_production = False
    a._managed_lease_token = "lease-router-0001"
    return a


def _managed(monkeypatch):
    monkeypatch.setattr(mx, "current_trust_mode", lambda: mx.TrustMode.MANAGED)


def _proposal_ref(final_args):
    args = json.dumps(final_args, sort_keys=True, separators=(",", ":"), default=str).encode()
    request_digest = hashlib.sha256(args).hexdigest()
    return f"proposal-{request_digest[:24]}", request_digest


def _mint_write_auth(agent, admitted, tmp_path, requested_path, content):
    env = admitted.envelope
    grant = FolderGrant(
        grant_id=env.command_id, tenant_id=env.tenant_id, principal_id=env.user_id,
        workspace_id=WS, canonical_root=agent._managed_workspace_root,
        permissions=frozenset({"write"}),
    )
    safe = resolve_within_grant(grant, requested_path, operation="write",
                                tenant_id=env.tenant_id, principal_id=env.user_id,
                                workspace_id=WS)
    edigest = compute_effect_digest("write", safe, WS, content_digest(content))
    final_args = {"path": requested_path, "content": content.decode()}
    proposal_id, request_digest = _proposal_ref(final_args)
    signer = TestEffectAuthority()
    now = datetime.now(UTC)
    auth = signer.mint(
        authorization_id="authz-router-write-01", tenant_id=env.tenant_id,
        user_id=env.user_id, workspace_id=WS, command_id=env.command_id,
        capability="fs.effect", operation="write", effect_digest=edigest,
        issued_at=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
        proposal_id=proposal_id, request_digest=request_digest,
    )
    agent._managed_test_authority_keys = signer.keyring()
    return auth, final_args


def test_write_without_authorization_emits_proposal_zero_side_effect(tmp_path, monkeypatch):
    _managed(monkeypatch)
    admitted = _admitted("nonce-router-0000000000000001")
    agent = _agent(tmp_path, admitted)
    final_args = {"path": "data.txt", "content": "hello"}
    outcome = router.route_managed_file_tool(agent, "write_file", final_args, task_id="task-00000001")
    assert outcome is not None and outcome.blocked is True
    assert "EffectAuthorization" in outcome.reason
    assert not (tmp_path / "workspace" / "data.txt").exists()


def test_authorized_write_executes_and_receipts(tmp_path, monkeypatch):
    _managed(monkeypatch)
    admitted = _admitted("nonce-router-0000000000000002")
    agent = _agent(tmp_path, admitted)
    content = b"authorized router write"
    auth, final_args = _mint_write_auth(agent, admitted, tmp_path, "data.txt", content)
    # Emit the proposal first (as the no-auth path would), then attach the auth.
    router.route_managed_file_tool(agent, "write_file", final_args, task_id="task-00000001")
    agent._effect_authorizations = [auth]
    outcome = router.route_managed_file_tool(agent, "write_file", final_args, task_id="task-00000001")
    assert outcome is not None and outcome.blocked is False
    body = json.loads(outcome.result)
    assert body["status"] == "ok" and body["bytes_written"] == len(content)
    assert (tmp_path / "workspace" / "data.txt").read_bytes() == content
    rec = get_effect_in_workspace(outcome.effect_id, Principal(admitted.envelope.tenant_id,
                                  admitted.envelope.user_id), WS, db_path=agent._managed_effects_db)
    assert rec is not None and rec.state.value == "committed"
    assert body["receipt"]["authorization_id"] == "authz-router-write-01"


def test_grant_bound_read(tmp_path, monkeypatch):
    _managed(monkeypatch)
    admitted = _admitted("nonce-router-0000000000000003")
    agent = _agent(tmp_path, admitted)
    (tmp_path / "workspace" / "r.txt").write_bytes(b"line1\nline2")
    outcome = router.route_managed_file_tool(agent, "read_file", {"path": "r.txt"}, task_id="task-00000001")
    assert outcome is not None and outcome.blocked is False
    body = json.loads(outcome.result)
    assert body["operation"] == "read"
    assert "1|line1" in body["content"] and "2|line2" in body["content"]


def test_read_outside_grant_refused(tmp_path, monkeypatch):
    _managed(monkeypatch)
    admitted = _admitted("nonce-router-0000000000000004")
    agent = _agent(tmp_path, admitted)
    outcome = router.route_managed_file_tool(agent, "read_file", {"path": "../escape.txt"}, task_id="task-00000001")
    assert outcome is not None and outcome.blocked is True


def test_remote_backend_fails_closed(tmp_path, monkeypatch):
    _managed(monkeypatch)
    admitted = _admitted("nonce-router-0000000000000005")
    agent = _agent(tmp_path, admitted, backend="docker")
    content = b"x"
    auth, final_args = _mint_write_auth(agent, admitted, tmp_path, "data.txt", content)
    agent._effect_authorizations = [auth]
    outcome = router.route_managed_file_tool(agent, "write_file", final_args, task_id="task-00000001")
    assert outcome is not None and outcome.blocked is True
    assert outcome.reason == MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE
    assert not (tmp_path / "workspace" / "data.txt").exists()


def test_replay_does_not_double_write(tmp_path, monkeypatch):
    _managed(monkeypatch)
    admitted = _admitted("nonce-router-0000000000000006")
    agent = _agent(tmp_path, admitted)
    content = b"once and only once"
    auth, final_args = _mint_write_auth(agent, admitted, tmp_path, "data.txt", content)
    router.route_managed_file_tool(agent, "write_file", final_args, task_id="task-00000001")
    agent._effect_authorizations = [auth]
    first = router.route_managed_file_tool(agent, "write_file", final_args, task_id="task-00000001")
    assert first.blocked is False
    target = tmp_path / "workspace" / "data.txt"
    assert target.read_bytes() == content
    # Replay the same authorization: must not write a second time / double-commit.
    second = router.route_managed_file_tool(agent, "write_file", final_args, task_id="task-00000001")
    assert second.blocked is True  # single-use authorization / consumed proposal
    assert target.read_bytes() == content


def test_non_managed_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mx, "current_trust_mode", lambda: mx.TrustMode.LOCAL_STANDALONE)
    admitted = _admitted("nonce-router-0000000000000007")
    agent = _agent(tmp_path, admitted)
    assert router.route_managed_file_tool(agent, "write_file", {"path": "x", "content": "y"},
                                          task_id="task-00000001") is None


def test_non_file_tool_returns_none(tmp_path, monkeypatch):
    _managed(monkeypatch)
    admitted = _admitted("nonce-router-0000000000000008")
    agent = _agent(tmp_path, admitted)
    assert router.route_managed_file_tool(agent, "terminal", {"cmd": "ls"},
                                          task_id="task-00000001") is None
