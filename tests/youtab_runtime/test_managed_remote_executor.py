"""Contract tests for the typed remote-backend fail-closed managed file model.

Covers the production default (fail closed), the frozen typed request/result
shapes and their canonical-JSON determinism, and the deterministic reference
sandbox that exercises the contract without ever being promotable to "live".
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from youtab_runtime.managed_remote_executor import (
    MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE,
    ManagedRemoteFileExecutor,
    NullRemoteFileExecutor,
    ReferenceRemoteFileExecutor,
    RemoteFileAuthorityUnavailableError,
    RemoteFileEffectRequest,
    RemoteFileEffectResult,
    default_remote_executor,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _request(**overrides) -> RemoteFileEffectRequest:
    base = dict(
        canonical_sandbox_root="rel/target.txt",
        workspace_id="ws-1",
        grant_ref="grant-abc",
        authorization_ref="auth-xyz",
        operation="write",
        request_digest=_digest("request"),
        effect_digest=_digest("effect"),
        idempotency_key="idem-1",
    )
    base.update(overrides)
    return RemoteFileEffectRequest(**base)


def test_null_executor_fails_closed_with_exact_status():
    executor = NullRemoteFileExecutor()
    with pytest.raises(RemoteFileAuthorityUnavailableError) as excinfo:
        executor.execute(_request())
    assert excinfo.value.status == MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE
    assert str(excinfo.value) == MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE
    assert (
        MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE
        == "MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE"
    )


def test_default_remote_executor_is_null_and_fail_closed():
    executor = default_remote_executor()
    assert isinstance(executor, NullRemoteFileExecutor)
    assert isinstance(executor, ManagedRemoteFileExecutor)
    with pytest.raises(RemoteFileAuthorityUnavailableError):
        executor.execute(_request())


def test_request_canonical_payload_is_deterministic_and_sorted():
    req_a = _request()
    req_b = _request()
    payload = req_a.canonical_payload()
    assert payload == req_b.canonical_payload()
    # sorted keys, compact separators, valid round-trip
    text = payload.decode("utf-8")
    assert ", " not in text and ": " not in text
    assert '"canonical_sandbox_root"' in text
    # first key alphabetically is authorization_ref
    assert text.startswith('{"authorization_ref"')


def test_request_rejects_extra_field():
    with pytest.raises(ValidationError):
        _request(unexpected="nope")


def test_request_rejects_bad_effect_digest():
    with pytest.raises(ValidationError):
        _request(effect_digest="not-a-hex-digest")
    with pytest.raises(ValidationError):
        _request(effect_digest=_digest("effect")[:-1])  # 63 chars


def test_reference_executor_write_then_read_is_reference_provenance(tmp_path):
    root = tmp_path / "ref_sandbox"
    executor = ReferenceRemoteFileExecutor(root)
    assert executor.is_reference_only is True

    write_result = executor.execute(_request(operation="write"))
    assert isinstance(write_result, RemoteFileEffectResult)
    assert write_result.executed is True
    assert write_result.provenance == "reference"
    assert write_result.provenance != "live"

    written = root / "rel" / "target.txt"
    assert written.read_bytes() == _digest("request").encode("utf-8")

    read_result = executor.execute(_request(operation="read"))
    assert read_result.provenance == "reference"
    assert read_result.result_digest == hashlib.sha256(
        _digest("request").encode("utf-8")
    ).hexdigest()


def test_reference_result_cannot_be_promoted_to_live(tmp_path):
    executor = ReferenceRemoteFileExecutor(tmp_path / "ref2")
    # The reference executor hard-codes provenance="reference": no operation it
    # supports can ever yield a "live" result.
    for op in ("write", "create", "read", "delete", "move"):
        # (re)create the target so read/delete/move have something to act on
        executor.execute(_request(operation="write"))
        result = executor.execute(_request(operation=op))
        assert result.provenance == "reference"
        assert result.provenance != "live"
    # The frozen result also refuses in-place relabelling to "live".
    result = executor.execute(_request(operation="write"))
    with pytest.raises((TypeError, ValidationError)):
        result.provenance = "live"  # type: ignore[misc]


def test_reference_executor_rejects_path_escape(tmp_path):
    executor = ReferenceRemoteFileExecutor(tmp_path / "ref3")
    with pytest.raises(ValueError):
        executor.execute(_request(operation="read", canonical_sandbox_root="../escape"))
