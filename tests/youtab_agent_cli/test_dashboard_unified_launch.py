"""Tests for the unified profile→machine dashboard launch routing.

`<profile> dashboard` routes to ONE machine-level dashboard instead of
spawning a per-profile server: attach (open browser at ?profile=) when one
is already listening, else re-exec as the machine dashboard with the
launching profile preselected. `--isolated` opts out.
"""
import sys
import types
import pytest


@pytest.fixture
def main_mod():
    import youtab_agent_cli.main as main_mod
    return main_mod


def _args(**kw):
    defaults = dict(
        status=False, stop=False, host="127.0.0.1", port=9119,
        no_open=True, insecure=False, skip_build=False,
        isolated=False, open_profile="",
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


class TestReexecPrimitiveSelection:
    """Which hand-over primitive each platform uses.

    Driven through ``_reexec_machine_dashboard`` so both branches are checked
    on every host: the rule is a one-line platform decision, and asserting it
    only on the platform you happen to be running is how the Windows branch
    went uncovered while the test that should have caught it spawned a real
    build instead.
    """

    def test_windows_waits_on_an_explicit_child(self, main_mod, monkeypatch):
        """`os.execvpe` can crash with 0xC0000005 on Windows under 3.14+."""
        monkeypatch.setattr(main_mod.sys, "platform", "win32")
        spawned = []
        monkeypatch.setattr(
            main_mod.os, "execvpe",
            lambda *a, **k: pytest.fail("execvpe must not be used on Windows"),
        )

        class _Proc:
            def wait(self):
                return 0

        def fake_popen(argv, env=None, **kwargs):
            spawned.append((argv, env))
            return _Proc()

        monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)

        with pytest.raises(SystemExit) as exc:
            main_mod._reexec_machine_dashboard(["py", "-m", "x"], {"K": "V"})

        assert exc.value.code == 0
        assert spawned == [(["py", "-m", "x"], {"K": "V"})]

    def test_posix_replaces_the_process(self, main_mod, monkeypatch):
        monkeypatch.setattr(main_mod.sys, "platform", "linux")
        execs = []
        monkeypatch.setattr(
            main_mod.subprocess, "Popen",
            lambda *a, **k: pytest.fail("Popen must not be used on POSIX"),
        )

        def fake_exec(exe, argv, env):
            execs.append((exe, argv, env))
            raise SystemExit(0)

        monkeypatch.setattr(main_mod.os, "execvpe", fake_exec)

        with pytest.raises(SystemExit):
            main_mod._reexec_machine_dashboard(["py", "-m", "x"], {"K": "V"})

        assert execs == [(sys.executable, ["py", "-m", "x"], {"K": "V"})]

    def test_the_windows_child_exit_code_is_propagated(self, main_mod, monkeypatch):
        """A failed re-exec must not read as a clean dashboard exit."""
        monkeypatch.setattr(main_mod.sys, "platform", "win32")

        class _Proc:
            def wait(self):
                return 3

        monkeypatch.setattr(main_mod.subprocess, "Popen", lambda *a, **k: _Proc())

        with pytest.raises(SystemExit) as exc:
            main_mod._reexec_machine_dashboard(["py"], {})

        assert exc.value.code == 3


class TestUnifiedDashboardRouting:


    def test_profile_launch_reexecs_machine_dashboard(self, main_mod, monkeypatch):
        """The hand-over contract: argv and env, whichever primitive is used.

        Both spawn primitives are intercepted, not just ``os.execvpe``. The
        product deliberately uses ``subprocess.Popen`` on Windows, so patching
        only ``execvpe`` left the real one live: on a Windows host this test
        used to run an actual ``vite build`` and then try to bind the dashboard
        port, failing with WinError 10048 rather than on any assertion.
        Whichever branch this host takes, nothing is spawned and the same
        argv/env contract is asserted.
        """
        monkeypatch.delenv("YOUTAB_AGENT_HOME", raising=False)
        monkeypatch.setattr(
            "youtab_agent_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        execs = []

        def fake_exec(exe, argv, env):
            execs.append((exe, argv, env))
            raise SystemExit(0)  # execvpe never returns

        def fake_popen(argv, env=None, **kwargs):
            # The Windows branch passes argv positionally and waits on the
            # child; record the same tuple so one assertion covers both.
            execs.append((argv[0], argv, env))
            raise SystemExit(0)

        monkeypatch.setattr(main_mod.os, "execvpe", fake_exec)
        monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)

        with pytest.raises(SystemExit):
            main_mod.cmd_dashboard(_args())

        assert len(execs) == 1
        exe, argv, env = execs[0]
        assert exe == sys.executable
        # Pinned to the default profile + launching profile preselected.
        assert "-p" in argv and argv[argv.index("-p") + 1] == "default"
        assert "--open-profile" in argv
        assert argv[argv.index("--open-profile") + 1] == "worker_x"
        # The child is pinned to the machine ROOT, not the launching profile's
        # YOUTAB_AGENT_HOME.  For a standard install (YOUTAB_AGENT_HOME unset) that root is
        # the platform-native default (~/.youtab-agent-runtime), NOT dropped — see the Docker
        # test below for why we resolve explicitly instead of popping.
        from youtab_constants import get_default_youtab_root
        assert env.get("YOUTAB_AGENT_HOME") == str(get_default_youtab_root())


    def test_desktop_profile_backend_skips_machine_dashboard_reroute(self, main_mod, monkeypatch):
        """A desktop-spawned named-profile backend (YOUTAB_AGENT_DESKTOP=1) must NOT
        reroute into the machine dashboard. The reroute re-execs as the default
        profile and exits, so the desktop never sees a ready backend → boot
        loop. The guard keeps desktop pool backends per-profile."""
        monkeypatch.setenv("YOUTAB_AGENT_DESKTOP", "1")
        monkeypatch.setattr(
            "youtab_agent_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        listening_calls = []
        monkeypatch.setattr(
            main_mod, "_dashboard_listening",
            lambda host, port: listening_calls.append(1) or False,
        )
        execs = []
        monkeypatch.setattr(main_mod.os, "execvpe", lambda *a, **k: execs.append(a))
        monkeypatch.setitem(sys.modules, "fastapi", None)

        with pytest.raises((SystemExit, AttributeError, ImportError, TypeError)):
            main_mod.cmd_dashboard(_args())
        assert listening_calls == []
        assert execs == []




