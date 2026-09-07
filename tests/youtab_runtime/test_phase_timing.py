"""WAVE-30F — process-lifetime phase timing marks + journal-safe snapshot."""
from __future__ import annotations

from youtab_runtime import phase_timing as pt
from youtab_runtime.model_timings import sanitize_timings


def setup_function(_):
    pt.reset()


def test_mark_and_snapshot_are_numeric_and_prefixed():
    pt.mark("agent_stack_imported")
    pt.mark("first_model_send")
    snap = pt.snapshot()
    assert set(snap) == {"lifecycle_agent_stack_imported_ms", "lifecycle_first_model_send_ms"}
    for v in snap.values():
        assert isinstance(v, float)
        assert v >= 0.0


def test_invalid_marks_are_ignored_failsoft():
    pt.mark("Bad Name")       # space + uppercase → ignored
    pt.mark("UPPER")          # uppercase → ignored
    pt.mark("ok_name")        # kept
    snap = pt.snapshot()
    assert snap == {"lifecycle_ok_name_ms": snap.get("lifecycle_ok_name_ms")}
    assert "lifecycle_ok_name_ms" in snap


def test_snapshot_survives_journal_sanitizer():
    # The emitted lifecycle keys must pass the run-journal timing redaction filter
    # (snake_case keys, numeric values) unchanged.
    pt.mark("agent_init_start")
    snap = pt.snapshot()
    clean = sanitize_timings(snap)
    assert clean == snap  # nothing dropped


def test_monotonic_ordering():
    pt.mark("a")
    pt.mark("b")
    snap = pt.snapshot()
    # b marked after a → its elapsed is >= a's.
    assert snap["lifecycle_b_ms"] >= snap["lifecycle_a_ms"]
