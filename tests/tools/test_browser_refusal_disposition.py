"""`browser_tool`'s policy refusals must be distinguishable from its failures.

The managed-run progress feed records a step for every tool that ran and
skips the ones that were refused. It tells the two apart by an explicit
`"authorization": "denied"` key
(`tools.registry.TOOL_AUTHORIZATION_DENIED`), because handlers use one error
envelope for both.

**The criterion is ORDERING, not the message text.** A guard carries the
marker only when it returns before the tool's action happens. The guards that
run the action first and then withhold its result must NOT carry it: the work
happened, so the step is true, and suppressing it deletes a trace row --
which AGENTS.md's capability posture forbids outright ("a mitigation must
preserve the feature while enforcing authority, tenant, trace, budget and
effect boundaries").

Every test here drives the real guard through its real entry point and reads
the disposition off the payload it returns. The earlier version of this file
parsed `browser_tool.py` and compared hard-coded counts of marked and
unmarked returns; that is a changing-inventory snapshot, which AGENTS.md:31
prohibits, and it broke on benign refactors while proving nothing about
runtime behaviour.
"""

from __future__ import annotations

import json

import pytest

from agent import tool_executor
from tools import browser_tool


def _refusal(payload: str | None) -> bool:
    """The disposition as the progress feed reads it."""
    assert payload is not None, "the guard did not fire"
    return tool_executor._is_authorization_refusal(payload)


@pytest.fixture
def cloud_backend(monkeypatch):
    """A non-local backend with the SSRF guards active.

    Every private-page guard in this module is gated on this state, so it is
    the precondition for driving any of them.
    """
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: False)
    monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: False)
    monkeypatch.setattr(browser_tool, "_is_local_sidecar_key", lambda *_a: False)
    monkeypatch.setattr(browser_tool, "_eval_ssrf_guard_active", lambda *_a: True)
    monkeypatch.setattr(
        browser_tool, "_current_page_private_url",
        lambda *_a: "http://169.254.169.254/latest/meta-data/",
    )
    return monkeypatch


# ---------------------------------------------------------------------------
# PRE-ACTION guards: these return before the tool does anything, so the call
# was refused and the feed must not record it as work.
# ---------------------------------------------------------------------------
def _navigate_apikey(monkeypatch):
    return browser_tool.browser_navigate(
        "https://example.test/x?token=sk-ant-abcdefghijklmnop")


def _navigate_sensitive_query(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sensitive_query_param_name",
                        lambda _url: "api_key")
    return browser_tool.browser_navigate("https://example.test/x?api_key=abc")


def _navigate_metadata(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sensitive_query_param_name", lambda _u: None)
    monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda _u: True)
    return browser_tool.browser_navigate("http://169.254.169.254/")


def _navigate_private(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sensitive_query_param_name", lambda _u: None)
    monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda _u: False)
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda _u: False)
    return browser_tool.browser_navigate("http://10.0.0.1/")


def _navigate_website_policy(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sensitive_query_param_name", lambda _u: None)
    monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda _u: False)
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda _u: True)
    monkeypatch.setattr(
        browser_tool, "check_website_access",
        lambda _u: {"message": "Blocked by website policy", "host": "x.test",
                    "rule": "deny", "source": "config.yaml"},
    )
    return browser_tool.browser_navigate("https://x.test/")


def _shared_page_guard(monkeypatch, action="click"):
    return browser_tool._blocked_private_page_action("t1", action)


def _console_eval_policy(monkeypatch):
    monkeypatch.setattr(browser_tool, "_restrict_browser_evaluate", lambda: True)
    monkeypatch.setattr(browser_tool, "_allow_unsafe_browser_evaluate", lambda: False)
    monkeypatch.setattr(browser_tool, "_risky_browser_eval_reason",
                        lambda _e: "document.cookie")
    monkeypatch.setattr(browser_tool, "_browser_eval",
                        lambda *_a, **_k: pytest.fail("the expression was evaluated"))
    return browser_tool.browser_console(expression="document.cookie")


def _console_private_page(monkeypatch):
    monkeypatch.setattr(
        browser_tool, "_run_browser_command",
        lambda *_a, **_k: pytest.fail("the console was read"))
    return browser_tool.browser_console()


def _eval_expression_prescan(monkeypatch):
    monkeypatch.setattr(browser_tool, "_expression_targets_private_url",
                        lambda _e: "http://10.0.0.1/")
    return browser_tool._browser_eval("fetch('http://10.0.0.1/')")


PRE_ACTION = [
    pytest.param(_navigate_apikey, id="navigate:api-key-in-url"),
    pytest.param(_navigate_sensitive_query, id="navigate:credential-query-param"),
    pytest.param(_navigate_metadata, id="navigate:cloud-metadata-floor"),
    pytest.param(_navigate_private, id="navigate:private-address"),
    pytest.param(_navigate_website_policy, id="navigate:website-policy"),
    pytest.param(_shared_page_guard, id="shared:private-page-action"),
    pytest.param(_console_eval_policy, id="console:eval-denylist"),
    pytest.param(_console_private_page, id="console:private-page"),
    pytest.param(_eval_expression_prescan, id="eval:expression-pre-scan"),
]


@pytest.mark.parametrize("drive", PRE_ACTION)
def test_a_pre_action_guard_is_reported_as_a_refusal(cloud_backend, drive):
    """The action never happened, so the feed must not record it."""
    payload = drive(cloud_backend)
    parsed = json.loads(payload)
    assert parsed.get("success") is False, parsed
    assert parsed.get("authorization") == "denied", parsed
    assert _refusal(payload)


