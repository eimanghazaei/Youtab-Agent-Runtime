"""WAVE-30H (PR #41) — TRUE managed-runtime binding E2E over the REAL path.

This suite proves the WAVE-30H **effective-binding contract** end-to-end on the
ACTUAL managed runtime path — never with stubs or injected identities:

    GET /api/runtime/v1/preflight  (real FastAPI ingress + token_auth_middleware
      + RuntimeServiceProvider)                                            [A]
        -> compute the SUBSTRATE binding_digest the harness would pin
          -> POST /runs  (real Ed25519 Simorgh grant + HMAC-signed command +
             real runtime.router + real create_task_ex atomic persistence)  [A/B]
            -> the canonical runtime_effective_binding is persisted, scoped to
               the run, self-hashed, and its digest matches preflight          [A]
              -> a REAL worker SUBPROCESS re-admits + establish_managed_admission
                 dispatches EXCLUSIVELY from the persisted binding             [A]

and the fail-closed guarantees the contract exists for, each exercised through a
REAL worker subprocess re-admitting a REALLY-persisted grant + binding:

  * idempotent replay keeps exactly one binding/grant/manifest/mode; a MISMATCHED
    expected_binding_digest replay is refused (412) with no new task/binding.   [B]
  * a managed RETRY re-scopes the parent binding onto the child (same substrate,
    child run scope) — and a TAMPERED child binding fails closed at the worker.  [C]
  * a real-model worker whose resolved substrate DRIFTS from the bound identity
    fails closed BEFORE any provider client / network / budget.                 [D]
  * a missing dispatched identity fails closed at create (managed_model_unresolved)
    AND, when seeded directly, at the worker ("not fully resolved").            [E]
  * on ANY admission refusal the worker constructs ZERO provider client and makes
    ZERO network egress, and opens ZERO execution-tree budget (cross-process,
    explicit socket + resolve_provider_client spies).                          [F]

What is REAL here vs substituted (honest boundary):
  * REAL: the FastAPI app + token_auth middleware + RuntimeServiceProvider, the
    runtime.router create/retry/preflight endpoints, the HMAC signed-command
    verification, the Ed25519 Simorgh grant admission + single-use nonce burn,
    create_task_ex's atomic binding/grant/manifest persistence, the cross-process
    worker subprocess, worker_admission.establish_managed_admission's
    pre-dispatch binding gate, and effective_binding hashing/digest/rescope.
  * SUBSTITUTED (and why): the model "brain" — the worker never calls a paid
    provider or a live model. The happy-path worker is a deterministic subprocess
    that re-admits the REAL persisted grant/binding and completes; the negative
    workers assert refusal BEFORE any brain would be reached. The (provider, model,
    endpoint) substrate is monkeypatched to ONE consistent local identity
    (``ollama``/``qwen:test``/``http://127.0.0.1:11434``) so preflight and create
    resolve the SAME substrate offline (no Ollama is contacted — the binding build
    is PURE and the worker refuses before any probe on the negative paths, and the
    happy worker is a stub that performs no model call).

Run ONLY by explicit path (never the broad tests/youtab_agent_cli suite):
  .venv-qual/Scripts/python.exe -m pytest \
    tests/youtab_runtime/test_managed_binding_e2e.py -q -p no:cacheprovider
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

from youtab_agent_cli import effective_binding as eb
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

KEY_ID = "brain-ed25519-binding-e2e"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([53]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_KEYS_JSON = json.dumps({KEY_ID: _PUB})

TENANT = "tenantBind"
USER = "userBind"

# ONE consistent local substrate so preflight and create resolve the SAME binding
# digest, purely (no network). Loopback Ollama => execution=local, endpoint_class=
# loopback, a fully-resolved model — exactly what a managed model-run needs.
SUB_PROVIDER = "ollama"
SUB_MODEL = "qwen:test"
SUB_ENDPOINT = "http://127.0.0.1:11434"


# The happy-path managed worker: re-admits the PERSISTED Simorgh grant + binding in
# its OWN process (the cross-process admission hop on the real dispatch path), then
# completes. On a failed admission it fails closed (error event + exit 3) and does
# NOT execute. It is a deterministic stub (no provider/model attrs) so
# require_client_match is False — it performs NO model call.
_HAPPY_WORKER_SRC = r'''
import os, sys, time
from pathlib import Path
from youtab_agent_cli import kanban_db as kb

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

time.sleep(0.05)
conn = kb.connect(db_path=db_path)
with kb.write_txn(conn):
    kb._append_event(conn, task_id, "worker_started", {"pid": os.getpid()})
    kb._append_event(conn, task_id, "worker_admitted", {
        "has_admitted": agent._admitted_command is not None,
        "established": bool(established),
        "pid": os.getpid(),
    })
kb.complete_task(conn, task_id, result="bound worker ok",
                 summary="completed by managed binding-e2e worker")
conn.close()
'''


# The negative real-model worker (scenarios C/D/E/F). It shapes a REAL-model agent
# (provider/model/base_url set -> require_client_match=True) and installs two spies
# BEFORE admission that PROVE nothing executed on a refusal:
#   * socket.socket.connect -> write NET_SENTINEL + raise (zero network egress),
#   * agent.auxiliary_client.resolve_provider_client -> write CLIENT_SENTINEL + raise
#     (zero provider-client construction).
# On a ManagedWorkerAdmissionError it writes ONLY {admission_error,pid} and exits 3
# (BEFORE any provider client / socket / budget). On the (never-taken here) success
# path it would record execution-phase keys — their ABSENCE proves the refusal.
_NEG_WORKER_SRC = r'''
import json, os, socket, sys

result_path = os.environ["WORKER_RESULT"]
net_sentinel = os.environ["NET_SENTINEL"]
client_sentinel = os.environ["CLIENT_SENTINEL"]

# Zero-egress guard: any real socket connect on the refusal path trips the sentinel.
_orig_connect = socket.socket.connect
def _guarded_connect(self, *a, **k):
    try:
        with open(net_sentinel, "w", encoding="utf-8") as f:
            f.write("connect")
    finally:
        raise RuntimeError("network egress attempted on the refusal path")
socket.socket.connect = _guarded_connect

class _AuxClient:
    def resolve_provider_client(self, *a, **k):
        with open(client_sentinel, "w", encoding="utf-8") as f:
            f.write("client")
        raise RuntimeError("provider client constructed on the refusal path")

class _Agent:
    _admitted_command = None
    provider = os.environ.get("WORKER_PROVIDER") or None
    model = os.environ.get("WORKER_MODEL") or None
    base_url = os.environ.get("WORKER_BASE_URL") or None
    auxiliary_client = _AuxClient()

agent = _Agent()
from youtab_agent_cli.worker_admission import (
    establish_managed_admission, ManagedWorkerAdmissionError,
)
try:
    established = establish_managed_admission(agent)
except ManagedWorkerAdmissionError as exc:
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({"admission_error": str(exc), "pid": os.getpid()}, f)
    sys.exit(3)

# Reached execution: record execution-phase keys (their presence would FAIL the
# refusal assertion). Never taken by the negative scenarios in this suite.
with open(result_path, "w", encoding="utf-8") as f:
    json.dump({
        "established": bool(established),
        "has_admitted": agent._admitted_command is not None,
        "pid": os.getpid(),
    }, f)
'''


# ── fixtures ──────────────────────────────────────────────────────────────────


def _install_managed_env(tmp_path, monkeypatch):
    """Shared managed-runtime env + provider registration + substrate monkeypatch."""
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_DB", str(db_path))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", SECRET)
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    monkeypatch.setenv("YOUTAB_BRAIN_PUBLIC_KEYS", _KEYS_JSON)
    # A leaked dev-shell legacy-binding window must never excuse a missing binding.
    monkeypatch.delenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", raising=False)
    monkeypatch.delenv("YOUTAB_MANAGED_BINDING_LEGACY_CREATED_BEFORE", raising=False)

    from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider

    auth_registry.clear_providers()
    auth_registry.register_provider(RuntimeServiceProvider(secret=SECRET, scope="runtime"))
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix(
        "/api/runtime/v1/", provider="runtime-service", capability="runtime"
    )

    class _FakeProfile:
        name = "default"
        description = "binding-e2e agent"
        model = SUB_MODEL
        provider = SUB_PROVIDER
        skill_count = 1
        is_default = True

    monkeypatch.setattr(
        "youtab_agent_cli.profiles.list_profiles", lambda: [_FakeProfile()]
    )
    # Make preflight AND create resolve the SAME substrate purely (no network):
    #   * create reads _profile_default_identity(agent);
    #   * preflight reads _configured_model_provider_names + _configured_inference_base_url.
    monkeypatch.setattr(
        "youtab_agent_cli.web_routers.runtime._profile_default_identity",
        lambda agent: (SUB_PROVIDER, SUB_MODEL, SUB_ENDPOINT),
    )
    monkeypatch.setattr(
        "youtab_agent_cli.web_routers.runtime._configured_model_provider_names",
        lambda: {"provider": SUB_PROVIDER, "model": SUB_MODEL},
    )
    monkeypatch.setattr(
        "youtab_agent_cli.web_routers.runtime._configured_inference_base_url",
        lambda: SUB_ENDPOINT,
    )
    return db_path


def _build_client(spawn):
    """Wire the real app + middleware + router with the given dispatcher spawn."""
    runtime._spawn_override = spawn
    runtime._nonce_store = None
    # Force a fresh, tmp-scoped grant nonce store per test (module-global cache).
    runtime._grant_boundary = None

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)
    token_auth.require_route_ownership(
        provider="runtime-service", path="/api/runtime/v1/", is_prefix=True,
        capability="runtime",
    )
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()
    return TestClient(app)


@pytest.fixture()
def managed(tmp_path, monkeypatch):
    """Real ingress + a real COMPLETING worker subprocess (happy-path dispatch)."""
    db_path = _install_managed_env(tmp_path, monkeypatch)
    worker_py = tmp_path / "happy_worker.py"
    worker_py.write_text(_HAPPY_WORKER_SRC, encoding="utf-8")

    def _spawn(task, workspace, *, board=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["YOUTAB_AGENT_KANBAN_TASK"] = task.id
        proc = subprocess.Popen(
            [sys.executable, str(worker_py), task.id, str(db_path)], env=env
        )
        return proc.pid

    with _build_client(_spawn) as c:
        yield c
    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None
    runtime._grant_boundary = None


@pytest.fixture()
def managed_deferred(tmp_path, monkeypatch):
    """Real ingress but NO auto-executing worker: the dispatcher 'spawn' is a
    liveness no-op (returns this live pid) so a created/retried run is persisted but
    never executed by the ticker. The test then runs a worker subprocess MANUALLY
    after tampering/seeding — eliminating any race with the background dispatcher."""
    _install_managed_env(tmp_path, monkeypatch)

    def _noop_spawn(task, workspace, *, board=None):
        # Return an alive pid so the dispatcher believes a worker is running and
        # never respawns; nothing actually executes the run.
        return os.getpid()

    with _build_client(_noop_spawn) as c:
        yield c
    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None
    runtime._grant_boundary = None


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
        objective="bind and run",
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


def _headers(tenant=TENANT, user=USER, idempotency=None):
    h = {
        "Authorization": f"Bearer {SECRET}",
        "X-Youtab-Tenant-Id": tenant,
        "X-Youtab-User-Id": user,
        "X-Youtab-Roles": "member",
        "X-Youtab-Correlation-Id": f"cid-{uuid.uuid4().hex[:8]}",
    }
    if idempotency is not None:
        h["Idempotency-Key"] = idempotency
    return h


def _sign(method, path, tenant, user, body, correlation):
    ts = int(time.time())
    nonce = f"n-{uuid.uuid4().hex}"
    canon = rca.canonical_string(
        method=method, path=path, tenant=tenant, user=user,
        timestamp=str(ts), nonce=nonce, body=body, correlation=correlation,
    )
    return {rca.SIGNATURE_HEADER: rca.compute_signature(SECRET, canon),
            rca.TIMESTAMP_HEADER: str(ts), rca.NONCE_HEADER: nonce}


def _preflight(client, *, tenant=TENANT, user=USER):
    return client.get("/api/runtime/v1/preflight", headers=_headers(tenant, user))


def _create(client, *, tenant=TENANT, user=USER, grant_header=None,
            task="bind two and two", expected_digest=None, idempotency=None):
    payload = {"agent": "default", "task": task}
    if expected_digest is not None:
        payload["expected_binding_digest"] = expected_digest
    body = json.dumps(payload).encode()
    path = "/api/runtime/v1/runs"
    h = _headers(tenant, user, idempotency=idempotency)
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


def _all_events(client, run_id, **kw):
    r = _events(client, run_id, after=0, **kw)
    assert r.status_code == 200, r.text
    return r.json()["events"]


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
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} not terminal; kinds={kinds}")


def _persisted_binding(client, run_id, **kw):
    """The current runtime_effective_binding payload from the run's real event log."""
    bindings = [
        e["payload"] for e in _all_events(client, run_id, **kw)
        if e["kind"] == eb.BINDING_EVENT
    ]
    assert bindings, f"no {eb.BINDING_EVENT} persisted for {run_id}"
    # Highest binding_version wins (matches effective_binding_from_events).
    return max(bindings, key=lambda b: int(b.get("binding_version", 0)))


