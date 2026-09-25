"""WAVE-26 agent 2: usage + tool idempotency (contract 4).

A retried API call carries the SAME retry-stable ``api_request_id``
(``"{turn_id}:api:{api_call_count}"`` computed before the retry loop), so the
observer must record exactly ONE usage row per ``api_request_id`` no matter how
many times the hook fires. Likewise a re-dispatched tool (same ``tool_call_id``)
must not duplicate its call/result rows.
"""

from __future__ import annotations

import pytest

from youtab_runtime.run_journal import Principal, list_events
from youtab_runtime.run_observer import create_run_observer


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "run_journal.db"


P = Principal("tenant-a", "user-1")


def _fake_cost(**_kwargs):
    return {"amount_usd": 0.01, "status": "estimated", "source": "s",
            "pricing_version": "v1"}


def _obs(run_id, db):
    return create_run_observer(run_id, P, db_path=db, cost_fn=_fake_cost)


def test_retry_same_api_request_id_yields_exactly_one_usage_row(db):
    obs = _obs("run1", db)
    req_id = "turn-1:api:1"

    first = obs.on_post_api_request(
        api_request_id=req_id, provider="p", model="m", api_mode="cc",
        usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    )
    # The retry: identical request id, but (say) the usage block differs / is
    # absent. The first authoritative row must win; no double-count.
    second = obs.on_post_api_request(
        api_request_id=req_id, provider="p", model="m", api_mode="cc",
        usage=None,
    )

    rows = list_events("run1", P, db_path=db, category="usage")
    assert len(rows) == 1, "a retried api_request_id must not double-count"
    # Idempotent append returns the original row.
    assert first.seq == second.seq == rows[0].seq
    # The first writer's known usage is preserved (retry did not clobber it).
    assert rows[0].payload["usage_status"] == "known"
    assert rows[0].payload["input_tokens"] == 100


def test_distinct_api_request_ids_each_count(db):
    obs = _obs("run1", db)
    for i in range(1, 4):
        obs.on_post_api_request(
            api_request_id=f"turn-1:api:{i}", provider="p", model="m",
            api_mode="cc", usage={"input_tokens": 1, "output_tokens": 1},
        )
    rows = list_events("run1", P, db_path=db, category="usage")
    assert len(rows) == 3
    assert sorted(r.payload["api_request_id"] for r in rows) == [
        "turn-1:api:1", "turn-1:api:2", "turn-1:api:3"
    ]


def test_unknown_usage_first_then_known_retry_keeps_first(db):
    # If the first observation had no usage and the retry does, the dedupe still
    # honours the first writer: the row stays unknown. This is intentional — the
    # api_request_id is retry-stable and the first append is authoritative.
    obs = _obs("run1", db)
    req_id = "turn-2:api:1"
    obs.on_post_api_request(api_request_id=req_id, provider="p", model="m",
                            api_mode="cc", usage=None)
    obs.on_post_api_request(api_request_id=req_id, provider="p", model="m",
                            api_mode="cc",
                            usage={"input_tokens": 9, "output_tokens": 9})
    rows = list_events("run1", P, db_path=db, category="usage")
    assert len(rows) == 1
    assert rows[0].payload["usage_status"] == "unknown"
    assert rows[0].payload["input_tokens"] is None


def test_tool_redispatch_same_id_is_idempotent(db):
    obs = _obs("run1", db)
    for _ in range(3):
        obs.on_post_tool_call(
            tool_name="t", args={"a": 1}, result="r", tool_call_id="tc-1",
            status="ok",
        )
    calls = list_events("run1", P, db_path=db, category="tool_call")
    results = list_events("run1", P, db_path=db, category="tool_result")
    assert len(calls) == 1
    assert len(results) == 1


def test_usage_and_tool_share_id_space_without_collision(db):
    # tool_call and tool_result both key on tool_call_id but live in different
    # categories, so the dedupe index (run_id, category, dedupe_key) keeps them
    # as two distinct rows.
    obs = _obs("run1", db)
    obs.on_post_tool_call(tool_name="t", args={}, result="r",
                          tool_call_id="shared-id", status="ok")
    calls = list_events("run1", P, db_path=db, category="tool_call")
    results = list_events("run1", P, db_path=db, category="tool_result")
    assert len(calls) == 1 and len(results) == 1
    assert calls[0].dedupe_key == results[0].dedupe_key == "shared-id"
    assert calls[0].seq != results[0].seq
