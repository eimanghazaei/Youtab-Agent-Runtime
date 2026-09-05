"""Tests for `youtab update --yes / -y` — assume yes for interactive prompts.

Covers:
  1. argparse parses the flag
  2. Config-migration prompt is auto-answered (no input() call) and migrate_config
     runs with interactive=False so API-key prompts are skipped
  3. Autostash restore prompt is auto-answered (prompt_for_restore == False, no
     input() call) and the stash is applied automatically
"""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from youtab_agent_cli.main import cmd_update


def _make_run_side_effect(
    branch="main", verify_ok=True, commit_count="1", dirty=False
):
    """Minimal subprocess.run side_effect for the update flow."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")
        if "rev-parse" in joined and "--verify" in joined:
            return subprocess.CompletedProcess(
                cmd, 0 if verify_ok else 128, stdout="", stderr=""
            )
        if "rev-list" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{commit_count}\n", stderr=""
            )
        # `git status --porcelain` for dirty-tree detection during autostash.
        if "status" in joined and "--porcelain" in joined:
            out = " M youtab_agent_cli/main.py\n" if dirty else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        # `git stash list` — return a stash ref when dirty (so _stash_local_changes
        # gets something to return). _stash_local_changes_if_needed is what we
        # actually patch in tests that exercise restore, so this is a catch-all.
        if "stash" in joined and "list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


class TestUpdateYesConfigMigration:
    """--yes auto-answers the config-migration prompt and skips API-key prompts."""

    # These tests assert the platform-independent config-migration branch of
    # cmd_update. On native Windows the full _cmd_update_impl also runs Windows-
    # only update machinery that these tests never intended to exercise — a
    # venv-lock guard that sys.exit(2)s when any venv python is running, plus
    # pausing/refreshing/restarting real Windows gateway processes. Forcing
    # _is_windows()->False routes through the same platform-neutral path the
    # test was authored against (git update, all mocked via subprocess.run),
    # making it deterministic and side-effect-free on Windows.
    #
    # Also stub three heavy, environment-sensitive prelude steps that run BEFORE
    # the migration step regardless of platform:
    #   - _clear_bytecode_cache(PROJECT_ROOT) os.walks the whole checkout (and
    #     descends into nested dev worktrees like .claude/worktrees/, which the
    #     production prune list doesn't skip).
    #   - _build_web_ui() shells out to a real `npm install` + `npm run build`
    #     via Popen (not the mocked subprocess.run), which can take 30s+.
    #   - sync_skills() copies the whole bundled-skills tree into YOUTAB_AGENT_HOME.
    # None is the behaviour under test, so stub them to fast no-ops. Passing
    # `new=` (a lambda) means no extra mock argument is injected into the
    # test signatures.
    @patch("youtab_agent_cli.main._is_windows", lambda: False)
    @patch("tools.skills_sync.sync_skills", lambda **k: {"copied": [], "updated": [], "user_modified": [], "cleaned": []})
    @patch("youtab_agent_cli.main._build_web_ui", lambda *a, **k: True)
    @patch("youtab_agent_cli.main._clear_bytecode_cache", lambda root: 0)
    @patch("youtab_agent_cli.config.migrate_config")
    @patch("youtab_agent_cli.config.check_config_version", return_value=(1, 2))
    @patch("youtab_agent_cli.config.get_missing_config_fields", return_value=[])
    @patch("youtab_agent_cli.config.get_missing_env_vars", return_value=["NEW_KEY"])
    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_yes_auto_migrates_without_input(
        self,
        mock_run,
        _mock_which,
        _mock_missing_env,
        _mock_missing_cfg,
        _mock_version,
        mock_migrate,
        capsys,
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )
        mock_migrate.return_value = {"env_added": [], "config_added": []}

        args = SimpleNamespace(yes=True)

        with patch("builtins.input") as mock_input:
            cmd_update(args)
            # Never prompted the user.
            mock_input.assert_not_called()

        # migrate_config was invoked with interactive=False — API-key prompts
        # are suppressed, matching gateway-mode semantics.
        assert mock_migrate.call_count == 1
        _, kwargs = mock_migrate.call_args
        assert kwargs.get("interactive") is False

        out = capsys.readouterr().out
        assert "--yes: auto-applying config migration" in out
        # The "Would you like to configure them now?" prompt text never appears.
        assert "Would you like to configure them now?" not in out

    # Same platform-neutral routing + unrelated-side-effect isolation as above.
    @patch("youtab_agent_cli.main._is_windows", lambda: False)
    @patch("tools.skills_sync.sync_skills", lambda **k: {"copied": [], "updated": [], "user_modified": [], "cleaned": []})
    @patch("youtab_agent_cli.main._build_web_ui", lambda *a, **k: True)
    @patch("youtab_agent_cli.main._clear_bytecode_cache", lambda root: 0)
    @patch("youtab_agent_cli.config.migrate_config")
    @patch("youtab_agent_cli.config.check_config_version", return_value=(1, 2))
    @patch("youtab_agent_cli.config.get_missing_config_fields", return_value=[])
    @patch("youtab_agent_cli.config.get_missing_env_vars", return_value=["NEW_KEY"])
    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_no_yes_flag_still_prompts_in_tty(
        self,
        mock_run,
        _mock_which,
        _mock_missing_env,
        _mock_missing_cfg,
        _mock_version,
        mock_migrate,
        capsys,
    ):
        """Regression guard: without --yes, the TTY prompt path still fires."""
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )
        mock_migrate.return_value = {"env_added": [], "config_added": []}

        args = SimpleNamespace(yes=False)

        # Patch ``sys.stdin.isatty`` and ``sys.stdout.isatty`` directly on the
        # real ``sys`` module instead of replacing ``youtab_agent_cli.main.sys`` with
        # a MagicMock. The MagicMock approach was flaky under ``pytest-xdist``
        # — a sibling test that imported ``youtab_agent_cli.main`` first could leave
        # a different ``sys`` reference resolved inside the function and the
        # mock would never be consulted, with CI then taking the
        # "Non-interactive session" branch instead of prompting.
        import sys as _sys

        with patch("builtins.input", return_value="n") as mock_input, patch.object(
            _sys.stdin, "isatty", return_value=True
        ), patch.object(_sys.stdout, "isatty", return_value=True):
            cmd_update(args)
            # The user was actually prompted.
            assert mock_input.called
            prompts = [c.args[0] if c.args else "" for c in mock_input.call_args_list]
            assert any("configure them now" in p for p in prompts)


class TestUpdateYesStashRestore:
    """--yes auto-restores the pre-update autostash without prompting."""

