"""Regression tests for #34107 — Docker UID/GID handling in ensure_youtab_home.

When Youtab runs in Docker with ``YOUTAB_AGENT_UID=1000`` / ``YOUTAB_AGENT_GID=911``,
the entrypoint chowns the top-level ``YOUTAB_AGENT_HOME`` once at startup. But
subdirectories created at runtime by ``ensure_youtab_home()`` — especially
for profile namespaces under ``profiles/<name>/`` spawned by kanban
workers — were landing as ``root:root`` and blocking subsequent
uid-mapped worker invocations with ``PermissionError [Errno 13]``.

The fix is a ``_chown_to_youtab_uid`` helper that reads the env vars and
applies chown after ``mkdir``, invoked from ``_secure_dir`` (which already
runs after every directory creation in the home-init path).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import _wincompat


# ---------------------------------------------------------------------------
# _resolve_youtab_uid_gid
# ---------------------------------------------------------------------------


class TestResolveYoutabUidGid:
    @_wincompat.requires_os_attr("chown")
    def test_returns_parsed_values_when_both_set(self, monkeypatch):
        monkeypatch.setenv("YOUTAB_AGENT_UID", "1000")
        monkeypatch.setenv("YOUTAB_AGENT_GID", "911")
        from youtab_agent_cli.config import _resolve_youtab_uid_gid
        uid, gid = _resolve_youtab_uid_gid()
        assert uid == 1000
        assert gid == 911


    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific")
    def test_windows_returns_none_none(self, monkeypatch):
        monkeypatch.setenv("YOUTAB_AGENT_UID", "1000")
        monkeypatch.setenv("YOUTAB_AGENT_GID", "911")
        from youtab_agent_cli.config import _resolve_youtab_uid_gid
        uid, gid = _resolve_youtab_uid_gid()
        assert uid is None
        assert gid is None


# ---------------------------------------------------------------------------
# _chown_to_youtab_uid
# ---------------------------------------------------------------------------


class TestChownToYoutabUid:
    @_wincompat.requires_os_attr("chown")
    def test_calls_os_chown_when_both_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YOUTAB_AGENT_UID", "1000")
        monkeypatch.setenv("YOUTAB_AGENT_GID", "911")
        from youtab_agent_cli import config as cfg

        d = tmp_path / "subdir"
        d.mkdir()

        with patch.object(cfg.os, "chown") as mock_chown:
            cfg._chown_to_youtab_uid(d)
        mock_chown.assert_called_once_with(d, 1000, 911)


    def test_eperm_is_silently_swallowed(self, tmp_path, monkeypatch):
        """When running as non-root, os.chown raises EPERM. That's fine —
        the entrypoint's startup chown -R will pick it up on restart, and
        in most cases the dir was already correctly-owned by the calling
        user anyway."""
        monkeypatch.setenv("YOUTAB_AGENT_UID", "1000")
        monkeypatch.setenv("YOUTAB_AGENT_GID", "911")
        from youtab_agent_cli import config as cfg

        d = tmp_path / "subdir"
        d.mkdir()

        def _raises_eperm(*args, **kwargs):
            raise PermissionError("operation not permitted")

        # create=True so the mock installs even where os.chown is absent (native
        # Windows). On POSIX this exercises the real EPERM swallow; on Windows
        # the helper early-returns (uid/gid resolve to None) so it proves the
        # portability contract: _chown_to_youtab_uid never raises.
        with patch.object(cfg.os, "chown", create=True, side_effect=_raises_eperm):
            # Must not raise — the catch is non-fatal.
            cfg._chown_to_youtab_uid(d)

    def test_attributeerror_swallowed_for_windows_compat(self, tmp_path, monkeypatch):
        """os.chown doesn't exist on Windows. Catching AttributeError keeps
        the helper portable.

        Runs on all platforms: create=True installs the mock even on native
        Windows. On POSIX the mock is reached and its AttributeError is
        swallowed by the helper; on Windows the helper early-returns before the
        chown call, so it proves the same contract — the helper never raises."""
        monkeypatch.setenv("YOUTAB_AGENT_UID", "1000")
        monkeypatch.setenv("YOUTAB_AGENT_GID", "911")
        from youtab_agent_cli import config as cfg

        d = tmp_path / "subdir"
        d.mkdir()

        with patch.object(
            cfg.os,
            "chown",
            create=True,
            side_effect=AttributeError("no chown on this platform"),
        ):
            cfg._chown_to_youtab_uid(d)  # must not raise

    @pytest.mark.skipif(
        hasattr(os, "chown"),
        reason="paired fail-closed test for platforms without os.chown (native "
        "Windows); on POSIX os.chown exists and the chown path is covered above",
    )
    def test_chown_helper_is_noop_where_os_chown_absent(self, tmp_path, monkeypatch):
        """Fail-closed proof for test_calls_os_chown_when_both_set: where
        os.chown is absent (native Windows), _chown_to_youtab_uid is a safe
        no-op even with YOUTAB_AGENT_UID/GID set — it never raises and never
        attempts a chown, because _resolve_youtab_uid_gid returns (None, None)
        on Windows (Docker uid mapping is a Linux-only concept)."""
        monkeypatch.setenv("YOUTAB_AGENT_UID", "1000")
        monkeypatch.setenv("YOUTAB_AGENT_GID", "911")
        from youtab_agent_cli import config as cfg

        d = tmp_path / "subdir"
        d.mkdir()

        attempts = {"n": 0}

        def _spy(*args, **kwargs):
            attempts["n"] += 1

        with patch.object(cfg.os, "chown", create=True, side_effect=_spy):
            cfg._chown_to_youtab_uid(d)  # must not raise

        assert attempts["n"] == 0, "chown must never be attempted on Windows"


# ---------------------------------------------------------------------------
# End-to-end: _secure_dir now also chowns
# ---------------------------------------------------------------------------


class TestSecureDirChown:
    @pytest.mark.skipif(sys.platform == "win32", reason="chown is no-op on Windows")
    def test_secure_dir_invokes_chown_when_env_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YOUTAB_AGENT_UID", "1000")
        monkeypatch.setenv("YOUTAB_AGENT_GID", "911")
        from youtab_agent_cli import config as cfg

        d = tmp_path / "subdir"
        d.mkdir()

        with patch.object(cfg.os, "chown") as mock_chown:
            cfg._secure_dir(d)
        mock_chown.assert_called_once_with(d, 1000, 911)

    @pytest.mark.skipif(sys.platform == "win32", reason="chown is no-op on Windows")
    def test_secure_dir_no_chown_when_env_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YOUTAB_AGENT_UID", raising=False)
        monkeypatch.delenv("YOUTAB_AGENT_GID", raising=False)
        from youtab_agent_cli import config as cfg

        d = tmp_path / "subdir"
        d.mkdir()

        with patch.object(cfg.os, "chown") as mock_chown:
            cfg._secure_dir(d)
        mock_chown.assert_not_called()
