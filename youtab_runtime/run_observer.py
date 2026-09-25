"""WAVE-26 per-run usage + structured tool-call observer (agent 2).

This module turns the runtime's *transient* observer hooks — ``post_api_request``
(model token/cost accounting) and ``post_tool_call`` (tool dispatch outcome) —
into *durable, ordered, principal-bound* facts in the shared run journal
(``youtab_runtime.run_journal``), so a run's real token spend and tool behaviour
can be judged from observable state rather than from an agent's self-report.

It writes three event shapes (frozen CONTRACTS.md contract 3):

  * ``usage`` / ``model_call`` — one row per model API call, deduped on the
    retry-stable ``api_request_id`` (``"{turn_id}:api:{api_call_count}"`` computed
    *before* the retry loop, ``conversation_loop.py:2002``) so retries NEVER
    double-count. When usage is missing the row records ``usage_status="unknown"``
    with every token field ``null`` — never ``0`` — and a first-class ``unknown``
    cost. Cost is estimated through ``agent.usage_pricing.estimate_usage_cost``,
    which keeps its own first-class ``unknown`` status and never fabricates ``$0``.
  * ``tool_call`` — one row per dispatched tool, carrying redacted args.
  * ``tool_result`` — one row per tool outcome, carrying a status, a redacted
    error and a result *digest* (never the raw result).

Both tool rows are keyed/deduped on ``tool_call_id`` so concurrent tool calls are
distinguished and a re-dispatch is idempotent.

Design constraints honoured:
  * The observer NEVER parses log strings — it consumes the already-structured
    hook payloads directly.
  * Every persisted payload passes through ``youtab_runtime.redaction`` first.
  * A hook must never break a live run: every public entry point is fail-soft
    (it logs and returns ``None`` on any internal error) EXCEPT for a bad
    principal, which is a programming error surfaced at construction time.
  * The observer is bound to exactly one ``(run_id, principal)`` by construction,
    so it can never attribute one run's spend to another principal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from youtab_runtime import redaction
from youtab_runtime.run_journal import (
    Principal,
    RunEvent,
    RunJournalError,
    append_event,
)

logger = logging.getLogger(__name__)


def _prune(**kwargs: Any) -> Dict[str, Any]:
    """Drop None-valued kwargs so an unmeasured attribute is simply absent
    (null != zero) rather than a null noise field on the span."""
    return {k: v for k, v in kwargs.items() if v is not None}

# Token buckets carried on a known usage payload (the shape produced by
# ``run_agent._usage_summary_for_api_request_hook`` / ``normalize_usage``).
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)

# Canonical tool_result statuses (contract 3). Anything else is normalised.
_TOOL_STATUSES = frozenset({"ok", "error", "cancelled", "timeout"})

#: The unknown-cost record used when usage is absent or pricing cannot be
#: determined. Mirrors ``estimate_usage_cost``'s first-class ``unknown`` result.
_UNKNOWN_COST: Dict[str, Any] = {
    "amount_usd": None,
    "status": "unknown",
    "source": "none",
    "pricing_version": None,
}


# --------------------------------------------------------------------------- #
# Pricing seam (injectable so tests never need the whole provider stack).
# --------------------------------------------------------------------------- #
def _default_cost_for_usage(
    *,
    model: Optional[str],
    provider: Optional[str],
    base_url: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    reasoning_tokens: int,
) -> Dict[str, Any]:
    """Estimate cost via the first-class pricing engine, keeping ``unknown``.

    Imported lazily so this module (and the benchmark sandbox) can import
    ``run_observer`` without pulling the pricing/provider tree. Any failure
    degrades to a first-class ``unknown`` cost — never a fabricated ``$0``.
    """
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

        usage = CanonicalUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
        )
        result = estimate_usage_cost(
            model or "",
            usage,
            provider=provider,
            base_url=base_url,
        )
        amount = result.amount_usd
        return {
            # Decimal -> float for JSON; None stays None (first-class unknown).
            "amount_usd": None if amount is None else float(amount),
            "status": result.status,
            "source": result.source,
            "pricing_version": result.pricing_version,
        }
    except Exception as exc:  # pragma: no cover - defensive; pricing is best-effort
        logger.debug("usage cost estimation failed: %s", exc)
        return dict(_UNKNOWN_COST)


#: A cost function takes keyword token buckets + routing and returns the cost
#: dict shape above. Injectable for tests.
CostFn = Callable[..., Dict[str, Any]]


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def _safe_ident(value: Any) -> Optional[str]:
    """Scrub+bound a short identifier string (provider/model/api_mode/...).

    Returns ``None`` for a missing value; otherwise a scrubbed, length-bounded
    string. Used instead of key-based redaction on typed fields whose keys would
    otherwise trip the secret-substring heuristic (e.g. ``*_tokens``).
    """
    if value is None:
        return None
    return redaction.scrub_text(str(value), max_chars=256)


def _as_int_or_none(value: Any) -> Optional[int]:
    """Coerce a token count to int, or ``None`` when genuinely absent.

    NB: we deliberately do NOT turn a missing value into ``0`` — an absent token
    count is ``unknown`` per contract, and ``0`` would silently understate spend.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_is_present(usage: Any) -> bool:
    """Return True when the hook delivered a real usage mapping.

    ``post_api_request`` passes ``usage=None`` when the provider returned no
    usage block (``run_agent._usage_summary_for_api_request_hook`` -> ``None``).
    An empty dict is treated the same way.
    """
    return isinstance(usage, dict) and bool(usage)


