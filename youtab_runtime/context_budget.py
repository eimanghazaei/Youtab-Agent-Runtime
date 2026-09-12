"""WAVE-30E — task-class prompt budgets + per-category token accounting.

Two responsibilities, both pure (no agent/provider imports), so they run in the
deterministic CI gate and can be reused by (a) the runtime instrumentation that
persists a per-call category breakdown and (b) the offline Phase-2 profiler.

1. **Adaptive, task-class-aware budgets** (Owner clarification): the initial
   serialized prompt has a TARGET range per task class — it is an engineering
   target, never a destructive truncation cap, and it is fully decoupled from the
   model's ``num_ctx`` capability (a 4K–8K prompt target does NOT remove the
   authorized 64K context).

2. **Category accounting**: split an assembled request into token counts by
   category (system/policy, skills index, tool schemas, conversation/history,
   task, response schema, …) so the 21,421-token canary can be explained and any
   reduction attributed to a specific category — with exact CHAR counts and a
   labelled char/4 token ESTIMATE (the exact serialized token count is the
   provider's own ``prompt_eval_count``, captured at run time by
   :mod:`youtab_runtime.model_timings`; offline we never claim provider-exact
   token numbers).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Same provider-agnostic char/4 rule the tool-search gate already uses, so the
# estimate is consistent across the codebase. Labelled as an estimate everywhere.
CHARS_PER_TOKEN = 4.0


# --------------------------------------------------------------------------- #
# Adaptive task-class budgets (targets, NOT truncation limits)                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Budget:
    """A target range for the INITIAL serialized prompt of a task class.

    ``target_min``/``target_max`` are engineering targets in estimated tokens.
    ``ctx_capability`` is the model context the class may legitimately use — the
    full authorized 64K is always available; the budget never lowers it.
    """

    task_class: str
    target_min: int
    target_max: int
    ctx_capability: int


# Owner-approved adaptive ladder. The prompt-token TARGET and the num_ctx
# CAPABILITY are independent axes: a simple task targets a small prompt but the
# 64K capability is always retained for tasks that need it.
TASK_CLASS_BUDGETS: Dict[str, Budget] = {
    # simple conversational / simple canary
    "simple": Budget("simple", 4_000, 8_000, 64_000),
    # standard single agent task
    "standard": Budget("standard", 4_000, 8_000, 64_000),
    # multi-step / tool-heavy
    "tool_heavy": Budget("tool_heavy", 8_000, 16_000, 64_000),
    # memory / RAG heavy
    "memory_rag": Budget("memory_rag", 16_000, 32_000, 64_000),
    # document / long-context
    "document": Budget("document", 32_000, 64_000, 64_000),
}

# A simple single-step task (e.g. a one-shot canary) maps to the ``simple`` class.
CANARY_TASK_CLASS = "simple"


def budget_for(task_class: str) -> Budget:
    """Return the budget for a task class, defaulting to ``standard``."""
    return TASK_CLASS_BUDGETS.get(task_class, TASK_CLASS_BUDGETS["standard"])


def within_budget(task_class: str, est_tokens: int) -> bool:
    """True iff an estimated initial-prompt token count is at/below the target max.

    Below ``target_min`` is fine (smaller is better); the gate is the max.
    """
    return est_tokens <= budget_for(task_class).target_max


# --------------------------------------------------------------------------- #
# Token estimation (labelled estimate; never claimed provider-exact)           #
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """char/4 token estimate for a string. Exact chars are recorded separately."""
    if not text:
        return 0
    return int(math.ceil(len(text) / CHARS_PER_TOKEN))


def estimate_schema_tokens(tool_defs: Any) -> Tuple[int, int]:
    """Return ``(chars, est_tokens)`` for a list of tool-definition dicts."""
    total_chars = 0
    for td in tool_defs or []:
        try:
            total_chars += len(json.dumps(td, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            total_chars += len(str(td))
    return total_chars, int(math.ceil(total_chars / CHARS_PER_TOKEN))


def _measure(text: str) -> Dict[str, int]:
    return {"chars": len(text or ""), "est_tokens": estimate_tokens(text or "")}


def _substring_between(haystack: str, start: str, end: str) -> str:
    """Return the inclusive block ``start..end`` if both markers are present."""
    if not haystack:
        return ""
    i = haystack.find(start)
    if i == -1:
        return ""
    j = haystack.find(end, i + len(start))
    if j == -1:
        return ""
    return haystack[i:j + len(end)]


# Marker for the skills index block emitted by prompt_builder.build_skills_system_prompt.
_SKILLS_START = "<available_skills>"
_SKILLS_END = "</available_skills>"


def measure_prompt_categories(
    *,
    system_prompt: str = "",
    tool_defs: Any = None,
    messages: Optional[List[Mapping[str, Any]]] = None,
    known_blocks: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Break an assembled request into per-category token accounting.

    Categories measured (each ``{chars, est_tokens}``):
      * ``tool_schemas`` — the model-facing ``tools`` array (the dominant canary
        contributor);
      * ``skills_index`` — the ``<available_skills>`` block inside the system
        prompt (bodies are lazy; only the index rides in-prompt);
      * ``system_other`` — the system prompt minus the skills index (identity,
        policy/guidance, kanban guidance, env hints, memory snapshot, …);
      * ``system_prompt_total`` — the whole system message;
      * per-``known_blocks`` entries (e.g. ``kanban_guidance``) when the caller
        supplies the exact block text, so large guidance blocks are attributable;
      * ``conversation`` — all non-system messages (history + the task turn);
      * ``total`` — the sum of system_prompt_total + tool_schemas + conversation.

    Everything is exact CHARS + a labelled char/4 ESTIMATE. No text is returned —
    only sizes — so the result is safe to persist through the journal.
    """
    messages = list(messages or [])
    skills_block = _substring_between(system_prompt, _SKILLS_START, _SKILLS_END)
    # Remove only the FIRST occurrence (the single skills block) so a recurrence of
    # the substring elsewhere can't over-count the removal.
    system_minus_skills = (
        system_prompt.replace(skills_block, "", 1) if skills_block else system_prompt
    )

    schema_chars, schema_tokens = estimate_schema_tokens(tool_defs)

    non_system = [m for m in messages if str(m.get("role")) != "system"]
    conv_text = "".join(str(m.get("content") or "") for m in non_system)
    # Split the conversation into the CURRENT task turn (the last non-system
    # message — the actual scenario/user content) and prior HISTORY, so a trivial
    # canary's task content can be shown to be a tiny fraction of the request.
    task_text = str(non_system[-1].get("content") or "") if non_system else ""
    history_text = "".join(str(m.get("content") or "") for m in non_system[:-1])

    categories: Dict[str, Any] = {
        "system_prompt_total": _measure(system_prompt),
        "system_other": _measure(system_minus_skills),
        "skills_index": _measure(skills_block),
        "tool_schemas": {"chars": schema_chars, "est_tokens": schema_tokens},
        "conversation": _measure(conv_text),
        "task": _measure(task_text),
        "history": _measure(history_text),
    }

    # Attribute caller-supplied named blocks (identity, constitutional/security
    # policy, kanban/runtime guidance, memory/RAG snapshot, deferred-tool index …)
    # found inside the system prompt, then report the still-UNATTRIBUTED remainder
    # of the system prompt so nothing is silently hidden. ``system_unattributed`` =
    # system_other minus the skills-index-free found blocks; it is a floor, honest
    # about what the accounting could not yet name.
    attributed_chars = 0
    if known_blocks:
        for name, text in known_blocks.items():
            present = text if (text and text in system_prompt) else ""
            block = _measure(present)
            categories[f"block_{name}"] = block
            # Only count a found block toward the remainder if it lives OUTSIDE the
            # skills index (which is already carved into its own category).
            if present and present not in skills_block:
                attributed_chars += block["chars"]
        remainder_chars = max(0, categories["system_other"]["chars"] - attributed_chars)
        categories["system_unattributed"] = {
            "chars": remainder_chars,
            "est_tokens": int(math.ceil(remainder_chars / CHARS_PER_TOKEN)),
        }

    total_chars = (
        categories["system_prompt_total"]["chars"]
        + schema_chars
        + categories["conversation"]["chars"]
    )
    total_tokens = (
        categories["system_prompt_total"]["est_tokens"]
        + schema_tokens
        + categories["conversation"]["est_tokens"]
    )
    categories["total"] = {"chars": total_chars, "est_tokens": total_tokens}
    categories["tool_count"] = len(list(tool_defs or []))
    # Percentage of the total estimated tokens per top-level category, so the
    # dominant contributor is obvious at a glance. Only the disjoint top-level
    # categories are included (skills_index is a subset of the system prompt and is
    # reported separately, not double-counted in the percentages).
    categories["percentages"] = _category_percentages(categories, total_tokens)
    return categories


