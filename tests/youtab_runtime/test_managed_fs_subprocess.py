"""Cross-process managed filesystem authorized-effect execution proof (item 9).

The function-level suites prove the fs spine (folder grant -> signed
:class:`EffectAuthorization` -> effect ledger + worker lease -> TOCTOU-safe open
-> workspace-bound receipt) within one process. This suite proves the part only a
REAL second OS process can: the sealed :class:`AdmittedCommand` cannot cross a
process boundary, so a genuine worker subprocess must reconstruct it from the
PERSISTED Simorgh grant + capability manifest + effective binding, then drive
:func:`youtab_runtime.managed_fs_gate.enforce_managed_fs_effect` end to end and
perform the ACTUAL host IO.

Chain covered here (hermetic, no live model, no HTTP):

    persist grant + manifest + binding  ->  spawn worker PROCESS  ->
    establish_managed_admission (re-admit, different PID)  ->
    enforce_managed_fs_effect (folder grant -> signed authorization verify+consume
    -> effect-ledger claim + worker lease -> TOCTOU-safe host open)  ->
    real host write/read  ->  settle  ->  workspace-bound receipt

The signed authorization is minted in the PARENT (which holds the test authority's
private key) for the exact ``(operation, canonical path, workspace, content)``
digest, serialized to JSON, and handed to the worker; the worker reconstructs and
verifies it. Every assertion checks BOTH the filesystem state AND the ledger /
receipt state. The negative matrix asserts fail-closed with ZERO side effect (no
file written) and, for pre-claim rejections, ZERO ledger row.

Runnable on Linux CI and locally (a plain ``python`` subprocess over temp SQLite
DBs; NOT the dispatcher's live-model CLI spawn).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from youtab_agent_cli import capability_manifest as cm
from youtab_agent_cli import kanban_db as kb
from youtab_runtime.approval import compute_effect_digest, content_digest
from youtab_runtime.contracts import BrainCommandEnvelopeV2
from youtab_runtime.effect_authorization import (
    TEST_AUTHORITY_KEY_PREFIX,
    TestEffectAuthority,
)
from youtab_runtime.effect_ledger import compute_effect_id, get_effect_in_workspace
from youtab_runtime.folder_grant import FolderGrant, resolve_within_grant
from youtab_runtime.managed_execution import (
    AdmissionIdentity,
    admit_managed_run,
)
from youtab_runtime.policy import AuthorityBoundary
from youtab_runtime.run_journal import Principal
from tools.registry import ToolRegistry, ToolSpec

REPO_ROOT = Path(__file__).resolve().parents[2]

KEY_ID = "brain-ed25519-fssubproc"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([41]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_KEYS_JSON = json.dumps({KEY_ID: _PUB})

TENANT = "tenant-alpha"
USER = "user-eiman"
WORKSPACE = "-"
COMMAND_ID = "cmd-fssubproc001"
_ACTION = {"read": "fs.read", "write": "fs.write", "create": "fs.create"}


# ── grant / ingress plumbing (mirrors test_managed_execution_subprocess) ──────


def _grant_header(*, signer=_SIGNER, nonce, now, task_id, **over):
    fields = dict(
        schema_version="youtab.agent-command.v2",
        issuer="youtab-one-brain",
        audience="youtab-agent-runtime",
        protocol_version="youtab.runtime-sig.v2",
        command_id=COMMAND_ID,
        task_id=task_id,
        root_run_id=task_id,
        parent_task_id=None,
        attempt=1,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        user_id=USER,
        membership_generation=1,
        authorization_epoch=1,
        agent_id="agent-default",
        engine_id="engine-local",
        trace_id="trace-fssubproc",
        nonce=nonce,
        objective="write a file under a folder grant",
        allowed_toolsets=("*",),
        allowed_memory_scopes=("*",),
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
        issued_at=now,
        expires_at=now + timedelta(minutes=30),
        key_id=KEY_ID,
        signature="0" * 88,
    )
    fields.update(over)
    env = BrainCommandEnvelopeV2(**fields)
    sig = base64.b64encode(signer.sign(env.canonical_payload())).decode()
    grant = env.model_dump(mode="json")
    grant["signature"] = sig
    return env, base64.b64encode(
        json.dumps(grant, separators=(",", ":")).encode()
    ).decode()


def _ingress_manifest(envelope):
    """Freeze the capability manifest the ingress endpoint would, from a synthetic
    registry (its contents are irrelevant to the fs gate, but a managed run MUST
    carry a valid, integrity-hashed manifest to re-admit)."""
    reg = ToolRegistry()
    reg.register_spec(
        ToolSpec(
            name="safe_read", toolset="reader", schema={"name": "safe_read"},
            handler=lambda a, **k: "{}", side_effect_class="read",
            provenance="test", version="1.0.0",
        )
    )
    return cm.build_ingress_binding(envelope, registry=reg)


def _persist_run(db_path, task_id, *, grant_header, manifest_payload):
    """Persist the grant + manifest + canonical effective binding exactly as the
    ingress + create_run path does, so the worker re-admits and reaches the fs
    gate."""
    from youtab_agent_cli import effective_binding as _eb
    from youtab_runtime import managed_execution as _mx

    conn = kb.connect(db_path=db_path)
    try:
        with kb.write_txn(conn):
            kb._append_event(
                conn, task_id, "runtime_execution_grant", {"grant": grant_header}
            )
            kb._append_event(
                conn, task_id, "runtime_capability_manifest", manifest_payload
            )
            env = _mx.decode_grant_header(grant_header)
            binding = _eb.build_effective_binding(
                provider="ollama", model="qwen:test",
                endpoint="http://127.0.0.1:11434",
                run_id=task_id, root_run_id=getattr(env, "root_run_id", task_id),
                tenant=getattr(env, "tenant_id", None),
                workspace=getattr(env, "workspace_id", None),
            )
            kb._append_event(conn, task_id, _eb.BINDING_EVENT, binding)
    finally:
        conn.close()


# ── parent-side authority derivation (parent holds the private key) ───────────


def _resolve_safe_path(workspace_root, requested_path, operation):
    """Resolve ``requested_path`` exactly as the worker's fs gate will, so the
    parent can bind the signed authorization to the identical effect digest."""
    grant = FolderGrant(
        grant_id=COMMAND_ID, tenant_id=TENANT, principal_id=USER,
        workspace_id=WORKSPACE, canonical_root=str(workspace_root),
        permissions=frozenset({operation}),
    )
    return resolve_within_grant(
        grant, requested_path, operation=operation,
        tenant_id=TENANT, principal_id=USER, workspace_id=WORKSPACE,
    )


def _effect_identity(workspace_root, requested_path, operation, content, run_id):
    """Return ``(safe_path, effect_digest, effect_id)`` the worker will derive."""
    safe_path = _resolve_safe_path(workspace_root, requested_path, operation)
    cdigest = content_digest(content)
    edigest = compute_effect_digest(operation, safe_path, WORKSPACE, cdigest)
    scope = {"ws": WORKSPACE, "path": safe_path, "content": cdigest}
    effect_id = compute_effect_id(
        run_id, Principal(TENANT, USER), _ACTION[operation], scope
    )
    return safe_path, edigest, effect_id


def _mint(
    authority, *, effect_digest, operation, authorization_id, now,
    tenant_id=TENANT, user_id=USER, workspace_id=WORKSPACE,
    ttl=timedelta(hours=1), issued_shift=timedelta(minutes=-1),
):
    return authority.mint(
        authorization_id=authorization_id,
        tenant_id=tenant_id, user_id=user_id, workspace_id=workspace_id,
        command_id=COMMAND_ID, capability="fs.effect", operation=operation,
        effect_digest=effect_digest,
        issued_at=now + issued_shift, expires_at=now + ttl,
    )


# ── worker: the REAL second process that drives the fs gate ───────────────────

_WORKER_SCRIPT = r'''
import base64, json, os, sys
from pathlib import Path

out_path = os.environ["WORKER_RESULT"]
result = {"pid": os.getpid()}


def _finish(code):
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    sys.exit(code)


class _Agent:
    _admitted_command = None


try:
    from youtab_agent_cli.worker_admission import establish_managed_admission
    agent = _Agent()
    establish_managed_admission(agent)
    result["established"] = agent._admitted_command is not None

    from youtab_runtime.effect_authorization import EffectAuthorization
    from youtab_runtime.managed_fs_gate import enforce_managed_fs_effect, settle

    op = os.environ["FS_OPERATION"]
    content_b64 = os.environ.get("FS_CONTENT_B64", "")
    content = base64.b64decode(content_b64) if op != "read" else None
    keyring = json.loads(os.environ["FS_KEYRING_JSON"])
    auth = EffectAuthorization.model_validate_json(
        Path(os.environ["FS_AUTH_JSON"]).read_text(encoding="utf-8")
    )

    effects_db = Path(os.environ["FS_EFFECTS_DB"])
    fd, effect = enforce_managed_fs_effect(
        agent._admitted_command,
        operation=op,
        requested_path=os.environ["FS_REQUESTED_PATH"],
        workspace_root=Path(os.environ["FS_WORKSPACE_ROOT"]),
        run_id=os.environ["FS_RUN_ID"],
        authorization=auth,
        owner_token=os.environ["FS_OWNER_TOKEN"],
        content=content,
        production=False,
        test_authority_keys=keyring,
        db_path=effects_db,
    )
    result["effect_id"] = effect.effect_id
    result["won"] = bool(effect.won)
    result["state"] = effect.state

    if fd is not None:
        try:
            if op == "read":
                data = os.read(fd, 1 << 20)
                result["read_b64"] = base64.b64encode(data).decode("ascii")
            else:
                os.write(fd, content)
        finally:
            os.close(fd)
        result["wrote"] = True
        result["settled"] = settle(
            effect, agent._admitted_command, committed=True,
            db_path=effects_db,
        )
    else:
        result["wrote"] = False
    _finish(0)
except SystemExit:
    raise
except BaseException as exc:  # noqa: BLE001 - any failure must fail closed, captured
    result["wrote"] = result.get("wrote", False)
    result["error_type"] = type(exc).__name__
    result["error"] = str(exc)
    _finish(4)
'''


def _run_fs_worker(tmp_path, db_path, task_id, *, fs_env):
    script = tmp_path / "fs_worker.py"
    script.write_text(_WORKER_SCRIPT, encoding="utf-8")
    result_file = tmp_path / f"fs_result_{task_id}_{fs_env['FS_RUN_TAG']}.json"
    env = dict(os.environ)
    env.pop("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", None)
    env["YOUTAB_RUNTIME_TRUST_MODE"] = "managed"
    env["YOUTAB_BRAIN_PUBLIC_KEYS"] = _KEYS_JSON
    env["YOUTAB_AGENT_KANBAN_DB"] = str(db_path)
    env["YOUTAB_AGENT_KANBAN_TASK"] = task_id
    env["YOUTAB_AGENT_HOME"] = str(tmp_path / "home")
    env["WORKER_RESULT"] = str(result_file)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    for key, value in fs_env.items():
        if key != "FS_RUN_TAG":
            env[key] = value

    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        **popen_kwargs,
    )
    try:
        _out, err = proc.communicate(timeout=120)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        _out, err = proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        err = (err or "") + "\n[worker timed out; process tree killed]"
    data = {}
    if result_file.exists():
        data = json.loads(result_file.read_text(encoding="utf-8"))
    data["_stderr"] = (err or "")[-2000:]
    return rc, data


def _kill_process_tree(proc):
    try:
        if os.name == "posix":
            import signal as _signal

            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


# ── test scaffolding ──────────────────────────────────────────────────────────


def _prepare(tmp_path, task_id, nonce, now):
    """Create the workspace dir, persist grant+manifest+binding, return the ws dir
    + effects db path. Also runs the parent ingress admission (burns the nonce)
    like the positive control, proving the same grant admits at ingress."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "kanban.db"
    effects_db = tmp_path / "effects.db"
    envelope, header = _grant_header(nonce=nonce, now=now, task_id=task_id)
    binding = _ingress_manifest(envelope)
    admit_managed_run(
        grant_header=header,
        identity=AdmissionIdentity(tenant=TENANT, user=USER, workspace=WORKSPACE),
        boundary=AuthorityBoundary(),
        public_keys={KEY_ID: _PUB},
        now=now,
        capability_binding=binding,
    )
    _persist_run(
        db_path, task_id,
        grant_header=header, manifest_payload=cm.binding_to_persisted(binding),
    )
    return workspace, db_path, effects_db


