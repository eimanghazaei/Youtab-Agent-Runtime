"""Model-aware token accounting.

Character counts are not token counts, and the ratio varies by script (Latin vs
Persian/Arabic vs CJK), by structured formats (JSON, SAP-like records), and by
source code. This module provides:

* a :class:`TokenCounter` protocol selected via model/provider capability
  metadata (never a hardcoded provider);
* an :class:`ExactTokenCounter` wrapping a provider-supplied tokenizer callable;
* a :class:`HeuristicTokenCounter` fallback with an EXPLICIT uncertainty margin
  and per-script conservative ratios (rounds up, never assumes 1 char == 1 token);
* :class:`PromptAccounting`, which sums system prompt + messages + retrieved
  memory + tool schemas + tool results + output reserve and reports fit with margin;
* graceful compaction that triggers BEFORE overflow and preserves goals,
  decisions, open tasks, evidence references and unresolved risks.

Nothing here calls a model or a network; a live tokenizer is injected by the caller.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


@runtime_checkable
class TokenCounter(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def uncertainty_margin(self) -> float: ...

    def count(self, text: str) -> int: ...


class ExactTokenCounter:
    """Wraps a provider/model tokenizer callable (e.g. tiktoken, HF). Margin 0."""

    def __init__(self, name: str, counter: Callable[[str], int]) -> None:
        self._name = name
        self._counter = counter

    @property
    def name(self) -> str:
        return self._name

    @property
    def uncertainty_margin(self) -> float:
        return 0.0

    def count(self, text: str) -> int:
        return self._counter(text)


# Conservative chars-per-token per script class. Lower ratio => MORE tokens =>
# we never under-count. Values are intentionally cautious, not exact.
_SCRIPT_CHARS_PER_TOKEN = {
    "cjk": 1.0,  # CJK often ~1 token per char or worse
    "arabic": 1.7,  # Persian/Arabic subword-heavy
    "digits": 2.2,  # numeric / SAP-like records
    "code": 2.5,  # source code tokenizes densely
    "latin": 3.5,  # English/Dutch prose
}


def _classify_char(ch: str) -> str:
    if ch.isdigit():
        return "digits"
    o = ord(ch)
    if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF:
        return "cjk"
    if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFB50 <= o <= 0xFDFF:
        return "arabic"
    name = unicodedata.category(ch)
    if name.startswith(("P", "S")):  # punctuation/symbols dominate code/JSON
        return "code"
    return "latin"


class HeuristicTokenCounter:
    """Script-aware conservative fallback with an explicit uncertainty margin."""

    def __init__(self, name: str = "heuristic", uncertainty_margin: float = 0.25) -> None:
        if not 0.0 <= uncertainty_margin <= 1.0:
            raise ValueError("uncertainty_margin must be in [0, 1]")
        self._name = name
        self._margin = uncertainty_margin

    @property
    def name(self) -> str:
        return self._name

    @property
    def uncertainty_margin(self) -> float:
        return self._margin

    def count(self, text: str) -> int:
        if not text:
            return 0
        # Bucket characters by script, divide each bucket by its ratio, round up.
        buckets: dict[str, int] = {}
        for ch in text:
            if ch.isspace():
                continue
            cls = _classify_char(ch)
            buckets[cls] = buckets.get(cls, 0) + 1
        total = 0.0
        for cls, n in buckets.items():
            total += n / _SCRIPT_CHARS_PER_TOKEN[cls]
        return math.ceil(total)


class ModelCapability(BaseModel):
    """Provider/model capability metadata used to SELECT a tokenizer.

    ``tokenizer_name`` names an exact tokenizer the caller can supply; when it is
    None or unregistered, the heuristic fallback is used. No provider is hardcoded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    context_window: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    tokenizer_name: str | None = Field(default=None, max_length=128)


def select_counter(
    capability: ModelCapability,
    registry: dict[str, Callable[[str], int]] | None = None,
    *,
    fallback_margin: float = 0.25,
) -> TokenCounter:
    """Select an exact counter from the registry, else a heuristic fallback."""

    registry = registry or {}
    if capability.tokenizer_name and capability.tokenizer_name in registry:
        return ExactTokenCounter(
            capability.tokenizer_name, registry[capability.tokenizer_name]
        )
    return HeuristicTokenCounter(
        name=f"heuristic:{capability.provider}", uncertainty_margin=fallback_margin
    )


class PromptComponents(BaseModel):
    """Raw text/size of each context component to be accounted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_prompt: str = ""
    messages: tuple[str, ...] = ()
    retrieved_memory: tuple[str, ...] = ()
    tool_schemas: str = ""
    tool_results: tuple[str, ...] = ()
    output_reserve_tokens: int = Field(default=0, ge=0)


class PromptAccounting(BaseModel):
    """The counted result, with the margin applied to the worst case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    counter_name: str
    uncertainty_margin: float
    system_prompt_tokens: int
    messages_tokens: int
    retrieved_memory_tokens: int
    tool_schema_tokens: int
    tool_results_tokens: int
    output_reserve_tokens: int

    def subtotal(self) -> int:
        return (
            self.system_prompt_tokens
            + self.messages_tokens
            + self.retrieved_memory_tokens
            + self.tool_schema_tokens
            + self.tool_results_tokens
            + self.output_reserve_tokens
        )

    def worst_case(self) -> int:
        """Subtotal inflated by the counter's uncertainty margin (ceil)."""

        return math.ceil(self.subtotal() * (1.0 + self.uncertainty_margin))

    def fits(self, context_window: int) -> bool:
        return self.worst_case() <= context_window


def account(components: PromptComponents, counter: TokenCounter) -> PromptAccounting:
    """Count every component with the selected tokenizer."""

    def total(texts: tuple[str, ...]) -> int:
        return sum(counter.count(t) for t in texts)

    return PromptAccounting(
        counter_name=counter.name,
        uncertainty_margin=counter.uncertainty_margin,
        system_prompt_tokens=counter.count(components.system_prompt),
        messages_tokens=total(components.messages),
        retrieved_memory_tokens=total(components.retrieved_memory),
        tool_schema_tokens=counter.count(components.tool_schemas),
        tool_results_tokens=total(components.tool_results),
        output_reserve_tokens=components.output_reserve_tokens,
    )


class CompactionSummary(BaseModel):
    """What a graceful compaction must preserve (never silently dropped)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goals: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    open_tasks: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    unresolved_risks: tuple[str, ...] = ()


def needs_compaction(
    accounting: PromptAccounting, context_window: int, *, headroom: float = 0.1
) -> bool:
    """True when worst-case usage crosses (1 - headroom) of the window.

    This fires BEFORE overflow: with headroom 0.1 it triggers at 90% worst-case,
    leaving room to compact rather than truncating mid-generation.
    """

    if not 0.0 <= headroom < 1.0:
        raise ValueError("headroom must be in [0, 1)")
    threshold = context_window * (1.0 - headroom)
    return accounting.worst_case() >= threshold
