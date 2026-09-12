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
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
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

# Socket egress sentinel — record then call through (never suppress). Also append
# each destination so a test can PROVE only loopback was dialed (no cloud endpoint).
_D = os.environ.get("E2E_NET_DEST")
_real_connect = socket.socket.connect
def _spy_connect(self, address, *a, **k):
    _touch(_N)
    try:
        if _D:
            with open(_D, "a") as _f:
                _f.write(repr(address) + "\n")
    except Exception:
        pass
    return _real_connect(self, address, *a, **k)
socket.socket.connect = _spy_connect

# Pre-admission sentinel — the single verified grant/binding load (Batch4 #1).
_P = os.environ.get("E2E_PREADMIT_SENTINEL")
try:
    import youtab_agent_cli.worker_admission as _wa
    _real_lvp = _wa.load_verified_preadmission
    def _spy_lvp(*a, **k):
        _touch(_P)
        return _real_lvp(*a, **k)
    _wa.load_verified_preadmission = _spy_lvp
    # Final managed-admission sentinel.
    _A = os.environ.get("E2E_ADMIT_SENTINEL")
    _real_ema = _wa.establish_managed_admission
    def _spy_ema(*a, **k):
        _touch(_A)
        return _real_ema(*a, **k)
    _wa.establish_managed_admission = _spy_ema
except Exception:
    pass

# Pre-credential provider-gate sentinel (Batch5 #2) — proves the concrete-provider /
# no-drift gate ran BEFORE credential resolution.
_RP = os.environ.get("E2E_ROUTEPLAN_SENTINEL")
try:
    import youtab_agent_cli.worker_admission as _wa2
    _real_abpa = _wa2.assert_bound_provider_admissible
    def _spy_abpa(*a, **k):
        _touch(_RP)
        return _real_abpa(*a, **k)
    _wa2.assert_bound_provider_admissible = _spy_abpa
except Exception:
    pass

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

# Credential-resolution COUNTER (Batch5 #3) — the UMBRELLA over all credential I/O:
# Vertex OAuth token mint AND Youtab key refresh both happen INSIDE
# resolve_runtime_provider, so if it never runs neither can. It is a COUNTER (one
# line appended per call) so a test can assert EXACTLY ONE resolution on a successful
# managed turn (no second _init_agent resolution) and ZERO on a pre-credential refusal.
# _ensure_runtime_credentials imports this name at call time, so patching the module
# attribute is picked up.
_R = os.environ.get("E2E_CREDRESOLVE_SENTINEL")
try:
    import youtab_agent_cli.runtime_provider as _rp
    _real_rrp = _rp.resolve_runtime_provider
    def _spy_rrp(*a, **k):
        try:
            if _R:
                with open(_R, "a") as _f:
                    _f.write("1\n")
        except Exception:
            pass
        return _real_rrp(*a, **k)
    _rp.resolve_runtime_provider = _spy_rrp
except Exception:
    pass

# Vertex OAuth token-mint sentinel.
_V = os.environ.get("E2E_VERTEX_SENTINEL")
try:
    import agent.vertex_adapter as _va
    _real_gvc = _va.get_vertex_config
    def _spy_gvc(*a, **k):
        _touch(_V)
        return _real_gvc(*a, **k)
    _va.get_vertex_config = _spy_gvc
except Exception:
    pass

# Youtab credential-refresh sentinel (pool.try_refresh_current on the pool class).
_Y = os.environ.get("E2E_YOUTAB_SENTINEL")
try:
    import agent.credential_pool as _cp
    for _nm in dir(_cp):
        _o = getattr(_cp, _nm)
        if isinstance(_o, type) and "try_refresh_current" in getattr(_o, "__dict__", {}):
            _real_trc = _o.try_refresh_current
            def _spy_trc(self, *a, _r=_real_trc, **k):
                _touch(_Y)
                return _r(self, *a, **k)
            _o.try_refresh_current = _spy_trc
except Exception:
    pass
