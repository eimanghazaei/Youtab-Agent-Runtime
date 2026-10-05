"""Cache persistence and invalidation work independently of live model discovery."""

import json
import subprocess
import sys

from youtab_agent_cli import model_provider_identity, provider_models_cache


def test_leaf_cache_can_save_load_and_invalidate_without_models(tmp_path):
    script = '''
import builtins, os, sys
os.environ["YOUTAB_AGENT_HOME"] = sys.argv[1]
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {"youtab_agent_cli.models", "youtab_agent_cli.auth", "youtab_agent_cli.config", "youtab_agent_cli.main"}:
        raise AssertionError("cache loaded live model/configuration machinery: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from youtab_agent_cli.provider_models_cache import _save_provider_models_cache, _load_provider_models_cache, clear_provider_models_cache
_save_provider_models_cache({"zai": {"models": ["synthetic-model"]}, "other": {"models": ["other-model"]}})
assert "zai" in _load_provider_models_cache()
clear_provider_models_cache(" GLM ")
assert _load_provider_models_cache() == {"other": {"models": ["other-model"]}}
clear_provider_models_cache()
assert _load_provider_models_cache() == {}
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_malformed_and_non_object_cache_are_ignored(tmp_path):
    path = tmp_path / "cache.json"
    for content in ("not json", "[]", "null"):
        path.write_text(content, encoding="utf-8")
        assert provider_models_cache._load_provider_models_cache(path=path) == {}


def test_models_compatibility_preserves_patchable_cache_path(tmp_path, monkeypatch):
    from youtab_agent_cli import models

    path = tmp_path / "patched-cache.json"
    monkeypatch.setattr(models, "_provider_models_cache_path", lambda: path)
    models._save_provider_models_cache({"zai": {"models": ["synthetic"]}, "other": {}})
    assert models._load_provider_models_cache()["zai"]["models"] == ["synthetic"]
    models.clear_provider_models_cache("glm")
    assert json.loads(path.read_text(encoding="utf-8")) == {"other": {}}
    models.clear_provider_models_cache()
    assert not path.exists()


def test_models_compatibility_preserves_patchable_helpers(monkeypatch):
    from youtab_agent_cli import models

    cache = {"synthetic-provider": {"models": ["synthetic"]}, "other": {}}
    saved = []
    monkeypatch.setattr(models, "_load_provider_models_cache", lambda: cache)
    monkeypatch.setattr(models, "_save_provider_models_cache", lambda value: saved.append(dict(value)))
    monkeypatch.setattr(models, "normalize_provider", lambda value: "synthetic-provider")
    models.clear_provider_models_cache("synthetic-alias")
    assert saved == [{"other": {}}]


def test_provider_identity_mapping_is_shared_and_patchable(monkeypatch):
    from youtab_agent_cli import models

    assert models._PROVIDER_ALIASES is model_provider_identity._PROVIDER_ALIASES
    assert model_provider_identity.normalize_provider(" GLM ") == models.normalize_provider(" GLM ") == "zai"
    assert model_provider_identity.normalize_provider(None) == "openrouter"
    assert model_provider_identity.normalize_provider("auto") == "auto"
    monkeypatch.setattr(models, "_PROVIDER_ALIASES", {"synthetic": "patched-provider"})
    assert models.normalize_provider("synthetic") == "patched-provider"
