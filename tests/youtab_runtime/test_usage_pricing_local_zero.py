"""WAVE-30D §B2 — verified-local exactly-€0 accounting (fail-closed, non-weakening).

Proves the strict local-zero classifier and the ``estimate_usage_cost`` branch:

* a local-inference provider on a verified-local endpoint prices at EXACTLY €0
  with a first-class ``local_zero`` / ``verified_local`` result;
* a remote/public endpoint, a bare hostname, a non-local provider, or an absent
  endpoint can NEVER claim the local-zero policy — an unknown cloud model still
  fails closed (``unknown``, amount ``None``), never inheriting €0;
* the campaign-budget pricing bridge returns ``Decimal("0")`` for local-zero and
  still RAISES ``PricingUnavailable`` for an unpriceable non-local call.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from agent.usage_pricing import (
    CanonicalUsage,
    classify_local_zero,
    estimate_usage_cost,
    is_verified_local_zero_endpoint,
)

_OLLAMA = "youtab-qwen35-9b-agent-64k:latest"
_USAGE = CanonicalUsage(input_tokens=1000, output_tokens=500)


# --- the strict classifier --------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434",
    "http://localhost:11434",
    "http://[::1]:11434",
    "http://192.168.1.50:11434",   # RFC1918 private
    "http://10.0.0.5:11434",       # RFC1918 private
    "http://169.254.10.10:11434",  # link-local
    "http://100.108.46.86:11434",  # Tailscale CGNAT
    "127.0.0.1:11434",             # scheme added
])
def test_verified_local_endpoint_accepts_local_targets(url):
    assert is_verified_local_zero_endpoint(url) is True


@pytest.mark.parametrize("url", [
    "http://8.8.8.8:11434",              # public IP
    "https://api.openai.com/v1",         # public host (name)
    "http://evil.example.com:11434",     # bare hostname -> never trusted
    "http://ollama.local:11434",         # dot-less-ish hostname (not an IP literal)
    "http://user:pass@127.0.0.1:11434",  # credentials-in-URL
    "",                                   # empty
    "not a url",
])
def test_verified_local_endpoint_rejects_remote_or_bogus(url):
    assert is_verified_local_zero_endpoint(url) is False


def test_classify_requires_both_provider_and_endpoint():
    assert classify_local_zero("ollama", "http://127.0.0.1:11434") is True
    assert classify_local_zero("vllm", "http://10.0.0.9:8000/v1") is True
    # Non-local provider on a loopback endpoint is NOT local-zero.
    assert classify_local_zero("openai", "http://127.0.0.1:11434") is False
    assert classify_local_zero("anthropic", "http://127.0.0.1:11434") is False
    # Local provider on a remote endpoint is NOT local-zero.
    assert classify_local_zero("ollama", "http://8.8.8.8:11434") is False
    assert classify_local_zero("ollama", None) is False
    assert classify_local_zero(None, "http://127.0.0.1:11434") is False


# --- estimate_usage_cost branch --------------------------------------------

def test_local_ollama_prices_exactly_zero():
    res = estimate_usage_cost(_OLLAMA, _USAGE, provider="ollama",
                              base_url="http://127.0.0.1:11434")
    assert res.amount_usd == Decimal("0")
    assert res.status == "local_zero"
    assert res.source == "verified_local"


def test_unknown_cloud_model_still_fails_closed_not_zero():
    # A remote/unknown model with no pricing entry must stay unknown (fail closed),
    # never inherit €0.
    res = estimate_usage_cost("some-unlisted-model", _USAGE, provider="ollama",
                              base_url="http://8.8.8.8:11434")
    assert res.amount_usd is None
    assert res.status == "unknown"


def test_local_provider_but_no_endpoint_is_not_zero():
    res = estimate_usage_cost(_OLLAMA, _USAGE, provider="ollama", base_url=None)
    assert res.status == "unknown"
    assert res.amount_usd is None


def test_non_local_provider_on_loopback_not_hijacked_to_zero():
    # provider must be a local-inference server; a cloud provider name never gets
    # the local-zero branch even if pointed at loopback.
    res = estimate_usage_cost("mystery-model", _USAGE, provider="openai",
                              base_url="http://127.0.0.1:11434")
    assert res.status != "local_zero"
    assert res.source != "verified_local"


# --- campaign-budget pricing bridge ----------------------------------------

def test_budget_bridge_returns_zero_for_local_and_raises_for_remote():
    from youtab_runtime.campaign_budget import PricingUnavailable, _usd_cost_or_fail

    got = _usd_cost_or_fail(_OLLAMA, input_tokens=1000, output_tokens=500,
                            provider="ollama", base_url="http://127.0.0.1:11434")
    assert got == Decimal("0")

    with pytest.raises(PricingUnavailable):
        _usd_cost_or_fail("some-unlisted-model", input_tokens=1000, output_tokens=500,
                          provider="ollama", base_url="http://8.8.8.8:11434")
