"""Behavior tests for managed-run progress steps.

A managed worker records a short, controlled ``runtime_step`` event as each tool
starts, so a UI shows what the agent is doing. These tests pin:

* the tool -> human phrase mapping and the unknown-tool fallback;
* the optional safe-noun detail (search query / file basename);
* secret scrubbing and length bounding of the line;
* fail-closed privacy: a credential in an argument never reaches the step;
* the managed-run gate (no ``YOUTAB_AGENT_KANBAN_TASK`` -> no event);
* that an emitted event round-trips as a PLAIN STRING payload, so it survives
  the gateway's string-only projection, and is fail-open on any error;
* that the step is emitted only AFTER a tool is authorized, so the durable feed
  never claims a rejected tool ran.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.conversation_loop import (
    _runtime_step_phrase,
    _scrub_step_text,
    emit_runtime_step,
)
from youtab_agent_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Pure phrase / scrub logic
# ---------------------------------------------------------------------------
def test_known_tool_maps_to_phrase_with_query_detail():
    assert (
        _runtime_step_phrase("web_search", '{"query": "best coffee"}')
        == "Searching the web: best coffee"
    )


def test_unknown_tool_falls_back_to_running_phrase():
    assert _runtime_step_phrase("frobnicate_widget", "") == "Running frobnicate widget"


def test_browser_prefix_falls_back_to_generic_browser_phrase():
    assert _runtime_step_phrase("browser_scroll", "") == "Using the browser"


def test_file_tool_shows_basename_only_not_full_path():
    s = _runtime_step_phrase(
        "read_file", '{"path": "/opt/data/private/secret/report.pdf"}'
    )
    assert s == "Reading a file: report.pdf"
    assert "/opt/data" not in s  # the directory path is never surfaced


def test_missing_tool_name_yields_no_line():
    assert _runtime_step_phrase("", '{"query": "x"}') == ""
    assert _runtime_step_phrase(None, None) == ""


def test_malformed_args_never_raise_and_yield_no_detail():
    # Not JSON, and a non-dict JSON value -- both degrade to just the phrase.
    assert _runtime_step_phrase("web_search", "not json at all") == "Searching the web"
    assert _runtime_step_phrase("web_search", "[1,2,3]") == "Searching the web"


def test_dict_args_are_accepted_as_well_as_json_text():
    """The executor hands over the already-parsed argument dict, not raw JSON."""
    assert (
        _runtime_step_phrase("web_search", {"query": "best coffee"})
        == "Searching the web: best coffee"
    )


def test_line_is_length_bounded():
    s = _runtime_step_phrase("web_search", '{"query": "' + "a" * 500 + '"}')
    assert len(s) <= 121  # 120 chars + the single ellipsis


# ---------------------------------------------------------------------------
# Privacy: credentials are scrubbed before anything is persisted
# ---------------------------------------------------------------------------
def test_secret_in_query_is_scrubbed_from_the_line():
    s = _runtime_step_phrase(
        "web_search", '{"query": "login token=sk-ABCD1234EFGH5678IJKL please"}'
    )
    assert "sk-ABCD1234EFGH5678IJKL" not in s
    # The governed redactor masks head/tail (``sk-ABC...IJKL``) rather than
    # emitting a fixed token, so assert the credential is gone, not the mask.
    assert "ABCD1234EFGH" not in s


def test_url_credentials_and_jwt_are_scrubbed():
    assert "hunter2" not in _scrub_step_text(
        "https://user:hunter2@example.com/x", max_chars=120
    )
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36"
    )
    assert jwt not in _scrub_step_text("session " + jwt, max_chars=200)


def test_scheme_delimited_credentials_are_redacted():
    """An auth scheme separates the key from the credential by a SPACE, so a
    pattern whose value part stops at whitespace leaves the credential in the
    clear unless the scheme is consumed too."""
    secret = "opaquecredential012345"
    for text in (
        f"Authorization: Bearer {secret}",
        f"authorization={secret}",
        f"Bearer {secret}",
        f"Authorization: Basic {secret}",
    ):
        assert secret not in _scrub_step_text(text, max_chars=200), text

    # And it must not swallow ordinary prose that merely contains the word.
    assert "bad news" in _scrub_step_text("the bearer of bad news", max_chars=200)


def test_cookie_and_session_credentials_are_redacted():
    """A Cookie header is credential material wholesale -- it may carry several
    pairs -- so the whole value goes, and framework session names are redacted
    at any value length."""
    secret = "opaquecredential012345"
    for text in (
        f"Cookie: sessionid={secret}",
        f"Set-Cookie: session={secret}; Path=/",
        f"Cookie: a=1; sessionid={secret}; b=2",
        f"sessionid={secret}",
        f"PHPSESSID={secret}",
        f"JSESSIONID={secret}",
        f"ASP.NET_SessionId={secret}",
        "session_id=abc123",
        "xsrf=abc123",
        "cookie: csrftoken=abc123def456ghi789",
    ):
        out = _scrub_step_text(text, max_chars=250)
        assert secret not in out, text
        assert "abc123" not in out, text

    assert "started" in _scrub_step_text("session: started", max_chars=250)
    assert "sessions" in _scrub_step_text("3 sessions open", max_chars=250)


def test_credential_name_tails_are_redacted():
    """The keyword need not sit immediately before the separator:
    ``AWS_SECRET_ACCESS_KEY=`` puts ``_ACCESS_KEY`` between ``secret`` and
    ``=``, which a pattern anchored on the separator misses entirely."""
    for text, secret in (
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCY", "wJalrXUtnFEMI"),
        ("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxR", "wJalrXUtnFEMI"),
        ("MY_API_SECRET_KEY=supersecretvalue123456", "supersecretvalue"),
        ("AWS_SESSION_TOKEN=FwoGZXIvYXdzEBYaDHh4", "FwoGZXIvYXdz"),
        ("GITHUB_TOKEN=AAAABBBBCCCCDDDDEEEE1111", "AAAABBBBCCCC"),
    ):
        assert secret not in _scrub_step_text(text, max_chars=250), text


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
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_RUN_ID", raising=False)
    kb.init_db()
    return home


def _new_task() -> str:
    # create_task manages its own write transaction.
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="progress run", assignee="ops")
    return t if isinstance(t, str) else getattr(t, "id", str(t))


def _claim(task_id: str) -> int:
    """Take the task ready -> running and return its attempt id.

    emit_runtime_step fences on the live attempt the way heartbeat_worker does,
    so a step only lands while the task is running under that run id.
    """
    with kb.connect_closing() as conn:
        kb.claim_task(conn, task_id)
        return kb.get_task(conn, task_id).current_run_id


def _runtime_steps(task_id: str) -> list:
    with kb.connect_closing() as conn:
        return [e for e in kb.list_events(conn, task_id) if e.kind == "runtime_step"]


def test_no_event_when_not_a_managed_run(kanban_home):
    task_id = _new_task()
    emit_runtime_step("web_search", '{"query":"x"}')
    assert _runtime_steps(task_id) == []


def test_managed_run_emits_plain_string_step(kanban_home, monkeypatch):
    task_id = _new_task()
    run_id = _claim(task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(run_id))
    emit_runtime_step("web_search", '{"query": "best coffee"}')
    steps = [e.payload for e in _runtime_steps(task_id)]
    assert steps == ["Searching the web: best coffee"]
    # It must be a PLAIN STRING (survives the gateway's string-only projection),
    # not a dict (which the gateway would blank).
    assert isinstance(steps[0], str)


def test_emit_is_fail_open_on_bad_input(kanban_home, monkeypatch):
    task_id = _new_task()
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(_claim(task_id)))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)

    class _Boom:
        def __str__(self):
            raise RuntimeError("boom")

    # Must not raise, and must not emit a bogus event.
    emit_runtime_step(_Boom(), {"query": "x"})
    assert _runtime_steps(task_id) == []


def test_step_event_is_attributed_to_the_worker_attempt(kanban_home, monkeypatch):
    """Steps carry the attempt id the dispatcher gave the worker, like the
    heartbeat/terminal events, so they stay grouped after a reclaim or retry."""
    task_id = _new_task()
    run_id = _claim(task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(run_id))

    emit_runtime_step("web_search", '{"query": "x"}')

    events = _runtime_steps(task_id)
    assert len(events) == 1
    assert events[0].run_id == run_id


def test_step_event_without_an_attempt_id_still_records(kanban_home, monkeypatch):
    """A missing or malformed attempt id must not lose the step (fail-open)."""
    task_id = _new_task()
    _claim(task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", "not-an-int")

    emit_runtime_step("web_search", '{"query": "x"}')

    events = _runtime_steps(task_id)
    assert len(events) == 1
    assert events[0].run_id is None


# ---------------------------------------------------------------------------
# Ordering: a step is recorded only once the tool is authorized
# ---------------------------------------------------------------------------
class _Decision:
    def __init__(self, allows: bool):
        self.allows_execution = allows
        self.message = "blocked by policy"


def _middleware_agent(allows: bool):
    return SimpleNamespace(
        _tool_guardrails=SimpleNamespace(
            before_call=lambda *_a, **_k: _Decision(allows)
        ),
        _guardrail_block_result=lambda _d: '{"error": "blocked by policy"}',
        session_id="s1",
        _current_turn_id="t1",
        _current_api_request_id="r1",
        _turns_since_memory=0,
        _iters_since_skill=0,
    )


def _drive_middleware(monkeypatch, *, allows: bool):
    """Run the real executor middleware, recording progress emissions."""
    from agent import relay_tools, tool_executor

    emitted: list = []
    monkeypatch.setattr(
        "youtab_agent_cli.middleware.apply_tool_request_middleware",
        lambda _name, args, **_kw: SimpleNamespace(payload=args, trace=[]),
    )
    monkeypatch.setattr(
        "youtab_agent_cli.middleware.run_tool_execution_middleware",
        lambda _name, args, callback, **_kw: callback(args),
    )
    monkeypatch.setattr(
        "youtab_agent_cli.plugins.resolve_pre_tool_block",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(
        tool_executor, "_emit_terminal_post_tool_call", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        relay_tools, "execute", lambda _n, args, cb, **_k: (cb(args), args)
    )
    monkeypatch.setattr(
        tool_executor,
        "_emit_managed_progress",
        lambda name, args: emitted.append((name, args)),
    )

    ran: list = []
    outcome = tool_executor._run_agent_tool_execution_middleware(
        _middleware_agent(allows),
        function_name="web_search",
        function_args={"query": "best coffee"},
        effective_task_id="task-1",
        tool_call_id="call-1",
        execute=lambda args: ran.append(args) or "ok",
    )
    return emitted, ran, outcome


def test_no_step_is_recorded_when_a_guardrail_blocks_the_tool(monkeypatch):
    """Emitting before dispatch recorded work that policy then rejected, and
    pushed model-proposed arguments into the durable feed unvalidated."""
    emitted, ran, outcome = _drive_middleware(monkeypatch, allows=False)
    assert ran == []  # the tool really did not run
    assert emitted == []  # and no step claimed that it did
    assert outcome.blocked is True


def test_step_is_recorded_once_the_tool_is_authorized(monkeypatch):
    emitted, ran, outcome = _drive_middleware(monkeypatch, allows=True)
    assert ran == [{"query": "best coffee"}]
    assert emitted == [("web_search", {"query": "best coffee"})]
    assert outcome.blocked is False


def test_google_api_key_in_a_query_is_redacted(kanban_home, monkeypatch):
    """A prefix allowlist maintained in this module kept missing the next shape
    (Bearer, cookies, AWS_SECRET_ACCESS_KEY, then AIza). The line now goes
    through the governed redactor, which owns that universe."""
    task_id = _new_task()
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(_claim(task_id)))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    key = "AIzaSyA12345678901234567890123456789012345"

    emit_runtime_step("web_search", '{"query": "maps %s please"}' % key)

    payloads = [e.payload for e in _runtime_steps(task_id)]
    assert payloads, "the step should still be recorded"
    assert key not in payloads[0]
    assert "AIzaSyA1234" not in payloads[0]


def test_step_is_dropped_for_a_superseded_attempt(kanban_home, monkeypatch):
    """A reclaimed or un-killable worker must not keep appending steps to an
    attempt that is no longer current -- the dashboard broadcasts every insert.
    Mirrors heartbeat_worker(..., expected_run_id=...)."""
    task_id = _new_task()
    run_id = _claim(task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(run_id + 1))

    emit_runtime_step("web_search", '{"query": "x"}')

    assert _runtime_steps(task_id) == []


def test_step_is_dropped_once_the_task_stops_running(kanban_home, monkeypatch):
    task_id = _new_task()
    run_id = _claim(task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(run_id))
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id = ?", (task_id,))

    emit_runtime_step("web_search", '{"query": "x"}')

    assert _runtime_steps(task_id) == []


def test_no_step_from_a_delegated_child_context(kanban_home, monkeypatch):
    """A delegate_task child runs in the same process and inherits the parent's
    task and run id. write_txn would have refused the write via
    _assert_not_delegated_child_mutation; an autocommit append bypasses that, so
    the boundary is checked explicitly. Otherwise every child tool shows up as
    parent work in the parent's durable feed."""
    from agent import delegation_context

    task_id = _new_task()
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(_claim(task_id)))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)

    monkeypatch.setattr(
        delegation_context, "is_delegated_child_process_context", lambda: True
    )
    emit_runtime_step("web_search", '{"query": "child work"}')
    assert _runtime_steps(task_id) == []

    # Non-vacuity: the same call records once the context is not a child.
    monkeypatch.setattr(
        delegation_context, "is_delegated_child_process_context", lambda: False
    )
    emit_runtime_step("web_search", '{"query": "parent work"}')
    assert len(_runtime_steps(task_id)) == 1
