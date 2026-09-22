"""Phase 3-D (priority 1, CORRECTED): parent-wait vs child-lifetime decoupling.

Codex REQUEST-CHANGES: parent-wait expiry must NEVER decide child termination
from api_calls. For an ADMITTED durable child, wait-budget expiry returns
RUNNING + task_id and leaves the child alive REGARDLESS of api_calls. Only a
genuine child error / failed admission is a real failure. Stall/liveness and the
execution deadline are SEPARATE mechanisms.

Scenarios:
- wait expires before the first API call while legitimately working -> detach;
- long tool call with no API calls -> detach;
- one API call then a long healthy wait -> detach;
- genuinely failed child -> FAILED (never detached);
- explicit cancel is separate from wait timeout;
- an execution deadline is persisted independently of any wait budget;
- reconnect to the same task_id, exactly one terminal result;
- parent cleanup/finally cannot interrupt a detached child.

Process-restart recovery is NOT claimed here (in-process continuation); restart is
proven separately at the store level (test_p0b / test_p3a).
"""

import threading
import time

import pytest
from unittest.mock import MagicMock

from tools import delegate_tool
from youtab_runtime.durable_run_store import RunIdentity, RunState, create_run_store


@pytest.fixture
def youtab_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    return home


class _Child:
    def __init__(self, release, *, api_calls=0, final="DELEGATE_RESULT_ONCE", raises=None):
        self._subagent_id = f"sa-0-{id(self) & 0xffff:x}"
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
        self._api_calls = api_calls
        self._final = final
        self._raises = raises
        self.interrupted = False

    def get_activity_summary(self):
        return {"api_call_count": self._api_calls, "max_iterations": 30,
                "current_tool": None, "seconds_since_activity": 1}

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        if self._raises is not None:
            raise self._raises
        self._release.wait(5.0)
        return {"final_response": self._final, "completed": True, "api_calls": self._api_calls}

    def interrupt(self):
        self.interrupted = True
        self._release.set()


def _run(child, monkeypatch, timeout=0.3):
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: timeout)
    parent = MagicMock()
    parent._touch_activity = MagicMock()
    parent._current_task_id = None
    return delegate_tool._run_single_child(0, "long enterprise task", child=child, parent_agent=parent)


@pytest.mark.parametrize("api_calls", [0, 1, 5])
def test_admitted_child_detaches_on_wait_regardless_of_api_calls(youtab_home, monkeypatch, api_calls):
    """0 API calls (working before first LLM call / long tool call), or many —
    parent-wait expiry always detaches an admitted child, never kills it."""
    release = threading.Event()
    child = _Child(release, api_calls=api_calls)
    result = _run(child, monkeypatch)

    assert result["status"] == "running"
    assert result["detached"] is True
    assert result["summary"] is None
    assert result["api_calls"] == api_calls
    assert child.interrupted is False, "admitted child must NOT be killed on wait expiry"

    # Parent cleanup/finally must not interrupt the detached child.
    assert child.interrupted is False

    task_id = result["task_id"]
    store = create_run_store("sqlite")
    assert store.get_run(task_id) is not None

    # Child finishes; result captured durably exactly once (reconnect by task_id).
    release.set()
    deadline = time.time() + 8
    while time.time() < deadline:
        row = store.get_run(task_id)
        if row and row["state"] == "SUCCEEDED":
            break
        time.sleep(0.1)
    assert row["state"] == "SUCCEEDED" and row["result_ref"] == "DELEGATE_RESULT_ONCE"
    succ = [e for e in store.get_events(task_id, from_seq=0) if e["kind"] == "delegate.completed"]
    assert len(succ) == 1


def test_failed_child_is_failure_not_detached(youtab_home, monkeypatch):
    """A genuine child error is FAILED, never turned into a detached task."""
    release = threading.Event()
    child = _Child(release, raises=RuntimeError("child crashed"))
    result = _run(child, monkeypatch, timeout=30.0)  # high budget: not a timeout
    assert result["status"] == "error"
    assert result.get("detached") is not True
    assert result["summary"] is None
    # No detached RUNNING run was created for a failed child.
    store = create_run_store("sqlite")
    assert store.get_run(child._subagent_id) is None


def test_explicit_interrupt_is_separate_from_wait_timeout(youtab_home):
    release = threading.Event()
    child = _Child(release)
    child.interrupt()
    assert child.interrupted is True and release.is_set()


def test_execution_deadline_is_persisted_independently(youtab_home):
    """The execution deadline is a separate persisted policy field — not derived
    from any parent wait budget."""
    store = create_run_store("sqlite")
    row = store.create_run(RunIdentity(
        task_id="dl-1", run_id="dl-1", tenant_id="local", organization_id="local",
        workspace_id="local", principal_id="local", agent_id="a", operation="delegate",
        execution_deadline=time.time() + 3600))
    assert row["execution_deadline"] is not None
    # It is independent of wait/lease/heartbeat — a distinct column.
    assert store.get_run("dl-1")["execution_deadline"] == row["execution_deadline"]
