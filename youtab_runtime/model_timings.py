"""WAVE-30E — native model timing extraction + redaction-safe timing records.

The local ECO/Qwen canary (``t_799c219d``) was instrumentation-blind: the Ollama
adapter discarded every native timing field, so ``load_duration`` /
``prompt_eval`` / ``eval`` / ``total_duration`` / TTFT were all UNAVAILABLE and
the pipeline could not be tuned with data.

This module is the pure, dependency-light core of the fix. It:

* extracts the **native Ollama timing fields** when the provider returns them —
  ``total_duration``, ``load_duration``, ``prompt_eval_count``,
  ``prompt_eval_cached_count``, ``prompt_eval_duration``, ``eval_count``,
  ``eval_duration`` — converting the **nanosecond** duration fields to
  milliseconds with no fabrication. IMPORTANT (WAVE-30F): these fields are emitted
  only by Ollama's **native** endpoints (``/api/chat`` / ``/api/generate``). The
  runtime's live traffic uses Ollama's **OpenAI-compat** endpoint
  (``/v1/chat/completions``), which does NOT carry them, so on the live path this
  extractor correctly returns ``{}`` and the record is marked
  ``native_timings_available: False``. Authoritative native timings for the exact
  runtime prompt are obtained out-of-band by :mod:`youtab_runtime.ollama_native`
  (the controlled bottleneck-attribution matrix), whose ``/api/chat`` raw timing
  dict is fed straight through this same extractor. See
  https://github.com/ollama/ollama/blob/main/docs/api.md;
* derives ``prompt_eval_tokens_per_second`` / ``eval_tokens_per_second`` **only**
  when both the matching count and duration are present (never estimated);
* assembles a single **all-numeric** timing record (wall-clock + optional TTFT +
  native fields) tagged ``cold`` vs ``warm`` and ``native_timings_available``,
  so it passes the run-journal redaction chokepoint by construction (numeric
  values under any key are preserved; no prompt/response/secret text is carried).

Honesty rules baked in:
* durations are exact when the API provides them; otherwise the field is absent —
  never guessed;
* wall-clock (``wall_ms``) is the runtime's own measurement and is labelled as
  such, distinct from the model's native ``total_duration_ms``;
* ``cold_start`` is a caller-supplied fact, never inferred from wall-clock.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Mapping, Optional

# A timing KEY may only be a lowercase snake_case identifier from this module's
# fixed vocabulary shape. This makes the "no free-text" guarantee cover keys too:
# a hostile mapping like ``{"Bearer sk-live-...": 42}`` (secret in a JSON key)
# is rejected, not persisted, even though its value is numeric.
_SAFE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Above this native ``load_duration`` (Ollama's own model-load timer, NOT
# wall-clock) a call is treated as a COLD start (the model had to be loaded into
# memory this call); at/below it the model was already resident (WARM). Using the
# native load timer keeps cold/warm labeling honest and off the wall-clock.
COLD_LOAD_THRESHOLD_MS = 1000.0

# Native Ollama duration fields are reported in NANOSECONDS.
_NATIVE_NS_FIELDS = (
    "total_duration",
    "load_duration",
    "prompt_eval_duration",
    "eval_duration",
)
# Native Ollama token-count fields (already integers).
_NATIVE_COUNT_FIELDS = (
    "prompt_eval_count",
    "prompt_eval_cached_count",
    "eval_count",
)

_NS_PER_MS = 1_000_000
_NS_PER_S = 1_000_000_000


def _is_finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from a mapping, a pydantic-style object, or its ``model_extra``.

    Ollama surfaces native timings either as top-level response attributes or
    (for unknown OpenAI-compat extensions) inside the SDK model's ``model_extra``.
    We probe all of these read paths without importing the SDK.
    """
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    # pydantic v2 stashes unknown fields in model_extra (a dict) — check it first
    # so a real extension field is found even when it is not a declared attribute.
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, Mapping) and key in extra:
        return extra[key]
    return getattr(obj, key, None)


