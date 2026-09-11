"""Real cross-process managed-execution integration (WAVE-30H correction 1).

Function-level e2e already proves the admission chain within one process
(``test_managed_execution_e2e``). This suite proves the part that only a REAL
second OS process can: the sealed :class:`AdmittedCommand` cannot cross a process
boundary, so a genuine worker subprocess must reconstruct it from the PERSISTED
Simorgh grant + capability manifest and gate an ACTUAL ``tool_executor``
invocation.

Chain covered here (hermetic, no live model, no HTTP):

    persist grant+manifest  ->  spawn worker PROCESS  ->  establish_managed_admission
    (re_admit, different PID)  ->  enforce_managed_tool_authority on real tools

with positive control + negative controls (missing grant, tampered manifest at
the worker; forged / expired / replayed / cross-tenant / missing at ingress).

This is runnable on Linux CI and locally (the subprocess is plain ``python`` over
a temp SQLite DB — NOT the dispatcher's live-model CLI spawn, which is exercised
separately). The full HTTP create_run + HMAC ingress hop is covered by the
connector golden tests + ``admit_managed_run`` unit tests; this file is the
cross-process authorization link.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from youtab_agent_cli import capability_manifest as cm
from youtab_agent_cli import kanban_db as kb
from youtab_runtime.contracts import BrainCommandEnvelopeV2
from youtab_runtime.managed_execution import (
    AdmissionIdentity,
    ManagedAdmissionError,
    admit_managed_run,
)
from youtab_runtime.policy import AuthorityBoundary
from tools.registry import ToolRegistry, ToolSpec

REPO_ROOT = Path(__file__).resolve().parents[2]

KEY_ID = "brain-ed25519-subproc"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([23]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_KEYS_JSON = json.dumps({KEY_ID: _PUB})
_NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


def _grant_header(
    *, signer=_SIGNER, toolsets=("*",), mem_scopes=("*",), nonce=None, now=_NOW, **over
):
    fields = dict(
        schema_version="youtab.agent-command.v2",
        issuer="youtab-one-brain",
        audience="youtab-agent-runtime",
        protocol_version="youtab.runtime-sig.v2",
        command_id="cmd-subproc0001",
        task_id="task-subproc0001",
        root_run_id="run-subproc0001",
        parent_task_id=None,
        attempt=1,
        tenant_id="tenant-alpha",
        workspace_id="-",
        user_id="user-eiman",
        membership_generation=1,
        authorization_epoch=1,
        agent_id="agent-default",
        engine_id="engine-local",
        trace_id="trace-subproc01",
        nonce=nonce or "grant-subproc-0123456789abcdef0123456789",
        objective="do the thing",
        allowed_toolsets=toolsets,
        allowed_memory_scopes=mem_scopes,
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
    return env, base64.b64encode(json.dumps(grant, separators=(",", ":")).encode()).decode()


def _ingress_manifest(envelope):
    """Freeze the capability manifest the ingress would, from a synthetic registry
    holding one read tool + one effectful tool (no 'late_tool')."""
    reg = ToolRegistry()
    reg.register_spec(
        ToolSpec(
            name="safe_read",
            toolset="reader",
            schema={"name": "safe_read"},
            handler=lambda a, **k: "{}",
            side_effect_class="read",
            provenance="test",
            version="1.0.0",
        )
    )
    reg.register_spec(
        ToolSpec(
            name="do_write",
            toolset="writer",
            schema={"name": "do_write"},
            handler=lambda a, **k: "{}",
            side_effect_class="write",
            provenance="test",
            version="1.0.0",
        )
    )
    return cm.build_ingress_binding(envelope, registry=reg)


def _persist_run(db_path: Path, task_id: str, *, grant_header, manifest_payload,
                 seed_binding=True, binding_run_id=None, binding_workspace=None):
    """Persist the grant (+ optional manifest) exactly as the ingress endpoint does.

    WAVE-30H: also seeds the canonical effective binding the real create_run always
    persists, keyed to the grant's tenant so the worker's pre-dispatch gate admits
    it. Pass ``seed_binding=False`` to exercise the missing-binding fail-closed path.
    ``binding_run_id`` / ``binding_workspace`` override the binding's run/workspace
    scope so an ADVERSARIAL test can seed a wholesale, self-consistent binding lifted
    from another run/workspace of the SAME tenant (the run-scope / workspace-scope
    substitution controls). Both default to this run's own authoritative scope
    (task_id / the grant's workspace) — a legitimate binding.
    """
    from youtab_agent_cli import effective_binding as _eb
    from youtab_runtime import managed_execution as _mx

    conn = kb.connect(db_path=db_path)
    try:
        with kb.write_txn(conn):
            if grant_header is not None:
                kb._append_event(conn, task_id, "runtime_execution_grant", {"grant": grant_header})
            if manifest_payload is not None:
                kb._append_event(conn, task_id, "runtime_capability_manifest", manifest_payload)
            if seed_binding and grant_header is not None:
                env = _mx.decode_grant_header(grant_header)
                _run = binding_run_id or task_id
                _ws = (
                    binding_workspace
                    if binding_workspace is not None
                    else getattr(env, "workspace_id", None)
                )
                binding = _eb.build_effective_binding(
                    provider="ollama", model="qwen:test",
                    endpoint="http://127.0.0.1:11434",
                    run_id=_run, root_run_id=getattr(env, "root_run_id", _run),
                    tenant=getattr(env, "tenant_id", None),
                    workspace=_ws,
                )
                kb._append_event(conn, task_id, _eb.BINDING_EVENT, binding)
    finally:
        conn.close()


_WORKER_SCRIPT = r'''
import json, os, sys

# Register the same synthetic tools so effect-class resolves in this process.
from tools.registry import registry, ToolSpec
registry.register_spec(ToolSpec(name="safe_read", toolset="reader",
    schema={"name": "safe_read"}, handler=lambda a, **k: "{}",
    side_effect_class="read", provenance="test", version="1.0.0"))
registry.register_spec(ToolSpec(name="do_write", toolset="writer",
    schema={"name": "do_write"}, handler=lambda a, **k: "{}",
    side_effect_class="write", provenance="test", version="1.0.0"))

from youtab_agent_cli.worker_admission import (
    establish_managed_admission, ManagedWorkerAdmissionError,
)

class _Agent:
    _admitted_command = None

agent = _Agent()
out_path = os.environ["WORKER_RESULT"]
try:
    established = establish_managed_admission(agent)
except ManagedWorkerAdmissionError as exc:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"admission_error": str(exc), "pid": os.getpid()}, f)
    sys.exit(3)

from agent.tool_executor import enforce_managed_tool_authority

# R5: the shared execution-tree budget was opened in THIS worker process from the
# grant's reasoning; debit one iteration to prove it is live and shared.
from youtab_runtime import execution_tree_budget as etb
tree_root = getattr(agent, "_execution_tree_root", None)
tree_snap = None
if tree_root is not None:
    etb.consume(tree_root, iterations=1, tokens=10)
    s = etb.snapshot(tree_root)
    tree_snap = {"iterations_used": s.iterations_used, "max_iterations": s.max_iterations,
                 "tokens_used": s.tokens_used}

result = {
    "established": bool(established),
    "has_admitted": agent._admitted_command is not None,
    "pid": os.getpid(),
    "tree_root": tree_root,
    "tree_snap": tree_snap,
    # a read tool in the frozen manifest -> allowed (None)
    "read_block": enforce_managed_tool_authority(agent, "safe_read", {}),
    # an effectful tool in the manifest -> blocked (effect gate)
    "write_block": enforce_managed_tool_authority(agent, "do_write", {}),
    # a tool NOT in the frozen manifest -> blocked even under "*"
    "late_block": enforce_managed_tool_authority(agent, "late_tool", {}),
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f)
'''


def _run_worker(tmp_path: Path, db_path: Path, task_id: str) -> tuple[int, dict]:
    script = tmp_path / "worker_script.py"
    script.write_text(_WORKER_SCRIPT, encoding="utf-8")
    result_file = tmp_path / f"worker_result_{task_id}.json"
    env = dict(os.environ)
    # Deterministic fail-closed: never let a stray legacy-binding migration flag from
    # the developer's shell quarantine (i.e. excuse) a missing binding in these tests.
    env.pop("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", None)
    env["YOUTAB_RUNTIME_TRUST_MODE"] = "managed"
    env["YOUTAB_BRAIN_PUBLIC_KEYS"] = _KEYS_JSON
    env["YOUTAB_AGENT_KANBAN_DB"] = str(db_path)
    env["YOUTAB_AGENT_KANBAN_TASK"] = task_id
    env["YOUTAB_AGENT_HOME"] = str(tmp_path / "home")
    env["WORKER_RESULT"] = str(result_file)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    # Spawn the worker in its OWN process group / session so that on a timeout or
    # a cancelled test run we can tear down the WHOLE tree (worker + any grandchild
    # it spawned) — never leaving a detached process behind. POSIX: start_new_session
    # + killpg; Windows: CREATE_NEW_PROCESS_GROUP + taskkill /T.
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Kill the worker and every descendant, leaving nothing detached behind."""
    try:
        if os.name == "posix":
            import signal as _signal

            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
            )
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


