"""WAVE-30D real worker-boundary regression for the Track A engine binding.

This is the end-to-end contract the live-canary defect (``t_2cf3c738``) broke:
a persisted local-server binding (``provider_override=ollama`` +
``model_override=<tag>``) must survive serialisation into the worker argv/env and
rehydrate — in a genuinely separate process — into a provider resolution that
dials the LOCAL endpoint, never the machine-default OpenRouter.

The defect is not mocked away: the resolution in step 4 runs the real
``resolve_runtime_provider`` in a real child process, on the exact environment
the dispatcher would hand the worker.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli._parser import build_top_level_parser

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TAG = "qwen-boundary-test:latest"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated YOUTAB_AGENT_HOME with an empty kanban DB (mirrors test_kanban_db)."""
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_local_binding_survives_worker_boundary(kanban_home, monkeypatch, tmp_path):
    # No cloud keys or stray base_url overrides may leak into the worker env, so
    # a wrong resolution can only come from the resolver, not the ambient env.
    for var in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OLLAMA_API_KEY",
        "CUSTOM_BASE_URL",
        "OPENROUTER_BASE_URL",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    # The protected local endpoint the runtime attested — propagated to the
    # worker via env (never via a process argument).
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11435")

    # --- 1. Persist the engine binding on a real task row (DB serialisation) ---
    with kb.connect() as conn:
        task_id, _created = kb.create_task_ex(
            conn,
            title="track A boundary",
            assignee="elias",
            created_by="test",
            provider_override="ollama",
            model_override=_TAG,
        )
        persisted = kb.get_task(conn, task_id)

    # The binding round-trips through the DB unchanged.
    assert persisted.provider_override == "ollama"
    assert persisted.model_override == _TAG

    # --- 2. Serialise into the worker argv + env (dispatcher spawn) ---
    monkeypatch.setattr(kb, "_resolve_youtab_argv", lambda: ["youtab"])
    captured: dict = {}
    _real_popen = subprocess.Popen

    class FakeProc:
        pid = 4321

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    kb._default_spawn(persisted, str(workspace))
    # Restore the real Popen so the child process below actually launches
    # (subprocess.run uses subprocess.Popen internally).
    monkeypatch.setattr(subprocess, "Popen", _real_popen)

    cmd = captured["cmd"]
    # The persisted provider+model reach the worker argv as an adjacent pair.
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == _TAG
    assert "--provider" in cmd and cmd[cmd.index("--provider") + 1] == "ollama"
    # The endpoint travels as a protected env value — NEVER as a process arg.
    assert captured["env"].get("OLLAMA_BASE_URL") == "http://127.0.0.1:11435"
    assert not any("11435" in part for part in cmd), (
        "endpoint must not appear in argv (visible in ps): " + " ".join(cmd)
    )

    # --- 3. Rehydrate through the REAL CLI parser (worker startup) ---
    parser, _subparsers, _chat_parser = build_top_level_parser()
    assert cmd[1:3] == ["-p", "elias"]
    args = parser.parse_args(cmd[3:])
    assert args.command == "chat"
    assert args.provider == "ollama"
    assert args.model == _TAG

    # --- 4. Resolve provider in a GENUINELY SEPARATE process, on the captured
    #        env, running the real resolver (defect NOT mocked) ---
    child = (
        "import json, sys\n"
        "from youtab_agent_cli.runtime_provider import resolve_runtime_provider\n"
        "r = resolve_runtime_provider(requested=sys.argv[1], target_model=sys.argv[2])\n"
        "print(json.dumps({'provider': r.get('provider'), "
        "'base_url': r.get('base_url'), 'api_key': r.get('api_key')}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child, args.provider, args.model],
        env=captured["env"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"child failed: {proc.stderr}"
    # Last stdout line is the JSON (config loaders may log above it).
    payload = json.loads(proc.stdout.strip().splitlines()[-1])

    # The worker resolves the LOCAL Ollama endpoint, not OpenRouter.
    assert payload["base_url"] == "http://127.0.0.1:11435/v1"
    assert "openrouter.ai" not in (payload["base_url"] or "")
    assert payload["provider"] == "custom"
    # No OpenRouter API key is requested — the keyless local placeholder is used.
    assert payload["api_key"] == "no-key-required"


def test_provider_override_without_model_is_refused(kanban_home):
    """A conflicting/partial binding (provider pinned but no model) must fail
    closed at persistence — the worker can never be launched half-bound."""
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="provider_override requires a model_override"):
            kb.create_task_ex(
                conn,
                title="half bound",
                assignee="elias",
                created_by="test",
                provider_override="ollama",
                model_override=None,
            )


def test_public_endpoint_binding_fails_closed_across_boundary(kanban_home, monkeypatch, tmp_path):
    """An unauthorised (public) endpoint must fail closed at the worker — an
    empty base_url — rather than silently resolving OpenRouter."""
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "CUSTOM_BASE_URL", "OPENROUTER_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://8.8.8.8:11434")

    with kb.connect() as conn:
        task_id, _ = kb.create_task_ex(
            conn,
            title="public endpoint",
            assignee="elias",
            created_by="test",
            provider_override="ollama",
            model_override=_TAG,
        )
        persisted = kb.get_task(conn, task_id)

    monkeypatch.setattr(kb, "_resolve_youtab_argv", lambda: ["youtab"])
    captured: dict = {}
    _real_popen = subprocess.Popen

    class FakeProc:
        pid = 4322

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda cmd, *a, **k: (captured.update(cmd=list(cmd), env=dict(k.get("env") or {})) or FakeProc()),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    kb._default_spawn(persisted, str(workspace))
    monkeypatch.setattr(subprocess, "Popen", _real_popen)

    child = (
        "import json, sys\n"
        "from youtab_agent_cli.runtime_provider import resolve_runtime_provider\n"
        "r = resolve_runtime_provider(requested='ollama', target_model=sys.argv[1])\n"
        "print(json.dumps({'provider': r.get('provider'), 'base_url': r.get('base_url')}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child, _TAG],
        env=captured["env"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"child failed: {proc.stderr}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["base_url"] == ""  # fail closed
    assert "openrouter.ai" not in (payload["base_url"] or "")
