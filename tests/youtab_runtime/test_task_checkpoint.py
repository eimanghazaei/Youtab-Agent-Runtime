from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from youtab_runtime.continuity import (
    ArtifactRef,
    CheckpointChain,
    CheckpointChainError,
    ResumePoint,
    TaskCheckpoint,
)
from youtab_runtime.memory import MemoryScope, ScopeAdmission

from .helpers import keypair, signed_envelope


def _scope(tenant: str = "tenant-alpha") -> MemoryScope:
    private, _ = keypair()
    envelope = signed_envelope(private, tenant_id=tenant)
    return MemoryScope.from_admission(
        envelope,
        ScopeAdmission(
            organization_id="org-acme",
            workspace_id="ws-sales",
            agent_id="agent-01",
            run_id="run-abc123",
        ),
    )


def _cp(
    scope: MemoryScope,
    sequence: int,
    *,
    parent_hash: str | None,
    next_action: str = "do the next thing",
    committed: tuple[str, ...] = (),
) -> TaskCheckpoint:
    return TaskCheckpoint(
        run_id="run-abc123",
        scope=scope,
        sequence=sequence,
        parent_hash=parent_hash,
        goal="close the Q3 opportunity",
        plan_version=1,
        completed_step_ids=tuple(f"s{i}" for i in range(sequence)),
        pending_step_ids=(f"s{sequence}",),
        artifacts=(ArtifactRef(artifact_id="draft-1", content_hash="a" * 64),),
        resume_point=ResumePoint(
            step_id=f"s{sequence}",
            next_action=next_action,
            committed_effect_refs=committed,
        ),
        created_by="agent-01",
        created_at=datetime.now(UTC),
    )


def test_chain_links_by_hash_and_reports_resume_point() -> None:
    scope = _scope()
    chain = CheckpointChain("run-abc123", scope)
    genesis = _cp(scope, 0, parent_hash=None)
    h0 = chain.append(genesis)
    second = _cp(scope, 1, parent_hash=h0, next_action="send the email", committed=("eff-1",))
    chain.append(second)
    chain.verify_contiguous()
    assert len(chain) == 2
    assert chain.safe_resume_point().next_action == "send the email"
    assert chain.safe_resume_point().committed_effect_refs == ("eff-1",)


def test_wrong_parent_hash_is_rejected() -> None:
    scope = _scope()
    chain = CheckpointChain("run-abc123", scope)
    chain.append(_cp(scope, 0, parent_hash=None))
    bad = _cp(scope, 1, parent_hash="b" * 64)
    with pytest.raises(CheckpointChainError):
        chain.append(bad)


def test_out_of_order_sequence_is_rejected() -> None:
    scope = _scope()
    chain = CheckpointChain("run-abc123", scope)
    h0 = chain.append(_cp(scope, 0, parent_hash=None))
    with pytest.raises(CheckpointChainError):
        chain.append(_cp(scope, 2, parent_hash=h0))


def test_scope_crossing_checkpoint_is_rejected() -> None:
    scope = _scope("tenant-alpha")
    other = _scope("tenant-beta")
    chain = CheckpointChain("run-abc123", scope)
    genesis_other = _cp(other, 0, parent_hash=None)
    with pytest.raises(CheckpointChainError):
        chain.append(genesis_other)


def test_accidental_corruption_is_detected_by_verify_contiguous() -> None:
    # NOTE: this proves tamper-EVIDENCE against accidental corruption/reordering,
    # NOT security against a malicious writer who recomputes every hash.
    scope = _scope()
    chain = CheckpointChain("run-abc123", scope)
    h0 = chain.append(_cp(scope, 0, parent_hash=None))
    chain.append(_cp(scope, 1, parent_hash=h0))
    # Corrupt the internal genesis (as a disk-corruption/bad-merge would), re-verify.
    corrupted = _cp(scope, 0, parent_hash=None, next_action="CORRUPTED")
    chain._chain[0] = corrupted  # type: ignore[attr-defined]
    with pytest.raises(CheckpointChainError):
        chain.verify_contiguous()


def test_genesis_parent_rules() -> None:
    scope = _scope()
    with pytest.raises(ValidationError):
        _cp(scope, 0, parent_hash="c" * 64)  # genesis must not have a parent
    with pytest.raises(ValidationError):
        _cp(scope, 1, parent_hash=None)  # non-genesis must have a parent


def test_step_cannot_be_completed_and_pending() -> None:
    scope = _scope()
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        TaskCheckpoint(
            run_id="run-abc123",
            scope=scope,
            sequence=0,
            parent_hash=None,
            goal="g",
            plan_version=0,
            completed_step_ids=("s1",),
            pending_step_ids=("s1",),
            resume_point=ResumePoint(step_id="s1", next_action="x"),
            created_by="agent-01",
            created_at=now,
        )


def test_model_switch_resume_is_content_addressed_and_stable() -> None:
    # Two identical checkpoints (e.g. rebuilt by a different model/provider) hash equal.
    scope = _scope()
    fixed = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    a = TaskCheckpoint(
        run_id="run-abc123", scope=scope, sequence=0, parent_hash=None,
        goal="g", plan_version=0, resume_point=ResumePoint(step_id="s0", next_action="go"),
        created_by="agent-01", created_at=fixed,
    )
    b = TaskCheckpoint(
        run_id="run-abc123", scope=scope, sequence=0, parent_hash=None,
        goal="g", plan_version=0, resume_point=ResumePoint(step_id="s0", next_action="go"),
        created_by="agent-01", created_at=fixed,
    )
    assert a.content_hash() == b.content_hash()
