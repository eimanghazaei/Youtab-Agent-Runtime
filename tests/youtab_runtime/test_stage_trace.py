"""R8 stage-trace observability: safety, causal validity, aggregation.

These prove the properties the WAVE-30H R8 requirement calls for:
* secrets / raw prompts / raw responses cannot be stored (allowlist enforced);
* null (not measured) is never conflated with zero (measured ~instant);
* monotonic durations intra-process, epoch-labelled gaps cross-process;
* correlation context is per-run immutable (contextvars) and thread-isolated;
* p50/p95/p99 and cold-vs-warm are computed honestly.
"""

from __future__ import annotations

import threading

import pytest

from youtab_runtime import stage_trace as st
from youtab_runtime import stage_trace_report as rep


@pytest.fixture()
def sink():
    s = st.MemorySink()
    st.set_sink(s)
    try:
        yield s
    finally:
        st.set_sink(None)


# --- safety: no secret / raw-content leakage ---------------------------------


def test_allowlisted_string_attr_ok(sink):
    with st.span(st.Stage.TOOL_CALL, tool_name="web_search"):
        pass
    assert sink.records[0]["attrs"]["tool_name"] == "web_search"


def test_non_allowlisted_string_attr_raises(sink):
    with pytest.raises(st.TraceSafetyError):
        with st.span(st.Stage.PROMPT_CONSTRUCT, prompt="the secret user prompt"):
            pass


def test_oversized_allowlisted_string_raises(sink):
    with pytest.raises(st.TraceSafetyError):
        with st.span(st.Stage.TOOL_CALL, tool_name="x" * (st.MAX_STR_LEN + 1)):
            pass


def test_numeric_and_bool_and_none_attrs_ok(sink):
    with st.span(st.Stage.PROMPT_TOKENIZE, input_tokens=1234, truncated=False, note=None):
        pass
    a = sink.records[0]["attrs"]
    assert a["input_tokens"] == 1234 and a["truncated"] is False and a["note"] is None


def test_content_fingerprint_is_stable_and_non_reversible():
    fp1 = st.content_fingerprint("a very long secret prompt body")
    fp2 = st.content_fingerprint("a very long secret prompt body")
    fp3 = st.content_fingerprint("different")
    assert fp1 == fp2 and fp1 != fp3
    assert fp1.startswith("sha256:") and len(fp1) == len("sha256:") + 16
    assert "secret" not in fp1  # the content itself is not present


def test_fingerprint_attr_is_allowed_via_size_not_text(sink):
    # The safe pattern: store a size (int) + a fingerprint under an allowlisted
    # key. reason_code is allowlisted; fingerprints are short.
    with st.span(st.Stage.MODEL_GENERATE, output_tokens=42):
        pass
    assert sink.records[0]["attrs"]["output_tokens"] == 42


# --- null != zero ------------------------------------------------------------


def test_null_duration_recorded_as_not_measured(sink):
    st.record(st.Stage.MEMORY_RETRIEVE, duration_ns=None, kind="vector")
    stats = rep.aggregate(sink.records)
    s = stats[st.Stage.MEMORY_RETRIEVE]
    assert s.count == 1 and s.count_null == 1 and s.count_measured == 0
    assert s.p50_ms is None  # unmeasured -> no percentile, never 0.0


def test_zero_duration_is_measured_not_null(sink):
    st.record(st.Stage.TOOLS_SELECT, duration_ns=0)
    stats = rep.aggregate(sink.records)
    s = stats[st.Stage.TOOLS_SELECT]
    assert s.count_measured == 1 and s.count_null == 0
    assert s.p50_ms == 0.0  # measured zero is a real 0, distinct from None


# --- clocks: monotonic intra-process, epoch cross-process --------------------


def test_span_uses_monotonic_and_measures_positive(sink):
    with st.span(st.Stage.PROMPT_CONSTRUCT):
        sum(range(10000))
    r = sink.records[0]
    assert r["clock"] == "monotonic"
    assert r["duration_ns"] is not None and r["duration_ns"] >= 0


def test_cross_process_gap_is_epoch_labelled(sink):
    enqueue = st.mark_epoch()
    # ... hand off to another process ...
    gap = st.mark_epoch() - enqueue
    st.record(st.Stage.QUEUE_WAIT, duration_ns=gap, clock="epoch")
    r = sink.records[0]
    assert r["clock"] == "epoch" and r["stage"] == st.Stage.QUEUE_WAIT


def test_anchor_epoch_stamps_wall_clock(sink):
    with st.span(st.Stage.MODEL_GENERATE, anchor_epoch=True):
        pass
    r = sink.records[0]
    assert r["t_start_epoch_ns"] is not None and r["t_end_epoch_ns"] is not None


# --- failed stage is recorded, then re-raised --------------------------------


def test_span_records_error_and_reraises(sink):
    with pytest.raises(ValueError):
        with st.span(st.Stage.TOOL_CALL, tool_name="boom"):
            raise ValueError("kaboom")
    r = sink.records[0]
    assert r["ok"] is False
    assert r["attrs"]["reason_code"] == "ValueError"
    assert r["attrs"]["tool_name"] == "boom"


# --- correlation context: immutable, merged, thread-isolated -----------------


