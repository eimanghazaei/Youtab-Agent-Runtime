"""Typed task checkpoint capsule and a deterministic hash-linked chain.

The capsule is intentionally compact and structured (not a transcript): it stores
plan position, step sets, artifact references (by hash, not payload), decisions,
unresolved questions, approval/effect references, and a single safe resume point.
Summaries cite original artifact/effect identifiers so a compacted checkpoint can
be verified against durable event/effect stores.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from youtab_runtime.memory.scope import MemoryScope


class ArtifactRef(BaseModel):
    """A reference to an artifact by id + content hash, never its payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str = Field(default="artifact", max_length=64)


class ResumePoint(BaseModel):
    """The latest position from which resuming replays no committed effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, max_length=128)
    next_action: str = Field(min_length=1, max_length=8192)
    committed_effect_refs: tuple[str, ...] = ()


class TaskCheckpoint(BaseModel):
    """One durable checkpoint in a task's continuity chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.task-checkpoint.v1"] = "youtab.task-checkpoint.v1"
    run_id: str = Field(min_length=1, max_length=128)
    scope: MemoryScope
    sequence: int = Field(ge=0)
    parent_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    goal: str = Field(min_length=1, max_length=65_536)
    acceptance_criteria: tuple[str, ...] = ()
    plan_version: int = Field(ge=0)
    completed_step_ids: tuple[str, ...] = ()
    pending_step_ids: tuple[str, ...] = ()
    blocked_step_ids: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    decisions: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    approval_refs: tuple[str, ...] = ()
    effect_refs: tuple[str, ...] = ()
    resume_point: ResumePoint

    created_by: str = Field(min_length=1, max_length=128)
    created_at: datetime

    @model_validator(mode="after")
    def _coherent(self) -> "TaskCheckpoint":
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.sequence == 0 and self.parent_hash is not None:
            raise ValueError("the genesis checkpoint (sequence 0) has no parent_hash")
        if self.sequence > 0 and self.parent_hash is None:
            raise ValueError("a non-genesis checkpoint must reference its parent_hash")
        overlap = set(self.completed_step_ids) & set(self.pending_step_ids)
        if overlap:
            raise ValueError(f"steps cannot be both completed and pending: {sorted(overlap)}")
        return self

    def content_hash(self) -> str:
        """Deterministic hash over the canonical JSON of this checkpoint.

        The hash covers ``parent_hash`` so it chains: any *accidental* change to
        an earlier checkpoint changes every later hash, making corruption and
        reordering detectable. This is tamper-EVIDENT against accidental damage,
        NOT secure against a malicious writer who can recompute the whole chain;
        hostile-tamper integrity needs keyed/asymmetric authentication supplied
        by the Durable Execution owner.
        """

        payload = self.model_dump(mode="json")
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CheckpointChainError(ValueError):
    """Raised when a checkpoint chain is broken, tampered, or out of order."""


class CheckpointChain:
    """A pure validator/accumulator for a single run's checkpoint chain.

    It is deterministic and holds only the verified checkpoints appended so far,
    so the same inputs always yield the same verdict — enough to validate a
    resume against durable state without a model in the loop.
    """

    def __init__(self, run_id: str, scope: MemoryScope) -> None:
        self._run_id = run_id
        self._scope = scope
        self._chain: list[TaskCheckpoint] = []

    def append(self, checkpoint: TaskCheckpoint) -> str:
        if checkpoint.run_id != self._run_id:
            raise CheckpointChainError("checkpoint run_id does not match the chain")
        if checkpoint.scope != self._scope:
            raise CheckpointChainError("checkpoint scope crosses the chain's scope")
        expected_seq = len(self._chain)
        if checkpoint.sequence != expected_seq:
            raise CheckpointChainError(
                f"expected sequence {expected_seq}, got {checkpoint.sequence}"
            )
        if expected_seq == 0:
            if checkpoint.parent_hash is not None:
                raise CheckpointChainError("genesis checkpoint must not have a parent")
        else:
            parent_hash = self._chain[-1].content_hash()
            if checkpoint.parent_hash != parent_hash:
                raise CheckpointChainError("parent_hash does not match the prior checkpoint")
        self._chain.append(checkpoint)
        return checkpoint.content_hash()

    def latest(self) -> TaskCheckpoint:
        if not self._chain:
            raise CheckpointChainError("empty checkpoint chain has no latest checkpoint")
        return self._chain[-1]

    def safe_resume_point(self) -> ResumePoint:
        """The resume point of the latest verified checkpoint.

        Resuming from here replays no committed effect: the checkpoint's
        ``committed_effect_refs`` name what already ran, and callers must skip
        those against the (Lane-owned) effect ledger.
        """

        return self.latest().resume_point

    def verify_contiguous(self) -> None:
        """Re-verify the whole chain's linkage from genesis.

        Detects accidental corruption / inconsistent relinking, not a malicious
        writer who recomputes every hash. See the class/module integrity note.
        """

        prev_hash: str | None = None
        for index, checkpoint in enumerate(self._chain):
            if checkpoint.sequence != index:
                raise CheckpointChainError("chain is not contiguously sequenced")
            if checkpoint.parent_hash != prev_hash:
                raise CheckpointChainError(f"broken link at sequence {index}")
            prev_hash = checkpoint.content_hash()

    def __len__(self) -> int:
        return len(self._chain)
