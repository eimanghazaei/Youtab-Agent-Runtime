"""Behavior contracts for inference reads without login orchestration."""
import json
import os
import subprocess
import sys

from youtab_agent_cli import auth, profile_inference


def test_profile_reader_shares_reentrant_auth_transaction(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({
        "providers": {"youtab_inference": {"agent_key": ""}},
    }), encoding="utf-8")
    with auth._auth_store_lock():
        holder = auth._auth_lock_holder_for(tmp_path / "auth.json")
        assert holder.depth == 1
        assert profile_inference.get_local_inference_token_state() == {"agent_key": ""}
        assert holder.depth == 1
    assert holder.depth == 0


def test_inference_policy_import_does_not_load_login_or_model_ui(tmp_path):
    env = {**os.environ, "YOUTAB_AGENT_HOME": str(tmp_path)}
    result = subprocess.run([
        sys.executable, "-c",
        "import sys; from youtab_agent_cli import profile_inference; "
        "assert 'youtab_agent_cli.auth' not in sys.modules; "
        "assert 'youtab_agent_cli.models' not in sys.modules; "
        "assert 'youtab_agent_cli.config' not in sys.modules; "
        "assert profile_inference.get_local_inference_token_state() is None",
    ], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
