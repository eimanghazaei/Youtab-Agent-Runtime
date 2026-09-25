"""R8 bridge: stage-trace spans persist into the principal-bound run_journal.

Proves spans land in the ONE durable event store under a `timing` category,
survive the redaction chokepoint (numeric leaves preserved), stay principal-
scoped, and round-trip back into the percentile aggregator. A span with no
principal falls back rather than being silently dropped.
"""

from __future__ import annotations

from pathlib import Path

from youtab_runtime import run_journal as rj
from youtab_runtime import stage_trace as st
from youtab_runtime import stage_trace_report as rep


def _db(tmp_path: Path) -> Path:
    return tmp_path / "run_journal.db"


def test_span_persists_to_journal_and_roundtrips(tmp_path):
    db = _db(tmp_path)

    class _Sink:
        def __call__(self, record):
            ctx = record["ctx"]
            rj.append_event(
                ctx["run_id"],
                rj.Principal(ctx["tenant"], ctx["user"]),
                "timing",
                record["stage"],
                {
                    "duration_ns": record["duration_ns"],
                    "ok": record["ok"],
                    "clock": record["clock"],
                    "attrs": record["attrs"],
                    "attempt": ctx.get("attempt"),
                },
                correlation_id=ctx.get("correlation_id"),
                db_path=db,
            )

    st.set_sink(_Sink())
    try:
        with st.trace_context_scope(
            tenant="acme", user="alice", run_id="run-1", correlation_id="cid-1"
        ):
            st.record(st.Stage.MODEL_TTFT, duration_ns=1_500_000)
            st.record(st.Stage.MODEL_GENERATE, duration_ns=42_000_000, output_tokens=99)
    finally:
        st.set_sink(None)

    recs = list(rep.read_journal("acme", "alice", db_path=db))
    stats = rep.aggregate(recs)
    assert stats[st.Stage.MODEL_TTFT].count_measured == 1
    assert stats[st.Stage.MODEL_GENERATE].p50_ms == 42.0
    # numeric attr survived the journal redaction chokepoint
    gen = next(r for r in recs if r["stage"] == st.Stage.MODEL_GENERATE)
    assert gen["attrs"]["output_tokens"] == 99


def test_journal_reads_are_principal_scoped(tmp_path):
    db = _db(tmp_path)
    rj.append_event(
        "run-x", rj.Principal("acme", "alice"), "timing", st.Stage.TOOL_CALL,
        {"duration_ns": 1000, "ok": True, "clock": "monotonic", "attrs": {}},
        db_path=db,
    )
    # A different principal sees nothing (no cross-tenant leak).
    other = list(rep.read_journal("evil", "mallory", db_path=db))
    assert other == []
    mine = list(rep.read_journal("acme", "alice", db_path=db))
    assert len(mine) == 1


def test_runjournalsink_falls_back_without_principal(tmp_path):
    captured = []
    sink = st.RunJournalSink(fallback=lambda rec: captured.append(rec))
    st.set_sink(sink)
    try:
        # No tenant/user/run_id in context -> cannot attribute -> fallback, not drop.
        with st.span(st.Stage.PROMPT_CONSTRUCT):
            pass
    finally:
        st.set_sink(None)
    assert len(captured) == 1 and captured[0]["stage"] == st.Stage.PROMPT_CONSTRUCT


def test_runjournalsink_writes_when_principal_present(tmp_path):
    db = _db(tmp_path)
    import youtab_runtime.run_journal as _rj

    # Point the bridge's default journal at the tmp db via db_path override:
    # RunJournalSink uses default_db_path, so patch it for this test.
    orig = _rj.default_db_path
    _rj.default_db_path = lambda: db  # type: ignore[assignment]
    try:
        st.set_sink(st.RunJournalSink())
        with st.trace_context_scope(tenant="acme", user="bob", run_id="run-2"):
            st.record(st.Stage.MEMORY_RETRIEVE, duration_ns=3_000_000, kind="vector")
    finally:
        st.set_sink(None)
        _rj.default_db_path = orig  # type: ignore[assignment]

    recs = list(rep.read_journal("acme", "bob", db_path=db))
    assert len(recs) == 1 and recs[0]["attrs"]["kind"] == "vector"
