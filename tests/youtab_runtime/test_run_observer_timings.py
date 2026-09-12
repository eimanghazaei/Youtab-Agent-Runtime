"""WAVE-30E — per-call timing record persists through the journal, redaction-safe."""
from __future__ import annotations

import pytest

from youtab_runtime.model_timings import build_call_timings, extract_native_ollama_timings
from youtab_runtime.run_journal import Principal, list_events
from youtab_runtime.run_observer import create_run_observer

P = Principal("tenant-a", "user-1")


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "run_journal.db"


def _native():
    return extract_native_ollama_timings({
        "total_duration": 5_589_157_167,
        "load_duration": 3_013_701_500,
        "prompt_eval_count": 21421,
        "prompt_eval_cached_count": 0,
        "prompt_eval_duration": 120_000_000_000,
        "eval_count": 49,
        "eval_duration": 8_000_000_000,
    })


def test_timings_persist_all_numeric(db):
    obs = create_run_observer("run-t", P, db_path=db)
    rec = build_call_timings(wall_s=181.0, ttft_s=27.5, native=_native(), cold_start=True)
    ev = obs.on_post_api_request(
        api_request_id="turn-1:api:1", provider="ollama",
        model="youtab-qwen35-9b-agent-64k:latest", api_mode="chat_completions",
        usage={"input_tokens": 21421, "output_tokens": 49, "total_tokens": 21470},
        base_url="http://127.0.0.1:11435/v1", turn_id="turn-1",
        timings=rec,
    )
    assert ev is not None
    t = ev.payload["timings"]
    # Native fields survived (they are the whole point).
    assert t["total_duration_ms"] == pytest.approx(5589.157, abs=1e-3)
    assert t["load_duration_ms"] == pytest.approx(3013.7015, abs=1e-3)
    assert t["prompt_eval_count"] == 21421
    assert t["eval_count"] == 49
    assert t["wall_ms"] == pytest.approx(181000.0)
    assert t["ttft_ms"] == pytest.approx(27500.0)
    assert t["cold_start"] is True
    assert t["native_timings_available"] is True
    # Every leaf is numeric/bool — no free text ever.
    for v in t.values():
        assert isinstance(v, (int, float, bool))

    # And it round-trips through the durable journal read.
    events = list_events("run-t", P, db_path=db, category="usage")
    assert len(events) == 1
    assert events[0].payload["timings"]["prompt_eval_count"] == 21421


def test_timings_cannot_smuggle_prompt_or_secret_text(db):
    obs = create_run_observer("run-x", P, db_path=db)
    # A malformed/hostile timings dict with prompt + secret text must be scrubbed
    # to nothing but its numeric leaves.
    hostile = {
        "wall_ms": 100.0,
        "prompt": "the full 21,421-token user prompt that must never persist",
        "authorization": "Bearer sk-secret-xyz",
        "raw_response": "assistant said ...",
    }
    ev = obs.on_post_api_request(
        api_request_id="turn-2:api:1", provider="ollama", model="m",
        api_mode="chat_completions",
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        timings=hostile,
    )
    t = ev.payload["timings"]
    assert t == {"wall_ms": 100.0}
    assert "prompt" not in t and "authorization" not in t and "raw_response" not in t


def test_usage_row_unaffected_when_no_timings(db):
    obs = create_run_observer("run-n", P, db_path=db)
    ev = obs.on_post_api_request(
        api_request_id="turn-3:api:1", provider="ollama", model="m",
        api_mode="chat_completions",
        usage={"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
    )
    assert "timings" not in ev.payload  # optional; absent → no key
    assert ev.payload["input_tokens"] == 5
