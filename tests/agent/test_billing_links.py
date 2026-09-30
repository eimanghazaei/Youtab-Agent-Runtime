"""Tests for provider-agnostic billing recovery links (agent/billing_links.py).

Behavior/invariant tests — no snapshotting of the exact URL strings beyond the
few that are the whole point of the mapping (the host they must land on).
"""

from __future__ import annotations

import pytest

from agent.billing_links import (
    BillingBlock,
    build_billing_block,
    is_youtab_inference_route,
)






def test_is_youtab_inference_route_helper():
    assert is_youtab_inference_route("youtab", "") is True
    assert is_youtab_inference_route("", "https://api.youtab.io/v1") is True
    assert is_youtab_inference_route("openai", "https://api.openai.com/v1") is False


@pytest.mark.parametrize("host", ["api.youtab.io", "inference-api.youtab.io"])
def test_youtab_billing_link_classifies_exact_compatibility_hosts(host):
    assert is_youtab_inference_route("custom", f"https://{host}/v1")
    assert build_billing_block(provider="custom", base_url=f"https://{host}/v1", model="model").is_youtab
    assert not is_youtab_inference_route("custom", f"https://child.{host}/v1")
    assert not is_youtab_inference_route("custom", f"https://{host}.attacker.test/v1")


def test_known_provider_by_slug_resolves_label_and_url():
    block = build_billing_block(provider="openai", base_url="", model="gpt-5")
    assert block.is_youtab is False
    assert block.provider_label == "OpenAI"
    assert block.billing_url is not None
    assert "openai.com" in block.billing_url










def test_to_dict_round_trips_all_fields():
    block = build_billing_block(provider="openai", base_url="", model="gpt-5")
    data = block.to_dict()
    assert set(data) == {
        "provider",
        "provider_label",
        "model",
        "billing_url",
        "is_youtab",
        "message",
    }
    assert isinstance(block, BillingBlock)
