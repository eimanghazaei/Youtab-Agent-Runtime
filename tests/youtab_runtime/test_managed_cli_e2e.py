"""WAVE-30H Batch2 #F1/#F2 — TRUE CLI subprocess E2E of managed pre-admission.

This exercises the REAL shipped worker command the dispatcher spawns
(``python -m youtab_agent_cli.main -p <profile> ... chat -q "work kanban task <id>"``,
and the goal-mode ``-Q`` variant) — NOT a hand-built agent double calling
``establish_managed_admission`` directly. It proves that a managed run with an
invalid grant/binding is refused (exit code 3) by ``preadmit_managed_run()``
BEFORE ``_init_agent`` constructs any provider client and BEFORE any socket/network
activity, and that the budget/execution-tree is never opened on refusal.

Instrumentation is installed via a ``sitecustomize.py`` on ``PYTHONPATH`` (imported
by the interpreter at startup, BEFORE the CLI runs) that records — but does not
suppress — a provider-client-construction sentinel and a socket-connect sentinel.
It never alters the production admission/construction ordering; it only observes it.
So on a genuine refusal the sentinels are ABSENT (nothing was constructed/dialed),
and on a run that reaches construction they are PRESENT.

Placed under tests/youtab_runtime (never tests/youtab_agent_cli) to avoid the
Windows ``youtab update`` collection hazard. Run by explicit path only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from youtab_agent_cli import capability_manifest as cm
from youtab_agent_cli import effective_binding as eb

# Reuse the real grant/manifest/persistence helpers from the cross-process suite.
from tests.youtab_runtime.test_managed_execution_subprocess import (
    _KEYS_JSON,
    _grant_header,
    _ingress_manifest,
    _persist_run,
)
from youtab_agent_cli import kanban_db as kb

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc

# sitecustomize installed on PYTHONPATH: records a client / network sentinel without
# suppressing behaviour, so a refusal (nothing constructed/dialed) leaves them ABSENT.
_SITECUSTOMIZE = r'''
import os, socket
_C = os.environ.get("E2E_CLIENT_SENTINEL")
_N = os.environ.get("E2E_NET_SENTINEL")

def _touch(p):
    try:
        if p:
            open(p, "a").close()
    except Exception:
        pass

# Socket egress sentinel — record then call through (never suppress).
_real_connect = socket.socket.connect
def _spy_connect(self, address, *a, **k):
    _touch(_N)
    return _real_connect(self, address, *a, **k)
socket.socket.connect = _spy_connect

# Provider-client construction sentinel — patch the centralized resolver so any
# AIAgent construction that resolves a provider client records it (agent_init imports
# the name at call time, so patching the module attribute is picked up).
try:
    import agent.auxiliary_client as _ac
    _real_rpc = _ac.resolve_provider_client
    def _spy_rpc(*a, **k):
        _touch(_C)
        return _real_rpc(*a, **k)
    _ac.resolve_provider_client = _spy_rpc
except Exception:
    pass
'''


def _pki_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pki"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return d


def _run_cli(tmp_path: Path, db_path: Path, task_id: str, *, quiet_Q: bool = False):
    """Run the REAL shipped worker CLI for a managed kanban task; return a result dict."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    client_sentinel = tmp_path / "client_constructed.sentinel"
    net_sentinel = tmp_path / "network_attempted.sentinel"
    pki = _pki_dir(tmp_path)

    env = dict(os.environ)
    env.pop("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", None)
    env.pop("YOUTAB_MANAGED_BINDING_LEGACY_CREATED_BEFORE", None)
    # ── Hermetic environment (Batch2 #F2 CI-portability fix) ──────────────────
    # This subprocess must behave IDENTICALLY on a developer machine and on a
    # clean CI runner. It previously inherited the developer's ambient provider
    # credentials/config, so ``main()``'s first-run guard (_has_any_provider_
    # configured) passed locally but on clean CI printed setup guidance to STDOUT
    # and exited 1 BEFORE the managed pre-admission path — an opaque rc=1 that
    # made every scenario a false pass locally. Strip ALL inherited provider
    # configuration so the outcome never depends on the host, then explicitly
    # supply one SAFE, NON-CLOUD provider condition (an isolated loopback base
    # URL — OPENAI_BASE_URL alone satisfies the guard for local models) that is
    # sufficient to pass the first-run guard WITHOUT real credentials. Refusal
    # scenarios still exit at pre-admission BEFORE any client/socket/budget, so
    # no network or model call is ever made; the valid scenario only needs
    # provider-client CONSTRUCTION (the sentinel), not a live model.
    for _leak in list(env):
        if (
            _leak.endswith(("_API_KEY", "_TOKEN", "_BASE_URL", "_API_BASE"))
            or _leak in {"OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "YOUTAB_PORTAL_TOKEN"}
        ):
            env.pop(_leak, None)
    env["YOUTAB_RUNTIME_TRUST_MODE"] = "managed"
    env["YOUTAB_BRAIN_PUBLIC_KEYS"] = _KEYS_JSON
    env["YOUTAB_AGENT_KANBAN_DB"] = str(db_path)
    env["YOUTAB_AGENT_KANBAN_TASK"] = task_id
    env["YOUTAB_AGENT_HOME"] = str(home)
    # Isolated loopback provider: passes the first-run guard deterministically on
    # any host; port 9 (discard) is not served, so should a run ever reach an
    # actual dial it fails closed rather than contacting a real endpoint. A
    # non-secret dummy key (deliberately NOT ``sk-`` shaped, so no secret scanner
    # false-positive) lets an OpenAI-compatible client construct if the resolved
    # profile is OpenAI-shaped.
    env["OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"
    env["OPENAI_API_KEY"] = "e2e-local-not-a-real-key"
    env["E2E_CLIENT_SENTINEL"] = str(client_sentinel)
    env["E2E_NET_SENTINEL"] = str(net_sentinel)
    # sitecustomize dir FIRST so it is imported at interpreter startup, then the repo.
    env["PYTHONPATH"] = str(pki) + os.pathsep + str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    cmd = [sys.executable, "-m", "youtab_agent_cli.main", "-p", "default",
           "chat", "-q", f"work kanban task {task_id}"]
    if quiet_Q:
        cmd.append("-Q")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    budget_db = home / "runtime" / "execution_tree_budget.db"
    return {
        "rc": proc.returncode,
        "stderr": proc.stderr or "",
        "stdout": proc.stdout or "",
        "client_constructed": client_sentinel.exists(),
        "network_attempted": net_sentinel.exists(),
        "budget_opened": budget_db.exists(),
    }


def _diag(res) -> str:
    """Render BOTH streams + the sentinel/rc state, so a failure can never be an
    opaque rc=1 again (Batch2 #F2)."""
    return (
        f"\nrc={res['rc']} client_constructed={res['client_constructed']} "
        f"network_attempted={res['network_attempted']} budget_opened={res['budget_opened']}"
        f"\n--- STDERR (last 1500) ---\n{res['stderr'][-1500:]}"
        f"\n--- STDOUT (last 1500) ---\n{res['stdout'][-1500:]}"
    )


def _assert_refused_before_construction(res, *, error_substr):
    """A pre-admission refusal: exit 3, the exact refusal reason, and PROOF nothing
    was constructed/dialed/budgeted (preadmit ran before _init_agent)."""
    assert res["rc"] == 3, _diag(res)
    assert "managed_admission_failed" in res["stderr"], _diag(res)
    assert error_substr in res["stderr"], _diag(res)
    assert not res["client_constructed"], "provider client was constructed on refusal" + _diag(res)
    assert not res["network_attempted"], "a socket was opened on refusal" + _diag(res)
    assert not res["budget_opened"], "execution-tree budget was opened on refusal" + _diag(res)


def _mint(nonce, *, now=None, signer=None, **over):
    now = now or datetime.now(UTC).replace(microsecond=0)
    kw = dict(nonce=nonce, now=now, **over)
    if signer is not None:
        kw["signer"] = signer
    return _grant_header(**kw)


def _persist_binding(db_path: Path, task_id: str, binding: dict) -> None:
    conn = kb.connect(db_path=db_path)
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, eb.BINDING_EVENT, binding)
    finally:
        conn.close()


# ── refusals: BEFORE _init_agent, zero client / zero network / zero budget ────

def test_cli_missing_binding_refused_before_init_agent(tmp_path):
    task_id = "task-cli-missing"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-missing-0123456789abcd")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=False)
    _assert_refused_before_construction(
        _run_cli(tmp_path, db, task_id), error_substr="has no effective binding")


def test_cli_tampered_binding_refused_before_init_agent(tmp_path):
    task_id = "task-cli-tampered"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-tampered-0123456789abc")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=False)
    binding = eb.build_effective_binding(
        provider="ollama", model="qwen:test", endpoint="http://127.0.0.1:11434",
        run_id=task_id, root_run_id=task_id, tenant=env.tenant_id,
        workspace=env.workspace_id)
    binding["model"] = "evil-swap"  # tamper after hashing -> stale hash
    _persist_binding(db, task_id, binding)
    _assert_refused_before_construction(
        _run_cli(tmp_path, db, task_id), error_substr="hash mismatch")


def test_cli_forged_grant_refused_before_init_agent(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    task_id = "task-cli-forged"
    db = tmp_path / "kanban.db"
    forged = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    env, header = _mint("grant-cli-forged-01234567890a", signer=forged)
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted)
    _assert_refused_before_construction(
        _run_cli(tmp_path, db, task_id), error_substr="grant re-admission failed")


def test_cli_expired_grant_refused_before_init_agent(tmp_path):
    task_id = "task-cli-expired"
    db = tmp_path / "kanban.db"
    past = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    env, header = _mint("grant-cli-expired-0123456789a", now=past)
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted)
    _assert_refused_before_construction(
        _run_cli(tmp_path, db, task_id), error_substr="grant re-admission failed")


