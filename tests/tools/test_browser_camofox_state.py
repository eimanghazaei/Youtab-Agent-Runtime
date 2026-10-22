"""Tests for Youtab-managed Camofox state helpers."""

from unittest.mock import patch


def _load_module():
    from tools import browser_camofox_state as state
    return state


class TestCamofoxStatePaths:
    def test_paths_are_profile_scoped(self, tmp_path):
        state = _load_module()
        with patch.object(state, "get_youtab_home", return_value=tmp_path):
            assert state.get_camofox_state_dir() == tmp_path / "browser_auth" / "camofox"


class TestCamofoxIdentity:
    def test_identity_is_deterministic(self, tmp_path):
        state = _load_module()
        with patch.object(state, "get_youtab_home", return_value=tmp_path):
            first = state.get_camofox_identity("task-1")
            second = state.get_camofox_identity("task-1")
            assert first == second


    def test_default_task_id(self, tmp_path):
        state = _load_module()
        with patch.object(state, "get_youtab_home", return_value=tmp_path):
            identity = state.get_camofox_identity()
            assert "user_id" in identity
            assert "session_key" in identity
            assert identity["user_id"].startswith("youtab_")
            assert identity["session_key"].startswith("task_")


class TestCamofoxPrincipalIsolation:
    """Two principals must never share one persistent browser profile.

    The Youtab home is scoped by AGENT PROFILE, not by tenant, so without this
    scoping every tenant running the same agent resolved the same
    ``browser_auth/camofox`` directory. Enabling
    ``browser.camofox.managed_persistence`` would then have put one user's
    logged-in cookies in the profile the next user's run adopts.
    """

    @staticmethod
    def _as_principal(monkeypatch, tenant, user):
        for name, value in (("YOUTAB_AGENT_TENANT", tenant),
                            ("YOUTAB_AGENT_KANBAN_CREATED_BY", user)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    def test_two_principals_get_different_state_dirs(self, tmp_path, monkeypatch):
        state = _load_module()
        with patch.object(state, "get_youtab_home", return_value=tmp_path):
            self._as_principal(monkeypatch, "org-A", "user-1")
            first = state.get_camofox_state_dir()
            self._as_principal(monkeypatch, "org-B", "user-2")
            second = state.get_camofox_state_dir()
        assert first != second
        # Neither may be an ancestor of the other: a shared parent would still
        # let one run reach the other's profile directory.
        assert first not in second.parents and second not in first.parents

    def test_two_users_in_one_tenant_are_also_isolated(self, tmp_path, monkeypatch):
        """Colleagues must not inherit each other's logged-in sessions either."""
        state = _load_module()
        with patch.object(state, "get_youtab_home", return_value=tmp_path):
            self._as_principal(monkeypatch, "org-A", "user-1")
            first = state.get_camofox_identity("task-1")
            self._as_principal(monkeypatch, "org-A", "user-2")
            second = state.get_camofox_identity("task-1")
        assert first["user_id"] != second["user_id"]
        assert first["session_key"] != second["session_key"]

    def test_same_principal_still_persists_across_calls(self, tmp_path, monkeypatch):
        """Isolation must not cost persistence — the whole point of the feature."""
        state = _load_module()
        with patch.object(state, "get_youtab_home", return_value=tmp_path):
            self._as_principal(monkeypatch, "org-A", "user-1")
            first_dir = state.get_camofox_state_dir()
            first_id = state.get_camofox_identity("task-1")
            second_dir = state.get_camofox_state_dir()
            second_id = state.get_camofox_identity("task-1")
        assert first_dir == second_dir
        assert first_id == second_id

    def test_desktop_installation_path_is_unchanged(self, tmp_path, monkeypatch):
        """No principal in the environment = single-user install: keep the exact
        historical directory, so existing profiles and logins survive upgrade."""
        state = _load_module()
        self._as_principal(monkeypatch, None, None)
        with patch.object(state, "get_youtab_home", return_value=tmp_path):
            assert state.get_camofox_state_dir() == tmp_path / "browser_auth" / "camofox"

    def test_either_identifier_alone_still_scopes(self, tmp_path, monkeypatch):
        """A run carrying only one half of the principal must not fall back to
        the shared directory."""
        state = _load_module()
        shared = tmp_path / "browser_auth" / "camofox"
        with patch.object(state, "get_youtab_home", return_value=tmp_path):
            self._as_principal(monkeypatch, "org-A", None)
            tenant_only = state.get_camofox_state_dir()
            self._as_principal(monkeypatch, None, "user-1")
            user_only = state.get_camofox_state_dir()
        assert tenant_only != shared and user_only != shared
        assert tenant_only != user_only

    def test_principal_identifiers_do_not_appear_in_the_path(self, tmp_path, monkeypatch):
        """Customer identifiers must not be written into on-disk paths, which end
        up quoted in logs and crash dumps."""
        state = _load_module()
        with patch.object(state, "get_youtab_home", return_value=tmp_path):
            self._as_principal(monkeypatch, "acme-corp-secret", "alice@example.com")
            path = str(state.get_camofox_state_dir())
        assert "acme-corp-secret" not in path
        assert "alice@example.com" not in path


class TestCamofoxConfigDefaults:
    def test_default_config_includes_camofox_controls(self):
        from youtab_agent_cli.config import DEFAULT_CONFIG

        browser_cfg = DEFAULT_CONFIG["browser"]
        assert browser_cfg["camofox"]["managed_persistence"] is False
        assert browser_cfg["camofox"]["user_id"] == ""
        assert browser_cfg["camofox"]["session_key"] == ""
        assert browser_cfg["camofox"]["adopt_existing_tab"] is False
