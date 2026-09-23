"""WAVE-30F — deterministic bottleneck attribution + injectable A/B runner."""
from __future__ import annotations

from youtab_runtime.bottleneck_matrix import (
    DISPROVEN,
    PROVEN_PRIMARY,
    PROVEN_SECONDARY,
    UNRESOLVED,
    PathMetrics,
    classify_bottlenecks,
    run_ab_paths,
)


def _by_component(findings):
    return {f.component: f for f in findings}


def test_prompt_evaluation_proven_primary():
    # Cold D, wall 100s. Model prompt-eval 60s (0.60) dominates; cold load 25s (0.25)
    # secondary; generation 10s (0.10) and runtime 5s (0.05) disproven.
    paths = {
        "D": PathMetrics("D", wall_s=100.0, cold_start=True),
        "C": PathMetrics("C", wall_s=5.0),
        "A": PathMetrics("A", load_duration_ms=25_000.0),
        "B": PathMetrics(
            "B",
            load_duration_ms=25_000.0,
            prompt_eval_duration_ms=60_000.0,
            eval_duration_ms=10_000.0,
            total_duration_ms=95_000.0,
            prompt_eval_count=21421,
            eval_count=49,
        ),
    }
    f = _by_component(classify_bottlenecks(paths))
    assert f["model_prompt_evaluation"].verdict == PROVEN_PRIMARY
    assert f["model_prompt_evaluation"].share_of_wall == 0.6
    assert f["model_cold_load"].verdict == PROVEN_SECONDARY
    assert f["model_generation"].verdict == DISPROVEN
    assert f["runtime_overhead"].verdict == DISPROVEN
    # Most severe first.
    assert classify_bottlenecks(paths)[0].component == "model_prompt_evaluation"


def test_runtime_overhead_proven_primary():
    # Runtime assembly/orchestration (path C) is 70s of a 100s end-to-end.
    paths = {
        "D": PathMetrics("D", wall_s=100.0, cold_start=True),
        "C": PathMetrics("C", wall_s=70.0),
        "B": PathMetrics(
            "B",
            load_duration_ms=2_000.0,
            prompt_eval_duration_ms=8_000.0,
            eval_duration_ms=5_000.0,
            total_duration_ms=15_000.0,
        ),
    }
    f = _by_component(classify_bottlenecks(paths))
    assert f["runtime_overhead"].verdict == PROVEN_PRIMARY
    # transport residual = 100000 - (2000+8000+5000) - 70000 = 15000 ms → 0.15 → disproven.
    assert f["transport_residual"].verdict == DISPROVEN
    assert abs(f["transport_residual"].evidence_ms - 15_000.0) < 1e-6


def test_cold_load_from_A_when_B_ran_warm_no_double_count():
    # Realistic ordering: A runs first (cold → loads model), B runs warm (load≈0).
    # Cold load must be attributed from A, and it must NOT be double-counted in the
    # transport residual. Components sum to the D wall exactly.
    paths = {
        "D": PathMetrics("D", wall_s=100.0, cold_start=True),
        "C": PathMetrics("C", wall_s=20.0),
        "A": PathMetrics("A", load_duration_ms=30_000.0, eval_duration_ms=500.0),
        "B": PathMetrics(
            "B",
            load_duration_ms=5.0,           # warm: model already resident
            prompt_eval_duration_ms=40_000.0,
            eval_duration_ms=10_000.0,
            total_duration_ms=50_005.0,     # warm total excludes the cold load
            prompt_eval_count=21421,
            eval_count=49,
        ),
    }
    f = _by_component(classify_bottlenecks(paths))
    # Cold load taken from A (30s cold), NOT B's warm 5ms (below threshold).
    assert f["model_cold_load"].evidence_ms == 30_000.0
    # residual = D - (cold_load + prompt_eval + gen) - C = 100000 - 80000 - 20000 = 0
    assert f["transport_residual"].evidence_ms == 0.0
    # Sum of the five component evidences == D wall (no double count, no gap).
    total = sum(
        f[c].evidence_ms for c in
        ("model_cold_load", "model_prompt_evaluation", "model_generation",
         "runtime_overhead", "transport_residual")
    )
    assert total == 100_000.0


