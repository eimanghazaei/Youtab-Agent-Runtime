"""A measurement must be labelled with the backend that actually ran.

``tools.wake_word`` coerces an explicit ``onnx`` engine to ``tflite`` on macOS
ARM64 (openWakeWord #336) and downgrades ``tflite`` to ``onnx`` when the tflite
runtime is missing, recording the truth in ``self.inference_framework``.
``evaluate_model`` used to build the engine and never read that back, so
``measure --backends onnx tflite`` on a coercing platform produced two tflite
runs labelled onnx/tflite and backend parity (target 5) found zero
disagreements by construction.

These tests drive the real ``evaluate_model.score_clips`` with a fake engine
whose reported ``inference_framework`` differs from the requested backend, and
assert the run is refused rather than scored under a false label.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

WAKEWORD = Path(__file__).resolve().parents[2] / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

em = importlib.import_module("evaluate_model")


class _FakeModel:
    def __init__(self) -> None:
        self.prediction_buffer = {"hey_youtab": [0.0]}


class _FakeEngine:
    """Enough of ``_OpenWakeWordEngine`` for ``score_clips`` to run one frame."""

    def __init__(self, actual_framework: str) -> None:
        self.inference_framework = actual_framework
        self._labels = ["hey_youtab"]
        self._model = _FakeModel()

    def reset(self) -> None:
        pass

    def process(self, frame) -> bool:
        return False


def _patch_engine(monkeypatch, actual_framework: str) -> None:
    monkeypatch.setattr(
        em, "_engine", lambda *a, **k: _FakeEngine(actual_framework)
    )


def test_a_coerced_backend_is_refused_not_scored_under_the_requested_label(monkeypatch):
    """onnx requested, tflite ran → refuse; the score never gets a false label."""
    _patch_engine(monkeypatch, actual_framework="tflite")
    audio = np.zeros((1, em.FRAME), dtype=np.int16)
    with pytest.raises(em.BackendFallbackRefused, match="onnx"):
        em.score_clips(audio, Path("hey_youtab.onnx"), "onnx", 0.6, 3, quiet=True)


def test_a_backend_that_ran_as_requested_is_accepted(monkeypatch):
    """Non-vacuity: a truthful label scores normally and does not raise."""
    _patch_engine(monkeypatch, actual_framework="onnx")
    audio = np.zeros((2, em.FRAME), dtype=np.int16)
    scores, fired = em.score_clips(
        audio, Path("hey_youtab.onnx"), "onnx", 0.6, 3, quiet=True
    )
    assert scores.shape == (2, 1)
    assert fired.tolist() == [False, False]


def test_the_helper_reports_the_real_framework_and_refuses_a_mismatch():
    assert em.engine_inference_framework(_FakeEngine("tflite"), "tflite") == "tflite"
    with pytest.raises(em.BackendFallbackRefused, match="never measured"):
        em.engine_inference_framework(_FakeEngine("tflite"), "onnx")
