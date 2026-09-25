"""Canonical scoped identity for every memory item.

Every stored or retrieved memory item is bound to an explicit
:class:`MemoryScope`. It is never guessed from filenames, working directories,
the latest session, or mutable client input.

BINDING TRUTH (verified against the canonical base envelope, ``contracts.py``):
the signed :class:`~youtab_runtime.contracts.BrainCommandEnvelope`
cryptographically binds ONLY ``tenant_id``, ``user_id`` (principal), ``task_id``,
``trace_id``, ``command_id``, ``nonce`` and ``parent_task_id``. It does **not**
carry ``organization_id``, ``workspace_id``, ``agent_id`` or ``run_id``.

Therefore only ``tenant_id`` and ``principal_id`` here are envelope-bound. The
remaining placement fields are supplied by :class:`ScopeAdmission` and are, on
the current base, **NOT cryptographically bound** — they depend on a
Gateway/Workspace-authority admission interface that does not yet exist (see
``docs/architecture/RUNTIME_SCOPE_GATEWAY_DEPENDENCY.md``). This module is
fail-closed: a scope cannot be built without explicit admission placement, and
placement is never inferred. Until Gateway provides a signed placement, callers
must treat org/workspace/agent/run as trusted-input-from-admission only.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from youtab_runtime.contracts import BrainCommandEnvelope

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"


class ScopeAdmission(BaseModel):
    """Placement fields supplied by authenticated admission, not by the agent.

    These dimensions (organization/workspace/agent/run/purpose) are NOT present
    in the signed command envelope on the current base. They must be resolved by
    the Gateway/Workspace authority at admission time and passed in explicitly;
    none may originate from tool arguments, model output, cwd, filename, profile
    or session key. Pending a signed Gateway placement, they are trusted input
    from admission, not cryptographically bound identity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str = Field(pattern=_ID)
    workspace_id: str = Field(pattern=_ID)
    agent_id: str = Field(pattern=_ID)
    run_id: str = Field(pattern=_ID)
    purpose: str = Field(default="default", pattern=_ID)


class MemoryScope(BaseModel):
    """The default-deny partition key for a memory item.

    A scope names exactly one (tenant, organization, workspace, principal,
    agent, run, purpose). Two scopes are compatible only when every dimension
    matches; there is no wildcard and no inheritance. Cross-scope access is a
    separate, gated operation handled by the router, never an implicit match.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(pattern=_ID)
    organization_id: str = Field(pattern=_ID)
    workspace_id: str = Field(pattern=_ID)
    principal_id: str = Field(pattern=_ID)
    agent_id: str = Field(pattern=_ID)
    run_id: str = Field(pattern=_ID)
    purpose: str = Field(default="default", pattern=_ID)

    @classmethod
    def from_admission(
        cls,
        envelope: BrainCommandEnvelope,
        admission: ScopeAdmission,
    ) -> "MemoryScope":
        """Build a scope from a command envelope and admission placement.

        The caller is responsible for having *admitted* the envelope
        (signature + replay + forbidden-scope checks) via
        :class:`~youtab_runtime.policy.AuthorityBoundary` before calling this.
        This method binds identity; it does not verify signatures.

        ``tenant_id``/``principal_id`` are taken from the (signed) envelope;
        org/workspace/agent/run/purpose come from ``admission`` and are only as
        trustworthy as that admission path (see module docstring). Fail-closed:
        every placement field is required, so a scope can never be half-formed.
        """

        return cls(
            tenant_id=envelope.tenant_id,
            organization_id=admission.organization_id,
            workspace_id=admission.workspace_id,
            principal_id=envelope.user_id,
            agent_id=admission.agent_id,
            run_id=admission.run_id,
            purpose=admission.purpose,
        )

    def partition_key(self) -> str:
        """A stable, collision-free storage partition string for this scope.

        Dimensions are percent-safe by construction (validated against
        ``_ID``) and joined with ``/`` which cannot appear inside a dimension,
        so distinct scopes never collide on their partition key.
        """

        return "/".join(
            (
                self.tenant_id,
                self.organization_id,
                self.workspace_id,
                self.principal_id,
                self.agent_id,
                self.run_id,
                self.purpose,
            )
        )

    def same_tenant(self, other: "MemoryScope") -> bool:
        return self.tenant_id == other.tenant_id

    def same_workspace(self, other: "MemoryScope") -> bool:
        return (
            self.tenant_id == other.tenant_id
            and self.organization_id == other.organization_id
            and self.workspace_id == other.workspace_id
        )
