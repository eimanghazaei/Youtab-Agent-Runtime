"""Causally-valid per-run / per-stage latency observability (WAVE-30H R8).

This module records *how long each execution stage took*, with enough
correlation context to reconstruct a single run's timeline and to aggregate
p50/p95/p99 and cold-vs-warm across many runs — WITHOUT ever storing secrets,
raw prompts or raw model responses.

Design rules (all enforced structurally, not by convention):

* **Secrets can't leak.** A span attribute may be ``bool``/``int``/``float``/
  ``None`` freely. A ``str`` attribute is accepted ONLY for an allowlisted key
  (:data:`SAFE_STR_KEYS`) and only up to :data:`MAX_STR_LEN`. Anything else
  raises :class:`TraceSafetyError`. To identify a prompt/response without
  storing it, callers pass a size (int) and/or :func:`content_fingerprint`.
* **null != zero.** ``duration_ns is None`` means "this stage was not measured";
  ``duration_ns == 0`` means "measured, and it was ~instant". The two are never
  conflated, in storage or in aggregation.
* **Right clock for the boundary.** Intra-process durations come from
  ``time.monotonic_ns()`` (immune to wall-clock steps). A cross-process gap
  (queue wait, worker startup) cannot share a monotonic base, so it is measured
  from wall-clock epoch marks and is labelled ``clock="epoch"`` so no one
  mistakes it for a monotonic span.
* **Immutable per-run context.** Correlation ids live in a ``contextvars``
  ContextVar (never a process-global mutable), so concurrent runs in one process
  never cross-contaminate — the same rule WAVE-30H correction 4 applied to
  memory namespacing.
* **Observability never breaks execution.** A sink failure is counted and
  swallowed; it can never raise into the traced code path.
* **Zero overhead when off.** When tracing is disabled the span does no clock
  reads and no emit; the wrapped body still runs. Instrumenting the system must
  not itself change the latency it is trying to measure.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

SCHEMA = "youtab.stage_trace/v1"

# --- canonical stage names (extensible: any string is allowed) ---------------


class Stage:
    """Canonical stage identifiers. Arbitrary strings are also permitted; these
    are the ones the R8 requirement enumerates so aggregation can label them."""

    ADMISSION_VERIFY = "admission.verify_signature"
    GRANT_VERIFY = "grant.verify"
    GRANT_PERSIST = "grant.persist"
    QUEUE_WAIT = "queue.wait"  # cross-process -> clock="epoch"
    DISPATCH_SCHEDULE = "dispatch.schedule"
    WORKER_STARTUP = "worker.startup"  # cross-process -> clock="epoch"
    MODEL_INIT = "model.init"  # attrs: cache_state="cold"|"warm"
    PROMPT_CONSTRUCT = "prompt.construct"
    PROMPT_TOKENIZE = "prompt.tokenize"  # attrs: input_tokens=int
    MODEL_TTFT = "model.ttft"
    MODEL_CALL = "model.call"  # authoritative per-call wall (live OpenAI-compat path)
    MODEL_GENERATE = "model.generate"  # attrs: output_tokens=int (native eval leg)
    TOOLS_DISCOVER = "tools.discover"
    TOOLS_SCHEMA_LOAD = "tools.schema_load"  # attrs: cache_state, tool_count
    TOOLS_SELECT = "tools.select"
    TOOL_CALL = "tool.call"  # attrs: tool_name, ok
    RETRY_BACKOFF = "retry.backoff"  # attrs: attempt, provider_throttled
    MEMORY_RETRIEVE = "memory.retrieve"  # attrs: kind="vector"|"graph"|"memory"
    CHILD_SPAWN = "child.spawn"
    CHILD_WAIT = "child.wait"
    OUTPUT_VALIDATE = "output.validate"
    OUTPUT_PERSIST = "output.persist"
    RUN_TOTAL = "run.total"  # top-level, usually clock="epoch"


KNOWN_STAGES: frozenset[str] = frozenset(
    v for k, v in vars(Stage).items() if not k.startswith("_") and isinstance(v, str)
)

# --- safety: what a string attribute is allowed to carry ---------------------

SAFE_STR_KEYS: frozenset[str] = frozenset(
    {
        "engine",
        "provider",
        "model_id",
        "tool_name",
        "cache_state",  # "cold" / "warm"
        "kind",  # memory retrieval kind
        "reason_code",  # short, non-secret failure/label code
        "result",  # short outcome label
        "stage",
    }
)
MAX_STR_LEN = 128


class TraceSafetyError(ValueError):
    """A span attribute would risk storing free-form / secret-bearing text."""


def content_fingerprint(data: Any) -> str:
    """A stable, non-reversible identity for a prompt/response/schema so we can
    tell "same content" from "different content" WITHOUT storing the content.

    Returns ``"sha256:<first-16-hex>"``. Never store the raw ``data`` anywhere.
    """
    if isinstance(data, str):
        raw = data.encode("utf-8", "surrogatepass")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raw = repr(data).encode("utf-8", "surrogatepass")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def _validate_attrs(attrs: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, val in attrs.items():
        if val is None or isinstance(val, (bool, int, float)):
            # bool is a subclass of int; both fine.
            clean[key] = val
            continue
        if isinstance(val, str):
            if key not in SAFE_STR_KEYS:
                raise TraceSafetyError(
                    f"string attribute {key!r} is not allowlisted; pass a size, "
                    f"a bool, or content_fingerprint() instead of raw text"
                )
            if len(val) > MAX_STR_LEN:
                raise TraceSafetyError(
                    f"string attribute {key!r} exceeds {MAX_STR_LEN} chars; it "
                    f"may be carrying content — store a fingerprint instead"
                )
            clean[key] = val
            continue
        raise TraceSafetyError(
            f"attribute {key!r} has unsupported type {type(val).__name__}; only "
            f"bool/int/float/None and allowlisted short str are permitted"
        )
    return clean


# --- immutable per-run correlation context -----------------------------------


@dataclass(frozen=True)
class TraceContext:
    correlation_id: Optional[str] = None
    run_id: Optional[str] = None
    root_run_id: Optional[str] = None
    attempt: Optional[int] = None
    agent_id: Optional[str] = None
    engine: Optional[str] = None
    provider: Optional[str] = None
    # Principal, so a span can bridge into the principal-bound run_journal.
    # These are ids, not secrets; blank stays None (never coerced).
    tenant: Optional[str] = None
    user: Optional[str] = None


_EMPTY = TraceContext()
_CTX: contextvars.ContextVar[TraceContext] = contextvars.ContextVar(
    "youtab_stage_trace_ctx", default=_EMPTY
)


def current_trace_context() -> TraceContext:
    return _CTX.get()


def bind_trace_context(**fields: Any) -> contextvars.Token:
    """Merge ``fields`` onto the current context and install the result.

    Returns a token; pass it to :func:`reset_trace_context`. Prefer
    :func:`trace_context_scope` where the lifetime is lexical.
    """
    known = {f for f in TraceContext.__dataclass_fields__}
    unknown = set(fields) - known
    if unknown:
        raise ValueError(f"unknown trace-context fields: {sorted(unknown)}")
    merged = replace(_CTX.get(), **{k: v for k, v in fields.items() if v is not None})
    return _CTX.set(merged)


def reset_trace_context(token: contextvars.Token) -> None:
    _CTX.reset(token)


@contextmanager
def trace_context_scope(**fields: Any) -> Iterator[TraceContext]:
    token = bind_trace_context(**fields)
    try:
        yield _CTX.get()
    finally:
        reset_trace_context(token)


# --- enable/disable (zero overhead when off) ---------------------------------

_TRUTHY_OFF = {"", "0", "false", "off", "no"}


def is_enabled() -> bool:
    """Tracing is on when ``YOUTAB_STAGE_TRACE`` is set to a truthy value, or a
    non-default sink has been installed (tests / experiments)."""
    if _sink is not _default_sink:
        return True
    return os.environ.get("YOUTAB_STAGE_TRACE", "").strip().lower() not in _TRUTHY_OFF


# --- sink --------------------------------------------------------------------

_dropped = 0
_dropped_lock = threading.Lock()


def dropped_count() -> int:
    """How many records the sink failed to persist (observability never raises,
    but it must not silently pretend success either)."""
    return _dropped


def _note_drop() -> None:
    global _dropped
    with _dropped_lock:
        _dropped += 1


class _JsonlSink:
    """Append-only, owner-only JSONL sink under the agent home. One line per
    span. Thread-safe. Never raises into the caller."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._secured = False

    def _resolve(self) -> Path:
        if self._path is not None:
            return self._path
        home = os.environ.get("YOUTAB_AGENT_HOME") or os.path.join(
            os.path.expanduser("~"), ".youtab-agent-runtime"
        )
        # Partition by pid so concurrent processes never interleave a line.
        return (
            Path(home)
            / "runtime"
            / "stage_traces"
            / f"trace-{os.getpid()}.jsonl"
        )

    def _secure(self, path: Path) -> None:
        if self._secured:
            return
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        else:
            try:
                from youtab_agent_cli.windows_acl import (
                    pywin32_available,
                    secure_directory_owner_only,
                )

                if pywin32_available():
                    secure_directory_owner_only(parent)
            except Exception:  # noqa: BLE001 - hardening is best-effort
                pass
        self._secured = True

    def __call__(self, record: Mapping[str, Any]) -> None:
        try:
            path = self._resolve()
            with self._lock:
                self._secure(path)
                line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:  # noqa: BLE001 - observability must never break a run
            _note_drop()


