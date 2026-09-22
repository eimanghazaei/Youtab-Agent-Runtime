"""Durable long-running-task continuity contracts.

A :class:`TaskCheckpoint` is the canonical, model-neutral capsule that lets a
task resume after context compaction, runtime restart, host restart, or a model/
provider switch. Checkpoints form a hash-linked chain so a replayed or tampered
checkpoint is detectable, and each carries an explicit ``latest_safe_resume_point``
so committed effects are never re-executed on resume.

This package holds the typed schema and a pure, deterministic
:class:`CheckpointChain` validator. It performs no I/O: persistence is delegated
to the existing durable substrate (``youtab_state`` ``state_meta`` for the resume
token, ``tools/checkpoint_manager`` for the workspace snapshot it references, and
the Lane-owned effect ledger for committed-effect truth). It does not reimplement
any of those.
"""

from __future__ import annotations

from .checkpoint import (
    ArtifactRef,
    CheckpointChain,
    CheckpointChainError,
    ResumePoint,
    TaskCheckpoint,
)

__all__ = [
    "ArtifactRef",
    "CheckpointChain",
    "CheckpointChainError",
    "ResumePoint",
    "TaskCheckpoint",
]
