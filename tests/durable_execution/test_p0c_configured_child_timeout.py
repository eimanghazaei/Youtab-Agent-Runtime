"""P0-C reproduction — configured child timeout loses partial work.

Durable-execution requirement: when a child hits a configured timeout, the parent
must receive the latest checkpoint / last progress / changed files / child id and
a recovery action — not merely "timed out" with no result.

This reproduction proves the CURRENT behavior on origin/main (0.19.1): with a
small injected child timeout, `_run_single_child` returns status="timeout",
`summary=None`, and NO recoverable execution state (only a diagnostic log path).
Uses an injected tiny timeout — does not wait 600 real seconds.
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


def test_configured_timeout_returns_summary_none_and_no_recoverable_state(
    youtab_home, monkeypatch
):
    # Inject a tiny configured timeout (bypasses the 30s floor deterministically).
    monkeypatch.setattr(dt, "_get_child_timeout", lambda: 0.2)

    child = _HangingChild()
    try:
        result = dt._run_single_child(0, "long enterprise task", child=child, parent_agent=None)
    finally:
        child.interrupt()

    # DEFECT REPRODUCED: timeout yields no summary.
    assert result["status"] == "timeout"
    assert result["summary"] is None
    assert result["timeout_seconds"] == 0.2
    assert result["exit_reason"] == "timeout"

    # The partial progress the child had ("partial work done") is discarded:
    # it appears nowhere in the returned result.
    assert "partial work done" not in str(result)

    # No recoverable execution state is returned — only a diagnostic log path.
    for missing in (
        "checkpoint",
        "last_progress",
        "changed_files",
        "artifacts",
        "child_id",
        "resume_token",
        "recovery_action",
    ):
        assert missing not in result, (
            f"unexpected recoverable field {missing!r} — if this now exists the "
            f"defect is being fixed; invert this assertion"
        )

    # What IS returned is a diagnostic log, not resumable state.
    assert result["timeout_phase"] == "before_first_llm_call"
    assert "diagnostic_path" in result
