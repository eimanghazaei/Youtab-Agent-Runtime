"""Versioned contracts exchanged between One Brain and the execution runtime."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Identifier = str


class ReasoningEnvelope(BaseModel):
    """One shared budget for the whole task tree, never per child."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(ge=1, le=10_000)
    max_spawn_depth: int = Field(ge=0, le=32)
    max_concurrent_agents: int = Field(ge=1, le=256)
    max_total_tokens: int = Field(ge=1, le=100_000_000)
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def require_aware_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deadline_at must be timezone-aware")
        return value.astimezone(UTC)


class BrainCommandEnvelope(BaseModel):
    """A signed, tenant-scoped order from Youtab One Brain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.agent-command.v1"]
    issuer: Literal["youtab-one-brain"]
    audience: Literal["youtab-agent-runtime"]
    command_id: Identifier = Field(min_length=8, max_length=128)
    task_id: Identifier = Field(min_length=8, max_length=128)
    parent_task_id: Identifier | None = Field(default=None, min_length=8, max_length=128)
    tenant_id: Identifier = Field(min_length=3, max_length=128)
    user_id: Identifier = Field(min_length=3, max_length=128)
    trace_id: Identifier = Field(min_length=8, max_length=128)
    nonce: Identifier = Field(min_length=16, max_length=256)
    objective: str = Field(min_length=1, max_length=65_536)
    allowed_toolsets: tuple[str, ...] = ()
    allowed_memory_scopes: tuple[str, ...] = ()
    effect_proposal_scopes: tuple[str, ...] = ()
    reasoning: ReasoningEnvelope
    issued_at: datetime
    expires_at: datetime
    key_id: Identifier = Field(min_length=3, max_length=128)
    signature: str = Field(min_length=40, max_length=256)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("command times must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator(
        "allowed_toolsets", "allowed_memory_scopes", "effect_proposal_scopes"
    )
    @classmethod
    def unique_sorted_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 128 for value in values):
            raise ValueError("scope names must be non-empty and at most 128 characters")
        if len(set(values)) != len(values):
            raise ValueError("duplicate scope")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def coherent_times(self) -> "BrainCommandEnvelope":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at")
        if self.reasoning.deadline_at > self.expires_at:
            raise ValueError("reasoning deadline cannot exceed command expiry")
        return self

    def canonical_payload(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def verify(self, public_key_b64: str, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if current < self.issued_at:
            raise ValueError("command is not yet valid")
        if current >= self.expires_at or current >= self.reasoning.deadline_at:
            raise ValueError("command expired")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(public_key_b64, validate=True)
            )
            signature = base64.b64decode(self.signature, validate=True)
            public_key.verify(signature, self.canonical_payload())
        except (ValueError, InvalidSignature) as exc:
            raise ValueError("invalid command signature") from exc


class ReasoningEnvelopeV2(BaseModel):
    """One durable shared budget for the whole execution tree (R5).

    Superset of :class:`ReasoningEnvelope`: adds a monetary ceiling
    (``max_cost_micros``) and a retry ceiling (``max_retries``). Delegated
    agents inherit the *remaining* root budget; a child never receives a fresh
    unlimited budget. Enforcement is unconditional for managed runs and must
    not depend on any benchmark campaign environment variable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(ge=1, le=10_000)
    max_spawn_depth: int = Field(ge=0, le=32)
    max_concurrent_agents: int = Field(ge=1, le=256)
    max_total_tokens: int = Field(ge=1, le=100_000_000)
    max_cost_micros: int = Field(ge=0, le=1_000_000_000_000)
    max_retries: int = Field(ge=0, le=64)
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def require_aware_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deadline_at must be timezone-aware")
        return value.astimezone(UTC)


