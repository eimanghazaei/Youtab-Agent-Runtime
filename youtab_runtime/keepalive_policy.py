"""WAVE-30H task 5 — bounded, memory-aware, fail-closed Ollama keep_alive policy.

``model.ollama_keep_alive`` (WAVE-30E) was a raw pass-through: whatever the config
said was sent verbatim to the model server. Keeping a large model resident is the
point (it removes the ~7 s cold load between runs — see the keep_alive before/after
in the evidence bundle), but an UNBOUNDED or UNMANAGED residency is a memory-safety
hazard on a shared runtime: an "indefinite" pin, or several large models each pinned
for a long time, can exhaust host memory.

This policy sits between the configured value and the request:

  * BOUNDED  — a finite residency is clamped to a configurable ceiling; an
    "indefinite" (-1) request is never honoured unbounded — it is bounded to the
    ceiling (or downgraded under pressure).
  * MEMORY-AWARE — when available memory is below a configurable fraction, the
    residency is capped short (or evicted), so keep_alive never contributes to an
    OOM.
  * FAIL-CLOSED — any uncertainty (unparseable value, probe failure treated as
    "assume pressure" only when explicitly configured) resolves to the SAFE side:
    an invalid value omits keep_alive (server default), never a longer pin than
    asked.

Configuration (env, all optional):
  YOUTAB_ECO_KEEP_ALIVE_MAX_SECONDS   ceiling for a finite/indefinite pin (default 1800 = 30 min)
  YOUTAB_ECO_KEEP_ALIVE_MEM_MIN_AVAIL available-memory fraction below which we downgrade (default 0.15)
  YOUTAB_ECO_KEEP_ALIVE_PRESSURE_SECONDS  residency to use under pressure (default 300 = 5 min)
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        v = int(str(os.environ.get(name, default)).strip())
        return v
    except (ValueError, TypeError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (ValueError, TypeError):
        return default


def _max_seconds() -> int:
    return max(0, _int_env("YOUTAB_ECO_KEEP_ALIVE_MAX_SECONDS", 1800))


def _pressure_threshold() -> float:
    f = _float_env("YOUTAB_ECO_KEEP_ALIVE_MEM_MIN_AVAIL", 0.15)
    return min(max(f, 0.0), 1.0)


def _pressure_seconds() -> int:
    return max(0, _int_env("YOUTAB_ECO_KEEP_ALIVE_PRESSURE_SECONDS", 300))


def parse_seconds(value: Any) -> Optional[float]:
    """Parse a keep_alive value to seconds. None = unset/unparseable; -1 = indefinite;
    0 = evict; >0 = finite seconds. Accepts ``30m``/``90s``/``2h``/int/``-1``/``off``."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s == "":
        return None
    if s in ("-1", "indefinite", "forever", "inf"):
        return -1.0
    if s in ("0", "off", "none", "evict", "0s"):
        return 0.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smh]?)", s)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def available_memory_fraction(memory_probe: Optional[Callable[[], float]] = None) -> Optional[float]:
    """Fraction of host memory currently available (0..1), or None if unknown.
    A provided probe overrides psutil (used by tests to simulate pressure)."""
    if memory_probe is not None:
        try:
            return float(memory_probe())
        except Exception:  # noqa: BLE001 — probe failure => unknown
            return None
    try:
        import psutil

        vm = psutil.virtual_memory()
        return (vm.available / vm.total) if vm.total else None
    except Exception:  # noqa: BLE001 — psutil optional / unavailable
        return None


def resolve_keep_alive(configured: Any, *, memory_probe: Optional[Callable[[], float]] = None) -> Optional[Any]:
    """Resolve the effective keep_alive value to send to the model server, or None
    to OMIT the field entirely (server default).

    Bounded + memory-aware + fail-closed. Returns an int number of seconds, ``0``
    for evict, or None to omit. Never returns an unbounded/indefinite pin.
    """
    secs = parse_seconds(configured)
    if secs is None:
        # unset or unparseable -> omit (baseline behaviour). Never a longer pin
        # than requested; a garbage value must not be forwarded verbatim.
        if configured not in (None, "") and str(configured).strip() != "":
            logger.warning("keep_alive: unparseable value %r; omitting (server default)", configured)
        return None
    if secs == 0.0:
        return 0  # explicit evict — honoured

    ceiling = _max_seconds()
    avail = available_memory_fraction(memory_probe)
    pressure = avail is not None and avail < _pressure_threshold()

    if secs < 0:  # indefinite requested — NEVER honoured unbounded
        eff = (min(_pressure_seconds(), ceiling) if ceiling else 0) if pressure else ceiling
        logger.warning(
            "keep_alive: indefinite requested (pressure=%s); bounding to %ss "
            "(policy: no unbounded pin)", pressure, eff)
        return int(eff)

    if pressure and secs > _pressure_seconds():
        logger.info("keep_alive: memory pressure (avail=%.3f) -> capping residency to %ss",
                    avail, _pressure_seconds())
        return int(_pressure_seconds())
    if secs > ceiling:
        logger.info("keep_alive: %ss exceeds ceiling; bounding to %ss", secs, ceiling)
        return int(ceiling)
    # in-bounds and no pressure: forward the ORIGINAL value unchanged (format-
    # preserving — "30m" stays "30m"; a raw int stays an int).
    return configured
