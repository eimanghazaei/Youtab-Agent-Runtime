"""WAVE-30H #3 — managed runs must NOT escape the sealed substrate via fallback.

``try_activate_fallback`` swaps ``agent.provider``/``model``/``base_url``/``client``
to the next provider in the fallback chain. For a MANAGED run that is bound to a
sealed substrate (validated at admission), such a switch would drift OFF the bound
identity with no signed rebind authorization — the signed rebind ACCEPT path is
intentionally deferred, so any managed fallback must FAIL CLOSED. The guard is the
first statement in the function, gated on ``_admitted_command`` (set at admission,
inherited by delegated children), so non-managed/standalone runs keep full fallback
resilience (proven by the existing tests/run_agent fallback suite, whose agents
carry no ``_admitted_command``).
"""
from __future__ import annotations

import types

from agent.chat_completion_helpers import try_activate_fallback
from agent.error_classifier import FailoverReason


def _managed_agent():
    return types.SimpleNamespace(
        _admitted_command=object(),  # went through managed admission
        provider="ollama", model="qwen:test", base_url="http://127.0.0.1:11434",
        client=object(),
        _fallback_chain=[{"provider": "openai", "model": "gpt-x",
                          "base_url": "https://api.openai.com"}],
        _fallback_index=0,
        _fallback_activated=False,
    )


def test_managed_agent_fallback_fails_closed(monkeypatch):
    # The guard is gated on MANAGED trust mode AND the per-agent admitted context.
    from youtab_runtime import managed_execution as mx
    monkeypatch.setattr(mx, "current_trust_mode", lambda: mx.TrustMode.MANAGED)

    # A provider-client build on this path would prove the sealed substrate escaped.
    import agent.auxiliary_client as ac

    def _boom(*a, **k):  # noqa: ANN001
        raise AssertionError("managed fallback must not resolve a provider client")

    monkeypatch.setattr(ac, "resolve_provider_client", _boom, raising=False)

    agent = _managed_agent()
    before = (agent.provider, agent.model, agent.base_url, agent.client)
    for reason in (FailoverReason.rate_limit, FailoverReason.billing, None):
        assert try_activate_fallback(agent, reason=reason) is False
    # The sealed substrate is UNCHANGED — no drift, no client swap.
    assert (agent.provider, agent.model, agent.base_url, agent.client) == before
    assert agent._fallback_index == 0  # chain not advanced

# Standalone (no _admitted_command) fallback SWITCHING — the no-regression side of
# this guard — is covered end-to-end by tests/run_agent/test_fallback_*.py, whose
# agents carry no _admitted_command and so never hit the managed guard.
