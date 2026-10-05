"""The shared auxiliary catalog preserves CLI menus and plugin reservations."""

import builtins

import pytest

from youtab_agent_cli.auxiliary_catalog import AUX_TASKS
from youtab_agent_cli.plugins import PluginContext, PluginManager, PluginManifest


def test_cli_keeps_same_catalog_object_and_picker_order():
    from youtab_agent_cli import main

    assert main._AUX_TASKS is AUX_TASKS
    assert main._all_aux_tasks()[:len(AUX_TASKS)] == AUX_TASKS


def test_registration_uses_shared_reservations_without_importing_main(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "youtab_agent_cli.main":
            pytest.fail("Plugin auxiliary registration imported the main CLI")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(PluginManifest(name="synthetic_plugin"), manager)
    for key, display_name, description in AUX_TASKS:
        with pytest.raises(ValueError, match="reserved for a built-in task"):
            context.register_auxiliary_task(key, display_name=display_name, description=description)
    context.register_auxiliary_task("synthetic_plugin_review", display_name="Review", description="Synthetic task")
    assert manager._aux_tasks["synthetic_plugin_review"]["display_name"] == "Review"
