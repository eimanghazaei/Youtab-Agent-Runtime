"""The removed Youtab device grant cannot step up Remote Spending."""

from __future__ import annotations

import pytest

import youtab_agent_cli.auth as auth


# ---------------------------------------------------------------------------
# youtab_token_has_billing_scope
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# step_up_youtab_billing_scope
# ---------------------------------------------------------------------------


def test_step_up_refuses_without_device_flow_or_auth_store_mutation(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("device flow or credential mutation must not run")

    monkeypatch.setattr(auth, "_youtab_device_code_login", forbidden)
    monkeypatch.setattr(auth, "_save_auth_store", forbidden)
    with pytest.raises(auth.AuthError) as exc:
        auth.step_up_youtab_billing_scope()
    assert exc.value.code == "unsupported_connection"
    assert "unavailable" in str(exc.value)


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