def _normalise_tool_status(
    status: Any, error_type: Any, error_message: Any, has_error: bool
) -> str:
    """Map a raw hook status onto the contract's ok|error|cancelled|timeout."""
    raw = (str(status).strip().lower() if status is not None else "")
    if raw in _TOOL_STATUSES:
        return raw
    et = (str(error_type).strip().lower() if error_type else "")
    em = (str(error_message).strip().lower() if error_message else "")
    if "timeout" in raw or "timeout" in et or "timed out" in em or "timeout" in em:
        return "timeout"
    if raw == "cancelled" or raw == "canceled" or "cancel" in et:
        return "cancelled"
    # "blocked", "error", any other non-empty status, or a derived error.
    if has_error or raw not in ("", "ok"):
        return "error"
    return "ok"


# --------------------------------------------------------------------------- #
# The observer.
# --------------------------------------------------------------------------- #
@dataclass
class RunObserver:
    """Durable sink for one run's usage + tool events.

    Bound to a single ``(run_id, principal)``. Register :meth:`on_post_api_request`
    for the ``post_api_request`` hook and :meth:`on_post_tool_call` for the
    ``post_tool_call`` hook (see :func:`register_run_observer`). Both methods
    accept the runtime's hook keyword payloads verbatim and ignore unknown keys,
    so they stay forward-compatible with hook-payload additions.
    """

    run_id: str
    principal: Principal
    correlation_id: Optional[str] = None
    db_path: Optional[Path] = None
    cost_fn: CostFn = _default_cost_for_usage

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal):
            raise RunJournalError("principal must be a Principal instance")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise RunJournalError("run_id must be a non-empty string")

    # -- usage --------------------------------------------------------------- #
    def on_post_api_request(
        self,
        *,
        api_request_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_mode: Optional[str] = None,
        usage: Any = None,
        base_url: Optional[str] = None,
        turn_id: Optional[str] = None,
        timings: Any = None,
        **_ignored: Any,
    ) -> Optional[RunEvent]:
        """Persist one authoritative ``usage`` / ``model_call`` event.

        Deduped on ``api_request_id`` (retry-stable) so a retried API call never
        double-counts. When usage is missing the event records
        ``usage_status="unknown"`` with ``null`` token fields and an ``unknown``
        cost — never ``0``.

        ``timings`` (WAVE-30E) is an OPTIONAL all-numeric per-call timing record
        (runtime wall-clock + streaming TTFT + native Ollama load/prompt-eval/eval
        durations and counts + cold/warm flag). It is re-sanitized to numeric
        leaves here before persistence, so it can never smuggle prompt/response
        text or a secret into the journal.
        """
        try:
            req_id = (api_request_id or "").strip() or None
            # These are typed, non-secret fields (identifiers + integer token
            # counts + a structured cost). We deliberately do NOT run them
            # through the key-based ``redact_mapping`` — that helper redacts any
            # key containing "token", which would wipe the very token counts this
            # event exists to record. String identifiers are scrubbed+bounded
            # individually instead.
            payload: Dict[str, Any] = {
                # Kept raw so payload.api_request_id == the dedupe_key. This is a
                # synthetic internal id ("{turn_id}:api:{n}"), not a secret.
                "api_request_id": req_id,
                "provider": _safe_ident(provider),
                "model": _safe_ident(model),
                "api_mode": _safe_ident(api_mode),
            }

            # WAVE-30E: attach the all-numeric timing record when present. Fail
            # soft — timing observability must never drop the authoritative usage
            # row. Re-sanitize (numeric leaves + the two safe boolean flags only)
            # so no free text can ride in even if a caller mis-populates it.
            if timings is not None:
                try:
                    from youtab_runtime.model_timings import sanitize_timings

                    clean_timings = sanitize_timings(timings)
                    if clean_timings:
                        payload["timings"] = clean_timings
                        # R8: also fan the numeric legs out into per-stage timing
                        # spans (model.call/ttft/generate/prompt.tokenize/init) so
                        # the percentile + cold/warm aggregator sees them. Same
                        # data, no extra provider call, no hot-path edit.
                        self._emit_timing_spans_from_call(
                            req_id=req_id, provider=provider, timings=clean_timings
                        )
                except Exception as timings_exc:  # pragma: no cover - defensive
                    logger.debug("usage timings sanitize failed: %s", timings_exc)

            if _usage_is_present(usage):
                buckets = {f: _as_int_or_none(usage.get(f)) for f in _TOKEN_FIELDS}
                # total may be absent from the summary dict; leave it None rather
                # than fabricating a sum that could disagree with the provider.
                payload.update(buckets)
                payload["usage_status"] = "known"
                # Cost is estimated in its own guard: a pricing failure must
                # degrade to an ``unknown`` cost, never drop the usage row.
                try:
                    payload["cost"] = self.cost_fn(
                        model=model,
                        provider=provider,
                        base_url=base_url,
                        input_tokens=buckets["input_tokens"] or 0,
                        output_tokens=buckets["output_tokens"] or 0,
                        cache_read_tokens=buckets["cache_read_tokens"] or 0,
                        cache_write_tokens=buckets["cache_write_tokens"] or 0,
                        reasoning_tokens=buckets["reasoning_tokens"] or 0,
                    )
                except Exception as cost_exc:
                    logger.debug("usage cost_fn raised: %s", cost_exc)
                    payload["cost"] = dict(_UNKNOWN_COST)
            else:
                for f in _TOKEN_FIELDS:
                    payload[f] = None
                payload["usage_status"] = "unknown"
                payload["cost"] = dict(_UNKNOWN_COST)

            return append_event(
                self.run_id,
                self.principal,
                "usage",
                "model_call",
                payload,
                correlation_id=self.correlation_id,
                # Dedupe on the retry-stable request id when present. Without an
                # id we cannot safely dedupe, so we record the call rather than
                # risk dropping a real usage row.
                dedupe_key=req_id,
                db_path=self.db_path,
            )
        except Exception as exc:  # never break a live run for observability
            logger.warning("run_observer.on_post_api_request failed: %s", exc,
                           exc_info=True)
            return None

    # -- R8 timing spans (WAVE-30H) ----------------------------------------- #
    def _record_timing_span(
        self,
        stage: str,
        *,
        duration_ns: Optional[int],
        ok: bool = True,
        clock: str = "monotonic",
        dedupe_key: Optional[str] = None,
        **attrs: Any,
    ) -> None:
        """Persist one R8 stage span into the ``timing`` journal category.

        Reuses stage_trace's attribute-safety validator so a bridged span obeys
        the same no-free-text rule as a first-class span, and shares the exact
        payload shape :func:`stage_trace_report.read_journal` reads back. Never
        raises — timing observability must not break a live run.
        """
        try:
            from youtab_runtime import stage_trace as _st

            clean = _st._validate_attrs(attrs)
            payload = {
                "duration_ns": duration_ns,  # None == not measured (never 0)
                "ok": ok,
                "clock": clock,
                "t_start_epoch_ns": None,
                "t_end_epoch_ns": None,
                "attrs": clean,
            }
            append_event(
                self.run_id,
                self.principal,
                "timing",
                stage,
                payload,
                correlation_id=self.correlation_id,
                dedupe_key=dedupe_key,
                db_path=self.db_path,
            )
        except Exception as exc:  # never break a run for observability
            logger.debug("run_observer timing span %s failed: %s", stage, exc)

    def _emit_timing_spans_from_call(
        self,
        *,
        req_id: Optional[str],
        provider: Optional[str],
        timings: Mapping[str, Any],
    ) -> None:
        """Derive per-stage spans from one model call's numeric timing record.

        Honest mapping (measured legs only; a missing leg is simply not emitted —
        null != zero):
        * ``model.call``  <- wall_ms  (authoritative on the live OpenAI-compat
          path, which exposes NO native timings)
        * ``model.ttft``  <- ttft_ms
        * ``model.generate`` <- eval_duration_ms  (native generation leg)
        * ``prompt.tokenize`` <- prompt_eval_duration_ms (native prefill; labelled
          reason_code so it is never mistaken for client-side tokenization)
        * ``model.init``  <- load_duration_ms, cache_state from cold_start
        """
        def _ms_to_ns(ms: Any) -> Optional[int]:
            try:
                v = float(ms)
            except (TypeError, ValueError):
                return None
            return int(v * 1_000_000) if v >= 0 else None

        prov = _safe_ident(provider)
        cache_state: Optional[str] = None
        cold = timings.get("cold_start")
        if isinstance(cold, bool):
            cache_state = "cold" if cold else "warm"
        # Distinct, genuinely-known process residency (first call in a fresh
        # per-run subprocess) — NEVER conflated with model cold_start above.
        proc_cold = timings.get("process_cold")
        process_cold = bool(proc_cold) if isinstance(proc_cold, bool) else None
        base = f"{req_id}:" if req_id else None

        wall = _ms_to_ns(timings.get("wall_ms"))
        if wall is not None:
            self._record_timing_span(
                "model.call", duration_ns=wall,
                dedupe_key=(base + "model.call") if base else None,
                **_prune(provider=prov, cache_state=cache_state,
                         process_cold=process_cold),
            )
        ttft = _ms_to_ns(timings.get("ttft_ms"))
        if ttft is not None:
            self._record_timing_span(
                "model.ttft", duration_ns=ttft,
                dedupe_key=(base + "model.ttft") if base else None,
                **_prune(provider=prov, cache_state=cache_state),
            )
        gen = _ms_to_ns(timings.get("eval_duration_ms"))
        if gen is not None:
            self._record_timing_span(
                "model.generate", duration_ns=gen,
                dedupe_key=(base + "model.generate") if base else None,
                **_prune(provider=prov, output_tokens=_as_int_or_none(
                    timings.get("eval_count"))),
            )
        prefill = _ms_to_ns(timings.get("prompt_eval_duration_ms"))
        if prefill is not None:
            self._record_timing_span(
                "prompt.tokenize", duration_ns=prefill,
                dedupe_key=(base + "prompt.tokenize") if base else None,
                reason_code="native_prompt_eval",
                **_prune(provider=prov, input_tokens=_as_int_or_none(
                    timings.get("prompt_eval_count"))),
            )
        init = _ms_to_ns(timings.get("load_duration_ms"))
        if init is not None:
            self._record_timing_span(
                "model.init", duration_ns=init,
                dedupe_key=(base + "model.init") if base else None,
                **_prune(provider=prov, cache_state=cache_state),
            )

    # -- tool call / result -------------------------------------------------- #
    def on_post_tool_call(
        self,
        *,
        tool_name: Optional[str] = None,
        args: Any = None,
        result: Any = None,
        tool_call_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        api_request_id: Optional[str] = None,
        duration_ms: int = 0,
        status: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        **_ignored: Any,
    ) -> Optional[Tuple[Optional[RunEvent], Optional[RunEvent]]]:
        """Persist a ``tool_call`` + ``tool_result`` pair for one dispatch.

        Both rows key on ``tool_call_id`` (in their respective categories) so
        concurrent calls are distinguished and a re-dispatch is idempotent.
        Returns ``(call_event, result_event)``; either may be ``None`` if that
        write failed (the other is still attempted).
        """
        call_id = (tool_call_id or "").strip() or None
        name = tool_name or ""

        call_event = self._emit_tool_call(
            call_id=call_id,
            name=name,
            args=args,
            turn_id=turn_id,
            api_request_id=api_request_id,
        )
        result_event = self._emit_tool_result(
            call_id=call_id,
            name=name,
            result=result,
            duration_ms=duration_ms,
            status=status,
            error_type=error_type,
            error_message=error_message,
        )
        # R8: a per-tool-call latency span for the percentile aggregator.
        try:
            dur = int(duration_ms)
        except (TypeError, ValueError):
            dur = None
        has_error = bool(error_message) or bool(error_type)
        norm = _normalise_tool_status(status, error_type, error_message, has_error)
        self._record_timing_span(
            "tool.call",
            duration_ns=(dur * 1_000_000) if dur is not None else None,
            ok=(norm == "ok"),
            dedupe_key=(f"{call_id}:tool.call" if call_id else None),
            **_prune(
                tool_name=(name[:128] if name else None),
                result=(norm[:128] if norm else None),
                reason_code=(str(error_type)[:128] if error_type else None),
            ),
        )
        return call_event, result_event

    def _emit_tool_call(
        self,
        *,
        call_id: Optional[str],
        name: str,
        args: Any,
        turn_id: Optional[str],
        api_request_id: Optional[str],
    ) -> Optional[RunEvent]:
        try:
            payload = {
                "tool_call_id": call_id,
                "tool_name": name,
                "args_redacted": redaction.redact_tool_args(name, args),
                "turn_id": turn_id,
                "api_request_id": (api_request_id or "").strip() or None,
            }
            return append_event(
                self.run_id,
                self.principal,
                "tool_call",
                "invoked",
                payload,
                correlation_id=self.correlation_id,
                dedupe_key=call_id,
                db_path=self.db_path,
            )
        except Exception as exc:
            logger.warning("run_observer tool_call emit failed: %s", exc,
                           exc_info=True)
            return None

    def _emit_tool_result(
        self,
        *,
        call_id: Optional[str],
        name: str,
        result: Any,
        duration_ms: int,
        status: Optional[str],
        error_type: Optional[str],
        error_message: Optional[str],
    ) -> Optional[RunEvent]:
        try:
            has_error = bool(error_message) or bool(error_type)
            norm_status = _normalise_tool_status(
                status, error_type, error_message, has_error
            )
            try:
                dur = int(duration_ms)
            except (TypeError, ValueError):
                dur = 0
            payload = {
                "tool_call_id": call_id,
                "tool_name": name,
                "status": norm_status,
                "error_type": (str(error_type) if error_type else None),
                "error_message_redacted": (
                    redaction.redact_error(error_message)
                    if error_message is not None else None
                ),
                "duration_ms": dur,
                "result_digest": redaction.redact_result(result),
            }
            return append_event(
                self.run_id,
                self.principal,
                "tool_result",
                norm_status,
                payload,
                correlation_id=self.correlation_id,
                dedupe_key=call_id,
                db_path=self.db_path,
            )
        except Exception as exc:
            logger.warning("run_observer tool_result emit failed: %s", exc,
                           exc_info=True)
            return None


