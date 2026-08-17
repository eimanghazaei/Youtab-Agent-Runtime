"""Thin capture / playback / guidance wrappers over the audio stack.

Deliberately small: all logic worth testing is in ``core``. This module only
touches hardware, and it imports ``sounddevice`` and ``pyttsx3`` *lazily inside
functions* so that ``core`` and its tests import with no audio stack present.

Two rules are load-bearing and live in how these are called, not here:

* Guidance is spoken to the output device and is **never captured or written**.
  ``speak_prompt`` plays through the speakers; ``MicRecorder`` reads the
  microphone; nothing routes one into the other.
* ``speak_prompt`` blocks (``pyttsx3.runAndWait`` is blocking) and returns
  before a caller starts the microphone, so the prompt and the recording never
  overlap. The sequencing is the caller's (``core.Session.record`` /
  ``app``), which is why it is testable without hardware.
"""

from __future__ import annotations

import importlib.util

import numpy as np

from .core import SAMPLE_RATE_HZ, validate_sample_rate


def audio_backends() -> dict[str, bool]:
    """Which optional backends are importable, without importing them."""
    return {
        "sounddevice": importlib.util.find_spec("sounddevice") is not None,
        "pyttsx3": importlib.util.find_spec("pyttsx3") is not None,
    }


def _import_sounddevice():
    try:
        import sounddevice  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - hardware/environment dependent
        raise RuntimeError(
            "sounddevice is required to record or play audio. Install the "
            "operator requirements: pip install -r requirements-recording-assistant.txt"
        ) from exc
    return sounddevice


def speak_prompt(text: str, *, enabled: bool = True) -> bool:
    """Speak ``text`` through the output device and block until it finishes.

    Returns True if it spoke, False if guidance is disabled or ``pyttsx3`` is
    not installed (guidance is optional; the on-screen phrase is always shown).
    Never records and never returns audio: the spoken prompt exists only on the
    speakers.
    """
    if not enabled or not text.strip():
        return False
    try:
        import pyttsx3  # noqa: PLC0415
    except Exception:  # pragma: no cover - optional dependency
        return False
    engine = pyttsx3.init()
    try:
        engine.say(text)
        engine.runAndWait()  # blocking: returns before the caller records
    finally:
        try:
            engine.stop()
        except Exception:  # pragma: no cover - driver dependent
            pass
    return True


class MicRecorder:
    """Start/stop microphone capture into a mono 16-bit buffer.

    ``start()`` opens an input stream and accumulates frames; ``stop()`` closes
    it and returns ``(int16 samples, rate)`` -- exactly what was captured, with
    no resampling. The rate is refused below 16 kHz, the app's floor.
    """

    def __init__(self, rate: int = SAMPLE_RATE_HZ, device=None) -> None:
        validate_sample_rate(rate)
        self.rate = rate
        self.device = device
        self._frames: list[np.ndarray] = []
        self._stream = None

    def start(self) -> None:
        sd = _import_sounddevice()
        self._frames = []

        def _callback(indata, _frames, _time, _status) -> None:
            self._frames.append(np.asarray(indata, dtype=np.int16).reshape(-1).copy())

        self._stream = sd.InputStream(
            samplerate=self.rate,
            channels=1,
            dtype="int16",
            device=self.device,
            callback=_callback,
        )
        self._stream.start()

    def stop(self) -> tuple[np.ndarray, int]:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._frames:
            return np.zeros(0, dtype=np.int16), self.rate
        return np.concatenate(self._frames).astype(np.int16), self.rate


def play_array(pcm, rate: int = SAMPLE_RATE_HZ) -> None:
    """Play captured samples back through the output device (blocking)."""
    sd = _import_sounddevice()
    sd.play(np.asarray(pcm, dtype=np.int16), rate)
    sd.wait()
