"""WAVE-30D: the worker's provider resolver must honour a local-server engine
binding (ollama/vllm/…) and NEVER silently fall back to the machine-default
OpenRouter provider.

Root cause of live-canary defect ``t_2cf3c738``: an engine-bound Track A task
persisted ``provider_override=ollama`` + ``model_override=<qwen tag>`` and the
dispatcher passed ``-m <model> --provider ollama`` to the worker, but
``resolve_runtime_provider(requested="ollama")`` resolved the endpoint to
``OPENROUTER_BASE_URL`` (the local endpoint env was never consulted) and then
failed with "empty API key / set OPENROUTER_API_KEY" before Qwen was ever
dispatched.

These tests pin the resolver contract. The real cross-process
serialisation/rehydration is covered by
``test_kanban_worker_local_binding_boundary.py``.
"""
from __future__ import annotations

import pytest

from youtab_agent_cli import engine_connection as ec
from youtab_agent_cli.runtime_provider import resolve_runtime_provider

_LOCAL_ENV_VARS = (
    "OLLAMA_BASE_URL",
    "OLLAMA_HOST",
    "VLLM_BASE_URL",
    "LMSTUDIO_BASE_URL",
    "CUSTOM_BASE_URL",
    "OPENROUTER_BASE_URL",
    "OPENAI_BASE_URL",
)
_KEY_ENV_VARS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OLLAMA_API_KEY",
)


@pytest.fixture
def clean_provider_env(monkeypatch):
    """A worker-like env: no cloud keys, no stray base_url overrides, empty config."""
    for var in _LOCAL_ENV_VARS + _KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Neutralise config.yaml so the resolver sees "no configured base_url".
    monkeypatch.setattr(
        "youtab_agent_cli.runtime_provider._get_model_config", lambda: {}
    )
    return monkeypatch


# ---------------------------------------------------------------------------
# engine_connection.inference_base_url_for_local_provider — endpoint classification
# ---------------------------------------------------------------------------


def test_inference_url_appends_v1_to_ollama_root(clean_provider_env):
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11435")
    assert (
        ec.inference_base_url_for_local_provider("ollama")
        == "http://127.0.0.1:11435/v1"
    )


def test_inference_url_no_double_v1(clean_provider_env):
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11435/v1")
    assert (
        ec.inference_base_url_for_local_provider("ollama")
        == "http://127.0.0.1:11435/v1"
    )


def test_inference_url_loopback_default_when_env_absent(clean_provider_env):
    assert (
        ec.inference_base_url_for_local_provider("ollama")
        == "http://127.0.0.1:11434/v1"
    )


def test_inference_url_private_host_allowed(clean_provider_env):
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://192.168.1.50:11434")
    assert (
        ec.inference_base_url_for_local_provider("ollama")
        == "http://192.168.1.50:11434/v1"
    )


def test_inference_url_tailnet_cgnat_allowed(clean_provider_env):
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://100.99.1.2:11434")
    assert (
        ec.inference_base_url_for_local_provider("ollama")
        == "http://100.99.1.2:11434/v1"
    )


def test_inference_url_public_host_fails_closed(clean_provider_env):
    """A public endpoint (no opt-in) resolves to "" — never a usable URL."""
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://8.8.8.8:11434")
    assert ec.inference_base_url_for_local_provider("ollama") == ""


def test_inference_url_credentials_in_url_fails_closed(clean_provider_env):
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://user:pass@127.0.0.1:11434")
    assert ec.inference_base_url_for_local_provider("ollama") == ""


def test_inference_url_non_local_provider_is_empty(clean_provider_env):
    assert ec.inference_base_url_for_local_provider("openrouter") == ""
    assert ec.inference_base_url_for_local_provider("anthropic") == ""
    assert ec.inference_base_url_for_local_provider("") == ""


def test_inference_url_vllm_default_already_has_v1(clean_provider_env):
    assert (
        ec.inference_base_url_for_local_provider("vllm")
        == "http://127.0.0.1:8000/v1"
    )


# ---------------------------------------------------------------------------
# resolve_runtime_provider — engine-bound local provider resolution
# ---------------------------------------------------------------------------


def test_ollama_binding_resolves_local_endpoint_not_openrouter(clean_provider_env):
    """The Track A case: provider=ollama + OLLAMA_BASE_URL → local /v1 endpoint."""
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11435")
    r = resolve_runtime_provider(
        requested="ollama", target_model="youtab-qwen35-9b-agent-64k:latest"
    )
    assert r["provider"] == "custom"
    assert r["base_url"] == "http://127.0.0.1:11435/v1"
    assert "openrouter.ai" not in r["base_url"]


def test_ollama_binding_requires_no_openrouter_key(clean_provider_env):
    """No OPENROUTER_API_KEY is consulted or demanded for an Ollama-bound run.

    The resolved key is the local ``no-key-required`` placeholder, so the worker
    never surfaces "set OPENROUTER_API_KEY" (the live-canary failure).
    """
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11435")
    r = resolve_runtime_provider(requested="ollama")
    assert r["api_key"] == "no-key-required"
    # And it is genuinely a local endpoint, so cli_agent_setup_mixin's
    # `_has_custom_base` branch accepts the placeholder rather than erroring.
    assert "openrouter.ai" not in r["base_url"]


def test_ollama_binding_public_endpoint_fails_closed_not_openrouter(clean_provider_env):
    """An unauthorised (public) endpoint fails closed with an EMPTY base_url.

    It must NOT degrade to the OpenRouter default — that would mis-attribute the
    run to a cloud provider.
    """
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://8.8.8.8:11434")
    r = resolve_runtime_provider(requested="ollama")
    assert r["base_url"] == ""
    assert r["provider"] == "custom"


