"""Tests for cross-profile auth fallback.

When ``YOUTAB_AGENT_HOME`` points to a named profile, ``read_credential_pool()``
and ``get_provider_auth_state()`` fall back to the global-root
``auth.json`` per-provider when the profile has no entries for that
provider.  Writes still target the profile only.

See the #18594 follow-up report: profile workers couldn't see providers
authenticated only at the global root.
"""

from __future__ import annotations

import json
import time
import base64
import io
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def _make_auth_store(pool: dict | None = None, providers: dict | None = None) -> dict:
    store: dict = {"version": 1}
    if pool is not None:
        store["credential_pool"] = pool
    if providers is not None:
        store["providers"] = providers
    return store


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Set up a global root + an active profile under Path.home()/.youtab-agent-runtime/profiles/coder.

    * Path.home() -> tmp_path
    * Global root -> tmp_path/.youtab-agent-runtime            (has its own auth.json fixture)
    * Profile     -> tmp_path/.youtab-agent-runtime/profiles/coder   (active, YOUTAB_AGENT_HOME points here)

    This mirrors the real "named profile mounted under the default root"
    layout that profile users actually have on disk.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    global_root = tmp_path / ".youtab-agent-runtime"
    global_root.mkdir()
    profile_dir = global_root / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(profile_dir))
    return {"global": global_root, "profile": profile_dir}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _inference_jwt(
    exp: int, scope: str = "inference:invoke", token_type: str = "inference_access",
    audience: str = "https://api.youtab.io/v1/inference",
) -> str:
    def encode(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    claims = {
        "iss": "https://api.youtab.io", "aud": audience, "type": token_type,
        "scope": scope, "exp": exp,
    }
    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.signature"


def test_profile_inference_token_is_local_and_resolves_gateway(profile_env, monkeypatch):
    from youtab_agent_cli import auth, runtime_provider

    token = _inference_jwt(int(time.time()) + 900)
    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "youtab": {"access_token": "global-account-token"},
    }))
    monkeypatch.setattr(runtime_provider, "resolve_youtab_runtime_credentials",
                        lambda **_kw: pytest.fail("backend OAuth refresh was attempted"))
    auth.persist_profile_inference_token(token)
    local = json.loads((profile_env["profile"] / "auth.json").read_text(encoding="utf-8"))
    state = local["providers"]["youtab"]
    assert state["agent_key"] == token
    assert state["agent_key_expires_at"].endswith("+00:00")
    assert set(state) == {
        "agent_key", "agent_key_expires_at", "agent_key_expires_in", "agent_key_obtained_at",
    }
    assert 0 < auth.inference_token_safety_seconds(state) < state["agent_key_expires_in"]
    assert "global-account-token" in (profile_env["global"] / "auth.json").read_text(encoding="utf-8")
    resolved = runtime_provider._resolve_profile_inference_runtime(
        requested_provider="youtab", target_model="deepseek.v4_flash",
    )
    assert resolved["api_key"] == token
    assert resolved["base_url"] == "https://api.youtab.io/v1"
    assert resolved["api_mode"] == "chat_completions"
    auth.persist_profile_inference_token(None)
    assert auth.get_local_inference_token_state()["agent_key"] == ""
    with pytest.raises(auth.AuthError, match="expired"):
        runtime_provider._resolve_profile_inference_runtime(
            requested_provider="youtab", target_model="deepseek.v4_flash",
        )


def test_profile_inference_cli_writer_uses_stdin_without_echo(profile_env, monkeypatch, capsys):
    from youtab_agent_cli import auth, auth_commands

    token = _inference_jwt(int(time.time()) + 900)
    monkeypatch.setattr("sys.stdin", io.StringIO(token))
    auth_commands.auth_profile_inference_token_command(SimpleNamespace(clear=False))
    assert auth.get_local_inference_token_state()["agent_key"] == token
    assert token not in capsys.readouterr().out
    auth_commands.auth_profile_inference_token_command(SimpleNamespace(clear=True))
    assert auth.get_local_inference_token_state()["agent_key"] == ""


