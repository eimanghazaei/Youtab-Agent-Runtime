"""WAVE-30E — native Ollama timing extraction + redaction-safe timing records."""
from __future__ import annotations

import math

import pytest

from youtab_runtime.model_timings import (
    build_call_timings,
    extract_native_ollama_timings,
    sanitize_timings,
)


class _Obj:
    """Minimal stand-in for an OpenAI-SDK response object with attributes."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _ExtraObj:
    """pydantic-v2-style object that stashes unknown fields in model_extra."""

    def __init__(self, model_extra=None, usage=None):
        self.model_extra = model_extra or {}
        self.usage = usage


def _representative_native_response():
    """A representative Ollama native (/api/chat) response's timing block.

    Durations are in NANOSECONDS per the Ollama API docs. This mirrors the exact
    field names Ollama emits so the extractor is proven against the real shape.
    """
    return {
        "total_duration": 5_589_157_167,      # 5589.157 ms
        "load_duration": 3_013_701_500,       # 3013.702 ms
        "prompt_eval_count": 26,
        "prompt_eval_cached_count": 10,
        "prompt_eval_duration": 342_546_000,  # 342.546 ms
        "eval_count": 298,
        "eval_duration": 4_799_921_000,       # 4799.921 ms
    }


def test_extract_converts_ns_to_ms_and_keeps_counts():
    native = extract_native_ollama_timings(_representative_native_response())
    assert native["total_duration_ms"] == pytest.approx(5589.157, abs=1e-3)
    assert native["load_duration_ms"] == pytest.approx(3013.7015, abs=1e-3)
    assert native["prompt_eval_duration_ms"] == pytest.approx(342.546, abs=1e-3)
    assert native["eval_duration_ms"] == pytest.approx(4799.921, abs=1e-3)
    # Counts copied verbatim (incl. the cached-prompt count the owner named).
    assert native["prompt_eval_count"] == 26
    assert native["prompt_eval_cached_count"] == 10
    assert native["eval_count"] == 298


def test_extract_derives_throughput_only_from_native_pairs():
    native = extract_native_ollama_timings(_representative_native_response())
    # eval tok/s = 298 / (4.799921 s) ≈ 62.09
    assert native["eval_tokens_per_second"] == pytest.approx(62.084, abs=0.05)
    # prompt-eval tok/s = 26 / 0.342546 s ≈ 75.9
    assert native["prompt_eval_tokens_per_second"] == pytest.approx(75.9, abs=0.1)


def test_extract_reads_from_model_extra_and_usage():
    # Native fields riding on model_extra (OpenAI-compat extension) + usage.
    resp = _ExtraObj(
        model_extra={"total_duration": 2_000_000_000, "eval_count": 100},
        usage=_Obj(eval_duration=1_000_000_000),  # 1 s
    )
    native = extract_native_ollama_timings(resp)
    assert native["total_duration_ms"] == pytest.approx(2000.0)
    assert native["eval_count"] == 100
    assert native["eval_tokens_per_second"] == pytest.approx(100.0)


def test_extract_absent_native_fields_returns_empty():
    # Stock OpenAI-compat response carries only usage tokens, no native timings.
    resp = _Obj(usage=_Obj(prompt_tokens=21421, completion_tokens=49))
    assert extract_native_ollama_timings(resp) == {}
    assert extract_native_ollama_timings(None) == {}


def test_extract_never_estimates_missing_leg():
    # eval_count present but eval_duration absent → NO throughput fabricated.
    native = extract_native_ollama_timings({"eval_count": 50})
    assert native["eval_count"] == 50
    assert "eval_tokens_per_second" not in native
    # duration present but count absent → no throughput either.
    native2 = extract_native_ollama_timings({"eval_duration": 1_000_000_000})
    assert "eval_tokens_per_second" not in native2


def test_sanitize_drops_all_text_and_negatives():
    dirty = {
        "wall_ms": 1234.5,
        "eval_count": 10,
        "prompt_text": "secret user prompt do not persist",   # dropped
        "authorization": "Bearer sk-abc123",                    # dropped
        "nested": {"a": 1},                                     # dropped (not numeric leaf)
        "negative": -5,                                          # dropped
        "nan": float("nan"),                                    # dropped
        "cold_start": True,                                      # kept (safe flag)
        "native_timings_available": 1,                           # coerced to bool
        7: 3,                                                    # non-str key dropped
    }
    clean = sanitize_timings(dirty)
    assert clean == {
        "wall_ms": 1234.5,
        "eval_count": 10,
        "cold_start": True,
        "native_timings_available": True,
    }
    # Absolutely no free-text value survives.
    for v in clean.values():
        assert isinstance(v, (int, float, bool))


def test_build_call_timings_wall_ttft_native_cold_warm():
    native = extract_native_ollama_timings(_representative_native_response())
    rec = build_call_timings(wall_s=6.1, ttft_s=3.2, native=native, cold_start=True)
    assert rec["wall_ms"] == pytest.approx(6100.0)
    assert rec["ttft_ms"] == pytest.approx(3200.0)
    assert rec["total_duration_ms"] == pytest.approx(5589.157, abs=1e-3)
    assert rec["native_timings_available"] is True
    assert rec["cold_start"] is True
    # Warm run, no native fields available (stock compat endpoint).
    warm = build_call_timings(wall_s=2.0, native={}, cold_start=False)
    assert warm["wall_ms"] == pytest.approx(2000.0)
    assert warm["native_timings_available"] is False
    assert warm["cold_start"] is False
    assert "ttft_ms" not in warm  # not measured → omitted, never zero-filled


def test_sanitize_rejects_hostile_keys():
    # A secret smuggled as a JSON *key* (numeric value) must be dropped — the
    # no-free-text guarantee covers keys, not only values.
    dirty = {
        "wall_ms": 12.0,
        "Bearer sk-live-abc123": 1,          # uppercase/space/dash → dropped
        "authorization: Bearer x": 2,        # dropped
        "UPPER": 3,                          # dropped (not lowercase)
        "has space": 4,                      # dropped
        "eval_count": 5,                     # kept (valid identifier)
    }
    clean = sanitize_timings(dirty)
    assert clean == {"wall_ms": 12.0, "eval_count": 5}


def test_cold_warm_derived_from_native_load_duration_not_wall_clock():
    # Cold: large native load_duration → cold_start True (derived from the native
    # model-load timer, NOT wall-clock).
    cold_native = extract_native_ollama_timings({"load_duration": 3_000_000_000})  # 3000 ms
    cold = build_call_timings(wall_s=1.0, native=cold_native)
    assert cold["cold_start"] is True
    # Warm: tiny native load_duration → cold_start False.
    warm_native = extract_native_ollama_timings({"load_duration": 5_000_000})       # 5 ms
    warm = build_call_timings(wall_s=1.0, native=warm_native)
    assert warm["cold_start"] is False
    # No native load_duration and no explicit fact → label omitted, never guessed.
    unknown = build_call_timings(wall_s=1.0, native={})
    assert "cold_start" not in unknown
    # Explicit caller fact overrides the native derivation.
    forced = build_call_timings(wall_s=1.0, native=cold_native, cold_start=False)
    assert forced["cold_start"] is False


def test_build_call_timings_all_numeric_leaves():
    rec = build_call_timings(
        wall_s=1.0, ttft_s=0.5,
        native=extract_native_ollama_timings(_representative_native_response()),
        cold_start=False,
    )
    for k, v in rec.items():
        assert isinstance(k, str)
        assert isinstance(v, (int, float, bool)), f"{k}={v!r} is not numeric/bool"
        if isinstance(v, float):
            assert math.isfinite(v)
