"""Tests for gateway linger auto-enable behavior on headless Linux installs."""

from types import SimpleNamespace

import pytest

import youtab_agent_cli.gateway as gateway


@pytest.fixture
def posix_systemd_host(monkeypatch):
    """Model the POSIX host a `systemctl --user` install runs on.

    ``_systemctl_cmd`` calls ``_ensure_user_systemd_env``, which starts with
    ``os.getuid()``. That attribute does not exist on Windows, so a developer
    running this suite there got ``AttributeError: module 'os' has no attribute
    'getuid'`` from inside the product before any assertion was reached -- a
    failure that says nothing about the linger behaviour under test.

    The platform is modelled rather than skipped, so the systemd install path
    is exercised on every host and the argv contract below is checked
    everywhere. Both env vars are pre-set because that is the branch a real
    logged-in systemd session takes: with them present the helper returns
    without probing ``/run/user/<uid>`` or mutating ``os.environ``, which is
    also what makes this deterministic on a Linux CI runner where that
    directory may genuinely exist.
    """
    monkeypatch.setattr(gateway.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")


class TestEnsureLingerEnabled:
    def test_linger_already_enabled_via_file(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr("getpass.getuser", lambda: "testuser")
        monkeypatch.setattr(gateway, "Path", lambda _path: SimpleNamespace(exists=lambda: True))

        calls = []
        monkeypatch.setattr(gateway.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

        gateway._ensure_linger_enabled()

        out = capsys.readouterr().out
        assert "Systemd linger is enabled" in out
        assert calls == []


    def test_loginctl_success_enables_linger(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr("getpass.getuser", lambda: "testuser")
        monkeypatch.setattr(gateway, "Path", lambda _path: SimpleNamespace(exists=lambda: False))
        monkeypatch.setattr(gateway, "get_systemd_linger_status", lambda: (False, ""))
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/loginctl")

        run_calls = []

        def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
            run_calls.append((cmd, capture_output, text, check))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway.subprocess, "run", fake_run)

        gateway._ensure_linger_enabled()

        out = capsys.readouterr().out
        assert "Enabling linger" in out
        assert "Linger enabled" in out
        assert run_calls == [(["loginctl", "enable-linger", "testuser"], True, True, False)]


    def test_loginctl_failure_shows_manual_guidance(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr("getpass.getuser", lambda: "testuser")
        monkeypatch.setattr(gateway, "Path", lambda _path: SimpleNamespace(exists=lambda: False))
        monkeypatch.setattr(gateway, "get_systemd_linger_status", lambda: (False, ""))
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/loginctl")
        monkeypatch.setattr(
            gateway.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="Permission denied"),
        )

        gateway._ensure_linger_enabled()

        out = capsys.readouterr().out
        assert "sudo loginctl enable-linger testuser" in out
        assert "Permission denied" in out


def test_systemd_install_calls_linger_helper(
    monkeypatch, tmp_path, capsys, posix_systemd_host
):
    unit_path = tmp_path / "systemd" / "user" / "youtab-gateway.service"

    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: unit_path)
    # Non-temp home so the temp-home write guard (which trips on the
    # hermetic test YOUTAB_AGENT_HOME) stays out of the way.
    monkeypatch.setattr(
        gateway,
        "generate_systemd_unit",
        lambda system=False, run_as_user=None: (
            '[Service]\nEnvironment="YOUTAB_AGENT_HOME=/home/alice/.youtab-agent-runtime"\n'
        ),
    )

    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append((cmd, check))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    helper_calls = []
    monkeypatch.setattr(gateway.subprocess, "run", fake_run)
    monkeypatch.setattr(gateway, "_ensure_linger_enabled", lambda: helper_calls.append(True))

    gateway.systemd_install(force=False)

    out = capsys.readouterr().out
    assert unit_path.exists()
    assert [cmd for cmd, _ in calls] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", gateway.get_service_name()],
    ]
    assert helper_calls == [True]
    assert "User service installed and enabled" in out
