"""Phase 3-D (priority 1): delegate_tool parent-wait vs child-lifetime decoupling.

DESIRED-INVARIANT: when the parent's configured child_timeout (a WAIT budget)
elapses on a PROGRESSING child, _run_single_child DETACHES — returns RUNNING +
task_id (never bare summary=None), does NOT kill the child, and the child keeps
running under its lease with its eventual result captured durably in the RunStore
for reconnect. A stuck 0-API-call child still times out (diagnostic path). Explicit
interrupt/cancel is a separate path from the wait-budget timeout.
"""

import threading
import time
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from tools import delegate_tool
from youtab_runtime.durable_run_store import create_run_store


@pytest.fixture
def youtab_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    return home


class _ProgressingChild:
    """A child that has made API calls and keeps working past the wait budget."""

    def __init__(self, release: threading.Event):
        self._subagent_id = "sa-0-detach"
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self.model = "test/model"
        self.provider = "testprov"
        self.api_mode = "chat_completions"
        self.base_url = "https://example.test/v1"
        self.max_iterations = 30
        self.quiet_mode = True
        self.enabled_toolsets = ["web"]
        self.valid_tool_names = {"web_search"}
        self.tools = [{"name": "web_search", "description": "s"}]
        self.ephemeral_system_prompt = "sys"
        self._release = release
        self.interrupted = False

    def get_activity_summary(self):
        return {"api_call_count": 3, "max_iterations": 30, "current_tool": None,
                "seconds_since_activity": 1}

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        # Keep working until released (models a healthy long task), then finish.
        self._release.wait(5.0)
        return {"final_response": "DELEGATE_RESULT_ONCE", "completed": True, "api_calls": 3}

    def interrupt(self):
        self.interrupted = True
        self._release.set()


def test_progressing_child_detaches_on_wait_budget_and_completes_once(youtab_home, monkeypatch):
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    release = threading.Event()
    child = _ProgressingChild(release)
    parent = MagicMock()
    parent._touch_activity = MagicMock()
    parent._current_task_id = None

    result = delegate_tool._run_single_child(0, "long enterprise task", child=child, parent_agent=parent)

    # Wait budget elapsed -> DETACH, not kill.
    assert result["status"] == "running"
    assert result["detached"] is True
    assert result["summary"] is None
    task_id = result["task_id"]
    assert task_id and "RunStore" in result["recovery_action"]
    assert child.interrupted is False, "a progressing child must NOT be killed on wait timeout"

    # Durable run exists and is RUNNING (child still going).
    store = create_run_store("sqlite")
    row = store.get_run(task_id)
    assert row is not None and row["state"] in ("RUNNING", "SUCCEEDED")

    # Let the child finish; its result is captured durably exactly once.
    release.set()
    deadline = time.time() + 8
    while time.time() < deadline:
        row = store.get_run(task_id)
        if row and row["state"] == "SUCCEEDED":
            break
        time.sleep(0.1)
    assert row["state"] == "SUCCEEDED", "detached child must complete and be recorded"
    assert row["result_ref"] == "DELEGATE_RESULT_ONCE"
    succ = [e for e in store.get_events(task_id, from_seq=0) if e["kind"] == "delegate.completed"]
    assert len(succ) == 1, "exactly-once terminal capture"


def test_explicit_interrupt_is_separate_from_wait_timeout(youtab_home, monkeypatch):
    """Explicit interrupt/cancel stops the child; distinct from the wait budget."""
    release = threading.Event()
    child = _ProgressingChild(release)
    # Direct interrupt (as the /stop path or parent cancel would trigger).
    child.interrupt()
    assert child.interrupted is True
    assert release.is_set()
