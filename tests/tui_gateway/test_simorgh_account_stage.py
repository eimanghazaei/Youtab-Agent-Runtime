"""A Desktop account bearer is one-use turn data, never a model argument."""

import hashlib
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server


def _stage(monkeypatch, *, peer="127.0.0.1:1234", desktop=True):
    monkeypatch.setenv("YOUTAB_AGENT_DESKTOP", "1" if desktop else "0")
    monkeypatch.setattr(server, "current_transport", lambda: SimpleNamespace(_peer=peer))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_: False)
    session = {"history_lock": threading.Lock(), "running": False}
    monkeypatch.setitem(server._sessions, "owned-session", session)
    text = "Read account A"
    params = {
        "session_id": "owned-session",
        "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "account_bearer": "synthetic-account",
    }
    response = server.handle_request({"id": "stage", "method": "simorgh.account.stage", "params": params})
    return session, text, response


def test_one_use_exact_text_and_no_bearer_echo(monkeypatch):
    session, text, response = _stage(monkeypatch)
    nonce = response["result"]["nonce"]
    assert "synthetic-account" not in str(response)
    assert server._take_simorgh_account_stage(session, text, "wrong") is None
    assert server._take_simorgh_account_stage(session, text, nonce) is None

    session, text, response = _stage(monkeypatch)
    nonce = response["result"]["nonce"]
    assert server._take_simorgh_account_stage(session, "Read account B", nonce) is None

    session, text, response = _stage(monkeypatch)
    nonce = response["result"]["nonce"]
    assert server._take_simorgh_account_stage(session, text, nonce) == "synthetic-account"
    assert server._take_simorgh_account_stage(session, text, nonce) is None


def test_nonlocal_busy_and_malformed_stage_fail_closed(monkeypatch):
    for peer, desktop in [("100.108.46.86:1234", True), ("127.0.0.1:1234", False)]:
        session, _, response = _stage(monkeypatch, peer=peer, desktop=desktop)
        assert "error" in response
        assert "_simorgh_account_stage" not in session

    session, text, response = _stage(monkeypatch)
    assert "result" in response
    session["running"] = True
    params = {"session_id": "owned-session", "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(),
              "account_bearer": "synthetic-account"}
    assert "error" in server.handle_request({"id": "busy", "method": "simorgh.account.stage", "params": params})
    session["running"] = False
    params["account_bearer"] = "bad bearer"
    assert "error" in server.handle_request({"id": "bad", "method": "simorgh.account.stage", "params": params})


def test_stage_expiry_removes_secret(monkeypatch):
    session, _, response = _stage(monkeypatch)
    nonce = response["result"]["nonce"]
    server._expire_simorgh_account_stage(session, nonce)
    assert "_simorgh_account_stage" not in session


def test_busy_submit_discards_staged_credential(monkeypatch):
    session, text, response = _stage(monkeypatch)
    session["running"] = True
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_handle_busy_submit", lambda rid, *_args, **_kwargs: server._ok(rid, {"status": "queued"}))
    result = server.handle_request({"id": "submit", "method": "prompt.submit", "params": {
        "session_id": "owned-session", "text": text,
        "simorgh_account_nonce": response["result"]["nonce"], "queued": True,
    }})
    assert result["result"]["status"] == "queued"
    assert "_simorgh_account_stage" not in session


@pytest.mark.parametrize("wrapped", [False, True])
def test_prompt_submit_only_binds_exact_unsanitized_account_turn(monkeypatch, wrapped):
    session, text, _ = _stage(monkeypatch)
    raw = "\x1b[200~" + text + "\x1b[201~" if wrapped else text
    session.update({"agent": SimpleNamespace(), "session_key": "owned-session",
                    "history": [], "history_version": 0, "attached_images": []})
    params = {"session_id": "owned-session", "prompt_sha256": hashlib.sha256(raw.encode()).hexdigest(),
              "account_bearer": "synthetic-account"}
    staged = server.handle_request({"id": "raw-stage", "method": "simorgh.account.stage", "params": params})
    observed = []
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *_: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *_args, **kwargs: observed.append(kwargs))

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    result = server.handle_request({"id": "submit", "method": "prompt.submit", "params": {
        "session_id": "owned-session", "text": raw,
        "simorgh_account_nonce": staged["result"]["nonce"],
    }})
    assert result["result"]["status"] == "streaming"
    assert observed == ([{}] if wrapped else [{"simorgh_account_bearer": "synthetic-account"}])
    assert "_simorgh_account_stage" not in session