def _category_percentages(categories: Mapping[str, Any], total_tokens: int) -> Dict[str, float]:
    """Percent-of-total est_tokens for the disjoint top-level request categories."""
    if total_tokens <= 0:
        return {}
    disjoint = ("system_prompt_total", "tool_schemas", "conversation")
    out: Dict[str, float] = {}
    for name in disjoint:
        val = categories.get(name)
        if isinstance(val, Mapping) and "est_tokens" in val:
            out[name] = round(100.0 * float(val["est_tokens"]) / float(total_tokens), 1)
    return out


# --------------------------------------------------------------------------- #
# Context planning — reserve output + tool-loop, never silently truncate       #
# --------------------------------------------------------------------------- #
# Default reservations (estimated tokens) kept below the model's context so a
# call always leaves room to answer and to run at least one tool round-trip.
DEFAULT_OUTPUT_RESERVE = 2_000
DEFAULT_TOOL_LOOP_RESERVE = 2_000
FULL_CTX_CAPABILITY = 64_000


@dataclass(frozen=True)
class ContextPlan:
    """A concrete, honest plan for one call's context usage.

    ``fits`` is True iff the estimated prompt PLUS the output and tool-loop
    reservations sit within the configured ``num_ctx``. When it does not fit,
    ``must_escalate`` is True and ``recommended_ctx`` names the smallest capacity
    (never above the authorized 64K) that would fit — the planner refuses to
    silently truncate; the caller must escalate context or shed content
    explicitly. ``dropped`` is always empty here by design (this planner never
    drops); any real dropping/summarizing must be done by the caller and reported.
    """

    task_class: str
    num_ctx: int
    est_prompt_tokens: int
    reserve_output_tokens: int
    reserve_tool_loop_tokens: int
    required_tokens: int
    headroom_tokens: int
    fits: bool
    must_escalate: bool
    within_prompt_target: bool
    recommended_ctx: int
    ctx_capability: int
    dropped: Tuple[str, ...] = ()


