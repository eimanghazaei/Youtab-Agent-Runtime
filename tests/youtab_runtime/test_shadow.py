from __future__ import annotations

import pytest

from youtab_runtime.memory import (
    BudgetInputs,
    HeuristicTokenCounter,
    PreservedContext,
    ShadowConfig,
    ShadowScenario,
    compute_shadow,
)
from youtab_runtime.memory.tokenizer import PromptComponents

SCENARIO_TEXT = {
    ShadowScenario.PERSIAN_LONG: "درآمد سه‌ماهه در همه مناطق افزایش یافت و ریسک اعتباری بررسی شد. " * 40,
    ShadowScenario.DUTCH_ENTERPRISE: "Het meerjarenplan voor de onderneming met afhankelijkheden. " * 40,
    ShadowScenario.CRM_HISTORY: "Customer OPP-4471 last contacted; stage negotiation; owner e.g. " * 40,
    ShadowScenario.ERP_RECONCILIATION: "PO-9912 GR/IR variance reconciled against invoice INV-3321. " * 40,
    ShadowScenario.SAP_RECORD: "MATNR=000000000001234567;WERKS=1000;MENGE=250.000;MEINS=EA " * 40,
    ShadowScenario.CAD_FEA: "PART=BRKT-22;MASS=0.0142kg;SOLVER=nastran;MESH=tet10;NODES=48213 " * 40,
    ShadowScenario.SOURCE_CODE: "def total(rows):\n    return sum(r.amount for r in rows)\n" * 40,
    ShadowScenario.TOOL_SCHEMA_HEAVY: "{\"name\":\"tool\",\"parameters\":{\"type\":\"object\"}} " * 40,
    ShadowScenario.LONG_RUNNING_RESUMED: "resumed step 412 of plan; next action send email. " * 40,
    ShadowScenario.HUGE_CORPUS: "one relevant retrieved fact about billing. ",
}

CORPUS = {
    ShadowScenario.HUGE_CORPUS: 50_000_000,
}


def _inputs() -> BudgetInputs:
    return BudgetInputs(context_window=128_000, output_reserve_tokens=8_000,
                        tool_schema_tokens=4_000, safety_context_tokens=1_000,
                        compaction_reserve_tokens=4_000)


def _preserved() -> PreservedContext:
    return PreservedContext(
        goals=("close OPP-4471",), decisions=("annual billing",),
        open_tasks=("await credit approval",), approval_refs=("appr-1",),
        artifact_refs=("artifact-77",), safety_instructions=("never send without approval",),
    )


def test_flag_is_disabled_by_default() -> None:
    assert ShadowConfig().token_budgeted_injection_enabled is False


@pytest.mark.parametrize("scenario", list(ShadowScenario))
def test_every_scenario_is_bounded_preserves_and_never_sends(scenario: ShadowScenario) -> None:
    counter = HeuristicTokenCounter()
    text = SCENARIO_TEXT[scenario]
    corpus = CORPUS.get(scenario, 200_000)
    production = PromptComponents(
        system_prompt="You are Youtab.", messages=(text, text), tool_schemas="{}" * 100,
        retrieved_memory=(text,), output_reserve_tokens=8_000,
    )
    cmp = compute_shadow(scenario, production, _inputs(), counter, _preserved(), corpus_tokens=corpus)
    assert cmp.sent_to_model is False
    assert cmp.prompt_bounded_by_budget is True
    assert cmp.preserved_all is True and cmp.missing_preserved_count == 0
    # retrieval slice is bounded, independent of corpus size
    assert cmp.retrieval_injected_tokens <= _inputs().context_window


def test_huge_corpus_does_not_blow_the_prompt() -> None:
    counter = HeuristicTokenCounter()
    prod = PromptComponents(system_prompt="s", messages=("hi",), output_reserve_tokens=8000)
    small = compute_shadow(ShadowScenario.HUGE_CORPUS, prod, _inputs(), counter, _preserved(), corpus_tokens=1_000)
    huge = compute_shadow(ShadowScenario.HUGE_CORPUS, prod, _inputs(), counter, _preserved(), corpus_tokens=50_000_000)
    # a 50000x larger corpus injects a BOUNDED slice, not 50000x more tokens
    assert huge.retrieval_injected_tokens >= small.retrieval_injected_tokens
    assert huge.retrieval_injected_tokens < 50_000_000
    assert huge.prompt_bounded_by_budget


def test_no_user_content_appears_in_redacted_metrics() -> None:
    counter = HeuristicTokenCounter()
    secret = "TOP-SECRET customer merger with Acme worth 5 million"
    production = PromptComponents(system_prompt=secret, messages=(secret,),
                                  retrieved_memory=(secret,), output_reserve_tokens=8000)
    preserved = PreservedContext(goals=(secret,))
    cmp = compute_shadow(ShadowScenario.CRM_HISTORY, production, _inputs(), counter, preserved,
                         corpus_tokens=1000)
    blob = cmp.model_dump_json()
    assert "TOP-SECRET" not in blob
    assert "Acme" not in blob
    assert "merger" not in blob
    # only digests + counts are present
    assert len(cmp.production_digest) == 64 and len(cmp.shadow_digest) == 64
