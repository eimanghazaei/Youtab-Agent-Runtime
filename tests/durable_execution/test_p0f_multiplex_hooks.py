"""P0-F BASELINE CHARACTERIZATION — gateway multiplex hook inertness (security).

SEPARATE security/lifecycle concern from durable run storage — must land as its
own small commit, never mixed with task-journal changes.

Defect [C-3.4]: gateway startup registers shell hooks exactly once from the
DEFAULT profile's config (`gateway/run.py:~10499` calls
`register_from_config(load_config(), ...)`), into a process-global plugin manager
(`agent/shell_hooks.py:246-278`). Secondary profiles' `hooks:` — including
tool-gating SECURITY hooks — are never registered and run inert; the omission is
silent.

This test reproduces the base behavior at the registration seam:
- the default profile's hook IS registered;
- a secondary profile's hook is NOT registered (inert);
- no error is raised (silent).

Candidate (future, separate commit) must prove: each profile registers only its
own hook set; no cross-profile leakage; hook failure is visible; single-profile
default behavior is unchanged.
"""

from pathlib import Path

import pytest

from agent import shell_hooks


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".youtab-agent-runtime"
    h.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    shell_hooks.reset_for_tests()
    yield h
    shell_hooks.reset_for_tests()


def _cfg(command: str, matcher: str):
    return {"hooks": {"pre_tool_call": [{"matcher": matcher, "command": command}]}}


def test_secondary_profile_security_hook_is_inert(home, tmp_path):
    default_cmd = str(tmp_path / "default_security_hook.sh")
    secondary_cmd = str(tmp_path / "secondary_security_hook.sh")
    default_cfg = _cfg(default_cmd, "terminal")
    secondary_cfg = _cfg(secondary_cmd, "file")

    # Sanity: both configs parse to real hook specs.
    default_spec = shell_hooks.iter_configured_hooks(default_cfg)[0]
    secondary_spec = shell_hooks.iter_configured_hooks(secondary_cfg)[0]

    # CURRENT gateway startup behavior: register ONLY the default profile's
    # config (mirrors run.py calling register_from_config(load_config())).
    # No exception is raised — the omission of secondary profiles is silent.
    registered = shell_hooks.register_from_config(default_cfg, accept_hooks=True)

    default_key = (default_spec.event, default_spec.matcher, default_spec.command)
    secondary_key = (secondary_spec.event, secondary_spec.matcher, secondary_spec.command)

    # Default profile's hook IS wired up...
    assert default_key in shell_hooks._registered
    assert any(s.command == default_cmd for s in registered)

    # ...but the secondary profile's SECURITY hook is NEVER registered -> inert.
    assert secondary_key not in shell_hooks._registered, (
        "DEFECT REPRODUCED: secondary profile's security hook is inert — the "
        "single-startup, process-global registration never sees other profiles"
    )

    # It is silent: registering the default did not raise for the missing one.
    from youtab_agent_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    wired_commands = [
        getattr(cb, "__self__", None) for cb in manager._hooks.get("pre_tool_call", [])
    ]
    # The secondary command string is absent from any wired callback closure.
    assert all(secondary_cmd not in repr(cb) for cb in manager._hooks.get("pre_tool_call", []))
    _ = wired_commands
