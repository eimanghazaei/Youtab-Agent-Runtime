"""R8: RunObserver fans a model call's numeric timings into per-stage spans.

This captures TTFT / generation / prefill / model-load / per-call-wall and
per-tool-call latency at the ONE observer chokepoint, with no extra provider
call and no hot-path edit, and without ever regressing the authoritative usage
row.
"""

from __future__ import annotations

from pathlib import Path

from youtab_runtime import run_journal as rj
from youtab_runtime import run_observer as ro
from youtab_runtime import stage_trace_report as rep


def _obs(db: Path) -> ro.RunObserver:
    return ro.create_run_observer(
        "run-1", rj.Principal("acme", "alice"), correlation_id="cid-1", db_path=db
    )


def test_native_timings_fan_out_to_stage_spans(tmp_path):
    db = tmp_path / "j.db"
    obs = _obs(db)
    obs.on_post_api_request(
        api_request_id="t1:api:1",
        provider="ollama",
        model="qwen",
        usage={"input_tokens": 100, "output_tokens": 50},
        timings={
            "wall_ms": 13300.0,
            "ttft_ms": 800.0,
            "eval_duration_ms": 4000.0,
            "eval_count": 50,
            "prompt_eval_duration_ms": 1200.0,
            "prompt_eval_count": 100,
            "load_duration_ms": 6000.0,
            "cold_start": True,
            "native_timings_available": True,
        },
    )
    stats = rep.aggregate(rep.read_journal("acme", "alice", db_path=db))
    assert stats["model.call"].p50_ms == 13300.0
    assert stats["model.ttft"].p50_ms == 800.0
    assert stats["model.generate"].p50_ms == 4000.0
    assert stats["prompt.tokenize"].p50_ms == 1200.0
    assert stats["model.init"].p50_ms == 6000.0
    # cold flag routed into the cold split
    assert stats["model.init"].cold_count == 1
    # native counts preserved as safe int attrs
    recs = list(rep.read_journal("acme", "alice", db_path=db))
    gen = next(r for r in recs if r["stage"] == "model.generate")
    assert gen["attrs"]["output_tokens"] == 50


def test_live_openai_compat_path_still_gets_wall_only(tmp_path):
    # No native fields (the real live path): only model.call from wall_ms.
    db = tmp_path / "j.db"
    obs = _obs(db)
    obs.on_post_api_request(
        api_request_id="t1:api:1",
        provider="openrouter",
        usage={"input_tokens": 10, "output_tokens": 5},
        timings={"wall_ms": 2500.0, "native_timings_available": False},
    )
    stats = rep.aggregate(rep.read_journal("acme", "alice", db_path=db))
    assert stats["model.call"].p50_ms == 2500.0
    # generation/prefill legs are ABSENT (null != zero) — not fabricated as 0.
    assert "model.generate" not in stats
    assert "prompt.tokenize" not in stats


def test_usage_row_not_regressed(tmp_path):
    db = tmp_path / "j.db"
    obs = _obs(db)
    ev = obs.on_post_api_request(
        api_request_id="t1:api:1",
        provider="ollama",
        usage={"input_tokens": 100, "output_tokens": 50},
        timings={"wall_ms": 1000.0},
    )
    assert ev is not None and ev.category == "usage"
    usage = rj.list_events("run-1", rj.Principal("acme", "alice"),
                           category="usage", db_path=db)
    assert len(usage) == 1 and usage[0].payload["input_tokens"] == 100


def test_timing_spans_dedupe_on_retry(tmp_path):
    db = tmp_path / "j.db"
    obs = _obs(db)
    payload = dict(
        api_request_id="t1:api:1", provider="ollama",
        usage={"input_tokens": 1, "output_tokens": 1},
        timings={"wall_ms": 1000.0, "ttft_ms": 100.0},
    )
    obs.on_post_api_request(**payload)
    obs.on_post_api_request(**payload)  # retry with same request id
    stats = rep.aggregate(rep.read_journal("acme", "alice", db_path=db))
    # deduped: one model.call, one model.ttft — not two
    assert stats["model.call"].count == 1
    assert stats["model.ttft"].count == 1


def test_tool_call_span_emitted(tmp_path):
    db = tmp_path / "j.db"
    obs = _obs(db)
    obs.on_post_tool_call(
        tool_name="web_search", tool_call_id="tc-1", duration_ms=250, status="ok"
    )
    stats = rep.aggregate(rep.read_journal("acme", "alice", db_path=db))
    assert stats["tool.call"].p50_ms == 250.0
    assert stats["tool.call"].count_error == 0


def test_tool_call_error_span(tmp_path):
    db = tmp_path / "j.db"
    obs = _obs(db)
    obs.on_post_tool_call(
        tool_name="flaky", tool_call_id="tc-2", duration_ms=10,
        status="error", error_type="TimeoutError", error_message="boom",
    )
    stats = rep.aggregate(rep.read_journal("acme", "alice", db_path=db))
    assert stats["tool.call"].count_error == 1
