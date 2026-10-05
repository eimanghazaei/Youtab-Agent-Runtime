"""Optional SDK bootstrap keeps its canonical API and explicit failure behavior."""
import subprocess
import sys


def test_bootstrap_export_is_canonical_and_runs_on_import():
    code = '''
import sys, types
calls = []
installer = types.ModuleType("tools.lazy_deps")
def ensure(feature, *, prompt=True):
    calls.append((feature, prompt))
installer.ensure = ensure
sys.modules["tools.lazy_deps"] = installer
from agent import bedrock_adapter
assert bedrock_adapter.ensure is installer.ensure
assert calls == [("provider.bedrock", False)]
bedrock_adapter.ensure("synthetic.feature", prompt=True)
assert calls[-1] == ("synthetic.feature", True)
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_missing_installer_keeps_importable_adapter_and_explicit_export_failure():
    code = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "tools.lazy_deps":
        raise ImportError("synthetic unavailable installer")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from agent.bedrock_adapter import *
assert callable(ensure)
try:
    ensure("provider.bedrock", prompt=False)
except ImportError as error:
    assert "installer is unavailable" in str(error)
else:
    raise AssertionError("missing installer was silently ignored")
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
