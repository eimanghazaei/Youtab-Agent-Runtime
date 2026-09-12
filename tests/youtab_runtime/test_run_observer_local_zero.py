"""WAVE-30D — verified-local €0 is recorded auditably, and the full agent loop
(model_call -> tool_call -> tool_result) is journalled WITHOUT persisting raw
prompts/results.

Uses the REAL cost function (``estimate_usage_cost``) so the local-zero pricing
branch is exercised end-to-end through the observer, exactly as the live worker's
``post_api_request`` hook drives it (it passes ``base_url=agent.base_url``).
"""
from __future__ import annotations

import json

import pytest

from youtab_runtime.run_journal import Principal, list_events
from youtab_runtime.run_observer import create_run_observer

P = Principal("tenant-a", "user-1")
_ECO_TAG = "youtab-qwen35-9b-agent-64k:latest"


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "run_journal.db"


def test_local_ollama_usage_records_exactly_zero_cost(db):
    obs = create_run_observer("run-local", P, db_path=db)  # real cost_fn
    ev = obs.on_post_api_request(
        api_request_id="t1:api:1",
        provider="ollama",
        model=_ECO_TAG,
        api_mode="chat_completions",
        base_url="http://127.0.0.1:11434",
        usage={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
        turn_id="t1",
    )
    assert ev is not None
    cost = ev.payload["cost"]
    assert cost["amount_usd"] == 0.0            # exactly €0, recorded (not absent)
    assert cost["status"] == "local_zero"
    assert cost["source"] == "verified_local"


def test_remote_unknown_model_records_unknown_not_zero(db):
    obs = create_run_observer("run-remote", P, db_path=db)
    ev = obs.on_post_api_request(
        api_request_id="t1:api:1",
        provider="ollama",
        model="mystery-model",
        base_url="http://8.8.8.8:11434",  # remote => never local-zero
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        turn_id="t1",
    )
    cost = ev.payload["cost"]
    assert cost["amount_usd"] is None           # first-class unknown, never a fake $0
    assert cost["status"] == "unknown"


def test_full_tool_loop_recorded_without_raw_prompt_or_result(db):
    obs = create_run_observer("run-loop", P, db_path=db)
    # model call
    obs.on_post_api_request(
        api_request_id="t1:api:1", provider="ollama", model=_ECO_TAG,
        base_url="http://127.0.0.1:11434",
        usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}, turn_id="t1",
    )
    # tool call + result — carry a distinctive secret-shaped string in the result.
    secret_marker = "SENTINEL-raw-model-output-must-not-be-persisted"
    obs.on_post_tool_call(
        tool_name="write_file",
        args={"path": "answer.txt", "content": "42"},
        result={"stdout": secret_marker, "ok": True},
        tool_call_id="call-1",
        turn_id="t1",
        api_request_id="t1:api:1",
        status="ok",
    )

    events = list(list_events("run-loop", P, limit=100, db_path=db))
    cats = [(e.category, e.kind) for e in events]
    assert ("usage", "model_call") in cats
    assert any(c == "tool_call" for c, _ in cats)
    assert any(c == "tool_result" for c, _ in cats)

    # The raw tool result string must never be persisted anywhere in the journal.
    blob = json.dumps([e.payload for e in events])
    assert secret_marker not in blob
    # The tool_result records a digest, not the raw value.
    tr = next(e for e in events if e.category == "tool_result")
    assert "digest" in json.dumps(tr.payload)
