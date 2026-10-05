"""Best-effort failures retain fallback behavior without logging private payloads."""
import logging

from youtab_agent_cli import credential_lifecycle, provider_models_cache, youtab_picker_catalog


def _private_failure(*args, **kwargs):
    raise RuntimeError("synthetic-private-error-detail")


def test_cache_write_and_invalidation_failures_are_private(tmp_path, monkeypatch, caplog):
    import utils
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(utils, "atomic_json_write", _private_failure)
    provider_models_cache._save_provider_models_cache({}, path=tmp_path / "cache.json")
    provider_models_cache.clear_provider_models_cache("synthetic", load_cache=_private_failure)
    assert "cache write failed" in caplog.text
    assert "cache invalidation failed" in caplog.text
    assert "synthetic-private-error-detail" not in caplog.text


def test_anonymous_pricing_fallback_does_not_log_credential_error(monkeypatch, caplog):
    import youtab_agent_cli.profile_inference as profile
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(profile, "get_local_inference_token_state", lambda: None)
    assert youtab_picker_catalog.resolve_youtab_pricing_credentials(
        _private_failure, lambda: "https://synthetic.invalid/v1",
    ) == ("", "https://synthetic.invalid/v1")
    assert "anonymous fallback" in caplog.text
    assert "synthetic-private-error-detail" not in caplog.text


def test_suppression_failure_keeps_credential_cleanup_result(monkeypatch, caplog):
    import youtab_agent_cli.auth as auth
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(credential_lifecycle, "_prune_env_pool_entries", lambda name: ["synthetic"])
    monkeypatch.setattr(credential_lifecycle, "_providers_for_env_var", lambda name: [])
    monkeypatch.setattr(auth, "suppress_credential_source", _private_failure)
    assert credential_lifecycle.purge_env_credential_references(
        "SYNTHETIC_API_KEY", clear_models_cache=False,
    ) == {"pool_pruned": ["synthetic"], "providers": ["synthetic"]}
    assert "source suppression failed" in caplog.text
    assert "synthetic-private-error-detail" not in caplog.text
