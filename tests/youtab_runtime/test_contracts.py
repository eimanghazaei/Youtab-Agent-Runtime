from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from youtab_runtime.contracts import BrainCommandEnvelope, ReasoningEnvelope

from .helpers import keypair, signed_envelope


def test_signed_brain_command_verifies() -> None:
    private, public = keypair()
    envelope = signed_envelope(private)

    envelope.verify(public)


def test_tampering_invalidates_signature() -> None:
    private, public = keypair()
    envelope = signed_envelope(private)
    tampered = envelope.model_copy(update={"objective": "changed after signing"})

    with pytest.raises(ValueError, match="invalid command signature"):
        tampered.verify(public)


def test_expired_command_is_rejected_before_execution() -> None:
    private, public = keypair()
    issued = datetime.now(UTC) - timedelta(hours=1)
    envelope = signed_envelope(private, now=issued)

    with pytest.raises(ValueError, match="expired"):
        envelope.verify(public, now=datetime.now(UTC))


def test_unknown_contract_fields_are_rejected() -> None:
    private, _ = keypair()
    payload = signed_envelope(private).model_dump(mode="json")
    payload["authority"] = "self-granted"

    with pytest.raises(ValidationError):
        BrainCommandEnvelope.model_validate(payload)


def test_reasoning_envelope_rejects_spawn_storm_shape() -> None:
    with pytest.raises(ValidationError):
        ReasoningEnvelope(
            max_iterations=1,
            max_spawn_depth=33,
            max_concurrent_agents=257,
            max_total_tokens=1,
            deadline_at=datetime.now(UTC),
        )