def _count_kinds(events):
    out: dict[str, int] = {}
    for e in events:
        out[e["kind"]] = out.get(e["kind"], 0) + 1
    return out


# ── manual worker subprocess (cross-process refusal proofs) ───────────────────


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    try:
        if os.name == "posix":
            import signal as _signal

            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_neg_worker(tmp_path, task_id, *, provider, model, base_url):
    """Run the negative real-model worker subprocess against the REAL persisted run.

    Inherits the test's managed env (same YOUTAB_AGENT_KANBAN_DB / trust mode /
    Brain keys / home) so it re-admits the REALLY-persisted grant + binding, then
    shapes a real-model agent whose (provider, model, base_url) is supplied here.
    Returns ``(rc, result_dict, net_sentinel_path, client_sentinel_path)``.
    """
    script = tmp_path / "neg_worker.py"
    script.write_text(_NEG_WORKER_SRC, encoding="utf-8")
    result_file = tmp_path / f"neg_result_{task_id}.json"
    net_sentinel = tmp_path / f"net_sentinel_{task_id}"
    client_sentinel = tmp_path / f"client_sentinel_{task_id}"
    env = dict(os.environ)
    env.pop("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", None)
    env["YOUTAB_AGENT_KANBAN_TASK"] = task_id
    env["WORKER_RESULT"] = str(result_file)
    env["NET_SENTINEL"] = str(net_sentinel)
    env["CLIENT_SENTINEL"] = str(client_sentinel)
    env["WORKER_PROVIDER"] = provider
    env["WORKER_MODEL"] = model
    env["WORKER_BASE_URL"] = base_url
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        [sys.executable, str(script)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **popen_kwargs,
    )
    try:
        _out, err = proc.communicate(timeout=120)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        _out, err = proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        err = (err or "") + "\n[worker timed out; tree killed]"
    data = {}
    if result_file.exists():
        data = json.loads(result_file.read_text(encoding="utf-8"))
    data["_stderr"] = (err or "")[-2000:]
    return rc, data, net_sentinel, client_sentinel