def test_warm_D_does_not_blame_transport_for_a_cold_load_it_never_paid():
    # HIGH-2 regression: A ran cold (loaded the model), but D itself ran WARM. The
    # 30s cold load must NOT be attributed to D nor leak into transport_residual,
    # or the tool would falsely blame SSH/transport — the outcome the Owner forbade.
    paths = {
        "D": PathMetrics("D", wall_s=50.0, cold_start=False),  # warm D
        "C": PathMetrics("C", wall_s=5.0),
        "A": PathMetrics("A", load_duration_ms=30_000.0),      # A ran cold
        "B": PathMetrics(
            "B",
            load_duration_ms=5.0,                # B warm
            prompt_eval_duration_ms=40_000.0,
            eval_duration_ms=3_000.0,
        ),
    }
    f = _by_component(classify_bottlenecks(paths))
    # Warm D paid no load → cold load is 0 / disproven, NOT 30s.
    assert f["model_cold_load"].evidence_ms == 0.0
    assert f["model_cold_load"].verdict == DISPROVEN
    # residual = 50000 - (0 + 40000 + 3000) - 5000 = 2000 → tiny, NOT a false blame.
    assert f["transport_residual"].evidence_ms == 2_000.0
    assert f["transport_residual"].verdict == DISPROVEN


def test_unknown_D_cold_state_leaves_cold_load_and_residual_unresolved():
    # If the driver did not record D's cold/warm state, we cannot honestly attribute
    # a cold load or a residual → both UNRESOLVED (never guessed, never blamed).
    paths = {
        "D": PathMetrics("D", wall_s=100.0),  # cold_start unset
        "C": PathMetrics("C", wall_s=5.0),
        "A": PathMetrics("A", load_duration_ms=25_000.0),
        "B": PathMetrics("B", load_duration_ms=25_000.0,
                         prompt_eval_duration_ms=60_000.0, eval_duration_ms=10_000.0),
    }
    f = _by_component(classify_bottlenecks(paths))
    assert f["model_cold_load"].verdict == UNRESOLVED
    assert f["transport_residual"].verdict == UNRESOLVED
    # Model prompt-eval is still attributable (it is prompt-dependent, not state-dependent).
    assert f["model_prompt_evaluation"].verdict == PROVEN_PRIMARY


def test_materially_negative_residual_is_unresolved_not_clamped():
    # C + model cost exceed the D wall (startup variance across process spawns) →
    # the runs are not comparable; residual is UNRESOLVED, not clamped to 0.
    paths = {
        "D": PathMetrics("D", wall_s=10.0, cold_start=False),
        "C": PathMetrics("C", wall_s=9.0),
        "B": PathMetrics("B", load_duration_ms=5.0,
                         prompt_eval_duration_ms=8_000.0, eval_duration_ms=5_000.0),
    }
    # residual raw = 10000 - 13000 - 9000 = -12000 ms → unresolved.
    f = _by_component(classify_bottlenecks(paths))
    assert f["transport_residual"].verdict == UNRESOLVED
    assert f["transport_residual"].evidence_ms is None


def test_missing_paths_are_unresolved_not_blamed():
    # Only D measured: every model/runtime component is UNRESOLVED, nothing blamed.
    paths = {"D": PathMetrics("D", wall_s=181.0)}
    findings = classify_bottlenecks(paths)
    for comp in ("model_cold_load", "model_prompt_evaluation", "model_generation",
                 "runtime_overhead", "transport_residual"):
        assert _by_component(findings)[comp].verdict == UNRESOLVED
    # Unresolved findings never claim a share.
    assert all(f.share_of_wall is None for f in findings)


def test_run_ab_paths_uses_minimal_task_for_A_and_exact_prompt_for_B():
    calls = []

    class _FakeResult:
        def __init__(self, wall):
            self.wall_s = wall
            self.ttft_s = 0.1
            self.raw_timings = {
                "load_duration": 2_000_000_000,     # 2000 ms
                "prompt_eval_count": 21421,
                "prompt_eval_duration": 5_000_000_000,
                "eval_count": 49,
                "eval_duration": 1_000_000_000,
                "total_duration": 8_000_000_000,
            }

    def fake_native_chat(*, base_url, model, messages, tools, options, keep_alive, api_key):
        calls.append({"messages": messages, "tools": tools, "options": options})
        return _FakeResult(wall=8.1)

    runtime_msgs = [
        {"role": "system", "content": "big system prompt"},
        {"role": "user", "content": "the real task"},
    ]
    runtime_tools = [{"type": "function", "function": {"name": "kanban_show"}}]
    paths = run_ab_paths(
        base_url="http://localhost:11434/v1",
        model="qwen3.5:9b",
        runtime_messages=runtime_msgs,
        runtime_tools=runtime_tools,
        options={"num_ctx": 64000},
        native_chat_fn=fake_native_chat,
    )
    # Path A got a minimal one-line task and NO tools.
    assert calls[0]["tools"] is None
    assert "OK" in calls[0]["messages"][0]["content"]
    # Path B replayed the EXACT runtime messages + tools + options.
    assert calls[1]["messages"] == runtime_msgs
    assert calls[1]["tools"] == runtime_tools
    assert calls[1]["options"] == {"num_ctx": 64000}
    # Native timings surfaced as ms on the PathMetrics.
    assert paths["B"].prompt_eval_count == 21421
    assert paths["B"].load_duration_ms == 2000.0
    assert paths["B"].cold_start is True
