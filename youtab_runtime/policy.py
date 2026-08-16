"""Authority, effect, memory, tenant, replay and budget policy."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrainCommandEnvelope, CompletionReport, EffectProposal


class EffectClass(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    NETWORK = "network"
    PROCESS = "process"
    CREDENTIAL = "credential"
    MEMORY_WRITE = "memory_write"
    EFFECT_AUTHORIZATION = "effect_authorization"


class ToolIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1, max_length=128)
    toolset: str = Field(min_length=1, max_length=128)
    effect_class: EffectClass
    arguments: dict[str, object]


class ManagedToolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execute_in_runtime: bool
    reason: str
    proposal: EffectProposal | None = None


class AuthorityBoundary:
    """Fail-closed policy for the non-sovereign managed runtime."""

    FORBIDDEN_TOOLSETS = frozenset(
        {"cognitive_authority", "effect_authority", "memory_promotion"}
    )
    FORBIDDEN_MEMORY_SCOPES = frozenset(
        {"write:sovereign", "promote:organization", "promote:weights"}
    )

    def __init__(self) -> None:
        self._seen_nonces: set[tuple[str, str]] = set()

    def admit(
        self,
        envelope: BrainCommandEnvelope,
        public_key_b64: str,
        *,
        now=None,
    ) -> None:
        envelope.verify(public_key_b64, now=now)
        forbidden_tools = self.FORBIDDEN_TOOLSETS & set(envelope.allowed_toolsets)
        if forbidden_tools:
            raise ValueError(f"authority-bearing toolset forbidden: {sorted(forbidden_tools)}")
        forbidden_memory = self.FORBIDDEN_MEMORY_SCOPES & set(
            envelope.allowed_memory_scopes
        )
        if forbidden_memory:
            raise ValueError(f"sovereign memory scope forbidden: {sorted(forbidden_memory)}")
        replay_key = (envelope.tenant_id, envelope.nonce)
        if replay_key in self._seen_nonces:
            raise ValueError("replayed command nonce")
        self._seen_nonces.add(replay_key)

    def decide_tool(
        self, envelope: BrainCommandEnvelope, intent: ToolIntent
    ) -> ManagedToolDecision:
        if intent.toolset not in envelope.allowed_toolsets:
            return ManagedToolDecision(
                execute_in_runtime=False,
                reason="toolset is outside the Brain-issued task contract",
            )
        if intent.effect_class in {EffectClass.NONE, EffectClass.READ}:
            return ManagedToolDecision(
                execute_in_runtime=True,
                reason="effect-free/read-only operation admitted by task contract",
            )
        arguments = json.dumps(
            intent.arguments, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        digest = hashlib.sha256(arguments).hexdigest()
        proposal = EffectProposal(
            proposal_id=f"proposal-{digest[:24]}",
            command_id=envelope.command_id,
            task_id=envelope.task_id,
            tenant_id=envelope.tenant_id,
            trace_id=envelope.trace_id,
            effect_class=intent.effect_class.value,
            tool_name=intent.tool_name,
            arguments_digest=digest,
            reason="external effect requires Youtab Brain Effect Gate authorization",
        )
        return ManagedToolDecision(
            execute_in_runtime=False,
            reason="runtime cannot authorize external effects",
            proposal=proposal,
        )

    @staticmethod
    def validate_completion(
        envelope: BrainCommandEnvelope, report: CompletionReport
    ) -> None:
        if report.task_id != envelope.task_id or report.tenant_id != envelope.tenant_id:
            raise ValueError("completion report crosses task or tenant boundary")
        if report.deactivation_status not in {"deactivated", "terminated", "checkpointed"}:
            raise ValueError("task-scoped agent did not deactivate or checkpoint")
