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

from .core import MIN_SAMPLE_RATE_HZ, SAMPLE_RATE_HZ, validate_sample_rate


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


# ── input-device selection ───────────────────────────────────────────────────
# On Windows a machine commonly exposes, besides its real microphone, a set of
# *virtual* inputs: vendor "AI noise-cancelling" capture devices, loopbacks and
# mixers. The dataset must be the raw microphone -- the Owner's rule is no
# denoising, no processing -- so auto-selection skips anything whose name marks
# it as one of those. An explicit ``--input-device`` (index or name) overrides
# this and is honoured exactly as given.

_NON_RAW_INPUT_MARKERS = (
    "noise", "cancel", "denoise", "clarity", "enhance", "virtual", "vac",
    "voicemeeter", "cable", "loopback", "stereo mix", "what u hear",
    "wave out", "mapper", "aggregate", "multi-output", "mix ",
    "speaker", "output", "playback",
)


def list_input_devices() -> list[dict]:
    """Every capture-capable device as a plain dict; no hardware is opened."""
    sd = _import_sounddevice()
    hostapis = sd.query_hostapis()
    devices = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            devices.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "channels": int(dev["max_input_channels"]),
                    "default_samplerate": int(dev.get("default_samplerate") or 0),
                    "hostapi": hostapis[dev["hostapi"]]["name"],
                    "raw": _looks_raw(dev["name"]),
                }
            )
    return devices


def _looks_raw(name: str) -> bool:
    """True if the device name does not mark it as virtual/processed."""
    low = name.lower()
    return not any(marker in low for marker in _NON_RAW_INPUT_MARKERS)


def resolve_input_device(preference=None) -> tuple[int, str]:
    """Return ``(index, name)`` of the capture device to use.

    With ``preference`` (an index or a case-insensitive name fragment) that
    device is used as given -- the operator's choice is never second-guessed.
    Without one, a raw hardware microphone is chosen, skipping virtual and
    noise-cancelling inputs, preferring a device that names itself a microphone.
    Raises ``RuntimeError`` with actionable guidance when nothing is usable.
    """
    devices = list_input_devices()
    if not devices:
        raise RuntimeError(
            "no microphone (input device) is available. Enable or plug in a "
            "microphone in Windows Sound settings, then retry. (--list-devices "
            "shows what the machine exposes.)"
        )
    if preference is not None and str(preference).strip() != "":
        pref = str(preference).strip()
        if pref.lstrip("-").isdigit():
            want = int(pref)
            for dev in devices:
                if dev["index"] == want:
                    return dev["index"], dev["name"]
            raise RuntimeError(
                f"--input-device {want} is not a capture device on this machine. "
                "Run --list-devices for the valid indices."
            )
        low = pref.lower()
        matches = [dev for dev in devices if low in dev["name"].lower()]
        if not matches:
            raise RuntimeError(
                f"no input device name contains {pref!r}. Run --list-devices."
            )
        return matches[0]["index"], matches[0]["name"]

    # Prefer the OS default input when one is set and it is a raw mic: that is
    # the microphone the operator chose in Windows Sound settings. (On a machine
    # with no default input set, this index is -1 and we fall through to name
    # ranking.)
    sd = _import_sounddevice()
    try:
        default_in = int(sd.default.device[0])
    except Exception:  # pragma: no cover - backend dependent
        default_in = -1
    by_index = {dev["index"]: dev for dev in devices}
    if default_in in by_index and by_index[default_in]["raw"]:
        chosen = by_index[default_in]
        return chosen["index"], chosen["name"]

    pool = [dev for dev in devices if dev["raw"]] or devices

    def rank(dev):
        low = dev["name"].lower()
        return (
            "array" in low or "microphone" in low or low.startswith("mic")
            or " mic" in low,
        )

    chosen = sorted(pool, key=rank, reverse=True)[0]
    return chosen["index"], chosen["name"]


def negotiate_rate(device, requested: int, floor: int = MIN_SAMPLE_RATE_HZ) -> int:
    """The highest-preference mono 16-bit rate the device accepts, at or above the floor.

    Tries the requested rate first (the Owner prefers 48 kHz), then common rates,
    never below the 16 kHz floor and never resampling: the returned rate is one
    the hardware delivers natively.
    """
    sd = _import_sounddevice()
    seen = []
    for rate in (int(requested), 48000, 44100, 32000, int(floor)):
        if rate < floor or rate in seen:
            continue
        seen.append(rate)
        try:
            sd.check_input_settings(
                device=device, channels=1, samplerate=rate, dtype="int16"
            )
            return rate
        except Exception:  # pragma: no cover - hardware dependent
            continue
    raise RuntimeError(
        f"input device {device} accepts no mono 16-bit rate at or above "
        f"{floor} Hz. Try a different --input-device."
    )


def capture_fixed(seconds: float, rate: int, device=None) -> tuple[np.ndarray, int]:
    """Record exactly ``seconds`` from ``device`` and return ``(int16, rate)``.

    A blocking, fixed-length capture for the device self-check. Raw samples only
    -- no resampling and no gain, exactly like ``MicRecorder``.
    """
    import time  # noqa: PLC0415

    recorder = MicRecorder(rate=rate, device=device)
    recorder.start()
    try:
        time.sleep(max(0.0, float(seconds)))
    finally:
        pcm, actual = recorder.stop()
    return pcm, actual


def import_from_here():  # pragma: no cover - convenience for headless callers
    """Import ``sounddevice`` eagerly so a missing backend fails early and clearly."""
    return _import_sounddevice()