def test_profile_inference_token_rejects_account_scope(profile_env):
    from youtab_agent_cli import auth

    with pytest.raises(auth.AuthError, match="Invalid inference credential"):
        auth.persist_profile_inference_token(_inference_jwt(int(time.time()) + 900, "account:read"))
    with pytest.raises(auth.AuthError, match="type, audience, scope"):
        auth.persist_profile_inference_token(_inference_jwt(
            int(time.time()) + 900, token_type="account_access",
        ))
    with pytest.raises(auth.AuthError, match="type, audience, scope"):
        auth.persist_profile_inference_token(_inference_jwt(
            int(time.time()) + 900, audience="https://api.youtab.io/v1/account",
        ))
    assert not (profile_env["profile"] / "auth.json").exists()


def test_preexisting_account_key_is_shadowed_not_promoted(profile_env, monkeypatch):
    from youtab_agent_cli import auth, runtime_provider

    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={
        "youtab": {"agent_key": _inference_jwt(
            int(time.time()) + 900, token_type="account_access",
        )},
    }))
    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "youtab": {"agent_key": _inference_jwt(int(time.time()) + 900)},
    }))
    monkeypatch.setenv("YOUTAB_AGENT_DESKTOP", "1")
    assert auth.get_local_inference_token_state()["agent_key"] == ""
    with pytest.raises(auth.AuthError, match="expired"):
        runtime_provider._resolve_profile_inference_runtime(
            requested_provider="youtab", target_model="deepseek.v4_flash",
        )


def test_profile_model_catalog_uses_exact_gateway_ids_without_fallback(profile_env, monkeypatch):
    from youtab_agent_cli import auth, models

    token = _inference_jwt(int(time.time()) + 900)
    auth.persist_profile_inference_token(token)
    seen = []

    def catalog(**kwargs):
        seen.append(kwargs)
        return ["deepseek.v4_flash", "anthropic/claude"]

    monkeypatch.setattr(auth, "fetch_youtab_models", catalog)
    assert models.provider_model_ids("youtab") == ["deepseek.v4_flash"]
    assert seen[0]["api_key"] == token
    assert seen[0]["inference_base_url"] == "https://api.youtab.io/v1"
    assert seen[0]["exact"] is True
    monkeypatch.setattr(auth, "fetch_youtab_models", lambda **_kw: (_ for _ in ()).throw(OSError()))
    assert models.provider_model_ids("youtab") == []


def test_short_lived_token_margin_never_exceeds_own_lifetime(profile_env):
    from youtab_agent_cli import auth, runtime_provider

    token = _inference_jwt(int(time.time()) + 12)
    auth.persist_profile_inference_token(token)
    state = auth.get_local_inference_token_state()
    assert 0 <= auth.inference_token_safety_seconds(state) < state["agent_key_expires_in"]
    assert runtime_provider._resolve_profile_inference_runtime(
        requested_provider="youtab", target_model="deepseek.v4_flash",
    )["api_key"] == token


def test_desktop_without_profile_token_never_uses_global_youtab(profile_env, monkeypatch):
    from youtab_agent_cli import auth, models, runtime_provider
    from youtab_agent_cli.proxy.adapters.youtab_portal import YoutabPortalAdapter

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "youtab": {"access_token": "global-account-token"},
    }))
    monkeypatch.setenv("YOUTAB_AGENT_DESKTOP", "1")
    monkeypatch.setattr(runtime_provider, "resolve_youtab_runtime_credentials",
                        lambda **_kw: pytest.fail("legacy refresh was attempted"))
    assert models.provider_model_ids("youtab") == []
    assert auth.get_youtab_auth_status_local()["logged_in"] is False
    with pytest.raises(auth.AuthError, match="profile Gateway credential"):
        runtime_provider._resolve_profile_inference_runtime(
            requested_provider="youtab", target_model="deepseek.v4_flash",
        )
    proxy = YoutabPortalAdapter()
    assert not proxy.is_authenticated()
    assert proxy.allowed_paths == frozenset({"/chat/completions", "/models"})
    with pytest.raises(RuntimeError, match="sign-in is required"):
        proxy.get_credential()


