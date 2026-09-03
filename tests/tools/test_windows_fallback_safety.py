"""Windows fallback fail-closed proofs (WAVE-25 criterion 5 / c3).

Each Windows-only fallback that WAVE-24 review flagged as "logic looks
fail-closed but is unpinned" gets an explicit WinError-injection test here, plus
the tirith unsupported-platform branch now honours ``tirith_fail_open``. All
tests force the Windows branch with ``monkeypatch.setattr(os, "name", "nt")`` so
they execute on the POSIX CI legs as well as natively on windows-latest.
"""

import os

import pytest


def _perm_error(winerror):
    exc = PermissionError(f"winerror {winerror}")
    exc.winerror = winerror
    return exc


class _ScriptedCallable:
    """Raises the scripted exception per call, else returns None (success)."""

    def __init__(self, script):
        self.script = script
        self.calls = 0

    def __call__(self, *args, **kwargs):
        i = self.calls
        self.calls += 1
        action = self.script[i] if i < len(self.script) else None
        if action is not None:
            raise action
        return None


# --------------------------------------------------------------------------- #
# utils._os_replace_resilient  (atomic_replace's Windows retry)
# --------------------------------------------------------------------------- #
class TestOsReplaceResilientWindows:
    @pytest.fixture(autouse=True)
    def _win(self, monkeypatch):
        import utils

        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(utils.time, "sleep", lambda *_: None)

    def test_transient_then_success(self, monkeypatch):
        import utils

        fake = _ScriptedCallable([_perm_error(32), _perm_error(5), None])
        monkeypatch.setattr(os, "replace", fake)
        utils._os_replace_resilient("src", "dst")  # must not raise
        assert fake.calls == 3

    def test_non_transient_raises_immediately(self, monkeypatch):
        import utils

        fake = _ScriptedCallable([_perm_error(13)])  # 13 not in {5,32}
        monkeypatch.setattr(os, "replace", fake)
        with pytest.raises(PermissionError):
            utils._os_replace_resilient("src", "dst")
        assert fake.calls == 1

    def test_exhaustion_reraises(self, monkeypatch):
        import utils

        attempts = utils._WINDOWS_REPLACE_MAX_ATTEMPTS
        fake = _ScriptedCallable([_perm_error(5)] * attempts)
        monkeypatch.setattr(os, "replace", fake)
        with pytest.raises(PermissionError):
            utils._os_replace_resilient("src", "dst")
        assert fake.calls == attempts


# --------------------------------------------------------------------------- #
# kanban_db._windows_resilient_fs_op  (remove_board rename/rmtree retry)
# --------------------------------------------------------------------------- #
class TestResilientFsOpWindows:
    @pytest.fixture(autouse=True)
    def _win(self, monkeypatch):
        from youtab_agent_cli import kanban_db

        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(kanban_db.time, "sleep", lambda *_: None)

    def test_transient_then_success(self):
        from youtab_agent_cli import kanban_db

        fake = _ScriptedCallable([_perm_error(32), _perm_error(5), None])
        kanban_db._windows_resilient_fs_op(fake)  # must not raise
        assert fake.calls == 3

    def test_non_transient_raises_immediately(self):
        from youtab_agent_cli import kanban_db

        fake = _ScriptedCallable([_perm_error(13)])
        with pytest.raises(PermissionError):
            kanban_db._windows_resilient_fs_op(fake)
        assert fake.calls == 1

    def test_exhaustion_reraises(self):
        from youtab_agent_cli import kanban_db

        fake = _ScriptedCallable([_perm_error(5)] * 12)
        with pytest.raises(PermissionError):
            kanban_db._windows_resilient_fs_op(fake)
        assert fake.calls == 12


# --------------------------------------------------------------------------- #
# tirith unsupported-platform branch honours tirith_fail_open (c3 core)
# --------------------------------------------------------------------------- #
class TestTirithUnsupportedPlatformHonoursFailOpen:
    def _cfg(self, *, fail_open):
        return {
            "tirith_enabled": True,
            "tirith_fail_open": fail_open,
            "tirith_path": "tirith",
            "tirith_timeout": 5,
        }

    def _patch(self, monkeypatch, *, fail_open):
        from tools import tirith_security

        monkeypatch.setattr(tirith_security, "is_platform_supported", lambda: False)
        monkeypatch.setattr(tirith_security, "_circuit_open", False)
        monkeypatch.setattr(
            tirith_security, "_load_security_config", lambda: self._cfg(fail_open=fail_open)
        )

    def test_fail_closed_config_blocks(self, monkeypatch):
        from tools import tirith_security

        self._patch(monkeypatch, fail_open=False)
        result = tirith_security.check_command_security("rm -rf /")
        assert result["action"] == "block"

    def test_default_fail_open_allows(self, monkeypatch):
        from tools import tirith_security

        self._patch(monkeypatch, fail_open=True)
        result = tirith_security.check_command_security("echo hi")
        assert result["action"] == "allow"
