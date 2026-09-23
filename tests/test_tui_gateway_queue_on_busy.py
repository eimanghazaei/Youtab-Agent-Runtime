"""A prompt that lands mid-turn is redirected or queued, never dropped.

Before this, ``prompt.submit`` on a running session returned ``session busy``,
forcing clients into a deadline-bounded busy-retry. When turn teardown outlived
the deadline — e.g. a slow, non-interruptible tool (``web_search``) still
running when the user hit stop — the resubmitted message was silently dropped
("it just doesn't listen"). The gateway now applies the ``busy_input_mode``
policy: redirect the live turn by default, with the legacy interrupt + queue
path retained as a compatibility fallback.
"""

import threading
import time
import types

import pytest

import tools.async_delegation as ad
from run_agent import AIAgent
from tui_gateway import server


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
        **extra,
    }


def _steer_agent():
    agent = object.__new__(AIAgent)
    agent._pending_steer_lock = threading.Lock()
    agent._pending_steer = None
    agent._gateway_steer_turn = None
    return agent


# ── _enqueue_prompt ────────────────────────────────────────────────────────

def test_enqueue_pins_text_and_transport():
    session = _session()
    server._enqueue_prompt(session, "hello", "ws-1")
    assert session["queued_prompt"] == {"text": "hello", "transport": "ws-1"}




# ── _handle_busy_submit (policy) ───────────────────────────────────────────

def test_busy_interrupt_mode_redirects_active_turn(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    seen = []
    agent = types.SimpleNamespace(
        _supports_active_turn_redirect=True,
        redirect=lambda text: seen.append(text) or True,
        interrupt=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("redirect must not hard-interrupt")
        ),
    )
    session = _session(agent=agent, running=True)
    session["inflight_turn"] = {"user": "original request", "assistant": "partial reply"}

    resp = server._handle_busy_submit("r1", "sid", session, "redirect", "ws-1")

    assert resp["result"]["status"] == "redirected"
    assert seen == ["redirect"]
    # Appended, not overwritten: the original prompt must stay recoverable.
    assert session["inflight_turn"]["user"] == "original request"
    assert session["inflight_turn"]["corrections"] == ["redirect"]
    assert session.get("queued_prompt") is None