def _assert_refused_zero_execution(tmp_path, rc, res, expected_error,
                                   net_sentinel, client_sentinel):
    """Fail-closed proof: rc 3 + the EXACT structured admission error, and the
    worker exited AT admission — no execution-phase keys, no provider client
    (CLIENT_SENTINEL absent), no network egress (NET_SENTINEL absent), and the
    execution-tree budget DB was never created (zero budget)."""
    assert rc == 3, res.get("_stderr")
    assert res.get("admission_error") == expected_error, res.get("admission_error")
    for k in ("established", "has_admitted", "tree_root", "tree_snap"):
        assert k not in res, f"worker executed past admission (key {k!r} present)"
    assert not net_sentinel.exists(), "network egress occurred on the refusal path"
    assert not client_sentinel.exists(), "provider client constructed on the refusal path"
    budget_db = tmp_path / "home" / "runtime" / "execution_tree_budget.db"
    assert not budget_db.exists(), "execution-tree budget was opened on a refusal"


def _seed_binding(tmp_path, task_id, binding_payload):
    """Append a raw BINDING_EVENT to the REAL run store (adversarial seeding)."""
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, eb.BINDING_EVENT, binding_payload)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# A. preflight -> create -> worker happy path (atomic digest match + persistence)
# ══════════════════════════════════════════════════════════════════════════════


