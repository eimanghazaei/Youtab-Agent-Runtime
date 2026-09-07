"""Ingress→worker capability-manifest binding (WAVE-30H correction 2).

Proves the registry snapshot + the ingress/worker binding helper deliver the
end-to-end property: the ``"*"`` full-envelope freezes the tools that existed and
were authorized at admission, a tool registered AFTER is never swept in, and the
persisted manifest reconstructs byte-identically in the worker (with tamper
detection).
"""

from __future__ import annotations

import json

import pytest

from tools.registry import ToolRegistry, ToolSpec, schema_hash
from youtab_agent_cli import capability_manifest as cm
from youtab_runtime.policy import AuthorityBoundary, EffectClass, ToolIntent

from tests.youtab_runtime.helpers import keypair, signed_envelope


def _handler(args, **kwargs):
    return json.dumps({"ok": True})


def _spec(name, toolset, **over):
    base = dict(
        name=name,
        toolset=toolset,
        schema={"name": name, "description": f"{name} tool"},
        handler=_handler,
        provenance="test",
        version="1.0.0",
    )
    base.update(over)
    return ToolSpec(**base)


def _reg_with_tools():
    reg = ToolRegistry()
    reg.register_spec(_spec("read_file", "safe"))
    reg.register_spec(_spec("web_search", "safe"))
    reg.register_spec(_spec("grant_authority", "effect_authority"))  # authority-bearing
    return reg


# ── registry snapshot ────────────────────────────────────────────────────────


def test_wildcard_manifest_excludes_authority_bearing_toolsets():
    reg = _reg_with_tools()
    pairs = reg.capability_manifest_pairs(
        {"*"}, exclude_toolsets=AuthorityBoundary.FORBIDDEN_TOOLSETS
    )
    names = {n for n, _ in pairs}
    assert names == {"read_file", "web_search"}  # grant_authority excluded
    # schema hashes present + correct
    by = dict(pairs)
    assert by["read_file"] == schema_hash({"name": "read_file", "description": "read_file tool"})


def test_named_toolset_manifest_is_scoped():
    reg = _reg_with_tools()
    reg.register_spec(_spec("draw", "canvas"))
    pairs = reg.capability_manifest_pairs({"canvas"})
    assert {n for n, _ in pairs} == {"draw"}


def test_acl_narrows_the_manifest():
    reg = _reg_with_tools()
    pairs = reg.capability_manifest_pairs({"*"}, acl_tool_names={"read_file"})
    assert {n for n, _ in pairs} == {"read_file"}


def test_unavailable_tool_is_excluded_from_manifest():
    reg = ToolRegistry()
    reg.register_spec(_spec("up", "safe", check_fn=lambda: True))
    reg.register_spec(_spec("down", "safe", check_fn=lambda: False))
    pairs = reg.capability_manifest_pairs({"*"})
    assert {n for n, _ in pairs} == {"up"}


# ── end-to-end: newly registered tool is not swept in by "*" ─────────────────


def test_new_tool_registered_after_admission_is_denied_under_star():
    reg = _reg_with_tools()
    private, public = keypair()
    envelope = signed_envelope(private, allowed_toolsets=("*",))

    # ingress freezes the manifest, seals it into the admitted context
    binding = cm.build_ingress_binding(envelope, registry=reg)
    boundary = AuthorityBoundary()
    admitted = boundary.admit(envelope, public, capability_binding=binding)

    # a tool registered AFTER admission
    reg.register_spec(_spec("late_tool", "safe"))

    # even under "*", the late tool is not authorized...
    late = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name="late_tool",
            toolset="safe",
            effect_class=EffectClass.READ,
            arguments={},
        ),
    )
    assert late.execute_in_runtime is False
    # ...but a tool that WAS in the frozen manifest still is
    early = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name="read_file",
            toolset="safe",
            effect_class=EffectClass.READ,
            arguments={},
        ),
    )
    assert early.execute_in_runtime is True


# ── persistence round-trip + tamper detection ───────────────────────────────


def test_persisted_manifest_round_trips_to_identical_binding():
    reg = _reg_with_tools()
    private, _ = keypair()
    envelope = signed_envelope(private, allowed_toolsets=("*",))
    binding = cm.build_ingress_binding(envelope, registry=reg)

    persisted = cm.binding_to_persisted(binding)
    rebuilt = cm.binding_from_persisted(persisted)
    assert rebuilt.manifest_hash == binding.manifest_hash
    assert rebuilt.tool_hashes == binding.tool_hashes


def test_tampered_persisted_manifest_is_rejected():
    reg = _reg_with_tools()
    private, _ = keypair()
    envelope = signed_envelope(private, allowed_toolsets=("*",))
    persisted = cm.binding_to_persisted(cm.build_ingress_binding(envelope, registry=reg))

    # An attacker adds a tool to the persisted list but cannot fix the bound hash.
    persisted["tool_hashes"].append(["exfiltrate", "deadbeef"])
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        cm.binding_from_persisted(persisted)