# ── positive control: real worker process gates real tools ───────────────────


def test_worker_subprocess_readmits_and_gates_real_tools(tmp_path, monkeypatch):
    task_id = "task-subproc0001"
    db_path = tmp_path / "kanban.db"

    # Anchor the grant window to the real clock: the worker subprocess re-admits
    # with its own wall-clock, so a fixed past instant would read as expired.
    now = datetime.now(UTC).replace(microsecond=0)
    envelope, header = _grant_header(now=now)
    # Ingress admission (verifies + burns the nonce, builds the sealed context).
    binding = _ingress_manifest(envelope)
    admitted = admit_managed_run(
        grant_header=header,
        identity=AdmissionIdentity(tenant="tenant-alpha", user="user-eiman", workspace="-"),
        boundary=AuthorityBoundary(),
        public_keys={KEY_ID: _PUB},
        now=now,
        capability_binding=binding,
    )
    assert admitted.capability_binding is not None
    _persist_run(
        db_path, task_id,
        grant_header=header, manifest_payload=cm.binding_to_persisted(binding),
    )

    rc, res = _run_worker(tmp_path, db_path, task_id)
    assert rc == 0, res.get("_stderr")
    assert res["established"] is True and res["has_admitted"] is True
    # genuinely a different process
    assert res["pid"] != os.getpid()
    # read tool authorized (in manifest, read effect) -> not blocked
    assert res["read_block"] is None, res["read_block"]
    # effectful tool -> blocked (needs Brain Effect Gate)
    assert res["write_block"] is not None
    # a tool registered after admission is NOT swept in by "*"
    assert res["late_block"] is not None
    assert "capability manifest" in res["late_block"]
    # R5: the shared execution-tree budget was opened in the worker and debited
    assert res["tree_root"] == "run-subproc0001"
    assert res["tree_snap"] == {"iterations_used": 1, "max_iterations": 5, "tokens_used": 10}


