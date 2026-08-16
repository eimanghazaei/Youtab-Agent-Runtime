"""Tests for the non-agentic chat-model warning detector.

Hermes 3/4 are third-party Nous Research chat models resold through the Youtab
Portal. They are not tool-call tuned, so selecting one should raise a warning.

Prior to this check, the warning fired on any model whose name contained
``"hermes"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``hermes-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under that tag namespace.

``is_non_agentic_chat_model`` should match only the real Hermes 3 / Hermes 4
chat families, and must never match Youtab Portal catalog models.
"""

from __future__ import annotations

import pytest

from youtab_agent_cli.model_switch import (
    _NON_AGENTIC_CHAT_MODEL_WARNING,
    _check_non_agentic_model_warning,
    is_non_agentic_chat_model,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "NousResearch/Hermes-3-Llama-3.1-70B",
        "NousResearch/Hermes-3-Llama-3.1-405B",
        "hermes-3",
        "Hermes-3",
        "hermes-4",
        "hermes-4-405b",
        "hermes_4_70b",
        "openrouter/hermes3:70b",
        "openrouter/nousresearch/hermes-4-405b",
        "NousResearch/Hermes3",
        "hermes-3.1",
    ],
)
def test_matches_real_hermes_chat_models(model_name: str) -> None:
    assert is_non_agentic_chat_model(model_name), (
        f"expected {model_name!r} to be flagged as Hermes 3/4"
    )
    assert (
        _check_non_agentic_model_warning(model_name)
        == _NON_AGENTIC_CHAT_MODEL_WARNING
    )


@pytest.mark.parametrize(
    "model_name",
    [
        # Tool-capable local Modelfiles that merely carry the vendor tag.
        "hermes-brain:qwen3-14b-ctx16k",
        "qwen3:14b",
        # Youtab Portal catalog models are agentic and must never be flagged.
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
        "openai/gpt-5.5",
        "openai/gpt-5.4-mini",
        "google/gemini-3.1-pro-preview",
        "deepseek/deepseek-v4-pro",
        # Youtab product identity must never trip the third-party detector.
        "youtab-3",
        "youtab-4",
        "youtab-agent-runtime",
        "",
    ],
)
def test_does_not_match_agentic_or_youtab_models(model_name: str) -> None:
    assert not is_non_agentic_chat_model(model_name), (
        f"expected {model_name!r} NOT to be flagged as Hermes 3/4"
    )
    assert _check_non_agentic_model_warning(model_name) == ""
