"""P0-E BASELINE CHARACTERIZATION — effect-then-die duplicate-effect window.

Ownership: Lane 1/2 own the canonical external-effect ledger
(`youtab_runtime/effect_ledger.py`, WAVE-26: effect_id + provider_idempotency_key
+ immutable committed receipt + UNKNOWN-on-recover). This stream owns task
continuation and must integrate through that boundary — NOT build a competing
ledger.

This test proves the BASE gap on origin/main (0.19.1):
1. the canonical effect ledger is ABSENT on base (Lane deliverable, not merged);
2. Kanban's retry path re-opens a failed task for re-execution with only a
   whole-task create-time idempotency_key — there is NO per-effect idempotency,
   so a worker that performed an external effect and then died before
   `kanban_complete` can be re-run, repeating the effect.

Distinctions the durable design must preserve (see the typed interface request):
rejection-before-exec -> no effect row; crash-after-possible-effect ->
UNKNOWN/RECONCILIATION_REQUIRED; confirmed-committed -> immutable receipt;
never blind-retry an unknown non-idempotent effect.
"""

import importlib.util
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


def test_canonical_effect_ledger_absent_on_base():
    """The Lane effect ledger is not present on origin/main — durable execution
    must call it as a typed boundary, not reimplement it."""
    assert importlib.util.find_spec("youtab_runtime.effect_ledger") is None, (
        "effect_ledger unexpectedly present — if Lane merged, switch P0-E to "
        "integrate with the real boundary instead of characterizing its absence"
    )
    # No 'effects' idempotency table anywhere in the kanban schema either.
    assert not Path("youtab_runtime/effect_ledger.py").exists()


def test_kanban_retry_reopens_task_without_per_effect_idempotency(kanban_home):
    conn = kb.connect()
    try:
        # Task created WITH a whole-task idempotency key (create-time dedup only).
        task_id = kb.create_task(
            conn, title="governed ERP update", body="POST /invoice",
            idempotency_key="erp-invoice-777",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (task_id,))
        assert kb.claim_task(conn, task_id) is not None

        # Worker performs the external effect (e.g. POSTs the invoice) and then
        # dies BEFORE calling kanban_complete. Modeled as a non-blocking failure.
        blocked = kb._record_task_failure(
            conn, task_id, "worker died after effect, before complete",
            outcome="crashed", failure_limit=2, release_claim=True, end_run=True,
        )
        assert blocked is False  # under the limit -> returns to 'ready'

        status = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        assert status == "ready", "failed-after-effect task is re-queued"

        # DEFECT WINDOW: the task can be re-claimed and re-run — the already
        # performed external effect would be repeated. Nothing consults a
        # per-effect idempotency/receipt (none exists on base).
        reclaim = kb.claim_task(conn, task_id)
        assert reclaim is not None, "re-execution is possible -> duplicate-effect risk"

        # Prove there is no per-effect idempotency surface on base: the only
        # idempotency is the whole-task create-time key.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "idempotency_key" in cols
        assert not any("effect" in c for c in cols), (
            "no per-effect idempotency column exists on base"
        )
        assert "effects" not in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }, "no canonical effects ledger table on base"
    finally:
        conn.close()
