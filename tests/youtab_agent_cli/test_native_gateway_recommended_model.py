"""Native Gateway onboarding must choose from its admitted model catalog."""
import pytest


@pytest.fixture
def native_catalog(monkeypatch):
    from youtab_agent_cli import auth, models

    monkeypatch.setenv("YOUTAB_AGENT_DESKTOP", "1")
    monkeypatch.setattr(auth, "get_local_inference_token_state", lambda: {"synthetic": True})
    monkeypatch.setattr(models, "get_preferred_silent_default_model", lambda provider: "deepseek-chat")

    def forbidden_portal_call(*args, **kwargs):
        raise AssertionError("native Gateway must not consult hosted Portal recommendations")

    monkeypatch.setattr(models, "get_curated_youtab_model_ids", forbidden_portal_call)
    monkeypatch.setattr(models, "check_youtab_free_tier", forbidden_portal_call)
    return models


@pytest.mark.parametrize("catalog, expected", [
    (["deepseek-reasoner", "deepseek-chat"], "deepseek-chat"),
    (["anthropic/claude-opus", "deepseek-chat"], "deepseek-chat"),
    (["anthropic/claude-opus"], ""),
    ([], ""),
])
def test_native_default_is_supported_and_admitted(monkeypatch, native_catalog, catalog, expected):
    from youtab_agent_cli.web_server import get_recommended_default_model

    monkeypatch.setattr(native_catalog, "cached_provider_model_ids", lambda provider: catalog)
    assert get_recommended_default_model("youtab") == {
        "provider": "youtab", "model": expected, "free_tier": None,
    }


def test_gateway_token_outside_desktop_uses_same_catalog(monkeypatch, native_catalog):
    from youtab_agent_cli.web_server import get_recommended_default_model

    monkeypatch.delenv("YOUTAB_AGENT_DESKTOP", raising=False)
    monkeypatch.setattr(native_catalog, "cached_provider_model_ids", lambda provider: ["deepseek-chat"])
    assert get_recommended_default_model("youtab")["model"] == "deepseek-chat"


def test_native_recommendation_http_contract(monkeypatch, native_catalog):
    from starlette.testclient import TestClient
    from youtab_agent_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(native_catalog, "cached_provider_model_ids", lambda provider: ["deepseek-chat"])
    with TestClient(app) as client:
        client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        response = client.get("/api/model/recommended-default", params={"provider": "youtab"})
    assert response.status_code == 200
    assert response.json() == {"provider": "youtab", "model": "deepseek-chat", "free_tier": None}


def test_native_catalog_failure_does_not_use_portal_fallback(monkeypatch, native_catalog):
    from youtab_agent_cli.web_server import get_recommended_default_model

    def failed_catalog(provider):
        raise TimeoutError("synthetic catalog failure")

    monkeypatch.setattr(native_catalog, "cached_provider_model_ids", failed_catalog)
    assert get_recommended_default_model("youtab")["model"] == ""


def test_hosted_portal_keeps_tier_recommendation_path(monkeypatch):
    from youtab_agent_cli import auth, models
    from youtab_agent_cli.web_server import get_recommended_default_model

    monkeypatch.delenv("YOUTAB_AGENT_DESKTOP", raising=False)
    monkeypatch.setattr(auth, "get_local_inference_token_state", lambda: None)
    monkeypatch.setattr(auth, "get_provider_auth_state", lambda provider: {})
    monkeypatch.setattr(models, "get_curated_youtab_model_ids", lambda: ["hosted/free-model"])
    monkeypatch.setattr(models, "get_pricing_for_provider", lambda provider: {})
    monkeypatch.setattr(models, "check_youtab_free_tier", lambda **kwargs: True)
    monkeypatch.setattr(models, "union_with_portal_free_recommendations", lambda ids, prices, url: (ids, prices))
    monkeypatch.setattr(models, "partition_youtab_models_by_tier", lambda ids, prices, **kwargs: (ids, []))
    assert get_recommended_default_model("youtab") == {
        "provider": "youtab", "model": "hosted/free-model", "free_tier": True,
    }