def _fs_env(*, operation, requested_path, workspace_root, run_id, auth, effects_db,
            keyring, content, owner_token, run_tag):
    auth_file = Path(workspace_root).parent / f"auth_{run_tag}.json"
    auth_file.write_text(auth.model_dump_json(), encoding="utf-8")
    env = {
        "FS_OPERATION": operation,
        "FS_REQUESTED_PATH": requested_path,
        "FS_WORKSPACE_ROOT": str(workspace_root),
        "FS_RUN_ID": run_id,
        "FS_AUTH_JSON": str(auth_file),
        "FS_EFFECTS_DB": str(effects_db),
        "FS_KEYRING_JSON": json.dumps(keyring),
        "FS_OWNER_TOKEN": owner_token,
        "FS_RUN_TAG": run_tag,
    }
    if operation != "read":
        env["FS_CONTENT_B64"] = base64.b64encode(content).decode("ascii")
    return env


def _now():
    return datetime.now(UTC).replace(microsecond=0)


# ── POSITIVE ──────────────────────────────────────────────────────────────────


def test_authorized_write_executes_and_receipts(tmp_path):
    task_id = "task-fs-write"
    content = b"managed authorized write payload\n"
    now = _now()
    workspace, db_path, effects_db = _prepare(
        tmp_path, task_id, "grant-fs-write-0123456789abcdef012345", now
    )
    _safe, edigest, effect_id = _effect_identity(
        workspace, "data.txt", "write", content, task_id
    )
    signer = TestEffectAuthority()
    auth = _mint(
        signer, effect_digest=edigest, operation="write",
        authorization_id="authz-fs-write-000001", now=now,
    )
    rc, res = _run_fs_worker(
        tmp_path, db_path, task_id,
        fs_env=_fs_env(
            operation="write", requested_path="data.txt", workspace_root=workspace,
            run_id=task_id, auth=auth, effects_db=effects_db,
            keyring=signer.keyring(), content=content, owner_token="lease-token-0001",
            run_tag="write",
        ),
    )
    assert rc == 0, res.get("_stderr")
    assert res["established"] is True
    assert res["pid"] != os.getpid()  # genuinely a second OS process
    assert res["won"] is True and res["wrote"] is True
    assert res["settled"] == "committed"
    assert res["effect_id"] == effect_id
    # filesystem state
    assert (workspace / "data.txt").read_bytes() == content
    # ledger / receipt state (workspace-bound)
    record = get_effect_in_workspace(
        effect_id, Principal(TENANT, USER), WORKSPACE, db_path=effects_db
    )
    assert record is not None
    assert record.is_terminal is True
    assert record.state.value == "committed"