def test_cli_cross_run_binding_refused_before_init_agent(tmp_path):
    task_id = "task-cli-crossrun"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-crossrun-0123456789")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 binding_run_id="task-SOME-OTHER-RUN")
    _assert_refused_before_construction(
        _run_cli(tmp_path, db, task_id), error_substr="cross-run")


def test_cli_cross_workspace_binding_refused_before_init_agent(tmp_path):
    task_id = "task-cli-crossws"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-crossws-01234567890")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 binding_workspace="workspace-beta")
    _assert_refused_before_construction(
        _run_cli(tmp_path, db, task_id), error_substr="cross-workspace")


def test_cli_cross_tenant_binding_refused_before_init_agent(tmp_path):
    task_id = "task-cli-crosstenant"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-crosstenant-0123")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=False)
    binding = eb.build_effective_binding(
        provider="ollama", model="qwen:test", endpoint="http://127.0.0.1:11434",
        run_id=task_id, root_run_id=task_id, tenant="tenant-OTHER",
        workspace=env.workspace_id)
    _persist_binding(db, task_id, binding)
    _assert_refused_before_construction(
        _run_cli(tmp_path, db, task_id), error_substr="tenant mismatch")


def test_cli_Q_quiet_path_missing_binding_refused_before_init_agent(tmp_path):
    # The goal-mode / machine-readable -Q path in main() must gate identically.
    task_id = "task-cli-Q-missing"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-Q-missing-01234567")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=False)
    _assert_refused_before_construction(
        _run_cli(tmp_path, db, task_id, quiet_Q=True),
        error_substr="has no effective binding")


