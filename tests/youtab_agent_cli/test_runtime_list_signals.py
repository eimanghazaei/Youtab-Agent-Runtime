"""WAVE-30G: the batched run-list signal query must exactly reproduce the
per-task ``_is_cancelled`` / ``_mode_from_events`` semantics it replaced (the
N+1 that loaded every run's full event history just to detect cancel + mode).
"""
from __future__ import annotations

import json
import sqlite3

from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli.web_routers import runtime as rt


def _conn_with_events(rows):
    """In-memory task_events table mirroring the columns the query reads.

    ``rows`` is a list of (task_id, kind, payload_obj_or_None). ids are assigned
    in insertion order so created_at/id ascending == insertion order.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE task_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id TEXT NOT NULL,"
        "  kind TEXT NOT NULL,"
        "  payload TEXT,"
        "  created_at INTEGER NOT NULL"
        ")"
    )
    for i, (task_id, kind, payload) in enumerate(rows):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, kind, json.dumps(payload) if payload is not None else None, i),
        )
    conn.commit()
    return conn


def _events_for(conn, task_id):
    """Reconstruct kb.Event objects (as list_events would) for one task."""
    out = []
    for r in conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall():
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(
            kb.Event(id=r["id"], task_id=r["task_id"], kind=r["kind"],
                     payload=payload, created_at=r["created_at"], run_id=None)
        )
    return out


def test_summary_signals_matches_per_task_semantics():
    cancel = rt._CANCEL_EVENT_KIND
    mode = rt._MODE_EVENT
    rows = [
        # A: cancel + two mode events — first valid mode (deterministic) wins.
        ("A", mode, {"mode": "deterministic"}),
        ("A", mode, {"mode": "model"}),
        ("A", cancel, {"by": "u"}),
        # B: single model mode, no cancel.
        ("B", mode, {"mode": "model"}),
        # C: no events at all (absent from table) — handled by caller default.
        # D: invalid mode payload → falls through to "model" default.
        ("D", mode, {"mode": "bogus"}),
        ("D", mode, {"mode": "deterministic"}),  # first VALID mode wins
        # E: cancel only, no mode.
        ("E", cancel, {"by": "u"}),
        # noise: an unrelated event kind must be ignored.
        ("B", "runtime_execution_mode_unrelated", {"mode": "deterministic"}),
    ]
    conn = _conn_with_events(rows)
    task_ids = ["A", "B", "C", "D", "E"]

    got = rt._summary_signals(conn, task_ids)

    # Explicit expectations.
    assert got["A"] == (True, "deterministic")
    assert got["B"] == (False, "model")
    assert got["C"] == (False, "model")   # no rows → default
    assert got["D"] == (False, "deterministic")  # first VALID mode wins
    assert got["E"] == (True, "model")    # cancel, mode defaults

    # Equivalence with the original per-task helpers over identical rows.
    for tid in task_ids:
        events = _events_for(conn, tid)
        assert got[tid] == (rt._is_cancelled(events), rt._mode_from_events(events))


def test_summary_signals_empty_input():
    conn = _conn_with_events([])
    assert rt._summary_signals(conn, []) == {}


def _insert_raw(conn, task_id, kind, raw_payload, created_at):
    """Insert a row with a RAW (already-serialized / possibly malformed) payload."""
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (task_id, kind, raw_payload, created_at),
    )
    conn.commit()


def test_summary_signals_edge_payloads_match_helpers():
    """Non-dict JSON, malformed JSON, and case/whitespace mode values all match
    the per-task helpers exactly (the isinstance / except / normalize paths)."""
    mode = rt._MODE_EVENT
    conn = _conn_with_events([])
    # F: valid-JSON but NOT a dict -> ignored, then a normalizable valid mode wins.
    _insert_raw(conn, "F", mode, json.dumps(["model"]), 0)
    _insert_raw(conn, "F", mode, json.dumps("deterministic"), 1)
    _insert_raw(conn, "F", mode, json.dumps({"mode": "  DETERMINISTIC "}), 2)
    # G: malformed JSON payload -> except->None -> skipped; no valid mode -> default.
    _insert_raw(conn, "G", mode, "{not valid json", 3)
    # H: valid dict but mode value is not in the allowed set -> default "model".
    _insert_raw(conn, "H", mode, json.dumps({"mode": "hybrid"}), 4)

    task_ids = ["F", "G", "H"]
    got = rt._summary_signals(conn, task_ids)
    assert got["F"] == (False, "deterministic")  # normalized, non-dict skipped
    assert got["G"] == (False, "model")
    assert got["H"] == (False, "model")
    for tid in task_ids:
        events = _events_for(conn, tid)
        assert got[tid] == (rt._is_cancelled(events), rt._mode_from_events(events))


def test_summary_signals_interleaved_created_order():
    """Rows interleaved across tasks in created-order still resolve per-task
    first-valid-mode-wins correctly."""
    mode = rt._MODE_EVENT
    cancel = rt._CANCEL_EVENT_KIND
    conn = _conn_with_events([])
    # created_at interleaves X and Y; X's first valid mode is 'model', Y's is
    # 'deterministic', and X gets cancelled after its mode.
    _insert_raw(conn, "X", mode, json.dumps({"mode": "model"}), 0)
    _insert_raw(conn, "Y", mode, json.dumps({"mode": "deterministic"}), 1)
    _insert_raw(conn, "X", cancel, json.dumps({"by": "u"}), 2)
    _insert_raw(conn, "Y", mode, json.dumps({"mode": "model"}), 3)  # later, ignored
    got = rt._summary_signals(conn, ["X", "Y"])
    assert got["X"] == (True, "model")
    assert got["Y"] == (False, "deterministic")


def test_summary_signals_chunks_beyond_variable_limit():
    """More tasks than the chunk size must not raise 'too many SQL variables'
    and must still return a signal for every task."""
    conn = _conn_with_events([])
    n = 2100  # > one 900-chunk
    for i in range(n):
        if i % 3 == 0:
            _insert_raw(conn, f"t{i}", rt._CANCEL_EVENT_KIND, json.dumps({"by": "u"}), i)
    task_ids = [f"t{i}" for i in range(n)]
    got = rt._summary_signals(conn, task_ids)
    assert len(got) == n
    assert got["t0"] == (True, "model")
    assert got["t1"] == (False, "model")
