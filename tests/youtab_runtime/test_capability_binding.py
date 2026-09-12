"""Per-run capability manifest binding (WAVE-30H correction 2).

The ``"*"`` full-envelope sentinel must authorize only the tools that were
registered, authorized and operational AT ADMISSION — never a tool registered
later, and never an authority-bearing tool. This suite proves the binding is
frozen into the sealed context, folded into the admission proof, and enforced by
``decide_tool``.
"""

from __future__ import annotations

import pytest

from youtab_runtime.admission import CapabilityBinding
from youtab_runtime.policy import AuthorityBoundary, EffectClass, ToolIntent

from tests.youtab_runtime.helpers import keypair, signed_envelope


# ── CapabilityBinding value semantics ────────────────────────────────────────


def test_build_normalizes_sorts_and_dedups():
    b = CapabilityBinding.build(
        [("b", "h2"), ("a", "h1"), ("a", "h1")],
        policy_version="epoch:1",
        acl_version="membership:1",
    )
    assert b.tool_hashes == (("a", "h1"), ("b", "h2"))


def test_manifest_hash_changes_with_every_component():
    base = CapabilityBinding.build(
        [("a", "h1")], policy_version="epoch:1", acl_version="membership:1"
    )
    diff_tool = CapabilityBinding.build(
        [("a", "h2")], policy_version="epoch:1", acl_version="membership:1"
    )
    diff_policy = CapabilityBinding.build(
        [("a", "h1")], policy_version="epoch:2", acl_version="membership:1"
    )
    diff_acl = CapabilityBinding.build(
        [("a", "h1")], policy_version="epoch:1", acl_version="membership:2"
    )
    diff_dyn = CapabilityBinding.build(
        [("a", "h1")],
        policy_version="epoch:1",
        acl_version="membership:1",
        dynamic_inclusion=True,
    )
    hashes = {
        base.manifest_hash,
        diff_tool.manifest_hash,
        diff_policy.manifest_hash,
        diff_acl.manifest_hash,
        diff_dyn.manifest_hash,
    }
    assert len(hashes) == 5


def test_authorizes_membership_and_dynamic_inclusion():
    b = CapabilityBinding.build(
        [("read_file", "h1")], policy_version="epoch:1", acl_version="membership:1"
    )
    assert b.authorizes("read_file") is True
    assert b.authorizes("write_file") is False
    dyn = CapabilityBinding.build(
        [("read_file", "h1")],
        policy_version="epoch:1",
        acl_version="membership:1",
        dynamic_inclusion=True,
    )
    assert dyn.authorizes("anything_new") is True


# ── decide_tool enforcement ──────────────────────────────────────────────────


def _admit_with(binding, *, allowed=("*",)):
    private, public = keypair()
    envelope = signed_envelope(private, allowed_toolsets=allowed)
    boundary = AuthorityBoundary()
    return boundary, boundary.admit(envelope, public, capability_binding=binding)


def test_star_authorizes_a_tool_in_the_frozen_manifest():
    binding = CapabilityBinding.build(
        [("read_file", "h1")], policy_version="epoch:1", acl_version="membership:1"
    )
    boundary, admitted = _admit_with(binding)
    decision = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name="read_file",
            toolset="safe",
            effect_class=EffectClass.READ,
            arguments={},
        ),
    )
    assert decision.execute_in_runtime is True


def test_star_does_not_authorize_a_tool_absent_from_the_manifest():
    # A tool that was NOT registered/authorized at admission (e.g. registered
    # later) is denied even though the grant carries the "*" full envelope.
    binding = CapabilityBinding.build(
        [("read_file", "h1")], policy_version="epoch:1", acl_version="membership:1"
    )
    boundary, admitted = _admit_with(binding)
    decision = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name="newly_registered_tool",
            toolset="safe",
            effect_class=EffectClass.READ,
            arguments={},
        ),
    )
    assert decision.execute_in_runtime is False
    assert "capability manifest" in decision.reason


def test_dynamic_inclusion_binding_allows_a_new_tool():
    binding = CapabilityBinding.build(
        [("read_file", "h1")],
        policy_version="epoch:1",
        acl_version="membership:1",
        dynamic_inclusion=True,
    )
    boundary, admitted = _admit_with(binding)
    decision = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name="added_at_runtime",
            toolset="safe",
            effect_class=EffectClass.READ,
            arguments={},
        ),
    )
    assert decision.execute_in_runtime is True


def test_no_binding_preserves_pre_manifest_behaviour():
    # Boundary unit tests / local-standalone admit without a binding: "*" still
    # authorizes by toolset (no manifest gate).
    private, public = keypair()
    envelope = signed_envelope(private, allowed_toolsets=("*",))
    boundary = AuthorityBoundary()
    admitted = boundary.admit(envelope, public)  # no capability_binding
    decision = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name="whatever",
            toolset="safe",
            effect_class=EffectClass.READ,
            arguments={},
        ),
    )
    assert decision.execute_in_runtime is True


def test_authority_bearing_toolset_denied_even_with_matching_manifest():
    # Defence in depth: even if a manifest somehow listed it, an authority-bearing
    # toolset is never executable in the managed runtime.
    binding = CapabilityBinding.build(
        [("grant_authority", "h1")],
        policy_version="epoch:1",
        acl_version="membership:1",
    )
    boundary, admitted = _admit_with(binding)
    decision = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name="grant_authority",
            toolset="effect_authority",
            effect_class=EffectClass.EFFECT_AUTHORIZATION,
            arguments={},
        ),
    )
    assert decision.execute_in_runtime is False


# ── proof binding: a swapped manifest fails verification ──────────────────────


def test_swapping_the_binding_on_a_handle_fails_verify_proof():
    tight = CapabilityBinding.build(
        [("read_file", "h1")], policy_version="epoch:1", acl_version="membership:1"
    )
    boundary, admitted = _admit_with(tight)
    admitted.verify_proof()  # genuine handle verifies
    # Simulate a smuggled/forged handle whose manifest was widened in place
    # (object.__setattr__ bypasses the frozen guard, as a real attacker would).
    wider = CapabilityBinding.build(
        [("read_file", "h1"), ("exfiltrate", "h9")],
        policy_version="epoch:1",
        acl_version="membership:1",
    )
    object.__setattr__(admitted, "capability_binding", wider)
    with pytest.raises(ValueError, match="admission proof invalid"):
        admitted.verify_proof()