def test_profile_proxy_uses_gateway_token_without_refresh(profile_env, monkeypatch):
    from youtab_agent_cli import auth
    from youtab_agent_cli.proxy.adapters import youtab_portal

    token = _inference_jwt(int(time.time()) + 900)
    auth.persist_profile_inference_token(token)
    monkeypatch.setattr(youtab_portal, "resolve_youtab_runtime_credentials",
                        lambda **_kw: pytest.fail("backend OAuth refresh was attempted"))
    proxy = youtab_portal.YoutabPortalAdapter()
    assert proxy.allowed_paths == frozenset({"/chat/completions", "/models"})
    credential = proxy.get_credential()
    assert credential.bearer == token
    assert credential.base_url == "https://api.youtab.io/v1"
    assert proxy.get_retry_credential(failed_credential=credential, status_code=401) is None


def test_gateway_engine_id_is_saved_without_alias_or_account_key(profile_env, monkeypatch):
    from youtab_agent_cli import auth, models, web_server

    auth.persist_profile_inference_token(_inference_jwt(int(time.time()) + 900))
    monkeypatch.setattr(models, "provider_model_ids", lambda *_a, **_kw: ["deepseek.v4_flash"])
    monkeypatch.setattr(web_server, "_normalize_main_model_assignment",
                        lambda *_a: pytest.fail("Engine ID was normalized"))
    monkeypatch.setattr(web_server, "load_config", lambda: {
        "model": {"provider": "youtab", "default": "old", "api_key": "old-account-key",
                  "base_url": "https://inference-api.youtab.io/v1"},
    })
    saved = []
    monkeypatch.setattr(web_server, "save_config", saved.append)
    from youtab_agent_cli import tools_config, youtab_subscription
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *_a, **_kw: [])
    monkeypatch.setattr(youtab_subscription, "apply_youtab_managed_defaults",
                        lambda *_a, **_kw: set())

    result = web_server._apply_model_assignment_sync(
        "main", "youtab", "deepseek.v4_flash", "", "",
    )
    assert result["model"] == "deepseek.v4_flash"
    assert saved[0]["model"]["default"] == "deepseek.v4_flash"
    assert saved[0]["model"]["base_url"] == "https://api.youtab.io/v1"
    assert "api_key" not in saved[0]["model"]
    with pytest.raises(Exception) as refused:
        web_server._apply_model_assignment_sync("main", "youtab", "unknown.engine", "", "")
    assert refused.value.status_code == 422
    assert len(saved) == 1
    from youtab_agent_cli.providers import youtab_api_mode
    with pytest.raises(ValueError, match="/v1/messages"):
        youtab_api_mode("anthropic.claude")


# ---------------------------------------------------------------------------
# read_credential_pool — provider-slice reads
# ---------------------------------------------------------------------------








def test_missing_global_auth_file_is_safe(profile_env):
    """Profile processes that never had a global auth.json still work."""
    from youtab_agent_cli.auth import read_credential_pool

    # No global auth.json written at all.
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-profile",
        }],
    }))

    assert read_credential_pool("openrouter")[0]["id"] == "prof-1"
    assert read_credential_pool("anthropic") == []


def test_malformed_global_auth_file_does_not_break_profile_read(profile_env):
    (profile_env["global"] / "auth.json").write_text("{not valid json", encoding="utf-8")
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-profile",
        }],
    }))

    from youtab_agent_cli.auth import read_credential_pool

    # Profile reads still work; malformed global is silently ignored.
    assert read_credential_pool("openrouter")[0]["id"] == "prof-1"
    # And no fallback for anthropic since global is unreadable.
    assert read_credential_pool("anthropic") == []


# ---------------------------------------------------------------------------
# read_credential_pool — whole-pool reads (provider_id=None)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_provider_auth_state — singleton fallback
# ---------------------------------------------------------------------------


def test_provider_auth_state_falls_back_to_global_when_profile_has_none(profile_env):
    from youtab_agent_cli.auth import get_provider_auth_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "youtab": {"access_token": "youtab-global", "refresh_token": "rt-global"},
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    state = get_provider_auth_state("youtab")
    assert state is not None
    assert state["access_token"] == "youtab-global"


def test_provider_auth_state_returns_none_when_neither_has_it(profile_env):
    from youtab_agent_cli.auth import get_provider_auth_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={}))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    assert get_provider_auth_state("youtab") is None


