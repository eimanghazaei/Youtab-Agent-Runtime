"""WAVE-30H (PR #44) — ONE CONTINUOUS deterministic managed-runtime E2E.

This is the single end-to-end proof the four Batch5 blockers were remediated
*in situ*, exercising every real transition of a managed model-run in one
uninterrupted flow — NOTHING is manually seeded on the positive path:

    runtime preflight (real FastAPI ingress + token_auth + RuntimeServiceProvider)
      -> create_run persistence (real Ed25519 Simorgh grant + HMAC command +
         create_task_ex atomic binding/grant/manifest/mode + row-pinned
         provider_override/model_override)
        -> PRODUCTION worker-invocation construction: the dispatcher spawn hook
           calls the REAL ``youtab_agent_cli.kanban_db.build_worker_invocation``
           and executes the returned (env, cmd) UNCHANGED
          -> the shipped worker CLI (``youtab -p default --cli --accept-hooks
             -m qwen:test --provider ollama chat -q "work kanban task <id>"`` and,
             for goal_mode, the ``-Q`` variant)
            -> single verified pre-admission load -> pre-credential provider gate
               -> credential resolution EXACTLY ONCE -> actual-resolved-identity
               vs binding compare -> real shipped provider client construction
               -> FINAL managed admission (same frozen snapshot)
              -> loopback inference against a hermetic 127.0.0.1 responder that
                 drives a REAL ``kanban_complete`` tool call
                -> the tool_executor authority gate consumes the sealed admitted
                   context and admits ``kanban_complete`` (effect-class ``none``)
                  -> the card reaches a TERMINAL completed state and the run is
                     observed terminal through the real events API.

The trust mode is ``managed`` throughout — the Simorgh admission + tool-authority
gates are ACTIVE, not inert. This proves the legitimate managed worker CAN admit
and complete its OWN grant-authorized task; it is NOT a bypass. The only
substituted boundary is the model "brain": a loopback OpenAI/Ollama-compatible
responder stands in for a paid provider (the (provider, model, endpoint) substrate
is one consistent local ``ollama``/``qwen:test``/loopback identity so preflight and
create bind the SAME digest offline). No paid/cloud endpoint is ever contacted —
the test asserts every socket destination dialed was loopback.

Focused suites remain the authoritative unit/adversarial coverage; this file adds
the ONE continuous happy-path traversal and does not replace them.

Run by explicit path only (never the broad tests/youtab_agent_cli suite):
  .venv-qual/Scripts/python.exe -m pytest \
    tests/youtab_runtime/test_managed_continuous_e2e.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from youtab_agent_cli import effective_binding as eb
from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli import runtime_command_auth as rca
from youtab_agent_cli.web_routers import runtime
from youtab_runtime import managed_execution as mx

# Reuse the REAL managed-ingress harness (app + middleware + router + dispatcher
# override) and the shipped-CLI sitecustomize sentinels — no re-implementation.
from tests.youtab_runtime.test_managed_binding_e2e import (
    REPO_ROOT,
    SUB_MODEL,
    SUB_PROVIDER,
    TENANT,
    USER,
    _all_events,
    _build_client,
    _headers,
    _install_managed_env,
    _mint_grant,
    _preflight,
    _persisted_binding,
    _sign,
    _wait_terminal,
)
from tests.youtab_runtime.test_managed_cli_e2e import _SITECUSTOMIZE

# Distinctive, stable marker of the goal-judge system prompt (goals.py
# JUDGE_SYSTEM_PROMPT) so the loopback server can answer a judge call with a
# terminal "done" verdict instead of an agent-turn tool call.
_JUDGE_MARKER = "strict judge evaluating"


class _ToolCallInferenceHandler(BaseHTTPRequestHandler):
    """Hermetic, loopback-only OpenAI/Ollama-compatible responder.

    Stateless content-routing (no shared counters → safe across turns):
      * a goal-judge request              -> a plain ``{"verdict":"done"}`` reply
      * a follow-up carrying a tool result -> a plain ``finish_reason:"stop"`` reply
      * otherwise (the worker's task turn)  -> a ``kanban_complete`` tool call

    Honours the request's ``stream`` flag (SSE when set, single JSON body
    otherwise). No cloud is ever contacted.
    """

    def log_message(self, *a):  # silence access logging
        pass

    # ── JSON helpers ─────────────────────────────────────────────────────────
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ── response builders ────────────────────────────────────────────────────
    @staticmethod
    def _assistant_message(*, content=None, tool_calls=None):
        msg = {"role": "assistant", "content": content}
        if tool_calls is not None:
            msg["tool_calls"] = tool_calls
        return msg

    def _completion_json(self, message, finish_reason):
        return {
            "id": "chatcmpl-e2e", "object": "chat.completion", "model": "qwen:test",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def _stream_chunks(self, message, finish_reason):
        delta = {"role": "assistant"}
        if message.get("content") is not None:
            delta["content"] = message["content"]
        if message.get("tool_calls") is not None:
            delta["tool_calls"] = [
                {
                    "index": 0,
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in message["tool_calls"]
            ]
        return [
            {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "model": "qwen:test",
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "model": "qwen:test",
             "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]},
            {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "model": "qwen:test",
             "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ]

    def _reply(self, message, finish_reason, stream):
        if stream:
            self._send_sse(self._stream_chunks(message, finish_reason))
        else:
            self._send_json(200, self._completion_json(message, finish_reason))

    # ── routing ──────────────────────────────────────────────────────────────
    def do_GET(self):
        if self.path.endswith("/models"):
            self._send_json(200, {"object": "list", "data": [{"id": "qwen:test", "object": "model"}]})
        elif "tags" in self.path:
            self._send_json(200, {"models": [{"name": "qwen:test"}]})
        else:
            self._send_json(200, {"status": "ok"})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(ln) if ln else b""
        if "chat/completions" not in self.path:
            # Ollama capability probes (/api/show, etc.) — fail-open, minimal body.
            self._send_json(200, {})
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            body = {}
        stream = bool(body.get("stream"))
        messages = body.get("messages") or []
        blob = " ".join(
            str(m.get("content") or "") for m in messages if isinstance(m, dict)
        ).lower()
        has_tool_result = any(
            isinstance(m, dict) and m.get("role") == "tool" for m in messages
        )

        if _JUDGE_MARKER in blob:
            # Goal-judge turn: report the acceptance criteria satisfied so the
            # goal_mode completion gate + goal loop resolve to "done".
            self._reply(
                self._assistant_message(
                    content='{"verdict": "done", "reason": "acceptance criteria satisfied"}'
                ),
                "stop", stream,
            )
        elif has_tool_result:
            # Post-tool follow-up: end the turn cleanly (finish_reason stop).
            self._reply(self._assistant_message(content="Task complete."), "stop", stream)
        else:
            # The worker's task turn: drive a REAL kanban_complete tool call. The
            # task id is read from YOUTAB_AGENT_KANBAN_TASK by the tool, so only a
            # summary is supplied (the handler requires summary OR result).
            self._reply(
                self._assistant_message(
                    tool_calls=[{
                        "id": "call_complete_1",
                        "type": "function",
                        "function": {
                            "name": "kanban_complete",
                            "arguments": json.dumps(
                                {"summary": "E2E: task verified and completed."}
                            ),
                        },
                    }]
                ),
                "tool_calls", stream,
            )


def _create_run(client, *, grant_header, expected_digest, goal_mode, task_text):
    """POST /runs through the REAL signed ingress (optionally goal_mode)."""
    payload = {"agent": "default", "task": task_text, "goal_mode": goal_mode}
    if expected_digest is not None:
        payload["expected_binding_digest"] = expected_digest
    body = json.dumps(payload).encode()
    path = "/api/runtime/v1/runs"
    h = _headers(TENANT, USER)
    h.update(_sign("POST", path, TENANT, USER, body, h["X-Youtab-Correlation-Id"]))
    h["Content-Type"] = "application/json"
    h[mx.GRANT_HEADER] = grant_header
    return client.post(path, content=body, headers=h)


def _worker_env(built_env, *, task_id, sentinel_dir, endpoint, pki, fixture_db):
    """Augment the env build_worker_invocation returned: install the loopback
    inference endpoint, the sitecustomize sentinels, and a hermetic provider
    surface — WITHOUT touching the constructed argv. Strips inherited ambient
    provider config so the run behaves identically on any host (mirrors the
    focused CLI-E2E harness)."""
    env = dict(built_env)
    for leak in list(env):
        if (
            leak.endswith(("_API_KEY", "_TOKEN", "_BASE_URL", "_API_BASE"))
            or leak in {"OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "YOUTAB_PORTAL_TOKEN"}
        ):
            env.pop(leak, None)
    # The shipped worker resolves ollama inference from OLLAMA_BASE_URL; the
    # loopback OPENAI_BASE_URL also satisfies main()'s first-run guard on a
    # clean host. A non-``sk-`` dummy key avoids any secret-scanner false hit.
    env["OLLAMA_BASE_URL"] = endpoint
    env["OPENAI_BASE_URL"] = endpoint
    env["OPENAI_API_KEY"] = "e2e-local-not-a-real-key"
    # Belt-and-braces: pin the worker's kanban store to the exact ingress DB so
    # load_verified_preadmission re-verifies the REALLY-persisted grant/binding.
    env["YOUTAB_AGENT_KANBAN_DB"] = str(fixture_db)
    env["YOUTAB_AGENT_KANBAN_TASK"] = task_id
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    env["E2E_CLIENT_SENTINEL"] = str(sentinel_dir / "client_constructed.sentinel")
    env["E2E_NET_SENTINEL"] = str(sentinel_dir / "network_attempted.sentinel")
    env["E2E_NET_DEST"] = str(sentinel_dir / "net_destinations.log")
    env["E2E_CREDRESOLVE_SENTINEL"] = str(sentinel_dir / "credresolve.sentinel")
    env["E2E_VERTEX_SENTINEL"] = str(sentinel_dir / "vertex_minted.sentinel")
    env["E2E_YOUTAB_SENTINEL"] = str(sentinel_dir / "youtab_refreshed.sentinel")
    env["E2E_PREADMIT_SENTINEL"] = str(sentinel_dir / "preadmit.sentinel")
    env["E2E_ROUTEPLAN_SENTINEL"] = str(sentinel_dir / "routeplan.sentinel")
    env["E2E_ADMIT_SENTINEL"] = str(sentinel_dir / "admit.sentinel")
    # sitecustomize dir FIRST (imported at interpreter startup), then the repo so
    # the working-tree code wins over any installed package.
    env["PYTHONPATH"] = (
        str(pki) + os.pathsep + str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    )
    return env


def _read_sentinels(sentinel_dir):
    def _exists(name):
        return (sentinel_dir / name).exists()

    cred = sentinel_dir / "credresolve.sentinel"
    credresolve_count = (
        len([ln for ln in cred.read_text().splitlines() if ln.strip()])
        if cred.exists() else 0
    )
    dest_log = sentinel_dir / "net_destinations.log"
    dests = dest_log.read_text().splitlines() if dest_log.exists() else []
    return {
        "preadmitted": _exists("preadmit.sentinel"),
        "provider_gate": _exists("routeplan.sentinel"),
        "client_constructed": _exists("client_constructed.sentinel"),
        "admitted": _exists("admit.sentinel"),
        "network_attempted": _exists("network_attempted.sentinel"),
        "credresolve_count": credresolve_count,
        "vertex_minted": _exists("vertex_minted.sentinel"),
        "youtab_refreshed": _exists("youtab_refreshed.sentinel"),
        "net_destinations": dests,
    }


def _pki_dir(tmp_path):
    d = tmp_path / "pki"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return d


def _run_continuous(tmp_path, monkeypatch, *, goal_mode):
    """Drive the full continuous managed E2E for one dispatch mode.

    Returns ``(run_id, sentinels, worker_log_text, terminal_status)``.
    """
    # A hermetic loopback inference server; its port defines the substrate
    # endpoint the ingress binds AND the worker's OLLAMA_BASE_URL — so the
    # ACTUAL resolved route matches the persisted binding exactly.
    server = HTTPServer(("127.0.0.1", 0), _ToolCallInferenceHandler)
    port = server.server_address[1]
    endpoint = f"http://127.0.0.1:{port}/v1"
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    fixture_db = _install_managed_env(tmp_path, monkeypatch)
    # Populate the tool registry in THIS (ingress) process exactly as production
    # does — the youtab runtime/gateway process imports model_tools, which calls
    # discover_builtin_tools() at import (model_tools.py:197). Without it the
    # ingress freezes an EMPTY capability manifest and the worker's real
    # kanban_complete would be (correctly) denied by the authority gate. This is
    # the established registry-population pattern used across the tool tests.
    from tools.registry import discover_builtin_tools
    discover_builtin_tools()
    # Bind preflight AND create to the loopback substrate (offline, pure).
    monkeypatch.setattr(
        "youtab_agent_cli.web_routers.runtime._profile_default_identity",
        lambda agent: (SUB_PROVIDER, SUB_MODEL, endpoint),
    )
    monkeypatch.setattr(
        "youtab_agent_cli.web_routers.runtime._configured_inference_base_url",
        lambda: endpoint,
    )
    monkeypatch.setattr(
        "youtab_agent_cli.web_routers.runtime._configured_model_provider_names",
        lambda: {"provider": SUB_PROVIDER, "model": SUB_MODEL},
    )
    # Force the interpreter-bound worker form so the SAME Python honours our
    # PYTHONPATH + sitecustomize on every host (a bare, unresolvable name makes
    # _resolve_youtab_argv fall through to ``sys.executable -m youtab_agent_cli.main``).
    monkeypatch.setenv("YOUTAB_AGENT_BIN", "youtab-e2e-force-module-form")

    pki = _pki_dir(tmp_path)
    worker_root = tmp_path / "worker"
    procs: "dict[str, subprocess.Popen]" = {}
    logs: "dict[str, Path]" = {}

    def _spawn(task, workspace, *, board=None):
        # Idempotent per task: if a live worker already exists, report its pid so
        # the dispatcher never respawns while it is completing.
        existing = procs.get(task.id)
        if existing is not None and existing.poll() is None:
            return existing.pid
        # PRODUCTION worker-invocation construction — the real builder, executed
        # UNCHANGED (only the env is augmented for observability + loopback).
        built_env, cmd = kb.build_worker_invocation(task, workspace, board=board)
        sentinel_dir = worker_root / task.id
        env = _worker_env(
            built_env, task_id=task.id, sentinel_dir=sentinel_dir,
            endpoint=endpoint, pki=pki, fixture_db=fixture_db,
        )
        log_path = sentinel_dir / "worker.log"
        logs[task.id] = log_path
        log_f = open(log_path, "wb")
        proc = subprocess.Popen(
            cmd, env=env,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL, stdout=log_f, stderr=subprocess.STDOUT,
        )
        procs[task.id] = proc
        return proc.pid

    try:
        with _build_client(_spawn) as client:
            pf = _preflight(client)
            assert pf.status_code == 200, pf.text
            digest = eb.binding_digest(pf.json()["effective_binding"])
            assert len(digest) == 64

            _env, header = _mint_grant()
            r = _create_run(
                client, grant_header=header, expected_digest=digest,
                goal_mode=goal_mode,
                task_text=f"continuous managed e2e {uuid.uuid4().hex[:8]}",
            )
            assert r.status_code == 200, r.text
            run_id = r.json()["run_id"]

            # The persisted binding is the canonical ollama substrate the worker
            # must re-admit and match (no manual seeding — this came from create).
            bound = _persisted_binding(client, run_id)
            assert bound["provider"] == SUB_PROVIDER and bound["model"] == SUB_MODEL

            # The REAL dispatched worker (built by build_worker_invocation) runs the
            # shipped CLI, admits the persisted grant/binding, constructs the bound
            # client, performs loopback inference, and completes the card. Real
            # cold-start CLI + inference is heavier than the stub — allow generously.
            try:
                data, _kinds, _payloads = _wait_terminal(client, run_id, timeout=120)
                terminal_status = data["status"]
            except AssertionError:
                terminal_status = "NOT_TERMINAL"
            # Event trail for a durable failure diagnostic (never an opaque timeout).
            event_kinds = [e["kind"] for e in _all_events(client, run_id)]
    finally:
        server.shutdown()
        server.server_close()
        for proc in procs.values():
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=15)
                except (OSError, subprocess.SubprocessError):
                    try:
                        proc.kill()
                    except OSError:
                        pass
        runtime.stop_dispatcher()
        runtime._spawn_override = None
        runtime._nonce_store = None
        runtime._grant_boundary = None

    sentinels = _read_sentinels(worker_root / run_id)
    log_text = ""
    lp = logs.get(run_id)
    if lp is not None and lp.exists():
        log_text = lp.read_text(encoding="utf-8", errors="replace")
    return run_id, sentinels, log_text, terminal_status, event_kinds


def _assert_full_chain(run_id, sentinels, log_text, terminal_status, event_kinds):
    diag = (
        f"\nrun_id={run_id} terminal_status={terminal_status!r}"
        f"\nevent_kinds={event_kinds}"
        f"\nsentinels={json.dumps(sentinels, indent=2)}"
        f"\n--- worker.log (last 3000) ---\n{log_text[-3000:]}"
    )
    # Ordered managed-path landmarks.
    assert sentinels["preadmitted"], "single verified pre-admission load did not run" + diag
    assert sentinels["provider_gate"], "pre-credential provider gate did not run" + diag
    # Blocker 3: credentials resolved EXACTLY ONCE (no _init_agent re-resolution).
    assert sentinels["credresolve_count"] == 1, (
        f"expected exactly one credential resolution, got "
        f"{sentinels['credresolve_count']}" + diag
    )
    # A real shipped provider client was constructed.
    assert sentinels["client_constructed"], "the bound provider client was not constructed" + diag
    # FINAL managed admission ran (and, since the run completed, succeeded).
    assert sentinels["admitted"], "final managed admission did not run" + diag
    # No drift to an unauthorized cloud provider.
    assert not sentinels["vertex_minted"], "a Vertex token was minted on the valid path" + diag
    assert not sentinels["youtab_refreshed"], "a Youtab key was refreshed on the valid path" + diag
    # Loopback-only: at least the inference dial, and NOTHING off-box.
    assert sentinels["net_destinations"], "expected at least the loopback inference dial" + diag
    for dest in sentinels["net_destinations"]:
        assert ("127.0.0.1" in dest or "localhost" in dest or "::1" in dest), (
            f"a non-loopback endpoint was contacted: {dest}" + diag
        )
    # Terminal run/task state: the card was completed by a REAL kanban_complete
    # tool call admitted through the active tool-authority gate.
    assert terminal_status == "completed", "run did not reach the completed terminal state" + diag


@pytest.mark.timeout(300)
def test_continuous_managed_e2e_chat_q(tmp_path, monkeypatch):
    """Non-goal worker: shipped ``chat -q "work kanban task <id>"`` dispatch."""
    run_id, sentinels, log_text, terminal_status, event_kinds = _run_continuous(
        tmp_path, monkeypatch, goal_mode=False
    )
    _assert_full_chain(run_id, sentinels, log_text, terminal_status, event_kinds)


@pytest.mark.timeout(300)
def test_continuous_managed_e2e_chat_Q(tmp_path, monkeypatch):
    """Goal-mode worker: shipped ``chat -q "work kanban task <id>" -Q`` dispatch
    (build_worker_invocation appends ``-Q`` for a goal_mode card). Same full
    chain; the goal loop exits as soon as the worker completes the card."""
    run_id, sentinels, log_text, terminal_status, event_kinds = _run_continuous(
        tmp_path, monkeypatch, goal_mode=True
    )
    _assert_full_chain(run_id, sentinels, log_text, terminal_status, event_kinds)