class BrainCommandEnvelopeV2(BaseModel):
    """A signed execution grant from Youtab One Brain (Simorgh) — canonical v2.

    This is the canonical source of execution authority (ADR-0002 DP1). It is
    minted and Ed25519-signed **only** by Simorgh; the engine holds only the
    public key and can never forge one. The field set is the Owner-mandated
    superset of v1 ``BrainCommandEnvelope`` and is specified byte-for-byte in
    ``docs/architecture/CANONICAL-SIGNATURE-GRANT.md`` §3.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.agent-command.v2"]
    issuer: Literal["youtab-one-brain"]
    audience: Literal["youtab-agent-runtime"]
    protocol_version: Literal["youtab.runtime-sig.v2"]
    command_id: Identifier = Field(min_length=8, max_length=128)
    task_id: Identifier = Field(min_length=8, max_length=128)
    root_run_id: Identifier = Field(min_length=8, max_length=128)
    parent_task_id: Identifier | None = Field(default=None, min_length=8, max_length=128)
    attempt: int = Field(ge=1, le=1_000)
    tenant_id: Identifier = Field(min_length=3, max_length=128)
    workspace_id: Identifier = Field(min_length=1, max_length=128)
    user_id: Identifier = Field(min_length=3, max_length=128)
    membership_generation: int = Field(ge=0, le=1_000_000_000)
    authorization_epoch: int = Field(ge=0, le=1_000_000_000)
    agent_id: Identifier = Field(min_length=1, max_length=128)
    engine_id: Identifier = Field(min_length=1, max_length=128)
    trace_id: Identifier = Field(min_length=8, max_length=128)
    nonce: Identifier = Field(min_length=16, max_length=256)
    objective: str = Field(min_length=1, max_length=65_536)
    allowed_toolsets: tuple[str, ...] = ()
    allowed_memory_scopes: tuple[str, ...] = ()
    allowed_artifact_scopes: tuple[str, ...] = ()
    effect_proposal_scopes: tuple[str, ...] = ()
    reasoning: ReasoningEnvelopeV2
    issued_at: datetime
    expires_at: datetime
    key_id: Identifier = Field(min_length=3, max_length=128)
    signature: str = Field(min_length=40, max_length=256)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("command times must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator(
        "allowed_toolsets",
        "allowed_memory_scopes",
        "allowed_artifact_scopes",
        "effect_proposal_scopes",
    )
    @classmethod
    def unique_sorted_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 128 for value in values):
            raise ValueError("scope names must be non-empty and at most 128 characters")
        if len(set(values)) != len(values):
            raise ValueError("duplicate scope")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def coherent_times(self) -> "BrainCommandEnvelopeV2":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at")
        if self.reasoning.deadline_at > self.expires_at:
            raise ValueError("reasoning deadline cannot exceed command expiry")
        return self

    def canonical_payload(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def verify(self, public_key_b64: str, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if current < self.issued_at:
            raise ValueError("command is not yet valid")
        if current >= self.expires_at or current >= self.reasoning.deadline_at:
            raise ValueError("command expired")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(public_key_b64, validate=True)
            )
            signature = base64.b64decode(self.signature, validate=True)
            public_key.verify(signature, self.canonical_payload())
        except (ValueError, InvalidSignature) as exc:
            raise ValueError("invalid command signature") from exc


class EffectProposal(BaseModel):
    """A requested external effect returned to the Brain for authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.effect-proposal.v1"] = "youtab.effect-proposal.v1"
    proposal_id: Identifier = Field(min_length=8, max_length=128)
    command_id: Identifier = Field(min_length=8, max_length=128)
    task_id: Identifier = Field(min_length=8, max_length=128)
    tenant_id: Identifier = Field(min_length=3, max_length=128)
    trace_id: Identifier = Field(min_length=8, max_length=128)
    effect_class: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    arguments_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=4096)
    runtime_authorized: Literal[False] = False


class CompletionReport(BaseModel):
    """Mandatory handoff and deactivation record for a task-scoped agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.agent-completion.v1"] = (
        "youtab.agent-completion.v1"
    )
    agent_id: Identifier = Field(min_length=3, max_length=128)
    parent_agent_id: Identifier | None = Field(default=None, min_length=3, max_length=128)
    task_id: Identifier = Field(min_length=8, max_length=128)
    tenant_id: Identifier = Field(min_length=3, max_length=128)
    assigned_objective: str = Field(min_length=1, max_length=65_536)
    actions_taken: tuple[str, ...] = ()
    tools_used: tuple[str, ...] = ()
    memory_used: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    conflicts_found: tuple[str, ...] = ()
    verifier_results: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    trace_refs: tuple[str, ...] = ()
    completion_status: Literal[
        "completed",
        "completed_with_uncertainty",
        "blocked",
        "failed",
        "escalated",
        "checkpointed_for_resume",
        "terminated_by_brain",
        "terminated_by_budget",
        "terminated_by_safety_gate",
    ]
    deactivation_status: Literal["deactivated", "terminated", "checkpointed"]

