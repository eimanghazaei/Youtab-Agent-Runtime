"""Full managed-filesystem operation matrix through the REAL production callsite
(Owner item 3).

Unlike ``test_managed_fs_router`` (which calls the router directly), this suite
drives every effectful managed operation — **patch (replace-mode), delete, move**
— through the actual production seam:

    agent.tool_executor._run_agent_tool_execution_middleware(
        agent, function_name="patch", ...)

which runs the Relay pass-through, the plugin/guardrail blocks, and then the
managed filesystem router inside ``_authorized_dispatch`` (BEFORE the general
authority gate). For every case we assert three things at once:

  * the REAL filesystem state (file bytes actually changed / deleted / moved, or
    provably untouched on a refusal);
  * the REAL ledger / receipt state (exactly one committed effect on success; no
    committed effect on a refusal; a non-terminal ``unknown`` effect when the
    host-IO raises AFTER the effect was claimed — never blind-replayed);
  * zero unintended side effects — the unrestricted shell backend
    (``ShellFileOperations._exec``) is NEVER used and the registered tool handler
    (``execute``) is NEVER reached on the managed path.

Authorizations are minted in PRODUCTION mode: an ephemeral, NON-test Ed25519 key
with a plain key id, ``agent._managed_authority_production=True`` and the key in
``agent._managed_authority_public_keys`` — the Runtime only ever *verifies* it.

The ONLY monkeypatching is trust-mode env (``YOUTAB_RUNTIME_TRUST_MODE=managed``)
and the ``ShellFileOperations._exec`` call-recording spy. The Relay / plugin /
execution-middleware layers are NOT patched — they pass through on their own
because no session is bound and no plugins are registered.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.tool_executor import _run_agent_tool_execution_middleware
from tools.file_operations import ShellFileOperations
from youtab_runtime.approval import compute_effect_digest, content_digest
from youtab_runtime.contracts import BrainCommandEnvelopeV2
from youtab_runtime.effect_authorization import EffectAuthorization
from youtab_runtime.effect_ledger import (
    get_effect_in_workspace,
    list_effects_in_workspace,
)
from youtab_runtime.folder_grant import (
    FolderGrant,
    _canonical,
    _is_within,
    resolve_within_grant,
)
from youtab_runtime.managed_execution import re_admit_worker_grant
from youtab_runtime.managed_remote_executor import (
    MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE,
)
from youtab_runtime.policy import AuthorityBoundary
from youtab_runtime.run_journal import Principal

WS = "-"  # the envelopes below are workspace-unscoped ("-")

# The Brain signing identity for the execution GRANT (envelope), plain key id.
_GRANT_KEY_ID = "brain-ed25519-prodmatrix"
_GRANT_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([41]) * 32)
_GRANT_PUB = base64.b64encode(_GRANT_SIGNER.public_key().public_bytes_raw()).decode()
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
        task_id="task-prodmatrix01",
        root_run_id="run-prodmatrix01",
        parent_task_id=None,
        attempt=1,
        tenant_id="tenant-alpha",
        workspace_id="-",
        user_id="user-eiman",
        membership_generation=1,
        authorization_epoch=1,
        agent_id="agent-default",
        engine_id="engine-local",
        trace_id="trace-prodmatrix01",
        nonce=nonce,
        objective="managed production fs operation matrix",
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
                 production=True, prod_keys=None):
        # ── router-read state ─────────────────────────────────────────────
        self._admitted_command = admitted
        self._managed_workspace_root = str(workspace_root)
        self._managed_effects_db = effects_db
        self._managed_fs_backend = backend
        self._managed_authority_production = production
        self._managed_authority_public_keys = prod_keys
        self._managed_test_authority_keys = None
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
    """Records whether the real registered-tool handler was reached. On the managed
    path it must NEVER be called; if it is, the test's ``spy.called is False``
    assertion fails."""

    def __init__(self, function_name):
        self._fn = function_name
        self.called = False
        self.calls = []

    def __call__(self, final_args):
        self.called = True
        self.calls.append(final_args)
        return json.dumps({"error": "execute() must not run on the managed path"},
                          ensure_ascii=False)


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


def _patch(agent, final_args, task_id):
    """Drive a managed ``patch`` call and return ``(outcome, spy)``."""
    spy = _ExecuteSpy("patch")
    return _drive(agent, "patch", dict(final_args), task_id, spy), spy


# --------------------------------------------------------------------------- #
# Effect-authorization minting (production-mode signing; matches the router's
# per-operation digest + proposal reference conventions exactly).
# --------------------------------------------------------------------------- #
def _principal(admitted):
    return Principal(admitted.envelope.tenant_id, admitted.envelope.user_id)


def _op_identity(admitted, workspace_root, *, operation, requested_path,
                 digest_bytes, descriptor, perms):
    """Compute (effect_digest, proposal_id, request_digest) the way the router does
    for ``operation`` — used to mint a matching authorization."""
    env = admitted.envelope
    grant = FolderGrant(
        grant_id=env.command_id, tenant_id=env.tenant_id, principal_id=env.user_id,
        workspace_id=WS, canonical_root=str(workspace_root),
        permissions=frozenset(perms),
    )
    safe = resolve_within_grant(grant, requested_path, operation=operation,
                                tenant_id=env.tenant_id, principal_id=env.user_id,
                                workspace_id=WS)
    edigest = compute_effect_digest(operation, safe, WS, content_digest(digest_bytes))
    rd = hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":"),
                   default=str).encode()
    ).hexdigest()
    return edigest, f"proposal-{rd[:24]}", rd


def _sign_auth(signer, key_id, *, authorization_id, admitted, effect_digest,
               proposal_id, request_digest, operation, issuer="youtab-one-brain",
               tenant_id=None, user_id=None, workspace_id=WS,
               issued_at=None, expires_at=None):
    """Sign a production-style EffectAuthorization (plain, non-test key id)."""
    env = admitted.envelope
    now = datetime.now(UTC)
    unsigned = EffectAuthorization(
        issuer=issuer, authorization_id=authorization_id,
        tenant_id=tenant_id or env.tenant_id, user_id=user_id or env.user_id,
        workspace_id=workspace_id, command_id=env.command_id,
        capability="fs.effect", operation=operation, effect_digest=effect_digest,
        issued_at=issued_at or (now - timedelta(minutes=1)),
        expires_at=expires_at or (now + timedelta(hours=1)),
        proposal_id=proposal_id, request_digest=request_digest,
        key_id=key_id, signature="0" * 64,
    )
    sig = base64.b64encode(signer.sign(unsigned.canonical_payload())).decode()
    return unsigned.model_copy(update={"signature": sig})


def _prod_key():
    signer = Ed25519PrivateKey.generate()
    key_id = "brain-effect-prod-" + base64.b16encode(os.urandom(3)).decode().lower()
    pub = base64.b64encode(signer.public_key().public_bytes_raw()).decode()
    return signer, key_id, pub


# ── operation descriptors (match managed_fs_router exactly) ──────────────────
def _replace_args(path, old, new, replace_all=False):
    return {"mode": "replace", "path": path, "old_string": old,
            "new_string": new, "replace_all": replace_all}


def _replace_bytes(old, new, replace_all=False):
    return json.dumps({"mode": "replace", "old": old, "new": new,
                       "replace_all": replace_all},
                      sort_keys=True, separators=(",", ":")).encode()


def _delete_args(path):
    return {"mode": "patch",
            "patch": f"*** Begin Patch\n*** Delete File: {path}\n*** End Patch"}


def _move_args(src, dst):
    return {"mode": "patch",
            "patch": f"*** Begin Patch\n*** Move File: {src} -> {dst}\n*** End Patch"}


def _move_bytes(src, dst):
    return json.dumps({"src": src, "dst": dst}, sort_keys=True,
                      separators=(",", ":")).encode()


def _committed(task, admitted, effects_db):
    return [e for e in list_effects_in_workspace(
        task, _principal(admitted), WS, db_path=effects_db)
        if e.state.value == "committed"]


# --------------------------------------------------------------------------- #
# Reparse (symlink / junction) helper — mirrors tests/.../test_grant_fs_toctou.py
# --------------------------------------------------------------------------- #
def _make_reparse(link: str, target: str) -> bool:
    """Create a directory symlink, else a Windows junction (no admin)."""
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


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def managed_env(monkeypatch):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")


@pytest.fixture
def exec_spy_disabled(monkeypatch):
    """Record every ``ShellFileOperations._exec`` invocation. Managed operations
    must NEVER hit this shell seam (they use grant-bound host-IO)."""
    spy = MagicMock(name="ShellFileOperations._exec")
    monkeypatch.setattr(ShellFileOperations, "_exec", spy)
    return spy


def _make(tmp_path, command_id, nonce, *, backend="local"):
    ws = tmp_path / "workspace"
    ws.mkdir()
    effects_db = tmp_path / "effects.db"
    admitted = _admit(command_id, nonce)
    signer, key_id, pub = _prod_key()
    agent = _Agent(admitted, ws, effects_db, backend=backend, production=True,
                   prod_keys={key_id: pub})
    return ws, effects_db, admitted, agent, signer, key_id


# =========================================================================== #
# POSITIVE MATRIX — each op executes exactly once through the real middleware.
# =========================================================================== #
def test_patch_replace_authorized_matrix(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-patch-1", "nonce-mx-patch-000000001")
    target = ws / "f.txt"
    target.write_bytes(b"hello world")
    task = "task-mx-patch-1"
    final_args = _replace_args("f.txt", "world", "there")

    # 1) No authorization → proposal emitted, zero side effect.
    out1, spy1 = _patch(agent, final_args, task)
    assert out1.blocked is True
    assert "EffectAuthorization" in json.loads(out1.result)["error"]
    assert target.read_bytes() == b"hello world"
    assert spy1.called is False and exec_spy_disabled.called is False

    # 2) Attach the production-signed authorization → executed, one committed receipt.
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=_replace_bytes("world", "there"), descriptor=final_args,
        perms={"read", "write"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-patch-replace-0001",
        admitted=admitted, effect_digest=edigest, proposal_id=proposal_id,
        request_digest=rd, operation="write")]

    out2, spy2 = _patch(agent, final_args, task)
    body = json.loads(out2.result)
    assert out2.blocked is False and body["operation"] == "patch"
    assert target.read_bytes() == b"hello there"
    rec = get_effect_in_workspace(body["receipt"]["effect_id"], _principal(admitted),
                                  WS, db_path=effects_db)
    assert rec is not None and rec.state.value == "committed"
    assert body["receipt"]["key_id"] == key_id
    assert len(_committed(task, admitted, effects_db)) == 1
    assert spy2.called is False and exec_spy_disabled.called is False


def test_v4a_delete_authorized_matrix(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-delete-1", "nonce-mx-delete-00000001")
    target = ws / "gone.txt"
    target.write_bytes(b"bye")
    task = "task-mx-delete-1"
    final_args = _delete_args("gone.txt")

    out1, spy1 = _patch(agent, final_args, task)
    assert out1.blocked is True and target.exists()
    assert spy1.called is False and exec_spy_disabled.called is False

    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="delete", requested_path="gone.txt",
        digest_bytes=b"", descriptor={"tool": "patch", "op": "delete", "path": "gone.txt"},
        perms={"delete"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-delete-00000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="delete")]

    out2, spy2 = _patch(agent, final_args, task)
    body = json.loads(out2.result)
    assert out2.blocked is False and body["operation"] == "delete"
    assert not target.exists()
    rec = get_effect_in_workspace(body["receipt"]["effect_id"], _principal(admitted),
                                  WS, db_path=effects_db)
    assert rec is not None and rec.state.value == "committed"
    assert len(_committed(task, admitted, effects_db)) == 1
    assert spy2.called is False and exec_spy_disabled.called is False


def test_v4a_move_authorized_matrix(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-move-1", "nonce-mx-move-000000001")
    src = ws / "a.txt"
    dst = ws / "b.txt"
    src.write_bytes(b"payload")
    task = "task-mx-move-1"
    final_args = _move_args("a.txt", "b.txt")

    out1, spy1 = _patch(agent, final_args, task)
    assert out1.blocked is True and src.exists() and not dst.exists()
    assert spy1.called is False and exec_spy_disabled.called is False

    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="move", requested_path="a.txt",
        digest_bytes=_move_bytes("a.txt", "b.txt"),
        descriptor={"tool": "patch", "op": "move", "src": "a.txt", "dst": "b.txt"},
        perms={"move"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-move-000000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="move")]

    out2, spy2 = _patch(agent, final_args, task)
    body = json.loads(out2.result)
    assert out2.blocked is False and body["operation"] == "move"
    assert not src.exists() and dst.read_bytes() == b"payload"
    rec = get_effect_in_workspace(body["receipt"]["effect_id"], _principal(admitted),
                                  WS, db_path=effects_db)
    assert rec is not None and rec.state.value == "committed"
    assert len(_committed(task, admitted, effects_db)) == 1
    assert spy2.called is False and exec_spy_disabled.called is False


def test_patch_replace_replay_executes_once(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-replay-1", "nonce-mx-replay-00000001")
    target = ws / "f.txt"
    target.write_bytes(b"count me once")
    task = "task-mx-replay-1"
    # 'once' appears exactly once in the file → unambiguous replace.
    final_args = _replace_args("f.txt", "once", "twice")

    out0, _ = _patch(agent, final_args, task)  # emit proposal
    assert out0.blocked is True

    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=_replace_bytes("once", "twice"), descriptor=final_args,
        perms={"read", "write"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-replay-00000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write")]

    first, spy1 = _patch(agent, final_args, task)
    assert first.blocked is False
    assert target.read_bytes() == b"count me twice"
    assert len(_committed(task, admitted, effects_db)) == 1

    # Replay the IDENTICAL authorization → blocked no-op, file unchanged, still one
    # committed effect.
    second, spy2 = _patch(agent, final_args, task)
    assert second.blocked is True
    assert target.read_bytes() == b"count me twice"
    assert len(_committed(task, admitted, effects_db)) == 1
    assert spy1.called is False and spy2.called is False
    assert exec_spy_disabled.called is False


# =========================================================================== #
# NEGATIVE / ADVERSARIAL MATRIX — each: refused, filesystem untouched, and no
# committed receipt in the ledger.
# =========================================================================== #
def test_missing_authorization_refused(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, _s, _k = _make(
        tmp_path, "cmd-mx-noauth-1", "nonce-mx-noauth-00000001")
    target = ws / "f.txt"
    target.write_bytes(b"orig")
    task = "task-mx-noauth-1"

    out, spy = _patch(agent, _replace_args("f.txt", "orig", "new"), task)
    assert out.blocked is True
    assert "EffectAuthorization" in json.loads(out.result)["error"]
    assert target.read_bytes() == b"orig"
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_expired_authorization_refused(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-expired-1", "nonce-mx-expired-0000001")
    target = ws / "f.txt"
    target.write_bytes(b"orig")
    task = "task-mx-expired-1"
    final_args = _replace_args("f.txt", "orig", "new")

    out0, _ = _patch(agent, final_args, task)  # emit proposal
    assert out0.blocked is True

    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=_replace_bytes("orig", "new"), descriptor=final_args,
        perms={"read", "write"})
    now = datetime.now(UTC)
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-expired-0000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write", issued_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1))]

    out, spy = _patch(agent, final_args, task)
    assert out.blocked is True
    assert "authorization rejected" in json.loads(out.result)["error"]
    assert target.read_bytes() == b"orig"
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_receipt_fields_and_foreign_principal_unreadable(
    tmp_path, managed_env, exec_spy_disabled
):
    """A committed op yields a canonical receipt bound to op / target / workspace /
    principal / final-state, and the receipt is unreadable to a foreign principal."""
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-receipt-1", "nonce-mx-receipt-0000001")
    target = ws / "r.txt"
    task = "task-mx-receipt-1"
    content = b"receipt payload"
    final_args = {"path": "r.txt", "content": content.decode()}
    _drive(agent, "write_file", dict(final_args), task, _ExecuteSpy("write_file"))
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="r.txt",
        digest_bytes=content, descriptor=final_args, perms={"write"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-receipt-000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write")]
    out = _drive(agent, "write_file", dict(final_args), task, _ExecuteSpy("write_file"))
    body = json.loads(out.result)
    # result carries operation + canonical target + receipt binding
    assert body["operation"] == "write"
    assert body["resolved_path"].endswith("r.txt")
    r = body["receipt"]
    assert r["workspace_id"] == WS and r["state"] == "committed"
    assert r["authorization_id"] == "authz-receipt-000001" and r["key_id"] == key_id
    # canonical ledger record: correct final state + tenant/principal-bound digest
    rec = get_effect_in_workspace(r["effect_id"], _principal(admitted), WS, db_path=effects_db)
    assert rec is not None and rec.state.value == "committed"
    assert rec.target_scope_digest  # canonical target/scope digest present
    # foreign PRINCIPAL cannot read the receipt (tenant/user-bound), and foreign
    # WORKSPACE cannot either.
    assert get_effect_in_workspace(r["effect_id"], Principal("other-tenant", "other-user"),
                                   WS, db_path=effects_db) is None
    assert get_effect_in_workspace(r["effect_id"], _principal(admitted), "other-ws",
                                   db_path=effects_db) is None
    assert target.read_bytes() == content


def test_replayed_copied_authorization_refused(tmp_path, managed_env, exec_spy_disabled):
    # Uses write_file so the replay reaches the single-use/correlation layer (a
    # patch-replace replay would instead be refused earlier because its old_string
    # is already gone — a different, also-safe layer). Content is identical on the
    # replay so the effect identity matches the consumed one.
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-copied-1", "nonce-mx-copied-00000001")
    target = ws / "f.txt"
    task = "task-mx-copied-1"
    content = b"committed once"
    final_args = {"path": "f.txt", "content": content.decode()}

    out0 = _drive(agent, "write_file", dict(final_args), task, _ExecuteSpy("write_file"))
    assert out0.blocked is True  # emit proposal
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=content, descriptor=final_args, perms={"write"})
    auth = _sign_auth(
        signer, key_id, authorization_id="authz-copied-00000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write")
    agent._effect_authorizations = [auth]

    first = _drive(agent, "write_file", dict(final_args), task, _ExecuteSpy("write_file"))
    assert first.blocked is False and target.read_bytes() == content
    assert len(_committed(task, admitted, effects_db)) == 1

    # Attacker copies the (now-consumed) authorization and resubmits it verbatim.
    agent._effect_authorizations = [auth]
    replay = _drive(agent, "write_file", dict(final_args), task, _ExecuteSpy("write_file"))
    assert replay.blocked is True
    assert target.read_bytes() == content
    assert len(_committed(task, admitted, effects_db)) == 1  # no second receipt
    assert exec_spy_disabled.called is False


def test_wrong_workspace_authorization_refused(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-wsbind-1", "nonce-mx-wsbind-00000001")
    target = ws / "f.txt"
    target.write_bytes(b"orig")
    task = "task-mx-wsbind-1"
    final_args = _replace_args("f.txt", "orig", "new")

    out0, _ = _patch(agent, final_args, task)
    assert out0.blocked is True
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=_replace_bytes("orig", "new"), descriptor=final_args,
        perms={"read", "write"})
    # Correct effect_digest (so it is selected) but the auth binds a DIFFERENT ws.
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-wsbind-0000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write", workspace_id="other-ws")]

    out, spy = _patch(agent, final_args, task)
    assert out.blocked is True
    assert "authorization rejected" in json.loads(out.result)["error"]
    assert target.read_bytes() == b"orig"
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_wrong_tenant_authorization_refused(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-tenant-1", "nonce-mx-tenant-00000001")
    target = ws / "f.txt"
    target.write_bytes(b"orig")
    task = "task-mx-tenant-1"
    final_args = _replace_args("f.txt", "orig", "new")

    out0, _ = _patch(agent, final_args, task)
    assert out0.blocked is True
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=_replace_bytes("orig", "new"), descriptor=final_args,
        perms={"read", "write"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-tenant-0000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write", tenant_id="tenant-intruder")]

    out, spy = _patch(agent, final_args, task)
    assert out.blocked is True
    assert "authorization rejected" in json.loads(out.result)["error"]
    assert target.read_bytes() == b"orig"
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_wrong_principal_authorization_refused(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-user-1", "nonce-mx-user-000000001")
    target = ws / "f.txt"
    target.write_bytes(b"orig")
    task = "task-mx-user-1"
    final_args = _replace_args("f.txt", "orig", "new")

    out0, _ = _patch(agent, final_args, task)
    assert out0.blocked is True
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=_replace_bytes("orig", "new"), descriptor=final_args,
        perms={"read", "write"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-user-00000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write", user_id="user-intruder")]

    out, spy = _patch(agent, final_args, task)
    assert out.blocked is True
    assert "authorization rejected" in json.loads(out.result)["error"]
    assert target.read_bytes() == b"orig"
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_changed_patch_content_after_authorization_refused(
    tmp_path, managed_env, exec_spy_disabled
):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-chg-1", "nonce-mx-chg-0000000001")
    target = ws / "f.txt"
    target.write_bytes(b"alpha beta")
    task = "task-mx-chg-1"

    # Authorize replacing 'beta'->'GAMMA', then drive a DIFFERENT replacement.
    authorized = _replace_args("f.txt", "beta", "GAMMA")
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=_replace_bytes("beta", "GAMMA"), descriptor=authorized,
        perms={"read", "write"})
    # Emit the proposal for the AUTHORIZED args so correlation would be possible.
    _patch(agent, authorized, task)
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-changed-0000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write")]

    # Drive with a tampered replacement (different new_string) → digest mismatch:
    # the authorization does not authorize THIS effect, so no auth is selected.
    tampered = _replace_args("f.txt", "beta", "EVIL")
    out, spy = _patch(agent, tampered, task)
    assert out.blocked is True
    assert "EffectAuthorization" in json.loads(out.result)["error"]
    assert target.read_bytes() == b"alpha beta"
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_changed_move_dst_after_authorization_refused(
    tmp_path, managed_env, exec_spy_disabled
):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-mvchg-1", "nonce-mx-mvchg-00000001")
    src = ws / "a.txt"
    src.write_bytes(b"payload")
    task = "task-mx-mvchg-1"

    authorized = _move_args("a.txt", "b.txt")
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="move", requested_path="a.txt",
        digest_bytes=_move_bytes("a.txt", "b.txt"),
        descriptor={"tool": "patch", "op": "move", "src": "a.txt", "dst": "b.txt"},
        perms={"move"})
    _patch(agent, authorized, task)  # emit proposal for a->b
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-mvchg-0000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="move")]

    # Drive a move to a DIFFERENT destination → different move digest → no auth.
    tampered = _move_args("a.txt", "c.txt")
    out, spy = _patch(agent, tampered, task)
    assert out.blocked is True
    assert "EffectAuthorization" in json.loads(out.result)["error"]
    assert src.read_bytes() == b"payload"
    assert not (ws / "b.txt").exists() and not (ws / "c.txt").exists()
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_path_traversal_refused(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, _s, _k = _make(
        tmp_path, "cmd-mx-trav-1", "nonce-mx-trav-000000001")
    outside = tmp_path / "escape.txt"
    outside.write_bytes(b"secret")
    task = "task-mx-trav-1"

    out, spy = _patch(agent, _replace_args("../escape.txt", "secret", "pwned"), task)
    assert out.blocked is True
    assert "refused" in json.loads(out.result)["error"]
    assert outside.read_bytes() == b"secret"
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_move_destination_outside_grant_fails_closed(
    tmp_path, managed_env, exec_spy_disabled
):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-mvout-1", "nonce-mx-mvout-00000001")
    src = ws / "a.txt"
    src.write_bytes(b"payload")
    outside = tmp_path / "out.txt"
    task = "task-mx-mvout-1"
    final_args = _move_args("a.txt", "../out.txt")

    # src is inside the grant, so the proposal + authorization mint succeed; the
    # DESTINATION is outside and must fail closed at the host-IO boundary.
    _patch(agent, final_args, task)  # emit proposal
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="move", requested_path="a.txt",
        digest_bytes=_move_bytes("a.txt", "../out.txt"),
        descriptor={"tool": "patch", "op": "move", "src": "a.txt", "dst": "../out.txt"},
        perms={"move"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-mvout-0000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="move")]

    out, spy = _patch(agent, final_args, task)
    assert out.blocked is True
    assert "refused" in json.loads(out.result)["error"]
    assert src.read_bytes() == b"payload"
    assert not outside.exists()
    # Destination-outside-grant is a PRE-EXECUTION rejection (validated before any
    # claim): ZERO effect row — never a committed OR an unknown effect.
    all_effects = list_effects_in_workspace(task, _principal(admitted), WS,
                                            db_path=effects_db)
    assert all_effects == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_unsupported_remote_backend_fails_closed(
    tmp_path, managed_env, exec_spy_disabled
):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-remote-1", "nonce-mx-remote-00000001", backend="docker")
    target = ws / "f.txt"
    target.write_bytes(b"orig")
    task = "task-mx-remote-1"
    final_args = _replace_args("f.txt", "orig", "new")

    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=_replace_bytes("orig", "new"), descriptor=final_args,
        perms={"read", "write"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-remote-0000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write")]

    out, spy = _patch(agent, final_args, task)
    assert out.blocked is True
    assert json.loads(out.result)["error"] == MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE
    assert target.read_bytes() == b"orig"
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_symlink_junction_parent_escape_fails_closed(
    tmp_path, managed_env, exec_spy_disabled
):
    ws, effects_db, admitted, agent, _s, _k = _make(
        tmp_path, "cmd-mx-junc-1", "nonce-mx-junc-000000001")
    inside = ws / "d"
    inside.mkdir()
    (inside / "f.txt").write_bytes(b"inside")
    outside = tempfile.mkdtemp(prefix="mx-junc-out-")
    with open(os.path.join(outside, "f.txt"), "wb") as fh:
        fh.write(b"OUTSIDE")
    task = "task-mx-junc-1"

    # Swap the 'd' component for a junction pointing OUTSIDE the grant.
    import shutil
    shutil.rmtree(inside)
    if not _make_reparse(str(inside), outside):
        # Platform cannot create a reparse point: assert the containment primitive
        # rejects the equivalent out-of-root real path (no silent skip).
        assert not _is_within(_canonical(str(ws)),
                              _canonical(os.path.join(outside, "f.txt")))
        return

    out, spy = _patch(agent, _replace_args("d/f.txt", "OUTSIDE", "pwned"), task)
    assert out.blocked is True
    assert "refused" in json.loads(out.result)["error"]
    # The out-of-grant file was NOT modified.
    with open(os.path.join(outside, "f.txt"), "rb") as fh:
        assert fh.read() == b"OUTSIDE"
    assert _committed(task, admitted, effects_db) == []
    assert spy.called is False and exec_spy_disabled.called is False


def test_crash_after_effect_left_unknown(
    tmp_path, managed_env, exec_spy_disabled, monkeypatch
):
    """A host-IO failure DURING the mutation phase (after the effect is claimed and
    the single-use authorization consumed) must leave the effect non-terminal
    ``unknown`` — never committed, never blind-replayed. The outcome is genuinely
    indeterminate (the write may have partially applied), so ``unknown`` +
    reconciliation is the correct classification (contrast: a pre-execution
    rejection produces zero effect row — see the move/patch pre-execution tests).

    Failure injection is at the router's host-IO boundary (``_host_write``), so the
    claim + consume have already happened when the mutation raises.
    """
    import youtab_runtime.managed_fs_router as _router

    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-crash-1", "nonce-mx-crash-00000001")
    target = ws / "f.txt"
    task = "task-mx-crash-1"
    content = b"crash payload"
    final_args = {"path": "f.txt", "content": content.decode()}

    _drive(agent, "write_file", dict(final_args), task, _ExecuteSpy("write_file"))
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=content, descriptor=final_args, perms={"write"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-crash-0000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write")]

    # The effect is claimed + authorization consumed, THEN the host-IO write fails.
    monkeypatch.setattr(
        _router, "_host_write",
        lambda *a, **k: _router.RouteOutcome(blocked=True, reason="simulated host-IO crash"))
    out = _drive(agent, "write_file", dict(final_args), task, _ExecuteSpy("write_file"))
    assert out.blocked is True
    assert "reconciliation" in json.loads(out.result)["error"]
    assert _committed(task, admitted, effects_db) == []
    all_effects = list_effects_in_workspace(task, _principal(admitted), WS,
                                            db_path=effects_db)
    assert len(all_effects) == 1 and all_effects[0].state.value == "unknown"
    assert exec_spy_disabled.called is False


def test_foreign_workspace_receipt_is_invisible(tmp_path, managed_env, exec_spy_disabled):
    ws, effects_db, admitted, agent, signer, key_id = _make(
        tmp_path, "cmd-mx-fws-1", "nonce-mx-fws-0000000001")
    target = ws / "f.txt"
    target.write_bytes(b"hello world")
    task = "task-mx-fws-1"
    final_args = _replace_args("f.txt", "world", "there")

    _patch(agent, final_args, task)  # emit proposal
    edigest, proposal_id, rd = _op_identity(
        admitted, ws, operation="write", requested_path="f.txt",
        digest_bytes=_replace_bytes("world", "there"), descriptor=final_args,
        perms={"read", "write"})
    agent._effect_authorizations = [_sign_auth(
        signer, key_id, authorization_id="authz-fws-00000001", admitted=admitted,
        effect_digest=edigest, proposal_id=proposal_id, request_digest=rd,
        operation="write")]

    out, spy = _patch(agent, final_args, task)
    body = json.loads(out.result)
    assert out.blocked is False
    effect_id = body["receipt"]["effect_id"]
    principal = _principal(admitted)
    # Visible in the bound workspace ...
    assert get_effect_in_workspace(effect_id, principal, WS,
                                   db_path=effects_db) is not None
    # ... invisible from any other workspace (fails closed, returns None).
    assert get_effect_in_workspace(effect_id, principal, "other-ws",
                                   db_path=effects_db) is None
    assert spy.called is False and exec_spy_disabled.called is False
