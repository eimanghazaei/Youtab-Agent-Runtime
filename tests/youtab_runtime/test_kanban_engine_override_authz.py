"""The per-task engine override is refused in-handler, like ``PUT /api/config``.

``POST /tasks``, ``PATCH /tasks/{}`` and ``POST /tasks/bulk`` stay ``plugin:use``
at the route table — creating and editing one's own tasks is the product. But
each also accepts ``model_override``/``provider_override``, which
``kanban_db.set_model_override`` persists as the raw provider+model the
dispatched worker runs against. Selecting a raw engine identifier is
``engine:select`` authority (the same act ``POST /api/model/set`` is held to),
and the route table cannot carve one optional sub-field out of an otherwise
user-owned mutation. So the handlers refuse it directly, before any DB write,
exactly as ``web_server.update_config`` refuses an engine-binding move inside
``PUT /api/config``.

These tests call the handlers the way the config-refusal tests call
``update_config``: with a fabricated request whose principal holds exactly the
scopes under test, on a hosted (``auth_required``) bind. The mutation test at the
bottom deletes the enforcement in the real plugin file, in a subprocess, and
proves a named test goes red — then restores it and proves it goes green.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli.authz import ENGINE_SELECT, PROVIDER_WRITE, Principal, Role

REPO = Path(__file__).resolve().parents[2]
PLUGIN_FILE = REPO / "plugins" / "kanban" / "dashboard" / "plugin_api.py"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    c = kb.connect()
    yield c
    c.close()


def _plugin():
    """Load the plugin module fresh from disk so a file mutation is visible."""
    spec = importlib.util.spec_from_file_location(
        "youtab_dashboard_plugin_kanban_engine_override_test", PLUGIN_FILE,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _request(*scopes):
    """A request whose principal holds exactly ``scopes``, on a hosted bind.

    ``auth_required=True`` is the hosted case; without it every caller resolves
    to the local Owner in ``_principal_for_request`` and the guard is untestable.
    ``token_authenticated`` + ``token_principal`` is the non-interactive seam the
    resolver reads first.
    """
    class _S:
        pass

    app_state, req_state = _S(), _S()
    app_state.auth_required = True
    req_state.token_authenticated = True
    req_state.token_principal = Principal(
        user_id="someone", org_id="acme", role=Role.NORMAL_USER,
        scopes=frozenset(scopes),
    )
    request = _S()
    request.app = type("_App", (), {"state": app_state})()
    request.state = req_state
    return request


# ---------------------------------------------------------------------------
# The predicate: set vs clear
# ---------------------------------------------------------------------------


class TestSelectionPredicate:
    """Only *selecting* a non-empty override is engine authority; clearing is not."""

    def _body(self, **kw):
        return type("_B", (), kw)()

    def test_a_non_empty_model_is_a_selection(self):
        p = _plugin()
        assert p._is_engine_override_selection(
            self._body(model_override="gpt-5", provider_override=None))

    def test_a_provider_alone_is_treated_as_a_selection(self):
        p = _plugin()
        assert p._is_engine_override_selection(
            self._body(model_override=None, provider_override="openai",
                       clear_model_override=False))

    def test_an_empty_model_is_a_clear_not_a_selection(self):
        p = _plugin()
        assert not p._is_engine_override_selection(
            self._body(model_override="", provider_override=None,
                       clear_model_override=False))

    def test_an_explicit_clear_is_not_a_selection(self):
        p = _plugin()
        assert not p._is_engine_override_selection(
            self._body(model_override=None, provider_override=None,
                       clear_model_override=True))

    def test_a_write_with_no_override_fields_is_not_a_selection(self):
        p = _plugin()
        assert not p._is_engine_override_selection(self._body(title="t"))


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


class TestCreateTaskEngineOverrideGuard:
    def test_a_normal_user_supplying_an_override_is_refused(self, kanban_home):
        # Named target of the mutation harness.
        p = _plugin()
        payload = p.CreateTaskBody(title="t", model_override="gpt-5",
                                   provider_override="openai")
        with pytest.raises(HTTPException) as ei:
            p.create_task(_request(), payload, board=None)
        assert ei.value.status_code == 403

    def test_a_normal_user_supplying_a_provider_alone_is_refused(self, kanban_home):
        p = _plugin()
        payload = p.CreateTaskBody(title="t", provider_override="openai")
        with pytest.raises(HTTPException) as ei:
            p.create_task(_request(), payload, board=None)
        assert ei.value.status_code == 403

    def test_the_refusal_lands_before_the_task_is_written(self, kanban_home, conn):
        p = _plugin()
        before = len(kb.list_tasks(conn))
        payload = p.CreateTaskBody(title="t", model_override="gpt-5")
        with pytest.raises(HTTPException):
            p.create_task(_request(), payload, board=None)
        assert len(kb.list_tasks(conn)) == before, "a refused create still wrote a task"

    def test_an_engine_select_holder_may_set_the_override(self, kanban_home):
        p = _plugin()
        payload = p.CreateTaskBody(title="t", model_override="gpt-5",
                                   provider_override="openai")
        result = p.create_task(_request(ENGINE_SELECT), payload, board=None)
        assert result["task"]["model_override"] == "gpt-5"
        assert result["task"]["provider_override"] == "openai"

    def test_provider_write_also_satisfies_the_guard(self, kanban_home):
        p = _plugin()
        payload = p.CreateTaskBody(title="t", model_override="gpt-5")
        result = p.create_task(_request(PROVIDER_WRITE), payload, board=None)
        assert result["task"]["model_override"] == "gpt-5"

    def test_a_normal_user_may_create_a_task_without_an_override(self, kanban_home):
        p = _plugin()
        payload = p.CreateTaskBody(title="plain task")
        result = p.create_task(_request(), payload, board=None)
        assert result["task"]["title"] == "plain task"
        assert result["task"]["model_override"] in (None, "")


# ---------------------------------------------------------------------------
# update_task (PATCH) — set vs clear
# ---------------------------------------------------------------------------


class TestUpdateTaskEngineOverrideGuard:
    def test_a_normal_user_setting_an_override_is_refused(self, kanban_home, conn):
        p = _plugin()
        tid = kb.create_task(conn, title="t")
        payload = p.UpdateTaskBody(model_override="gpt-5", provider_override="openai")
        with pytest.raises(HTTPException) as ei:
            p.update_task(_request(), tid, payload, board=None)
        assert ei.value.status_code == 403

    def test_an_engine_select_holder_may_set_the_override(self, kanban_home, conn):
        p = _plugin()
        tid = kb.create_task(conn, title="t")
        payload = p.UpdateTaskBody(model_override="gpt-5", provider_override="openai")
        result = p.update_task(_request(ENGINE_SELECT), tid, payload, board=None)
        assert result["task"]["model_override"] == "gpt-5"

    def test_a_normal_user_may_clear_their_own_override(self, kanban_home, conn):
        """Clearing is not selecting an engine; the guard must not over-reach."""
        p = _plugin()
        tid = kb.create_task(conn, title="t")
        kb.set_model_override(conn, tid, "gpt-5", provider="openai")
        payload = p.UpdateTaskBody(clear_model_override=True)
        result = p.update_task(_request(), tid, payload, board=None)
        assert result["task"]["model_override"] in (None, "")

    def test_a_normal_user_may_empty_string_clear_their_own_override(self, kanban_home, conn):
        p = _plugin()
        tid = kb.create_task(conn, title="t")
        kb.set_model_override(conn, tid, "gpt-5", provider="openai")
        payload = p.UpdateTaskBody(model_override="")
        result = p.update_task(_request(), tid, payload, board=None)
        assert result["task"]["model_override"] in (None, "")

    def test_a_normal_user_may_edit_a_task_without_touching_the_override(self, kanban_home, conn):
        p = _plugin()
        tid = kb.create_task(conn, title="t")
        payload = p.UpdateTaskBody(title="renamed")
        result = p.update_task(_request(), tid, payload, board=None)
        assert result["task"]["title"] == "renamed"


# ---------------------------------------------------------------------------
# bulk_update
# ---------------------------------------------------------------------------


class TestBulkUpdateEngineOverrideGuard:
    def test_a_normal_user_bulk_setting_an_override_is_refused(self, kanban_home, conn):
        p = _plugin()
        tid = kb.create_task(conn, title="t")
        payload = p.BulkTaskBody(ids=[tid], model_override="gpt-5",
                                 provider_override="openai")
        with pytest.raises(HTTPException) as ei:
            p.bulk_update(_request(), payload, board=None)
        assert ei.value.status_code == 403

    def test_an_engine_select_holder_may_bulk_set_the_override(self, kanban_home, conn):
        p = _plugin()
        tid = kb.create_task(conn, title="t")
        payload = p.BulkTaskBody(ids=[tid], model_override="gpt-5")
        result = p.bulk_update(_request(ENGINE_SELECT), payload, board=None)
        assert result["results"][0]["ok"] is True
        assert kb.get_task(conn, tid).model_override == "gpt-5"

    def test_a_normal_user_may_bulk_edit_without_an_override(self, kanban_home, conn):
        p = _plugin()
        tid = kb.create_task(conn, title="t")
        payload = p.BulkTaskBody(ids=[tid], priority=5)
        result = p.bulk_update(_request(), payload, board=None)
        assert result["results"][0]["ok"] is True


# ---------------------------------------------------------------------------
# Mutation: delete the enforcement in the real file, prove a named test flips
# ---------------------------------------------------------------------------

#: The guard's decision line — unique in the plugin file.
_GUARD_LINE = "if not (caller.has(ENGINE_SELECT) or caller.has(PROVIDER_WRITE)):"

#: The named test that must go red when the guard stops refusing.
_NAMED = (
    "test_kanban_engine_override_authz.py::"
    "TestCreateTaskEngineOverrideGuard::"
    "test_a_normal_user_supplying_an_override_is_refused"
)


def _child_env():
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    return env


def _run_named():
    return subprocess.run(
        [sys.executable, "-m", "pytest", _NAMED, "-q", "-p", "no:cacheprovider"],
        cwd=str(Path(__file__).parent),
        env=_child_env(),
        capture_output=True,
        text=True,
    )


def _disable_guard(text: str) -> str:
    """Neuter the guard's refusal while keeping the file syntactically valid.

    ``if not (...)`` becomes ``if False and not (...)`` — always false, so the
    ``raise`` never runs and a normal user is admitted. One anchored line.
    """
    lines = text.splitlines(keepends=True)
    matched = [i for i, ln in enumerate(lines) if ln.strip() == _GUARD_LINE]
    assert len(matched) == 1, (
        f"expected exactly one guard line; anchor drifted (found {len(matched)})"
    )
    lines[matched[0]] = lines[matched[0]].replace(
        "if not (", "if False and not (", 1)
    return "".join(lines)


def test_deleting_the_handler_guard_turns_a_named_test_red():
    """Byte-exact backup, disable the guard, subprocess pytest, restore.

    Proves the in-handler refusal is load-bearing: with it neutered the named
    negative test goes RED (a normal user's override is accepted), and with it
    restored the same test goes GREEN.
    """
    original = PLUGIN_FILE.read_bytes()
    try:
        baseline = _run_named()
        assert baseline.returncode == 0, (
            "named test was not green before mutation:\n"
            + baseline.stdout + baseline.stderr
        )

        PLUGIN_FILE.write_bytes(_disable_guard(original.decode("utf-8")).encode("utf-8"))

        red = _run_named()
        assert red.returncode != 0, (
            "disabling the handler guard did NOT turn the named test red — the "
            "guard is not load-bearing:\n" + red.stdout + red.stderr
        )
    finally:
        PLUGIN_FILE.write_bytes(original)

    green = _run_named()
    assert green.returncode == 0, (
        "restoring the guard did not return the named test to green:\n"
        + green.stdout + green.stderr
    )