def test_authorized_read_executes(tmp_path):
    task_id = "task-fs-read"
    file_bytes = b"pre-existing readable content\n"
    now = _now()
    workspace, db_path, effects_db = _prepare(
        tmp_path, task_id, "grant-fs-read-0123456789abcdef0123456", now
    )
    (workspace / "input.txt").write_bytes(file_bytes)
    _safe, edigest, effect_id = _effect_identity(
        workspace, "input.txt", "read", None, task_id
    )
    signer = TestEffectAuthority()
    auth = _mint(
        signer, effect_digest=edigest, operation="read",
        authorization_id="authz-fs-read-0000001", now=now,
    )
    rc, res = _run_fs_worker(
        tmp_path, db_path, task_id,
        fs_env=_fs_env(
            operation="read", requested_path="input.txt", workspace_root=workspace,
            run_id=task_id, auth=auth, effects_db=effects_db,
            keyring=signer.keyring(), content=None, owner_token="lease-token-0002",
            run_tag="read",
        ),
    )
    assert rc == 0, res.get("_stderr")
    assert res["won"] is True and res["wrote"] is True
    assert base64.b64decode(res["read_b64"]) == file_bytes
    record = get_effect_in_workspace(
        effect_id, Principal(TENANT, USER), WORKSPACE, db_path=effects_db
    )
    assert record is not None and record.state.value == "committed"


