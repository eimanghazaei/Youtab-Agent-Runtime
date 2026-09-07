"""Authority, effect, memory, tenant, replay and budget policy."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .admission import _ADMISSION_CAPABILITY, AdmittedCommand, seal_admitted_command
from .contracts import (
    BrainCommandEnvelope,
    BrainCommandEnvelopeV2,
    CompletionReport,
    EffectProposal,
)

# Either grant schema may be admitted: v1 (legacy) and v2 (the canonical
# Owner-mandated superset) share the attribute surface admit()/decide_tool rely
# on (allowed_toolsets, allowed_memory_scopes, tenant_id, nonce, command_id,
# task_id, trace_id, verify(), canonical_payload()).
GrantEnvelope = BrainCommandEnvelope | BrainCommandEnvelopeV2


class AdmissionError(ValueError):
    """Fail-closed admission failure that is neither a signature nor a scope
    error — e.g. the durable replay store could not complete its atomic claim.
    Distinct so the router can surface it as 503 (unavailable) rather than 401
    (rejected)."""


class NonceClaimStore(Protocol):
    """The durable, atomic single-use nonce seam admission shares with the HMAC
    transport layer (ADR-0002 DP7). ``claim`` returns True only for the first
    caller to present ``key``; it MUST be atomic across threads AND processes and
    persistent across restarts. In managed mode a durable store MUST be injected;
    the in-process fallback below protects a single process only (tests/dev)."""

    def claim(self, key: str, ts: int) -> bool: ...


class _InProcessNonceClaim:
    """Single-process fallback claimer. NOT durable; managed deployments MUST
    inject a persistent store. Kept so unit tests and the local-standalone mode
    work without a database, while making the non-durability explicit."""

    durable = False

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def claim(self, key: str, ts: int) -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True


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

    def __init__(self, *, nonce_store: NonceClaimStore | None = None) -> None:
        # DP7: durable, atomic, restart-persistent replay protection. Managed
        # deployments MUST inject a persistent, cross-process store (the same
        # ``SqliteNonceStore`` seam the HMAC transport layer uses). The default
        # in-process claimer protects a SINGLE process only (tests / the
        # local-standalone trust mode) and is explicitly non-durable.
        self._nonce_store: NonceClaimStore = nonce_store or _InProcessNonceClaim()

    def _verify_and_scope(self, envelope: GrantEnvelope, public_key_b64: str, *, now) -> None:
        """Signature + forbidden-scope checks shared by admit() and re_admit()."""
        envelope.verify(public_key_b64, now=now)
        forbidden_tools = self.FORBIDDEN_TOOLSETS & set(envelope.allowed_toolsets)
        if forbidden_tools:
            raise ValueError(f"authority-bearing toolset forbidden: {sorted(forbidden_tools)}")
        forbidden_memory = self.FORBIDDEN_MEMORY_SCOPES & set(
            envelope.allowed_memory_scopes
        )
        if forbidden_memory:
            raise ValueError(f"sovereign memory scope forbidden: {sorted(forbidden_memory)}")

    def admit(
        self,
        envelope: GrantEnvelope,
        public_key_b64: str,
        *,
        now=None,
    ) -> AdmittedCommand:
        """Verify + authorize a Brain command and mint the sealed execution
        context. Returns an :class:`AdmittedCommand` — the ONLY value that can
        subsequently authorize a tool decision (finding #5 fix). Fail-closed on
        every check; never returns without a valid, replay-fresh admission.

        This is the INGRESS admission: it consumes the grant's single-use nonce
        (durable, atomic) so the same grant cannot be admitted twice."""
        self._verify_and_scope(envelope, public_key_b64, now=now)
        # Durable, atomic single-use replay claim keyed by (tenant, nonce). The
        # store's claim is a single check-and-record op (no check-then-act
        # window) that also holds across processes and restarts. A store error
        # MUST fail closed (AdmissionError -> 503), never silently admit.
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        replay_key = f"{envelope.tenant_id}\x1f{envelope.nonce}"
        try:
            claimed = self._nonce_store.claim(replay_key, int(moment.timestamp()))
        except Exception as exc:  # noqa: BLE001 — any store failure is fail-closed
            raise AdmissionError("admission replay store unavailable") from exc
        if not claimed:
            raise ValueError("replayed command nonce")
        # Only now — after signature, scope and durable-replay checks pass — is
        # the sealed, unforgeable context minted. ``_ADMISSION_CAPABILITY`` is
        # module-private to youtab_runtime; tool/agent code cannot obtain it.
        return seal_admitted_command(_ADMISSION_CAPABILITY, envelope, now=moment)

    def re_admit(
        self,
        envelope: GrantEnvelope,
        public_key_b64: str,
        *,
        now=None,
    ) -> AdmittedCommand:
        """Re-seal a grant already admitted at ingress, WITHOUT re-claiming its
        nonce — for the worker process that executes a managed run.

        The sealed :class:`AdmittedCommand` carries a per-process HMAC proof and
        so cannot cross the ingress→worker process boundary; the worker must
        reconstruct it. This is safe precisely because the grant it re-seals is
        read from the DURABLE run record written by the authenticated ingress
        (which already verified the signature AND consumed the single-use nonce)
        — not from an untrusted caller. Re-admission still re-verifies the
        Ed25519 signature, expiry and forbidden scopes, so a tampered persisted
        grant is rejected; it merely does not double-burn the nonce (which admit()
        already consumed and which would otherwise fail as ``replayed``)."""
        self._verify_and_scope(envelope, public_key_b64, now=now)
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        return seal_admitted_command(_ADMISSION_CAPABILITY, envelope, now=moment)

    def decide_tool(
        self, admitted: AdmittedCommand, intent: ToolIntent
    ) -> ManagedToolDecision:
        # Finding #5 fix, structural: the only accepted authority is an
        # AdmittedCommand, which cannot exist unless admit() produced it. A bare
        # (possibly un-admitted / bogus-signature) envelope can no longer reach a
        # positive tool decision.
        if not isinstance(admitted, AdmittedCommand):
            raise TypeError("decide_tool requires an AdmittedCommand from admit()")
        admitted.verify_proof()
        envelope = admitted.envelope
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
        admitted: AdmittedCommand, report: CompletionReport
    ) -> None:
        # Completion is gated by the same sealed context that authorized the run,
        # so a report cannot be validated against an envelope that was never
        # admitted (DP5). Task/tenant coherence is read from the admitted
        # envelope, never from caller-supplied identity.
        if not isinstance(admitted, AdmittedCommand):
            raise TypeError("validate_completion requires an AdmittedCommand from admit()")
        admitted.verify_proof()
        envelope = admitted.envelope
        if report.task_id != envelope.task_id or report.tenant_id != envelope.tenant_id:
            raise ValueError("completion report crosses task or tenant boundary")
        if report.deactivation_status not in {"deactivated", "terminated", "checkpointed"}:
            raise ValueError("task-scoped agent did not deactivate or checkpoint")
