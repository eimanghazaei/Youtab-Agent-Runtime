"""Provider-neutral ``<ENV>_FILE`` credential support (WAVE-30B §4).

Proves that every credential-bearing provider gains file-based secret delivery
through the two key-read chokepoints — ``config.get_env_value_prefer_dotenv``
(registry providers) and ``runtime_provider._getenv`` (custom providers) — keyed
on the env-var NAME, with no provider-specific code; that a dual source fails
closed; that plaintext-env use warns; and that live-benchmark mode makes
file-based delivery mandatory. The secret VALUE never lands in ``os.environ``.
"""

from __future__ import annotations

import os

import pytest

from youtab_agent_cli import secret_file as sf
from youtab_agent_cli.secret_file import SecretFileError


def _write_strict_key(tmp_path, value, name="provider.key"):
    """Create a strict-tier-compliant key file cross-platform (0400 on POSIX,
    protected owner-only DACL on Windows)."""
    path = str(tmp_path / name)
    if os.name == "nt":
        from youtab_agent_cli import windows_acl

        if not windows_acl.pywin32_available():
            pytest.skip("provider key-file strict tier needs pywin32 on Windows")
        windows_acl.secure_write_secret_file(path, (value + "\n").encode("utf-8"))
    else:
        with open(path, "wb") as fh:
            fh.write((value + "\n").encode("utf-8"))
        os.chmod(path, 0o400)
    return path


# --- helpers ---------------------------------------------------------------


