"""WAVE-30F — controlled bottleneck-attribution matrix for the local ECO path.

The Owner's requirement: do NOT blame the Mac, the SSH tunnel, Ollama, Qwen, or
the Runtime without a controlled comparison. This module defines the measurement
schema and the *deterministic* attribution logic for four paths measured with the
same model, digest, task, generation options, and prompt payload:

  A. Direct Ollama (native /api/chat) with a MINIMAL task — isolates model
     load + baseline generation with no large prompt.
  B. Direct Ollama (native /api/chat) with the EXACT runtime-assembled messages,
     tools, and options — isolates the model's prompt-evaluation + generation cost
     of the real ~21K prompt, free of Runtime orchestration.
  C. Runtime prompt assembly + orchestration WITHOUT inference — isolates Runtime
     lifecycle/assembly overhead (startup, tool-schema build, serialization).
  D. Full Agent Runtime execution — the total the canary actually experienced.

Paths A and B are driven by :mod:`youtab_runtime.ollama_native` (authoritative
native timings). Paths C and D are driven by the Runtime itself (its own phase
timers + journal) at an Owner-authorized canary. This module is the pure analysis
layer: :func:`classify_bottlenecks` takes the measured metrics and returns typed
findings — it fabricates nothing, and marks a component ``UNRESOLVED`` when its
inputs are absent rather than guessing.

Nothing here runs live at import; the live run happens only at an authorized
canary via :func:`run_ab_paths` (local Ollama only — never a cloud provider).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

# Attribution verdicts (Owner vocabulary).
PROVEN_PRIMARY = "PROVEN_PRIMARY"
PROVEN_SECONDARY = "PROVEN_SECONDARY"
DISPROVEN = "DISPROVEN"
UNRESOLVED = "UNRESOLVED"

# A component owning ≥ this fraction of the end-to-end wall is the primary
# bottleneck; ≥ the secondary fraction is a proven secondary contributor; a
# measured component below it is disproven as the dominant cause.
PRIMARY_FRACTION = 0.50
SECONDARY_FRACTION = 0.20


@dataclass
class PathMetrics:
    """Measured metrics for one path. Missing fields stay ``None`` (never zero).

    ``wall_s`` is the observer's own end-to-end wall-clock for the path. The native
    fields (ms) come from Ollama's /api/chat timing block for A/B. ``phase_ms`` is
    the Runtime's own millisecond phase breakdown for C/D (startup, worker spawn,
    prompt assembly, serialization, transport, post-processing, …).
    """

    label: str
    wall_s: Optional[float] = None
    load_duration_ms: Optional[float] = None
    prompt_eval_duration_ms: Optional[float] = None
    eval_duration_ms: Optional[float] = None
    total_duration_ms: Optional[float] = None
    prompt_eval_count: Optional[int] = None
    eval_count: Optional[int] = None
    ttft_s: Optional[float] = None
    cold_start: Optional[bool] = None
    phase_ms: Dict[str, float] = field(default_factory=dict)


@dataclass
class Finding:
    """One attributed component of the end-to-end latency."""

    component: str
    verdict: str
    share_of_wall: Optional[float]  # 0..1 fraction of the end-to-end wall, or None
    evidence_ms: Optional[float]
    detail: str


def _ms(v: Optional[float]) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def classify_bottlenecks(
    paths: Mapping[str, PathMetrics],
    *,
    primary_fraction: float = PRIMARY_FRACTION,
    secondary_fraction: float = SECONDARY_FRACTION,
) -> List[Finding]:
    """Attribute the end-to-end (path D) wall to measured components, deterministically.

    Components:
      * ``model_cold_load`` — native ``load_duration`` (from B, else A);
      * ``model_prompt_evaluation`` — native ``prompt_eval_duration`` (from B: the
        real ~21K prompt);
      * ``model_generation`` — native ``eval_duration`` (from B);
      * ``runtime_overhead`` — Runtime assembly + orchestration without inference
        (path C wall), i.e. everything that is NOT the model call;
      * ``transport_residual`` — the unexplained remainder of D after the model
        (B total) and Runtime overhead (C) are removed — the honest bucket that
        would capture SSH/transport if it were material.

    Each component's share is a fraction of the path-D wall. A component missing its
    inputs is returned ``UNRESOLVED`` (never assumed zero, never blamed).
    """
    d = paths.get("D")
    d_wall_ms = _ms(d.wall_s) * 1000.0 if (d and _ms(d.wall_s) is not None) else None

    b = paths.get("B")
    a = paths.get("A")

    def _share(ms: Optional[float]) -> Optional[float]:
        if ms is None or d_wall_ms is None or d_wall_ms <= 0:
            return None
        return round(ms / d_wall_ms, 4)

    def _verdict(share: Optional[float], evidence: Optional[float]) -> str:
        if evidence is None or share is None:
            return UNRESOLVED
        if share >= primary_fraction:
            return PROVEN_PRIMARY
        if share >= secondary_fraction:
            return PROVEN_SECONDARY
        return DISPROVEN

    findings: List[Finding] = []

    # model cold load — prefer B (same prompt/options), fall back to A.
    load_ms = None
    load_src = None
    if b and _ms(b.load_duration_ms) is not None:
        load_ms, load_src = _ms(b.load_duration_ms), "B"
    elif a and _ms(a.load_duration_ms) is not None:
        load_ms, load_src = _ms(a.load_duration_ms), "A"
    findings.append(
        Finding(
            "model_cold_load",
            _verdict(_share(load_ms), load_ms),
            _share(load_ms),
            load_ms,
            f"native load_duration from path {load_src}" if load_ms is not None
            else "no native load_duration measured (path A/B not run) → unresolved",
        )
    )

    # model prompt evaluation — the real ~21K prompt (path B).
    pe_ms = _ms(b.prompt_eval_duration_ms) if b else None
    pe_count = b.prompt_eval_count if b else None
    findings.append(
        Finding(
            "model_prompt_evaluation",
            _verdict(_share(pe_ms), pe_ms),
            _share(pe_ms),
            pe_ms,
            f"native prompt_eval_duration for {pe_count} prompt tokens (path B)"
            if pe_ms is not None else "path B not run → unresolved",
        )
    )

    # model generation — path B.
    gen_ms = _ms(b.eval_duration_ms) if b else None
    gen_count = b.eval_count if b else None
    findings.append(
        Finding(
            "model_generation",
            _verdict(_share(gen_ms), gen_ms),
            _share(gen_ms),
            gen_ms,
            f"native eval_duration for {gen_count} generated tokens (path B)"
            if gen_ms is not None else "path B not run → unresolved",
        )
    )

    # runtime overhead — path C wall (assembly + orchestration, no inference).
    c = paths.get("C")
    c_ms = _ms(c.wall_s) * 1000.0 if (c and _ms(c.wall_s) is not None) else None
    findings.append(
        Finding(
            "runtime_overhead",
            _verdict(_share(c_ms), c_ms),
            _share(c_ms),
            c_ms,
            "path C wall (Runtime assembly + orchestration without inference)"
            if c_ms is not None else "path C not run → unresolved",
        )
    )

    # transport residual — D minus (model total B) minus (runtime overhead C).
    residual_ms = None
    b_total = _ms(b.total_duration_ms) if b else None
    if d_wall_ms is not None and b_total is not None and c_ms is not None:
        residual_ms = max(0.0, d_wall_ms - b_total - c_ms)
    findings.append(
        Finding(
            "transport_residual",
            _verdict(_share(residual_ms), residual_ms),
            _share(residual_ms),
            residual_ms,
            "D wall minus model total (B) minus runtime overhead (C)"
            if residual_ms is not None
            else "needs D, B(total_duration) and C to compute → unresolved",
        )
    )

    # Rank: proven first, then by share desc; unresolved sinks to the bottom.
    order = {PROVEN_PRIMARY: 0, PROVEN_SECONDARY: 1, DISPROVEN: 2, UNRESOLVED: 3}
    findings.sort(key=lambda f: (order.get(f.verdict, 9), -(f.share_of_wall or 0.0)))
    return findings


def run_ab_paths(
    *,
    base_url: Optional[str],
    model: str,
    runtime_messages: List[Mapping[str, Any]],
    runtime_tools: Optional[List[Mapping[str, Any]]],
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[Any] = None,
    minimal_task: str = "Reply with the single word: OK.",
    api_key: Optional[str] = None,
    native_chat_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, PathMetrics]:
    """Run paths A and B against LOCAL Ollama's native /api/chat (authorized canary only).

    Path A uses a minimal one-line task; path B replays the exact runtime-assembled
    ``messages`` + ``tools`` + ``options``. Both yield authoritative native timings.
    ``native_chat_fn`` is injectable for tests; it defaults to
    :func:`youtab_runtime.ollama_native.native_chat`. Never contacts a cloud
    provider (native Ollama is local).
    """
    if native_chat_fn is None:
        from youtab_runtime.ollama_native import native_chat as native_chat_fn  # type: ignore

    from youtab_runtime.model_timings import extract_native_ollama_timings

    def _measure(label: str, messages, tools) -> PathMetrics:
        res = native_chat_fn(
            base_url=base_url, model=model, messages=messages, tools=tools,
            options=options, keep_alive=keep_alive, api_key=api_key,
        )
        native = extract_native_ollama_timings(getattr(res, "raw_timings", {}) or {})
        return PathMetrics(
            label=label,
            wall_s=getattr(res, "wall_s", None),
            ttft_s=getattr(res, "ttft_s", None),
            load_duration_ms=native.get("load_duration_ms"),
            prompt_eval_duration_ms=native.get("prompt_eval_duration_ms"),
            eval_duration_ms=native.get("eval_duration_ms"),
            total_duration_ms=native.get("total_duration_ms"),
            prompt_eval_count=native.get("prompt_eval_count"),
            eval_count=native.get("eval_count"),
            cold_start=(native["load_duration_ms"] > 1000.0)
            if "load_duration_ms" in native else None,
        )

    return {
        "A": _measure("A", [{"role": "user", "content": minimal_task}], None),
        "B": _measure("B", list(runtime_messages), list(runtime_tools or []) or None),
    }
