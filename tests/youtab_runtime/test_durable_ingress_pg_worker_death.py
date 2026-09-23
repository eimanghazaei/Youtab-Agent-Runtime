"""Real PostgreSQL + worker-process death at the managed approval boundary.

The server retains the fenced authority while independent worker processes call
the shipped private HTTP endpoints. Killing a worker after a durable ack must
not create another run, approval request, or effect claim on retry. A claim is
an execution fence, not a receipt or proof that the host mutation happened.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn

from tests.youtab_runtime import test_managed_binding_e2e as binding_e2e
import youtab_runtime.durable_ingress_process as dip
from youtab_agent_cli.web_routers import runtime
from youtab_runtime.durable_ingress import DurableRunStateAuthority

psycopg = pytest.importorskip("psycopg")

_ADMIN_DSN = os.environ.get(
    "YOUTAB_TEST_PG_DSN", "postgresql://youtab:devpass@127.0.0.1:55432/durable"
)


def _pg_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(), reason="real PostgreSQL unavailable"
)


@pytest.fixture
def pg_dsn():
    dbname = "worker_death_" + uuid.uuid4().hex[:12]
    with psycopg.connect(_ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    dsn = f'{_ADMIN_DSN.rsplit("/", 1)[0]}/{dbname}'
    try:
        yield dsn
    finally:
        with psycopg.connect(_ADMIN_DSN, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                (dbname,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


_WORKER_SCRIPT = r"""
import json, os, sys, time, urllib.error, urllib.request
url, cap, body_json, ack_file = sys.argv[1:]
req = urllib.request.Request(url, data=body_json.encode(), method="POST",
                             headers={"X-Youtab-Worker-Cap": cap,
                                      "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=10) as response:
        result = {"status": response.status, "body": json.load(response)}
except urllib.error.HTTPError as error:
    result = {"status": error.code, "body": json.load(error)}
staged_ack = ack_file + ".tmp"
with open(staged_ack, "w", encoding="utf-8") as out:
    json.dump(result, out)
    out.flush()
    os.fsync(out.fileno())
os.replace(staged_ack, ack_file)
while True:
    time.sleep(1)
"""


def _start_http_server(app):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "private HTTP server did not start"
    return f"http://127.0.0.1:{port}", server, thread


def _kill_after_ack(url: str, cap: str, body: dict, ack_path: Path) -> dict:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WORKER_SCRIPT,
            url,
            cap,
            json.dumps(body),
            str(ack_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while (
            not ack_path.exists()
            and proc.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert ack_path.exists(), f"worker exited before ack: {proc.poll()}"
        result = json.loads(ack_path.read_text(encoding="utf-8"))
        proc.kill()  # kill the worker, never the server authority
        proc.wait(timeout=10)
        return result
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stderr:
            proc.stderr.close()


def test_pg_worker_death_after_approval_and_claim_has_no_duplicate_or_false_applied(
    tmp_path, monkeypatch, pg_dsn
):
    binding_e2e._install_managed_env(tmp_path, monkeypatch)
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_INGRESS", "1")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_PG_DSN", pg_dsn)
    dip.reset_for_tests()

    def noop_spawn(task, workspace, *, board=None):
        return os.getpid()

    server = thread = None
    try:
        with binding_e2e._build_client(noop_spawn) as client:
            _, first_grant = binding_e2e._mint_grant()
            created = binding_e2e._create(
                client, grant_header=first_grant, idempotency="worker-death-run"
            )
            assert created.status_code == 200, created.text
            run_id = created.json()["run_id"]
            binding_e2e._freeze_dispatcher()
            cap = binding_e2e._worker_cap(run_id)
            authority = dip.get_ingress_authority()
            assert authority.ready() and authority.get_run(run_id)["lease_owner"]
            base, server, thread = _start_http_server(client.app)
            approval_id = "worker-death-approval"
            request_body = {
                "approval_id": approval_id,
                "effect_digest": "effect-1",
                "action": "write",
                "mode": "once",
            }
            request_url = f"{base}/api/runtime/worker/v1/runs/{run_id}/approval-request"

            first = _kill_after_ack(
                request_url, cap, request_body, tmp_path / "first.json"
            )
            assert first["status"] == 200 and first["body"]["durable"] is True
            _, replay_grant = binding_e2e._mint_grant()
            replay = binding_e2e._create(
                client, grant_header=replay_grant, idempotency="worker-death-run"
            )
            assert replay.status_code == 200 and replay.json()["run_id"] == run_id
            second = _kill_after_ack(
                request_url, cap, request_body, tmp_path / "second.json"
            )
            assert second["status"] == 200 and second["body"]["durable"] is True
            assert first["body"]["effect_digest"] == second["body"]["effect_digest"]
            requests = [
                e
                for e in authority.get_events(run_id)
                if e["kind"] == "approval_request"
            ]
            assert len(requests) == 1

            _, grant = binding_e2e._mint_grant()
            decision = binding_e2e._approve(
                client, run_id, approval_id, "approve", grant_header=grant
            )
            assert decision.status_code == 200, decision.text

            claim_url = (
                f"{base}/api/runtime/worker/v1/runs/{run_id}/approval/"
                f"{approval_id}/effect-claim"
            )
            claimed = _kill_after_ack(
                claim_url,
                cap,
                {"attempt_id": "worker-1", "effect_digest": "effect-1"},
                tmp_path / "claim.json",
            )
            assert claimed["status"] == 200 and claimed["body"]["durable"] is True
            retry = _kill_after_ack(
                claim_url,
                cap,
                {"attempt_id": "worker-2", "effect_digest": "effect-1"},
                tmp_path / "retry.json",
            )
            assert retry["status"] == 409
            assert retry["body"]["detail"]["error"] == "effect_already_claimed"
            events = authority.get_events(run_id)
            assert len([e for e in events if e["kind"] == "effect_claim"]) == 1
            assert len([e for e in events if e["kind"] == "approval_request"]) == 1
            row = authority.get_run(run_id)
            assert row["result_ref"] is None and row["state"] == "RUNNING"
            with psycopg.connect(pg_dsn) as conn:
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM runs WHERE run_id=%s", (run_id,)
                    ).fetchone()[0]
                    == 1
                )
            # The original worker's cap cannot authorize a fresh effect after a
            # server-authority takeover. Prior active work becomes UNKNOWN,
            # preserving the one claim without fabricating an applied receipt.
            authority.release()
            successor = DurableRunStateAuthority(backend="postgres", dsn=pg_dsn)
            try:
                assert run_id in successor.acquire()
                recovered = successor.get_run(run_id)
                assert (
                    recovered["state"] == "UNKNOWN" and recovered["result_ref"] is None
                )
                assert (
                    len([
                        e
                        for e in successor.get_events(run_id)
                        if e["kind"] == "effect_claim"
                    ])
                    == 1
                )
                stale = _kill_after_ack(
                    claim_url,
                    cap,
                    {"attempt_id": "worker-3", "effect_digest": "effect-1"},
                    tmp_path / "stale.json",
                )
                assert stale["status"] in (401, 409, 503)
                assert (
                    len([
                        e
                        for e in successor.get_events(run_id)
                        if e["kind"] == "effect_claim"
                    ])
                    == 1
                )
            finally:
                successor.release()
    finally:
        if server is not None:
            server.should_exit = True
            thread.join(timeout=10)
        runtime.stop_dispatcher()
        runtime._spawn_override = None
        runtime._nonce_store = None
        runtime._grant_boundary = None
        dip.reset_for_tests()
