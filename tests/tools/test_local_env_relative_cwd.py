"""Regression tests for local terminal initial cwd normalization."""

from pathlib import Path

from tools.environments.local import LocalEnvironment, _resolve_local_initial_cwd
from tests import _wincompat


def test_relative_initial_cwd_resolves_from_parent(tmp_path, monkeypatch):
    project = tmp_path / "youtab-agent-runtime"
    project.mkdir()
    monkeypatch.chdir(tmp_path)

    assert _resolve_local_initial_cwd("youtab-agent-runtime") == str(project)


@_wincompat.requires_posix
def test_local_environment_keeps_existing_relative_child_cwd(tmp_path, monkeypatch):
    project = tmp_path / "youtab-agent-runtime"
    project.mkdir()
    monkeypatch.chdir(tmp_path)

    env = LocalEnvironment(cwd="youtab-agent-runtime", timeout=5)
    try:
        result = env.execute("pwd", timeout=5)
    finally:
        env.cleanup()

    assert result["returncode"] == 0
    assert result["output"].strip() == str(project)
