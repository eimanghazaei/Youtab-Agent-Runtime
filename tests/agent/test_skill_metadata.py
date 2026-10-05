"""Metadata/config discovery contracts below runtime and tool dependencies."""

import json
import os
import subprocess
import sys
from contextvars import Context

import pytest
import yaml

from agent import skill_metadata as metadata
from agent import skill_utils


@pytest.fixture
def skill_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("YOUTAB_AGENT_PLATFORM", raising=False)
    monkeypatch.delenv("YOUTAB_AGENT_SESSION_PLATFORM", raising=False)
    metadata._external_dirs_cache_clear()
    yield home
    metadata._external_dirs_cache_clear()


def _skill(root, name, key, **fields):
    directory = root / name
    directory.mkdir(parents=True)
    frontmatter = {
        "name": directory.name,
        "metadata": {"youtab": {"config": {
            "key": key, "description": key, "default": "default-value",
        }}},
        **fields,
    }
    (directory / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump(frontmatter) + "---\nBody\n", encoding="utf-8",
    )


def test_discovery_keeps_filters_order_and_config_resolution(skill_home, tmp_path):
    skills = skill_home / "skills"
    external = tmp_path / "external"
    _skill(skills, "a-local", "wiki.path")
    _skill(skills, "hidden", "hidden.value")
    _skill(skills, "incompatible", "wrong.value", platforms=["unknown-test-os"])
    _skill(skills, "a-local/references/archived", "archived.value")
    _skill(skills, "_org/active/shared", "org.value")
    _skill(skills, "_org/stale/old", "stale.value")
    (skills / "_org/.active_org").write_text("active", encoding="utf-8")
    _skill(external, "a-duplicate", "wiki.path")
    _skill(external, "b-extra", "extra.value")
    (skill_home / "config.yaml").write_text(yaml.safe_dump({"skills": {
        "disabled": ["hidden"], "external_dirs": [str(external)],
        "config": {"wiki": {"path": "~/test-wiki"}, "extra": {"value": 0}},
    }}), encoding="utf-8")

    declarations = metadata.discover_all_skill_config_vars()
    by_key = {var["key"]: var for var in declarations}
    assert set(by_key) == {"wiki.path", "org.value", "extra.value"}
    assert by_key["wiki.path"]["skill"] == "a-local"
    assert skill_utils.discover_all_skill_config_vars() == declarations
    values = metadata.resolve_skill_config_values(declarations)
    assert values["wiki.path"] == os.path.expanduser("~/test-wiki")
    assert values["org.value"] == "default-value"
    assert values["extra.value"] == 0


def test_both_import_paths_share_config_cache(skill_home, monkeypatch):
    (skill_home / "config.yaml").write_text(
        "skills:\n  disabled: [hidden]\n  config:\n    item: configured\n",
        encoding="utf-8",
    )
    real_loader = metadata.yaml_load
    reads = []

    def loader(text):
        reads.append(text)
        return real_loader(text)

    monkeypatch.setattr(metadata, "yaml_load", loader)
    assert metadata.get_disabled_skill_names() == {"hidden"}
    assert skill_utils.resolve_skill_config_values([{"key": "item"}]) == {"item": "configured"}
    assert len(reads) == 1
    skill_utils._raw_config_cache_clear()
    metadata.resolve_skill_config_values([{"key": "item"}])
    assert len(reads) == 2


def test_gateway_facade_and_metadata_share_session_platform(skill_home, monkeypatch):
    from agent import session_context as leaf
    from gateway import session_context as facade

    (skill_home / "config.yaml").write_text(
        "skills:\n  disabled: [global]\n  platform_disabled:\n"
        "    discord: [discord-only]\n    telegram: [telegram-only]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("YOUTAB_AGENT_SESSION_PLATFORM", "telegram")
    assert facade._SESSION_PLATFORM is leaf._SESSION_PLATFORM
    assert facade._VAR_MAP is leaf._VAR_MAP

    def check():
        facade._SESSION_PLATFORM.set("discord")
        assert metadata.get_disabled_skill_names() == {"global", "discord-only"}
        assert skill_utils.get_disabled_skill_names() == {"global", "discord-only"}
        leaf._SESSION_PLATFORM.set("")
        assert facade.get_session_env("YOUTAB_AGENT_SESSION_PLATFORM") == ""
        assert metadata.get_disabled_skill_names() == {"global"}

    Context().run(check)


def test_metadata_discovery_runs_without_runtime_imports(skill_home):
    _skill(skill_home / "skills", "standalone", "example.value")
    (skill_home / "config.yaml").write_text("skills:\n  disabled: []\n", encoding="utf-8")
    # Exercise real file discovery in a fresh interpreter while denying runtime
    # imports. This catches package initializer dependencies as well as calls.
    script = '''
import importlib.abc
import json
import sys

class RuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "agent.skill_utils" or fullname.split(".")[0] in {"gateway", "tools", "youtab_agent_cli", "run_agent"}:
            raise AssertionError("Runtime import during metadata discovery: " + fullname)

sys.meta_path.insert(0, RuntimeImports())
from agent.skill_metadata import discover_all_skill_config_vars, resolve_skill_config_values
from agent.session_context import set_session_vars, clear_session_vars
tokens = set_session_vars(platform="discord")
declarations = discover_all_skill_config_vars()
print(json.dumps(resolve_skill_config_values(declarations)))
clear_session_vars(tokens)
'''
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        env=os.environ.copy(), check=True,
    )
    assert json.loads(result.stdout) == {"example.value": "default-value"}


def test_cli_missing_skill_settings_use_metadata_discovery(skill_home):
    from youtab_agent_cli.config import get_missing_skill_config_vars

    _skill(skill_home / "skills", "wiki", "wiki.path")
    (skill_home / "config.yaml").write_text("skills:\n  disabled: []\n", encoding="utf-8")
    missing = get_missing_skill_config_vars()
    assert [(var["key"], var["skill"]) for var in missing] == [("wiki.path", "wiki")]
