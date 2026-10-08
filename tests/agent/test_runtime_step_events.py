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

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# Imported as a MODULE as well as by name, deliberately: `_drive_middleware`
# monkeypatches `emit_runtime_step` on the module object, which cannot be done
# through the bound name. Both forms of the same module in one file is
# `py/import-and-import-from`, so the names come off the module here rather
# than via a second `from` import.
import agent.conversation_loop as conversation_loop

_runtime_step_phrase = conversation_loop._runtime_step_phrase
_scrub_step_text = conversation_loop._scrub_step_text
emit_runtime_step = conversation_loop.emit_runtime_step
from youtab_agent_cli import kanban_db as kb

# Envelopes used by the ordering tests below. Named rather than inlined so
# the marker that distinguishes a refusal from a failure is readable.
QUERY_ARGS = '{"query": "x"}'
REFUSAL_READ_BLOCK = (
    '{"error": "Blocked: Youtab internal path", "authorization": "denied"}'
)
REFUSAL_EDIT_APPROVAL = (
    '{"error": "Edit approval denied by ACP client; file was not modified.", '
    '"authorization": "denied"}'
)
REFUSAL_REORDERED = (
    '{"authorization": "denied", "success": false, "error": "nope"}'
)
FAILURE_PROVIDER_TIMEOUT = '{"error": "search provider timed out after 30s"}'
FAILURE_NOT_FOUND = '{"error": "file not found"}'
FAILURE_MENTIONS_AUTH = (
    '{"error": "authorization header missing from the upstream response"}'
)


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