def extract_native_ollama_timings(response: Any) -> Dict[str, Any]:
    """Return the native Ollama timing fields present on ``response``.

    Looks at the response object itself and its nested ``usage`` (native fields
    can ride on either, depending on Ollama version / proxy). Duration fields are
    converted ns → ms as ``<name>_ms``; count fields are copied verbatim. Derived
    throughput is included only when its count AND duration are both present.

    Returns ``{}`` when the provider exposes no native timing field (the common
    case for the stock OpenAI-compat endpoint) — the caller then records only its
    own wall-clock, and marks native timings unavailable. Never raises.
    """
    out: Dict[str, Any] = {}
    if response is None:
        return out
    usage = _get(response, "usage")
    sources = (response, usage)

    def _first_present(key: str) -> Any:
        for src in sources:
            val = _get(src, key)
            if val is not None:
                return val
        return None

    for field in _NATIVE_NS_FIELDS:
        raw = _first_present(field)
        if _is_finite_number(raw) and raw >= 0:
            out[f"{field}_ms"] = round(float(raw) / _NS_PER_MS, 3)
    for field in _NATIVE_COUNT_FIELDS:
        raw = _first_present(field)
        if _is_finite_number(raw) and raw >= 0:
            out[field] = int(raw)

    # Derived throughput — ONLY when both legs are present, from native ns values
    # (not the rounded ms) to avoid compounding rounding. Never estimated.
    prompt_eval_count = _first_present("prompt_eval_count")
    prompt_eval_duration = _first_present("prompt_eval_duration")
    if (
        _is_finite_number(prompt_eval_count) and prompt_eval_count > 0
        and _is_finite_number(prompt_eval_duration) and prompt_eval_duration > 0
    ):
        out["prompt_eval_tokens_per_second"] = round(
            float(prompt_eval_count) / (float(prompt_eval_duration) / _NS_PER_S), 3
        )
    eval_count = _first_present("eval_count")
    eval_duration = _first_present("eval_duration")
    if (
        _is_finite_number(eval_count) and eval_count > 0
        and _is_finite_number(eval_duration) and eval_duration > 0
    ):
        out["eval_tokens_per_second"] = round(
            float(eval_count) / (float(eval_duration) / _NS_PER_S), 3
        )
    return out


def sanitize_timings(raw: Any) -> Dict[str, Any]:
    """Keep only finite, non-negative numeric leaves under safe snake_case keys.

    A hard guarantee that a timing payload can carry NO string/secret/free-text in
    EITHER a value OR a key: any non-numeric/negative value is dropped, and any key
    that is not a lowercase snake_case identifier is dropped. Booleans that are
    semantic flags (``cold_start``, ``native_timings_available``) are preserved as
    the only non-numeric value exception because they are safe fixed-vocabulary
    booleans.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for key, val in raw.items():
        # Keys must be a safe snake_case identifier — no free text can ride in a
        # key any more than in a value.
        if not isinstance(key, str) or not _SAFE_KEY.fullmatch(key):
            continue
        if key in ("cold_start", "native_timings_available", "process_cold"):
            out[key] = bool(val)
            continue
        if _is_finite_number(val) and val >= 0:
            # Preserve ints as ints, floats as floats.
            out[key] = int(val) if isinstance(val, int) else float(val)
    return out


def build_call_timings(
    *,
    wall_s: Optional[float] = None,
    ttft_s: Optional[float] = None,
    native: Optional[Mapping[str, Any]] = None,
    cold_start: Optional[bool] = None,
) -> Dict[str, Any]:
    """Assemble one all-numeric timing record for a single model call.

    * ``wall_ms`` — the runtime's own end-to-end wall-clock for the call
      (distinct from the model's native ``total_duration_ms``).
    * ``ttft_ms`` — real streaming time-to-first-token, when measured.
    * native Ollama fields (already ns→ms) merged verbatim.
    * ``cold_start`` — caller-supplied fact (cold vs warm), never inferred here.
    * ``native_timings_available`` — True iff any native field was captured.

    All values pass through :func:`sanitize_timings`, so the returned dict is
    safe to persist directly to the run journal.
    """
    rec: Dict[str, Any] = {}
    if _is_finite_number(wall_s) and wall_s >= 0:
        rec["wall_ms"] = round(float(wall_s) * 1000.0, 3)
    if _is_finite_number(ttft_s) and ttft_s >= 0:
        rec["ttft_ms"] = round(float(ttft_s) * 1000.0, 3)
    native_clean = sanitize_timings(native or {})
    rec.update(native_clean)
    rec["native_timings_available"] = bool(native_clean)
    # Cold/warm: an explicit caller fact wins; otherwise derive it from the NATIVE
    # load_duration (never from wall-clock). When neither is available the label
    # is omitted rather than guessed.
    if cold_start is not None:
        rec["cold_start"] = bool(cold_start)
    elif "load_duration_ms" in native_clean:
        rec["cold_start"] = native_clean["load_duration_ms"] > COLD_LOAD_THRESHOLD_MS
    return sanitize_timings(rec)
