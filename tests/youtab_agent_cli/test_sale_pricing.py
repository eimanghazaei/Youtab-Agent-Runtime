"""Sale UI pricing helpers: gateway pricing.original → discount chrome."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import youtab_agent_cli.models as models_mod
compute_sale_discount = models_mod.compute_sale_discount
fetch_models_with_pricing = models_mod.fetch_models_with_pricing


@pytest.mark.parametrize("nan", [float("nan"), Decimal("NaN"), "nan"])
def test_sale_discount_rejects_float_decimal_and_text_nan(nan):
    assert compute_sale_discount(nan, "1", {"prompt": "2", "completion": "1"}) is None
    assert compute_sale_discount("1", "1", {"prompt": nan, "completion": "1"}) is None


def test_sale_discount_preserves_decimal_prices_and_free_model_rule():
    assert compute_sale_discount(Decimal("0.5"), Decimal("1"), {
        "prompt": Decimal("1"), "completion": Decimal("2"),
    }) == (50, "1", "2")
    assert compute_sale_discount(Decimal("0"), Decimal("0"), {
        "prompt": Decimal("1"), "completion": Decimal("2"),
    }) is None






def test_fetch_models_with_pricing_copies_nested_original(monkeypatch):
    models_mod._pricing_cache.clear()
    payload = {
        "data": [
            {
                "id": "anthropic/claude-sonnet-5",
                "pricing": {
                    "prompt": "0.0000016",
                    "completion": "0.000008",
                    "input_cache_read": "0.00000016",
                    "original": {
                        "prompt": "0.000002",
                        "completion": "0.00001",
                        "input_cache_read": "0.0000002",
                    },
                },
            },
            {
                "id": "free/model",
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
    }
    body = json.dumps(payload).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda *a: False

    monkeypatch.setattr(
        models_mod,
        "_urlopen_model_catalog_request",
        lambda req, timeout=8.0: resp,
    )

    # Youtab Portal opts in via include_sale_original=True.
    result = fetch_models_with_pricing(
        api_key="sk-test",
        base_url="https://example.test",
        force_refresh=True,
        include_sale_original=True,
    )
    paid = result["anthropic/claude-sonnet-5"]
    assert paid["prompt"] == "0.0000016"
    assert paid["completion"] == "0.000008"
    assert paid["original"] == {
        "prompt": "0.000002",
        "completion": "0.00001",
        "input_cache_read": "0.0000002",
    }
    assert "original" not in result["free/model"]




def test_resolve_youtab_pricing_credentials_honors_inference_env_override(monkeypatch):
    """Staging profiles set YOUTAB_INFERENCE_BASE_URL — pricing must follow it.

    Without this, anonymous/failed-auth fallback hits prod and sale
    ``pricing.original`` never reaches Desktop/CLI pickers.
    """
    monkeypatch.setenv(
        "YOUTAB_INFERENCE_BASE_URL",
        "https://stg-inference-api.youtab.io/v1",
    )
    # Auth resolution fails / returns nothing — the env override must still win.
    monkeypatch.setattr(
        "youtab_agent_cli.auth.resolve_youtab_runtime_credentials",
        lambda: None,
    )
    api_key, base_url = models_mod._resolve_youtab_pricing_credentials()
    assert api_key == ""
    assert base_url == "https://stg-inference-api.youtab.io/v1"


