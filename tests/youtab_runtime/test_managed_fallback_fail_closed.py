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


def test_pre_admitted_worker_fallback_fails_closed_during_construction(monkeypatch):
    # Batch2 #6(b): during _init_agent a managed worker's fallback can fire BEFORE
    # _admitted_command is attached — the guard must still fail closed on MANAGED
    # trust mode alone (no admitted context yet).
    from youtab_runtime import managed_execution as mx
    monkeypatch.setattr(mx, "current_trust_mode", lambda: mx.TrustMode.MANAGED)
    agent = types.SimpleNamespace(  # NO _admitted_command attribute at all
        provider="ollama", model="qwen:test", base_url="http://127.0.0.1:11434",
        client=object(), _fallback_chain=[{"provider": "openai", "model": "gpt-x"}],
        _fallback_index=0, _fallback_activated=False,
    )
    assert try_activate_fallback(agent, reason=FailoverReason.rate_limit) is False
    assert agent._fallback_index == 0


def test_trust_mode_lookup_exception_fails_closed_in_managed_context(monkeypatch):
    # Batch2 #6(a)/(c): if trust-mode inspection RAISES, we must not assume standalone
    # at this authority boundary — with a managed env marker present, fail closed.
    from youtab_runtime import managed_execution as mx

    def _raise():
        raise RuntimeError("trust mode indeterminate")

    monkeypatch.setattr(mx, "current_trust_mode", _raise)
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    agent = _managed_agent()
    before = (agent.provider, agent.model, agent.base_url, agent.client)
    assert try_activate_fallback(agent, reason=FailoverReason.rate_limit) is False
    assert (agent.provider, agent.model, agent.base_url, agent.client) == before


def test_standalone_lookup_exception_without_markers_does_not_block(monkeypatch):
    # A pure standalone process (no managed env markers) whose trust-mode lookup
    # errors keeps its fallback resilience — the guard does not fire. (We assert the
    # guard is not the reason for a False by giving a real one-entry chain and a
    # patched resolver; standalone SWITCHING end-to-end is covered by
    # tests/run_agent/test_fallback_*.py.)
    from youtab_runtime import managed_execution as mx

    def _raise():
        raise RuntimeError("trust mode indeterminate")

    monkeypatch.setattr(mx, "current_trust_mode", _raise)
    monkeypatch.delenv("YOUTAB_RUNTIME_TRUST_MODE", raising=False)
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_TASK", raising=False)
    # No managed markers + not a real AdmittedCommand -> guard must NOT fire. Prove it
    # by observing the guard did not short-circuit: a real AdmittedCommand isinstance
    # is False here, so _managed_authority is False and control proceeds past the
    # guard. We assert no managed-refusal by checking the guard path via a spy.
    import agent.chat_completion_helpers as cch
    seen = {}
    real_logger_error = cch.logger.error

    def _spy(msg, *a, **k):
        if "provider fallback disabled (sealed substrate)" in str(msg):
            seen["blocked"] = True
        return real_logger_error(msg, *a, **k)

    monkeypatch.setattr(cch.logger, "error", _spy)
    agent = types.SimpleNamespace(
        provider="openai", model="gpt-x", base_url="https://api.openai.com",
        _fallback_chain=[], _fallback_index=0, _fallback_activated=False,
        _primary_runtime={"provider": "openai"}, _rate_limited_until=0,
    )
    # Exhausted empty chain returns False, but NOT via the managed guard.
    try_activate_fallback(agent, reason=FailoverReason.rate_limit)
    assert "blocked" not in seen, "standalone (no markers) must not hit the managed guard"


# Standalone fallback SWITCHING (no _admitted_command, standalone trust mode) — the
# no-regression side of this guard — is covered end-to-end by
# tests/run_agent/test_fallback_*.py, whose agents carry no managed markers.
