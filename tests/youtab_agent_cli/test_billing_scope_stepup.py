"""Tests for the Phase 2b billing:manage scope step-up (auth.py)."""

from __future__ import annotations

import pytest

import youtab_agent_cli.auth as auth
from youtab_agent_cli.auth import (
    YOUTAB_BILLING_MANAGE_SCOPE,
    youtab_token_has_billing_scope,
    step_up_youtab_billing_scope,
)


# ---------------------------------------------------------------------------
# youtab_token_has_billing_scope
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# step_up_youtab_billing_scope
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_persist(monkeypatch):
    """Neutralize the persistence side-effects so step-up tests are pure."""
    monkeypatch.setattr(auth, "_auth_store_lock", lambda: _NullCtx())
    monkeypatch.setattr(auth, "_load_auth_store", lambda: {})
    monkeypatch.setattr(auth, "_save_provider_state", lambda *a, **kw: None)
    monkeypatch.setattr(auth, "_save_auth_store", lambda *a, **kw: "auth.json")
    monkeypatch.setattr(auth, "_write_shared_youtab_state", lambda *a, **kw: None)
    monkeypatch.setattr(auth, "_sync_youtab_pool_from_auth_store", lambda: None)


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_step_up_requests_billing_scope_and_reuses_prior_urls(monkeypatch, _stub_persist):
    monkeypatch.setattr(
        auth,
        "get_provider_auth_state",
        lambda p: {
            "scope": "inference:invoke tool:invoke",
            "portal_base_url": "https://preview.example.com",
            "inference_base_url": "https://inf.example.com",
            "client_id": "youtab-cli",
        },
    )
    captured = {}

    def _fake_login(**kw):
        captured.update(kw)
        # Simulate the admin ticking the box → token comes back WITH the scope.
        return {"scope": "inference:invoke tool:invoke billing:manage", "access_token": "t"}

    monkeypatch.setattr(auth, "_youtab_device_code_login", _fake_login)

    granted = step_up_youtab_billing_scope()
    assert granted is True
    # Requested scope must include billing:manage, preserving prior scopes.
    assert YOUTAB_BILLING_MANAGE_SCOPE in captured["scope"].split()
    assert "inference:invoke" in captured["scope"].split()
    # Reuses the prior credential's deployment URLs (so a preview stays a preview).
    assert captured["portal_base_url"] == "https://preview.example.com"
    assert captured["client_id"] == "youtab-cli"


# ---------------------------------------------------------------------------
# on_verification callback plumbing (TUI surfaces the device-flow URL via this)
# ---------------------------------------------------------------------------




def test_device_login_fires_on_verification_before_polling(monkeypatch):
    """The removed device grant must reject before requesting or polling a code."""
    calls: list[str] = []
    monkeypatch.setattr(auth, "_request_device_code", lambda **kw: calls.append("request"))
    monkeypatch.setattr(auth, "_poll_for_token", lambda **kw: calls.append("poll"))
    with pytest.raises(auth.AuthError) as exc:
        auth._youtab_device_code_login(open_browser=False)
    assert exc.value.code == "unsupported_device_code"
    assert calls == []
