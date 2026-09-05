"""Tests for the WAVE-26 shared failure-state vocabulary (contract 8)."""

from __future__ import annotations

from youtab_runtime.run_states import (
    EFFECT_AMBIGUOUS_STATES,
    EFFECT_TERMINAL_STATES,
    EffectState,
    EgressDecision,
    Outcome,
    OwnershipOutcome,
    ProcessState,
    RUN_TERMINAL_STATUSES,
    effect_transition_allowed,
)


def test_enums_serialise_as_their_text():
    assert EffectState.COMMITTED == "committed"
    assert str(EffectState.COMMITTED) == "committed"
    assert EgressDecision.DENIED == "denied"
    assert ProcessState.KILLED == "killed"
    assert OwnershipOutcome.OWNED == "owned"
    assert Outcome.UNKNOWN == "unknown"


def test_terminal_and_ambiguous_partitions():
    assert EFFECT_TERMINAL_STATES == {
        EffectState.COMMITTED, EffectState.FAILED, EffectState.CANCELLED
    }
    assert EFFECT_AMBIGUOUS_STATES == {
        EffectState.UNKNOWN, EffectState.RECONCILIATION_REQUIRED
    }
    # A state cannot be both terminal and ambiguous.
    assert EFFECT_TERMINAL_STATES.isdisjoint(EFFECT_AMBIGUOUS_STATES)


def test_happy_path_transitions_allowed():
    assert effect_transition_allowed(EffectState.PLANNED, EffectState.AUTHORIZED)
    assert effect_transition_allowed(EffectState.AUTHORIZED, EffectState.IN_PROGRESS)
    assert effect_transition_allowed(EffectState.IN_PROGRESS, EffectState.COMMITTED)


def test_terminal_states_have_no_outgoing_transitions():
    for terminal in EFFECT_TERMINAL_STATES:
        assert not effect_transition_allowed(terminal, EffectState.IN_PROGRESS)
        assert not effect_transition_allowed(terminal, EffectState.COMMITTED)


def test_cannot_skip_straight_from_planned_to_committed():
    # Committing must pass through IN_PROGRESS; a plan cannot self-commit.
    assert not effect_transition_allowed(EffectState.PLANNED, EffectState.COMMITTED)


def test_unknown_reachable_from_every_nonterminal_state():
    for src in (EffectState.PLANNED, EffectState.AUTHORIZED, EffectState.IN_PROGRESS):
        assert effect_transition_allowed(src, EffectState.UNKNOWN)


def test_unknown_can_be_reconciled_but_not_blindly_progressed():
    # UNKNOWN may be resolved to a terminal state or escalated, but there is no
    # transition back into IN_PROGRESS (which would be a blind retry).
    assert effect_transition_allowed(EffectState.UNKNOWN, EffectState.COMMITTED)
    assert effect_transition_allowed(
        EffectState.UNKNOWN, EffectState.RECONCILIATION_REQUIRED)
    assert not effect_transition_allowed(EffectState.UNKNOWN, EffectState.IN_PROGRESS)


def test_run_terminal_statuses_are_strings():
    assert "cancelled" in RUN_TERMINAL_STATUSES
    assert "done" in RUN_TERMINAL_STATUSES
