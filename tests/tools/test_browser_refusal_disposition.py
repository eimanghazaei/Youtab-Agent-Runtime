"""`browser_tool`'s policy refusals must be distinguishable from its failures.

The managed-run progress feed records a step for every tool that ran and
skips the ones that were refused. It tells the two apart by an explicit
`"authorization": "denied"` key
(`tools.registry.TOOL_AUTHORIZATION_DENIED`), because handlers use one error
envelope for both and inferring from the envelope was wrong in the costly
direction -- an authorized call that ran and then failed stopped appearing in
the feed at all.

`browser_tool` has twenty-five error returns. Seventeen refuse before the
action happens and carry the marker; eight do not and must not, because
marking them would suppress a row for work that really occurred or hide a
failure worth seeing. Both halves are asserted here: the inventory test pins
the counts so a new guard cannot be added without a decision, and the
behavioural tests drive the real guards rather than asserting on source.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSER_TOOL = REPO_ROOT / "tools" / "browser_tool.py"

#: Functions whose error returns are POLICY refusals, with how many each has.
#: `_blocked_private_page_action` is one site serving three tools
#: (browser_click, browser_type, browser_press).
EXPECTED_MARKED = {
    "browser_navigate": 6,
    "browser_snapshot": 1,
    "browser_back": 1,
    "_blocked_private_page_action": 1,
    "browser_console": 2,
    "_browser_eval": 3,
    "_camofox_eval": 1,
    "browser_get_images": 1,
    "browser_vision": 1,
}

#: Error returns that are NOT authorization decisions, and why. Marking any of
#: these would delete a legitimate progress row.
EXPECTED_UNMARKED = {
    # Two post-redirect guards: `_run_browser_command(open)` has already
    # succeeded, so the page WAS fetched; these withhold the result and reset
    # the session to about:blank. The navigation happened.
    "browser_navigate": 3,       # + the navigation-failed runtime return
    "browser_scroll": 1,         # invalid `direction` -- input validation
    "_browser_eval": 1,          # generic runtime error
    "_camofox_eval": 2,          # JS-eval unsupported (capability) + generic
    "browser_vision": 1,         # screenshot file missing -- runtime
}


def _error_returns() -> tuple[dict[str, int], dict[str, int]]:
    """Every error return in browser_tool.py, split by whether it is marked."""
    src = BROWSER_TOOL.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    tree = ast.parse(src)

    owner: dict[int, tuple[str, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                previous = owner.get(line)
                if previous is None or node.lineno > previous[1]:
                    owner[line] = (node.name, node.lineno)

    marked: dict[str, int] = {}
    unmarked: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        segment = ast.get_source_segment(src, node.value) or ""
        if "json.dumps" not in segment and "tool_error" not in segment:
            continue
        if not any(token in segment for token in
                   ('"success": False', "'success': False", "tool_error")):
            continue
        name = owner.get(node.lineno, ("<module>", 0))[0]
        bucket = marked if '"authorization": "denied"' in segment else unmarked
        bucket[name] = bucket.get(name, 0) + 1
    return marked, unmarked


def test_every_policy_refusal_carries_the_marker() -> None:
    """A new guard cannot be added without deciding which half it is in.

    Counts rather than line numbers, so ordinary edits to the file do not
    churn this test while a new or removed error return still does.
    """
    marked, _ = _error_returns()
    assert marked == EXPECTED_MARKED, (
        "the set of MARKED policy refusals changed. If a guard was added, give "
        "it `\"authorization\": \"denied\"` when it refuses before the action "
        "happens, and update EXPECTED_MARKED. If it is a runtime failure, leave "
        "it unmarked and update EXPECTED_UNMARKED instead -- marking a failure "
        "deletes a progress row for work that really happened.\n"
        f"expected {EXPECTED_MARKED}\ngot      {marked}"
    )


def test_the_unmarked_returns_stay_unmarked() -> None:
    """The other half, which is the easier one to get wrong.

    Marking these would look like completing a pattern and would silently
    suppress steps -- in particular the two post-redirect guards, where the
    browser really did navigate before the result was withheld.
    """
    _, unmarked = _error_returns()
    assert unmarked == EXPECTED_UNMARKED, (
        "the set of UNMARKED error returns changed; see the note in "
        "browser_tool.browser_navigate above the pre-navigation guards.\n"
        f"expected {EXPECTED_UNMARKED}\ngot      {unmarked}"
    )


def test_the_marker_matches_the_registry_constant() -> None:
    """The literal in browser_tool must be the registry's value."""
    from tools.registry import TOOL_AUTHORIZATION_DENIED

    src = BROWSER_TOOL.read_text(encoding="utf-8")
    assert f'"authorization": "{TOOL_AUTHORIZATION_DENIED}"' in src