def test_resolve_credential_file_returns_none_without_file(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY_FILE", raising=False)
    assert sf.resolve_credential_file("OPENAI_API_KEY", inline_present=False) is None


def test_resolve_credential_file_dual_source_fails_closed(tmp_path, monkeypatch):
    path = _write_strict_key(tmp_path, "SENTINEL-file-value-0001")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", path)
    with pytest.raises(SecretFileError, match="ambiguous secret source"):
        sf.resolve_credential_file("OPENAI_API_KEY", inline_present=True)


def test_read_named_key_file_env(tmp_path, monkeypatch):
    path = _write_strict_key(tmp_path, "SENTINEL-named-file-env-0002")
    monkeypatch.setenv("MY_CUSTOM_KEY_PATH", path)
    assert sf.read_named_key_file_env("MY_CUSTOM_KEY_PATH") == "SENTINEL-named-file-env-0002"
    monkeypatch.delenv("MY_CUSTOM_KEY_PATH", raising=False)
    assert sf.read_named_key_file_env("MY_CUSTOM_KEY_PATH") is None
    assert sf.read_named_key_file_env("") is None


# --- registry chokepoint: config.get_env_value_prefer_dotenv ----------------


def test_registry_provider_key_from_file(tmp_path, monkeypatch):
    from youtab_agent_cli import config

    value = "SENTINEL-deepseek-from-file-0003"
    path = _write_strict_key(tmp_path, value)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", path)
    monkeypatch.setattr(config, "load_env", lambda: {})
    got = config.get_env_value_prefer_dotenv("DEEPSEEK_API_KEY")
    assert got == value
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert all(value not in v for v in os.environ.values())


def test_registry_dual_source_fails_closed(tmp_path, monkeypatch):
    from youtab_agent_cli import config

    path = _write_strict_key(tmp_path, "SENTINEL-file-0004")
    monkeypatch.setenv("OPENAI_API_KEY", "SENTINEL-inline-0004")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", path)
    monkeypatch.setattr(config, "load_env", lambda: {})
    with pytest.raises(SecretFileError, match="ambiguous secret source"):
        config.get_env_value_prefer_dotenv("OPENAI_API_KEY")


def test_registry_plaintext_env_warns_once(tmp_path, monkeypatch, caplog):
    from youtab_agent_cli import config

    monkeypatch.delenv("XAI_API_KEY_FILE", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "SENTINEL-plain-0005")
    monkeypatch.setattr(config, "load_env", lambda: {})
    sf._warned_plaintext_vars.discard("XAI_API_KEY")
    monkeypatch.delenv("YOUTAB_AGENT_LIVE_BENCHMARK", raising=False)
    import logging

    with caplog.at_level(logging.WARNING, logger="youtab_agent_cli.secret_file"):
        assert config.get_env_value_prefer_dotenv("XAI_API_KEY") == "SENTINEL-plain-0005"
    assert any("XAI_API_KEY_FILE" in r.message for r in caplog.records)
    # secret value never appears in the warning
    assert all("SENTINEL-plain-0005" not in r.message for r in caplog.records)


def test_live_benchmark_blocks_plaintext_provider_key(monkeypatch):
    from youtab_agent_cli import config

    monkeypatch.delenv("OPENAI_API_KEY_FILE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "SENTINEL-plain-live-0006")
    monkeypatch.setenv("YOUTAB_AGENT_LIVE_BENCHMARK", "1")
    monkeypatch.setattr(config, "load_env", lambda: {})
    with pytest.raises(SecretFileError, match="live-benchmark"):
        config.get_env_value_prefer_dotenv("OPENAI_API_KEY")


def test_live_benchmark_allows_file_provider_key(tmp_path, monkeypatch):
    from youtab_agent_cli import config

    value = "SENTINEL-file-live-0007"
    path = _write_strict_key(tmp_path, value)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", path)
    monkeypatch.setenv("YOUTAB_AGENT_LIVE_BENCHMARK", "1")
    monkeypatch.setattr(config, "load_env", lambda: {})
    assert config.get_env_value_prefer_dotenv("OPENAI_API_KEY") == value


# --- custom chokepoint: runtime_provider._getenv ----------------------------


def test_custom_getenv_key_env_from_file(tmp_path, monkeypatch):
    from youtab_agent_cli import runtime_provider

    value = "SENTINEL-custom-keyenv-file-0008"
    path = _write_strict_key(tmp_path, value)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("GLM_API_KEY_FILE", path)
    assert runtime_provider._getenv("GLM_API_KEY") == value


def test_custom_getenv_dual_source_fails_closed(tmp_path, monkeypatch):
    from youtab_agent_cli import runtime_provider

    path = _write_strict_key(tmp_path, "SENTINEL-file-0009")
    monkeypatch.setenv("GLM_API_KEY", "SENTINEL-inline-0009")
    monkeypatch.setenv("GLM_API_KEY_FILE", path)
    with pytest.raises(SecretFileError, match="ambiguous secret source"):
        runtime_provider._getenv("GLM_API_KEY")


@pytest.mark.parametrize("name,is_cred", [
    ("OPENAI_API_KEY", True),
    ("DEEPSEEK_API_KEY", True),
    ("ANTHROPIC_TOKEN", True),
    ("HF_TOKEN", True),
    ("GLM_API_KEY", True),
    ("OPENROUTER_BASE_URL", False),
    ("CUSTOM_BASE_URL", False),
    ("AZURE_FOUNDRY_BASE_URL", False),
    ("YOUTAB_AGENT_YOUTAB_TIMEOUT_SECONDS", False),
    ("YOUTAB_AGENT_INFERENCE_PROVIDER", False),
])
def test_looks_like_credential_env(name, is_cred):
    assert sf.looks_like_credential_env(name) is is_cred


def test_getenv_base_url_not_blocked_in_live_mode(monkeypatch):
    """NEW-1: a non-credential env var (base URL) must NOT be refused under
    live-benchmark mode by the generic _getenv reader."""
    from youtab_agent_cli import runtime_provider

    monkeypatch.setenv("YOUTAB_AGENT_LIVE_BENCHMARK", "1")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.delenv("OPENROUTER_BASE_URL_FILE", raising=False)
    assert runtime_provider._getenv("OPENROUTER_BASE_URL") == "https://openrouter.ai/api/v1"


def test_getenv_api_key_still_blocked_in_live_mode(monkeypatch):
    from youtab_agent_cli import runtime_provider

    monkeypatch.setenv("YOUTAB_AGENT_LIVE_BENCHMARK", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SENTINEL-plain-live")
    monkeypatch.delenv("DEEPSEEK_API_KEY_FILE", raising=False)
    with pytest.raises(SecretFileError, match="live-benchmark"):
        runtime_provider._getenv("DEEPSEEK_API_KEY")


def test_live_benchmark_helper_reads_flag(monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_LIVE_BENCHMARK", "yes")
    assert sf.live_benchmark_file_secrets_required() is True
    monkeypatch.setenv("YOUTAB_AGENT_LIVE_BENCHMARK", "0")
    assert sf.live_benchmark_file_secrets_required() is False
    monkeypatch.delenv("YOUTAB_AGENT_LIVE_BENCHMARK", raising=False)
    assert sf.live_benchmark_file_secrets_required() is False
