"""WAVE-30H — a retry MUST inherit the original run's complete execution binding.

Greptile blocker #2 (unit layer): ``_retry_execution_binding`` assembles the
authoritative binding a retry child inherits — resolved provider/model pin (row)
+ branded engine selection + clamped cost policy — and FAILS CLOSED (HTTP 422)
when that recorded binding is invalid or internally conflicting, so a retry can
never silently fall back to a default provider/model. These are the pure,
dispatch-free contract tests; the HTTP create->retry inheritance + atomic-persist
fail-closed cases live in ``tests/youtab_agent_cli/test_runtime_api.py``.
"""
from __future__ import annotations

import types

import pytest
from fastapi import HTTPException

from youtab_agent_cli.web_routers import runtime as R


def _task(model_override=None, provider_override=None):
    return types.SimpleNamespace(
        model_override=model_override, provider_override=provider_override
    )


def _ev(kind, payload):
    return types.SimpleNamespace(kind=kind, payload=payload)


def test_inherits_full_binding_from_row_and_events():
    task = _task(model_override="ollama/qwen", provider_override="ollama")
    events = [
        _ev(R._ENGINE_EVENT, {"profile_id": "eco.v01", "public_label": "ECO"}),
        _ev(R._LIMITS_EVENT, {"max_cost_eur": 3, "worker_attempt_limit": 1}),
    ]
    b = R._retry_execution_binding(task, events)
    assert b["model_override"] == "ollama/qwen"
    assert b["provider_override"] == "ollama"
    assert b["engine_selection"] == {"profile_id": "eco.v01", "public_label": "ECO"}
    assert b["limits"] == {"max_cost_eur": 3, "worker_attempt_limit": 1}


def test_unbound_original_inherits_nothing_but_is_not_an_error():
    # An original that legitimately ran on the worker default (no engine, no
    # override) is reproduced faithfully — NOT coerced into some new default.
    b = R._retry_execution_binding(_task(), [])
    assert b == {"model_override": None, "provider_override": None,
                 "engine_selection": None, "limits": None}


def test_provider_without_model_fails_closed():
    # A provider pin with no concrete model cannot resolve -> refuse, never let it
    # fall through to a default model.
    with pytest.raises(HTTPException) as ei:
        R._retry_execution_binding(_task(provider_override="ollama"), [])
    assert ei.value.status_code == 422
    assert ei.value.detail["error"] == "retry_binding_invalid"


def test_engine_selection_missing_profile_id_fails_closed():
    task = _task(model_override="ollama/qwen", provider_override="ollama")
    events = [_ev(R._ENGINE_EVENT, {"profile_id": "", "public_label": "ECO"})]
    with pytest.raises(HTTPException) as ei:
        R._retry_execution_binding(task, events)
    assert ei.value.status_code == 422
    assert ei.value.detail["error"] == "retry_binding_invalid"


def test_whitespace_only_override_is_treated_as_absent():
    b = R._retry_execution_binding(_task(model_override="   "), [])
    assert b["model_override"] is None


def test_limits_helper_returns_none_when_absent():
    assert R._limits_from_events([_ev("other", {"x": 1})]) is None