# ── NEGATIVE: fail-closed, zero side effect, zero ledger row (pre-claim) ───────


def _assert_no_ledger_row(effect_id, effects_db):
    assert (
        get_effect_in_workspace(
            effect_id, Principal(TENANT, USER), WORKSPACE, db_path=effects_db
        )
        is None
    )


def test_forged_authorization_rejected(tmp_path):
    task_id = "task-fs-forged"
    content = b"forged-issuer payload\n"
    now = _now()
    workspace, db_path, effects_db = _prepare(
        tmp_path, task_id, "grant-fs-forged-0123456789abcdef01234", now
    )
    _safe, edigest, effect_id = _effect_identity(
        workspace, "data.txt", "write", content, task_id
    )
    trusted = TestEffectAuthority()
    # A DIFFERENT authority whose key id is NOT in the keyring handed to the worker.
    forger = TestEffectAuthority(key_id=TEST_AUTHORITY_KEY_PREFIX + "forger")
    auth = _mint(
        forger, effect_digest=edigest, operation="write",
        authorization_id="authz-fs-forged-00001", now=now,
    )
    rc, res = _run_fs_worker(
        tmp_path, db_path, task_id,
        fs_env=_fs_env(
            operation="write", requested_path="data.txt", workspace_root=workspace,
            run_id=task_id, auth=auth, effects_db=effects_db,
            keyring=trusted.keyring(), content=content, owner_token="lease-token-0003",
            run_tag="forged",
        ),
    )
    assert rc == 4, res.get("_stderr")
    assert res["wrote"] is False
    assert res["error_type"] in {
        "UntrustedIssuerError", "InvalidAuthorizationSignatureError"
    }, res
    assert not (workspace / "data.txt").exists()
    _assert_no_ledger_row(effect_id, effects_db)