_default_sink: Callable[[Mapping[str, Any]], None] = _JsonlSink()
_sink: Callable[[Mapping[str, Any]], None] = _default_sink


def set_sink(sink: Optional[Callable[[Mapping[str, Any]], None]]) -> None:
    """Install a custom sink (e.g. an in-memory list for tests, or the run
    journal). ``None`` restores the default JSONL sink."""
    global _sink
    _sink = sink if sink is not None else _default_sink


def get_sink() -> Callable[[Mapping[str, Any]], None]:
    return _sink


class RunJournalSink:
    """Bridge spans into the durable, principal-bound :mod:`run_journal` under a
    ``timing`` category — so production has ONE event store (deduped, redacted,
    per-run ``seq``), not a parallel file.

    A span carries a principal only when the trace context has ``tenant`` +
    ``user`` + ``run_id`` (true for every managed/worker run). When it does not
    (e.g. a bare experiment with no principal bound), the span is handed to a
    fallback sink instead of being dropped — losing a measurement silently would
    violate the null!=zero honesty rule at the storage layer too.
    """

    def __init__(
        self, fallback: Optional[Callable[[Mapping[str, Any]], None]] = None
    ) -> None:
        self._fallback = fallback if fallback is not None else _JsonlSink()

    def __call__(self, record: Mapping[str, Any]) -> None:
        ctx = record.get("ctx") or {}
        tenant = ctx.get("tenant")
        user = ctx.get("user")
        run_id = ctx.get("run_id")
        if not (tenant and user and run_id):
            try:
                self._fallback(record)
            except Exception:  # noqa: BLE001 - observability never raises
                _note_drop()
            return
        try:
            from youtab_runtime.run_journal import Principal, append_event

            payload = {
                "duration_ns": record.get("duration_ns"),
                "ok": record.get("ok"),
                "clock": record.get("clock"),
                "t_start_epoch_ns": record.get("t_start_epoch_ns"),
                "t_end_epoch_ns": record.get("t_end_epoch_ns"),
                "attrs": record.get("attrs") or {},
                "attempt": ctx.get("attempt"),
                "root_run_id": ctx.get("root_run_id"),
                "agent_id": ctx.get("agent_id"),
                "engine": ctx.get("engine"),
                "provider": ctx.get("provider"),
            }
            append_event(
                run_id,
                Principal(tenant, user),
                "timing",
                str(record.get("stage")),
                payload,
                correlation_id=ctx.get("correlation_id"),
            )
        except Exception:  # noqa: BLE001 - observability must never break a run
            _note_drop()


