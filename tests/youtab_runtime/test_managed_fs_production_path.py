"""Production positive path: a registered file tool driven THROUGH the real
``agent.tool_executor`` authority seam (Owner items 5 + 6).

Unlike ``test_managed_fs_router`` (which calls the router directly) and
``test_managed_execution_e2e`` (which calls the authority gate directly), this
suite exercises the ACTUAL production callsite:

    agent.tool_executor._run_agent_tool_execution_middleware(
        agent, function_name="write_file"/"read_file", ...)

which internally runs the Relay pass-through, the plugin/guardrail blocks, and
then — BEFORE ``enforce_managed_tool_authority`` — invokes
``youtab_runtime.managed_fs_router.route_managed_file_tool`` inside its
``_authorized_dispatch`` closure. We prove that:

  * a Brain-authorized managed write really happens via grant-bound host-IO and a
    canonical receipt, and its effect is committed in the ledger;
  * the unrestricted shell backend (``ShellFileOperations._exec``) is NEVER used
    for a managed local write, and the registered tool handler (``execute``) is
    never called on the router-owned path (bypass proof);
  * unsigned / forged / changed-request / remote / replayed calls fail closed
    with zero filesystem side effect and zero committed effect;
  * the shell-bypass spy is genuinely wired: in local-standalone the same driver
    DOES reach ``execute`` and DOES hit ``ShellFileOperations._exec``.

No authority seam is mocked: the router, ``enforce_managed_tool_authority``,
``grant_fs`` and ``ShellFileOperations`` (other than the ``_exec`` call-recording
spy below) run for real. The only monkeypatching is trust-mode env + the
``_exec`` spy; the Relay / plugin / execution-middleware layers are NOT patched —
they pass through on their own because no session is bound and no plugins are
registered (verified by the tests passing).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.tool_executor import _run_agent_tool_execution_middleware
from tools.file_operations import ShellFileOperations
from tools.file_tools import read_file_tool, write_file_tool
from youtab_runtime.approval import compute_effect_digest, content_digest
from youtab_runtime.contracts import BrainCommandEnvelopeV2
from youtab_runtime.effect_authorization import EffectAuthorization, TestEffectAuthority
from youtab_runtime.effect_ledger import get_effect_in_workspace
from youtab_runtime.folder_grant import FolderGrant, resolve_within_grant
from youtab_runtime.managed_execution import re_admit_worker_grant
from youtab_runtime.managed_remote_executor import (
    MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE,
)
from youtab_runtime.policy import AuthorityBoundary
from youtab_runtime.run_journal import Principal

WS = "-"  # the envelopes below are workspace-unscoped ("-")

# The Brain signing identity for the execution GRANT (envelope), plain key id.
_GRANT_KEY_ID = "brain-ed25519-prodpath"
_GRANT_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([23]) * 32)
_GRANT_PUB = base64.b64encode(
    _GRANT_SIGNER.public_key().public_bytes_raw()
).decode()
_GRANT_KEYS = {_GRANT_KEY_ID: _GRANT_PUB}


# --------------------------------------------------------------------------- #
# Grant / admission helpers (v2 envelope, real-time window, re-admitted worker)
# --------------------------------------------------------------------------- #
def _grant_header(*, command_id: str, nonce: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    fields = dict(
        schema_version="youtab.agent-command.v2",
        issuer="youtab-one-brain",
        audience="youtab-agent-runtime",
        protocol_version="youtab.runtime-sig.v2",
        command_id=command_id,
        task_id="task-prodpath01",
        root_run_id="run-prodpath01",
        parent_task_id=None,
        attempt=1,
        tenant_id="tenant-alpha",
        workspace_id="-",
        user_id="user-eiman",
        membership_generation=1,
        authorization_epoch=1,
        agent_id="agent-default",
        engine_id="engine-local",
        trace_id="trace-prodpath01",
        nonce=nonce,
        objective="managed production write path",
        allowed_toolsets=("*",),
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
            "deadline_at": now + timedelta(minutes=20),
        },
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
        key_id=_GRANT_KEY_ID,
        signature="0" * 88,
    )
    env = BrainCommandEnvelopeV2(**fields)
    sig = base64.b64encode(_GRANT_SIGNER.sign(env.canonical_payload())).decode()
    grant = env.model_dump(mode="json")
    grant["signature"] = sig
    return base64.b64encode(json.dumps(grant, separators=(",", ":")).encode()).decode()


def _admit(command_id: str, nonce: str):
    return re_admit_worker_grant(
        grant_header=_grant_header(command_id=command_id, nonce=nonce),
        boundary=AuthorityBoundary(),
        public_keys=_GRANT_KEYS,
    )


# --------------------------------------------------------------------------- #
# A minimal agent exposing BOTH the router attributes and the middleware seams
# the real ``_run_agent_tool_execution_middleware`` reads.
# --------------------------------------------------------------------------- #
class _Guardrails:
    def before_call(self, name, args):
        return type("_D", (), {"allows_execution": True, "message": None})()


class _Checkpoint:
    enabled = False


class _Agent:
    def __init__(self, admitted, workspace_root, effects_db, *, backend="local",
                 production, prod_keys=None, test_keys=None):
        # ── router-read state ─────────────────────────────────────────────
        self._admitted_command = admitted
        self._managed_workspace_root = str(workspace_root)
        self._managed_effects_db = effects_db
        self._managed_fs_backend = backend
        self._managed_authority_production = production
        self._managed_authority_public_keys = prod_keys
        self._managed_test_authority_keys = test_keys
        self._effect_authorizations = None
        self._managed_lease_token = f"lease-{getattr(admitted, 'tenant_id', 'x')}-0001"
        # ── middleware / _begin_tool_execution seams ──────────────────────
        self.quiet_mode = True
        self.tool_progress_mode = "off"
        self.session_id = ""
        self._current_turn_id = ""
        self._current_api_request_id = ""
        self._current_tool = None
        self.tool_progress_callback = None
        self.tool_start_callback = None
        self.verbose_logging = False
        self.log_prefix_chars = 80
        self._turns_since_memory = 0
        self._iters_since_skill = 0
        self._checkpoint_mgr = _Checkpoint()
        self._tool_guardrails = _Guardrails()

    def _touch_activity(self, *a, **k):
        return None

    def _guardrail_block_result(self, decision):
        return json.dumps({"error": "guardrail"}, ensure_ascii=False)


class _ExecuteSpy:
    """Records whether the real registered-tool handler was reached, and — when
    reached — drives the REAL standalone file tool (so the positive control
    actually goes through ``ShellFileOperations``)."""

    def __init__(self, function_name):
        self._fn = function_name
        self.called = False
        self.calls = []

    def __call__(self, final_args):
        self.called = True
        self.calls.append(final_args)
        if self._fn == "write_file":
            return write_file_tool(final_args["path"], final_args.get("content", ""),
                                   task_id="default")
        return read_file_tool(final_args["path"], task_id="default")


def _drive(agent, function_name, function_args, task_id, spy):
    return _run_agent_tool_execution_middleware(
        agent,
        function_name=function_name,
        function_args=function_args,
        effective_task_id=task_id,
        tool_call_id="tc-1",
        execute=spy,
        middleware_trace=[],
        begin_execution=None,
    )


# --------------------------------------------------------------------------- #
# Effect-authorization minting (matches the router's digest + proposal ref)
# --------------------------------------------------------------------------- #
def _proposal_ref(final_args):
    args = json.dumps(final_args, sort_keys=True, separators=(",", ":"),
                      default=str).encode()
    request_digest = hashlib.sha256(args).hexdigest()
    return f"proposal-{request_digest[:24]}", request_digest


def _effect_params(admitted, workspace_root, requested_path, content):
    env = admitted.envelope
    grant = FolderGrant(
        grant_id=env.command_id, tenant_id=env.tenant_id, principal_id=env.user_id,
        workspace_id=WS, canonical_root=str(workspace_root),
        permissions=frozenset({"write"}),
    )
    safe = resolve_within_grant(grant, requested_path, operation="write",
                                tenant_id=env.tenant_id, principal_id=env.user_id,
                                workspace_id=WS)
    edigest = compute_effect_digest("write", safe, WS, content_digest(content))
    final_args = {"path": requested_path, "content": content.decode()}
    proposal_id, request_digest = _proposal_ref(final_args)
    return edigest, proposal_id, request_digest, final_args


def _sign_effect_auth(signer, key_id, issuer, *, authorization_id, admitted,
                      effect_digest, proposal_id, request_digest,
                      operation="write"):
    """Sign an EffectAuthorization with an arbitrary Ed25519 key (production-style,
    no test prefix) — mirrors the grant signing in test_managed_execution_e2e."""
    env = admitted.envelope
    now = datetime.now(UTC)
    unsigned = EffectAuthorization(
        issuer=issuer, authorization_id=authorization_id, tenant_id=env.tenant_id,
        user_id=env.user_id, workspace_id=WS, command_id=env.command_id,
        capability="fs.effect", operation=operation, effect_digest=effect_digest,
        issued_at=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
        proposal_id=proposal_id, request_digest=request_digest,
        key_id=key_id, signature="0" * 64,
    )
    sig = base64.b64encode(signer.sign(unsigned.canonical_payload())).decode()
    return unsigned.model_copy(update={"signature": sig})


@pytest.fixture
def managed_env(monkeypatch):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")


@pytest.fixture
def exec_spy_disabled(monkeypatch):
    """Record every ``ShellFileOperations._exec`` invocation. Managed writes must
    NEVER hit this shell seam (they use grant-bound host-IO)."""
    spy = MagicMock(name="ShellFileOperations._exec")
    monkeypatch.setattr(ShellFileOperations, "_exec", spy)
    return spy


# =========================================================================== #
# 1) PRODUCTION-mode authorized write (Owner item 4): ephemeral non-test key.
# =========================================================================== #
def test_managed_write_through_tool_executor_authorized(
    tmp_path, managed_env, exec_spy_disabled
):
    ws = tmp_path / "workspace"
    ws.mkdir()
    effects_db = tmp_path / "effects.db"
    admitted = _admit("cmd-prod-write-1", "nonce-prod-write-0000001")

    # Ephemeral, NON-test production keypair with a PLAIN key id.
    prod_signer = Ed25519PrivateKey.generate()
    prod_key_id = "brain-effect-prod-01"
    prod_pub = base64.b64encode(prod_signer.public_key().public_bytes_raw()).decode()

    agent = _Agent(admitted, ws, effects_db, production=True,
                   prod_keys={prod_key_id: prod_pub})
    content = b"production authorized write via tool_executor"
    edigest, proposal_id, request_digest, final_args = _effect_params(
        admitted, ws, "data.txt", content)
    target = ws / "data.txt"
    task = "task-prod-w-1"

    # First drive: NO authorization → proposal emitted, zero side effect.
    spy = _ExecuteSpy("write_file")
    res1 = _drive(agent, "write_file", dict(final_args), task, spy)
    body1 = json.loads(res1.result)
    assert res1.blocked is True
    assert "EffectAuthorization" in body1["error"]
    assert not target.exists()
    assert spy.called is False
    assert exec_spy_disabled.called is False

    # Attach the production-signed authorization and drive again → committed write.
    auth = _sign_effect_auth(
        prod_signer, prod_key_id, "youtab-one-brain",
        authorization_id="authz-prod-write-0001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=request_digest)
    agent._effect_authorizations = [auth]

    spy2 = _ExecuteSpy("write_file")
    res2 = _drive(agent, "write_file", dict(final_args), task, spy2)
    body2 = json.loads(res2.result)

    assert res2.blocked is False
    assert body2["status"] == "ok"
    assert body2["bytes_written"] == len(content)
    assert target.read_bytes() == content
    # canonical receipt fields
    receipt = body2["receipt"]
    assert receipt["authorization_id"] == "authz-prod-write-0001"
    assert receipt["key_id"] == prod_key_id
    assert receipt["workspace_id"] == WS
    # ledger committed
    rec = get_effect_in_workspace(
        receipt["effect_id"],
        Principal(admitted.envelope.tenant_id, admitted.envelope.user_id),
        WS, db_path=effects_db)
    assert rec is not None and rec.state.value == "committed"
    # bypass proofs: registered handler never called; shell backend never used
    assert spy2.called is False
    assert exec_spy_disabled.called is False


# =========================================================================== #
# 2) Test-authority (production=False + TestEffectAuthority) authorized write.
# =========================================================================== #
def test_managed_write_test_authority(tmp_path, managed_env, exec_spy_disabled):
    ws = tmp_path / "workspace"
    ws.mkdir()
    effects_db = tmp_path / "effects.db"
    admitted = _admit("cmd-test-write-1", "nonce-test-write-0000001")

    signer = TestEffectAuthority()
    agent = _Agent(admitted, ws, effects_db, production=False,
                   test_keys=signer.keyring())
    content = b"test-authority authorized write"
    edigest, proposal_id, request_digest, final_args = _effect_params(
        admitted, ws, "data.txt", content)
    target = ws / "data.txt"
    task = "task-test-w-1"

    spy = _ExecuteSpy("write_file")
    res1 = _drive(agent, "write_file", dict(final_args), task, spy)
    assert res1.blocked is True and not target.exists()

    auth = signer.mint(
        authorization_id="authz-test-write-0001",
        tenant_id=admitted.envelope.tenant_id, user_id=admitted.envelope.user_id,
        workspace_id=WS, command_id=admitted.envelope.command_id,
        capability="fs.effect", operation="write", effect_digest=edigest,
        issued_at=datetime.now(UTC) - timedelta(minutes=1),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        proposal_id=proposal_id, request_digest=request_digest)
    agent._effect_authorizations = [auth]

    spy2 = _ExecuteSpy("write_file")
    res2 = _drive(agent, "write_file", dict(final_args), task, spy2)
    body2 = json.loads(res2.result)

    assert res2.blocked is False and body2["status"] == "ok"
    assert body2["bytes_written"] == len(content)
    assert target.read_bytes() == content
    rec = get_effect_in_workspace(
        body2["receipt"]["effect_id"],
        Principal(admitted.envelope.tenant_id, admitted.envelope.user_id),
        WS, db_path=effects_db)
    assert rec is not None and rec.state.value == "committed"
    assert spy2.called is False
    assert exec_spy_disabled.called is False


# =========================================================================== #
# 3) Grant-bound read through the tool_executor.
# =========================================================================== #
def test_managed_read_through_tool_executor(tmp_path, managed_env, exec_spy_disabled):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "r.txt").write_bytes(b"alpha\nbeta\ngamma")
    effects_db = tmp_path / "effects.db"
    admitted = _admit("cmd-read-1", "nonce-read-000000000001")
    agent = _Agent(admitted, ws, effects_db, production=True, prod_keys={})

    spy = _ExecuteSpy("read_file")
    res = _drive(agent, "read_file", {"path": "r.txt"}, "task-read-1", spy)
    body = json.loads(res.result)

    assert res.blocked is False
    assert body["operation"] == "read"
    assert "1|alpha" in body["content"]
    assert "2|beta" in body["content"]
    assert "3|gamma" in body["content"]
    assert spy.called is False
    assert exec_spy_disabled.called is False


# =========================================================================== #
# 4) Unsigned write blocked, zero side effect + zero committed effect.
# =========================================================================== #
def test_unsigned_write_blocked_zero_side_effect(
    tmp_path, managed_env, exec_spy_disabled
):
    ws = tmp_path / "workspace"
    ws.mkdir()
    effects_db = tmp_path / "effects.db"
    admitted = _admit("cmd-unsigned-1", "nonce-unsigned-00000001")
    agent = _Agent(admitted, ws, effects_db, production=True, prod_keys={})
    content = b"should never be written"
    _e, _p, _rd, final_args = _effect_params(admitted, ws, "data.txt", content)
    target = ws / "data.txt"

    spy = _ExecuteSpy("write_file")
    res = _drive(agent, "write_file", dict(final_args), "task-unsigned-1", spy)
    body = json.loads(res.result)

    assert res.blocked is True
    assert "EffectAuthorization" in body["error"]
    assert not target.exists()
    assert spy.called is False
    assert exec_spy_disabled.called is False
    # No effect was ever committed (the proposal is not a committed effect).
    from youtab_runtime.effect_ledger import list_effects_in_workspace
    effects = list_effects_in_workspace(
        "task-unsigned-1",
        Principal(admitted.envelope.tenant_id, admitted.envelope.user_id),
        WS, db_path=effects_db)
    assert effects == []


# =========================================================================== #
# 5) Forged authorization (key not in the trusted keyring) blocked, no file.
# =========================================================================== #
def test_forged_authorization_blocked(tmp_path, managed_env, exec_spy_disabled):
    ws = tmp_path / "workspace"
    ws.mkdir()
    effects_db = tmp_path / "effects.db"
    admitted = _admit("cmd-forged-1", "nonce-forged-0000000001")
    # production keyring is EMPTY → any signer's key id is untrusted.
    agent = _Agent(admitted, ws, effects_db, production=True, prod_keys={})
    content = b"forged authority attempt"
    edigest, proposal_id, request_digest, final_args = _effect_params(
        admitted, ws, "data.txt", content)
    target = ws / "data.txt"

    # Emit the proposal first so correlation passes and we reach the crypto check.
    spy0 = _ExecuteSpy("write_file")
    _drive(agent, "write_file", dict(final_args), "task-forged-1", spy0)

    forged_signer = Ed25519PrivateKey.generate()
    auth = _sign_effect_auth(
        forged_signer, "attacker-key-01", "attacker",
        authorization_id="authz-forged-000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=request_digest)
    agent._effect_authorizations = [auth]

    spy = _ExecuteSpy("write_file")
    res = _drive(agent, "write_file", dict(final_args), "task-forged-1", spy)
    body = json.loads(res.result)

    assert res.blocked is True
    assert "authorization rejected" in body["error"]
    assert not target.exists()
    assert spy.called is False
    assert exec_spy_disabled.called is False


# =========================================================================== #
# 6) Authorization whose request_digest does not match the proposal (changed).
# =========================================================================== #
def test_wrong_request_digest_blocked(tmp_path, managed_env, exec_spy_disabled):
    ws = tmp_path / "workspace"
    ws.mkdir()
    effects_db = tmp_path / "effects.db"
    admitted = _admit("cmd-changed-1", "nonce-changed-00000001")
    prod_signer = Ed25519PrivateKey.generate()
    prod_key_id = "brain-effect-prod-06"
    prod_pub = base64.b64encode(prod_signer.public_key().public_bytes_raw()).decode()
    agent = _Agent(admitted, ws, effects_db, production=True,
                   prod_keys={prod_key_id: prod_pub})
    content = b"request digest mismatch"
    edigest, proposal_id, _correct_rd, final_args = _effect_params(
        admitted, ws, "data.txt", content)
    target = ws / "data.txt"

    # Emit the real proposal (records the correct request_digest).
    spy0 = _ExecuteSpy("write_file")
    _drive(agent, "write_file", dict(final_args), "task-changed-1", spy0)

    # Correct effect_digest (so the auth is selected) but a WRONG request_digest.
    wrong_rd = hashlib.sha256(b"a different request").hexdigest()
    auth = _sign_effect_auth(
        prod_signer, prod_key_id, "youtab-one-brain",
        authorization_id="authz-changed-00001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=wrong_rd)
    agent._effect_authorizations = [auth]

    spy = _ExecuteSpy("write_file")
    res = _drive(agent, "write_file", dict(final_args), "task-changed-1", spy)
    body = json.loads(res.result)

    assert res.blocked is True
    assert "correlation failed" in body["error"]
    assert "ProposalChanged" in body["error"]
    assert not target.exists()
    assert spy.called is False
    assert exec_spy_disabled.called is False


# =========================================================================== #
# 7) Remote backend fails closed (no host-side containment).
# =========================================================================== #
def test_remote_backend_fails_closed(tmp_path, managed_env, exec_spy_disabled):
    ws = tmp_path / "workspace"
    ws.mkdir()
    effects_db = tmp_path / "effects.db"
    admitted = _admit("cmd-remote-1", "nonce-remote-0000000001")
    signer = TestEffectAuthority()
    agent = _Agent(admitted, ws, effects_db, backend="docker", production=False,
                   test_keys=signer.keyring())
    content = b"remote should fail closed"
    edigest, proposal_id, request_digest, final_args = _effect_params(
        admitted, ws, "data.txt", content)
    auth = signer.mint(
        authorization_id="authz-remote-00001",
        tenant_id=admitted.envelope.tenant_id, user_id=admitted.envelope.user_id,
        workspace_id=WS, command_id=admitted.envelope.command_id,
        capability="fs.effect", operation="write", effect_digest=edigest,
        issued_at=datetime.now(UTC) - timedelta(minutes=1),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        proposal_id=proposal_id, request_digest=request_digest)
    agent._effect_authorizations = [auth]

    spy = _ExecuteSpy("write_file")
    res = _drive(agent, "write_file", dict(final_args), "task-remote-1", spy)
    body = json.loads(res.result)

    assert res.blocked is True
    assert body["error"] == MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE
    assert not (ws / "data.txt").exists()
    assert spy.called is False
    assert exec_spy_disabled.called is False


# =========================================================================== #
# 8) POSITIVE CONTROL: local-standalone reaches the real handler + shell backend.
# =========================================================================== #
def test_standalone_write_uses_shell_backend(
    tmp_path, monkeypatch, exec_spy_disabled
):
    # Explicit local-standalone trust mode: the router returns None, the managed
    # gate is inert, so the real registered handler (execute) runs and drives the
    # standalone file tool through ShellFileOperations.
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "local-standalone")
    ws = tmp_path / "workspace"
    ws.mkdir()
    effects_db = tmp_path / "effects.db"
    # No admission is required for standalone; the gate is inert.
    agent = _Agent(None, ws, effects_db, production=False)
    target = ws / "standalone.txt"
    final_args = {"path": str(target), "content": "standalone write"}

    spy = _ExecuteSpy("write_file")
    res = _drive(agent, "write_file", dict(final_args), "task-standalone-1", spy)

    assert res.blocked is False
    # The real registered handler WAS reached ...
    assert spy.called is True
    # ... and it went through the shell backend (proving the bypass spy is wired
    # and that the managed cases' "not called" assertions are meaningful).
    assert exec_spy_disabled.called is True


# =========================================================================== #
# 9) Replay: the same authorization twice writes+commits once, then no-ops.
# =========================================================================== #
def test_replay_no_double_write(tmp_path, managed_env, exec_spy_disabled):
    ws = tmp_path / "workspace"
    ws.mkdir()
    effects_db = tmp_path / "effects.db"
    admitted = _admit("cmd-replay-1", "nonce-replay-0000000001")
    signer = TestEffectAuthority()
    agent = _Agent(admitted, ws, effects_db, production=False,
                   test_keys=signer.keyring())
    content = b"once and only once"
    edigest, proposal_id, request_digest, final_args = _effect_params(
        admitted, ws, "data.txt", content)
    target = ws / "data.txt"
    task = "task-replay-1"

    spy0 = _ExecuteSpy("write_file")
    _drive(agent, "write_file", dict(final_args), task, spy0)  # emit proposal
    auth = signer.mint(
        authorization_id="authz-replay-00001",
        tenant_id=admitted.envelope.tenant_id, user_id=admitted.envelope.user_id,
        workspace_id=WS, command_id=admitted.envelope.command_id,
        capability="fs.effect", operation="write", effect_digest=edigest,
        issued_at=datetime.now(UTC) - timedelta(minutes=1),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        proposal_id=proposal_id, request_digest=request_digest)
    agent._effect_authorizations = [auth]

    spy1 = _ExecuteSpy("write_file")
    first = _drive(agent, "write_file", dict(final_args), task, spy1)
    body1 = json.loads(first.result)
    assert first.blocked is False and body1["status"] == "ok"
    assert target.read_bytes() == content
    effect_id = body1["receipt"]["effect_id"]
    principal = Principal(admitted.envelope.tenant_id, admitted.envelope.user_id)
    assert get_effect_in_workspace(
        effect_id, principal, WS, db_path=effects_db).state.value == "committed"

    # Replay the identical authorization: single-use / consumed proposal → blocked
    # no-op; the file is unchanged and no second effect is committed.
    spy2 = _ExecuteSpy("write_file")
    second = _drive(agent, "write_file", dict(final_args), task, spy2)
    assert second.blocked is True
    assert target.read_bytes() == content
    # still exactly one committed effect for this run
    from youtab_runtime.effect_ledger import list_effects_in_workspace
    committed = [
        e for e in list_effects_in_workspace(task, principal, WS, db_path=effects_db)
        if e.state.value == "committed"
    ]
    assert len(committed) == 1
    assert spy1.called is False and spy2.called is False
    assert exec_spy_disabled.called is False
