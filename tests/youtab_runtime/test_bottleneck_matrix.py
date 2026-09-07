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
    # D wall 100s. Model prompt-eval 60s (0.60) dominates; load 25s (0.25) secondary;
    # generation 10s (0.10) and runtime 5s (0.05) disproven.
    paths = {
        "D": PathMetrics("D", wall_s=100.0),
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
        "D": PathMetrics("D", wall_s=100.0),
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
    # transport residual = 100000 - 15000 - 70000 = 15000 ms → 0.15 → disproven.
    assert f["transport_residual"].verdict == DISPROVEN
    assert abs(f["transport_residual"].evidence_ms - 15_000.0) < 1e-6


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