def test_ollama_binding_missing_endpoint_uses_loopback_default(clean_provider_env):
    r = resolve_runtime_provider(requested="ollama")
    assert r["base_url"] == "http://127.0.0.1:11434/v1"


def test_model_identity_is_not_substituted(clean_provider_env):
    """target_model passes through unchanged — no silent model substitution."""
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11435")
    tag = "youtab-qwen35-9b-agent-64k:latest"
    r = resolve_runtime_provider(requested="ollama", target_model=tag)
    # The resolver does not rewrite the model; the -m <tag> the dispatcher passed
    # is the authoritative model. (model_override reaching args.model is proven in
    # the boundary test.) Here we assert the resolver never injects a foreign
    # model name of its own.
    assert r.get("model") in (None, tag)


def test_vllm_binding_resolves_local_endpoint(clean_provider_env):
    clean_provider_env.setenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1")
    r = resolve_runtime_provider(requested="vllm")
    assert r["base_url"] == "http://127.0.0.1:8000/v1"
    assert "openrouter.ai" not in r["base_url"]


def test_resolved_config_is_complete_for_client_construction(clean_provider_env):
    """A bound local run yields a complete OpenAI-compatible client config
    (base_url + key + chat_completions api_mode), so the full model→tool loop
    can run against the local server. The live loop itself is exercised by the
    deterministic seam + benchmark suites; here we prove wireability."""
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11435")
    r = resolve_runtime_provider(
        requested="ollama", target_model="youtab-qwen35-9b-agent-64k:latest"
    )
    assert r["base_url"].endswith("/v1")
    assert r["api_key"]  # non-empty (placeholder for the keyless local server)
    assert r["api_mode"] == "chat_completions"


# ---------------------------------------------------------------------------
# Non-local providers must be UNCHANGED (Track B + normal behaviour)
# ---------------------------------------------------------------------------


def test_openrouter_resolution_unchanged(clean_provider_env):
    clean_provider_env.setenv("OPENROUTER_API_KEY", "sk-or-test")
    r = resolve_runtime_provider(requested="openrouter")
    assert r["provider"] == "openrouter"
    assert "openrouter.ai" in r["base_url"]
    assert r["api_key"] == "sk-or-test"


def test_bare_custom_without_local_alias_keeps_openrouter_fallback(clean_provider_env):
    """A bare ``custom`` request (NOT a local-server alias) with no configured
    base_url keeps the prior OpenRouter fallback — the fix narrows behaviour to
    local-server providers only."""
    r = resolve_runtime_provider(requested="custom")
    # Unchanged legacy behaviour: base_url falls back to the OpenRouter default.
    assert "openrouter.ai" in r["base_url"]


# ---------------------------------------------------------------------------
# Path-independent fail-closed invariant (reviewer F1/F2): a local-server-bound
# run can NEVER resolve an unauthorised / cloud endpoint, regardless of which
# resolution branch produced it.
# ---------------------------------------------------------------------------


def test_custom_base_url_override_to_cloud_fails_closed(clean_provider_env):
    """F1: a stray CUSTOM_BASE_URL pointing at a cloud host must NOT override the
    local binding — the invariant fails it closed rather than dialing the cloud."""
    clean_provider_env.setenv("CUSTOM_BASE_URL", "https://openrouter.ai/api/v1")
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11435")
    r = resolve_runtime_provider(requested="ollama")
    assert r["base_url"] == ""
    assert r["api_key"] == ""
    assert r["source"] == "local-binding-fail-closed"


def test_custom_base_url_override_to_loopback_is_allowed(clean_provider_env):
    """A legitimate loopback CUSTOM_BASE_URL override is authorised and kept."""
    clean_provider_env.setenv("CUSTOM_BASE_URL", "http://127.0.0.1:9999/v1")
    r = resolve_runtime_provider(requested="ollama")
    assert r["base_url"] == "http://127.0.0.1:9999/v1"


def test_explicit_public_base_url_fails_closed_for_local_provider(clean_provider_env):
    r = resolve_runtime_provider(
        requested="ollama", explicit_base_url="https://api.some-cloud.example/v1"
    )
    assert r["base_url"] == ""
    assert r["source"] == "local-binding-fail-closed"


def test_invariant_catches_any_upstream_branch_resolving_cloud(clean_provider_env):
    """F2: whatever internal branch (named-custom entry, credential pool, …)
    produced a cloud endpoint for a local-bound provider, the single exit
    invariant fails it closed. Simulated by forcing the impl to return a cloud
    result — proving the guard is genuinely path-independent."""
    import youtab_agent_cli.runtime_provider as rp

    def _fake_impl(**_kwargs):
        return {
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-should-be-stripped",
            "source": "named-custom-or-pool",
        }

    clean_provider_env.setattr(rp, "_resolve_runtime_provider_impl", _fake_impl)
    r = resolve_runtime_provider(requested="ollama")
    assert r["base_url"] == ""
    assert r["api_key"] == ""
    assert r["source"] == "local-binding-fail-closed"


def test_invariant_does_not_touch_non_local_providers(clean_provider_env):
    """The invariant must not fire for cloud providers — openrouter keeps its
    normal cloud endpoint + key."""
    clean_provider_env.setenv("OPENROUTER_API_KEY", "sk-or-test")
    r = resolve_runtime_provider(requested="openrouter")
    assert "openrouter.ai" in r["base_url"]
    assert r["api_key"] == "sk-or-test"
    assert r["source"] != "local-binding-fail-closed"
