"""Per-run capability manifest binding (WAVE-30H correction 2).

The bridge between the tool registry and the sealed admission context. It lives
in ``youtab_agent_cli`` (which already imports both ``youtab_runtime`` and the
tool ``registry``) so ``youtab_runtime`` stays free of a dependency on the tool
layer.

The ``"*"`` full-envelope sentinel in a grant means "all tools this principal is
entitled to" — NOT "any tool that ever exists". At ingress we freeze the exact
set of tool names + schema hashes that were registered, authorized (grant
toolset envelope, agent ACL, and — excluded outright — authority-bearing
toolsets) and operational, plus the effective policy/ACL versions (the grant's
own signed ``authorization_epoch`` and ``membership_generation``). That frozen
:class:`CapabilityBinding` is sealed into the admitted context, persisted with
the grant, and reconstructed identically in the worker — so a tool registered
after admission is never swept in by ``"*"``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from youtab_runtime.admission import CapabilityBinding
from youtab_runtime.policy import AuthorityBoundary

# The kind under which the frozen manifest is persisted alongside the grant.
MANIFEST_EVENT_KIND = "runtime_capability_manifest"


def _versions(envelope) -> tuple[str, str]:
    """Effective (policy_version, acl_version) taken from the SIGNED grant.

    ``authorization_epoch`` is Simorgh's policy version and
    ``membership_generation`` is the tenant/agent ACL generation; both are inside
    the Ed25519-signed envelope, so binding them ties the manifest to the exact
    authorization state the grant was minted against.
    """
    # v2 grants carry both; a legacy v1 envelope carries neither, so default to 0
    # (a stable, honest "unversioned" marker) rather than crashing.
    epoch = getattr(envelope, "authorization_epoch", 0) or 0
    membership = getattr(envelope, "membership_generation", 0) or 0
    return (f"epoch:{int(epoch)}", f"membership:{int(membership)}")


def build_ingress_binding(
    envelope,
    *,
    registry,
    acl_tool_names: Optional[Iterable[str]] = None,
    dynamic_inclusion: bool = False,
) -> CapabilityBinding:
    """Freeze the per-run capability manifest from the LIVE registry at ingress.

    Authority-bearing toolsets are excluded outright, so ``"*"`` never sweeps them
    in; ``decide_tool`` additionally hard-denies them. ``acl_tool_names``, when
    supplied, narrows the manifest to the agent's own allow-list of tool names.
    """
    allowed = set(envelope.allowed_toolsets)
    pairs = registry.capability_manifest_pairs(
        allowed,
        acl_tool_names=set(acl_tool_names) if acl_tool_names is not None else None,
        exclude_toolsets=AuthorityBoundary.FORBIDDEN_TOOLSETS,
    )
    policy_version, acl_version = _versions(envelope)
    return CapabilityBinding.build(
        pairs,
        policy_version=policy_version,
        acl_version=acl_version,
        dynamic_inclusion=dynamic_inclusion,
    )


def binding_to_persisted(binding: CapabilityBinding) -> dict[str, Any]:
    """Serialize the frozen manifest for durable persistence with the grant."""
    return {
        "tool_hashes": [[name, schema_hash] for name, schema_hash in binding.tool_hashes],
        "policy_version": binding.policy_version,
        "acl_version": binding.acl_version,
        "dynamic_inclusion": binding.dynamic_inclusion,
        "manifest_hash": binding.manifest_hash,
    }


def binding_from_persisted(data: dict[str, Any]) -> CapabilityBinding:
    """Reconstruct the frozen manifest the ingress persisted (worker side).

    Fail-closed on corruption: if the persisted ``manifest_hash`` does not match
    the hash recomputed from the persisted contents, the manifest was tampered
    and re-admission must not proceed.
    """
    binding = CapabilityBinding.build(
        [tuple(pair) for pair in data.get("tool_hashes", [])],
        policy_version=data.get("policy_version", ""),
        acl_version=data.get("acl_version", ""),
        dynamic_inclusion=bool(data.get("dynamic_inclusion", False)),
    )
    persisted_hash = data.get("manifest_hash")
    if persisted_hash and persisted_hash != binding.manifest_hash:
        raise ValueError("persisted capability manifest hash mismatch (tampered)")
    return binding
