"""Tests for the Youtab-Youtab-3/4 non-agentic warning detector.

Prior to this check, the warning fired on any model whose name contained
``"youtab"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``youtab-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under the "youtab" tag namespace.

``is_youtab_youtab_non_agentic`` should only match the actual Youtab B.V.
Youtab-3 / Youtab-4 chat family.
"""

from __future__ import annotations

import pytest

from youtab_agent_cli.model_switch import (
    _YOUTAB_AGENT_MODEL_WARNING,
    _check_youtab_model_warning,
    is_youtab_youtab_non_agentic,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "YoutabBV/Youtab-3-Llama-3.1-70B",
        "YoutabBV/Youtab-3-Llama-3.1-405B",
        "youtab-3",
        "Youtab-3",
        "youtab-4",
        "youtab-4-405b",
        "youtab_4_70b",
        "openrouter/youtab3:70b",
        "openrouter/youtab/youtab-4-405b",
        "YoutabBV/Youtab3",
        "youtab-3.1",
    ],
)
def test_matches_real_youtab_youtab_chat_models(model_name: str) -> None:
    assert is_youtab_youtab_non_agentic(model_name), (
        f"expected {model_name!r} to be flagged as Youtab Youtab 3/4"
    )
    assert _check_youtab_model_warning(model_name) == _YOUTAB_AGENT_MODEL_WARNING