# ---------------------------------------------------------------------------
# Behaviour: drive the real guards, then feed the real payload to the real
# detector. No stubbing of either side.
# ---------------------------------------------------------------------------
def _detects(payload: str) -> bool:
    from agent import tool_executor

    return tool_executor._is_authorization_refusal(payload)


def test_a_credential_bearing_url_is_refused_and_detected() -> None:
    """Pre-navigation guard: returns before any browser command runs.

    No browser is needed precisely because the guard fires first, which is
    also what makes it an authorization refusal.
    """
    from tools.browser_tool import browser_navigate

    raw = browser_navigate("https://example.test/x?token=sk-ant-abcdefghijklmnop")
    payload = json.loads(raw)

    assert payload["success"] is False
    assert "Blocked:" in payload["error"]
    assert payload["authorization"] == "denied"
    assert _detects(raw), raw


def test_the_shared_private_page_guard_is_refused_and_detected(monkeypatch) -> None:
    """One site, three tools: browser_click, browser_type and browser_press."""
    from tools import browser_tool

    monkeypatch.setattr(browser_tool, "_eval_ssrf_guard_active", lambda *_a: True)
    monkeypatch.setattr(browser_tool, "_current_page_private_url",
                        lambda *_a: "http://169.254.169.254/latest/meta-data/")

    for action in ("click", "type", "press"):
        raw = browser_tool._blocked_private_page_action("t1", action)
        assert raw is not None, action
        payload = json.loads(raw)
        assert payload["authorization"] == "denied", action
        assert action in payload["error"], action
        assert _detects(raw), action


def test_the_console_eval_policy_refusal_is_detected(monkeypatch) -> None:
    """The config-gated denylist: the expression is never evaluated."""
    from tools import browser_tool

    monkeypatch.setattr(browser_tool, "_restrict_browser_evaluate", lambda: True)
    monkeypatch.setattr(browser_tool, "_allow_unsafe_browser_evaluate", lambda: False)
    monkeypatch.setattr(browser_tool, "_risky_browser_eval_reason",
                        lambda _expression: "document.cookie")
    # If the guard did not fire, this would be reached -- so the assertion
    # below also proves the expression was not evaluated.
    monkeypatch.setattr(browser_tool, "_browser_eval",
                        lambda *_a, **_k: pytest.fail("the expression was evaluated"))

    raw = browser_tool.browser_console(expression="document.cookie")
    payload = json.loads(raw)

    assert payload["success"] is False
    assert payload["authorization"] == "denied"
    assert "document.cookie" in payload["error"]
    assert _detects(raw), raw


def test_an_ordinary_browser_failure_is_not_detected_as_a_refusal() -> None:
    """The half that must keep emitting steps.

    `browser_scroll` with a bad direction is input validation, not a policy
    decision: the tool ran and rejected the argument, and the feed should say
    so.
    """
    from tools.browser_tool import browser_scroll

    raw = browser_scroll(direction="sideways")
    payload = json.loads(raw)

    assert payload["success"] is False
    assert "authorization" not in payload, payload
    assert not _detects(raw), raw
