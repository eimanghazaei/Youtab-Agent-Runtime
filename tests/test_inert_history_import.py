"""Synthetic account-sync history through real temporary SQLite and HTTP imports."""

import pytest
from starlette.testclient import TestClient

from youtab_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(db_path=tmp_path / "state.db")
    yield database
    database.close()


def history(count=1, content="synthetic history"):
    return {
        "id": "synthetic-import", "title": "Synced history",
        "started_at": 1000, "last_active": 2000,
        "messages": [{"role": "user" if i % 2 == 0 else "assistant", "content": content}
                     for i in range(count)],
    }


@pytest.mark.parametrize("count,content", [(10_001, "text"), (30, "x" * 200_000)], ids=["message-count", "text-size"])
def test_inert_import_extends_only_opt_in_limits(db, count, content):
    payload = history(count, content)
    assert db.import_sessions([payload])["ok"] is False
    result = db.import_sessions([payload], inert_history=True)
    assert result["imported"] == 1
    assert len(db.get_messages(payload["id"])) == count
    assert db.get_session(payload["id"])["started_at"] == 1000
    assert db.get_messages(payload["id"])[-1]["timestamp"] == 2000
    payload["messages"][0]["content"] = "overwrite attempt"
    assert db.import_sessions([payload], inert_history=True)["skipped"] == 1
    assert db.get_messages(payload["id"])[0]["content"] == content


def test_inert_projection_strips_authority_and_preserves_explicit_dates(db):
    payload = history()
    payload.update({"cwd": "C:/synthetic/path", "model": "synthetic-model",
                    "model_config": {"api_key": "synthetic-secret"},
                    "system_prompt": "synthetic authority", "parent_session_id": "other",
                    "command_envelope": {"authority": "synthetic"}, "api_key": "synthetic-secret"})
    payload["messages"][0].update({"timestamp": 1500, "tool_calls": [{"name": "execute"}],
                                  "api_content": "synthetic provider payload",
                                  "reasoning": "synthetic secret", "display_metadata": {"cwd": "path"}})
    assert db.import_sessions([payload], inert_history=True)["ok"]
    session = db.get_session(payload["id"])
    for field in ("cwd", "model", "model_config", "system_prompt", "parent_session_id"):
        assert session[field] is None
    message = db.get_messages(payload["id"])[0]
    assert message["timestamp"] == 1500
    for field in ("tool_calls", "api_content", "reasoning", "display_metadata"):
        assert not message.get(field)


@pytest.mark.parametrize("message", [
    {"role": "system", "content": "synthetic"},
    {"role": "tool", "content": "synthetic"},
    {"role": "assistant", "content": [{"text": "synthetic"}]},
    {"role": "user", "content": "x" * 262_145},
    {"role": "user", "content": "synthetic", "timestamp": float("nan")},
    {"role": "user", "content": "synthetic", "timestamp": 10 ** 400},
])
def test_inert_rejects_forbidden_messages_atomically(db, message):
    payload = history()
    payload["messages"] = [message]
    with pytest.raises(ValueError):
        db.import_sessions([history(), payload], inert_history=True)
    assert db.get_session(payload["id"]) is None


@pytest.mark.parametrize("count,content", [(100_001, "x"), (65, "x" * 262_144)], ids=["message-count", "text-size"])
def test_inert_rejects_total_bounds(db, count, content):
    with pytest.raises(ValueError):
        db.import_sessions([history(count, content)], inert_history=True)
    assert db.get_session("synthetic-import") is None


@pytest.mark.parametrize("count,content", [(10_001, "text"), (30, "x" * 200_000)], ids=["message-count", "text-size"])
def test_api_inert_import_uses_real_temporary_db(tmp_path, monkeypatch, count, content):
    from youtab_agent_cli import web_server

    database_path = tmp_path / "api-state.db"
    monkeypatch.setattr(web_server, "_open_session_db_for_profile",
                        lambda profile: SessionDB(db_path=database_path))
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    payload = {"sessions": [history(count, content)]}
    payload["sessions"][0]["model_config"] = {"api_key": "synthetic-secret"}
    payload["sessions"][0]["cwd"] = "C:/synthetic/path"
    assert client.post("/api/sessions/import", json=payload).status_code == 400
    payload["inert_history"] = True
    response = client.post("/api/sessions/import", json=payload)
    assert response.status_code == 200, response.text
    database = SessionDB(db_path=database_path)
    try:
        assert len(database.get_messages("synthetic-import")) == count
        assert database.get_session("synthetic-import")["model_config"] is None
        assert database.get_session("synthetic-import")["cwd"] is None
    finally:
        database.close()


def test_api_inert_import_rejects_role_and_non_boolean_opt_in(tmp_path, monkeypatch):
    from youtab_agent_cli import web_server

    database_path = tmp_path / "api-state.db"
    monkeypatch.setattr(web_server, "_open_session_db_for_profile",
                        lambda profile: SessionDB(db_path=database_path))
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    payload = {"sessions": [history()], "inert_history": "true"}
    assert client.post("/api/sessions/import", json=payload).status_code == 400
    payload["inert_history"] = True
    payload["sessions"][0]["messages"][0]["role"] = "tool"
    assert client.post("/api/sessions/import", json=payload).status_code == 400
    database = SessionDB(db_path=database_path)
    try:
        assert database.get_session("synthetic-import") is None
    finally:
        database.close()
