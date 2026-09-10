"""WAVE-30H Phase-A — REAL managed user-lifecycle E2E over the HTTP runtime plane.

The subprocess suite (``test_managed_execution_subprocess``) proves the worker
re-admits a persisted grant and gates real tools in a second OS process. This
suite proves the *other* half the Owner's Phase A requires: the **managed
lifecycle through the actual HTTP create_run plane**, with a real Simorgh
Ed25519 grant and a real dispatched worker subprocess — not mocks.

    User headers (tenant/user/session)                     [A1]
      → HMAC-signed create_run + Ed25519 Simorgh grant      [A2]
        → ingress admits + freezes manifest + PERSISTS both [A2]
          → dispatcher → real worker PROCESS re-admits       [A12]
            → ordered, resumable progress/result events      [A3/A7/A8]
              → redacted user-visible output                 [A9]
    cross-tenant isolation + grant identity binding          [A10]
    cancel is grant-gated and terminal                       [A6]
    exactly-once completion / no duplicate execution          [A8/A11]

Negative controls at the HTTP ingress: missing / forged / expired / replayed /
cross-tenant grant → the correct fail-closed HTTP status.

Hermetic and CI-runnable: the model "brain" is a deterministic local worker
(the same handoff shape as the real dispatcher), so no provider credentials and
no live tunnel are needed; the runtime, admission, persistence, dispatch, event
and isolation paths are all REAL.

NOT covered here and reported as an honest gap (see ``test_phaseA_gap_notes``):
interactive clarification (A4), approval-continuation (A5) and pause/resume
(part of A6) are not first-class operations on the managed create_run plane.
"""
from __future__ import annotations

import base64
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
from youtab_runtime.contracts import BrainCommandEnvelopeV2

REPO_ROOT = str(Path(__file__).resolve().parents[2])
SECRET = secrets.token_urlsafe(48)

