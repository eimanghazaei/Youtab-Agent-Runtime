"""Aggregate R8 stage traces into p50/p95/p99 and cold-vs-warm (WAVE-30H R8).

Consumes the JSONL records emitted by :mod:`youtab_runtime.stage_trace` and
produces per-stage latency statistics. Two honesty rules carry through from the
recorder:

* ``duration_ns is None`` is "not measured" and is counted separately
  (``count_null``); it NEVER enters a percentile as a zero.
* cold vs warm is split on the span's own ``cache_state`` attribute (or a
  boolean ``cold``); a stage with no cache signal contributes to neither split
  (again: absence is not zero).

No dependency on numpy — percentiles use linear interpolation (the same method
numpy calls ``linear`` / type-7), so results match common tools.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional


def percentile(values: list[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile of ``values`` at ``q`` in [0, 100].

    Returns ``None`` for an empty input (never 0.0 — an unmeasured stage has no
    percentile).
    """
    if not values:
        return None
    if q < 0 or q > 100:
        raise ValueError("q must be in [0, 100]")
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    rank = (q / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(s[lo])
    frac = rank - lo
    return float(s[lo] + (s[hi] - s[lo]) * frac)


@dataclass
class StageStats:
    stage: str
    count: int = 0  # spans seen for this stage
    count_measured: int = 0  # spans with a real duration
    count_null: int = 0  # spans that occurred but were not timed
    count_error: int = 0  # spans with ok == False
    p50_ms: Optional[float] = None
    p95_ms: Optional[float] = None
    p99_ms: Optional[float] = None
    min_ms: Optional[float] = None
    max_ms: Optional[float] = None
    mean_ms: Optional[float] = None
    cold_count: int = 0
    warm_count: int = 0
    cold_p50_ms: Optional[float] = None
    warm_p50_ms: Optional[float] = None
    clocks: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["clocks"] = sorted(self.clocks)
        return d


def _ns_to_ms(ns: float) -> float:
    return ns / 1_000_000.0


def _is_cold(attrs: Mapping[str, Any]) -> Optional[bool]:
    cs = attrs.get("cache_state")
    if isinstance(cs, str):
        low = cs.lower()
        if low == "cold":
            return True
        if low == "warm":
            return False
    cold = attrs.get("cold")
    if isinstance(cold, bool):
        return cold
    return None


def aggregate(records: Iterable[Mapping[str, Any]]) -> dict[str, StageStats]:
    """Group records by stage and compute latency statistics per stage."""
    durations: dict[str, list[float]] = {}
    cold_durs: dict[str, list[float]] = {}
    warm_durs: dict[str, list[float]] = {}
    stats: dict[str, StageStats] = {}

    for rec in records:
        stage = rec.get("stage")
        if not isinstance(stage, str):
            continue
        st = stats.setdefault(stage, StageStats(stage=stage))
        st.count += 1
        clock = rec.get("clock")
        if isinstance(clock, str):
            st.clocks.add(clock)
        if rec.get("ok") is False:
            st.count_error += 1
        dur = rec.get("duration_ns")
        if dur is None:
            st.count_null += 1  # occurred, not timed — never treated as zero
            continue
        try:
            dur_ns = float(dur)
        except (TypeError, ValueError):
            st.count_null += 1
            continue
        st.count_measured += 1
        durations.setdefault(stage, []).append(dur_ns)
        attrs = rec.get("attrs") or {}
        cold = _is_cold(attrs)
        if cold is True:
            cold_durs.setdefault(stage, []).append(dur_ns)
        elif cold is False:
            warm_durs.setdefault(stage, []).append(dur_ns)

    for stage, st in stats.items():
        vals = durations.get(stage, [])
        if vals:
            st.p50_ms = _round(_ns_to_ms(percentile(vals, 50) or 0.0))
            st.p95_ms = _round(_ns_to_ms(percentile(vals, 95) or 0.0))
            st.p99_ms = _round(_ns_to_ms(percentile(vals, 99) or 0.0))
            st.min_ms = _round(_ns_to_ms(min(vals)))
            st.max_ms = _round(_ns_to_ms(max(vals)))
            st.mean_ms = _round(_ns_to_ms(sum(vals) / len(vals)))
        cvals = cold_durs.get(stage, [])
        wvals = warm_durs.get(stage, [])
        st.cold_count = len(cvals)
        st.warm_count = len(wvals)
        if cvals:
            st.cold_p50_ms = _round(_ns_to_ms(percentile(cvals, 50) or 0.0))
        if wvals:
            st.warm_p50_ms = _round(_ns_to_ms(percentile(wvals, 50) or 0.0))
    return stats


def _round(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 3)


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield records from one trace file, skipping malformed lines."""
    p = Path(path)
    if not p.exists():
        return
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def read_dir(directory: str | Path, *, pattern: str = "trace-*.jsonl") -> Iterator[dict[str, Any]]:
    """Yield records across every trace file in a directory (multi-process)."""
    d = Path(directory)
    if not d.exists():
        return
    for f in sorted(d.glob(pattern)):
        yield from read_jsonl(f)


def read_journal(
    tenant: str,
    user: str,
    *,
    run_id: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> Iterator[dict[str, Any]]:
    """Yield stage-trace records from the run_journal ``timing`` category.

    Cross-run for a principal (for p50/p95/p99), or a single ``run_id``. Maps a
    journal row back to the record shape :func:`aggregate` expects (stage =
    event kind; duration/ok/clock/attrs from the payload).
    """
    from youtab_runtime.run_journal import (
        Principal,
        list_events,
        list_events_by_category,
    )

    principal = Principal(tenant, user)
    kwargs: dict[str, Any] = {}
    if db_path is not None:
        kwargs["db_path"] = Path(db_path)
    if run_id is not None:
        events = list_events(run_id, principal, category="timing", **kwargs)
    else:
        events = list_events_by_category(principal, "timing", **kwargs)
    for ev in events:
        payload = ev.payload or {}
        yield {
            "stage": ev.kind,
            "duration_ns": payload.get("duration_ns"),
            "ok": payload.get("ok", True),
            "clock": payload.get("clock"),
            "t_start_epoch_ns": payload.get("t_start_epoch_ns"),
            "t_end_epoch_ns": payload.get("t_end_epoch_ns"),
            "attrs": payload.get("attrs") or {},
            "ctx": {
                "run_id": ev.run_id,
                "correlation_id": ev.correlation_id,
                "root_run_id": payload.get("root_run_id"),
                "attempt": payload.get("attempt"),
                "agent_id": payload.get("agent_id"),
                "engine": payload.get("engine"),
                "provider": payload.get("provider"),
            },
        }


def format_report(stats: Mapping[str, StageStats], *, order: Optional[list[str]] = None) -> str:
    """A stable, human-readable table. Sorted by p95 desc unless ``order`` given,
    so the dominant bottleneck floats to the top."""
    rows = list(stats.values())
    if order:
        rank = {s: i for i, s in enumerate(order)}
        rows.sort(key=lambda s: rank.get(s.stage, len(order)))
    else:
        rows.sort(key=lambda s: (s.p95_ms if s.p95_ms is not None else -1.0), reverse=True)

    head = (
        f"{'stage':<28} {'n':>4} {'meas':>5} {'null':>5} {'err':>4} "
        f"{'p50':>9} {'p95':>9} {'p99':>9} {'cold_p50':>9} {'warm_p50':>9} {'clock':>10}"
    )
    lines = [head, "-" * len(head)]
    for s in rows:
        lines.append(
            f"{s.stage:<28} {s.count:>4} {s.count_measured:>5} {s.count_null:>5} "
            f"{s.count_error:>4} "
            f"{_fmt(s.p50_ms):>9} {_fmt(s.p95_ms):>9} {_fmt(s.p99_ms):>9} "
            f"{_fmt(s.cold_p50_ms):>9} {_fmt(s.warm_p50_ms):>9} "
            f"{'/'.join(sorted(s.clocks)):>10}"
        )
    return "\n".join(lines)


def _fmt(ms: Optional[float]) -> str:
    return "-" if ms is None else f"{ms:.2f}"
