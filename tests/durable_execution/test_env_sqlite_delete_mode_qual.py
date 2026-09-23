"""Environment qualification — Kanban claim fencing under SQLite DELETE journal mode.

The authoritative env links SQLite 3.49.1, which is vulnerable to the WAL-reset
corruption bug; youtab_state therefore opens fresh DBs in journal_mode=DELETE
(pre-WAL default) instead of WAL. DELETE mode changes concurrency (writers take
an exclusive lock; no concurrent readers during a write) but must NOT weaken the
claim/fencing CORRECTNESS the durable model relies on.

This test qualifies that exactly-one-winner claim semantics hold under DELETE
mode: N concurrent claimers race for one ready task; exactly one wins.
"""

import threading
from pathlib import Path

import pytest

from youtab_agent_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_journal_mode_is_delete_on_vulnerable_sqlite(kanban_home):
    conn = kb.connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()
    # On the linked 3.49.1 build the safeguard selects DELETE (or truncate/persist
    # family); it must never silently be WAL on a vulnerable build.
    assert mode in ("delete", "truncate", "persist", "memory", "off"), mode


def test_exactly_one_claimer_wins_under_delete_mode(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="contended task", body="x")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?",
                (task_id,),
            )
    finally:
        conn.close()

    winners = []
    barrier = threading.Barrier(6)

    def _try_claim(i):
        c = kb.connect()
        try:
            barrier.wait()
            claimed = kb.claim_task(c, task_id, claimer=f"host:worker-{i}")
            if claimed is not None:
                winners.append(f"worker-{i}")
        finally:
            c.close()

    threads = [threading.Thread(target=_try_claim, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(winners) == 1, f"exactly one claimer must win; got {winners}"