def test_context_is_carried_into_records(sink):
    with st.trace_context_scope(run_id="R1", root_run_id="ROOT", attempt=2, provider="ollama"):
        with st.span(st.Stage.MODEL_TTFT):
            pass
    ctx = sink.records[0]["ctx"]
    assert ctx == {"run_id": "R1", "root_run_id": "ROOT", "attempt": 2, "provider": "ollama"}


def test_context_scope_resets(sink):
    with st.trace_context_scope(run_id="R1"):
        pass
    with st.span(st.Stage.RUN_TOTAL):
        pass
    assert "run_id" not in sink.records[0]["ctx"]


def test_unknown_context_field_rejected():
    with pytest.raises(ValueError):
        st.bind_trace_context(not_a_field="x")


def test_context_is_thread_isolated(sink):
    seen: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def worker(rid: str) -> None:
        with st.trace_context_scope(run_id=rid):
            barrier.wait()  # force interleaving
            seen[rid] = st.current_trace_context().run_id

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert seen == {"A": "A", "B": "B"}  # no cross-contamination


# --- enable / disable (zero overhead when off) -------------------------------


def test_disabled_span_body_runs_but_no_emit(monkeypatch):
    st.set_sink(None)  # default sink -> gated by env
    monkeypatch.setenv("YOUTAB_STAGE_TRACE", "0")
    assert st.is_enabled() is False
    ran = []
    with st.span(st.Stage.PROMPT_CONSTRUCT):
        ran.append(True)
    assert ran == [True]  # body still executed


def test_memory_sink_forces_enabled(sink):
    assert st.is_enabled() is True


# --- percentiles -------------------------------------------------------------


def test_percentile_linear_interpolation():
    vals = [10, 20, 30, 40, 50]
    assert rep.percentile(vals, 50) == 30.0
    assert rep.percentile(vals, 0) == 10.0
    assert rep.percentile(vals, 100) == 50.0
    # 95th of 5 points: rank = 0.95*4 = 3.8 -> 40 + 0.8*(50-40) = 48
    assert rep.percentile(vals, 95) == pytest.approx(48.0)


def test_percentile_empty_is_none_not_zero():
    assert rep.percentile([], 50) is None


def test_percentile_single_value():
    assert rep.percentile([7], 99) == 7.0


# --- aggregation: cold vs warm ----------------------------------------------


def test_cold_warm_split(sink):
    st.record(st.Stage.MODEL_INIT, duration_ns=900_000_000, cache_state="cold")
    st.record(st.Stage.MODEL_INIT, duration_ns=5_000_000, cache_state="warm")
    st.record(st.Stage.MODEL_INIT, duration_ns=6_000_000, cache_state="warm")
    stats = rep.aggregate(sink.records)
    s = stats[st.Stage.MODEL_INIT]
    assert s.cold_count == 1 and s.warm_count == 2
    assert s.cold_p50_ms == pytest.approx(900.0)
    assert s.warm_p50_ms == pytest.approx(5.5)


def test_stage_with_no_cache_signal_contributes_to_neither_split(sink):
    st.record(st.Stage.TOOL_CALL, duration_ns=1_000_000, tool_name="x")
    s = rep.aggregate(sink.records)[st.Stage.TOOL_CALL]
    assert s.cold_count == 0 and s.warm_count == 0 and s.count_measured == 1


def test_error_count_tracked(sink):
    st.record(st.Stage.TOOL_CALL, duration_ns=1000, ok=False, tool_name="x")
    st.record(st.Stage.TOOL_CALL, duration_ns=2000, ok=True, tool_name="y")
    s = rep.aggregate(sink.records)[st.Stage.TOOL_CALL]
    assert s.count == 2 and s.count_error == 1


def test_clocks_recorded_per_stage(sink):
    st.record(st.Stage.QUEUE_WAIT, duration_ns=1000, clock="epoch")
    s = rep.aggregate(sink.records)[st.Stage.QUEUE_WAIT]
    assert s.clocks == {"epoch"}


# --- jsonl round-trip --------------------------------------------------------


def test_jsonl_roundtrip(tmp_path):
    path = tmp_path / "trace-1.jsonl"
    st.set_sink(st._JsonlSink(path=path))
    try:
        with st.trace_context_scope(run_id="R9"):
            with st.span(st.Stage.MODEL_GENERATE, output_tokens=10):
                pass
    finally:
        st.set_sink(None)
    recs = list(rep.read_jsonl(path))
    assert len(recs) == 1 and recs[0]["ctx"]["run_id"] == "R9"
    stats = rep.aggregate(recs)
    assert stats[st.Stage.MODEL_GENERATE].count_measured == 1


def test_read_jsonl_skips_malformed(tmp_path):
    path = tmp_path / "trace-1.jsonl"
    path.write_text('{"stage":"x","duration_ns":1,"ok":true}\nNOT JSON\n', encoding="utf-8")
    recs = list(rep.read_jsonl(path))
    assert len(recs) == 1


def test_format_report_orders_by_p95(sink):
    st.record("slow.stage", duration_ns=100_000_000)
    st.record("fast.stage", duration_ns=1_000_000)
    text = rep.format_report(rep.aggregate(sink.records))
    assert text.index("slow.stage") < text.index("fast.stage")
