"""Full-history sync export uses only a disposable local session database."""
import asyncio

from youtab_agent_cli.web_routers import sessions
from youtab_state import SessionDB


def test_sync_export_preserves_compressed_history_and_original_dates(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    db = SessionDB(path)
    db.create_session("root", "desktop")
    db.append_message("root", "user", "old message", timestamp=101)
    db.end_session("root", "compression")
    db.create_session("tip", "desktop", parent_session_id="root")
    db.append_message("tip", "assistant", "new message", timestamp=201)
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET started_at=100, ended_at=150 WHERE id='root'"))
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET started_at=200 WHERE id='tip'"))
    db.close()
    monkeypatch.setattr(sessions, "_open_session_db_for_profile", lambda profile: SessionDB(path))
    result = asyncio.run(sessions.export_session_endpoint("tip", lineage=True))
    assert result["started_at"] == 100
    assert [message["content"] for message in result["messages"]] == ["old message", "new message"]
    assert [message["timestamp"] for message in result["messages"]] == [101, 201]
    root_export = asyncio.run(sessions.export_session_endpoint("root", lineage=True))
    assert root_export["messages"] == result["messages"]
    flat = asyncio.run(sessions.export_session_endpoint("tip"))
    assert len(flat["messages"]) == 1  # Existing export behavior remains opt-in.


def test_sync_inventory_includes_archived_chats_without_pinned_backfill(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    db = SessionDB(path)
    for index in range(105):
        sid = f"chat-{index}"
        db.create_session(sid, "desktop")
        db.append_message(sid, "user", "history")
        db._execute_write(lambda conn, sid=sid, index=index: conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?", (index + 100, sid)))
    db.set_session_archived("chat-0", True)
    db.set_session_pinned("chat-0", True)
    db.close()
    monkeypatch.setattr(sessions, "_open_session_db_for_profile", lambda profile: SessionDB(path))
    monkeypatch.setattr(sessions, "_maybe_auto_archive_for_profile", lambda *args: None)
    first = sessions.get_sessions(limit=100, archived="include", include_pinned=False)
    second = sessions.get_sessions(limit=100, offset=100, archived="include", include_pinned=False)
    assert len(first["sessions"]) == 100
    ids = [row["id"] for row in first["sessions"] + second["sessions"]]
    assert len(set(ids)) == 105
    assert "chat-0" in ids
    with_db = SessionDB(path)
    try:
        assert with_db.get_session("chat-0")["archived"] == 1
    finally:
        with_db.close()
