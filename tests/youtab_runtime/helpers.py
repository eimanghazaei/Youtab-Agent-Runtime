from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from youtab_runtime.contracts import BrainCommandEnvelope


def keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public).decode("ascii")


def signed_envelope(
    private: Ed25519PrivateKey,
    *,
    now: datetime | None = None,
    tenant_id: str = "tenant-alpha",
    nonce: str = "nonce-0000000000000001",
    allowed_toolsets: tuple[str, ...] = ("safe",),
    allowed_memory_scopes: tuple[str, ...] = ("read:user",),
    objective: str = "Produce a bounded evidence packet.",
) -> BrainCommandEnvelope:
    current = now or datetime.now(UTC)
    raw = {
        "schema_version": "youtab.agent-command.v1",
        "issuer": "youtab-one-brain",
        "audience": "youtab-agent-runtime",
        "command_id": "command-0001",
        "task_id": "task-00000001",
        "parent_task_id": None,
        "tenant_id": tenant_id,
        "user_id": "user-alpha",
        "trace_id": "trace-00000001",
        "nonce": nonce,
        "objective": objective,
        "allowed_toolsets": allowed_toolsets,
        "allowed_memory_scopes": allowed_memory_scopes,
        "effect_proposal_scopes": ("filesystem.write",),
        "reasoning": {
            "max_iterations": 20,
            "max_spawn_depth": 2,
            "max_concurrent_agents": 3,
            "max_total_tokens": 50_000,
            "deadline_at": current + timedelta(minutes=5),
        },
        "issued_at": current - timedelta(seconds=1),
        "expires_at": current + timedelta(minutes=10),
        "key_id": "brain-signing-key-1",
        "signature": "placeholder-signature-that-is-long-enough",
    }
    unsigned = BrainCommandEnvelope.model_validate(raw)
    raw["signature"] = base64.b64encode(
        private.sign(unsigned.canonical_payload())
    ).decode("ascii")
    return BrainCommandEnvelope.model_validate(raw)
