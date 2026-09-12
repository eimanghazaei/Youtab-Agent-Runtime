"""Extensible Tool Registry acceptance proof (WAVE-30H R11 / ADR-0003).

Proves the Owner's capability-preserving invariant on an isolated ToolRegistry:

  * A new ``computer_use``-class tool is registered, discovered, selected and
    invoked WITHOUT editing any core tool list or routing switch — a single
    ``register_spec`` call.
  * No fixed tool-count ceiling exists (38/61/69 or any global maximum).
  * For every registered, enabled (operational) and authorized tool:
        unexpected missing = 0, undocumented hidden = 0,
        unauthorized callable = 0, unavailable falsely advertised = 0.
  * Dynamic registration, removal, versioning, schema-hash change,
    authorization denial, unavailable providers and name collisions all behave
    fail-safe (fail closed on integrity/authorization; never silently hide an
    otherwise-authorized tool).
"""

from __future__ import annotations

import json

import pytest

from tools.registry import ToolRegistry, ToolSpec, schema_hash

COMPUTER_USE_SCHEMA = {
    "name": "computer_use",
    "description": "Synthetic computer_use-class tool: screenshot/click/type on a virtual display.",
    "parameters": {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    },
}


def _computer_use_handler(args, **kwargs):
    return json.dumps({"ok": True, "action": args.get("action")})


def _spec(**over):
    base = dict(
        name="computer_use",
        toolset="computer",
        schema=COMPUTER_USE_SCHEMA,
        handler=_computer_use_handler,
        capabilities=("gui", "vision"),
        side_effect_class="process",
        provenance="device-local",
        version="1.0.0",
    )
    base.update(over)
    return ToolSpec(**base)


def _reg():
    return ToolRegistry()


# --- headline proof -------------------------------------------------------

def test_synthetic_computer_use_register_discover_select_invoke_no_core_edits():
    reg = _reg()
    reg.register_spec(_spec())  # the ONLY call needed — no routing switch touched

    # discover (existence)
    assert "computer_use" in reg.get_all_tool_names()
    index_names = {e["name"] for e in reg.describe_index()}
    assert "computer_use" in index_names

    # select (schema retrieval, OpenAI format)
    defs = reg.get_definitions({"computer_use"})
    assert len(defs) == 1
    assert defs[0]["type"] == "function"
    assert defs[0]["function"]["name"] == "computer_use"

    # invoke (reaches the handler via pure dict dispatch)
    out = reg.dispatch("computer_use", {"action": "screenshot"})
    assert json.loads(out) == {"ok": True, "action": "screenshot"}

    # manifest carries version + schema hash
    manifest = {m["name"]: m for m in reg.tool_manifest()}
    assert manifest["computer_use"]["version"] == "1.0.0"
    assert manifest["computer_use"]["schema_hash"] == schema_hash(COMPUTER_USE_SCHEMA)


# --- Owner's enumerated behaviours ----------------------------------------

def test_dynamic_registration_grows_manifest_no_ceiling():
    reg = _reg()
    for i in range(200):  # far beyond any historical 38/61/69 snapshot
        reg.register_spec(
            _spec(name=f"tool_{i}", toolset=f"ts_{i}", schema={"name": f"tool_{i}"})
        )
    # No cap: the manifest is exactly as long as what was registered.
    assert len(reg.tool_manifest()) == 200
    assert len(reg.get_all_tool_names()) == 200


def test_removal_makes_tool_undiscoverable_and_uncallable():
    reg = _reg()
    reg.register_spec(_spec())
    reg.deregister("computer_use")
    assert "computer_use" not in reg.get_all_tool_names()
    assert reg.get_definitions({"computer_use"}) == []
    assert "Unknown tool" in reg.dispatch("computer_use", {"action": "x"})


def test_versioning_and_schema_hash_change():
    reg = _reg()
    reg.register_spec(_spec(version="1.0.0"))
    h1 = reg.get_entry("computer_use").schema_hash
    # Re-register the SAME name+toolset with a new version and changed schema.
    new_schema = {**COMPUTER_USE_SCHEMA, "description": "v2 — adds scroll"}
    reg.register_spec(_spec(version="2.0.0", schema=new_schema))
    entry = reg.get_entry("computer_use")
    assert entry.version == "2.0.0"
    assert entry.schema_hash != h1
    assert entry.schema_hash == schema_hash(new_schema)


def test_authorization_denial_visible_but_not_authorized():
    reg = _reg()
    reg.register_spec(_spec())
    # Grant envelope authorizes a different tool only.
    index = {e["name"]: e for e in reg.describe_index(authorized_names={"something_else"})}
    # Existence is STILL visible (never silently hidden) ...
    assert "computer_use" in index
    # ... but it is not advertised as authorized.
    assert index["computer_use"]["authorized"] is False


def test_unavailable_provider_not_advertised_but_existence_visible():
    reg = _reg()
    reg.register_spec(_spec(check_fn=lambda: False))  # provider unavailable
    # Not advertised as callable (absent from the model-facing definitions) ...
    assert reg.get_definitions({"computer_use"}) == []
    # ... yet its existence remains visible in the index with available=False.
    index = {e["name"]: e for e in reg.describe_index()}
    assert index["computer_use"]["available"] is False


def test_name_collision_rejected_without_override():
    reg = _reg()
    reg.register_spec(_spec(toolset="computer"))
    # Same name, DIFFERENT toolset, no override -> rejected; original preserved.
    reg.register_spec(_spec(toolset="rogue", schema={"name": "computer_use", "x": 1}))
    assert reg.get_entry("computer_use").toolset == "computer"


def test_name_collision_allowed_with_override():
    reg = _reg()
    reg.register_spec(_spec(toolset="computer"))
    reg.register_spec(_spec(toolset="replacement", override=True))
    assert reg.get_entry("computer_use").toolset == "replacement"


def test_bad_side_effect_class_is_rejected():
    with pytest.raises(ValueError):
        _spec(side_effect_class="nuclear")


# --- the four-zero invariant ----------------------------------------------

def test_four_zero_discoverability_invariant():
    reg = _reg()
    # A mix: operational+authorized, unavailable, and one authorized-only-subset.
    reg.register_spec(_spec(name="alpha", toolset="a", schema={"name": "alpha"}))
    reg.register_spec(_spec(name="beta", toolset="b", schema={"name": "beta"}))
    reg.register_spec(
        _spec(name="gamma", toolset="c", schema={"name": "gamma"}, check_fn=lambda: False)
    )
    authorized = {"alpha", "beta", "gamma"}
    index = {e["name"]: e for e in reg.describe_index(authorized_names=authorized)}
    defs = {d["function"]["name"] for d in reg.get_definitions(set(reg.get_all_tool_names()))}

    # (1) Unexpected missing = 0: every operational+authorized tool is selectable.
    for name in ("alpha", "beta"):
        assert index[name]["available"] and index[name]["authorized"]
        assert name in defs

    # (2) Undocumented hidden = 0: every registered tool appears in the index.
    assert set(index) == set(reg.get_all_tool_names())

    # (3) Unauthorized callable = 0: nothing outside the authorized set is marked
    #     authorized (here all are authorized; a tool outside the set is not).
    outside = {e["name"] for e in reg.describe_index(authorized_names={"alpha"})
               if e["authorized"]}
    assert outside == {"alpha"}

    # (4) Unavailable falsely advertised = 0: an unavailable tool is never in the
    #     model-facing definitions, though its existence stays visible.
    assert "gamma" not in defs
    assert "gamma" in index and index["gamma"]["available"] is False