# ---------------------------------------------------------------------------
# _load_provider_state — internal global fallback (issue #18594 follow-up)
#
# Several runtime helpers (notably ``resolve_youtab_runtime_credentials`` and
# ``resolve_youtab_access_token``) call ``_load_provider_state`` directly with
# a profile-loaded auth store rather than going through
# ``get_provider_auth_state``. Without the fallback wired into
# ``_load_provider_state`` itself, those helpers raise ``"Youtab is not
# logged into Youtab Portal"`` even though the user has a valid global Youtab
# login. These tests pin the per-provider shadowing into the helper.
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Classic mode — no fallback path should ever trigger
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Writes stay scoped to the profile
# ---------------------------------------------------------------------------


def test_write_credential_pool_targets_profile_not_global(profile_env):
    from youtab_agent_cli.auth import read_credential_pool, write_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-1",
            "label": "global",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-global",
        }],
    }))

    write_credential_pool("openrouter", [{
        "id": "prof-new",
        "label": "profile-new",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-profile-new",
    }])

    # Global auth.json unchanged.
    global_data = json.loads((profile_env["global"] / "auth.json").read_text(encoding="utf-8"))
    assert global_data["credential_pool"]["openrouter"][0]["id"] == "glob-1"

    # Profile auth.json holds the new entry.
    profile_data = json.loads((profile_env["profile"] / "auth.json").read_text(encoding="utf-8"))
    assert profile_data["credential_pool"]["openrouter"][0]["id"] == "prof-new"

    # Subsequent read returns profile (shadows global).
    assert [e["id"] for e in read_credential_pool("openrouter")] == ["prof-new"]




def test_auth_lock_reentrancy_is_scoped_after_profile_context_switch(profile_env):
    """Changing profile context cannot inherit another store's lock depth."""
    import youtab_agent_cli.auth as auth
    from youtab_constants import reset_youtab_home_override, set_youtab_home_override

    profile_b = profile_env["global"] / "profiles" / "reviewer"
    profile_b.mkdir(parents=True)
    profile_b_lock = profile_b / "auth.lock"

    with auth._auth_store_lock():
        holder_a = auth._auth_lock_holder_for(profile_env["profile"] / "auth.json")
        assert getattr(holder_a, "depth", 0) == 1

        token = set_youtab_home_override(profile_b)
        try:
            holder_b = auth._auth_lock_holder_for(profile_b / "auth.json")
            assert holder_b is not holder_a
            assert getattr(holder_b, "depth", 0) == 0
            assert not profile_b_lock.exists()

            with auth._auth_store_lock():
                assert profile_b_lock.exists()
                assert getattr(holder_b, "depth", 0) == 1
        finally:
            reset_youtab_home_override(token)

    assert getattr(holder_a, "depth", 0) == 0


# ---------------------------------------------------------------------------
# write_credential_pool — stale-snapshot cooldown merge
# ---------------------------------------------------------------------------


@pytest.fixture()
def classic_env(tmp_path, monkeypatch):
    """Classic single-root layout (YOUTAB_AGENT_HOME != ~/.youtab-agent-runtime, no profiles)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    youtab_home = tmp_path / "classic"
    youtab_home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(youtab_home))
    return youtab_home


def _pool_entry(**overrides) -> dict:
    entry = {
        "id": "cred-x",
        "label": "key-x",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-x",
    }
    entry.update(overrides)
    return entry




def test_write_pool_never_merges_cooldown_onto_reauthed_entry(classic_env):
    """A token change means re-auth: the old cooldown must never carry over.

    A fresh login intentionally clears the entry's status; resurrecting the
    stale cooldown onto the new credentials would bench a just-authorized key.
    """
    from youtab_agent_cli.auth import write_credential_pool

    _write(classic_env / "auth.json", _make_auth_store(pool={
        "openrouter": [_pool_entry(
            access_token="sk-old",
            last_status="exhausted",
            last_status_at=time.time() - 60,  # newer AND unexpired
            last_error_code=429,
        )],
    }))

    # Same entry id, freshly re-authed with a new token and cleared status.
    write_credential_pool("openrouter", [_pool_entry(access_token="sk-new")])

    data = json.loads((classic_env / "auth.json").read_text(encoding="utf-8"))
    persisted = data["credential_pool"]["openrouter"][0]
    assert persisted["access_token"] == "sk-new"
    assert persisted.get("last_status") != "exhausted"
    assert persisted.get("last_error_code") is None