@pytest.mark.parametrize("raw", ["not-an-int", "", "  ", "12.5", "1e3"])
def test_a_step_without_a_valid_attempt_id_is_dropped(kanban_home, monkeypatch, raw):
    """No valid attempt id means no row -- the opposite of the old fail-open.

    This used to record the step with ``run_id=None`` so that a malformed id
    could not lose it. That fallback defeated the fence it sits beside: with
    ``current_run_id`` out of the predicate, a reclaimed worker whose attempt
    has been superseded keeps appending unattributed steps to a task that is
    running again under a NEW attempt, and the dashboard broadcasts every one,
    so a live attempt's feed interleaves with a dead one's.

    Nothing legitimate reaches this path. The dispatcher always exports
    ``YOUTAB_AGENT_KANBAN_RUN_ID = str(task.current_run_id)`` when it spawns a
    worker, so a missing or non-numeric value in a process that DOES carry
    ``YOUTAB_AGENT_KANBAN_TASK`` is an anomaly -- and losing one cosmetic row
    during an anomaly is the cheap side of that trade.
    """
    task_id = _new_task()
    _claim(task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", raw)

    emit_runtime_step("web_search", QUERY_ARGS)

    assert _runtime_steps(task_id) == []


def test_a_cold_database_drops_the_step_instead_of_paying_for_init(
    kanban_home, monkeypatch
):
    """A cosmetic row must never pay cold initialization.

    ``connect()`` skips the cross-process init lock -- bounded at
    ``_INIT_LOCK_TIMEOUT_SECONDS``, 10 seconds -- plus header validation, the
    integrity probe and additive migrations only once this process has
    initialized the path. A ``busy_timeout_ms`` does not bound any of that; it
    limits SQLite's own lock waits, which come afterwards. So in a freshly
    spawned worker the first progress row would sit in front of the tool
    result on its way to the model and spend run budget on a row nothing
    depends on.

    The check lives inside ``connect()``, on the resolution it already
    performs. A separate probe recomputed it -- ``kanban_db_path()`` plus
    ``path.resolve()`` on the same config-derived value -- and that second
    sink made the python CodeQL leg report two new ``py/path-injection``
    findings at severity 7.5. One resolution, one sink.

    Simulated by emptying the per-process cache, which is exactly the state a
    new worker starts in.
    """
    task_id = _new_task()
    run_id = _claim(task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setattr(kb, "_INITIALIZED_PATHS", set())

    emit_runtime_step("web_search", QUERY_ARGS)

    monkeypatch.undo()
    assert _runtime_steps(task_id) == []


def test_only_if_initialized_refuses_before_any_lock_work(kanban_home, monkeypatch):
    """The contract the emitter relies on, asserted directly.

    ``emit_runtime_step`` is fail-open and swallows everything, so it cannot
    distinguish "dropped because cold" from "dropped because of a bug". This
    pins the mechanism: a cold path raises ``ColdDatabase``, and it raises
    BEFORE opening a connection -- reaching the connection at all is what
    costs the time.
    """
    opened: list = []
    monkeypatch.setattr(kb, "_sqlite_connect",
                        lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(
                            AssertionError("should not have opened a connection")))
    monkeypatch.setattr(kb, "_INITIALIZED_PATHS", set())

    with pytest.raises(kb.ColdDatabase):
        kb.connect(only_if_initialized=True)

    assert opened == []

    # And a warm path still connects normally -- the flag must not break the
    # ordinary case.
    monkeypatch.undo()
    with kb.connect_closing(only_if_initialized=True) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


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


def _drive_middleware(monkeypatch, *, allows: bool, tool_result: object = "ok"):
    """Run the real executor middleware, recording progress emissions.

    ``emit_runtime_step`` is what gets stubbed, NOT
    ``_emit_managed_progress`` -- the refusal gate lives in the latter, so
    patching it would stub out the behaviour under test. ``tool_result`` is
    what the tool handler returns, which is how a tool-internal refusal is
    signalled.
    """
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
        conversation_loop, "emit_runtime_step",
        lambda name, args: emitted.append((name, args)),
    )

    ran: list = []
    outcome = tool_executor._run_agent_tool_execution_middleware(
        _middleware_agent(allows),
        function_name="web_search",
        function_args={"query": "best coffee"},
        effective_task_id="task-1",
        tool_call_id="call-1",
        execute=lambda args: ran.append(args) or tool_result,
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


@pytest.mark.parametrize("refusal", [
    # What read_file's internal-path / credential denylist now returns.
    REFUSAL_READ_BLOCK,
    # What acp_adapter.edit_approval returns for write_file / patch.
    REFUSAL_EDIT_APPROVAL,
    # A handler that returns the object rather than a JSON string.
    {"error": "nope", "authorization": "denied"},
    # Key order must not matter: the marker is found by parsing, not by a
    # prefix test over the raw text.
    REFUSAL_REORDERED,
])
def test_no_step_when_the_tool_itself_refuses(monkeypatch, refusal):
    """Middleware clearance is not authorization -- the handler checks too.

    Policy, scope and guardrails all pass here, and the tool is reached. It
    then refuses on its own account: `read_file` applies the Youtab
    internal-path and credential-store denylist, and `write_file`/`patch` can
    be refused by ACP edit approval. Emitting before `execute` recorded
    "Reading auth.json" -- the basename of a file the agent was never allowed
    to open -- and asserted that a refused tool was running, durably, to every
    dashboard subscriber.
    """
    emitted, ran, outcome = _drive_middleware(
        monkeypatch, allows=True, tool_result=refusal
    )
    assert ran == [{"query": "best coffee"}]   # the handler was reached
    assert emitted == []                        # and recorded nothing
    assert outcome.blocked is False             # not a middleware block


@pytest.mark.parametrize("failure", [
    # web_search_tool returns this shape when its provider raises: the call
    # was authorized, it ran, and then it failed.
    FAILURE_PROVIDER_TIMEOUT,
    FAILURE_NOT_FOUND,
    {"error": "connection reset"},
    # An error that merely mentions the word is not a refusal.
    FAILURE_MENTIONS_AUTH,
])
def test_an_ordinary_failure_after_execution_still_records_a_step(
    monkeypatch, failure
):
    """The feed must not go blank exactly when something goes wrong.

    The first version of this check treated every ``tool_error(...)`` envelope
    as an authorization refusal. Handlers use that same envelope for ordinary
    post-execution failures, so an authorized search that really ran and then
    timed out produced no progress row at all -- the feed went blank during
    precisely the runs worth reading. Refusal is now carried by the refusing
    site (``tools.registry.TOOL_AUTHORIZATION_DENIED``) rather than inferred
    from the envelope.
    """
    emitted, ran, outcome = _drive_middleware(
        monkeypatch, allows=True, tool_result=failure
    )
    assert ran == [{"query": "best coffee"}]
    assert emitted == [("web_search", {"query": "best coffee"})], failure
    assert outcome.blocked is False


def test_the_terminal_approval_gate_marks_its_refusals(monkeypatch) -> None:
    """The dangerous-command gate refuses by RETURNING, so it must say so.

    ``terminal_tool`` runs ``_check_all_guards`` itself; a denial never reaches
    the executor middleware. Both of its outcomes mean the command did not
    run -- ``blocked`` outright, and ``pending_approval`` in gateway "ask"
    mode where it has not run YET -- so a progress row saying "Running a
    command" is false in both cases.

    Driven through the real ``terminal_tool`` entry point with the guard
    stubbed, rather than by asserting on the source, so a future rewrite of
    the payload keeps being checked.
    """
    import json as _json

    from agent import tool_executor
    from tools import terminal_tool as tt

    for approval in (
        {"approved": False, "status": "blocked",
         "message": "Command denied: rm -rf /. Use the approval prompt.",
         "description": "destructive"},
        {"approved": False, "status": "pending_approval",
         "command": "rm -rf /", "description": "destructive",
         "pattern_key": "rm-rf", "allow_permanent": True},
    ):
        monkeypatch.setattr(tt, "_check_all_guards", lambda *a, **k: approval)
        raw = tt.terminal_tool(command="rm -rf /")
        payload = _json.loads(raw)

        assert payload.get("authorization") == "denied", (
            f"the {approval['status']} payload is not marked as an authorization "
            f"refusal, so the progress feed records it as work: {payload}"
        )
        assert tool_executor._is_authorization_refusal(raw), (
            f"the executor does not recognise the {approval['status']} payload"
        )


def test_the_browser_private_page_guards_mark_their_refusals(monkeypatch) -> None:
    """Camofox and raw-CDP private-address guards refuse by returning too.

    Same shape as the terminal gate: these run inside the handler, below the
    executor middleware, and the action never happens. A row saying
    "Browsing the web" or "Using the browser" is false.
    """
    from agent import tool_executor
    from tools import browser_camofox as bc
    from tools import browser_cdp_tool as cdp
    from tools import browser_tool

    # The guard defers both imports to call time (browser_tool imports this
    # module, so a module-scope import would be circular), which is why these
    # are patched on browser_tool rather than on browser_camofox.
    monkeypatch.setattr(browser_tool, "_eval_ssrf_guard_active", lambda *a, **k: True)
    monkeypatch.setattr(
        browser_tool, "_camofox_current_page_private_url",
        lambda tab_id, user_id: "http://169.254.169.254/",
    )
    refusal = bc._camofox_private_page_block(
        {"tab_id": "t1", "user_id": "u1"}, "task-1", "click"
    )
    assert refusal is not None, "the guard did not fire on a private address"
    assert tool_executor._is_authorization_refusal(refusal), refusal

    refusal = cdp._private_page_guard_error("http://169.254.169.254/", "Runtime.evaluate")
    assert tool_executor._is_authorization_refusal(refusal), refusal


def test_a_raised_failure_still_records_a_step(monkeypatch) -> None:
    """An authorized tool that THREW still ran.

    The executors around this one catch exceptions and turn them into
    ordinary tool-error results, so skipping the emitter on a raise loses
    exactly the failing steps -- the same hole as treating every error
    envelope as a refusal, reached through the exception path instead of the
    return path. The exception itself must still propagate unchanged.
    """
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
    monkeypatch.setattr("youtab_agent_cli.plugins.resolve_pre_tool_block",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_executor, "_emit_terminal_post_tool_call",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(relay_tools, "execute",
                        lambda _n, args, cb, **_k: (cb(args), args))
    monkeypatch.setattr(conversation_loop, "emit_runtime_step",
                        lambda name, args: emitted.append((name, args)))

    def boom(_args):
        raise RuntimeError("provider exploded mid-call")

    with pytest.raises(RuntimeError, match="provider exploded mid-call"):
        tool_executor._run_agent_tool_execution_middleware(
            _middleware_agent(True),
            function_name="web_search",
            function_args={"query": "best coffee"},
            effective_task_id="task-1",
            tool_call_id="call-1",
            execute=boom,
        )

    assert emitted == [("web_search", {"query": "best coffee"})]


def test_the_sensitive_path_write_guards_mark_their_refusals() -> None:
    """`write_file` and `patch` refusals DO disclose, unlike terminal/browser.

    I previously argued these rows were harmless because
    `_runtime_step_detail` returns nothing for `terminal` and `browser_*`.
    That is true of those two and false here: the detail for `write_file`,
    `patch` and `read_file` is the path's basename. So an unmarked
    `write_file(path="/etc/passwd")` refusal recorded
    "Writing a file: passwd" -- the argument of a blocked call, durably, to
    every dashboard subscriber.

    `_check_sensitive_path` and `_check_cross_profile_path` now return the
    explicit authorization disposition.
    """
    from agent import tool_executor
    from tools.registry import tool_authorization_error

    for message in ("Refusing to write to a sensitive path: /etc/passwd",
                    "Refusing to cross profile boundary: ../other/profile"):
        assert tool_executor._is_authorization_refusal(
            tool_authorization_error(message)
        ), message


@pytest.mark.parametrize("text", [
    "Basic dTpw",                       # u:p -- four characters
    "Basic YWRtaW46cGFzcw==",           # admin:pass
    "search Basic dTpw now",            # mid-sentence
])
def test_a_short_basic_credential_is_redacted(text):
    """Basic credentials have no minimum encoded length.

    The bare-scheme pattern applied an eight-character prose floor to `basic`
    as well as `bearer`, so `Basic dTpw` -- which is `u:p` -- survived into a
    row that is persisted and broadcast to task viewers. The governed
    redactor does not cover a bare scheme without an `Authorization:` header,
    so nothing else caught it either.
    """
    assert "[redacted]" in _scrub_step_text(text, max_chars=120), text


@pytest.mark.parametrize("text", [
    "basic setup for the project",
    "basic auth plan",
    "bearer of bad news",
    "the basic idea",
])
def test_prose_that_looks_like_a_scheme_survives(text):
    """The reason the floor was there, kept without the floor.

    Dropping the length rule for `basic` alone would redact ordinary prose,
    so the discriminator is what a Basic credential IS: base64 of
    `user:password`. `setup` and `basic` are not valid base64; `plan` and
    `auth` decode without a colon. `bearer` keeps its floor because a Bearer
    token shorter than eight characters is not a token.
    """
    assert _scrub_step_text(text, max_chars=120) == text, text


def test_the_search_files_path_guard_marks_its_refusal(monkeypatch) -> None:
    """`search_files` has its own pre-execution guard, driven for real.

    `get_read_block_error` returns before `file_ops.search` runs, so nothing
    was searched -- but it returned a plain `tool_error`, so the feed
    recorded "Searching the files" for a refused call. Same class as the
    read/write guards, a separate site.

    `file_ops.search` is replaced with a `pytest.fail`, so this also proves
    the guard returns BEFORE the search -- which is what makes it a refusal
    rather than a withheld result.
    """
    from agent import tool_executor
    from tools import file_tools

    monkeypatch.setattr(file_tools, "get_read_block_error",
                        lambda _p: "Blocked: Youtab internal path")
    monkeypatch.setattr(
        file_tools, "_get_file_ops",
        lambda _t: pytest.fail("the search ran despite the guard"))

    # The registered handler (`registry.register(name="search_files", ...,
    # handler=_handle_search_files)`), which is the real entry point; the
    # guard itself lives in a nested `search_tool`.
    raw_result = file_tools._handle_search_files({"pattern": "password", "path": "."})
    payload = json.loads(raw_result)

    assert payload.get("authorization") == "denied", payload
    assert "Blocked:" in payload["error"], payload
    assert tool_executor._is_authorization_refusal(raw_result), raw_result


def test_the_executors_refusal_marker_matches_the_registrys() -> None:
    """``tool_executor`` duplicates the marker as a literal to stay off the
    import path of the whole tool surface. That duplication is only safe if
    something fails when the two drift."""
    from agent import tool_executor
    from tools.registry import TOOL_AUTHORIZATION_DENIED

    assert tool_executor._TOOL_AUTHORIZATION_DENIED == TOOL_AUTHORIZATION_DENIED


def test_the_registry_helper_produces_what_the_executor_detects() -> None:
    """End to end on the envelope, so a change to either side shows up here."""
    from agent import tool_executor
    from tools.registry import tool_authorization_error, tool_error

    assert tool_executor._is_authorization_refusal(
        tool_authorization_error("Blocked: Youtab internal path")
    )
    assert not tool_executor._is_authorization_refusal(
        tool_error("provider timed out")
    )


def test_a_large_successful_result_is_not_parsed_to_decide(monkeypatch):
    """A big result must still emit, and must not be parsed to find out.

    Tool results carry whole file contents, so a successful `read_file` can be
    a megabyte of JSON. The substring probe rejects it after one scan without
    parsing, which is what the old size cap was for -- the cap just also broke
    the case below.
    """
    big = '{"content": "' + "x" * 20000 + '"}'
    emitted, ran, _outcome = _drive_middleware(monkeypatch, allows=True, tool_result=big)
    assert emitted == [("web_search", {"query": "best coffee"})]
    assert ran == [{"query": "best coffee"}]


def test_a_large_MARKED_refusal_is_still_recognised(monkeypatch):
    """The hole the size cap had, and the reason it is gone.

    A 4096-character cap meant a correctly marked refusal was ignored solely
    because its envelope was large -- and that is reachable, not theoretical:
    gateway ask mode echoes the full command back in its pending-approval
    payload, and the approval parser accepts compound commands well past any
    cap one would pick. The feed then recorded "Running a command" for a
    command that never ran, which is exactly what the marker exists to
    prevent.

    9000 characters is over the old cap by more than twice.
    """
    from tools.registry import tool_authorization_error

    refusal = tool_authorization_error("Command denied: " + "x" * 9000)
    assert len(refusal) > 4096, "fixture is not larger than the old cap"

    emitted, ran, _outcome = _drive_middleware(
        monkeypatch, allows=True, tool_result=refusal
    )
    assert ran == [{"query": "best coffee"}]
    assert emitted == [], "a marked refusal must not be recorded as work"


def test_plain_text_and_non_string_results_still_emit(monkeypatch):
    """Not every tool returns JSON; those must not be read as refusals."""
    for value in ("ok", "", None, 42, ["a"], '{"results": []}'):
        emitted, _ran, _outcome = _drive_middleware(
            monkeypatch, allows=True, tool_result=value
        )
        assert emitted == [("web_search", {"query": "best coffee"})], value


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


class _RecordingConn:
    """Wraps a real connection and records every statement executed on it.

    Also completes the task the moment the first statement returns, which is
    the TOCTOU window: with a SELECT fence followed by a separate INSERT, that
    completion lands between the two and the stale worker still inserts and
    broadcasts a step for an attempt that has ended.
    """

    def __init__(self, inner, on_first_statement):
        self._inner = inner
        self._on_first = on_first_statement
        self.statements: list[str] = []

    def execute(self, sql, parameters=(), /):
        result = self._inner.execute(sql, parameters)
        self.statements.append(" ".join(sql.split()))
        if len(self.statements) == 1:
            self._on_first()
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_the_attempt_check_and_the_append_are_one_statement(kanban_home, monkeypatch):
    """The fence must be part of the write, not a preflight before it.

    This append deliberately runs in SQLite autocommit -- a cosmetic row must
    not take write_txn's BEGIN IMMEDIATE retry boundary, which can stall a
    tool for minutes during a worker stampede. In autocommit, two statements
    are two transactions: a worker being reclaimed passes the SELECT, the
    dispatcher completes or supersedes the task, and the INSERT still lands.
    The dashboard broadcasts every insert, so a finished task visibly grows
    steps.

    The window cannot be closed by ordering the two statements differently,
    only by removing the second one, so that is what is asserted here: exactly
    ONE statement reaches the connection, it is the INSERT, and it carries the
    attempt predicate itself. `test_step_is_dropped_for_a_superseded_attempt`
    and `..._once_the_task_stops_running` both pass against the two-statement
    shape; this is the one that does not.
    """
    task_id = _new_task()
    run_id = _claim(task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(run_id))

    def complete_the_task():
        with kb.connect_closing() as other:
            other.execute("UPDATE tasks SET status='done' WHERE id = ?", (task_id,))

    recorded: list[_RecordingConn] = []
    real_connect = kb.connect_closing

    class _Ctx:
        def __enter__(self):
            self._cm = real_connect(busy_timeout_ms=2000)
            wrapper = _RecordingConn(self._cm.__enter__(), complete_the_task)
            recorded.append(wrapper)
            return wrapper

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    monkeypatch.setattr(kb, "connect_closing", lambda **kwargs: _Ctx())

    emit_runtime_step("web_search", '{"query": "x"}')

    assert recorded, "the emitter never opened a connection"
    statements = recorded[0].statements
    assert len(statements) == 1, (
        "the emitter ran more than one statement, so the attempt check and the "
        f"append are separate transactions under autocommit: {statements}"
    )
    assert statements[0].startswith("INSERT INTO task_events"), statements[0]
    assert "status = 'running'" in statements[0], (
        f"the INSERT does not carry the running fence: {statements[0]}"
    )
    assert "current_run_id = ?" in statements[0], (
        f"the INSERT does not carry the attempt fence: {statements[0]}"
    )


def test_the_atomic_append_reports_whether_it_wrote(kanban_home):
    """The helper's return value is how a caller can tell a drop from a write.

    Checked directly because `emit_runtime_step` is fail-open and swallows
    everything, so a silent regression to "always returns None" would not
    surface through it.
    """
    task_id = _new_task()
    run_id = _claim(task_id)

    with kb.connect_closing() as conn:
        assert kb.append_event_if_run_active(
            conn, task_id, "runtime_step", "step one", run_id=run_id
        ) is True
        # Superseded attempt: the row is fenced out, not written.
        assert kb.append_event_if_run_active(
            conn, task_id, "runtime_step", "stale", run_id=run_id + 1
        ) is False
        # And once the task stops running, neither form writes.
        conn.execute("UPDATE tasks SET status='done' WHERE id = ?", (task_id,))
        assert kb.append_event_if_run_active(
            conn, task_id, "runtime_step", "after the end", run_id=run_id
        ) is False
        assert kb.append_event_if_run_active(
            conn, task_id, "runtime_step", "after the end, unattributed"
        ) is False

    steps = _runtime_steps(task_id)
    assert len(steps) == 1, [s.payload for s in steps]


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
