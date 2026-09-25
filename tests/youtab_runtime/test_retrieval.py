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
    return MemoryScope(tenant_id="tenant-alpha", organization_id="org-acme",
                       workspace_id="ws-sales", principal_id="user-alpha", agent_id="agent-01",
                       run_id="run-1", purpose="default")


def _placement(priv, *, nonce="nonce-placement-0001"):
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
    claim = MemoryClaim.new(scope=_scope(), memory_type=MemoryType.SEMANTIC, content=content,
                            source_type="verified", trust_level=TrustLevel.VALIDATED).with_status(
                                ClaimStatus.VALIDATED)
    return RetrievedMemory(claim=claim, score=1.0, citation=f"live:{claim.memory_id}")


class _StubTransport:
    def __init__(self, result: MemoryBusResult):
        self._payload = result.model_dump()

    def send(self, *, idempotency_key, payload):
        return self._payload


def _pipeline(result: MemoryBusResult, *, enabled=True, budget=4000, artifact_threshold=1000):
    priv, pub = _gw_keypair()
    client = AuthenticatedSimorghClient(SimorghClientConfig(enabled=True),
                                        transport=_StubTransport(result))
    verifier = PlacementVerifier({"gw-key-1": pub})
    cfg = RetrievalConfig(enabled=enabled, retrieval_token_budget=budget,
                          artifact_ref_token_threshold=artifact_threshold)
    counter = ExactTokenCounter("words", lambda s: len(s.split()))
    return RetrievalPipeline(cfg, client, verifier, counter), priv


def test_disabled_pipeline_returns_live_unavailable() -> None:
    pipe, priv = _pipeline(MemoryBusResult(), enabled=False)
    r = pipe.retrieve(MemoryQuery(scope=_scope(), text="x"), _placement(priv), _env())
    assert r.outcome is RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE
    assert r.injected == ()


def test_enabled_valid_retrieval_injects_bounded_slice() -> None:
    result = MemoryBusResult(results=(_validated("short fact one"), _validated("short fact two")))
    pipe, priv = _pipeline(result)
    r = pipe.retrieve(MemoryQuery(scope=_scope(), text="fact"), _placement(priv), _env())
    assert r.outcome is RetrievalOutcome.OK
    assert len(r.injected) == 2
    assert r.retrieval_digest


def test_large_artifact_is_referenced_not_inlined() -> None:
    big = _validated(" ".join(["w"] * 5000))  # 5000 tokens > threshold
    small = _validated("small fact")
    result = MemoryBusResult(results=(big, small))
    pipe, priv = _pipeline(result, artifact_threshold=1000)
    r = pipe.retrieve(MemoryQuery(scope=_scope(), text="w"), _placement(priv), _env())
    assert small in r.injected
    assert big not in r.injected
    assert big.citation in r.references


def test_budget_overflow_items_are_referenced() -> None:
    items = tuple(_validated(" ".join(["w"] * 300)) for _ in range(20))  # 6000 tokens total
    result = MemoryBusResult(results=items)
    pipe, priv = _pipeline(result, budget=1000, artifact_threshold=100000)
    r = pipe.retrieve(MemoryQuery(scope=_scope(), text="w"), _placement(priv), _env())
    injected_tokens = sum(len((rm.claim.content or "").split()) for rm in r.injected)
    assert injected_tokens <= 1000
    assert len(r.references) > 0  # the rest referenced, not inlined


def test_degraded_provider_result_is_live_unavailable() -> None:
    pipe, priv = _pipeline(MemoryBusResult(degraded=True, degraded_reason="brain down"))
    r = pipe.retrieve(MemoryQuery(scope=_scope(), text="x"), _placement(priv), _env())
    assert r.outcome is RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE


def test_scope_mismatch_between_request_and_placement() -> None:
    pipe, priv = _pipeline(MemoryBusResult())
    other = MemoryScope(tenant_id="tenant-beta", organization_id="org-acme", workspace_id="ws-sales",
                        principal_id="user-alpha", agent_id="agent-01", run_id="run-1", purpose="default")
    r = pipe.retrieve(MemoryQuery(scope=other, text="x"), _placement(priv), _env())
    assert r.outcome is RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE


class _CountingTransport:
    def __init__(self):
        self.calls = 0

    def send(self, *, idempotency_key, payload):
        self.calls += 1
        return MemoryBusResult().model_dump()


def test_malformed_placement_signature_no_provider_query() -> None:
    # PR#56 finding 2: a malformed-base64 placement signature must yield
    # LIVE_MEMORY_UNAVAILABLE and make NO provider query.
    from youtab_runtime.memory import ExactTokenCounter, PlacementVerifier, RetrievalConfig

    priv, pub = _gw_keypair()
    transport = _CountingTransport()
    client = AuthenticatedSimorghClient(SimorghClientConfig(enabled=True), transport=transport)
    verifier = PlacementVerifier({"gw-key-1": pub})
    pipe = RetrievalPipeline(RetrievalConfig(enabled=True), client, verifier,
                             ExactTokenCounter("words", lambda s: len(s.split())))
    good = _placement(priv)
    bad = good.model_copy(update={"signature": "!!!!not-valid-base64-but-long-enough-xxxxxxxx!!!!"})
    r = pipe.retrieve(MemoryQuery(scope=_scope(), text="x"), bad, _env())
    assert r.outcome is RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE
    assert transport.calls == 0  # provider was NOT queried