def test_expired_authorization_rejected(tmp_path):
    task_id = "task-fs-expired"
    content = b"expired authorization payload\n"
    now = _now()
    workspace, db_path, effects_db = _prepare(
        tmp_path, task_id, "grant-fs-expired-0123456789abcdef0123", now
    )
    _safe, edigest, effect_id = _effect_identity(
        workspace, "data.txt", "write", content, task_id
    )
    signer = TestEffectAuthority()
    auth = _mint(
        signer, effect_digest=edigest, operation="write",
        authorization_id="authz-fs-expired-0001", now=now,
        issued_shift=timedelta(hours=-2), ttl=timedelta(hours=-1),
    )
    rc, res = _run_fs_worker(
        tmp_path, db_path, task_id,
        fs_env=_fs_env(
            operation="write", requested_path="data.txt", workspace_root=workspace,
            run_id=task_id, auth=auth, effects_db=effects_db,
            keyring=signer.keyring(), content=content, owner_token="lease-token-0004",
            run_tag="expired",
        ),
    )
    assert rc == 4, res.get("_stderr")
    assert res["wrote"] is False
    assert res["error_type"] == "InvalidAuthorizationSignatureError", res
    assert "expired" in res["error"]
    assert not (workspace / "data.txt").exists()
    _assert_no_ledger_row(effect_id, effects_db)