def plan_context(
    task_class: str,
    est_prompt_tokens: int,
    *,
    num_ctx: Optional[int] = None,
    reserve_output_tokens: int = DEFAULT_OUTPUT_RESERVE,
    reserve_tool_loop_tokens: int = DEFAULT_TOOL_LOOP_RESERVE,
) -> ContextPlan:
    """Plan context for a call without ever silently truncating.

    ``num_ctx`` defaults to the class's full capability (64K) when unset. The plan
    reserves ``reserve_output_tokens`` for the answer and ``reserve_tool_loop_tokens``
    for continuation, then checks the prompt fits in the remainder. If it does not,
    ``must_escalate`` is set and ``recommended_ctx`` is the smallest capacity up to
    64K that fits (or 64K when even that is insufficient — the caller must then shed
    content explicitly and report it). The full 64K capability is always retained.
    """
    b = budget_for(task_class)
    ctx = int(num_ctx) if (isinstance(num_ctx, int) and num_ctx > 0) else b.ctx_capability
    est = max(0, int(est_prompt_tokens))
    required = est + max(0, reserve_output_tokens) + max(0, reserve_tool_loop_tokens)
    headroom = ctx - required
    fits = headroom >= 0
    # Smallest capacity up to the 64K ceiling that would fit the required tokens.
    if required <= ctx:
        recommended = ctx
    else:
        recommended = min(FULL_CTX_CAPABILITY, max(ctx, required))
    return ContextPlan(
        task_class=b.task_class,
        num_ctx=ctx,
        est_prompt_tokens=est,
        reserve_output_tokens=max(0, reserve_output_tokens),
        reserve_tool_loop_tokens=max(0, reserve_tool_loop_tokens),
        required_tokens=required,
        headroom_tokens=headroom,
        fits=fits,
        must_escalate=not fits,
        within_prompt_target=within_budget(task_class, est),
        recommended_ctx=recommended,
        ctx_capability=b.ctx_capability,
    )


def numeric_category_summary(categories: Mapping[str, Any]) -> Dict[str, int]:
    """Flatten the category tree to an all-numeric ``{name_est_tokens: int}`` map.

    Journal-safe (numeric only): used to persist a per-call category breakdown as
    a redaction-clean event without any prompt text.
    """
    out: Dict[str, int] = {}
    for name, val in categories.items():
        if isinstance(val, Mapping) and "est_tokens" in val:
            out[f"{name}_est_tokens"] = int(val["est_tokens"])
            out[f"{name}_chars"] = int(val.get("chars", 0))
        elif isinstance(val, int):
            out[name] = val
    return out
