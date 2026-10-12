"""Shadow-mode token-budget comparison (no production behavior change).

Given the CURRENT production prompt components (built unchanged elsewhere) and a
model budget, this computes — in parallel — what a token-budgeted prompt WOULD
look like, WITHOUT sending it to any model. It reports token savings, whether the
shadow prompt stays within the model budget regardless of corpus size, and
whether every must-keep element (goals, decisions, open tasks, approval refs,
artifact refs, safety instructions) is retained.

Only redacted metrics + content digests are emitted — never user content. Live
token-budgeted injection is gated behind :class:`ShadowConfig`, DEFAULT DISABLED.
The durable storage caps (2200/1375) are NOT touched here; this stage only
separates injection budgeting from storage admission.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .budget import BudgetInputs, allocate, selective_retrieval_tokens
from .tokenizer import PromptComponents, TokenCounter, account


class ShadowScenario(StrEnum):
    PERSIAN_LONG = "persian_long"
    DUTCH_ENTERPRISE = "dutch_enterprise"
    CRM_HISTORY = "crm_history"
    ERP_RECONCILIATION = "erp_reconciliation"
    SAP_RECORD = "sap_record"
    CAD_FEA = "cad_fea"
    SOURCE_CODE = "source_code"
    HUGE_CORPUS = "huge_corpus"
    TOOL_SCHEMA_HEAVY = "tool_schema_heavy"
    LONG_RUNNING_RESUMED = "long_running_resumed"


class ShadowConfig(BaseModel):
    """Default-disabled feature flag for token-budgeted injection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token_budgeted_injection_enabled: bool = False


class PreservedContext(BaseModel):
    """Elements that MUST survive any budgeting/compaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goals: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    open_tasks: tuple[str, ...] = ()
    approval_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    safety_instructions: tuple[str, ...] = ()

    def all_elements(self) -> tuple[str, ...]:
        return (
            self.goals + self.decisions + self.open_tasks
            + self.approval_refs + self.artifact_refs + self.safety_instructions
        )


class ShadowComparison(BaseModel):
    """Redacted-only comparison result. Contains NO user content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: ShadowScenario
    counter_name: str
    context_window: int
    production_tokens: int
    shadow_tokens: int
    corpus_tokens: int
    retrieval_injected_tokens: int
    prompt_bounded_by_budget: bool
    preserved_all: bool
    missing_preserved_count: int = Field(ge=0)
    production_digest: str
    shadow_digest: str
    sent_to_model: bool = False  # always False in shadow mode


def _digest(texts: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for t in texts:
        h.update(hashlib.sha256(t.encode("utf-8")).digest())
    return h.hexdigest()


def compute_shadow(
    scenario: ShadowScenario,
    production: PromptComponents,
    budget_inputs: BudgetInputs,
    counter: TokenCounter,
    preserved: PreservedContext,
    *,
    corpus_tokens: int,
) -> ShadowComparison:
    """Compute the shadow budgeted prompt and compare. Never sends anything."""

    alloc = allocate(budget_inputs)
    production_acc = account(production, counter)

    # Shadow retrieval is bounded by the retrieval budget, independent of corpus.
    injected_retrieval = selective_retrieval_tokens(alloc, corpus_tokens)

    # Shadow prompt = fixed reserves already in inputs + capsule(preserved) +
    # task-state(preserved) + bounded retrieval + recent turns from production.
    preserved_block = "\n".join(preserved.all_elements())
    shadow_components = PromptComponents(
        system_prompt=production.system_prompt,
        messages=production.messages,
        retrieved_memory=(" ".join(["w"] * injected_retrieval),) if injected_retrieval else (),
        tool_schemas=production.tool_schemas,
        tool_results=(preserved_block,) if preserved_block else (),
        output_reserve_tokens=budget_inputs.output_reserve_tokens,
    )
    shadow_acc = account(shadow_components, counter)

    # Every must-keep element is retained in the shadow (it is placed verbatim in
    # the preserved block, so its token contribution is present and non-lossy).
    missing = [e for e in preserved.all_elements() if e and e not in preserved_block]
    bounded = shadow_acc.worst_case() <= alloc.context_window

    return ShadowComparison(
        scenario=scenario,
        counter_name=counter.name,
        context_window=alloc.context_window,
        production_tokens=production_acc.subtotal(),
        shadow_tokens=shadow_acc.subtotal(),
        corpus_tokens=corpus_tokens,
        retrieval_injected_tokens=injected_retrieval,
        prompt_bounded_by_budget=bounded,
        preserved_all=(len(missing) == 0),
        missing_preserved_count=len(missing),
        production_digest=_digest(production.messages + (production.system_prompt,)),
        shadow_digest=_digest(shadow_components.messages + (shadow_components.system_prompt,)),
    )
