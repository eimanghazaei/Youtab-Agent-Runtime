"""Install guidance can run without importing configuration or update machinery."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from youtab_agent_cli import install_method


def test_guidance_import_has_no_configuration_dependency(tmp_path):
    script = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {"youtab_agent_cli.config", "youtab_agent_cli.update_cmd", "youtab_agent_cli.main"}:
        raise AssertionError("guidance loaded execution/configuration machinery: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from youtab_agent_cli.install_method import recommended_update_command_for_method, format_docker_update_message
assert recommended_update_command_for_method("git") == "youtab update"
assert "docker pull" in format_docker_update_message()
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("method,fragment", [
    ("git", "youtab update"), ("docker", "docker pull"), ("nixos", "nixos-rebuild"),
])
def test_code_scoped_guidance_uses_actual_install_stamp(tmp_path, monkeypatch, method, fragment):
    monkeypatch.delenv("YOUTAB_AGENT_MANAGED", raising=False)
    install_method.stamp_install_method(method, tmp_path)
    monkeypatch.setattr(install_method, "get_project_root", lambda: tmp_path)
    assert install_method.detect_install_method(tmp_path) == method
    assert fragment in install_method.recommended_update_command()


def test_config_compatibility_keeps_patchable_dependency_seams(tmp_path, monkeypatch):
    from youtab_agent_cli import config

    monkeypatch.setattr(config, "get_managed_update_command", lambda: "synthetic managed upgrade")
    assert config.recommended_update_command() == "synthetic managed upgrade"
    monkeypatch.setattr(config, "get_managed_update_command", lambda: None)
    monkeypatch.setattr(config, "detect_install_method", lambda root: "docker")
    assert config.recommended_update_command() == "docker pull youtab/youtab-agent-runtime:latest"
    monkeypatch.setattr(config, "get_managed_system", lambda: "NixOS")
    assert "nixos-rebuild" in config.format_managed_message()
    assert config.format_docker_update_message is install_method.format_docker_update_message


@pytest.mark.parametrize("method,fragment", [("docker", "docker pull"), ("nix", "flake input")])
def test_update_check_uses_canonical_guidance_without_git(tmp_path, monkeypatch, capsys, method, fragment):
    from youtab_agent_cli import update_cmd

    root = tmp_path / "runtime"
    root.mkdir()
    install_method.stamp_install_method(method, root)
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(PROJECT_ROOT=root))
    monkeypatch.setattr(update_cmd.subprocess, "run", lambda *args, **kwargs: pytest.fail("git executed"))
    with pytest.raises(SystemExit) as exc:
        update_cmd._cmd_update_check()
    assert exc.value.code == 1
    assert fragment in capsys.readouterr().out
