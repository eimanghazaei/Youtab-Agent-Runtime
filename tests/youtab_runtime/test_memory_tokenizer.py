from __future__ import annotations

import pytest

from youtab_runtime.memory import (
    CompactionSummary,
    ExactTokenCounter,
    HeuristicTokenCounter,
    ModelCapability,
    PromptAccounting,
    PromptComponents,
    TokenCounter,
    account,
    needs_compaction,
    select_counter,
)
from youtab_runtime.memory.budget import (
    BudgetInputs,
    allocate,
    selective_retrieval_tokens,
)

SAMPLES = {
    "english": "The quarterly revenue grew by twelve percent across all regions.",
    "dutch": "De omzet groeide dit kwartaal met twaalf procent in alle regio's.",
    "persian": "درآمد سه‌ماهه در همه مناطق دوازده درصد رشد کرد و سود خالص افزایش یافت.",
    "json": '{"opportunity_id":"OPP-4471","amount":125000,"stage":"negotiation","owner":"e.g"}',
    "code": "def total(rows):\n    return sum(r.amount for r in rows if r.active)\n",
    "sap": "MATNR=000000000001234567;WERKS=1000;MENGE=250.000;MEINS=EA;LGORT=0001",
    "cad": "PART=BRKT-22;UNITS=mm;MASS=0.0142kg;SOLVER=nastran-2024;MESH=tet10;NODES=48213",
}


def test_heuristic_never_assumes_one_char_one_token() -> None:
    c = HeuristicTokenCounter()
    for name, text in SAMPLES.items():
        n = c.count(text)
        assert n > 0, name
        # token count must differ from raw char length (not 1:1)
        assert n != len(text), name
        # and be a conservative fraction of characters (< char count)
        assert n < len(text), name


def test_script_sensitivity_persian_vs_english_density() -> None:
    c = HeuristicTokenCounter()
    # equal-length-ish prose: Persian/Arabic script tokenizes denser than Latin
    per_char_persian = c.count(SAMPLES["persian"]) / len(SAMPLES["persian"])
    per_char_english = c.count(SAMPLES["english"]) / len(SAMPLES["english"])
    assert per_char_persian > per_char_english


def test_exact_counter_uses_provider_tokenizer_and_zero_margin() -> None:
    ec = ExactTokenCounter("fake-bpe", lambda s: len(s.split()))
    assert isinstance(ec, TokenCounter)
    assert ec.uncertainty_margin == 0.0
    assert ec.count("one two three") == 3


def test_select_counter_prefers_registered_exact_else_heuristic() -> None:
    cap_exact = ModelCapability(
        model_id="m1", provider="acme", context_window=128000,
        max_output_tokens=8000, tokenizer_name="acme-bpe",
    )
    reg = {"acme-bpe": lambda s: len(s)}
    chosen = select_counter(cap_exact, reg)
    assert isinstance(chosen, ExactTokenCounter)
    cap_unknown = ModelCapability(
        model_id="m2", provider="local", context_window=32000,
        max_output_tokens=4000, tokenizer_name=None,
    )
    fb = select_counter(cap_unknown, reg, fallback_margin=0.3)
    assert isinstance(fb, HeuristicTokenCounter)
    assert fb.uncertainty_margin == 0.3


def test_uncertainty_margin_is_applied_to_worst_case() -> None:
    counter = HeuristicTokenCounter(uncertainty_margin=0.25)
    comp = PromptComponents(system_prompt="hello world", output_reserve_tokens=100)
    acc = account(comp, counter)
    assert acc.worst_case() >= acc.subtotal()
    assert acc.worst_case() == pytest.approx(acc.subtotal() * 1.25, abs=1)


def test_account_sums_every_component() -> None:
    counter = ExactTokenCounter("words", lambda s: len(s.split()))
    comp = PromptComponents(
        system_prompt="a b c",              # 3
        messages=("d e", "f"),               # 2 + 1
        retrieved_memory=("g h i j",),       # 4
        tool_schemas="k l",                  # 2
        tool_results=("m",),                 # 1
        output_reserve_tokens=10,
    )
    acc = account(comp, counter)
    assert acc.system_prompt_tokens == 3
    assert acc.messages_tokens == 3
    assert acc.retrieved_memory_tokens == 4
    assert acc.tool_schema_tokens == 2
    assert acc.tool_results_tokens == 1
    assert acc.subtotal() == 3 + 3 + 4 + 2 + 1 + 10


def test_large_external_memory_does_not_scale_prompt_linearly() -> None:
    # The prompt only ever carries the SELECTED retrieval slice, sized by budget,
    # regardless of how large the durable corpus is.
    counter = ExactTokenCounter("words", lambda s: len(s.split()))
    inputs = BudgetInputs(
        context_window=128000, output_reserve_tokens=8000,
        tool_schema_tokens=4000, safety_context_tokens=1000, compaction_reserve_tokens=4000,
    )
    alloc = allocate(inputs)
    prompt_sizes = []
    for corpus_tokens in (1_000, 1_000_000, 50_000_000):
        injected = selective_retrieval_tokens(alloc, corpus_tokens)
        # build a retrieved-memory block of exactly `injected` word-tokens
        block = " ".join(["w"] * injected)
        comp = PromptComponents(retrieved_memory=(block,), output_reserve_tokens=8000)
        acc = account(comp, counter)
        prompt_sizes.append(acc.retrieved_memory_tokens)
    # small corpus injects only what exists; both large corpora cap at the budget.
    assert prompt_sizes[0] == 1_000  # 1k corpus < budget -> injects all 1k
    # 1M and 50M corpora both inject exactly the retrieval budget: a 50x larger
    # corpus produces 0% larger prompt -> NOT linear.
    assert prompt_sizes[1] == prompt_sizes[2] == alloc.retrieval_tokens
    assert alloc.retrieval_tokens < 1_000_000


def test_graceful_compaction_triggers_before_overflow() -> None:
    counter = ExactTokenCounter("words", lambda s: len(s.split()))
    window = 1000
    # grow messages until compaction is signalled; assert it fires BEFORE overflow
    fired_at = None
    for n in range(1, 2000):
        comp = PromptComponents(messages=(" ".join(["w"] * n),), output_reserve_tokens=0)
        acc = account(comp, counter)
        if needs_compaction(acc, window, headroom=0.1):
            fired_at = acc.worst_case()
            break
    assert fired_at is not None
    assert fired_at < window  # signalled before we ever exceeded the window


def test_compaction_summary_preserves_required_fields() -> None:
    s = CompactionSummary(
        goals=("close OPP-4471",),
        decisions=("use annual billing",),
        open_tasks=("await approval",),
        evidence_refs=("mem-abc", "artifact-1"),
        unresolved_risks=("credit check pending",),
    )
    # all five preservation dimensions are first-class, not free text
    assert s.goals and s.decisions and s.open_tasks and s.evidence_refs and s.unresolved_risks