# ── worker negative controls ─────────────────────────────────────────────────


def test_worker_refuses_when_no_grant_persisted(tmp_path):
    task_id = "task-nogrant"
    db_path = tmp_path / "kanban.db"
    # create schema but persist NO grant event
    _persist_run(db_path, task_id, grant_header=None, manifest_payload=None)
    rc, res = _run_worker(tmp_path, db_path, task_id)
    assert rc == 3, res.get("_stderr")
    assert "admission_error" in res


def test_worker_refuses_tampered_manifest(tmp_path):
    task_id = "task-tampered"
    db_path = tmp_path / "kanban.db"
    envelope, header = _grant_header(nonce="grant-subproc-tampered-0123456789abcd")
    binding = _ingress_manifest(envelope)
    persisted = cm.binding_to_persisted(binding)
    persisted["tool_hashes"].append(["exfiltrate", "deadbeef"])  # widen, hash now stale
    _persist_run(db_path, task_id, grant_header=header, manifest_payload=persisted)
    rc, res = _run_worker(tmp_path, db_path, task_id)
    assert rc == 3, res.get("_stderr")
    assert "admission_error" in res


def _assert_refused_before_any_execution(tmp_path, rc, res, expected_error):
    """A pre-dispatch refusal that PROVES nothing executed.

    Asserts rc 3 and the EXACT structured admission error by EQUALITY (never a
    substring that could accidentally match the task id), then proves the worker
    exited AT admission — before any provider client, tool call, or budget: none of
    the execution-phase result keys are present, and the execution-tree budget DB was
    never even created (``open_tree``/``consume`` run only AFTER admission succeeds),
    so budget consumption == 0 and provider-call count == 0.
    """
    assert rc == 3, res.get("_stderr")
    assert res.get("admission_error") == expected_error, res.get("admission_error")
    for k in ("established", "has_admitted", "tree_root", "tree_snap",
              "read_block", "write_block", "late_block"):
        assert k not in res, f"worker executed past admission (key {k!r} present)"
    budget_db = tmp_path / "home" / "runtime" / "execution_tree_budget.db"
    assert not budget_db.exists(), "execution-tree budget was opened on a refusal"


