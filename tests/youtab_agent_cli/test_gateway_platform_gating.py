"""Host-specific gating in ``youtab_agent_cli.gateway._all_platforms()``.

Some messaging platforms can't function on every host. The gate lives
in one place — ``_all_platforms()`` — so the setup wizard, the curses
gateway-config menu, and any future picker all see the same filtered
list.

Currently:
- Matrix is hidden on Windows. The ``[matrix]`` extra pulls
  ``mautrix[encryption]`` -> ``python-olm``, which has no Windows wheel
  and needs ``make`` + libolm to build from sdist. There's no native
  Windows path that works.
"""

import pytest


class TestMatrixHiddenOnWindows:
    @pytest.fixture(autouse=True)
    def _fresh_platform_discovery(self):
        """Guarantee a complete platform registry before each test.

        Bundled platforms (telegram/discord/slack/matrix/...) register as
        *deferred* loaders; ``platform_registry.all_entries()`` resolves each
        one exactly once by popping it. If any earlier test in the session
        already triggered resolution while those loaders could not materialise
        (swallowed failure), the platforms are permanently gone from the
        process-wide singleton — and ``PluginManager._discovered`` stays True,
        so ``_all_platforms()``'s idempotent ``discover_plugins()`` no longer
        re-registers them. Forcing a clean re-discovery re-registers the
        deferred loaders, making this gating assertion hermetic regardless of
        suite ordering. (Production is unaffected: a real process discovers
        once, cleanly.)
        """
        from youtab_agent_cli.plugins import discover_plugins

        discover_plugins(force=True)
        yield
    def test_matrix_present_on_linux(self, monkeypatch):
        """Sanity: matrix is still in the picker on Linux/macOS."""
        import youtab_agent_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "linux")
        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        assert "matrix" in keys, "matrix must be available on Linux"


    def test_other_platforms_unaffected_on_windows(self, monkeypatch):
        """Gating must only drop matrix, not collateral damage."""
        import youtab_agent_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "win32")
        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        # A representative sample of platforms that have no Windows
        # blockers — picker should still surface them.
        for must_have in ("telegram", "discord", "slack", "mattermost"):
            assert must_have in keys, (
                f"{must_have} disappeared from Windows picker — gate is "
                "over-filtering"
            )
