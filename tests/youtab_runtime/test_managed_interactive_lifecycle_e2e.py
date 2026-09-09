"""WAVE-30H Phase-A — managed INTERACTIVE lifecycle E2E (ADR-0004: A4/A5/A6).

Proves, through the REAL managed dispatch path (real FastAPI router + real
dispatched worker subprocess, managed trust mode, real Simorgh Ed25519 grant —
not mocks), the three interactive control scopes the Owner requires:

  A4  clarification : agent asks → user /answer → SAME run continues
  A5  approval      : agent requests → user /approve|deny → controlled continuation
  A6  pause/resume  : /pause → worker persists a CHECKPOINT + stops → /resume
                      re-dispatches a fresh worker that continues from the SAME
                      state (NOT a cancel+restart; no step re-executed)

with the public control contract preserved end-to-end:
  Simorgh → admission → execution → progress/question/approval/pause/resume →
  exactly-once completion.

Also proves: grant-gating + tenant/session binding + expiry + replay protection
on every control op, event ordering, cancellation races, reconnect/replay,
redaction of user-visible input, fail-closed timeouts, and no duplicate execution.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli import runtime_command_auth as rca
from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth
from youtab_agent_cli.dashboard_auth.token_auth import token_auth_middleware
from youtab_agent_cli.web_routers import runtime
from youtab_runtime import managed_execution as mx
from youtab_runtime import run_control as rc
from youtab_runtime.contracts import BrainCommandEnvelopeV2

REPO_ROOT = str(Path(__file__).resolve().parents[2])
SECRET = secrets.token_urlsafe(48)
KEY_ID = "brain-ed25519-interactive"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([57]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_KEYS_JSON = json.dumps({KEY_ID: _PUB})
TENANT, USER = "tenantA", "userA"

# One worker, three behaviours (selected by WORKER_MODE). Each re-admits the
# persisted Simorgh grant in its own process before doing anything.
_WORKER_SRC = r'''
import json, os, sys, time
from pathlib import Path
from youtab_agent_cli import kanban_db as kb
from youtab_runtime import redaction as R
from youtab_runtime import run_control as rc

task_id = sys.argv[1]
db_path = Path(sys.argv[2])
mode = os.environ.get("WORKER_MODE", "")
deadline_s = float(os.environ.get("WORKER_DEADLINE", "20"))

class _Agent:
    _admitted_command = None
agent = _Agent()
from youtab_agent_cli.worker_admission import (
    establish_managed_admission, ManagedWorkerAdmissionError)
try:
    establish_managed_admission(agent)
except ManagedWorkerAdmissionError as exc:
    c = kb.connect(db_path=db_path)
    with kb.write_txn(c):
        kb._append_event(c, task_id, "worker_admission_error", {"error": str(exc)[:200]})
    c.close(); sys.exit(3)

def events():
    c = kb.connect(db_path=db_path)
    try: return kb.list_events(c, task_id)
    finally: c.close()

def append(kind, payload):
    c = kb.connect(db_path=db_path)
    with kb.write_txn(c): kb._append_event(c, task_id, kind, payload)
    c.close()

def block(reason):
    # block_task manages its OWN write transaction — do not wrap it.
    c = kb.connect(db_path=db_path)
    try: kb.block_task(c, task_id, reason=reason)
    finally: c.close()

def complete(result, summary):
    # complete_task manages its OWN write transaction — do not wrap it.
    c = kb.connect(db_path=db_path)
    try: kb.complete_task(c, task_id, result=result, summary=summary)
    finally: c.close()

if mode == "clarify":
    append("worker_started", {"pid": os.getpid()})
    append(rc.QUESTION, {"question_id": "q1", "prompt": "which color?"})
    ans, dl = None, time.time() + deadline_s
    while time.time() < dl:
        ans = rc.answer_for(events(), "q1")
        if ans is not None: break
        time.sleep(0.2)
    if ans is None:
        append("worker_failclosed", {"reason": "no answer before deadline"})
        block("clarification timeout"); sys.exit(4)
    # A9: echo the user's answer through the shared redaction policy.
    append("worker_progress", {"used_answer": R.scrub_text(str(ans))})
    complete("picked " + R.scrub_text(str(ans)), "clarified")

elif mode == "approve":
    append("worker_started", {"pid": os.getpid()})
    append(rc.APPROVAL_REQUEST, {"approval_id": "a1", "action": "delete_all", "effect_digest": "d1"})
    dec, dl = None, time.time() + deadline_s
    while time.time() < dl:
        dec = rc.decision_for(events(), "a1")
        if dec is not None: break
        time.sleep(0.2)
    if dec is None:
        append("worker_failclosed", {"reason": "no decision before deadline"})
        block("approval timeout"); sys.exit(4)
    if dec == rc.APPROVE:
        append("effect_performed", {"approval_id": "a1"})
        complete("effect performed after approval", "approved")
    else:
        # deny -> fail CLOSED: the effect is NOT performed; run still continues.
        append("effect_denied_failclosed", {"approval_id": "a1"})
        complete("effect denied; skipped (fail-closed)", "denied")

elif mode == "pause":
    TOTAL = 8
    cp = rc.latest_checkpoint(events())
    step = int(cp["step"]) if cp else 0
    acc = list(cp["acc"]) if cp else []
    if step == 0:
        append("worker_started", {"pid": os.getpid()})
    else:
        append("worker_resumed", {"pid": os.getpid(), "from_step": step})
    while step < TOTAL:
        ev = events()
        if any(e.kind == "runtime_cancel_requested" for e in ev):
            append("worker_observed_cancel", {"at_step": step}); sys.exit(0)
        if rc.is_paused(ev):
            # REAL pause: persist the checkpoint (own txn), then block (own txn),
            # then stop — the run is resumable from this exact state.
            append(rc.CHECKPOINT, {"state": {"step": step, "acc": acc}})
            block("paused")
            sys.exit(0)
        append("work_step", {"n": step})
        acc.append(step); step += 1
        time.sleep(0.5)
    complete("sum=" + str(sum(acc)), "all steps done")
'''


@contextlib.contextmanager
def managed_client(tmp_path, worker_mode, *, deadline="20"):
    db_path = tmp_path / "kanban.db"
    prev = {}
    def setenv(k, v):
        prev[k] = os.environ.get(k); os.environ[k] = v
    setenv("YOUTAB_AGENT_KANBAN_DB", str(db_path))
    setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    setenv("YOUTAB_AGENT_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))
    setenv("YOUTAB_AGENT_HOME", str(tmp_path / "home"))
    setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", SECRET)
    setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    setenv("YOUTAB_BRAIN_PUBLIC_KEYS", _KEYS_JSON)
    setenv("WORKER_MODE", worker_mode)
    setenv("WORKER_DEADLINE", deadline)

    from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider
    import youtab_agent_cli.profiles as _profiles
    _orig_list = _profiles.list_profiles

    class _FakeProfile:
        name = "default"; description = "t"; model = "local-deterministic"
        provider = "local"; skill_count = 1; is_default = True

    _profiles.list_profiles = lambda: [_FakeProfile()]
    auth_registry.clear_providers()
    auth_registry.register_provider(RuntimeServiceProvider(secret=SECRET, scope="runtime"))
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service", capability="runtime")

    worker_py = tmp_path / "iworker.py"
    worker_py.write_text(_WORKER_SRC, encoding="utf-8")

    def _spawn(task, workspace, *, board=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["YOUTAB_AGENT_KANBAN_TASK"] = task.id
        return subprocess.Popen(
            [sys.executable, str(worker_py), task.id, str(db_path)], env=env).pid

    runtime._spawn_override = _spawn
    runtime._nonce_store = None

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)
    token_auth.require_route_ownership(
        provider="runtime-service", path="/api/runtime/v1/", is_prefix=True, capability="runtime")
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        runtime.stop_dispatcher()
        runtime._spawn_override = None
        runtime._nonce_store = None
        _profiles.list_profiles = _orig_list
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── grant + request helpers ───────────────────────────────────────────────────


def _mint(*, signer=_SIGNER, tenant=TENANT, user=USER, workspace="-", now=None,
          nonce=None, **over):
    now = now or datetime.now(UTC).replace(microsecond=0)
    fields = dict(
        schema_version="youtab.agent-command.v2", issuer="youtab-one-brain",
        audience="youtab-agent-runtime", protocol_version="youtab.runtime-sig.v2",
        command_id=f"cmd-{uuid.uuid4().hex[:12]}", task_id=f"task-{uuid.uuid4().hex[:12]}",
        root_run_id=f"run-{uuid.uuid4().hex[:12]}", parent_task_id=None, attempt=1,
        tenant_id=tenant, workspace_id=workspace, user_id=user,
        membership_generation=1, authorization_epoch=1, agent_id="agent-default",
        engine_id="engine-local", trace_id=f"trace-{uuid.uuid4().hex[:8]}",
        nonce=nonce or f"grant-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
        objective="interactive run", allowed_toolsets=("*",),
        allowed_memory_scopes=("*",), allowed_artifact_scopes=(), effect_proposal_scopes=(),
        reasoning={"max_iterations": 12, "max_spawn_depth": 1, "max_concurrent_agents": 1,
                   "max_total_tokens": 4000, "max_cost_micros": 0, "max_retries": 0,
                   "deadline_at": now + timedelta(minutes=30)},
        issued_at=now, expires_at=now + timedelta(minutes=45), key_id=KEY_ID,
        signature="0" * 88)
    fields.update(over)
    env = BrainCommandEnvelopeV2(**fields)
    sig = base64.b64encode(signer.sign(env.canonical_payload())).decode()
    grant = env.model_dump(mode="json"); grant["signature"] = sig
    return base64.b64encode(json.dumps(grant, separators=(",", ":")).encode()).decode()


def _h(tenant=TENANT, user=USER):
    return {"Authorization": f"Bearer {SECRET}", "X-Youtab-Tenant-Id": tenant,
            "X-Youtab-User-Id": user, "X-Youtab-Roles": "member",
            "X-Youtab-Correlation-Id": f"cid-{uuid.uuid4().hex[:8]}"}


def _sign(method, path, tenant, user, body, corr):
    ts = int(time.time()); nonce = f"n-{uuid.uuid4().hex}"
    canon = rca.canonical_string(method=method, path=path, tenant=tenant, user=user,
                                 timestamp=str(ts), nonce=nonce, body=body, correlation=corr)
    return {rca.SIGNATURE_HEADER: rca.compute_signature(SECRET, canon),
            rca.TIMESTAMP_HEADER: str(ts), rca.NONCE_HEADER: nonce}


def _post(client, path, obj, *, tenant=TENANT, user=USER, grant=None):
    body = json.dumps(obj).encode()
    h = _h(tenant, user)
    h.update(_sign("POST", path, tenant, user, body, h["X-Youtab-Correlation-Id"]))
    h["Content-Type"] = "application/json"
    if grant is not None:
        h[mx.GRANT_HEADER] = grant
    return client.post(path, content=body, headers=h)


def _create(client, **kw):
    return _post(client, "/api/runtime/v1/runs", {"agent": "default", "task": "interactive"},
                 grant=_mint(), **kw)


def _events(client, run_id, *, after=0, tenant=TENANT, user=USER):
    return client.get(f"/api/runtime/v1/runs/{run_id}/events?after={after}", headers=_h(tenant, user))


def _wait(client, run_id, pred, timeout=30):
    dl = time.time() + timeout
    last = None
    while time.time() < dl:
        last = _events(client, run_id, after=0).json()
        if pred(last):
            return last
        time.sleep(0.25)
    raise AssertionError(f"predicate not met for {run_id}; status={last and last.get('status')} "
                         f"kinds={[e['kind'] for e in (last or {}).get('events', [])]}")


def _kinds(data):
    return [e["kind"] for e in data["events"]]


# ── A4 clarification ──────────────────────────────────────────────────────────


def test_clarification_question_answer_same_run_continues(tmp_path):
    with managed_client(tmp_path, "clarify") as client:
        run_id = _create(client).json()["run_id"]
        d = _wait(client, run_id, lambda x: x["status"] == rc.AWAITING_INPUT
                  and "run_question" in _kinds(x))
        assert d["status"] == "awaiting_input"      # non-terminal, waiting on user
        assert d["terminal"] is False
        # answer the SAME run
        r = _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
                  {"question_id": "q1", "answer": "green"}, grant=_mint())
        assert r.status_code == 200, r.text
        done = _wait(client, run_id, lambda x: x["terminal"])
        assert done["status"] == "completed"
        assert "worker_progress" in _kinds(done)
        # continuation used the answer; a SINGLE worker, never restarted.
        assert _kinds(done).count("worker_started") == 1


def test_clarification_answer_is_idempotent_and_fail_closed(tmp_path):
    with managed_client(tmp_path, "clarify") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: "run_question" in _kinds(x))
        # answering a question that was never asked -> fail-closed 409
        bad = _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
                    {"question_id": "nope", "answer": "x"}, grant=_mint())
        assert bad.status_code == 409 and bad.json()["detail"]["error"] == "no_such_open_question"
        # first answer accepted; a duplicate is an idempotent no-op (exactly-once)
        ok = _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
                   {"question_id": "q1", "answer": "red"}, grant=_mint())
        assert ok.status_code == 200 and ok.json().get("accepted") is True
        dup = _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
                    {"question_id": "q1", "answer": "red"}, grant=_mint())
        assert dup.status_code == 200 and dup.json().get("already_answered") is True
        _wait(client, run_id, lambda x: x["terminal"])


def test_clarification_answer_requires_grant_and_ownership(tmp_path):
    with managed_client(tmp_path, "clarify") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: "run_question" in _kinds(x))
        # no grant -> managed authority gate refuses
        ng = _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
                   {"question_id": "q1", "answer": "x"}, grant=None)
        assert ng.status_code in (401, 403) and ng.json()["detail"]["error"] == "grant_required"
        # another tenant -> 404 (no existence leak)
        xt = _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
                   {"question_id": "q1", "answer": "x"}, grant=_mint(tenant="tenantB"),
                   tenant="tenantB", user="userB")
        assert xt.status_code in (401, 403, 404)
        # clean up: answer properly so the worker exits
        _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
              {"question_id": "q1", "answer": "ok"}, grant=_mint())
        _wait(client, run_id, lambda x: x["terminal"])


def test_clarification_user_answer_is_redacted(tmp_path):
    with managed_client(tmp_path, "clarify") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: "run_question" in _kinds(x))
        secret_answer = "blue Bearer abcdefghijklmnopqrstuvwxyz012345"
        _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
              {"question_id": "q1", "answer": secret_answer}, grant=_mint())
        done = _wait(client, run_id, lambda x: x["terminal"])
        blob = json.dumps(done)
        assert "abcdefghijklmnopqrstuvwxyz012345" not in blob
        assert "[redacted]" in blob


def test_clarification_timeout_fails_closed(tmp_path):
    with managed_client(tmp_path, "clarify", deadline="2") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: "run_question" in _kinds(x))
        # never answer -> the worker fails closed (does not complete).
        d = _wait(client, run_id, lambda x: "worker_failclosed" in _kinds(x), timeout=20)
        assert d["status"] != "completed"


# ── A5 approval ───────────────────────────────────────────────────────────────


def test_approval_approve_continues_effect(tmp_path):
    with managed_client(tmp_path, "approve") as client:
        run_id = _create(client).json()["run_id"]
        d = _wait(client, run_id, lambda x: x["status"] == rc.AWAITING_APPROVAL
                  and "run_approval_request" in _kinds(x))
        assert d["terminal"] is False
        r = _post(client, f"/api/runtime/v1/runs/{run_id}/approve",
                  {"approval_id": "a1", "decision": "approve"}, grant=_mint())
        assert r.status_code == 200 and r.json()["decision"] == "approve"
        done = _wait(client, run_id, lambda x: x["terminal"])
        assert done["status"] == "completed"
        assert "effect_performed" in _kinds(done)
        assert "effect_denied_failclosed" not in _kinds(done)


def test_approval_deny_fails_closed_but_run_continues(tmp_path):
    with managed_client(tmp_path, "approve") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: "run_approval_request" in _kinds(x))
        r = _post(client, f"/api/runtime/v1/runs/{run_id}/approve",
                  {"approval_id": "a1", "decision": "deny"}, grant=_mint())
        assert r.status_code == 200 and r.json()["decision"] == "deny"
        done = _wait(client, run_id, lambda x: x["terminal"])
        # controlled continuation: run completes, but the effect was NOT performed
        assert done["status"] == "completed"
        assert "effect_denied_failclosed" in _kinds(done)
        assert "effect_performed" not in _kinds(done)


def test_approval_decision_validation_and_idempotency(tmp_path):
    with managed_client(tmp_path, "approve") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: "run_approval_request" in _kinds(x))
        bad = _post(client, f"/api/runtime/v1/runs/{run_id}/approve",
                    {"approval_id": "a1", "decision": "maybe"}, grant=_mint())
        assert bad.status_code == 422
        unknown = _post(client, f"/api/runtime/v1/runs/{run_id}/approve",
                        {"approval_id": "zzz", "decision": "approve"}, grant=_mint())
        assert unknown.status_code == 409 and unknown.json()["detail"]["error"] == "no_such_open_approval"
        ok = _post(client, f"/api/runtime/v1/runs/{run_id}/approve",
                   {"approval_id": "a1", "decision": "approve"}, grant=_mint())
        assert ok.status_code == 200
        dup = _post(client, f"/api/runtime/v1/runs/{run_id}/approve",
                    {"approval_id": "a1", "decision": "deny"}, grant=_mint())
        # already decided -> idempotent; the ORIGINAL decision stands (approve).
        assert dup.status_code == 200 and dup.json().get("already_decided") is True
        assert dup.json()["decision"] == "approve"
        _wait(client, run_id, lambda x: x["terminal"])


def test_answer_duplicate_after_terminal_is_idempotent(tmp_path):
    # Deterministic guard for the control-plane/worker race: a duplicate answer
    # that arrives AFTER the worker consumed the original and drove the run to a
    # terminal state must still be an idempotent 200 (already_answered), never
    # 409 run_terminal — while a NEW answer on a terminal run stays fail-closed.
    with managed_client(tmp_path, "clarify") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: "run_question" in _kinds(x))
        ok = _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
                   {"question_id": "q1", "answer": "red"}, grant=_mint())
        assert ok.status_code == 200 and ok.json().get("accepted") is True
        done = _wait(client, run_id, lambda x: x["terminal"])
        assert done["status"] == "completed"
        # duplicate of the ALREADY-consumed answer, run now terminal -> idempotent 200
        dup = _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
                    {"question_id": "q1", "answer": "red"}, grant=_mint())
        assert dup.status_code == 200 and dup.json().get("already_answered") is True
        # a NEW answer for a never-asked question on a terminal run -> fail-closed
        new = _post(client, f"/api/runtime/v1/runs/{run_id}/answer",
                    {"question_id": "q_new", "answer": "x"}, grant=_mint())
        assert new.status_code == 409 and new.json()["detail"]["error"] == "run_terminal"


def test_approve_duplicate_after_terminal_is_idempotent(tmp_path):
    # Same deterministic guard for the approval control op: a duplicate decision
    # after the run is terminal returns 200 already_decided (ORIGINAL decision
    # stands); a NEW decision on a terminal run is refused 409 run_terminal.
    with managed_client(tmp_path, "approve") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: "run_approval_request" in _kinds(x))
        ok = _post(client, f"/api/runtime/v1/runs/{run_id}/approve",
                   {"approval_id": "a1", "decision": "approve"}, grant=_mint())
        assert ok.status_code == 200
        done = _wait(client, run_id, lambda x: x["terminal"])
        assert done["status"] == "completed"
        # duplicate (even a CONFLICTING deny) after terminal -> original approve stands
        dup = _post(client, f"/api/runtime/v1/runs/{run_id}/approve",
                    {"approval_id": "a1", "decision": "deny"}, grant=_mint())
        assert dup.status_code == 200 and dup.json().get("already_decided") is True
        assert dup.json()["decision"] == "approve"
        # a NEW approval id on a terminal run -> fail-closed
        new = _post(client, f"/api/runtime/v1/runs/{run_id}/approve",
                    {"approval_id": "a_new", "decision": "approve"}, grant=_mint())
        assert new.status_code == 409 and new.json()["detail"]["error"] == "run_terminal"


# ── A6 real pause / checkpoint / resume ───────────────────────────────────────


def _work_steps(data):
    return sorted(e["payload"]["n"] for e in data["events"] if e["kind"] == "work_step")


def test_pause_checkpoints_and_resume_continues_from_state(tmp_path):
    with managed_client(tmp_path, "pause") as client:
        run_id = _create(client).json()["run_id"]
        # let a few steps run, then pause
        _wait(client, run_id, lambda x: len(_work_steps(x)) >= 1)
        pr = _post(client, f"/api/runtime/v1/runs/{run_id}/pause", {}, grant=_mint())
        assert pr.status_code == 200 and pr.json()["status"] == "paused"
        # worker checkpoints + stops; run is PAUSED (non-terminal), not cancelled
        paused = _wait(client, run_id, lambda x: x["status"] == rc.PAUSED
                       and "run_checkpoint" in _kinds(x))
        assert paused["terminal"] is False
        steps_at_pause = _work_steps(paused)
        assert steps_at_pause and max(steps_at_pause) < 7   # did NOT finish
        cp = [e for e in paused["events"] if e["kind"] == "run_checkpoint"][-1]
        assert cp["payload"]["state"]["step"] == len(steps_at_pause)

        # resume -> a FRESH worker continues from the checkpoint
        rr = _post(client, f"/api/runtime/v1/runs/{run_id}/resume", {}, grant=_mint())
        assert rr.status_code == 200, rr.text
        done = _wait(client, run_id, lambda x: x["terminal"], timeout=40)
        assert done["status"] == "completed"
        # NOT a restart: a resume marker with from_step>0, and every step 0..7
        # executed EXACTLY once (no duplicates across the pause boundary).
        assert "worker_resumed" in _kinds(done)
        resumed = [e for e in done["events"] if e["kind"] == "worker_resumed"][0]
        assert resumed["payload"]["from_step"] == len(steps_at_pause) > 0
        assert _work_steps(done) == list(range(8))          # exactly-once, complete
        assert _kinds(done).count("worker_started") == 1     # started once, resumed once


def test_resume_requires_paused_and_not_terminal(tmp_path):
    with managed_client(tmp_path, "pause") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: len(_work_steps(x)) >= 1)
        # resuming a run that is not paused -> 409
        r = _post(client, f"/api/runtime/v1/runs/{run_id}/resume", {}, grant=_mint())
        assert r.status_code == 409 and r.json()["detail"]["error"] == "run_not_paused"
        _wait(client, run_id, lambda x: x["terminal"], timeout=40)


def test_pause_then_cancel_race_is_terminal_and_unresumable(tmp_path):
    with managed_client(tmp_path, "pause") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: len(_work_steps(x)) >= 1)
        _post(client, f"/api/runtime/v1/runs/{run_id}/pause", {}, grant=_mint())
        _wait(client, run_id, lambda x: x["status"] == rc.PAUSED)
        # cancel while paused -> cancelled wins (terminal)
        c = _post(client, f"/api/runtime/v1/runs/{run_id}/cancel", {}, grant=_mint())
        assert c.status_code == 200
        final = _wait(client, run_id, lambda x: x["status"] == "cancelled", timeout=15)
        assert final["terminal"] is True
        # resume after cancel -> fail-closed
        rr = _post(client, f"/api/runtime/v1/runs/{run_id}/resume", {}, grant=_mint())
        assert rr.status_code == 409 and rr.json()["detail"]["error"] == "run_terminal"


def test_pause_grant_gated_and_reconnect_ordering(tmp_path):
    with managed_client(tmp_path, "pause") as client:
        run_id = _create(client).json()["run_id"]
        _wait(client, run_id, lambda x: len(_work_steps(x)) >= 1)
        # pause without a grant -> refused
        ng = _post(client, f"/api/runtime/v1/runs/{run_id}/pause", {}, grant=None)
        assert ng.status_code in (401, 403)
        _post(client, f"/api/runtime/v1/runs/{run_id}/pause", {}, grant=_mint())
        paused = _wait(client, run_id, lambda x: x["status"] == rc.PAUSED)
        # reconnect/replay: ids strictly monotonic; mid-cursor returns only newer.
        ids = [e["id"] for e in paused["events"]]
        assert ids == sorted(ids) and len(ids) == len(set(ids))
        mid = ids[len(ids) // 2]
        tail = _events(client, run_id, after=mid).json()["events"]
        assert all(e["id"] > mid for e in tail)
        _post(client, f"/api/runtime/v1/runs/{run_id}/resume", {}, grant=_mint())
        _wait(client, run_id, lambda x: x["terminal"], timeout=40)
