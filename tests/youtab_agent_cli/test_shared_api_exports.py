"""Compatibility APIs retain callable identity and their wildcard surface."""
import importlib

import pytest


@pytest.mark.parametrize("module_name", [
    "agent.bedrock_adapter", "youtab_agent_cli.auth", "youtab_agent_cli.config",
    "youtab_agent_cli.model_switch", "youtab_agent_cli.models",
    "youtab_agent_cli.route_identity",
])
def test_export_contract_preserves_existing_public_bindings(module_name):
    module = importlib.import_module(module_name)
    namespace = {}
    exec(f"from {module_name} import *", namespace)
    for name, value in vars(module).items():
        if not name.startswith("_"):
            assert namespace[name] is value


@pytest.mark.parametrize("module_name, public_name, leaf_name, leaf_module", [
    ("agent.bedrock_adapter", "reset_discovery_cache", "reset_discovery_cache", "agent.bedrock_catalog"),
    ("agent.bedrock_adapter", "_extract_provider_from_arn", "_extract_provider_from_arn", "agent.bedrock_catalog"),
    ("youtab_agent_cli.auth", "fetch_youtab_models", "fetch_youtab_models", "youtab_agent_cli.youtab_model_catalog"),
    ("youtab_agent_cli.config", "stamp_install_method", "stamp_install_method", "youtab_agent_cli.install_method"),
    ("youtab_agent_cli.config", "format_docker_update_message", "format_docker_update_message", "youtab_agent_cli.install_method"),
    ("youtab_agent_cli.model_switch", "ModelIdentity", "ModelIdentity", "youtab_agent_cli.model_alias_catalog"),
    ("youtab_agent_cli.models", "_format_price_per_mtok", "_format_price_per_mtok", "youtab_agent_cli.model_pricing"),
    ("youtab_agent_cli.models", "compute_sale_discount", "compute_sale_discount", "youtab_agent_cli.model_pricing"),
    ("youtab_agent_cli.route_identity", "normalize_route_base_url", "normalize_route_base_url", "youtab_agent_cli.route_url"),
])
def test_retained_shared_exports_keep_identity(module_name, public_name, leaf_name, leaf_module):
    module = importlib.import_module(module_name)
    leaf = importlib.import_module(leaf_module)
    assert public_name in module.__all__
    assert getattr(module, public_name) is getattr(leaf, leaf_name)
