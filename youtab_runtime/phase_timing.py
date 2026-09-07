"""WAVE-30F — process-lifetime phase timing for the per-run worker.

The Track A worker runs each run in a *freshly spawned* ``youtab chat -q`` OS
subprocess that handles one run and exits, so the ~13.3s of pre-first-token
overhead is dominated by cold Python process start + one-shot agent construction
(nothing is amortized across runs — worker pools are ADR-gated and out of scope).

To attribute that 13.3s at an authorized canary, this module records a handful of
monotonic milestones between process start and the first model send, then emits an
all-numeric ``lifecycle_phase_*_ms`` block on the first per-call timing event (so
it rides the same redaction-clean journal path as the native timings).

It is deliberately tiny and fail-soft: a mark can never raise into the hot startup
path, and ``T0`` is captured at *import* time — so importing this module as early
as possible in the worker entrypoint makes ``T0`` approximate process start.
"""

from __future__ import annotations

import re
import time
from typing import Dict

# Captured when this module is first imported. Import it early in the worker
# entrypoint so this approximates the process-start instant.
_T0 = time.monotonic()

# Ordered marks: phase name → elapsed ms since _T0.
_MARKS: "Dict[str, float]" = {}

# Marks must be safe snake_case identifiers so the emitted keys pass the run-journal
# timing sanitizer (which rejects any non-snake_case key).
_SAFE_MARK = re.compile(r"[a-z][a-z0-9_]{0,48}")


def mark(name: str) -> None:
    """Record the elapsed ms since process start for milestone ``name``.

    Fail-soft: an invalid name or any error is swallowed — lifecycle observability
    must never break the startup path. The first mark for a name wins is NOT
    enforced; the latest call updates it (marks are milestones, called once).
    """
    try:
        if not isinstance(name, str) or not _SAFE_MARK.fullmatch(name):
            return
        _MARKS[name] = round((time.monotonic() - _T0) * 1000.0, 3)
    except Exception:
        pass


def elapsed_ms() -> float:
    """Milliseconds since process start (import of this module)."""
    try:
        return round((time.monotonic() - _T0) * 1000.0, 3)
    except Exception:
        return 0.0


def snapshot(prefix: str = "lifecycle_") -> Dict[str, float]:
    """Return the recorded marks as an all-numeric ``{prefix}{name}_ms`` dict.

    Safe to merge into a per-call timing record and persist through the journal
    redaction chokepoint (numeric values, snake_case keys). Returns a copy.
    """
    out: Dict[str, float] = {}
    for name, ms in _MARKS.items():
        key = f"{prefix}{name}_ms"
        if _SAFE_MARK.fullmatch(name):
            out[key] = ms
    return out


def reset() -> None:
    """Clear recorded marks (test-only; does not reset T0)."""
    _MARKS.clear()
