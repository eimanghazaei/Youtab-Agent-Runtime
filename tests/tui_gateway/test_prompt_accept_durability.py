"""An accepted prompt is durable before its turn can start.

prompt.submit answers ``streaming`` and then waits for the deferred agent
build. Packaged-app evidence showed the desktop tearing its backend down inside
that wait (a prompt close during a slow first-turn build): the prompt had no
marker and no transcript row, so it was lost after relaunch. These tests pin
the acceptance boundary: the crash-recovery marker exists as soon as the RPC
returns, the session stays listable while it only has that marker, and only an
explicit client-visible conclusion (cancel, or a build failure reported as
"not sent") retires it.
"""

from __future__ import annotations

import threading
import types

from tui_gateway import server as gw
from tui_gateway.turn_marker import pending_turn_keys, read_turn_marker, record_turn_start
from youtab_state import SessionDB


def _session(tmp_path, **extra):
    return {
        "agent": None,
        "agent_ready": threading.Event(),
        "session_key": "sess-accept-001",
        "profile_home": str(tmp_path),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


class _HeldThread:
    """Captures the deferred post-build runner so a test can run it later."""

    started: list = []

    def __init__(self, target=None, **_kwargs):
        self._target = target

    def start(self):
        _HeldThread.started.append(self._target)


def _submit(monkeypatch, session, sid, text):
    _HeldThread.started = []
    monkeypatch.setattr(gw, "_load_cfg", lambda: {})
    monkeypatch.setattr(gw, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(gw, "_persist_branch_seed", lambda _session: None)
    monkeypatch.setattr(gw, "_start_agent_build", lambda _sid, _session: None)
    monkeypatch.setattr(gw.threading, "Thread", _HeldThread)
    gw._sessions[sid] = session
    try:
        return gw.handle_request(
            {"id": "r1", "method": "prompt.submit", "params": {"session_id": sid, "text": text}}
        )
    finally:
        gw._sessions.pop(sid, None)


def test_accepted_prompt_has_durable_marker_before_the_agent_is_built(tmp_path, monkeypatch):
    session = _session(tmp_path)
    text = "E2E caption\n@image:composer-images/capture.png"

    resp = _submit(monkeypatch, session, "sid-accept", text)

    assert resp["result"] == {"status": "streaming"}
    assert not session["agent_ready"].is_set(), "the turn has not started"
    marker = read_turn_marker(tmp_path, "sess-accept-001")
    assert marker is not None, "an accepted prompt must survive a process death during the build"
    assert marker["prompt"] == text
    assert marker["attempts"] == 0


def test_accepted_marker_carries_staged_attachment_refs(tmp_path, monkeypatch):
    image = tmp_path / "composer-images" / "capture.png"
    image.parent.mkdir()
    image.write_bytes(b"png")
    session = _session(tmp_path, attached_images=[str(image)])

    _submit(monkeypatch, session, "sid-image", "E2E caption")

    marker = read_turn_marker(tmp_path, "sess-accept-001")
    assert marker is not None
    lines = marker["prompt"].splitlines()
    assert lines[0] == "E2E caption"
    assert len(lines) == 2 and lines[1].startswith("@image:") and "capture.png" in lines[1]
    # Staged images stay with the session for the real turn to consume.
    assert session["attached_images"] == [str(image)]


def test_cancel_before_agent_ready_retires_the_accepted_marker(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _submit(monkeypatch, session, "sid-cancel", "hello")
    assert read_turn_marker(tmp_path, "sess-accept-001") is not None

    emitted = []
    monkeypatch.setattr(gw, "_emit", lambda event, sid, payload=None: emitted.append(event))
    monkeypatch.setattr(gw, "_wait_agent_for_prompt", lambda _s, _r, _i: None)
    with session["history_lock"]:
        session["_turn_cancel_requested"] = True
    _HeldThread.started[0]()

    assert read_turn_marker(tmp_path, "sess-accept-001") is None
    assert "error" in emitted
    assert session["running"] is False


def test_build_failure_reported_as_not_sent_retires_the_marker(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _submit(monkeypatch, session, "sid-fail", "hello")

    terminal = []
    monkeypatch.setattr(gw, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_session_info", lambda *a, **k: {})
    monkeypatch.setattr(
        gw, "_emit_terminal_turn_error", lambda sid, sess, message: terminal.append(message)
    )
    monkeypatch.setattr(
        gw,
        "_wait_agent_for_prompt",
        lambda _s, rid, _i: gw._err(rid, 5032, "your message was not sent; retry"),
    )
    _HeldThread.started[0]()

    assert terminal == ["your message was not sent; retry"]
    assert read_turn_marker(tmp_path, "sess-accept-001") is None


def test_ready_agent_runs_the_turn_and_keeps_the_marker_for_the_turn(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _submit(monkeypatch, session, "sid-run", "hello")

    ran = []
    monkeypatch.setattr(gw, "_wait_agent_for_prompt", lambda _s, _r, _i: None)
    monkeypatch.setattr(gw, "_run_prompt_submit", lambda rid, sid, sess, text: ran.append(text))
    _HeldThread.started[0]()

    assert ran == ["hello"]
    # The turn owns the marker from here; only its durable conclusion retires it.
    assert read_turn_marker(tmp_path, "sess-accept-001") is not None


def test_pending_recovery_ids_respect_the_auto_continue_window(tmp_path, monkeypatch):
    record_turn_start(tmp_path, "fresh", "hello")
    monkeypatch.setattr(gw, "_auto_continue_config", lambda: (True, 900.0, 2))
    assert gw.pending_recovery_session_ids(tmp_path) == ["fresh"]
    assert pending_turn_keys(tmp_path, max_age_s=-1) == []
    monkeypatch.setattr(gw, "_auto_continue_config", lambda: (False, 900.0, 2))
    assert gw.pending_recovery_session_ids(tmp_path) == []


def test_message_less_session_with_pending_prompt_stays_listable(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("pending-first-prompt", source="desktop")
        db.create_session("abandoned-draft", source="desktop")

        hidden = [r["id"] for r in db.list_sessions_rich(min_message_count=1)]
        assert hidden == []

        listed = db.list_sessions_rich(min_message_count=1, include_ids=["pending-first-prompt"])
        assert [r["id"] for r in listed] == ["pending-first-prompt"]
        assert db.session_count(min_message_count=1, include_ids=["pending-first-prompt"]) == 1
        assert db.session_count(min_message_count=1) == 0
    finally:
        db.close()


def test_project_tree_lists_a_pending_first_prompt(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("pending-first-prompt", source="desktop")
        monkeypatch.setattr(gw, "_youtab_home", tmp_path)
        monkeypatch.setattr(gw, "pending_recovery_session_ids", lambda home: ["pending-first-prompt"])
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return []

        fake_db = types.SimpleNamespace(list_sessions_rich=_capture)
        try:
            gw._project_tree_inputs(fake_db, 10, include_discovered=False)
        except Exception:
            pass  # only the listing arguments matter here
        assert seen.get("include_ids") == ["pending-first-prompt"]
        assert seen.get("min_message_count") == 1
    finally:
        db.close()


def test_agent_build_delay_seam_is_inert_unless_armed(monkeypatch):
    slept = []
    monkeypatch.setattr(gw.time, "sleep", slept.append)
    for value in (None, "", "not-a-number", "0"):
        if value is None:
            monkeypatch.delenv("YOUTAB_AGENT_TEST_AGENT_BUILD_DELAY_S", raising=False)
        else:
            monkeypatch.setenv("YOUTAB_AGENT_TEST_AGENT_BUILD_DELAY_S", value)
        gw._test_agent_build_delay()
    assert slept == []
    monkeypatch.setenv("YOUTAB_AGENT_TEST_AGENT_BUILD_DELAY_S", "999")
    gw._test_agent_build_delay()
    assert slept == [120.0]


def test_marker_write_failure_refuses_prompt_before_agent_or_ack(tmp_path, monkeypatch):
    def _broken(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(gw, "record_turn_start", _broken)
    session = _session(tmp_path)

    resp = _submit(monkeypatch, session, "sid-broken-marker", "hello")

    assert resp["error"]["code"] == 5030
    assert "not accepted" in resp["error"]["message"]
    assert session["running"] is False
    assert session.get("inflight_turn") is None
    assert _HeldThread.started == []
    assert read_turn_marker(tmp_path, session["session_key"]) is None

    monkeypatch.setattr(gw, "record_turn_start", record_turn_start)
    retry = _submit(monkeypatch, session, "sid-broken-marker", "hello")
    assert retry["result"] == {"status": "streaming"}
    assert read_turn_marker(tmp_path, session["session_key"])["pending"]["text"] == "hello"


def test_marker_store_io_failure_is_reported_at_prompt_acceptance(tmp_path, monkeypatch):
    from tui_gateway import turn_marker

    def _broken_store(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(turn_marker, "_store", _broken_store)
    session = _session(tmp_path)

    resp = _submit(monkeypatch, session, "sid-io-failure", "hello")

    assert resp["error"]["code"] == 5030
    assert session["running"] is False
    assert _HeldThread.started == []


def test_unreadable_marker_file_refuses_new_prompt_without_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "desktop" / "interrupted_turns.json"
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")
    session = _session(tmp_path)

    resp = _submit(monkeypatch, session, "sid-corrupt-marker", "hello")

    assert resp["error"]["code"] == 5030
    assert path.read_text(encoding="utf-8") == "{broken"
    assert session["running"] is False
    assert _HeldThread.started == []


def test_full_marker_journal_refuses_new_prompt_without_evicting_one(tmp_path, monkeypatch):
    from tui_gateway import turn_marker

    monkeypatch.setattr(turn_marker, "_MAX_ENTRIES", 1)
    record_turn_start(tmp_path, "existing", "keep this prompt", strict=True)
    session = _session(tmp_path)

    resp = _submit(monkeypatch, session, "sid-full-marker", "new prompt")

    assert resp["error"]["code"] == 5030
    assert read_turn_marker(tmp_path, "existing")["prompt"] == "keep this prompt"
    assert read_turn_marker(tmp_path, session["session_key"]) is None
    assert session["running"] is False


def test_oversize_prompt_is_not_acknowledged_with_truncated_recovery(tmp_path, monkeypatch):
    from tui_gateway import turn_marker

    monkeypatch.setattr(turn_marker, "_MAX_PROMPT_CHARS", 3)
    session = _session(tmp_path)

    resp = _submit(monkeypatch, session, "sid-oversize", "hello")

    assert resp["error"]["code"] == 5030
    assert read_turn_marker(tmp_path, session["session_key"]) is None
    assert session["running"] is False


def test_image_upload_does_not_wait_for_the_agent_build(tmp_path, monkeypatch):
    """The first message's image upload gates its prompt.submit in the client;
    it must not sit behind the deferred agent build."""
    import base64

    session = _session(tmp_path)
    builds = []
    monkeypatch.setattr(gw, "_start_agent_build", lambda sid, _session: builds.append(sid))

    def _no_wait(*_args, **_kwargs):
        raise AssertionError("attachment staging must not wait for the agent")

    monkeypatch.setattr(gw, "_wait_agent", _no_wait)
    monkeypatch.setattr(gw, "_youtab_home", tmp_path)
    png = base64.b64encode(bytes.fromhex("89504e470d0a1a0a") + bytes(32)).decode()
    gw._sessions["sid-upload"] = session
    try:
        resp = gw.handle_request(
            {
                "id": "r1",
                "method": "image.attach_bytes",
                "params": {"session_id": "sid-upload", "content_base64": png, "filename": "capture.png"},
            }
        )
    finally:
        gw._sessions.pop("sid-upload", None)

    assert resp["result"]["attached"] is True, resp
    assert builds == ["sid-upload"], "the build still starts in the background"
    assert not session["agent_ready"].is_set()
    assert len(session["attached_images"]) == 1


def test_accepted_marker_keeps_the_original_input_for_replay(tmp_path, monkeypatch):
    image = tmp_path / "composer-images" / "capture.png"
    image.parent.mkdir()
    image.write_bytes(b"png")
    session = _session(tmp_path, attached_images=[str(image)])

    _submit(monkeypatch, session, "sid-pending", "E2E caption")

    marker = read_turn_marker(tmp_path, "sess-accept-001")
    assert marker["pending"] == {"text": "E2E caption", "images": [str(image)]}


def test_accepted_marker_is_fsynced_before_it_is_published(tmp_path, monkeypatch):
    from tui_gateway import turn_marker

    synced = []
    real_fsync = turn_marker.os.fsync
    monkeypatch.setattr(turn_marker.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))

    _submit(monkeypatch, _session(tmp_path), "sid-fsync", "hello")

    assert synced, "the recovery record must reach stable storage, not only the page cache"
    assert read_turn_marker(tmp_path, "sess-accept-001") is not None


def test_full_journal_reclaims_only_unrecoverable_markers(tmp_path, monkeypatch):
    """Stale markers (past the auto-continue window) are discarded by resume
    anyway, so they must not lock out new prompts; fresh ones are kept."""
    import json

    from tui_gateway import turn_marker

    monkeypatch.setattr(turn_marker, "_MAX_ENTRIES", 2)
    record_turn_start(tmp_path, "stale", "old prompt", strict=True)
    record_turn_start(tmp_path, "fresh", "recent prompt", strict=True)
    path = tmp_path / "desktop" / "interrupted_turns.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["stale"]["started_at"] -= 3600  # older than the 15-minute window
    path.write_text(json.dumps(data), encoding="utf-8")
    session = _session(tmp_path)

    resp = _submit(monkeypatch, session, "sid-reclaim", "new prompt")

    assert resp["result"] == {"status": "streaming"}
    assert read_turn_marker(tmp_path, "stale") is None
    assert read_turn_marker(tmp_path, "fresh")["prompt"] == "recent prompt"
    assert read_turn_marker(tmp_path, "sess-accept-001")["prompt"] == "new prompt"


def test_prompt_without_a_durable_session_key_is_refused(tmp_path, monkeypatch):
    session = _session(tmp_path, session_key="")

    resp = _submit(monkeypatch, session, "sid-no-key", "hello")

    assert resp["error"]["code"] == 5030
    assert session["running"] is False
    assert _HeldThread.started == []
