"""Deterministic integration worker (NON-PRODUCTION, AR-PROD-01 preview).

An honestly-labelled stand-in for the real model-backed agent worker, used ONLY
for local/staging integration when no development model credential is configured.
It is a *real* external process that exercises the *real* run lifecycle — it
opens its own kanban connection, emits ordered events, writes a real artifact,
and marks the task complete — but its "reasoning" is a fixed transform of the
task, NOT a language model. Every event and the result are explicitly tagged
``[deterministic-integration-agent]`` so no viewer mistakes it for a real LLM.

Activation is gated by :func:`youtab_agent_cli.web_routers.runtime` via the
``YOUTAB_AGENT_RUNTIME_DETERMINISTIC_WORKER`` env flag, which is refused in
production. This module never runs a model and never touches production data.

Invoked as: ``python -m youtab_agent_cli.runtime_integration_worker <task_id> <db_path>``
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: runtime_integration_worker <task_id> <db_path>", file=sys.stderr)
        return 2
    task_id = argv[1]
    db_path = Path(argv[2])

    from youtab_agent_cli import kanban_db as kb

    # A short, visible amount of "work" so live event streaming is observable.
    conn = kb.connect(db_path=db_path)
    try:
        task = kb.get_task(conn, task_id)
        task_title = task.title if task else task_id
        task_body = (task.body if task and task.body else task_title) or ""

        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_started",
                             {"agent": "deterministic-integration-agent", "pid": os.getpid()})
        time.sleep(0.4)
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_progress",
                             {"step": "analyzing task", "note": "[deterministic-integration-agent]"})
        time.sleep(0.4)
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_progress",
                             {"step": "producing result", "note": "[deterministic-integration-agent]"})

        summary = f"[deterministic-integration-agent] Completed task: {task_title}"
        result = (
            f"[deterministic-integration-agent] This run was executed by the "
            f"deterministic integration worker (no language model was called).\n\n"
            f"Task received:\n{task_body}\n\n"
            f"Deterministic outcome: task acknowledged and processed end-to-end "
            f"through the real Youtab Agent Runtime lifecycle."
        )
        artifact = (
            "[deterministic-integration-agent] artifact\n"
            f"task_id: {task_id}\n"
            f"task: {task_title}\n"
            "status: completed\n"
        ).encode("utf-8")
        kb.store_attachment_bytes(
            conn, task_id, "integration_result.txt", artifact, content_type="text/plain",
        )
        kb.complete_task(conn, task_id, result=result, summary=summary)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
