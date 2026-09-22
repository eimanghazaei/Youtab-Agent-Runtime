"""Canonical scoped identity for every memory item.

Every stored or retrieved memory item is bound to an explicit
:class:`MemoryScope`. Scope is derived only from an admitted
:class:`~youtab_runtime.contracts.BrainCommandEnvelope` plus admission-supplied
placement (organization/workspace/agent/run). It is never guessed from
filenames, working directories, the latest session, or mutable client input.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from youtab_runtime.contracts import BrainCommandEnvelope

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"


class ScopeAdmission(BaseModel):
    """Placement fields supplied by authenticated admission, not by the agent.

    ``tenant_id`` and ``principal_id`` come from the signed command envelope;
    the remaining dimensions are resolved by the Gateway/Workspace authority at
    admission time and passed in explicitly. None may originate from tool
    arguments or model output.
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
