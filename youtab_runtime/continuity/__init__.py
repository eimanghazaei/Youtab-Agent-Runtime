"""Durable long-running-task continuity contracts.

STATUS: IMPLEMENTED_NOT_INTEGRATED — pending Durable Execution owner review.

Task identity, the task event journal, TaskCheckpoint *persistence*, resume
tokens, lease/fencing, heartbeat and parent/child lifecycle are owned by the
separate Durable Execution session, NOT by the Enterprise Memory session. This
module is a *proposed* typed capsule + validator offered for that owner's review;
it is deliberately NOT wired into ``state_meta`` or any live path, and it does
not compete with the Durable owner's checkpoint schema. The Master Integrator
decides whether it is accepted, adapted, or dropped. See the interface request in
``docs/architecture/RUNTIME_DURABLE_EXECUTION_INTERFACE_REQUEST.md``.

A :class:`TaskCheckpoint` is a compact, model-neutral capsule that could let a
task resume after context compaction, runtime restart, host restart, or a model/
provider switch, with an explicit ``resume_point`` so committed effects are not
re-executed on resume. Checkpoints form a hash-linked chain.

INTEGRITY (corrected): the hash chain is **tamper-evident against accidental
corruption and accidental reordering only**. It is NOT secure against a malicious
writer who can recompute hashes. Hostile-tamper integrity would require a keyed
HMAC (protected key), an asymmetric signature, an append-only authoritative DB
boundary, or authenticated storage with a fenced writer identity — and those
belong to the Durable Execution owner, not this Memory-stream module.

This package performs no I/O; persistence, if adopted, is the Durable owner's.
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
