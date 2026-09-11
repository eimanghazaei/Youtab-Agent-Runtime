"""WAVE-30H — the canonical runtime identity-binding contract (pure unit layer).

Deterministic, offline. Pins the builder's resolution (execution / endpoint-class /
cost-policy / deferred digest / unresolved handling) and the append-only
highest-version / corrupt-guard semantics of the event reader.
"""
from __future__ import annotations

import types

from youtab_agent_cli import effective_binding as eb


def _ev(kind, payload):
    return types.SimpleNamespace(kind=kind, payload=payload)


def test_build_cloud_binding_is_metered_and_probe_free():
    b = eb.build_effective_binding(provider="openai", model="gpt-x", endpoint="")
    assert b["execution"] == "cloud"
    assert b["endpoint_class"] == "cloud"
    assert b["provider_cost_policy"] == "campaign_budget_eur"
    assert b["model_identifier_status"] == "resolved"
    assert b["model_ref"] == "openai/gpt-x"
    # No create-time probe: cloud digest is not applicable and never filled here.
    assert b["digest_status"] == "not_applicable"
    assert b["model_digest"] is None
    assert set(b) == set(eb.BINDING_FIELDS)


def test_build_local_ollama_binding_is_local_zero_and_digest_deferred():
    b = eb.build_effective_binding(
        provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434"
    )
    assert b["execution"] == "local"
    assert b["endpoint_class"] == "loopback"
    assert b["provider_cost_policy"] == "local_zero_verified"
    # The manifest digest requires a network probe → deferred, never resolved here.
    assert b["digest_status"] == "not_probed"
    assert b["model_digest"] is None


def test_build_unresolved_model_is_not_presented_as_effective():
    b = eb.build_effective_binding(provider="openai", model=None)
    assert b["model"] is None
    assert b["model_ref"] is None
    assert b["model_identifier_status"] == "OWNER_SELECTION_REQUIRED"


def test_build_case_normalizes_provider():
    b = eb.build_effective_binding(provider="OpenAI", model="m")
    assert b["provider"] == "openai"


def test_build_extra_link_fields_merge():
    b = eb.build_effective_binding(
        provider="openai", model="m", extra={"parent_run_id": "r-1", "subagent_id": "s-1"}
    )
    assert b["parent_run_id"] == "r-1" and b["subagent_id"] == "s-1"


def test_from_events_returns_highest_version():
    evs = [
        _ev("other", {}),
        _ev(eb.BINDING_EVENT, {"binding_version": 1, "provider": "a"}),
        _ev(eb.BINDING_EVENT, {"binding_version": 2, "provider": "b"}),
    ]
    assert eb.effective_binding_from_events(evs)["provider"] == "b"


def test_from_events_none_when_absent():
    assert eb.effective_binding_from_events([_ev("other", {})]) is None


def test_from_events_corrupt_version_is_flagged():
    evs = [_ev(eb.BINDING_EVENT, {"binding_version": "not-an-int", "provider": "a"})]
    assert eb.effective_binding_from_events(evs) == {"__corrupt__": True}


def test_classify_endpoint_local_vs_public():
    assert eb.classify_endpoint("http://127.0.0.1:11434") == "loopback"
    assert eb.classify_endpoint("http://10.0.0.5:11434") == "private"
    assert eb.classify_endpoint("https://api.openai.com") == "hostname"
    assert eb.classify_endpoint("") == "unavailable"