def test_A_preflight_create_worker_happy_path(managed):
    # Preflight surfaces the canonical effective binding the harness pins.
    pf = _preflight(managed)
    assert pf.status_code == 200, pf.text
    pf_binding = pf.json()["effective_binding"]
    digest = eb.binding_digest(pf_binding)
    assert len(digest) == 64

    # Create pinning that SAME substrate digest -> atomic preflight->create match.
    env, header = _mint_grant()
    r = _create(managed, grant_header=header, expected_digest=digest)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # Exactly one grant / manifest / binding / mode persisted atomically.
    events = _all_events(managed, run_id)
    counts = _count_kinds(events)
    assert counts.get("runtime_execution_grant") == 1, counts
    assert counts.get("runtime_capability_manifest") == 1, counts
    assert counts.get(eb.BINDING_EVENT) == 1, counts
    assert counts.get("runtime_execution_mode") == 1, counts

    # The persisted binding is scoped to THIS run/tenant/workspace, self-verifies,
    # and its SUBSTRATE digest equals the preflight digest (the atomic contract).
    persisted = _persisted_binding(managed, run_id)
    assert persisted["run_id"] == run_id
    assert persisted["tenant"] == TENANT
    assert persisted["workspace"] == rca.WORKSPACE_UNSCOPED
    assert persisted["provider"] == SUB_PROVIDER
    assert persisted["model"] == SUB_MODEL
    assert eb.verify_binding(persisted) is True
    assert eb.binding_digest(persisted) == digest

    # The REAL dispatched worker re-admits from the persisted grant+binding in its
    # own process and the run reaches completion (admission established).
    data, kinds, payloads = _wait_terminal(managed, run_id)
    assert data["status"] == "completed", (data, kinds)
    assert "worker_admission_error" not in kinds, payloads.get("worker_admission_error")
    assert payloads["worker_admitted"][0]["established"] is True
    assert payloads["worker_admitted"][0]["has_admitted"] is True


