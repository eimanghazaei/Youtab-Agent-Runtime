"""Artifact installs never enter the legacy Git/ZIP update pipeline."""

import json
from types import SimpleNamespace

import pytest

from youtab_agent_cli import update_cmd


def _setup(tmp_path, monkeypatch, marker):
    root = tmp_path / "runtime"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    (home / "youtab-setup.exe").write_bytes(b"setup")
    if marker is not None:
        (root / ".youtab-agent-runtime-bootstrap-complete").write_text(marker)
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(
        PROJECT_ROOT=root, _is_windows=lambda: True,
    ))
    monkeypatch.setattr(update_cmd, "get_youtab_home", lambda: home)
    return root, home


def test_artifact_update_hands_off_to_existing_setup(tmp_path, monkeypatch):
    _, home = _setup(tmp_path, monkeypatch, json.dumps({
        "releaseBaseUrl": "https://api.youtab.io/pilot-runtime-" + "a" * 32 + "/releases",
        "releaseSequence": 3,
    }))
    calls = []
    monkeypatch.setattr(update_cmd.subprocess, "Popen", lambda args, **kwargs: calls.append(args))
    update_cmd._cmd_update_impl(SimpleNamespace(yes=True), False)
    assert calls == [[str(home / "youtab-setup.exe"), "--update"]]


@pytest.mark.parametrize("marker", ["not json", "{}", None])
def test_customer_update_refuses_missing_or_tampered_identity(tmp_path, monkeypatch, marker):
    _setup(tmp_path, monkeypatch, marker)
    monkeypatch.setattr(update_cmd.subprocess, "Popen", lambda *_a, **_k: pytest.fail("Setup launched"))
    with pytest.raises(SystemExit) as exc:
        update_cmd._cmd_update_impl(SimpleNamespace(yes=True), False)
    assert exc.value.code == 2


def test_legacy_no_git_without_staged_setup_keeps_existing_check_behavior(tmp_path, monkeypatch):
    root, home = _setup(tmp_path, monkeypatch, None)
    (home / "youtab-setup.exe").unlink()
    with pytest.raises(SystemExit) as exc:
        update_cmd._cmd_update_check()
    assert exc.value.code == 1  # existing "not a git repository" result
    assert not (root / ".git").exists()