def test_worker_refuses_when_binding_missing(tmp_path):
    # WAVE-30H: a managed run with a valid grant + manifest but NO effective binding
    # fails closed at the pre-dispatch gate. Anchor the grant window to the real clock
    # (like the positive control) so re-admission SUCCEEDS and execution genuinely
    # reaches the missing-binding gate — then assert the EXACT structured error and
    # prove rc 3 with zero provider calls, zero budget, no execution-tree snapshot.
    task_id = "task-nobinding"
    db_path = tmp_path / "kanban.db"
    now = datetime.now(UTC).replace(microsecond=0)
    envelope, header = _grant_header(nonce="grant-subproc-nobinding-0123456789ab", now=now)
    persisted = cm.binding_to_persisted(_ingress_manifest(envelope))
    _persist_run(db_path, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=False)
    rc, res = _run_worker(tmp_path, db_path, task_id)
    _assert_refused_before_any_execution(
        tmp_path, rc, res,
        f"managed run {task_id} has no effective binding; refusing "
        "(no fallback, no standalone downgrade)",
    )


def test_worker_refuses_tampered_binding(tmp_path):
    # A persisted binding whose identity was mutated after it was hashed fails the
    # worker's self-hash verification. Non-expired grant so re-admission succeeds and
    # execution reaches the integrity gate; assert the EXACT hash-mismatch error and
    # prove rc 3 with zero provider calls, zero budget, no execution-tree snapshot.
    from youtab_agent_cli import effective_binding as eb

    task_id = "task-tamperedbinding"
    db_path = tmp_path / "kanban.db"
    now = datetime.now(UTC).replace(microsecond=0)
    envelope, header = _grant_header(nonce="grant-subproc-tamperbind-0123456789ab", now=now)
    persisted = cm.binding_to_persisted(_ingress_manifest(envelope))
    _persist_run(db_path, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=False)
    binding = eb.build_effective_binding(
        provider="ollama", model="qwen:test", endpoint="http://127.0.0.1:11434",
        run_id=task_id, root_run_id=getattr(envelope, "root_run_id", task_id),
        tenant=getattr(envelope, "tenant_id", None),
        workspace=getattr(envelope, "workspace_id", None),
    )
    binding["model"] = "evil-swap"  # mutate after hashing -> hash no longer matches
    conn = kb.connect(db_path=db_path)
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, eb.BINDING_EVENT, binding)
    finally:
        conn.close()
    rc, res = _run_worker(tmp_path, db_path, task_id)
    _assert_refused_before_any_execution(
        tmp_path, rc, res,
        f"managed run {task_id} effective binding hash mismatch "
        "(tampered/malformed); refusing",
    )