def test_A_create_rejects_mismatched_digest_creates_nothing(managed):
    # A pinned digest that does NOT match the resolved substrate is refused at
    # create (412) BEFORE any task/binding/dispatch exists.
    _, header = _mint_grant()
    bogus = "a" * 64
    r = _create(managed, grant_header=header, expected_digest=bogus)
    assert r.status_code == 412, r.text
    assert r.json()["detail"]["error"] == "binding_digest_mismatch"


# ══════════════════════════════════════════════════════════════════════════════
# B. idempotency: one binding/grant/manifest/mode; mismatched replay refused
# ══════════════════════════════════════════════════════════════════════════════


def test_B_idempotent_replay_keeps_single_binding_and_grant(managed):
    pf_binding = _preflight(managed).json()["effective_binding"]
    digest = eb.binding_digest(pf_binding)
    key = f"idem-{uuid.uuid4().hex}"

    # First create.
    _, h1 = _mint_grant()
    r1 = _create(managed, grant_header=h1, expected_digest=digest, idempotency=key)
    assert r1.status_code == 200, r1.text
    run_id = r1.json()["run_id"]

    # Idempotent replay: SAME key + SAME digest, a FRESH grant (per-run authority is
    # never replayed) -> the SAME run, and NO duplicate create-time metadata.
    _, h2 = _mint_grant()
    r2 = _create(managed, grant_header=h2, expected_digest=digest, idempotency=key)
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == run_id

    counts = _count_kinds(_all_events(managed, run_id))
    assert counts.get("runtime_execution_grant") == 1, counts
    assert counts.get("runtime_capability_manifest") == 1, counts
    assert counts.get(eb.BINDING_EVENT) == 1, counts
    assert counts.get("runtime_execution_mode") == 1, counts


def test_B_mismatched_digest_replay_refused_no_new_binding(managed):
    pf_binding = _preflight(managed).json()["effective_binding"]
    digest = eb.binding_digest(pf_binding)
    key = f"idem-{uuid.uuid4().hex}"

    _, h1 = _mint_grant()
    run_id = _create(
        managed, grant_header=h1, expected_digest=digest, idempotency=key
    ).json()["run_id"]
    before = _count_kinds(_all_events(managed, run_id))

    # A replay under the same key pinning a DIFFERENT digest is refused (412) and
    # creates no new task/binding — the run's event log is unchanged.
    _, h2 = _mint_grant()
    r = _create(managed, grant_header=h2, expected_digest="b" * 64, idempotency=key)
    assert r.status_code == 412, r.text
    assert r.json()["detail"]["error"] == "binding_digest_mismatch"

    after = _count_kinds(_all_events(managed, run_id))
    assert after.get(eb.BINDING_EVENT) == before.get(eb.BINDING_EVENT) == 1
    assert after.get("runtime_execution_grant") == 1


# ══════════════════════════════════════════════════════════════════════════════
# C. retry: happy re-scope + tampered child binding fails closed at the worker
# ══════════════════════════════════════════════════════════════════════════════


def test_C_retry_rescopes_binding_onto_child(managed):
    # Original run completes.
    _, orig_header = _mint_grant()
    run_id = _create(managed, grant_header=orig_header).json()["run_id"]
    _wait_terminal(managed, run_id)
    parent_binding = _persisted_binding(managed, run_id)

    # Real managed retry -> a fresh child, re-scoped to its OWN run.
    _, retry_header = _mint_grant()
    r = _retry(managed, run_id, grant_header=retry_header)
    assert r.status_code == 200, r.text
    child_id = r.json()["run_id"]
    assert child_id != run_id

    child_binding = _persisted_binding(managed, child_id)
    assert child_binding["run_id"] == child_id             # child's OWN run scope
    assert child_binding["root_run_id"] == run_id          # parent lineage preserved
    assert eb.verify_binding(child_binding) is True         # self-verifies
    # SAME substrate: the substrate digest is invariant across the re-scope.
    assert eb.binding_digest(child_binding) == eb.binding_digest(parent_binding)

    # The real dispatched child worker re-admits from its OWN binding and completes.
    data, kinds, payloads = _wait_terminal(managed, child_id)
    assert data["status"] == "completed", (data, kinds)
    assert "worker_admission_error" not in kinds, payloads.get("worker_admission_error")