def test_busy_interrupt_mode_ignores_completed_background_delegation(monkeypatch):
    """A terminal delegation must not suppress normal busy-turn interruption."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    calls = {"interrupt": 0}
    agent = types.SimpleNamespace(
        interrupt=lambda *a, **k: calls.__setitem__("interrupt", calls["interrupt"] + 1)
    )
    session = _session(agent=agent, running=True)

    with ad._records_lock:
        ad._records["deleg_completed"] = {
            "delegation_id": "deleg_completed",
            "status": "completed",
            "session_key": "session-key",
            "origin_ui_session_id": "sid",
        }

    try:
        resp = server._handle_busy_submit("r1", "sid", session, "continue", "ws-1")
    finally:
        with ad._records_lock:
            ad._records.clear()

    assert resp["result"]["status"] == "queued"
    assert calls["interrupt"] == 1
    assert session["queued_prompt"]["text"] == "continue"




def test_busy_steer_mode_injects_when_accepted(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    agent = _steer_agent()
    session = _session(agent=agent, running=True)
    token = object()
    agent.begin_steer_turn(token)
    session["busy_steer_token"] = token

    resp = server._handle_busy_submit("r1", "sid", session, "nudge", "ws-1")

    assert resp["result"]["status"] == "steered"
    assert session.get("queued_prompt") is None
    assert agent.close_steer_turn(token) == "nudge"


def test_busy_steer_queues_when_turn_completed_before_acceptance(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    agent = _steer_agent()
    session = _session(agent=agent, running=True)
    old_turn = {"user": "A"}
    session["inflight_turn"] = old_turn
    token = object()
    agent.begin_steer_turn(token)
    session["busy_steer_token"] = token
    assert agent.close_steer_turn(token) is None

    assert server._handle_busy_submit("r1", "sid", session, "B", "ws-1")["result"] == {"status": "queued"}
    assert session["queued_prompt"]["text"] == "B"
    assert agent._pending_steer is None


def test_busy_steer_completion_during_acceptance_queues_once(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    agent = _steer_agent()
    session = _session(agent=agent, running=True)
    session["inflight_turn"] = {"user": "A"}
    token = object()
    agent.begin_steer_turn(token)
    session["busy_steer_token"] = token
    entered = threading.Event()
    release = threading.Event()
    real_steer = agent.steer_for_turn

    def delayed_steer(text, turn_token):
        entered.set()
        assert release.wait(2)
        return real_steer(text, turn_token)

    agent.steer_for_turn = delayed_steer
    response = []
    submit = threading.Thread(target=lambda: response.append(
        server._handle_busy_submit("r1", "sid", session, "B", "ws-1")
    ))
    submit.start()
    assert entered.wait(2)
    # Agent finalization closes acceptance before the pending call gets to
    # the slot. The gateway must deliver B as a next-turn prompt instead.
    assert agent.close_steer_turn(token) is None
    release.set()
    submit.join(2)
    assert not submit.is_alive()
    assert response[0]["result"] == {"status": "queued"}
    assert session["queued_prompt"]["text"] == "B"
    assert agent._pending_steer is None


def test_busy_redirect_acks_accepted_text_when_turn_is_replaced(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    session = _session(running=True)
    old_turn = {"user": "A"}
    session["inflight_turn"] = old_turn

    def _redirect(_text):
        with session["history_lock"]:
            session["inflight_turn"] = {"user": "C"}
        return True

    session["agent"] = types.SimpleNamespace(
        _supports_active_turn_redirect=True, redirect=_redirect
    )
    assert server._handle_busy_submit("r1", "sid", session, "B", "ws-1")["result"] == {"status": "redirected"}
    assert old_turn.get("corrections") is None
    assert session.get("queued_prompt") is None


@pytest.mark.parametrize("mode,method,finish", [
    ("interrupt", "redirect", False),
])
def test_prompt_submit_never_reclaims_an_accepted_busy_correction(
    monkeypatch, mode, method, finish
):
    """Acceptance by the old agent wins over a later turn-state change."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: mode)
    session = _session(running=True)
    old_turn = {"user": "A"}
    session["inflight_turn"] = old_turn
    accepted = []

    def _accept(text):
        accepted.append(text)
        with session["history_lock"]:
            if finish:
                session["running"] = False
            else:
                session["inflight_turn"] = {"user": "C"}
        return True

    agent = types.SimpleNamespace(_supports_active_turn_redirect=True)
    setattr(agent, method, _accept)
    session["agent"] = agent
    monkeypatch.setattr(
        server, "_record_accepted_turn",
        lambda *_args: pytest.fail("accepted correction was submitted as a new turn"),
    )
    server._sessions["sid-busy-race"] = session
    try:
        response = server.handle_request({
            "id": "r1", "method": "prompt.submit",
            "params": {"session_id": "sid-busy-race", "text": "B"},
        })
    finally:
        server._sessions.pop("sid-busy-race", None)

    assert response["result"] == {"status": "steered" if finish else "redirected"}
    assert accepted == ["B"]
    assert session.get("queued_prompt") is None






def test_busy_helper_retries_when_turn_finished(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    session = _session(running=False)

    assert server._handle_busy_submit("r1", "sid", session, "run now", "ws-1") is None
    assert session.get("queued_prompt") is None






def test_busy_interrupt_mode_queues_multimodal_payload_instead_of_redirect(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    seen = []
    rich = [
        {"type": "text", "text": "caption"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]
    agent = types.SimpleNamespace(
        _supports_active_turn_redirect=True,
        redirect=lambda text: seen.append(text) or True,
        interrupt=lambda *a, **k: None,
    )
    session = _session(agent=agent, running=True)

    resp = server._handle_busy_submit("r1", "sid", session, rich, "ws-1")

    assert resp["result"]["status"] == "queued"
    assert seen == []
    assert session["queued_prompt"]["text"] == rich


# ── _drain_queued_prompt ───────────────────────────────────────────────────

def test_drain_fires_queued_prompt_and_claims_running(monkeypatch):
    fired = {}
    monkeypatch.setattr(
        server, "_run_prompt_submit",
        lambda rid, sid, session, text: fired.update(rid=rid, sid=sid, text=text),
    )
    session = _session(queued_prompt={"text": "go", "transport": "ws-9"})

    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert fired == {"rid": "r1", "sid": "sid", "text": "go"}
    assert session["running"] is True
    assert session["queued_prompt"] is None
    assert session["transport"] == "ws-9"






def test_drain_releases_running_on_dispatch_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("dispatch failed")
    monkeypatch.setattr(server, "_run_prompt_submit", _boom)
    session = _session(queued_prompt={"text": "go", "transport": None})

    assert server._drain_queued_prompt("r1", "sid", session) is True
    # Failure must not leave the session wedged as running.
    assert session["running"] is False

