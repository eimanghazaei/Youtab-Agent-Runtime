"""Authenticated Simorgh MemoryBus client — typed interface, DEFAULT DISABLED.

This is the Runtime-side client contract for a live Simorgh MemoryBus. It ships
DISABLED (`SimorghClientConfig.enabled=False`) and contains NO concrete live
transport (no fake provider). It orchestrates, around an injected
:class:`SimorghTransport`, the cross-cutting concerns a live client needs:

* scope binding — tenant/principal from signed fields; org/workspace/agent/run
  marked UNBOUND until Gateway signs them (see RUNTIME_SCOPE_GATEWAY_DEPENDENCY.md).
  Fail-closed: a live request with an unbound-but-required scope is refused.
* deadlines + cooperative cancellation;
* bounded retries with an idempotency key (the request digest);
* a circuit breaker (closed / open / half-open);
* request and response digests;
* redacted logging (digests + scope ids only, never content/secrets);
* NO silent fallback to reference/built-in — failures raise or return an explicit
  degraded live result, and results are always live-typed (`MemoryBusResult`),
  never `ReferenceMemoryBusResult`.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .bus import MemoryBusResult, MemoryQuery, degraded_result
from .scope import MemoryScope


class MemoryUnavailable(RuntimeError):
    """Raised when the live path cannot serve and no fallback is permitted."""


class ScopeNotSigned(RuntimeError):
    """Raised when a required scope dimension is not cryptographically bound."""


class ScopeBinding(BaseModel):
    """Which scope dimensions are signed vs admission-supplied (unbound)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: MemoryScope
    # On the current base envelope only tenant+principal are signed.
    signed_dimensions: frozenset[str] = frozenset({"tenant_id", "principal_id"})

    def require_signed(self, required: frozenset[str]) -> None:
        """Fail closed unless every required dimension is signed."""

        missing = required - self.signed_dimensions
        if missing:
            raise ScopeNotSigned(
                f"live routing requires signed scope; unbound: {sorted(missing)}"
            )


class SimorghClientConfig(BaseModel):
    """Client configuration. DISABLED by default; nothing goes live implicitly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    deadline_ms: int = Field(default=2_000, ge=1)
    max_retries: int = Field(default=2, ge=0, le=10)
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_cooldown_ms: int = Field(default=30_000, ge=1)
    # Dimensions that MUST be signed before a live query is allowed. Default
    # matches today's envelope (tenant+principal); tighten once Gateway signs more.
    required_signed_dimensions: frozenset[str] = frozenset(
        {"tenant_id", "principal_id"}
    )


@runtime_checkable
class SimorghTransport(Protocol):
    """The injected network boundary. A live impl lives in the Gateway/Simorgh repo.

    ``send`` returns the raw JSON-able response dict for one attempt, or raises to
    signal a retriable failure. It receives the idempotency key so the server can
    dedupe. It must never be a reference/fake data source.
    """

    def send(self, *, idempotency_key: str, payload: dict[str, object]) -> dict[str, object]: ...


@dataclass
class _Breaker:
    failure_threshold: int
    cooldown_ms: int
    _failures: int = 0
    _opened_at: float | None = field(default=None)

    def allow(self, now: float) -> bool:
        if self._opened_at is None:
            return True
        if (now - self._opened_at) * 1000.0 >= self.cooldown_ms:
            # half-open: allow a trial
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self, now: float) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = now

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


def request_digest(request: MemoryQuery) -> str:
    payload = request.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def response_digest(raw: dict[str, object]) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuthenticatedSimorghClient:
    """Orchestrates a live MemoryBus query. Ships disabled; fail-closed."""

    def __init__(
        self,
        config: SimorghClientConfig,
        transport: SimorghTransport | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._transport = transport
        self._clock = clock
        self._breaker = _Breaker(
            config.breaker_failure_threshold, config.breaker_cooldown_ms
        )
        self.last_request_digest: str | None = None
        self.last_response_digest: str | None = None

    def safe_log_fields(self, request: MemoryQuery) -> dict[str, str]:
        """Redacted log fields: scope ids + digest only — never content/secrets."""

        s = request.scope
        return {
            "tenant_id": s.tenant_id,
            "workspace_id": s.workspace_id,
            "principal_id": s.principal_id,
            "run_id": s.run_id,
            "request_digest": request_digest(request),
        }

    def query(
        self,
        request: MemoryQuery,
        binding: ScopeBinding,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> MemoryBusResult:
        # 1. disabled -> fail closed, NO fallback to reference/built-in.
        if not self._config.enabled:
            raise MemoryUnavailable("Simorgh client is disabled (default); no fallback")
        # 2. scope must be signed for the required dimensions -> fail closed.
        binding.require_signed(self._config.required_signed_dimensions)
        if self._transport is None:
            raise MemoryUnavailable("no transport configured; live path unavailable")

        digest = request_digest(request)
        self.last_request_digest = digest
        start = self._clock()
        deadline_s = self._config.deadline_ms / 1000.0
        attempts = self._config.max_retries + 1
        last_err: Exception | None = None

        for _ in range(attempts):
            if is_cancelled is not None and is_cancelled():
                raise MemoryUnavailable("query cancelled")
            now = self._clock()
            if now - start >= deadline_s:
                break
            if not self._breaker.allow(now):
                # breaker open: fail closed via explicit degraded (no fallback), not raise
                break
            try:
                raw = self._transport.send(
                    idempotency_key=digest,
                    payload=request.model_dump(mode="json"),
                )
                self.last_response_digest = response_digest(raw)
                # Validate as a LIVE result; a reference-shaped payload cannot pass.
                result = MemoryBusResult.model_validate(raw)
                self._breaker.record_success()
                return result
            except Exception as exc:  # retriable transport/validation failure
                last_err = exc
                self._breaker.record_failure(self._clock())

        # 3. exhausted/deadline/breaker -> explicit degraded LIVE result, no fallback.
        reason = "deadline or retries exhausted"
        if self._breaker.is_open:
            reason = "circuit breaker open"
        if last_err is not None:
            reason = f"{reason}: {type(last_err).__name__}"
        return degraded_result(reason)