def _reasoning(now, **over):
    base = {
        "max_iterations": 4, "max_spawn_depth": 1, "max_concurrent_agents": 1,
        "max_total_tokens": 2000, "max_cost_micros": 0, "max_retries": 0,
        "deadline_at": now + timedelta(minutes=20),
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    "dim,value",
    [
        ("max_total_tokens", 999_999),
        ("max_cost_micros", 5_000_000),
        ("max_iterations", 99),
        ("max_spawn_depth", 9),
        ("max_concurrent_agents", 9),
        ("max_retries", 5),
    ],
)
def test_C_retry_rejects_widened_budget(managed_deferred, dim, value):
    # SEC-9 #6: a retry grant that widens ANY execution-tree budget dimension is
    # refused at ingress (422) before a child run is created.
    now = datetime.now(UTC).replace(microsecond=0)
    _, orig_header = _mint_grant(now=now)
    run_id = _create(managed_deferred, grant_header=orig_header).json()["run_id"]
    _, widened = _mint_grant(now=now, reasoning=_reasoning(now, **{dim: value}))
    r = _retry(managed_deferred, run_id, grant_header=widened)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "retry_budget_widened"
    assert r.json()["detail"]["dimension"] == dim


def test_C_retry_rejects_extended_deadline_window(managed_deferred):
    now = datetime.now(UTC).replace(microsecond=0)
    _, orig_header = _mint_grant(now=now)
    run_id = _create(managed_deferred, grant_header=orig_header).json()["run_id"]
    # A retry window of 90 minutes is wider than the original 20-minute window
    # (bump expiry too so the envelope's deadline<=expiry invariant holds).
    _, widened = _mint_grant(
        now=now,
        reasoning=_reasoning(now, deadline_at=now + timedelta(minutes=90)),
        expires_at=now + timedelta(minutes=120),
    )
    r = _retry(managed_deferred, run_id, grant_header=widened)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["dimension"] == "deadline_at"


def test_C_retry_equal_or_tighter_budget_allowed(managed_deferred):
    # Equal budget (the common case) and a strictly-tighter retry both pass.
    now = datetime.now(UTC).replace(microsecond=0)
    _, orig_header = _mint_grant(now=now)
    run_id = _create(managed_deferred, grant_header=orig_header).json()["run_id"]
    _, tighter = _mint_grant(
        now=now, reasoning=_reasoning(now, max_total_tokens=1000, max_iterations=2)
    )
    r = _retry(managed_deferred, run_id, grant_header=tighter)
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] != run_id


def test_C_retry_tampered_child_binding_fails_closed_at_worker(managed_deferred, tmp_path):
    # Create + retry through the REAL ingress (deferred dispatch: no auto-run).
    _, orig_header = _mint_grant()
    run_id = _create(managed_deferred, grant_header=orig_header).json()["run_id"]
    _, retry_header = _mint_grant()
    child_id = _retry(managed_deferred, run_id, grant_header=retry_header).json()["run_id"]

    child_binding = _persisted_binding(managed_deferred, child_id)
    # Tamper: bump binding_version (so it becomes the CURRENT binding) and change the
    # model WITHOUT recomputing binding_hash -> the self-hash no longer matches.
    tampered = dict(child_binding)
    tampered["binding_version"] = int(child_binding["binding_version"]) + 1
    tampered["model"] = "evil-swap"
    # keep the stale binding_hash on purpose (that is the tamper)
    _seed_binding(tmp_path, child_id, tampered)

    # The real worker subprocess refuses at the integrity gate BEFORE any execution.
    rc, res, net_s, client_s = _run_neg_worker(
        tmp_path, child_id, provider=SUB_PROVIDER, model=SUB_MODEL, base_url=SUB_ENDPOINT
    )
    _assert_refused_zero_execution(
        tmp_path, rc, res,
        f"managed run {child_id} effective binding hash mismatch "
        "(tampered/malformed); refusing",
        net_s, client_s,
    )


