"""Shared readiness probes stay read-only and reflect optional SDK installs."""

import importlib.machinery
import os
from pathlib import Path
import subprocess
import sys

from tools import local_capabilities as caps


def test_optional_stt_probe_observes_install_after_initial_read(monkeypatch):
    installed = False

    def find_spec(name):
        assert name == "faster_whisper"
        return importlib.machinery.ModuleSpec(name, None) if installed else None

    monkeypatch.setattr(caps._ilu, "find_spec", find_spec)
    assert not caps.has_faster_whisper()
    installed = True
    assert caps.has_faster_whisper()


def test_browser_probe_reflects_disk_change_without_runtime_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
    monkeypatch.setattr(caps.shutil, "which", lambda _: None)
    assert not caps.chromium_installed(search_roots=lambda: [str(tmp_path)])
    (tmp_path / "chromium-1208").mkdir()
    assert caps.chromium_installed(search_roots=lambda: [str(tmp_path)])


def test_engine_config_precedes_env_and_invalid_config_is_auto(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_ENGINE", "lightpanda")
    assert caps.browser_engine(lambda: {"browser": {"engine": "chrome"}}) == "chrome"
    assert caps.browser_engine(lambda: {}) == "lightpanda"
    assert caps.browser_engine(lambda: {"browser": {"engine": "unknown"}}) == "auto"


def test_subscription_readiness_does_not_load_registered_tools(tmp_path):
    script = r'''
import importlib.abc
import sys
from unittest.mock import patch

denied_imports = []
class DenyTools(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'tools.browser_tool', 'tools.transcription_tools', 'tools.lazy_deps'}:
            denied_imports.append(fullname)
            raise AssertionError('readiness imported runtime: ' + fullname)

sys.meta_path.insert(0, DenyTools())
from youtab_agent_cli import youtab_subscription as subscription
from tools import local_capabilities as caps
from youtab_agent_cli import config
with patch.object(subscription, '_has_agent_browser', lambda: True), \
     patch.object(subscription, 'get_env_value', lambda _: ''), \
     patch.object(config, 'read_raw_config', lambda: {'browser': {'engine': 'chrome'}}), \
     patch.object(caps, 'chromium_installed', lambda: True), \
     patch.object(caps, 'has_faster_whisper', lambda: True):
    assert subscription._local_browser_runnable()
    assert subscription._local_stt_backend_available()
assert denied_imports == [], denied_imports
'''
    env = {**os.environ, "YOUTAB_AGENT_HOME": str(tmp_path)}
    result = subprocess.run([sys.executable, "-c", script],
                            cwd=Path(__file__).resolve().parents[2], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