# --------------------------------------------------------------------------- #
# Factory + registration.
# --------------------------------------------------------------------------- #
def create_run_observer(
    run_id: str,
    principal: Principal,
    *,
    correlation_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    cost_fn: CostFn = _default_cost_for_usage,
) -> RunObserver:
    """Build a :class:`RunObserver` bound to ``(run_id, principal)``."""
    return RunObserver(
        run_id=run_id,
        principal=principal,
        correlation_id=correlation_id,
        db_path=db_path,
        cost_fn=cost_fn,
    )


def principal_from_identity(identity: Any) -> Principal:
    """Build a :class:`Principal` from a runtime identity object.

    Accepts anything exposing ``tenant``/``user`` (e.g. ``RuntimeIdentity`` from
    ``web_routers/runtime.py``). Fail-closed on a missing/blank field, mirroring
    the journal's own principal invariant.
    """
    tenant = getattr(identity, "tenant", None)
    user = getattr(identity, "user", None)
    return Principal(tenant=str(tenant or ""), user=str(user or ""))


@dataclass(frozen=True)
class _Registration:
    """Handle returned by :func:`register_run_observer` for later removal."""

    observer: RunObserver
    _callbacks: Tuple[Tuple[str, Callable[..., Any]], ...]

    def unregister(self) -> None:
        unregister_run_observer(self)