def test_worker_refuses_cross_run_substituted_binding(tmp_path):
    # ADVERSARIAL run-scope: a wholesale, self-consistent binding lifted from
    # ANOTHER run of the SAME tenant/workspace (valid self-hash, matching tenant)
    # is injected into this run's stream. It passes hash + tenant but its run_id is
    # NOT the run this worker executes (YOUTAB_AGENT_KANBAN_TASK) -> refused at the
    # pre-dispatch gate, rc 3, BEFORE any provider client or budget: the worker
    # never reaches the tree-budget section, so no snapshot is recorded (zero spend,
    # zero provider calls).
    task_id = "task-crossrun"
    db_path = tmp_path / "kanban.db"
    # Anchor the grant window to the real clock so the worker's re-admission (which
    # re-verifies expiry) succeeds and execution reaches the binding gate — the gate
    # under test — rather than being short-circuited by an expired grant.
    now = datetime.now(UTC).replace(microsecond=0)
    envelope, header = _grant_header(nonce="grant-subproc-crossrun-0123456789abc", now=now)
    persisted = cm.binding_to_persisted(_ingress_manifest(envelope))
    _persist_run(db_path, task_id, grant_header=header, manifest_payload=persisted,
                 binding_run_id="task-SOME-OTHER-RUN")
    rc, res = _run_worker(tmp_path, db_path, task_id)
    _assert_refused_before_any_execution(
        tmp_path, rc, res,
        f"managed run {task_id} binding run_id does not match the executing run "
        "(cross-run/stale binding substitution); refusing",
    )


def test_worker_refuses_cross_workspace_substituted_binding(tmp_path):
    # ADVERSARIAL workspace-scope: a self-consistent binding scoped to a DIFFERENT
    # workspace of the SAME tenant. The run is admitted under the grant's workspace
    # ("-"); the binding claims "workspace-beta" -> refused at the pre-dispatch gate,
    # rc 3, BEFORE any provider client or budget (no tree snapshot => zero spend).
    task_id = "task-crossws"
    db_path = tmp_path / "kanban.db"
    # Anchor the grant window to the real clock (see the cross-run test) so
    # re-admission succeeds and execution reaches the workspace-scope gate.
    now = datetime.now(UTC).replace(microsecond=0)
    envelope, header = _grant_header(nonce="grant-subproc-crossws-0123456789abcd", now=now)
    persisted = cm.binding_to_persisted(_ingress_manifest(envelope))
    _persist_run(db_path, task_id, grant_header=header, manifest_payload=persisted,
                 binding_workspace="workspace-beta")
    rc, res = _run_worker(tmp_path, db_path, task_id)
    _assert_refused_before_any_execution(
        tmp_path, rc, res,
        f"managed run {task_id} binding workspace does not match the admitted "
        "workspace (cross-workspace binding substitution); refusing",
    )


# ── ingress negative controls (the admission the endpoint runs) ──────────────


def _ident():
    return AdmissionIdentity(tenant="tenant-alpha", user="user-eiman", workspace="-")


def test_ingress_forged_grant_rejected():
    forged = Ed25519PrivateKey.from_private_bytes(bytes([99]) * 32)
    _, header = _grant_header(signer=forged, nonce="grant-forged-0123456789abcdef012345")
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=header, identity=_ident(),
                          boundary=AuthorityBoundary(), public_keys={KEY_ID: _PUB}, now=_NOW)
    assert ei.value.code == "grant_rejected"


def test_ingress_expired_grant_rejected():
    past = _NOW - timedelta(hours=2)
    _, header = _grant_header(now=past, nonce="grant-expired-0123456789abcdef01234")
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=header, identity=_ident(),
                          boundary=AuthorityBoundary(), public_keys={KEY_ID: _PUB}, now=_NOW)
    assert ei.value.code == "grant_rejected"


def test_ingress_replayed_grant_rejected():
    _, header = _grant_header(nonce="grant-replay-0123456789abcdef0123456")
    boundary = AuthorityBoundary()
    admit_managed_run(grant_header=header, identity=_ident(),
                      boundary=boundary, public_keys={KEY_ID: _PUB}, now=_NOW)
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=header, identity=_ident(),
                          boundary=boundary, public_keys={KEY_ID: _PUB}, now=_NOW)
    assert ei.value.code == "grant_rejected"


def test_ingress_cross_tenant_grant_rejected():
    _, header = _grant_header(nonce="grant-xtenant-0123456789abcdef01234")
    other = AdmissionIdentity(tenant="tenant-beta", user="user-eiman", workspace="-")
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=header, identity=other,
                          boundary=AuthorityBoundary(), public_keys={KEY_ID: _PUB}, now=_NOW)
    assert ei.value.code == "grant_identity_mismatch"


def test_ingress_missing_grant_rejected():
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=None, identity=_ident(),
                          boundary=AuthorityBoundary(), public_keys={KEY_ID: _PUB}, now=_NOW)
    assert ei.value.code == "grant_required"
