"""WAVE-30H task 5 — bounded, memory-aware, fail-closed keep_alive policy."""
from __future__ import annotations

import pytest

from youtab_runtime import keepalive_policy as kp


def _hi():   # plenty of memory
    return 0.90


def _low():  # under the 0.15 default pressure threshold
    return 0.05


def test_unset_and_unparseable_omit():
    assert kp.resolve_keep_alive(None) is None
    assert kp.resolve_keep_alive("") is None
    assert kp.resolve_keep_alive("garbage!!") is None       # fail-closed: omit, never pin
    assert kp.resolve_keep_alive("30x") is None


def test_evict_and_in_bounds_preserved():
    assert kp.resolve_keep_alive(0) == 0
    assert kp.resolve_keep_alive("off") == 0
    # in-bounds finite value forwarded UNCHANGED (format-preserving), no pressure
    assert kp.resolve_keep_alive("30m", memory_probe=_hi) == "30m"
    assert kp.resolve_keep_alive(300, memory_probe=_hi) == 300


def test_indefinite_is_never_unbounded():
    # "-1" must never be forwarded verbatim — bounded to the ceiling.
    assert kp.resolve_keep_alive("-1", memory_probe=_hi) == 1800
    assert kp.resolve_keep_alive("indefinite", memory_probe=_hi) == 1800


def test_over_ceiling_is_clamped():
    assert kp.resolve_keep_alive("2h", memory_probe=_hi) == 1800     # 7200 -> 1800
    assert kp.resolve_keep_alive(999999, memory_probe=_hi) == 1800


def test_memory_pressure_caps_residency():
    # under pressure, a long residency is capped to the pressure window (300s)
    assert kp.resolve_keep_alive("30m", memory_probe=_low) == 300
    assert kp.resolve_keep_alive("-1", memory_probe=_low) == 300
    # a short residency under the pressure window is untouched
    assert kp.resolve_keep_alive(120, memory_probe=_low) == 120


def test_configurable_ceiling(monkeypatch):
    monkeypatch.setenv("YOUTAB_ECO_KEEP_ALIVE_MAX_SECONDS", "600")
    assert kp.resolve_keep_alive("30m", memory_probe=_hi) == 600     # 1800 -> 600 ceiling


def test_probe_failure_does_not_assume_pressure():
    # a probe that raises => unknown => NOT treated as pressure (in-bounds preserved)
    def _boom():
        raise RuntimeError("no psutil")
    assert kp.resolve_keep_alive("5m", memory_probe=_boom) == "5m"


# ── real managed provider path under simulated memory pressure (gate 4) ───────


class _VM:
    def __init__(self, avail_frac):
        self.total = 16 * 1024**3
        self.available = int(self.total * avail_frac)


def _custom_profile():
    from providers import get_provider_profile
    prof = get_provider_profile("custom") or get_provider_profile("ollama")
    assert prof is not None
    return prof


def _extras(prof, monkeypatch, avail_frac, keep_alive):
    monkeypatch.setattr("psutil.virtual_memory", lambda: _VM(avail_frac))
    return prof.build_api_kwargs_extras(ollama_num_ctx=8192, ollama_keep_alive=keep_alive,
                                        reasoning_config={"enabled": False})


def test_provider_path_normal_memory_preserves_residency(monkeypatch):
    prof = _custom_profile()
    extra, _ = _extras(prof, monkeypatch, 0.90, "30m")  # plenty of memory
    assert extra["keep_alive"] == "30m"                 # bounded residency, unchanged
    # capability/output behaviour unchanged by keep_alive (num_ctx + think still set)
    assert extra["options"]["num_ctx"] == 8192
    assert extra["think"] is False


def test_provider_path_memory_pressure_caps_residency(monkeypatch):
    prof = _custom_profile()
    extra, _ = _extras(prof, monkeypatch, 0.05, "30m")  # under pressure
    assert extra["keep_alive"] == 300                   # capped — no uncontrolled pinning
    assert extra["options"]["num_ctx"] == 8192          # capability unchanged
    assert extra["think"] is False


def test_provider_path_indefinite_never_unbounded_even_normal(monkeypatch):
    prof = _custom_profile()
    extra, _ = _extras(prof, monkeypatch, 0.90, "-1")   # request an indefinite pin
    assert extra["keep_alive"] == 1800                  # bounded to ceiling, never -1
    extra2, _ = _extras(prof, monkeypatch, 0.05, "-1")  # indefinite under pressure
    assert extra2["keep_alive"] == 300                  # capped further — no OOM risk


def test_provider_path_invalid_fails_closed_omits(monkeypatch):
    prof = _custom_profile()
    extra, _ = _extras(prof, monkeypatch, 0.90, "garbage")
    assert "keep_alive" not in extra                    # fail-closed: omit, never pin
    assert extra["options"]["num_ctx"] == 8192          # rest of the request intact
