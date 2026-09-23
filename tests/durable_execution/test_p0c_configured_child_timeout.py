"""P0-C — configured child timeout (CORRECTED: now decoupled from child lifetime).

NOTE: originally characterized the C-2.1c defect (timeout -> summary=None, no
recoverable state). That defect is now FIXED: parent-wait expiry DETACHES the
admitted child with a durable task reference. git history preserves the baseline.

Durable-execution requirement: when a child hits a configured timeout, the parent
must receive the latest checkpoint / last progress / changed files / child id and
a recovery action — not merely "timed out" with no result.

With a small injected child timeout (does not wait 600 real seconds), the
admitted child is DETACHED: `_run_single_child` returns status="running" +
task_id + recovery_action, and the child is not interrupted.
"""

import threading
import time

import pytest

from tools import delegate_tool as dt


@pytest.fixture
def youtab_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    return home


class _HangingChild:
    """Child that blocks in run_conversation until interrupted (never returns)."""

    def __init__(self):
        self._subagent_id = "sa-0-p0c"
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
        self.tools = [{"name": "web_search", "description": "search"}]
        self.ephemeral_system_prompt = "sys"
        self._api_call_count = 0
        self._hang = threading.Event()

    def get_activity_summary(self):
        return {
            "api_call_count": self._api_call_count,
            "max_iterations": self.max_iterations,
            "current_tool": None,
            "seconds_since_activity": 1,
        }

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        # Simulate a healthy child making progress but not yet done.
        self._hang.wait(5.0)
        return {"final_response": "partial work done", "completed": False, "api_calls": 0}

    def interrupt(self):
        self._hang.set()


def test_configured_timeout_detaches_with_durable_reference(youtab_home, monkeypatch):
    """CORRECTED (was the C-2.1c defect characterization): a configured
    child_timeout is a PARENT WAIT budget. When it elapses on an admitted child
    that is still working (here 0 API calls yet), the child is DETACHED with a
    durable task reference and recovery action — NOT killed, NOT a bare
    summary=None with no recoverable state. git history preserves the baseline."""
    monkeypatch.setattr(dt, "_get_child_timeout", lambda: 0.2)

    child = _HangingChild()
    result = dt._run_single_child(0, "long enterprise task", child=child, parent_agent=None)

    # Detached, not killed; a durable task reference + recovery action are returned.
    assert result["status"] == "running"
    assert result["detached"] is True
    assert result["task_id"]
    assert result["recovery_action"] and "RunStore" in result["recovery_action"]
    assert result["exit_reason"] == "wait_budget_detached"
    # summary is None but ALWAYS paired with a durable reference (never bare).
    assert result["summary"] is None and result["task_id"]
    # The child was NOT interrupted by the parent wait timeout.
    assert getattr(child, "_hang").is_set() is False
    child.interrupt()  # cleanup only
