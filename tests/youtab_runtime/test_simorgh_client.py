from __future__ import annotations

import pytest

from youtab_runtime.memory import (
    AuthenticatedSimorghClient,
    MemoryBusResult,
    MemoryQuery,
    MemoryScope,
    MemoryUnavailable,
    ReferenceMemoryBusResult,
    ScopeAdmission,
    ScopeBinding,
    ScopeNotSigned,
    SimorghClientConfig,
    request_digest,
)

from .helpers import keypair, signed_envelope


def _scope() -> MemoryScope:
    private, _ = keypair()
    env = signed_envelope(private)
    return MemoryScope.from_admission(
        env,
        ScopeAdmission(
            organization_id="org-acme", workspace_id="ws-sales",
            agent_id="agent-01", run_id="run-abc123",
        ),
    )


def _query() -> MemoryQuery:
    return MemoryQuery(scope=_scope(), text="pricing")


def _binding(signed=frozenset({"tenant_id", "principal_id"})) -> ScopeBinding:
    return ScopeBinding(scope=_scope(), signed_dimensions=signed)


class _StubTransport:
    """In-TEST double (not shipped). Returns a live-shaped payload or fails."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def send(self, *, idempotency_key, payload):
        self.calls.append(idempotency_key)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_default_disabled_fails_closed_no_fallback() -> None:
    client = AuthenticatedSimorghClient(SimorghClientConfig())  # enabled=False
    with pytest.raises(MemoryUnavailable):
        client.query(_query(), _binding())


def test_enabled_without_transport_fails_closed() -> None:
    client = AuthenticatedSimorghClient(SimorghClientConfig(enabled=True), transport=None)
    with pytest.raises(MemoryUnavailable):
        client.query(_query(), _binding())


def test_unsigned_required_scope_fails_closed() -> None:
    cfg = SimorghClientConfig(enabled=True, required_signed_dimensions=frozenset(
        {"tenant_id", "principal_id", "workspace_id"}))
    t = _StubTransport([MemoryBusResult().model_dump()])
    client = AuthenticatedSimorghClient(cfg, transport=t)
    # workspace_id is NOT in the signed set -> fail closed
    with pytest.raises(ScopeNotSigned):
        client.query(_query(), _binding())


def test_successful_live_query_returns_live_result_with_digests() -> None:
    live = MemoryBusResult().model_dump()
    t = _StubTransport([live])
    client = AuthenticatedSimorghClient(SimorghClientConfig(enabled=True), transport=t)
    result = client.query(_query(), _binding())
    assert isinstance(result, MemoryBusResult)
    assert not isinstance(result, ReferenceMemoryBusResult)
    assert client.last_request_digest and client.last_response_digest
    assert t.calls == [request_digest(_query())]  # idempotency key == request digest


def test_retries_reuse_idempotency_key_then_degrade() -> None:
    t = _StubTransport([RuntimeError("boom1"), RuntimeError("boom2"), RuntimeError("boom3")])
    cfg = SimorghClientConfig(enabled=True, max_retries=2)
    client = AuthenticatedSimorghClient(cfg, transport=t)
    result = client.query(_query(), _binding())
    # 3 attempts, all with the SAME idempotency key; no exception, explicit degraded
    assert len(t.calls) == 3
    assert len(set(t.calls)) == 1
    assert result.degraded is True and result.is_live is True


def test_circuit_breaker_opens_and_blocks() -> None:
    fails = [RuntimeError("x")] * 6
    cfg = SimorghClientConfig(enabled=True, max_retries=10, breaker_failure_threshold=3,
                              breaker_cooldown_ms=999999)
    # frozen clock so cooldown never elapses
    client = AuthenticatedSimorghClient(cfg, transport=_StubTransport(fails), clock=lambda: 0.0)
    result = client.query(_query(), _binding())
    assert result.degraded is True
    assert "circuit breaker open" in (result.degraded_reason or "")


def test_no_silent_fallback_reference_payload_cannot_pass_as_live() -> None:
    # A reference-shaped payload must NOT validate as a live MemoryBusResult.
    ref_payload = ReferenceMemoryBusResult().model_dump()
    t = _StubTransport([ref_payload, ref_payload, ref_payload])
    client = AuthenticatedSimorghClient(SimorghClientConfig(enabled=True, max_retries=2),
                                        transport=t)
    result = client.query(_query(), _binding())
    # validation of the reference payload fails -> degraded live result, never reference
    assert isinstance(result, MemoryBusResult)
    assert result.degraded is True


def test_cancellation_fails_closed() -> None:
    t = _StubTransport([MemoryBusResult().model_dump()])
    client = AuthenticatedSimorghClient(SimorghClientConfig(enabled=True), transport=t)
    with pytest.raises(MemoryUnavailable):
        client.query(_query(), _binding(), is_cancelled=lambda: True)


def test_safe_log_fields_never_leak_content() -> None:
    client = AuthenticatedSimorghClient(SimorghClientConfig(enabled=True))
    q = MemoryQuery(scope=_scope(), text="TOP-SECRET pricing memo body")
    fields = client.safe_log_fields(q)
    joined = " ".join(fields.values())
    assert "TOP-SECRET" not in joined
    assert "pricing memo" not in joined
    assert fields["request_digest"] and fields["tenant_id"] == "tenant-alpha"
