"""Behavioural tests for the Youtab immutable-runtime download policy."""

from argparse import Namespace

import pytest

from hermes_cli import main
from hermes_cli.youtab_runtime_policy import runtime_artifact_downloads_denied
from tools import lazy_deps


def test_policy_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YOUTAB_RUNTIME_DOWNLOAD_POLICY", raising=False)
    assert runtime_artifact_downloads_denied() is False


def test_policy_overrides_durable_lazy_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("YOUTAB_RUNTIME_DOWNLOAD_POLICY", "deny")
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setenv("HERMES_LAZY_INSTALL_TARGET", str(tmp_path / "lazy"))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"security": {"allow_lazy_installs": True}},
    )

    assert lazy_deps._allow_lazy_installs() is False


def test_install_specs_never_invokes_installer_when_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOUTAB_RUNTIME_DOWNLOAD_POLICY", "deny")
    monkeypatch.setattr(
        lazy_deps,
        "_venv_pip_install",
        lambda *_args, **_kwargs: pytest.fail("installer must not run"),
    )

    result = lazy_deps.install_specs(["example-package==1.0.0"])

    assert result.ok is False
    assert result.blocked is True
    assert "Youtab policy" in result.reason


def test_self_update_is_blocked_before_network_or_mutation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("YOUTAB_RUNTIME_DOWNLOAD_POLICY", "deny")
    monkeypatch.setattr(
        "hermes_cli.config.is_managed",
        lambda: pytest.fail("update path must stop before install detection"),
    )

    main.cmd_update(Namespace(check=False, gateway=False))

    output = capsys.readouterr().out
    assert "Youtab managed runtime" in output
    assert "digest-pinned Youtab runtime image" in output
