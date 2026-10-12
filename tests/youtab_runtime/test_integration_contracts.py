"""Reproducible integration-readiness tests against the DOCUMENTED interfaces.

These assemble the full Runtime memory path with test doubles standing in for the
not-yet-available Gateway signer and Simorgh transport. They prove the Runtime
side is wired correctly and fail-closed; they are NOT live integration (no real
Gateway/Simorgh service). When the real interfaces arrive (R1-R4 in
MEMORY_PAUSE_HANDOFF.md), the doubles are replaced and these become live tests.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from youtab_runtime.memory import (
    AuthenticatedSimorghClient,
    ClaimStatus,
    ExactTokenCounter,
    MemoryBusResult,
    MemoryClaim,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    PlacementVerifier,
    RetrievalConfig,
    RetrievalOutcome,
    RetrievalPipeline,
    RetrievedMemory,
    SignedScopePlacement,
    SimorghClientConfig,
    TrustLevel,
)

from .helpers import keypair, signed_envelope


def _gw_keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return priv, base64.b64encode(pub).decode("ascii")


def _scope() -> MemoryScope:
    return MemoryScope(tenant_id="tenant-alpha", organization_id="org-acme", workspace_id="ws-sales",
                       principal_id="user-alpha", agent_id="agent-01", run_id="run-1", purpose="default")


def _signed_placement(priv, *, nonce="nonce-integration-0001"):
    cur = datetime.now(UTC)
    raw = {
        "schema_version": "youtab.scope-placement.v1", "algorithm": "ed25519",
        "issuer": "youtab-gateway", "command_id": "command-0001", "trace_id": "trace-00000001",
        "tenant_id": "tenant-alpha", "principal_id": "user-alpha", "organization_id": "org-acme",
        "workspace_id": "ws-sales", "agent_id": "agent-01", "run_id": "run-1", "purpose": "default",
        "delegation_authority_ref": "grant-1", "nonce": nonce,
        "issued_at": (cur - timedelta(seconds=1)).isoformat(),
        "expires_at": (cur + timedelta(minutes=5)).isoformat(), "key_id": "gw-key-1",
        "signature": "placeholder-signature-that-is-long-enough",
    }
    unsigned = SignedScopePlacement.model_validate(raw)
    raw["signature"] = base64.b64encode(priv.sign(unsigned.canonical_payload())).decode("ascii")
    return SignedScopePlacement.model_validate(raw)


def _env():
    private, _ = keypair()
    return signed_envelope(private)


def _validated(content: str) -> RetrievedMemory:
    c = MemoryClaim.new(scope=_scope(), memory_type=MemoryType.SEMANTIC, content=content,
                        source_type="verified", trust_level=TrustLevel.VALIDATED).with_status(
                            ClaimStatus.VALIDATED)
    return RetrievedMemory(claim=c, score=1.0, citation=f"live:{c.memory_id}")


class _StubGatewaySignedTransport:
    """Stands in for the Simorgh MemoryBus endpoint (R1/R3). Returns live results."""

    def __init__(self, result: MemoryBusResult):
        self._payload = result.model_dump()

    def send(self, *, idempotency_key, payload):
        return self._payload


def _assembled(result: MemoryBusResult, *, enabled=True):
    gw_priv, gw_pub = _gw_keypair()
    client = AuthenticatedSimorghClient(SimorghClientConfig(enabled=True),
                                        transport=_StubGatewaySignedTransport(result))
    verifier = PlacementVerifier({"gw-key-1": gw_pub})  # test key registry (R2)
    pipe = RetrievalPipeline(RetrievalConfig(enabled=enabled), client, verifier,
                             ExactTokenCounter("words", lambda s: len(s.split())))
    return pipe, gw_priv


def test_end_to_end_signed_placement_enables_bounded_cited_retrieval() -> None:
    result = MemoryBusResult(results=(_validated("acme prefers annual billing"),
                                      _validated("acme HQ in Utrecht")))
    pipe, gw_priv = _assembled(result)
    r = pipe.retrieve(MemoryQuery(scope=_scope(), text="acme"), _signed_placement(gw_priv), _env())
    assert r.outcome is RetrievalOutcome.OK
    assert len(r.injected) == 2
    assert all(m.citation.startswith("live:") for m in r.injected)  # provenance/citations carried
    assert r.retrieval_digest


def test_end_to_end_fails_closed_without_signed_placement_when_key_unknown() -> None:
    result = MemoryBusResult(results=(_validated("x"),))
    # verifier has a DIFFERENT registry -> placement signature won't verify (R2 gap)
    gw_priv, _ = _gw_keypair()
    client = AuthenticatedSimorghClient(SimorghClientConfig(enabled=True),
                                        transport=_StubGatewaySignedTransport(result))
    other_priv, other_pub = _gw_keypair()
    verifier = PlacementVerifier({"gw-key-1": other_pub})  # wrong key for key_id
    pipe = RetrievalPipeline(RetrievalConfig(enabled=True), client, verifier,
                             ExactTokenCounter("words", lambda s: len(s.split())))
    r = pipe.retrieve(MemoryQuery(scope=_scope(), text="x"), _signed_placement(gw_priv), _env())
    assert r.outcome is RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE  # fail closed


def test_end_to_end_disabled_pipeline_is_unavailable() -> None:
    pipe, gw_priv = _assembled(MemoryBusResult(), enabled=False)
    r = pipe.retrieve(MemoryQuery(scope=_scope(), text="x"), _signed_placement(gw_priv), _env())
    assert r.outcome is RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE


def test_durable_reference_shape_is_transport_only() -> None:
    """Documents the Durable interface Memory consumes (R4): opaque refs only."""
    # Memory only ever holds these opaque references; it owns no lease/journal/state.
    durable_ref = {
        "run_id": "run-1",
        "resume_point_ref": "opaque-durable-handle-xyz",
        "committed_effect_refs": ("eff-1", "eff-2"),
        "checkpoint_digest": "0" * 64,
        "terminal_status": "checkpointed",
    }
    # Runtime treats them as opaque strings; no interpretation/ownership.
    assert isinstance(durable_ref["resume_point_ref"], str)
    assert all(isinstance(e, str) for e in durable_ref["committed_effect_refs"])
