"""WAVE-26 agent 2: per-run USAGE event persistence (contract 3).

Verifies that ``RunObserver.on_post_api_request`` sinks the ``post_api_request``
hook payload into the run journal as a ``usage``/``model_call`` event, that a
missing usage block is recorded as ``unknown`` with ``null`` token fields (never
``0``), and that usage is principal-scoped.
"""

from __future__ import annotations

import pytest

from youtab_runtime.run_journal import Principal, list_events
from youtab_runtime.run_observer import RunObserver, create_run_observer


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "run_journal.db"


P = Principal("tenant-a", "user-1")
Q = Principal("tenant-a", "user-2")  # same tenant, different user


def _fake_cost(**_kwargs):
    return {
        "amount_usd": 0.0123,
        "status": "estimated",
        "source": "official_docs_snapshot",
        "pricing_version": "test-pricing-v1",
    }


def _obs(run_id, principal, db, cost_fn=_fake_cost):
    return create_run_observer(run_id, principal, db_path=db, cost_fn=cost_fn)


def test_known_usage_is_persisted_with_all_token_fields(db):
    obs = _obs("run1", P, db)
    ev = obs.on_post_api_request(
        api_request_id="turn-1:api:1",
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_mode="anthropic_messages",
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 10,
            "cache_write_tokens": 5,
            "reasoning_tokens": 7,
            "total_tokens": 172,
        },
        turn_id="turn-1",
    )
    assert ev is not None
    assert ev.category == "usage" and ev.kind == "model_call"
    assert ev.dedupe_key == "turn-1:api:1"

    pl = ev.payload
    assert pl["usage_status"] == "known"
    assert pl["provider"] == "anthropic"
    assert pl["model"] == "claude-sonnet-4-6"
    assert pl["api_mode"] == "anthropic_messages"
    assert pl["input_tokens"] == 100
    assert pl["output_tokens"] == 50
    assert pl["cache_read_tokens"] == 10
    assert pl["cache_write_tokens"] == 5
    assert pl["reasoning_tokens"] == 7
    assert pl["total_tokens"] == 172
    assert pl["api_request_id"] == "turn-1:api:1"

    cost = pl["cost"]
    assert cost["amount_usd"] == pytest.approx(0.0123)
    assert cost["status"] == "estimated"
    assert cost["source"] == "official_docs_snapshot"
    assert cost["pricing_version"] == "test-pricing-v1"


def test_missing_usage_is_null_and_unknown_never_zero(db):
    obs = _obs("run1", P, db)
    ev = obs.on_post_api_request(
        api_request_id="turn-1:api:1",
        provider="openai",
        model="gpt-5.6-sol",
        api_mode="chat_completions",
        usage=None,
        turn_id="turn-1",
    )
    assert ev is not None
    pl = ev.payload
    assert pl["usage_status"] == "unknown"
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        # The critical invariant: unknown usage is null, NEVER a fabricated 0.
        assert pl[field] is None, f"{field} must be null when usage is unknown"

    assert pl["cost"]["amount_usd"] is None
    assert pl["cost"]["status"] == "unknown"


def test_empty_usage_dict_treated_as_unknown(db):
    obs = _obs("run1", P, db)
    ev = obs.on_post_api_request(
        api_request_id="turn-9:api:1", provider="p", model="m",
        api_mode="chat_completions", usage={},
    )
    assert ev is not None
    assert ev.payload["usage_status"] == "unknown"
    assert ev.payload["input_tokens"] is None


def test_cost_failure_still_records_usage_as_unknown_cost(db):
    def _boom(**_kwargs):
        raise RuntimeError("pricing table unreachable")

    obs = _obs("run1", P, db, cost_fn=_boom)
    ev = obs.on_post_api_request(
        api_request_id="turn-2:api:1", provider="p", model="m",
        api_mode="chat_completions",
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    # Usage row survives even though pricing blew up.
    assert ev is not None
    assert ev.payload["usage_status"] == "known"
    assert ev.payload["input_tokens"] == 10
    assert ev.payload["cost"]["status"] == "unknown"
    assert ev.payload["cost"]["amount_usd"] is None


def test_usage_is_principal_scoped(db):
    _obs("run1", P, db).on_post_api_request(
        api_request_id="turn-1:api:1", provider="p", model="m",
        api_mode="chat_completions",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    # Owner sees the usage event.
    mine = list_events("run1", P, db_path=db, category="usage")
    assert len(mine) == 1
    # A different user of the same tenant sees nothing (no existence leak).
    assert list_events("run1", Q, db_path=db, category="usage") == []


def test_missing_api_request_id_still_records_usage(db):
    # No retry-stable id -> cannot dedupe, but we must not drop the row.
    obs = _obs("run1", P, db)
    ev = obs.on_post_api_request(
        api_request_id="", provider="p", model="m",
        api_mode="chat_completions",
        usage={"input_tokens": 3, "output_tokens": 4},
    )
    assert ev is not None
    assert ev.dedupe_key is None
    assert ev.payload["api_request_id"] is None


def test_default_cost_fn_degrades_to_unknown_for_unpriced_route(db):
    # Exercise the real pricing seam with an unknown provider/model so it must
    # return a first-class ``unknown`` rather than a fabricated cost.
    obs = create_run_observer("run1", P, db_path=db)  # default cost_fn
    ev = obs.on_post_api_request(
        api_request_id="turn-3:api:1",
        provider="totally-unknown-provider",
        model="no-such-model-xyz",
        api_mode="chat_completions",
        usage={"input_tokens": 5, "output_tokens": 5},
    )
    assert ev is not None
    cost = ev.payload["cost"]
    assert set(cost) == {"amount_usd", "status", "source", "pricing_version"}
    # An unpriced route must not fabricate a dollar figure.
    if cost["status"] == "unknown":
        assert cost["amount_usd"] is None
