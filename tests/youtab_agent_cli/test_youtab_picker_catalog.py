"""Shared picker transport and pure transforms use synthetic isolated inputs."""
import json
import subprocess
import sys
from io import BytesIO

from youtab_agent_cli import youtab_picker_catalog as picker


def test_leaf_import_does_not_load_auth_models_or_registered_tools():
    code = """
import sys
from youtab_agent_cli import youtab_picker_catalog, model_pricing
assert 'youtab_agent_cli.auth' not in sys.modules
assert 'youtab_agent_cli.models' not in sys.modules
assert 'tools.browser_tool' not in sys.modules
assert 'tools.transcription_tools' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_shared_pricing_cache_and_sale_projection():
    calls = []
    cache = {}

    def open_url(req, *, timeout):
        calls.append(req)
        return BytesIO(json.dumps({"data": [{"id": "synthetic/model", "pricing": {
            "prompt": "0.000001", "completion": "0.000002",
            "original": {"prompt": "0.000002", "completion": "0.000004"},
        }}]}).encode())

    first = picker.fetch_models_with_pricing(
        "synthetic-token", "https://synthetic.invalid", cache=cache,
        open_url=open_url, include_sale_original=True,
    )
    assert first["synthetic/model"]["original"]["prompt"] == "0.000002"
    assert calls[0].get_header("Authorization") == "Bearer synthetic-token"
    assert picker.fetch_models_with_pricing(
        base_url="https://synthetic.invalid", cache=cache, open_url=open_url,
    ) is first
    assert len(calls) == 1


def test_recommendations_use_last_good_temporary_disk_copy(tmp_path):
    path = tmp_path / "recommendations.json"
    base = "https://synthetic.invalid"
    payload = {"freeRecommendedModels": [{"modelName": "synthetic/free"}]}
    picker._write_youtab_recommended_disk(base, payload, disk_path=lambda: path)

    def unavailable(*args, **kwargs):
        raise OSError("synthetic offline")

    result = picker.fetch_youtab_recommended_models(
        base, cache={}, open_url=unavailable,
        read_disk=lambda url: picker._read_youtab_recommended_disk(
            url, disk_path=lambda: path),
    )
    ids, prices = picker.union_with_portal_free_recommendations(
        ["synthetic/paid"], {}, base, fetch_recommendations=lambda *a, **k: result,
    )
    assert ids == ["synthetic/paid", "synthetic/free"]
    assert picker.partition_youtab_models_by_tier(ids, prices, True) == (
        ["synthetic/free"], ["synthetic/paid"],
    )


def test_pricing_credentials_preserve_override_and_profile_precedence(monkeypatch):
    import youtab_agent_cli.profile_inference as profile
    monkeypatch.setattr(profile, "get_local_inference_token_state", lambda: None)
    assert picker.resolve_youtab_pricing_credentials(
        lambda: {"api_key": "synthetic", "base_url": "https://credentials.invalid/v1"},
        lambda: "https://override.invalid/v1",
    ) == ("synthetic", "https://override.invalid/v1")
    monkeypatch.setattr(profile, "get_local_inference_token_state", lambda: {})
    assert picker.resolve_youtab_pricing_credentials(
        lambda: {"api_key": "synthetic", "base_url": "https://credentials.invalid/v1"},
        lambda: "https://override.invalid/v1",
    ) == ("", "")


def test_force_fresh_entitlement_updates_shared_picker_cache(monkeypatch):
    from types import SimpleNamespace
    from youtab_agent_cli import models
    import youtab_agent_cli.youtab_account as account
    monkeypatch.setattr(models, "_free_tier_cache", picker._free_tier_cache)
    monkeypatch.setitem(picker._free_tier_cache, "value", (True, picker.time.monotonic()))
    calls = []

    def reader(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(is_free_tier=False)

    monkeypatch.setattr(account, "get_youtab_portal_account_info", reader)
    assert picker.check_youtab_free_tier(reader, force_fresh=True) is False
    assert models.check_youtab_free_tier() is False
    assert calls == [{"force_fresh": True}]
