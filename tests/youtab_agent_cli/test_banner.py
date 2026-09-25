"""Tests for banner toolset name normalization and skin color usage."""

import hashlib
from unittest.mock import patch

from rich.console import Console

import youtab_agent_cli.banner as banner
import model_tools
import tools.mcp_tool


def test_cprint_falls_back_to_plain_print_when_prompt_toolkit_has_no_console(capsys):
    with patch(
        "prompt_toolkit.print_formatted_text",
        side_effect=RuntimeError("no console screen buffer"),
    ):
        banner.cprint("fallback text")

    assert capsys.readouterr().out == "fallback text\n"


def test_banner_hides_internal_skill_categories_without_disabling_them():
    """Internal skill groups stay available but are omitted from the banner."""
    catalog = {
        "autonomous-ai-agents": ["claude-code", "codex"],
        "email": ["himalaya"],
        "software-development": ["dogfood"],
        "creative": ["architecture-diagram"],
    }

    visible = banner._get_banner_visible_skills(catalog)

    assert visible == {"creative": ["architecture-diagram"]}
    assert catalog["autonomous-ai-agents"] == ["claude-code", "codex"]


def test_default_banner_uses_exact_youtab_logo_asset_and_ocean_blue_branding():
    """The official PNG is bundled byte-for-byte; guessed logo art must not return."""
    logo_bytes = banner.YOUTAB_LOGO_ASSET.read_bytes()
    git_blob = hashlib.sha1(
        f"blob {len(logo_bytes)}\0".encode() + logo_bytes,
        usedforsecurity=False,
    ).hexdigest()

    assert banner.YOUTAB_LOGO_ASSET.name == "Youtab_AI_COS.PNG"
    assert git_blob == "a4d0e65fbc7fe8789eaee2d9f1eca35feb4ed9c2"
    assert banner.YOUTAB_RUNTIME_LOGO == "[bold #0096FF]Youtab RunTime[/]"
    assert banner.YOUTAB_LOGO_HERO == ""
    assert banner._render_youtab_logo(48).plain.strip()
    assert banner.format_banner_version_label().startswith("Youtab RunTime v")








def test_build_welcome_banner_title_falls_back_when_no_tag():
    """Without a resolvable tag, the panel title renders as plain text (no hyperlink escape)."""
    import io
    from unittest.mock import patch as _patch
    import youtab_agent_cli.banner as _banner
    import model_tools as _mt
    import tools.mcp_tool as _mcp

    _banner._latest_release_cache = None
    buf = io.StringIO()
    with (
        _patch.object(_mt, "check_tool_availability", return_value=(["web"], [])),
        _patch.object(_banner, "get_available_skills", return_value={}),
        _patch.object(_banner, "get_update_result", return_value=None),
        _patch.object(_mcp, "get_mcp_status", return_value=[]),
        _patch.object(_banner, "get_latest_release_tag", return_value=None),
    ):
        console = Console(file=buf, force_terminal=True, color_system="truecolor", width=160)
        _banner.build_welcome_banner(
            console=console, model="x", cwd="/tmp",
            session_id="abc123",
            tools=[{"function": {"name": "read_file"}}],
            get_toolset_for_tool=lambda n: "file",
        )

    raw = buf.getvalue()
    assert "Youtab RunTime v" in raw, "Version label missing from title"
    assert "\x1b]8;" not in raw, "OSC-8 hyperlink should not be emitted without a tag"






def test_build_welcome_banner_non_moa_unchanged(tmp_path, monkeypatch):
    """A normal provider still renders the bare model slug, no MoA prefix."""
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path / ".youtab-agent-runtime"))
    (tmp_path / ".youtab-agent-runtime").mkdir()

    with (
        patch.object(model_tools, "check_tool_availability", return_value=([], [])),
        patch.object(banner, "get_available_skills", return_value={}),
        patch.object(banner, "get_update_result", return_value=None),
        patch.object(tools.mcp_tool, "get_mcp_status", return_value=[]),
    ):
        console = Console(record=True, force_terminal=False, color_system=None, width=160)
        banner.build_welcome_banner(
            console=console,
            model="anthropic/claude-opus-4.8",
            cwd="/tmp/project",
            tools=[],
            enabled_toolsets=[],
            provider="openrouter",
        )

    out = console.export_text()
    assert "claude-opus-4.8" in out
    assert "MoA:" not in out