def test_wrong_digest_authorization_rejected(tmp_path):
    task_id = "task-fs-digest"
    authorized_content = b"the content the authority signed\n"
    written_content = b"a DIFFERENT payload the worker tries to write\n"
    now = _now()
    workspace, db_path, effects_db = _prepare(
        tmp_path, task_id, "grant-fs-digest-0123456789abcdef01234", now
    )
    # Authorization bound to the digest of the AUTHORIZED content...
    _safe, edigest_ok, _ = _effect_identity(
        workspace, "data.txt", "write", authorized_content, task_id
    )
    # ...but the worker writes DIFFERENT content -> different effect digest/id.
    _safe2, _edigest_bad, effect_id_written = _effect_identity(
        workspace, "data.txt", "write", written_content, task_id
    )
    signer = TestEffectAuthority()
    auth = _mint(
        signer, effect_digest=edigest_ok, operation="write",
        authorization_id="authz-fs-digest-0001", now=now,
    )
    rc, res = _run_fs_worker(
        tmp_path, db_path, task_id,
        fs_env=_fs_env(
            operation="write", requested_path="data.txt", workspace_root=workspace,
            run_id=task_id, auth=auth, effects_db=effects_db,
            keyring=signer.keyring(), content=written_content,
            owner_token="lease-token-0005", run_tag="digest",
        ),
    )
    assert rc == 4, res.get("_stderr")
    assert res["wrote"] is False
    assert res["error_type"] == "ApprovalDigestMismatchError", res
    assert not (workspace / "data.txt").exists()
    _assert_no_ledger_row(effect_id_written, effects_db)


def test_cross_workspace_authorization_rejected(tmp_path):
    task_id = "task-fs-crossws"
    content = b"cross-workspace payload\n"
    now = _now()
    workspace, db_path, effects_db = _prepare(
        tmp_path, task_id, "grant-fs-crossws-0123456789abcdef012", now
    )
    _safe, edigest, effect_id = _effect_identity(
        workspace, "data.txt", "write", content, task_id
    )
    signer = TestEffectAuthority()
    # Authorization bound to a DIFFERENT workspace than the admitted run ("-").
    auth = _mint(
        signer, effect_digest=edigest, operation="write",
        authorization_id="authz-fs-crossws-001", now=now,
        workspace_id="ws-other",
    )
    rc, res = _run_fs_worker(
        tmp_path, db_path, task_id,
        fs_env=_fs_env(
            operation="write", requested_path="data.txt", workspace_root=workspace,
            run_id=task_id, auth=auth, effects_db=effects_db,
            keyring=signer.keyring(), content=content, owner_token="lease-token-0006",
            run_tag="crossws",
        ),
    )
    assert rc == 4, res.get("_stderr")
    assert res["wrote"] is False
    assert res["error_type"] == "ApprovalBindingError", res
    assert not (workspace / "data.txt").exists()
    _assert_no_ledger_row(effect_id, effects_db)


def test_cross_tenant_authorization_rejected(tmp_path):
    task_id = "task-fs-crosstenant"
    content = b"cross-tenant payload\n"
    now = _now()
    workspace, db_path, effects_db = _prepare(
        tmp_path, task_id, "grant-fs-crosstenant-0123456789abc", now
    )
    _safe, edigest, effect_id = _effect_identity(
        workspace, "data.txt", "write", content, task_id
    )
    signer = TestEffectAuthority()
    # Authorization bound to a DIFFERENT tenant than the admitted run.
    auth = _mint(
        signer, effect_digest=edigest, operation="write",
        authorization_id="authz-fs-crosstenant1", now=now,
        tenant_id="tenant-beta",
    )
    rc, res = _run_fs_worker(
        tmp_path, db_path, task_id,
        fs_env=_fs_env(
            operation="write", requested_path="data.txt", workspace_root=workspace,
            run_id=task_id, auth=auth, effects_db=effects_db,
            keyring=signer.keyring(), content=content, owner_token="lease-token-0007",
            run_tag="crosstenant",
        ),
    )
    assert rc == 4, res.get("_stderr")
    assert res["wrote"] is False
    assert res["error_type"] == "ApprovalBindingError", res
    assert not (workspace / "data.txt").exists()
    _assert_no_ledger_row(effect_id, effects_db)