'''


def _pki_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pki"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return d


def _run_cli(tmp_path: Path, db_path: Path, task_id: str, *, quiet_Q: bool = False,
             extra_env: "dict | None" = None, extra_argv: "list | None" = None):
    """Run the REAL shipped worker CLI for a managed kanban task; return a result dict."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    client_sentinel = tmp_path / "client_constructed.sentinel"
    net_sentinel = tmp_path / "network_attempted.sentinel"
    net_dest = tmp_path / "net_destinations.log"
    credresolve_sentinel = tmp_path / "credresolve.sentinel"
    vertex_sentinel = tmp_path / "vertex_minted.sentinel"
    youtab_sentinel = tmp_path / "youtab_refreshed.sentinel"
    preadmit_sentinel = tmp_path / "preadmit.sentinel"
    routeplan_sentinel = tmp_path / "routeplan.sentinel"
    admit_sentinel = tmp_path / "admit.sentinel"
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
    env["E2E_NET_DEST"] = str(net_dest)
    env["E2E_CREDRESOLVE_SENTINEL"] = str(credresolve_sentinel)
    env["E2E_VERTEX_SENTINEL"] = str(vertex_sentinel)
    env["E2E_YOUTAB_SENTINEL"] = str(youtab_sentinel)
    env["E2E_PREADMIT_SENTINEL"] = str(preadmit_sentinel)
    env["E2E_ROUTEPLAN_SENTINEL"] = str(routeplan_sentinel)
    env["E2E_ADMIT_SENTINEL"] = str(admit_sentinel)
    # sitecustomize dir FIRST so it is imported at interpreter startup, then the repo.
    env["PYTHONPATH"] = str(pki) + os.pathsep + str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Per-scenario overrides (applied LAST so a test can set a provider/endpoint the
    # blanket strip above would otherwise have removed, e.g. OLLAMA_BASE_URL).
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})

    cmd = [sys.executable, "-m", "youtab_agent_cli.main", "-p", "default"]
    if extra_argv:
        cmd += list(extra_argv)
    cmd += ["chat", "-q", f"work kanban task {task_id}"]
    if quiet_Q:
        cmd.append("-Q")
    # Decode the child's streams as UTF-8 with replacement so a non-cp1252 byte in
    # the worker's output can never raise in the Windows subprocess reader thread
    # (the banner/status lines carry unicode); we only assert on ASCII substrings.
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    budget_db = home / "runtime" / "execution_tree_budget.db"
    dests = net_dest.read_text().splitlines() if net_dest.exists() else []
    credresolve_count = (
        len([ln for ln in credresolve_sentinel.read_text().splitlines() if ln.strip()])
        if credresolve_sentinel.exists() else 0
    )
    return {
        "rc": proc.returncode,
        "stderr": proc.stderr or "",
        "stdout": proc.stdout or "",
        "client_constructed": client_sentinel.exists(),
        "network_attempted": net_sentinel.exists(),
        "net_destinations": dests,
        "budget_opened": budget_db.exists(),
        "credential_resolved": credresolve_count > 0,
        "credresolve_count": credresolve_count,
        "vertex_minted": vertex_sentinel.exists(),
        "youtab_refreshed": youtab_sentinel.exists(),
        "preadmitted": preadmit_sentinel.exists(),
        "route_planned": routeplan_sentinel.exists(),
        "admitted": admit_sentinel.exists(),
    }


def _diag(res) -> str:
    """Render BOTH streams + the sentinel/rc state, so a failure can never be an
    opaque rc=1 again (Batch2 #F2)."""
    return (
        f"\nrc={res['rc']} preadmitted={res['preadmitted']} provider_gate={res['route_planned']} "
        f"client_constructed={res['client_constructed']} admitted={res['admitted']} "
        f"network_attempted={res['network_attempted']} budget_opened={res['budget_opened']} "
        f"credential_resolved={res['credential_resolved']} credresolve_count={res['credresolve_count']} "
        f"vertex_minted={res['vertex_minted']} youtab_refreshed={res['youtab_refreshed']} "
        f"net_destinations={res['net_destinations']}"
        f"\n--- STDERR (last 1500) ---\n{res['stderr'][-1500:]}"
        f"\n--- STDOUT (last 1500) ---\n{res['stdout'][-1500:]}"
    )


def _assert_refused_before_construction(res, *, error_substr):
    """A grant/binding pre-admission refusal: exit 3, the exact refusal reason, and
    PROOF nothing was resolved/constructed/dialed/budgeted. Batch3 #1: because
    preadmit now runs BEFORE _ensure_runtime_credentials(), an invalid grant/binding
    is refused with ZERO credential resolution — so the Vertex token-mint and Youtab
    key-refresh sentinels (both reachable only INSIDE resolve_runtime_provider) are
    likewise ABSENT."""
    assert res["rc"] == 3, _diag(res)
    assert "managed_admission_failed" in res["stderr"], _diag(res)
    assert error_substr in res["stderr"], _diag(res)
    assert not res["credential_resolved"], "credentials were resolved on an invalid-grant refusal" + _diag(res)
    assert not res["vertex_minted"], "a Vertex token was minted on refusal" + _diag(res)
    assert not res["youtab_refreshed"], "a Youtab key was refreshed on refusal" + _diag(res)
    assert not res["client_constructed"], "provider client was constructed on refusal" + _diag(res)
    assert not res["network_attempted"], "a socket was opened on refusal" + _diag(res)
    assert not res["budget_opened"], "execution-tree budget was opened on refusal" + _diag(res)
    assert not res["admitted"], "final managed admission ran on refusal" + _diag(res)


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


# ── unpinned auto + drifting route: refused BEFORE credential resolution ──────

def test_cli_unpinned_auto_refused_before_credential_resolution(tmp_path):
    # Batch5 #2: a VALID, self-consistent managed binding (bound to ollama) whose
    # CONFIGURED route is unpinned (the default ``provider: auto``) must be refused by
    # the PRE-CREDENTIAL provider gate — a managed run must be pinned to a concrete
    # provider so credential resolution can never select/mint an unbound provider.
    # NO credential is resolved (credresolve_count == 0).
    task_id = "task-cli-auto-unpinned"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-autounpin-01234")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=True)  # valid, self-consistent ollama binding for this run
    res = _run_cli(tmp_path, db, task_id)  # no --provider -> resolves to "auto"
    assert res["rc"] == 3, _diag(res)
    assert "managed_admission_failed" in res["stderr"], _diag(res)
    assert "before credential resolution" in res["stderr"], _diag(res)
    # Either the unresolved-requested or the drift branch (both are pre-credential).
    assert ("requested provider is unresolved" in res["stderr"]
            or "drifted from the bound identity" in res["stderr"]), _diag(res)
    assert "has no effective binding" not in res["stderr"], _diag(res)
    assert "grant re-admission failed" not in res["stderr"], _diag(res)
    # Ordering proof: pre-admission + the pre-credential provider gate ran, but ZERO
    # credential resolution / client / network / budget / final admission.
    assert res["preadmitted"], "pre-admission did not run" + _diag(res)
    assert res["route_planned"], "pre-credential provider gate did not run" + _diag(res)
    assert res["credresolve_count"] == 0, (
        "DEFECT: credentials were resolved before the pre-credential refusal" + _diag(res)
    )
    assert not res["vertex_minted"] and not res["youtab_refreshed"], _diag(res)
    assert not res["client_constructed"], "a client was constructed" + _diag(res)
    assert not res["network_attempted"], "a socket was opened before refusal" + _diag(res)
    assert not res["budget_opened"], "execution-tree budget was opened before refusal" + _diag(res)
    assert not res["admitted"], "final admission ran on a pre-credential refusal" + _diag(res)


# ── adversarial: drifting CLOUD primary must not mint/refresh before refusal ──

def test_cli_ollama_bound_but_vertex_primary_refused_without_mint(tmp_path):
    # Codex C.1: an Ollama-bound run misconfigured with a drifting VERTEX primary is
    # refused before credential resolution. The Vertex OAuth mint (get_vertex_config)
    # is NEVER called, resolve_runtime_provider is NEVER called, and there is zero
    # socket/client/budget activity.
    task_id = "task-cli-vertex-drift"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-vertexdrift-0123")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=True)  # valid ollama binding
    res = _run_cli(tmp_path, db, task_id, extra_argv=["--provider", "vertex"])
    assert res["rc"] == 3, _diag(res)
    assert "drifted from the bound identity" in res["stderr"], _diag(res)
    assert "vertex" in res["stderr"], _diag(res)
    assert res["preadmitted"] and res["route_planned"], _diag(res)
    assert not res["credential_resolved"], "resolve_runtime_provider ran on a vertex drift" + _diag(res)
    assert not res["vertex_minted"], "a Vertex OAuth token was minted on a drift refusal" + _diag(res)
    assert not res["client_constructed"] and not res["network_attempted"], _diag(res)
    assert not res["budget_opened"] and not res["admitted"], _diag(res)


def test_cli_ollama_bound_but_youtab_primary_refused_without_refresh(tmp_path):
    # Codex C.2: an Ollama-bound run misconfigured with a drifting YOUTAB primary is
    # refused before credential resolution. The Youtab key refresh
    # (pool.try_refresh_current) is NEVER called; zero socket/client/budget activity.
    task_id = "task-cli-youtab-drift"
    db = tmp_path / "kanban.db"
    env, header = _mint("grant-cli-youtabdrift-0123")
    persisted = cm.binding_to_persisted(_ingress_manifest(env))
    _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                 seed_binding=True)  # valid ollama binding
    res = _run_cli(tmp_path, db, task_id, extra_argv=["--provider", "youtab"])
    assert res["rc"] == 3, _diag(res)
    assert "drifted from the bound identity" in res["stderr"], _diag(res)
    assert "youtab" in res["stderr"], _diag(res)
    assert res["preadmitted"] and res["route_planned"], _diag(res)
    assert not res["credential_resolved"], "resolve_runtime_provider ran on a youtab drift" + _diag(res)
    assert not res["youtab_refreshed"], "a Youtab key was refreshed on a drift refusal" + _diag(res)
    assert not res["client_constructed"] and not res["network_attempted"], _diag(res)
    assert not res["budget_opened"] and not res["admitted"], _diag(res)


# ── positive: a VALID managed run whose route EXACTLY matches its binding ─────

class _FakeInferenceHandler(BaseHTTPRequestHandler):
    """A hermetic, loopback-only OpenAI/Ollama-compatible responder. No cloud."""

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": "qwen:test"}]})
        elif "tags" in self.path:
            self._send(200, {"models": [{"name": "qwen:test"}]})
        else:
            self._send(200, {"status": "ok"})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        if ln:
            self.rfile.read(ln)
        self._send(200, {
            "id": "chatcmpl-e2e", "object": "chat.completion", "model": "qwen:test",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


def test_cli_canonical_ollama_engine_bound_run_proceeds_through_managed_path(tmp_path):
    # Codex B (Batch5): a TRUE successful shipped-CLI E2E using the CANONICAL
    # engine-bound Ollama identity emitted by runtime create — provider ``ollama``
    # (NOT a hand-invented ``custom`` binding), built with the SAME
    # ``eb.build_effective_binding(provider="ollama", ...)`` create uses. The worker
    # dispatches ollama through the ``custom`` transport, so this also proves local-
    # alias canonicalization: authoritative(custom, ollama) == ollama == bound.
    # Ordered path: preadmit (single load) -> pre-credential provider gate -> resolve
    # credentials EXACTLY ONCE -> compare ACTUAL resolved identity -> construct the
    # bound client -> final admission (same snapshot) -> proceed, contacting ONLY the
    # hermetic loopback server.
    server = HTTPServer(("127.0.0.1", 0), _FakeInferenceHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        task_id = "task-cli-ollama-canonical"
        db = tmp_path / "kanban.db"
        env, header = _mint("grant-cli-ollamacanon-01")
        persisted = cm.binding_to_persisted(_ingress_manifest(env))
        _persist_run(db, task_id, grant_header=header, manifest_payload=persisted,
                     seed_binding=False)
        # The worker's ollama inference endpoint is OLLAMA_BASE_URL (kept as-is when it
        # already ends /v1). Build the CANONICAL binding for this engine identity with
        # the SAME endpoint so the ACTUAL resolved route matches the binding exactly.
        endpoint = f"http://127.0.0.1:{port}/v1"
        binding = eb.build_effective_binding(
            provider="ollama", model="qwen:test", endpoint=endpoint,
            run_id=task_id, root_run_id=task_id, tenant=env.tenant_id,
            workspace=env.workspace_id)
        _persist_binding(db, task_id, binding)
        res = _run_cli(
            tmp_path, db, task_id,
            # The protected ollama deployment endpoint (re-added after the blanket
            # strip); the shipped worker resolves inference from it.
            extra_env={"OLLAMA_BASE_URL": endpoint},
            # Pin the provider explicitly (config default ``provider: auto`` would be
            # refused as unpinned) and the model.
            extra_argv=["--provider", "ollama", "-m", "qwen:test"],
        )
        # Ordered proof of the valid managed path.
        assert res["preadmitted"], "grant/binding pre-admission did not run" + _diag(res)
        assert res["route_planned"], "pre-credential provider gate did not run" + _diag(res)
        assert res["client_constructed"], "the bound provider client was not constructed" + _diag(res)
        assert res["admitted"], "final managed admission did not run" + _diag(res)
        assert res["rc"] == 0, _diag(res)
        assert "managed_admission_failed" not in res["stderr"], _diag(res)
        # Blocker 3: credentials were resolved EXACTLY ONCE (no second _init_agent
        # resolution / TOCTOU).
        assert res["credresolve_count"] == 1, (
            f"expected exactly one credential resolution, got {res['credresolve_count']}"
            + _diag(res)
        )
        # No drift to an unauthorized cloud provider.
        assert not res["vertex_minted"], "a Vertex token was minted on the valid path" + _diag(res)
        assert not res["youtab_refreshed"], "a Youtab key was refreshed on the valid path" + _diag(res)
        # No cloud: every socket destination dialed was loopback.
        assert res["net_destinations"], "expected at least the loopback inference dial" + _diag(res)
        for dest in res["net_destinations"]:
            assert "127.0.0.1" in dest or "localhost" in dest or "::1" in dest, (
                f"a non-loopback endpoint was contacted: {dest}" + _diag(res)
            )
    finally:
        server.shutdown()
        server.server_close()
