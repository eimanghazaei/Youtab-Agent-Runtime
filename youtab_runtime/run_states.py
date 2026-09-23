"""WAVE-26 shared failure-state vocabulary (frozen contract 8).

Every WAVE-26 subsystem — effect ledger, egress audit, process control and the
benchmark harness — reports outcomes using the enums defined here so that no two
components invent divergent state names. These are plain string enums (``StrEnum``)
so a value compares/serialises as its own text and round-trips through JSON and
SQLite ``TEXT`` columns unchanged.

The vocabulary deliberately makes ``unknown`` a first-class, reportable outcome
in every dimension. WAVE-26 forbids silently coercing an ambiguous result to
``pass`` or to ``0``; when a component cannot prove what happened it must say so.
"""

from __future__ import annotations

from enum import StrEnum
from typing import FrozenSet


class EffectState(StrEnum):
    """Lifecycle of one externally observable effect (contract 5/8).

    An effect may commit at most once. When the outcome of a side effect cannot
    be determined (crash between doing the effect and recording it) the ledger
    records ``UNKNOWN`` or ``RECONCILIATION_REQUIRED`` and never blindly retries.
    """

    PLANNED = "planned"
    AUTHORIZED = "authorized"
    IN_PROGRESS = "in_progress"
    COMMITTED = "committed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"


#: States after which an effect is settled and must not be rewritten.
EFFECT_TERMINAL_STATES: FrozenSet[EffectState] = frozenset(
    {EffectState.COMMITTED, EffectState.FAILED, EffectState.CANCELLED}
)

#: Non-terminal states that must never trigger an automatic retry, because the
#: real-world outcome is unproven.
EFFECT_AMBIGUOUS_STATES: FrozenSet[EffectState] = frozenset(
    {EffectState.UNKNOWN, EffectState.RECONCILIATION_REQUIRED}
)

#: Legal forward transitions. UNKNOWN/RECONCILIATION_REQUIRED are reachable from
#: any non-terminal state (a crash can strand an effect at any point).
_NONTERMINAL = (
    EffectState.PLANNED,
    EffectState.AUTHORIZED,
    EffectState.IN_PROGRESS,
)
EFFECT_TRANSITIONS: dict[EffectState, FrozenSet[EffectState]] = {
    EffectState.PLANNED: frozenset(
        {EffectState.AUTHORIZED, EffectState.CANCELLED, EffectState.FAILED,
         EffectState.UNKNOWN, EffectState.RECONCILIATION_REQUIRED}
    ),
    EffectState.AUTHORIZED: frozenset(
        {EffectState.IN_PROGRESS, EffectState.CANCELLED, EffectState.FAILED,
         EffectState.UNKNOWN, EffectState.RECONCILIATION_REQUIRED}
    ),
    EffectState.IN_PROGRESS: frozenset(
        {EffectState.COMMITTED, EffectState.FAILED, EffectState.CANCELLED,
         EffectState.UNKNOWN, EffectState.RECONCILIATION_REQUIRED}
    ),
    # Terminal states have no outgoing transitions.
    EffectState.COMMITTED: frozenset(),
    EffectState.FAILED: frozenset(),
    EffectState.CANCELLED: frozenset(),
    # Ambiguous states may only be resolved by an explicit reconciliation, which
    # is modelled as a fresh transition decided out-of-band, not an auto-retry.
    EffectState.UNKNOWN: frozenset(
        {EffectState.COMMITTED, EffectState.FAILED, EffectState.CANCELLED,
         EffectState.RECONCILIATION_REQUIRED}
    ),
    EffectState.RECONCILIATION_REQUIRED: frozenset(
        {EffectState.COMMITTED, EffectState.FAILED, EffectState.CANCELLED}
    ),
}


def effect_transition_allowed(src: EffectState, dst: EffectState) -> bool:
    """Return True if ``src -> dst`` is a legal effect-ledger transition."""
    return dst in EFFECT_TRANSITIONS.get(src, frozenset())


class EgressDecision(StrEnum):
    """Outbound-network audit outcome (contract 3/8).

    ``AUTHORIZED``/``DENIED`` are recorded *before* any network side effect;
    ``ATTEMPTED``/``SUCCEEDED``/``FAILED`` after. ``UNKNOWN`` is used when policy
    state is unavailable (and the boundary must then fail closed).
    """

    REQUESTED = "requested"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    ATTEMPTED = "attempted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ProcessState(StrEnum):
    """Harness-owned process lifecycle (contract 4/8)."""

    SPAWNED = "spawned"
    READY = "ready"
    RESTART_REQUESTED = "restart_requested"
    KILLED = "killed"
    RECOVERED = "recovered"
    SHUTDOWN = "shutdown"


class OwnershipOutcome(StrEnum):
    """Result of proving a PID belongs to the harness before signalling it.

    A kill/signal is refused unless ``OWNED``. ``AMBIGUOUS`` (cannot prove) is
    treated as not-owned: the harness never signals a process it cannot claim.
    """

    OWNED = "owned"
    NOT_OWNED = "not_owned"
    AMBIGUOUS = "ambiguous"


class Outcome(StrEnum):
    """Benchmark verdict / generic outcome (contract 7/8)."""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


#: Existing kanban run terminal statuses (surfaced as lifecycle events). Kept as
#: bare strings because they are owned by the kanban schema, not by WAVE-26.
RUN_TERMINAL_STATUSES: FrozenSet[str] = frozenset(
    {"done", "blocked", "crashed", "timed_out", "failed", "cancelled"}
)