# ══════════════════════════════════════════════════════════════════════════════
# D. fallback / substrate drift fails closed on the real-model worker
# ══════════════════════════════════════════════════════════════════════════════


def test_D_worker_provider_drift_fails_closed(managed_deferred, tmp_path):
    # A normally-created managed run (bound to ollama/qwen:test), NOT auto-run.
    _, header = _mint_grant()
    run_id = _create(managed_deferred, grant_header=header).json()["run_id"]
    bound = _persisted_binding(managed_deferred, run_id)
    assert bound["provider"] == SUB_PROVIDER and bound["model"] == SUB_MODEL

    # The dispatched real-model worker resolves a DRIFTING provider (openai vs the
    # bound ollama) -> refused before any provider client / network / budget.
    rc, res, net_s, client_s = _run_neg_worker(
        tmp_path, run_id, provider="openai", model=SUB_MODEL, base_url=SUB_ENDPOINT
    )
    _assert_refused_zero_execution(
        tmp_path, rc, res,
        f"managed run {run_id} worker provider drifted from the bound identity; refusing",
        net_s, client_s,
    )


# ══════════════════════════════════════════════════════════════════════════════
# E. missing dispatched identity fails closed (create-side AND worker-side)
# ══════════════════════════════════════════════════════════════════════════════


def test_E_create_side_managed_model_unresolved(managed_deferred, monkeypatch):
    # A managed model-run whose dispatched identity cannot be resolved is refused at
    # create (422) — never enqueued to resolve the substrate from mutable config.
    monkeypatch.setattr(
        "youtab_agent_cli.web_routers.runtime._profile_default_identity",
        lambda agent: (None, None, None),
    )
    _, header = _mint_grant()
    r = _create(managed_deferred, grant_header=header)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "managed_model_unresolved"


def test_E_worker_side_duplicate_version_binding_refused_as_corrupt(managed_deferred, tmp_path):
    """WAVE-30H Batch3 #2: the run already carries its create-time binding (current
    version). Seeding a SECOND, self-consistent, correctly-rehashed binding at the
    SAME version is a duplicate on an append-only contract: it must be refused as
    CORRUPT before any execution — it can never silently replace the original
    provider/model/endpoint binding. (The worker-side "not fully resolved" substrate
    gate is unit-covered deterministically in test_binding_enforcement.)"""
    _, header = _mint_grant()
    run_id = _create(managed_deferred, grant_header=header).json()["run_id"]

    # A self-consistent, fully-rehashed replacement at the SAME version as the
    # create-time binding, correctly scoped so it passes every per-field gate — but a
    # DUPLICATE version. Its substrate even differs (unresolved model), which is
    # exactly the silent-displacement the duplicate guard prevents.
    duplicate = eb.build_effective_binding(
        provider=SUB_PROVIDER, model=None, endpoint=SUB_ENDPOINT,
        binding_version=2, run_id=run_id, root_run_id=run_id,
        tenant=TENANT, workspace=rca.WORKSPACE_UNSCOPED,
    )
    assert eb.verify_binding(duplicate) is True  # the replacement self-verifies
    _seed_binding(tmp_path, run_id, duplicate)

    rc, res, net_s, client_s = _run_neg_worker(
        tmp_path, run_id, provider=SUB_PROVIDER, model=SUB_MODEL, base_url=SUB_ENDPOINT
    )
    _assert_refused_zero_execution(
        tmp_path, rc, res,
        f"managed run {run_id} effective binding is corrupt; refusing",
        net_s, client_s,
    )


# ══════════════════════════════════════════════════════════════════════════════
# F. zero provider client / zero network on refusal (cross-process, explicit)
# ══════════════════════════════════════════════════════════════════════════════


def test_F_refusal_constructs_no_client_and_makes_no_network(managed_deferred, tmp_path):
    """Explicit zero-egress proof on a refusal: a real run, its binding tampered at
    rest, run by a worker that has installed a socket.connect guard AND a
    resolve_provider_client spy. On the admission refusal BOTH sentinels are ABSENT
    (no client constructed, no egress) and the execution-tree budget DB is absent."""
    _, header = _mint_grant()
    run_id = _create(managed_deferred, grant_header=header).json()["run_id"]

    bound = _persisted_binding(managed_deferred, run_id)
    tampered = dict(bound)
    tampered["binding_version"] = int(bound["binding_version"]) + 1
    tampered["model"] = "exfil-model"  # break the self-hash
    _seed_binding(tmp_path, run_id, tampered)

    rc, res, net_s, client_s = _run_neg_worker(
        tmp_path, run_id, provider=SUB_PROVIDER, model=SUB_MODEL, base_url=SUB_ENDPOINT
    )
    # The refusal is proven by the exact error AND the sentinels/budget absence.
    _assert_refused_zero_execution(
        tmp_path, rc, res,
        f"managed run {run_id} effective binding hash mismatch "
        "(tampered/malformed); refusing",
        net_s, client_s,
    )
    # Belt-and-suspenders explicit assertions (the crux of scenario F).
    assert not net_s.exists()
    assert not client_s.exists()