@pytest.mark.parametrize("action", ["click", "type", "press"])
def test_the_shared_page_guard_covers_all_three_input_tools(cloud_backend, action):
    """One site serving three tools, and all three return it before acting.

    `browser_click`, `browser_type` and `browser_press` each `return blocked`
    ahead of their `_run_browser_command`, which is what makes this one
    pre-action rather than three separate decisions.
    """
    payload = browser_tool._blocked_private_page_action("t1", action)
    parsed = json.loads(payload)
    assert action in parsed["error"]
    assert parsed["authorization"] == "denied"
    assert _refusal(payload)


# ---------------------------------------------------------------------------
# POST-ACTION guards: the action ran, and the guard withholds its result. The
# work happened, so the step is TRUE and must still be emitted.
# ---------------------------------------------------------------------------
def _ok(data=None):
    return {"success": True, "data": data or {}}


def _snapshot_after_run(monkeypatch):
    # The guard does not use `_current_page_private_url`: it evaluates
    # `window.location.href` itself and feeds the result to `_is_safe_url`.
    # So the stub has to answer both commands the function issues.
    def run(_key, command, args=None, **_kwargs):
        if command == "eval":
            return _ok({"result": '"http://10.0.0.1/"'})
        return _ok({"snapshot": "tree", "refs": {}})

    monkeypatch.setattr(browser_tool, "_run_browser_command", run)
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda _u: False)
    return browser_tool.browser_snapshot()


def _back_after_run(monkeypatch):
    monkeypatch.setattr(browser_tool, "_run_browser_command",
                        lambda *_a, **_k: _ok({"url": "http://10.0.0.1/"}))
    return browser_tool.browser_back()


def _get_images_after_run(monkeypatch):
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda _u: False)
    monkeypatch.setattr(browser_tool, "_run_browser_command",
                        lambda *_a, **_k: _ok({"result": []}))
    return browser_tool.browser_get_images()


def _camofox_eval_after_run(monkeypatch):
    """The Camofox eval path, which posts the expression BEFORE rechecking.

    `_camofox_eval` imports `_ensure_tab` and `_post` from
    `tools.browser_camofox` inside the function body, so that module is the
    patch target rather than `browser_tool`.
    """
    from tools import browser_camofox

    posted: list[str] = []

    def post(path, body=None, **_kwargs):
        posted.append(path)
        return {"result": "null"}

    monkeypatch.setattr(browser_camofox, "_ensure_tab",
                        lambda _task: {"tab_id": "t1", "user_id": "u1"})
    monkeypatch.setattr(browser_camofox, "_post", post)
    monkeypatch.setattr(browser_tool, "_eval_ssrf_guard_active", lambda _t: True)
    monkeypatch.setattr(browser_tool, "_camofox_current_page_private_url",
                        lambda *_a, **_k: "http://10.0.0.1/")

    payload = browser_tool._camofox_eval("location='http://10.0.0.1/'")

    # The point of the case: the expression was DELIVERED to the page before
    # the guard looked. Asserted rather than assumed, because assuming it is
    # how this guard got misclassified in the first place -- an audit of the
    # lines between `def` and the guard missed the `_post` above it.
    assert any("/evaluate" in p for p in posted), (
        "this driver only tests a POST-action guard if the eval really ran: "
        + repr(posted)
    )
    return payload


POST_ACTION = [
    pytest.param(_snapshot_after_run, id="snapshot:after-the-snapshot"),
    pytest.param(_back_after_run, id="back:after-the-history-move"),
    pytest.param(_get_images_after_run, id="get_images:after-the-eval"),
    pytest.param(_camofox_eval_after_run, id="camofox_eval:after-the-eval"),
]


@pytest.mark.parametrize("drive", POST_ACTION)
def test_a_post_action_guard_is_not_reported_as_a_refusal(cloud_backend, drive):
    """The half I got wrong first, and the reason the criterion is ordering.

    Each of these runs its command, succeeds, and only then discovers the page
    is private. The snapshot was taken, the history move executed, the eval
    ran. Marking them suppressed a true progress row for work that really
    happened.
    """
    payload = drive(cloud_backend)
    parsed = json.loads(payload)
    assert parsed.get("success") is False, parsed
    assert "Blocked:" in parsed.get("error", ""), parsed
    assert "authorization" not in parsed, (
        "this guard withholds a RESULT after acting; marking it deletes a true "
        f"trace row: {parsed}"
    )
    assert not _refusal(payload)


def test_an_ordinary_browser_failure_is_not_reported_as_a_refusal() -> None:
    """Input validation is not a policy decision.

    `browser_scroll` with a bad direction ran and rejected the argument, and
    the feed should say so rather than going silent.
    """
    payload = browser_tool.browser_scroll(direction="sideways")
    parsed = json.loads(payload)
    assert parsed["success"] is False
    assert "authorization" not in parsed, parsed
    assert not _refusal(payload)


def test_the_registry_helper_produces_what_the_executor_detects() -> None:
    """End to end on the envelope, so a change to either side shows up.

    This replaces an assertion that searched `browser_tool.py` for the
    literal string: what matters is that the value the guards emit is the one
    the executor recognises, which is observable without reading source.
    """
    from tools.registry import tool_authorization_error, tool_error

    assert tool_executor._is_authorization_refusal(
        tool_authorization_error("Blocked: Youtab internal path")
    )
    assert not tool_executor._is_authorization_refusal(
        tool_error("provider timed out")
    )