class MemorySink:
    """A thread-safe in-memory sink for tests and short experiments."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self.records.append(dict(record))

    def clear(self) -> None:
        with self._lock:
            self.records.clear()


# --- emission ----------------------------------------------------------------


def _emit(
    stage: str,
    *,
    duration_ns: Optional[int],
    ok: bool,
    clock: str,
    t_start_epoch_ns: Optional[int],
    t_end_epoch_ns: Optional[int],
    attrs: dict[str, Any],
) -> None:
    ctx = _CTX.get()
    record = {
        "schema": SCHEMA,
        "stage": stage,
        "duration_ns": duration_ns,  # None == not measured (never coerced to 0)
        "ok": ok,
        "clock": clock,
        "t_start_epoch_ns": t_start_epoch_ns,
        "t_end_epoch_ns": t_end_epoch_ns,
        "ctx": {k: v for k, v in asdict(ctx).items() if v is not None},
        "attrs": attrs,
    }
    try:
        _sink(record)
    except Exception:  # noqa: BLE001 - belt-and-braces; sinks already guard
        _note_drop()


@contextmanager
def span(
    stage: str,
    *,
    clock: str = "monotonic",
    anchor_epoch: bool = False,
    **attrs: Any,
) -> Iterator[dict[str, Any]]:
    """Time an intra-process stage. Emits one record on exit.

    On exception the span still emits with ``ok=False`` and a non-secret
    ``reason_code`` (the exception class name), then re-raises — a failed stage
    is data, not something to hide.

    Yields a mutable dict the body may add safe attrs to (validated at emit).
    Set ``anchor_epoch=True`` to also stamp wall-clock start/end so a downstream
    process can chain a cross-process gap onto this span.
    """
    live = attrs.pop("_force", None) or is_enabled()
    box: dict[str, Any] = dict(attrs)
    if not live:
        # No clock reads, no emit. Body still runs.
        yield box
        return
    start_epoch = time.time_ns() if anchor_epoch else None
    t0 = time.monotonic_ns()
    ok = True
    reason_code: Optional[str] = None
    try:
        yield box
    except BaseException as exc:  # noqa: BLE001 - record then re-raise
        ok = False
        reason_code = type(exc).__name__[:MAX_STR_LEN]
        raise
    finally:
        dur = time.monotonic_ns() - t0
        end_epoch = time.time_ns() if anchor_epoch else None
        if reason_code is not None:
            box.setdefault("reason_code", reason_code)
        _emit(
            stage,
            duration_ns=dur,
            ok=ok,
            clock=clock,
            t_start_epoch_ns=start_epoch,
            t_end_epoch_ns=end_epoch,
            attrs=_validate_attrs(box),
        )


def record(
    stage: str,
    *,
    duration_ns: Optional[int],
    ok: bool = True,
    clock: str = "monotonic",
    t_start_epoch_ns: Optional[int] = None,
    t_end_epoch_ns: Optional[int] = None,
    **attrs: Any,
) -> None:
    """Emit a stage with an already-computed duration.

    Use for receive-time measurements (TTFT), and for cross-process gaps
    (``clock="epoch"``, ``duration_ns = pickup_epoch - enqueue_epoch``).
    ``duration_ns=None`` records that the stage occurred but was not timed
    (null != zero).
    """
    if not is_enabled():
        return
    _emit(
        stage,
        duration_ns=duration_ns,
        ok=ok,
        clock=clock,
        t_start_epoch_ns=t_start_epoch_ns,
        t_end_epoch_ns=t_end_epoch_ns,
        attrs=_validate_attrs(dict(attrs)),
    )


def mark_epoch() -> int:
    """A wall-clock nanosecond mark for one side of a cross-process handoff.

    Stamp it on the producing side (e.g. enqueue) and carry it to the consuming
    side (e.g. worker pickup), which computes ``time.time_ns() - mark`` and calls
    :func:`record` with ``clock="epoch"``. Always available (not gated), because
    the mark must survive even if tracing is toggled between the two sides.
    """
    return time.time_ns()
