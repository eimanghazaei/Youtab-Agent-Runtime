"""Unit tests for the TEST-ONLY durable-write fault seam at the real session
transcript chokepoint (``run_agent._maybe_inject_persist_fault``).

The seam is what the packaged crash-recovery e2e uses to fault durable
persistence *at the place every ``_persist_session`` routes message writes
through* — not the gateway finalizer, which would miss the incremental
mid-turn flushes that already made the transcript durable. It must be inert
unless armed, and the ``assistant`` mode must fail only when an un-persisted
assistant message is about to be written (so the earlier user-turn flush, and
thus session visibility/resumability, is left intact).
"""

from __future__ import annotations

import pytest

from run_agent import _maybe_inject_persist_fault

_ENV = "YOUTAB_AGENT_TEST_PERSIST_FAULT"


def test_seam_inert_without_env(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    _maybe_inject_persist_fault("assistant")  # no raise
    _maybe_inject_persist_fault()             # no raise
    monkeypatch.setenv(_ENV, "0")
    _maybe_inject_persist_fault("assistant")  # no raise
    monkeypatch.setenv(_ENV, "")
    _maybe_inject_persist_fault("assistant")  # no raise


def test_all_mode_fails_pre_loop_and_any_row(monkeypatch):
    """``all`` trips both the pre-loop guard (role=None) and any per-row call."""
    monkeypatch.setenv(_ENV, "all")
    with pytest.raises(RuntimeError):
        _maybe_inject_persist_fault()            # pre-loop
    with pytest.raises(RuntimeError):
        _maybe_inject_persist_fault("user")      # any row


def test_assistant_mode_passes_pre_loop_and_user_rows(monkeypatch):
    """``assistant`` must NOT fire pre-loop or on user/tool rows — only the
    per-row assistant write — so earlier rows commit and the user turn stays
    durable + resumable."""
    monkeypatch.setenv(_ENV, "assistant")
    _maybe_inject_persist_fault()          # pre-loop: no raise
    _maybe_inject_persist_fault("user")    # user row: no raise
    _maybe_inject_persist_fault("tool")    # tool row: no raise


def test_assistant_mode_fails_on_assistant_row(monkeypatch):
    monkeypatch.setenv(_ENV, "assistant")
    with pytest.raises(RuntimeError):
        _maybe_inject_persist_fault("assistant")