KEY_ID = "brain-ed25519-http-e2e"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([41]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_KEYS_JSON = json.dumps({KEY_ID: _PUB})

TENANT = "tenantA"
USER = "userA"

# A managed deterministic worker: re-admits the PERSISTED Simorgh grant in its
# own process (proving the cross-process admission hop on the real dispatch
# path), emits ordered events, routes a secret through the SHARED redaction
# policy the run-observer uses, writes an artifact, and completes. On a failed
# admission it fails closed (error event + non-zero exit) and does NOT run.
_WORKER_SRC = r'''
import json, os, sys, time
from pathlib import Path
from youtab_agent_cli import kanban_db as kb
from youtab_runtime import redaction as R

task_id = sys.argv[1]
db_path = Path(sys.argv[2])

class _Agent:
    _admitted_command = None

agent = _Agent()
from youtab_agent_cli.worker_admission import (
    establish_managed_admission, ManagedWorkerAdmissionError,
)
try:
    established = establish_managed_admission(agent)
except ManagedWorkerAdmissionError as exc:
    conn = kb.connect(db_path=db_path)
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "worker_admission_error", {"error": str(exc)[:200]})
    conn.close()
    sys.exit(3)

time.sleep(0.1)
conn = kb.connect(db_path=db_path)
with kb.write_txn(conn):
    kb._append_event(conn, task_id, "worker_started", {"pid": os.getpid()})
    # Proof the worker reconstructed the sealed admitted context in THIS process.
    kb._append_event(conn, task_id, "worker_admitted", {
        "has_admitted": agent._admitted_command is not None,
        "established": bool(established),
        "tree_root": getattr(agent, "_execution_tree_root", None),
        "pid": os.getpid(),
    })
    # A9: a secret-shaped value routed through the shared redaction policy never
    # reaches the user-visible event stream.
    redacted = R.redact_mapping({
        "api_key": "sk-SUPERSECRETVALUE1234567890abcdef",
        "note": "call used Bearer abcdefghijklmnopqrstuvwxyz012345",
        "keep": "ok",
    })
    kb._append_event(conn, task_id, "worker_progress", {"redacted": redacted})
kb.store_attachment_bytes(
    conn, task_id, "result.txt", b"managed worker output: 2 + 2 = 4",
    content_type="text/plain",
)
kb.complete_task(conn, task_id, result="2 + 2 = 4",
                 summary="completed by managed deterministic worker")
conn.close()
'''


@pytest.fixture()
def managed(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_DB", str(db_path))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", SECRET)
    # Managed trust mode + Brain keyring (the whole point of this suite).
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    monkeypatch.setenv("YOUTAB_BRAIN_PUBLIC_KEYS", _KEYS_JSON)

    from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider
    auth_registry.clear_providers()
    auth_registry.register_provider(RuntimeServiceProvider(secret=SECRET, scope="runtime"))
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service", capability="runtime")

    class _FakeProfile:
        name = "default"
        description = "test agent"
        model = "local-deterministic"
        provider = "local"
        skill_count = 1
        is_default = True

    monkeypatch.setattr(
        "youtab_agent_cli.profiles.list_profiles", lambda: [_FakeProfile()]
    )

    worker_py = tmp_path / "managed_worker.py"
    worker_py.write_text(_WORKER_SRC, encoding="utf-8")

    def _spawn(task, workspace, *, board=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        # The worker re-admits from the PERSISTED grant keyed by this task id.
        env["YOUTAB_AGENT_KANBAN_TASK"] = task.id
        proc = subprocess.Popen(
            [sys.executable, str(worker_py), task.id, str(db_path)], env=env,
        )
        return proc.pid

    runtime._spawn_override = _spawn
    runtime._nonce_store = None

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)
    token_auth.require_route_ownership(
        provider="runtime-service", path="/api/runtime/v1/", is_prefix=True,
        capability="runtime")
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()

    with TestClient(app) as c:
        yield c

    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None


# ── grant minting + request helpers ──────────────────────────────────────────


def _mint_grant(*, signer=_SIGNER, tenant=TENANT, user=USER, workspace="-",
                toolsets=("*",), now=None, nonce=None, **over):
    now = now or datetime.now(UTC).replace(microsecond=0)
    fields = dict(
        schema_version="youtab.agent-command.v2",
        issuer="youtab-one-brain",
        audience="youtab-agent-runtime",
        protocol_version="youtab.runtime-sig.v2",
        command_id=f"cmd-{uuid.uuid4().hex[:12]}",
        task_id=f"task-{uuid.uuid4().hex[:12]}",
        root_run_id=f"run-{uuid.uuid4().hex[:12]}",
        parent_task_id=None,
        attempt=1,
        tenant_id=tenant,
        workspace_id=workspace,
        user_id=user,
        membership_generation=1,
        authorization_epoch=1,
        agent_id="agent-default",
        engine_id="engine-local",
        trace_id=f"trace-{uuid.uuid4().hex[:8]}",
        nonce=nonce or f"grant-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
        objective="add two and two",
        allowed_toolsets=toolsets,
        allowed_memory_scopes=("*",),
        allowed_artifact_scopes=(),
        effect_proposal_scopes=(),
        reasoning={
            "max_iterations": 4, "max_spawn_depth": 1, "max_concurrent_agents": 1,
            "max_total_tokens": 2000, "max_cost_micros": 0, "max_retries": 0,
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
    header = base64.b64encode(json.dumps(grant, separators=(",", ":")).encode()).decode()
    return env, header


def _headers(tenant=TENANT, user=USER):
    return {
        "Authorization": f"Bearer {SECRET}",
        "X-Youtab-Tenant-Id": tenant,
        "X-Youtab-User-Id": user,
        "X-Youtab-Roles": "member",
        "X-Youtab-Correlation-Id": f"cid-{uuid.uuid4().hex[:8]}",
    }


def _sign(method, path, tenant, user, body, correlation):
    ts = int(time.time())
    nonce = f"n-{uuid.uuid4().hex}"
    canon = rca.canonical_string(
        method=method, path=path, tenant=tenant, user=user,
        timestamp=str(ts), nonce=nonce, body=body, correlation=correlation,
    )
    return {rca.SIGNATURE_HEADER: rca.compute_signature(SECRET, canon),
            rca.TIMESTAMP_HEADER: str(ts), rca.NONCE_HEADER: nonce}


def _create(client, *, tenant=TENANT, user=USER, grant_header=None, task="add 2 and 2"):
    body = json.dumps({"agent": "default", "task": task}).encode()
    path = "/api/runtime/v1/runs"
    h = _headers(tenant, user)
    h.update(_sign("POST", path, tenant, user, body, h["X-Youtab-Correlation-Id"]))
    h["Content-Type"] = "application/json"
    if grant_header is not None:
        h[mx.GRANT_HEADER] = grant_header
    return client.post(path, content=body, headers=h)


def _cancel(client, run_id, *, tenant=TENANT, user=USER, grant_header=None):
    body = b"{}"
    path = f"/api/runtime/v1/runs/{run_id}/cancel"
    h = _headers(tenant, user)
    h.update(_sign("POST", path, tenant, user, body, h["X-Youtab-Correlation-Id"]))
    h["Content-Type"] = "application/json"
    if grant_header is not None:
        h[mx.GRANT_HEADER] = grant_header
    return client.post(path, content=body, headers=h)


def _retry(client, run_id, *, tenant=TENANT, user=USER, grant_header=None):
    body = b"{}"
    path = f"/api/runtime/v1/runs/{run_id}/retry"
    h = _headers(tenant, user)
    h.update(_sign("POST", path, tenant, user, body, h["X-Youtab-Correlation-Id"]))
    h["Content-Type"] = "application/json"
    if grant_header is not None:
        h[mx.GRANT_HEADER] = grant_header
    return client.post(path, content=body, headers=h)


def _events(client, run_id, *, after=0, tenant=TENANT, user=USER):
    return client.get(
        f"/api/runtime/v1/runs/{run_id}/events?after={after}",
        headers=_headers(tenant, user),
    )


def _wait_terminal(client, run_id, timeout=30):
    deadline = time.time() + timeout
    cursor, kinds, payloads = 0, [], {}
    while time.time() < deadline:
        r = _events(client, run_id, after=cursor)
        assert r.status_code == 200, r.text
        data = r.json()
        for e in data["events"]:
            kinds.append(e["kind"])
            payloads.setdefault(e["kind"], []).append(e["payload"])
        cursor = data["cursor"]
        if data["terminal"]:
            return data, kinds, payloads
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} not terminal; kinds={kinds}")


# ── A1/A2/A12 positive: admit → persist → worker re-admits → complete ─────────


def test_managed_create_admits_persists_and_worker_completes(managed):
    env, header = _mint_grant()
    r = _create(managed, grant_header=header)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # A1: identity/tenant/session binding — the persisted task is owned by the
    # calling tenant/user (enforced on every read via _load_owned_task).
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        task = kb.get_task(conn, run_id)
        assert task.tenant == TENANT and task.created_by == USER

    # A2: the grant + frozen manifest are persisted with NO identity/scope drift.
    ev0 = _events(managed, run_id, after=0).json()["events"]
    grant_events = [e for e in ev0 if e["kind"] == "runtime_execution_grant"]
    manifest_events = [e for e in ev0 if e["kind"] == "runtime_capability_manifest"]
    assert len(grant_events) == 1, ev0
    assert len(manifest_events) == 1, ev0
    persisted = mx.decode_grant_header(grant_events[0]["payload"]["grant"])
    assert persisted.tenant_id == env.tenant_id == TENANT
    assert persisted.user_id == env.user_id == USER
    assert persisted.workspace_id == env.workspace_id
    assert persisted.root_run_id == env.root_run_id
    assert tuple(persisted.allowed_toolsets) == tuple(env.allowed_toolsets)
    assert persisted.reasoning.max_iterations == env.reasoning.max_iterations
    # the frozen manifest carries concrete tool hashes (correction 2: "*" frozen)
    assert manifest_events[0]["payload"].get("tool_hashes") is not None

    # A12/A3: the real dispatched worker re-admits in its own process and the
    # lifecycle reaches completion with ordered progress/result events.
    data, kinds, payloads = _wait_terminal(managed, run_id)
    assert data["status"] == "completed"
    assert "worker_admission_error" not in kinds, payloads.get("worker_admission_error")
    assert "worker_started" in kinds
    assert payloads["worker_admitted"][0]["has_admitted"] is True
    assert payloads["worker_admitted"][0]["established"] is True


# ── A2/A12 managed RETRY: re-minted grant is persisted onto the child ─────────


def test_managed_retry_persists_fresh_grant_and_child_worker_completes(managed):
    """WAVE-30H regression: a managed retry MUST persist its freshly-admitted
    grant + frozen manifest onto the CHILD run, so the dispatched retry worker can
    re-admit from its OWN events (worker_admission.establish_managed_admission).

    Before the fix, runtime_retry_run admitted a grant (proving authority) but
    discarded it — the child had no runtime_execution_grant event, so in managed
    trust mode its worker raised ManagedWorkerAdmissionError and fail-closed
    never executed. This exercises the real ingress → dispatch → child-worker path
    with real Ed25519 grants (no mocks) and proves the child can now complete.
    """
    # An original managed run completes.
    orig_env, orig_header = _mint_grant()
    run_id = _create(managed, grant_header=orig_header).json()["run_id"]
    _wait_terminal(managed, run_id)

    # Retry WITH a fresh valid grant — re-minted for THIS request (never copied
    # from the original), matching the "grant re-minted fresh per run" invariant.
    retry_env, retry_header = _mint_grant()
    r = _retry(managed, run_id, grant_header=retry_header)
    assert r.status_code == 200, r.text
    child_id = r.json()["run_id"]
    assert child_id != run_id

    # The freshly-admitted grant + frozen capability manifest are PERSISTED onto
    # the CHILD (the defect: they were dropped). Exactly one each, no identity/
    # scope drift, and it is THIS retry's grant — not the original's.
    cev = _events(managed, child_id, after=0).json()["events"]
    child_grants = [e for e in cev if e["kind"] == "runtime_execution_grant"]
    child_manifests = [e for e in cev if e["kind"] == "runtime_capability_manifest"]
    assert len(child_grants) == 1, cev
    assert len(child_manifests) == 1, cev
    persisted = mx.decode_grant_header(child_grants[0]["payload"]["grant"])
    assert persisted.tenant_id == retry_env.tenant_id == TENANT
    assert persisted.user_id == retry_env.user_id == USER
    assert persisted.workspace_id == retry_env.workspace_id
    assert persisted.command_id == retry_env.command_id      # this retry's grant…
    assert persisted.command_id != orig_env.command_id       # …re-minted, not copied
    assert child_manifests[0]["payload"].get("tool_hashes") is not None

    # The real dispatched CHILD worker re-admits from its OWN persisted grant and
    # reaches completion — proving a managed retry can actually execute.
    data, kinds, payloads = _wait_terminal(managed, child_id)
    assert data["status"] == "completed"
    assert "worker_admission_error" not in kinds, payloads.get("worker_admission_error")
    assert payloads["worker_admitted"][0]["has_admitted"] is True
    assert payloads["worker_admitted"][0]["established"] is True


# ── A3/A7/A8 ordering, resumability, exactly-once ─────────────────────────────


def test_managed_events_ordered_resumable_and_exactly_once(managed):
    _, header = _mint_grant()
    run_id = _create(managed, grant_header=header).json()["run_id"]
    data, kinds, payloads = _wait_terminal(managed, run_id)

    # A8 ordering: monotonic strictly-increasing event ids.
    all_ev = _events(managed, run_id, after=0).json()["events"]
    ids = [e["id"] for e in all_ev]
    assert ids == sorted(ids) and len(ids) == len(set(ids))

    # A7 resumability/replay: reading from 0 twice is identical; reading from a
    # mid cursor returns only strictly-newer events (no gaps, no dupes).
    first = _events(managed, run_id, after=0).json()["events"]
    again = _events(managed, run_id, after=0).json()["events"]
    assert [e["id"] for e in first] == [e["id"] for e in again]
    mid = ids[len(ids) // 2]
    tail = _events(managed, run_id, after=mid).json()["events"]
    assert all(e["id"] > mid for e in tail)
    assert [e["id"] for e in tail] == [i for i in ids if i > mid]

    # A8/A11 exactly-once: the worker ran exactly once — a single worker_started /
    # worker_admitted / completion, never duplicated by reconnecting readers.
    assert kinds.count("worker_started") == 1
    assert kinds.count("worker_admitted") == 1


# ── A9 redaction of user-visible output ───────────────────────────────────────


def test_managed_user_visible_output_is_redacted(managed):
    _, header = _mint_grant()
    run_id = _create(managed, grant_header=header).json()["run_id"]
    _wait_terminal(managed, run_id)
    blob = json.dumps(_events(managed, run_id, after=0).json())
    # The secret-shaped values the worker routed through the shared policy must
    # NOT appear in the user-visible event stream; the redaction marker does.
    assert "sk-SUPERSECRETVALUE1234567890abcdef" not in blob
    assert "abcdefghijklmnopqrstuvwxyz012345" not in blob
    assert "[redacted]" in blob


# ── A6 cancel is grant-gated and terminal ─────────────────────────────────────


def test_managed_cancel_requires_grant_and_is_terminal(managed):
    _, header = _mint_grant()
    run_id = _create(managed, grant_header=header).json()["run_id"]

    # cancel without a grant is refused by the managed execution-authority gate.
    no_grant = _cancel(managed, run_id, grant_header=None)
    assert no_grant.status_code in (401, 403), no_grant.text

    # cancel WITH a fresh valid grant is accepted and drives a terminal state.
    _, cancel_grant = _mint_grant()
    ok = _cancel(managed, run_id, grant_header=cancel_grant)
    assert ok.status_code == 200, ok.text
    # terminal product state includes a cancel signal.
    deadline = time.time() + 15
    while time.time() < deadline:
        d = _events(managed, run_id, after=0).json()
        if d["terminal"] or any(True for e in d["events"] if "cancel" in e["kind"]):
            break
        time.sleep(0.2)
    final = _events(managed, run_id, after=0).json()
    assert final["status"] in ("cancelled", "completed")


# ── A10 cross-tenant isolation (identity binding + read isolation) ────────────


def test_managed_cross_tenant_grant_rejected(managed):
    # Grant says tenant-beta but the caller identity is tenantA → identity
    # mismatch, fail-closed at ingress (no run created).
    _, header = _mint_grant(tenant="tenant-beta")
    r = _create(managed, grant_header=header)
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_identity_mismatch"


def test_managed_cross_tenant_run_read_isolation(managed):
    _, header = _mint_grant()
    run_id = _create(managed, grant_header=header).json()["run_id"]
    # another tenant must not even learn the run exists.
    other = _events(managed, run_id, after=0, tenant="tenantB", user="userB")
    assert other.status_code == 404
    detail = other.json()["detail"]["error"]
    assert detail == "run_not_found"


# ── A2 negative controls at the HTTP ingress ──────────────────────────────────


def test_managed_missing_grant_rejected(managed):
    r = _create(managed, grant_header=None)
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_required"


def test_managed_forged_grant_rejected(managed):
    forged = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    _, header = _mint_grant(signer=forged)
    r = _create(managed, grant_header=header)
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_rejected"


def test_managed_expired_grant_rejected(managed):
    past = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    _, header = _mint_grant(now=past)
    r = _create(managed, grant_header=header)
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_rejected"


def test_managed_replayed_grant_rejected(managed):
    # Same grant nonce on two ingress admissions → the second is a replay. Each
    # request is transport-signed with its OWN fresh HMAC nonce, so the rejection
    # is the GRANT nonce burn, not transport replay.
    _, header = _mint_grant(nonce="grant-replay-fixed-0123456789abcdef0123456789")
    first = _create(managed, grant_header=header)
    assert first.status_code == 200, first.text
    second = _create(managed, grant_header=header)
    assert second.status_code in (401, 403), second.text
    assert second.json()["detail"]["error"] == "grant_rejected"


# ── honest Phase-A scope note (A4/A5/pause-resume) ────────────────────────────


def test_phaseA_interactive_contract_present(managed):
    """The managed interactive-lifecycle contract (ADR-0004, A4/A5/A6) is now
    first-class on the create_run plane: /answer, /approve, /pause, /resume are
    registered POST routes. (Behavioural proofs live in
    test_managed_interactive_lifecycle_e2e.py.)"""
    paths = {getattr(r, "path", "") for r in runtime.router.routes}
    for op in ("answer", "approve", "pause", "resume"):
        assert f"/api/runtime/v1/runs/{{run_id}}/{op}" in paths, (op, sorted(paths))
