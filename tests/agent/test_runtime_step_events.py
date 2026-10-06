"""Behavior tests for managed-run progress steps (agent.conversation_loop).

A managed worker emits a short, controlled ``runtime_step`` event before each
batch of tool calls so a UI shows what the agent is doing. These tests pin:

* the tool -> human phrase mapping and unknown-tool fallback;
* the optional safe-noun detail (search query / file basename);
* secret scrubbing and length bounding of the summary;
* fail-closed privacy: a secret in an argument never reaches the summary;
* the managed-run gate (no ``YOUTAB_AGENT_KANBAN_TASK`` -> no event);
* that an emitted event round-trips as a PLAIN STRING payload (so it survives
  the gateway's string-only projection), fail-open on any error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.conversation_loop import (
    _emit_runtime_step,
    _runtime_step_summary,
    _scrub_step_text,
)
from youtab_agent_cli import kanban_db as kb


# --- a minimal stand-in for an OpenAI-style tool call ------------------------
class _FakeFn:
    def __init__(self, name: str, arguments: str = ""):
        self.name = name
        self.arguments = arguments


class _FakeTC:
    def __init__(self, name: str, arguments: str = ""):
        self.function = _FakeFn(name, arguments)


# ---------------------------------------------------------------------------
# Pure summary / scrub logic
# ---------------------------------------------------------------------------
def test_known_tool_maps_to_phrase_with_query_detail():
    s = _runtime_step_summary([_FakeTC("web_search", '{"query": "best coffee"}')])
    assert s == "Searching the web: best coffee"


def test_unknown_tool_falls_back_to_running_phrase():
    assert (
        _runtime_step_summary([_FakeTC("frobnicate_widget")])
        == "Running frobnicate widget"
    )


def test_browser_prefix_falls_back_to_generic_browser_phrase():
    assert _runtime_step_summary([_FakeTC("browser_scroll")]) == "Using the browser"


def test_file_tool_shows_basename_only_not_full_path():
    s = _runtime_step_summary([
        _FakeTC("read_file", '{"path": "/opt/data/private/secret/report.pdf"}')
    ])
    assert s == "Reading a file: report.pdf"
    assert "/opt/data" not in s  # the directory path is never surfaced


def test_multiple_calls_are_summarised_with_a_count():
    s = _runtime_step_summary([
        _FakeTC("web_search", '{"query":"x"}'),
        _FakeTC("read_file"),
    ])
    assert s.endswith("(+1 more)")


def test_empty_batch_yields_empty_summary():
    assert _runtime_step_summary([]) == ""
    assert _runtime_step_summary(None) == ""


def test_malformed_args_never_raise_and_yield_no_detail():
    # Not JSON, and a non-dict JSON value — both degrade to just the phrase.
    assert (
        _runtime_step_summary([_FakeTC("web_search", "not json at all")])
        == "Searching the web"
    )
    assert (
        _runtime_step_summary([_FakeTC("web_search", "[1,2,3]")]) == "Searching the web"
    )


# ---------------------------------------------------------------------------
# Privacy: secrets are scrubbed, length is bounded
# ---------------------------------------------------------------------------
def test_secret_in_query_is_scrubbed_from_summary():
    s = _runtime_step_summary([
        _FakeTC("web_search", '{"query": "login token=sk-ABCD1234EFGH5678IJKL please"}')
    ])
    assert "sk-ABCD1234EFGH5678IJKL" not in s
    assert "[redacted]" in s


def test_url_credentials_and_jwt_are_scrubbed():
    assert "hunter2" not in _scrub_step_text(
        "https://user:hunter2@example.com/x", max_chars=120
    )
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36"
    assert jwt not in _scrub_step_text("session " + jwt, max_chars=200)


def test_summary_is_length_bounded():
    s = _runtime_step_summary([_FakeTC("web_search", '{"query": "' + "a" * 500 + '"}')])
    assert len(s) <= 121  # 120 chars + the single ellipsis


# ---------------------------------------------------------------------------
# Emission: managed-run gate, plain-string round-trip, fail-open
# ---------------------------------------------------------------------------
@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _new_task() -> str:
    # create_task manages its own write transaction.
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="progress run", assignee="ops")
    return t if isinstance(t, str) else getattr(t, "id", str(t))


def _runtime_steps(task_id: str) -> list[str]:
    with kb.connect_closing() as conn:
        events = kb.list_events(conn, task_id)
    return [e.payload for e in events if e.kind == "runtime_step"]


def test_no_event_when_not_a_managed_run(kanban_home):
    task_id = _new_task()
    # No YOUTAB_AGENT_KANBAN_TASK set -> the emit is a no-op.
    _emit_runtime_step([_FakeTC("web_search", '{"query":"x"}')])
    assert _runtime_steps(task_id) == []


def test_managed_run_emits_plain_string_step(kanban_home, monkeypatch):
    task_id = _new_task()
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    _emit_runtime_step([_FakeTC("web_search", '{"query": "best coffee"}')])
    steps = _runtime_steps(task_id)
    assert steps == ["Searching the web: best coffee"]
    # It must be a PLAIN STRING (survives the gateway's string-only projection),
    # not a dict (which the gateway would blank).
    assert isinstance(steps[0], str)


def test_emit_is_fail_open_on_bad_input(kanban_home, monkeypatch):
    task_id = _new_task()
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)

    class _Boom:
        @property
        def function(self):
            raise RuntimeError("boom")

    # Must not raise, and must not emit a bogus event.
    _emit_runtime_step([_Boom()])
    assert _runtime_steps(task_id) == []
