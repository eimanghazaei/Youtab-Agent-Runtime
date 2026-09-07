"""One-variable-at-a-time latency experiment harness (WAVE-30H R8).

The R8 requirement: "run controlled experiments that change one variable at a
time. Identify the actual dominant bottleneck from traces and measurements
before modifying performance behavior."

This harness enforces that discipline structurally:

* :func:`one_variable_matrix` takes a baseline config and, for each axis, emits
  cells that differ from the baseline in EXACTLY ONE axis — so any latency delta
  is attributable to that single variable, never a confounded combination.
* :func:`run_experiment` runs each cell ``repetitions`` times through a supplied
  ``call_fn``, records a stage span per measured leg, and aggregates each cell
  independently into p50/p95/p99 + cold/warm.

It is provider-agnostic by design. ``call_fn(config) -> timings_dict`` is the
only coupling:

* :func:`native_call_fn` drives the real local Ollama ``/api/chat`` diagnostic
  client (loopback-enforced by ``ollama_native``) — for the Owner-authorized
  diagnostic run on a host with a reachable model.
* :func:`synthetic_call_fn` is a deterministic, network-free model whose latency
  is a documented function of the config — used to VALIDATE this analysis
  pipeline (that it attributes a change to the right axis) WITHOUT a provider.

Nothing here modifies runtime behavior, hides tools, caps the registry, or
shortens a prompt. It only measures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from youtab_runtime import stage_trace as st
from youtab_runtime import stage_trace_report as rep

#: The axes an experiment may vary. Each maps to a config key.
AXES = ("num_ctx", "tool_count", "prompt_tokens", "process_cold")


@dataclass(frozen=True)
class ExperimentCell:
    """One measurement point: a full config that differs from the baseline in a
    single ``axis`` (``axis == "baseline"`` for the reference cell)."""

    name: str
    axis: str
    value: Any
    config: Mapping[str, Any]


def one_variable_matrix(
    baseline: Mapping[str, Any],
    variations: Mapping[str, list[Any]],
) -> list[ExperimentCell]:
    """Build the baseline cell plus, per axis, one cell per variant value — each
    differing from the baseline in exactly that one axis."""
    unknown = set(variations) - set(AXES)
    if unknown:
        raise ValueError(f"unknown experiment axes: {sorted(unknown)}")
    cells = [ExperimentCell("baseline", "baseline", None, dict(baseline))]
    for axis, values in variations.items():
        for v in values:
            cfg = dict(baseline)
            cfg[axis] = v
            cells.append(ExperimentCell(f"{axis}={v}", axis, v, cfg))
    return cells


@dataclass
class CellResult:
    cell: ExperimentCell
    repetitions: int
    stats: dict[str, rep.StageStats] = field(default_factory=dict)
    errors: int = 0

    def stage_p50(self, stage: str) -> Optional[float]:
        s = self.stats.get(stage)
        return s.p50_ms if s else None


def _emit_spans_from_timings(
    timings: Mapping[str, Any], sink: st.MemorySink, config: Mapping[str, Any]
) -> None:
    """Record one call's numeric legs as stage spans into ``sink`` (honest: only
    measured legs; process_cold routed to the process split)."""
    def _ns(ms: Any) -> Optional[int]:
        try:
            v = float(ms)
        except (TypeError, ValueError):
            return None
        return int(v * 1_000_000) if v >= 0 else None

    proc_cold = config.get("process_cold")
    proc_attr = {"process_cold": bool(proc_cold)} if isinstance(proc_cold, bool) else {}
    mapping = [
        ("model.call", "wall_ms", {}),
        ("model.ttft", "ttft_ms", {}),
        ("model.generate", "eval_duration_ms", {}),
        ("prompt.tokenize", "prompt_eval_duration_ms", {"reason_code": "native_prompt_eval"}),
        ("model.init", "load_duration_ms", {}),
    ]
    prev_sink = st.get_sink()
    st.set_sink(sink)
    try:
        for stage, key, extra in mapping:
            ns = _ns(timings.get(key))
            if ns is None:
                continue
            attrs = dict(extra)
            if stage in ("model.call", "model.init"):
                attrs.update(proc_attr)
            st.record(stage, duration_ns=ns, **attrs)
    finally:
        st.set_sink(prev_sink)


def run_experiment(
    cells: list[ExperimentCell],
    call_fn: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    repetitions: int = 20,
) -> list[CellResult]:
    """Run every cell ``repetitions`` times through ``call_fn`` and aggregate each
    cell independently. ``call_fn`` returns a numeric timings dict (wall_ms,
    ttft_ms, eval_duration_ms, prompt_eval_duration_ms, load_duration_ms, ...).
    A call that raises counts as an error for that cell and is skipped."""
    results: list[CellResult] = []
    for cell in cells:
        sink = st.MemorySink()
        errors = 0
        for _ in range(max(1, repetitions)):
            try:
                timings = call_fn(cell.config)
            except Exception:  # noqa: BLE001 - a failed call is data, not a crash
                errors += 1
                continue
            _emit_spans_from_timings(timings, sink, cell.config)
        results.append(
            CellResult(
                cell=cell,
                repetitions=repetitions,
                stats=rep.aggregate(sink.records),
                errors=errors,
            )
        )
    return results


def format_experiment(results: list[CellResult], *, stage: str = "model.call") -> str:
    """A compact per-cell table for one focus ``stage`` (default the per-call
    wall), plus the baseline delta so the dominant variable is obvious."""
    base = next((r for r in results if r.cell.axis == "baseline"), None)
    base_p50 = base.stage_p50(stage) if base else None
    head = f"{'cell':<24} {'axis':<14} {'p50_ms':>10} {'p95_ms':>10} {'d_vs_base':>12} {'err':>4}"
    lines = [f"# experiment focus stage: {stage}", head, "-" * len(head)]
    for r in results:
        s = r.stats.get(stage)
        p50 = s.p50_ms if s else None
        p95 = s.p95_ms if s else None
        delta = (
            f"{p50 - base_p50:+.1f}"
            if (p50 is not None and base_p50 is not None)
            else "-"
        )
        lines.append(
            f"{r.cell.name:<24} {r.cell.axis:<14} "
            f"{_f(p50):>10} {_f(p95):>10} {delta:>12} {r.errors:>4}"
        )
    return "\n".join(lines)


def _f(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.1f}"


# --------------------------------------------------------------------------- #
# call_fn implementations
# --------------------------------------------------------------------------- #
def synthetic_call_fn(config: Mapping[str, Any]) -> dict[str, Any]:
    """A deterministic, network-free model of per-call latency for validating the
    analysis pipeline. Latency is a documented linear function of the config:

    * process cold-start adds a large fixed load cost (mirrors the real per-run
      cold subprocess), attributed to ``load_duration_ms``;
    * prompt prefill scales with prompt_tokens AND the requested num_ctx;
    * generation is fixed here (the experiment varies inputs, not output length).

    These coefficients are illustrative, NOT measured real latencies — this
    function exists to prove the harness attributes a delta to the right axis.
    """
    prompt_tokens = int(config.get("prompt_tokens", 500))
    num_ctx = int(config.get("num_ctx", 8192))
    tool_count = int(config.get("tool_count", 38))
    process_cold = bool(config.get("process_cold", False))

    load_ms = 6000.0 if process_cold else 20.0
    # Prefill grows with the ACTUAL prompt tokens (and the serialized tool
    # schemas that ride in the prompt). Allocating a larger context WINDOW is
    # cheap KV-cache setup — a minor coefficient — because cost comes from
    # filling it, not sizing it.
    prefill_ms = 0.5 * prompt_tokens + 0.001 * num_ctx + 1.0 * tool_count
    gen_ms = 2000.0
    ttft_ms = load_ms + prefill_ms  # first token waits for load + prefill
    wall_ms = load_ms + prefill_ms + gen_ms
    return {
        "wall_ms": round(wall_ms, 3),
        "ttft_ms": round(ttft_ms, 3),
        "prompt_eval_duration_ms": round(prefill_ms, 3),
        "prompt_eval_count": prompt_tokens,
        "eval_duration_ms": gen_ms,
        "eval_count": 200,
        "load_duration_ms": round(load_ms, 3),
        "process_cold": process_cold,
        "native_timings_available": True,
    }


def native_call_fn(
    *, base_url: str, model: str, timeout: float = 120.0
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Return a call_fn that drives the REAL local Ollama /api/chat diagnostic
    client (loopback-enforced by ollama_native). For the Owner-authorized
    diagnostic run on a host with a reachable model — NOT used in offline tests.

    The returned fn reads ``num_ctx``/``prompt_tokens`` from the cell config to
    shape the request; ``process_cold`` is honoured by the caller sequencing
    (a fresh keep_alive=0 call before a cold cell). Never logs prompt/response.
    """
    from youtab_runtime import ollama_native  # loopback-enforced client

    def _call(config: Mapping[str, Any]) -> dict[str, Any]:
        num_ctx = int(config.get("num_ctx", 8192))
        prompt_tokens = int(config.get("prompt_tokens", 500))
        # A synthetic, content-free prompt sized to prompt_tokens (~4 chars/token).
        filler = ("word " * max(1, prompt_tokens)).strip()
        messages = [{"role": "user", "content": filler[: prompt_tokens * 4]}]
        result = ollama_native.native_chat(
            base_url=base_url,
            model=model,
            messages=messages,
            options={"num_ctx": num_ctx},
            keep_alive=(0 if config.get("process_cold") else "5m"),
            stream=True,
            timeout=timeout,
        )
        native = dict(getattr(result, "raw_timings", {}) or {})
        native["wall_ms"] = round(getattr(result, "wall_s", 0.0) * 1000.0, 3)
        ttft_s = getattr(result, "ttft_s", None)
        if ttft_s is not None:
            native["ttft_ms"] = round(ttft_s * 1000.0, 3)
        native["process_cold"] = bool(config.get("process_cold", False))
        return native

    return _call