def test_grant_escape_rejected(tmp_path):
    task_id = "task-fs-escape"
    content = b"escape attempt payload\n"
    now = _now()
    workspace, db_path, effects_db = _prepare(
        tmp_path, task_id, "grant-fs-escape-0123456789abcdef01234", now
    )
    signer = TestEffectAuthority()
    # A validly-signed authorization the worker never reaches: resolution fails
    # first because the requested path escapes the grant root ("..").
    auth = _mint(
        signer, effect_digest="0" * 64, operation="write",
        authorization_id="authz-fs-escape-0001", now=now,
    )
    escaped_target = tmp_path / "escape.txt"
    rc, res = _run_fs_worker(
        tmp_path, db_path, task_id,
        fs_env=_fs_env(
            operation="write", requested_path="../escape.txt",
            workspace_root=workspace, run_id=task_id, auth=auth,
            effects_db=effects_db, keyring=signer.keyring(), content=content,
            owner_token="lease-token-0008", run_tag="escape",
        ),
    )
    assert rc == 4, res.get("_stderr")
    assert res["wrote"] is False
    assert res["error_type"] == "GrantScopeError", res
    assert not escaped_target.exists()
    # Resolution fails BEFORE the authorization is consumed or any effect claimed,
    # so no effects ledger DB is ever created.
    assert not effects_db.exists()


# ── NEGATIVE: replay — at-most-once, no double write, no second commit ─────────


def test_replay_does_not_double_write_or_double_commit(tmp_path):
    task_id = "task-fs-replay"
    content = b"first-and-only committed write\n"
    now = _now()
    workspace, db_path, effects_db = _prepare(
        tmp_path, task_id, "grant-fs-replay-0123456789abcdef01234", now
    )
    _safe, edigest, effect_id = _effect_identity(
        workspace, "data.txt", "write", content, task_id
    )
    signer = TestEffectAuthority()
    auth = _mint(
        signer, effect_digest=edigest, operation="write",
        authorization_id="authz-fs-replay-0001", now=now,
    )

    def run(run_tag):
        return _run_fs_worker(
            tmp_path, db_path, task_id,
            fs_env=_fs_env(
                operation="write", requested_path="data.txt",
                workspace_root=workspace, run_id=task_id, auth=auth,
                effects_db=effects_db, keyring=signer.keyring(), content=content,
                owner_token="lease-token-0009", run_tag=run_tag,
            ),
        )

    # First invocation: authorizes, claims, writes, commits.
    rc1, res1 = run("replay1")
    assert rc1 == 0, res1.get("_stderr")
    assert res1["won"] is True and res1["wrote"] is True
    assert res1["settled"] == "committed"
    assert (workspace / "data.txt").read_bytes() == content
    record1 = get_effect_in_workspace(
        effect_id, Principal(TENANT, USER), WORKSPACE, db_path=effects_db
    )
    assert record1 is not None and record1.state.value == "committed"
    attempts_after_first = record1.attempts

    # Second invocation of the SAME authorization + effects DB: must NOT re-write
    # and must NOT create a second commit. Either the claim does not win (already
    # committed effect) or the single-use authorization is refused as consumed.
    rc2, res2 = run("replay2")
    assert res2.get("wrote") is False, res2
    if rc2 == 0:
        # Effect already registered -> claim did not win (fd None), no re-verify.
        assert res2["won"] is False
        assert res2["state"] == "committed"
        assert res2["effect_id"] == effect_id
    else:
        # Or the one-time authorization id was already consumed -> fail closed.
        assert rc2 == 4
        assert res2["error_type"] == "ApprovalConsumedError", res2

    # Filesystem unchanged (no second, different write) and still exactly one
    # committed effect whose attempt count did not advance from a second claim.
    assert (workspace / "data.txt").read_bytes() == content
    record2 = get_effect_in_workspace(
        effect_id, Principal(TENANT, USER), WORKSPACE, db_path=effects_db
    )
    assert record2 is not None and record2.state.value == "committed"
    assert record2.attempts == attempts_after_first