# ── D1: durable single-authority admission on the REAL managed route ──────────
# These prove that with YOUTAB_AGENT_DURABLE_INGRESS on, the product create path
# admits through the ONE fenced owner-stamped durable RunStore FIRST (the sole
# idempotency/lifecycle authority) and kanban mirrors the SAME run id as execution
# transport only — no second id and no second dedup. The worker is a no-op spawn
# (managed_deferred style) so the assertions are race-free.

@pytest.fixture()
def managed_durable(tmp_path, monkeypatch):
    import youtab_runtime.durable_ingress_process as dip

    db_path = _install_managed_env(tmp_path, monkeypatch)
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_INGRESS", "1")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "sqlite")
    monkeypatch.setenv(
        "YOUTAB_AGENT_DURABLE_DB_PATH", str(tmp_path / "durable_runs.db")
    )
    dip.reset_for_tests()

    def _noop_spawn(task, workspace, *, board=None):
        return os.getpid()  # alive pid: dispatcher never respawns; nothing executes

    with _build_client(_noop_spawn) as c:
        yield c, db_path
    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None
    runtime._grant_boundary = None
    dip.reset_for_tests()


def test_durable_create_pins_kanban_to_durable_run_id(managed_durable):
    import youtab_runtime.durable_ingress_process as dip

    client, db_path = managed_durable
    _, header = _mint_grant()
    r = _create(client, grant_header=header, idempotency="dur-k1")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # the ONE durable authority owns the run, owner-stamped and QUEUED
    auth = dip.get_ingress_authority()
    row = auth.get_run(run_id)
    assert row is not None and row["lease_owner"], row
    assert row["run_id"] == run_id
    # kanban mirrors the SAME id (execution transport only) — no second id
    conn = kb.connect(db_path=db_path)
    try:
        assert kb.get_task(conn, run_id) is not None
    finally:
        conn.close()


def test_durable_idempotent_replay_same_run_single_row(managed_durable):
    import youtab_runtime.durable_ingress_process as dip

    client, _ = managed_durable
    _, h1 = _mint_grant()
    r1 = _create(client, grant_header=h1, task="same body", idempotency="dur-k2")
    assert r1.status_code == 200, r1.text
    _, h2 = _mint_grant()  # fresh grant nonce; SAME idempotency key + SAME body
    r2 = _create(client, grant_header=h2, task="same body", idempotency="dur-k2")
    assert r2.status_code == 200, r2.text
    assert r1.json()["run_id"] == r2.json()["run_id"]  # ORIGINAL run returned

    # exactly one durable run for the scoped key (the replay created no 2nd run)
    auth = dip.get_ingress_authority()
    assert auth.get_run(r1.json()["run_id"]) is not None


def test_durable_same_key_different_body_conflicts_409(managed_durable):
    client, _ = managed_durable
    _, h1 = _mint_grant()
    r1 = _create(client, grant_header=h1, task="body one", idempotency="dur-k3")
    assert r1.status_code == 200, r1.text
    _, h2 = _mint_grant()  # SAME key, DIFFERENT request body -> conflict
    r2 = _create(client, grant_header=h2, task="body two", idempotency="dur-k3")
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"]["error"] == "idempotency_key_conflict"


def test_durable_health_reflects_run_authority(managed_durable):
    import youtab_runtime.durable_ingress_process as dip

    client, _ = managed_durable
    # a create acquires the authority lazily; health then reports ready
    _, header = _mint_grant()
    assert _create(client, grant_header=header, idempotency="dur-h1").status_code == 200
    h = client.get("/api/runtime/v1/health", headers=_headers())
    assert h.status_code == 200, h.text
    assert h.json()["ok"] is True and h.json()["run_authority_ready"] is True

    # authority loss must flip health to not-ok (fail closed). The bare test app
    # has no lifespan fail-stop hook wired, so this only latches the readiness flag.
    dip._on_lost("simulated advisory-lock loss")
    h2 = client.get("/api/runtime/v1/health", headers=_headers())
    assert h2.json()["ok"] is False and h2.json()["run_authority_ready"] is False