def register_run_observer(observer: RunObserver) -> _Registration:
    """Wire an observer's methods into the plugin hook manager.

    Registers :meth:`RunObserver.on_post_api_request` for ``post_api_request``
    and :meth:`RunObserver.on_post_tool_call` for ``post_tool_call``. Returns a
    handle whose :meth:`_Registration.unregister` removes exactly these
    callbacks (so per-run observers can be torn down at run end without
    disturbing others). Best-effort: if the plugin manager is unavailable this
    raises ``RuntimeError`` so the caller can decide, rather than silently
    dropping observability.
    """
    try:
        from youtab_agent_cli import plugins as _plugins

        manager = _plugins.get_plugin_manager()
        hooks = manager._hooks  # noqa: SLF001 - intentional integration seam
    except Exception as exc:  # pragma: no cover - integration environment only
        raise RuntimeError(f"plugin hook manager unavailable: {exc}") from exc

    pairs = (
        ("post_api_request", observer.on_post_api_request),
        ("post_tool_call", observer.on_post_tool_call),
    )
    for hook_name, cb in pairs:
        hooks.setdefault(hook_name, []).append(cb)
    return _Registration(observer=observer, _callbacks=pairs)


def unregister_run_observer(registration: _Registration) -> None:
    """Remove the exact callbacks added by :func:`register_run_observer`."""
    try:
        from youtab_agent_cli import plugins as _plugins

        manager = _plugins.get_plugin_manager()
        hooks = manager._hooks  # noqa: SLF001
    except Exception:  # pragma: no cover - integration environment only
        return
    for hook_name, cb in registration._callbacks:
        bucket = hooks.get(hook_name)
        if not bucket:
            continue
        try:
            bucket.remove(cb)
        except ValueError:
            pass