# ── valid binding: preadmit does NOT block; construction is reached ───────────

def test_cli_valid_binding_passes_preadmission_and_reaches_construction(tmp_path):
    # A VALID managed binding must NOT be refused by the pre-admission gate: the run
    # proceeds PAST preadmit into _init_agent (provider-client construction reached ->
    # CLIENT sentinel present) and there is NO pre-admission refusal. (A full model
    # turn needs a live model + matching profile substrate, which the real HTTP+worker
    # E2E in test_managed_binding_e2e.py drives; here we prove the gate is not
    # over-blocking a valid run and that preadmit precedes construction.)
    task_id = "task-cli-valid"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-valid-012345678901")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=True)  # a valid, self-consistent binding for this run
    res = _run_cli(tmp_path, db, task_id)
    # Construction was reached -> preadmit admitted the valid binding and ran BEFORE
    # _init_agent (the sentinel only fires from provider-client resolution).
    assert res["client_constructed"], _diag(res)
    # It was NOT stopped by a PRE-admission refusal (a post-construction drift/model
    # failure may still occur — construction happening is the discriminator that the
    # gate did not block a valid run).
    if "managed_admission_failed" in res["stderr"]:
        # If admission failed, it must be a POST-construction reason (drift), never
        # the pre-admission "no effective binding" / grant refusal.
        assert "has no effective binding" not in res["stderr"]
        assert "grant re-admission failed" not in res["stderr"]
