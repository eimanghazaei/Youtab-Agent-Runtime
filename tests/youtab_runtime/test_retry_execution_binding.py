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

from youtab_agent_cli import effective_binding as eb
from youtab_agent_cli.web_routers import runtime as R


def _binding(**over):
    base = {
        "binding_version": 1, "provider": "ollama", "model": "qwen",
        "model_ref": "ollama/qwen", "model_identifier_status": "resolved",
        "execution": "local", "endpoint_class": "loopback",
        "provider_cost_policy": "local_zero_verified",
        "digest_status": "not_probed", "model_digest": None,
        "bound_at": "2026-09-11T00:00:00Z",
    }
    base.update(over)
    return base


def _task(model_override=None, provider_override=None):
    return types.SimpleNamespace(
        model_override=model_override, provider_override=provider_override
    )


def _ev(kind, payload):
    return types.SimpleNamespace(kind=kind, payload=payload)


def test_inherits_full_binding_from_row_and_events():
    task = _task(model_override="ollama/qwen", provider_override="ollama")
    binding = _binding()
    events = [
        _ev(R._ENGINE_EVENT, {"profile_id": "eco.v01", "public_label": "ECO"}),
        _ev(R._LIMITS_EVENT, {"max_cost_eur": 3, "worker_attempt_limit": 1}),
        _ev(eb.BINDING_EVENT, binding),
    ]
    b = R._retry_execution_binding(task, events)
    assert b["model_override"] == "ollama/qwen"
    assert b["provider_override"] == "ollama"
    assert b["engine_selection"] == {"profile_id": "eco.v01", "public_label": "ECO"}
    assert b["limits"] == {"max_cost_eur": 3, "worker_attempt_limit": 1}
    # WAVE-30H: the immutable binding is inherited UNCHANGED (same version).
    assert b["effective_binding"] == binding


def test_unbound_original_inherits_nothing_but_is_not_an_error():
    # A LEGACY original with no engine/grant/binding (NOT attested) is reproduced
    # faithfully — not coerced into a default, not refused (the legacy-safe path).
    b = R._retry_execution_binding(_task(), [])
    assert b == {"model_override": None, "provider_override": None,
                 "engine_selection": None, "limits": None,
                 "effective_binding": None}


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


def test_attested_run_without_binding_fails_closed():
    # A run that carries an engine/grant event (attested) but NO binding event is a
    # corrupt/legacy state that cannot be safely inherited — refuse the retry.
    events = [_ev(R._ENGINE_EVENT, {"profile_id": "eco.v01", "public_label": "ECO"})]
    with pytest.raises(HTTPException) as ei:
        R._retry_execution_binding(_task(), events)
    assert ei.value.status_code == 422
    assert ei.value.detail["error"] == "retry_binding_missing"


def test_corrupt_binding_fails_closed():
    events = [_ev(eb.BINDING_EVENT, {"binding_version": "x", "provider": "ollama"})]
    with pytest.raises(HTTPException) as ei:
        R._retry_execution_binding(_task(), events)
    assert ei.value.status_code == 422
    assert ei.value.detail["error"] == "retry_binding_invalid"


def test_resolved_binding_pins_child_row_when_no_row_override():
    # A no-row-override original whose binding resolved a concrete model pins the
    # child row FROM the binding, so the child dispatches on the same substrate.
    events = [_ev(eb.BINDING_EVENT, _binding(provider="openai", model="gpt-x",
                                             model_ref="openai/gpt-x",
                                             provider_cost_policy="campaign_budget_eur",
                                             execution="cloud", endpoint_class="cloud",
                                             digest_status="not_applicable"))]
    b = R._retry_execution_binding(_task(), events)
    assert b["model_override"] == "openai/gpt-x"
    assert b["provider_override"] == "openai"


def test_config_change_after_original_does_not_drift():
    # The inherited binding names the ORIGINAL identity; the child row is pinned from
    # it regardless of any later config change (the pin is config-independent).
    binding = _binding(provider="openai", model="gpt-orig", model_ref="openai/gpt-orig",
                       provider_cost_policy="campaign_budget_eur", execution="cloud",
                       endpoint_class="cloud", digest_status="not_applicable")
    b = R._retry_execution_binding(_task(), [_ev(eb.BINDING_EVENT, binding)])
    assert b["model_override"] == "openai/gpt-orig"
    assert b["effective_binding"]["model"] == "gpt-orig"  # inherited, not re-resolved


def test_unresolved_binding_does_not_pin_but_is_not_an_error():
    # An unresolved/config-default binding has no concrete identity to pin → the
    # child re-resolves as the original did (legacy-safe), never refused.
    binding = _binding(provider="ollama", model=None, model_ref=None,
                       model_identifier_status="OWNER_SELECTION_REQUIRED")
    b = R._retry_execution_binding(_task(), [_ev(eb.BINDING_EVENT, binding)])
    assert b["model_override"] is None
    assert b["provider_override"] is None
    assert b["effective_binding"]["model"] is None
