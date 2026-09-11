"""WAVE-30H — the canonical runtime identity-binding contract (pure unit layer).

Deterministic, offline. Pins the builder's resolution (execution / endpoint-class /
cost-policy / deferred digest / unresolved handling) and the append-only
highest-version / corrupt-guard semantics of the event reader.
"""
from __future__ import annotations

import types

import pytest

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


def test_rescope_preserves_substrate_and_reruns_scope():
    parent = eb.build_effective_binding(
        provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434",
        run_id="run-A", root_run_id="run-A", tenant="t1", workspace="w1",
    )
    child = eb.rescope_binding(parent, run_id="run-B", tenant="t1", workspace="w1")
    # New run scope, preserved lineage root, and the child self-verifies.
    assert child["run_id"] == "run-B"
    assert child["root_run_id"] == "run-A"  # lineage preserved
    assert eb.verify_binding(child)
    # SUBSTRATE is invariant (digest excludes run/tenant/workspace) => provably no
    # provider/model/endpoint drift across the re-scope.
    assert eb.binding_digest(child) == eb.binding_digest(parent)
    # ...but the per-run hash DIFFERS (run_id is a hashed identity field), so a
    # child binding can never be replayed as the parent's and vice versa.
    assert child["binding_hash"] != parent["binding_hash"]
    # The input is never mutated.
    assert parent["run_id"] == "run-A"


def test_rescope_can_move_workspace_and_tenant_scope():
    parent = eb.build_effective_binding(
        provider="ollama", model="m", run_id="run-A", tenant="t1", workspace="w1",
    )
    child = eb.rescope_binding(parent, run_id="run-B", tenant="t2", workspace="w2")
    assert (child["tenant"], child["workspace"]) == ("t2", "w2")
    assert eb.verify_binding(child)
    assert eb.binding_digest(child) == eb.binding_digest(parent)


def test_rescope_defaults_root_to_new_run_when_parent_has_none():
    parent = eb.build_effective_binding(provider="ollama", model="m")  # no run scope
    child = eb.rescope_binding(parent, run_id="run-B")
    assert child["run_id"] == "run-B"
    assert child["root_run_id"] == "run-B"
    assert eb.verify_binding(child)


def test_rescope_refuses_non_self_verifying_binding():
    # WAVE-30H #4: a tampered-at-rest parent (fields changed, stale hash) must NOT be
    # laundered into a fresh valid child hash.
    parent = eb.build_effective_binding(provider="ollama", model="m", run_id="run-A")
    tampered = dict(parent)
    tampered["model"] = "evil"  # hash no longer matches
    assert not eb.verify_binding(tampered)
    with pytest.raises(ValueError, match="does not self-verify"):
        eb.rescope_binding(tampered, run_id="run-B")


def test_attested_digest_is_hashed_and_tamper_evident():
    # WAVE-30H #7: a pinned model_digest is covered by binding_hash.
    b = eb.build_effective_binding(
        provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434",
        run_id="run-A", model_digest="a" * 64,
    )
    assert b["digest_status"] == "attested"
    assert b["model_digest"] == "a" * 64
    assert eb.verify_binding(b)
    b["model_digest"] = "b" * 64  # mutate after hashing
    assert not eb.verify_binding(b)


def test_attested_digest_excluded_from_substrate_digest():
    # WAVE-30H #7 must NOT re-break #1: the SUBSTRATE digest is invariant whether or
    # not a model_digest is attested (same substrate -> same binding_digest), so a
    # preflight (which may probe) and the pure create still match atomically.
    common = dict(provider="ollama", model="qwen:tag",
                  endpoint="http://127.0.0.1:11434", run_id="run-A")
    attested = eb.build_effective_binding(model_digest="a" * 64, **common)
    not_probed = eb.build_effective_binding(**common)
    assert eb.binding_digest(attested) == eb.binding_digest(not_probed)
    # ...but the per-run hashes DIFFER (the digest is a hashed identity field).
    assert attested["binding_hash"] != not_probed["binding_hash"]
