"""WAVE-30F — native Ollama /api/chat parsing + base-url derivation.

Pure, network-free tests against recorded Ollama native-response shapes. They
prove the diagnostic client extracts authoritative native timings (the fields the
OpenAI-compat endpoint omits) and that its raw timing dict feeds the shared ns→ms
extractor, so there is one conversion authority.
"""
from __future__ import annotations

import json

import pytest

from youtab_runtime.model_timings import extract_native_ollama_timings
from youtab_runtime.ollama_native import (
    DEFAULT_NATIVE_BASE_URL,
    _assert_local_host,
    derive_native_base_url,
    parse_native_nonstream,
    parse_native_stream,
)


@pytest.mark.parametrize(
    "given,expected",
    [
        ("http://localhost:11434/v1", "http://localhost:11434"),
        ("http://localhost:11434/v1/", "http://localhost:11434"),
        ("http://host:11434", "http://host:11434"),
        ("http://host:11434/", "http://host:11434"),
        ("https://ollama.lan:443/v1", "https://ollama.lan:443"),
        ("", DEFAULT_NATIVE_BASE_URL),
        (None, DEFAULT_NATIVE_BASE_URL),
    ],
)
def test_derive_native_base_url(given, expected):
    assert derive_native_base_url(given) == expected


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434",
    "http://192.168.1.5:11434",   # RFC-1918 private (LAN Ollama)
    "http://10.0.0.9:11434",
    "http://[::1]:11434",         # IPv6 loopback
])
def test_assert_local_host_accepts_loopback_and_private(url):
    _assert_local_host(url)  # must not raise


@pytest.mark.parametrize("url", [
    "http://8.8.8.8:11434",       # public IP → refuse
    "http://1.1.1.1/v1",
])
def test_assert_local_host_rejects_public(url):
    with pytest.raises(ValueError):
        _assert_local_host(url)


def _final_stream_line():
    # The real shape of Ollama's terminal (done:true) /api/chat streamed object.
    return {
        "model": "qwen3.5:9b",
        "created_at": "2026-09-07T00:00:00Z",
        "message": {"role": "assistant", "content": ""},
        "done_reason": "stop",
        "done": True,
        "total_duration": 5_589_157_167,
        "load_duration": 3_013_701_500,
        "prompt_eval_count": 21421,
        "prompt_eval_duration": 342_546_000,
        "eval_count": 49,
        "eval_duration": 4_799_921_000,
    }


def test_parse_stream_aggregates_content_timings_and_ttft():
    lines = [
        json.dumps({"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "Hel"}, "done": False}),
        "",  # blank line ignored
        "not json",  # garbage ignored
        json.dumps({"message": {"content": "lo"}, "done": False}),
        json.dumps(_final_stream_line()),
    ]
    # Deterministic injected clock: opened at 100.0, first content chunk at 100.5.
    ticks = iter([100.5])
    result = parse_native_stream(lines, opened_at=100.0, first_token_clock=lambda: next(ticks))

    assert result.content == "Hello"
    assert result.model == "qwen3.5:9b"
    assert result.done_reason == "stop"
    assert result.ttft_s == pytest.approx(0.5)
    # Native timings captured verbatim (nanoseconds) from the terminal object.
    assert result.raw_timings["prompt_eval_count"] == 21421
    assert result.raw_timings["eval_count"] == 49
    assert result.raw_timings["total_duration"] == 5_589_157_167


def test_parse_stream_captures_tool_calls():
    lines = [
        json.dumps(
            {
                "model": "qwen3.5:9b",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "kanban_show", "arguments": {"task_id": "t1"}}}],
                },
                "done": False,
            }
        ),
        json.dumps(_final_stream_line()),
    ]
    ticks = iter([10.2])
    result = parse_native_stream(lines, opened_at=10.0, first_token_clock=lambda: next(ticks))
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "kanban_show"
    # A tool call counts as "first token" for TTFT purposes.
    assert result.ttft_s == pytest.approx(0.2)


def test_parse_stream_ttft_omitted_without_clock():
    lines = [json.dumps(_final_stream_line())]
    result = parse_native_stream(lines)  # no clock injected
    assert result.ttft_s is None


def test_parse_stream_ttft_from_receive_times_not_drain_time():
    # The receive-time path (what native_chat uses): the first CONTENT line arrived
    # at t=100.3; the final line (with timings) arrived much later at t=120.0. TTFT
    # must be 0.3s (first token), NOT ~20s (full stream drain) — this is the HIGH-1
    # regression guard.
    lines = [
        json.dumps({"model": "qwen3.5:9b", "message": {"content": ""}, "done": False}),  # empty content
        json.dumps({"message": {"content": "Hi"}, "done": False}),   # FIRST real token
        json.dumps(_final_stream_line()),
    ]
    recv = [100.05, 100.3, 120.0]  # aligned to raw line positions
    result = parse_native_stream(lines, opened_at=100.0, line_recv_times=recv)
    assert result.ttft_s == pytest.approx(0.3)
    assert result.raw_timings["prompt_eval_count"] == 21421


def test_parse_nonstream():
    obj = dict(_final_stream_line())
    obj["message"] = {"role": "assistant", "content": "done"}
    result = parse_native_nonstream(obj)
    assert result.content == "done"
    assert result.raw_timings["prompt_eval_count"] == 21421
    assert result.chunk_count == 1


def test_raw_timings_feed_shared_ns_to_ms_extractor():
    # The client's raw ns dict must convert identically through the one authority.
    result = parse_native_stream([json.dumps(_final_stream_line())])
    native = extract_native_ollama_timings(result.raw_timings)
    assert native["total_duration_ms"] == pytest.approx(5589.157, abs=1e-3)
    assert native["load_duration_ms"] == pytest.approx(3013.7015, abs=1e-3)
    assert native["prompt_eval_count"] == 21421
    assert native["eval_count"] == 49
    # Generation throughput derived only from the native pair: 49 / 4.799921 s.
    assert native["eval_tokens_per_second"] == pytest.approx(10.208, abs=0.05)
