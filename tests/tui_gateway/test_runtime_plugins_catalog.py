"""Runtime plugin catalogue identity is stable across equal display names."""

from pathlib import Path

import tui_gateway.server as server
from youtab_agent_cli import plugins_cmd


def _catalogue(monkeypatch):
    rows = [
        ("shared", "1", "Web plugin", "bundled", Path("web/shared"), "web/shared"),
        ("shared", "1", "Image plugin", "bundled", Path("image/shared"), "image/shared"),
    ]
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: rows)
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: {"image/shared"})
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", set)


def test_catalogue_returns_canonical_keys_for_duplicate_names(monkeypatch):
    _catalogue(monkeypatch)
    result = server._methods["plugins.manage"](1, {"action": "list"})["result"]
    rows = {row["key"]: row for row in result["plugins"]}
    assert set(rows) == {"web/shared", "image/shared"}
    assert rows["web/shared"]["status"] == "not enabled"
    assert rows["image/shared"]["status"] == "enabled"


def test_toggle_returns_the_plugin_selected_by_canonical_key(monkeypatch):
    _catalogue(monkeypatch)
    calls = []

    def toggle(name, *, enabled):
        calls.append((name, enabled))
        return {"ok": True, "unchanged": False}

    monkeypatch.setattr(plugins_cmd, "dashboard_set_agent_plugin_enabled", toggle)
    result = server._methods["plugins.manage"](1, {
        "action": "toggle", "name": "image/shared", "enable": True,
    })["result"]
    assert calls == [("image/shared", True)]
    assert result["plugin"]["key"] == "image/shared"
