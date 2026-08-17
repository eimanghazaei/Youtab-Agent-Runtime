"""The capture path, from what the device hands over to what the engine scores.

``test_wake_word.py`` drives the detector's *control* flow — start, pause, fire,
cooldown, ownership — with streams that return ``[0] * n``. That leaves the part
between the microphone and ``engine.process()`` unasserted, and it is the part
that differs on every operating system:

* the format the stream is opened at (16 kHz, one channel, int16), which is a
  *request* PortAudio and the OS satisfy by converting — this module converts
  nothing itself, so if the request is wrong the model is fed the wrong audio;
* the ``(frames, channels)`` array ``sounddevice`` actually returns, which is
  two-dimensional even for mono, so ``data[:, 0]`` is the normal path rather
  than an edge case;
* the seam between one delivered chunk and the next — whether any audio is lost
  or repeated when the device's chunk size is not a multiple of the 1280-sample
  (80 ms) engine frame;
* the int16 sample scale, where being wrong is silent: this module divides by
  32768 itself, so float samples in -1..1 arrive 32768x too quiet, every score
  collapses into a flat band near zero, and it reads exactly like a dead model.

Everything here drives the real ``WakeWordDetector._run`` loop. Only the device
is stood in, and the stand-ins model PortAudio's contract rather than a
convenient simplification: ``read(n)`` assembles exactly ``n`` frames out of
awkwardly sized internal chunks, which is what the real one does.

numpy is imported unconditionally, for the reason
``test_wake_word_model_assets.py`` records: it reaches this job as a transitive
of the branding gate's OCR engine, so a missing one is a broken environment and
a skip would hide the only test of the capture format.
"""

from __future__ import annotations

import inspect
import itertools
import logging
import sys
import types

import numpy as np
import pytest

import tools.wake_word as ww

from .test_wake_word import _poll_until

FRAME = ww._OpenWakeWordEngine.frame_length          # 1280 samples, 80 ms


def _source(samples: int, seed: int = 7) -> np.ndarray:
    """A fixed int16 signal. Local Generator, never the global RNG."""
    return np.random.default_rng(seed).integers(-8000, 8000, samples, dtype=np.int16)


# ── device stand-ins ─────────────────────────────────────────────────────────


class _SourceExhausted(OSError):
    """Ends a test cleanly through the runtime's own stream-read error path."""


class _RingStream:
    """A device that delivers awkward chunks behind an exact ``read(n)``.

    This is PortAudio's contract. A host API delivers whatever chunk size it
    likes — 480 samples on a 48 kHz CoreAudio callback, 441 under MME — and
    ``InputStream.read(n)`` hides that by not returning until it has assembled
    exactly ``n`` frames from its ring buffer. ``CHUNK`` is coprime with the
    1280-sample engine frame on purpose, so almost every read spans a chunk
    seam and a runtime that dropped or repeated a remainder would show up as a
    discontinuity in what the engine received.
    """

    CHUNK = 373

    def __init__(self, source: np.ndarray, channels: int = 1, **_kw):
        self.source = source
        self.channels = channels
        self.closed = False
        self.started = False
        self._cursor = 0
        self._buffer = np.empty(0, dtype=np.int16)

    def start(self):
        self.started = True

    def _pull(self) -> None:
        chunk = self.source[self._cursor:self._cursor + self.CHUNK]
        if not len(chunk):
            raise _SourceExhausted("fixture source exhausted")
        self._cursor += len(chunk)
        self._buffer = np.concatenate([self._buffer, chunk])

    def read(self, n):
        while len(self._buffer) < n:
            self._pull()
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        if self.channels == 1:
            return out.reshape(-1, 1), False
        # A device that ignored channels=1: channel 0 is the signal, the rest is
        # something the runtime must not mix in.
        other = np.full(len(out), 32000, dtype=np.int16)
        return np.stack([out, other], axis=1), False

    def stop(self):
        pass

    def close(self):
        self.closed = True


class _ShortReadStream(_RingStream):
    """A ``read(n)`` that under-delivers: the frame boundary is not respected.

    ``sounddevice`` fills the whole request, but the runtime does not verify it,
    so this pins what happens if a wrapper or a host ever short-reads: the
    engine gets a frame that is not 80 ms, and the audit question is whether
    anything is lost at that seam.
    """

    def read(self, n):
        if not len(self._buffer):
            self._pull()
        take = min(n, len(self._buffer))
        out, self._buffer = self._buffer[:take], self._buffer[take:]
        return out.reshape(-1, 1), False


class _FloatStream(_RingStream):
    """The contract violation: correct audio, wrong scale.

    Loud int16 audio handed over as float32 in -1..1 — what a caller gets by
    "just" changing ``dtype``, or by feeding ``soundfile``/``librosa`` output
    straight into the engine.
    """

    def read(self, n):
        data, overflow = super().read(n)
        return (data.astype(np.float32) / 32768.0), overflow


class _RecordingEngine:
    """Records every frame the detector hands over, verbatim."""

    frame_length = FRAME

    def __init__(self):
        self.frames: list[np.ndarray] = []
        self.resets = 0
        self.closed = False

    def process(self, frame) -> bool:
        self.frames.append(np.asarray(frame).copy())
        return False

    def reset(self) -> None:
        self.resets += 1

    def close(self) -> None:
        self.closed = True

    @property
    def captured(self) -> np.ndarray:
        return (np.concatenate(self.frames) if self.frames
                else np.empty(0, dtype=np.int16))


def _fake_audio(monkeypatch, stream_factory):
    """Install a fake ``sounddevice`` and return the list of open() kwargs."""
    opened: list[dict] = []

    def _input_stream(**kwargs):
        opened.append(dict(kwargs))
        return stream_factory(**kwargs)

    monkeypatch.setattr(
        ww, "_import_audio",
        lambda: (types.SimpleNamespace(InputStream=_input_stream), np),
    )
    return opened


def _drain(detector, engine, source_len):
    """Let the detector consume a finite source, then stop it."""
    frames = source_len // FRAME
    _poll_until(
        lambda: len(engine.frames) >= frames or not detector.running,
        f"the detector to consume {frames} frames of the fixture source",
    )
    detector.stop()


# ── the requested capture format ─────────────────────────────────────────────


def test_the_stream_is_opened_as_16k_mono_int16_in_one_frame_blocks(monkeypatch):
    """The whole of this module's sample-rate and channel handling is this call.

    Nothing downstream resamples or downmixes, so these five arguments *are* the
    conversion contract: get one wrong and the model is scored on audio it was
    never trained for, with no error anywhere.
    """
    engine = _RecordingEngine()
    opened = _fake_audio(monkeypatch, lambda **kw: _RingStream(_source(FRAME * 4), **kw))
    detector = ww.WakeWordDetector(engine, lambda: None, input_device=3)
    detector.start()
    try:
        assert opened == [{
            "device": 3,
            "samplerate": 16000,
            "channels": 1,
            "dtype": "int16",
            "blocksize": FRAME,
        }]
    finally:
        detector.stop()

    # ...and the constants those arguments come from.
    assert ww.SAMPLE_RATE == 16000
    assert FRAME == 1280                      # 80 ms at 16 kHz
    assert FRAME / ww.SAMPLE_RATE == 0.08
    assert ww.FRAME_DTYPE == "int16"


def test_the_block_size_asked_of_the_device_is_the_engine_frame(monkeypatch):
    """A device that honours ``blocksize`` delivers exactly one frame per read.

    Porcupine reports its own ``frame_length`` (512 samples), so this has to
    follow the engine rather than the openWakeWord constant.
    """
    engine = _RecordingEngine()
    engine.frame_length = 512
    opened = _fake_audio(monkeypatch, lambda **kw: _RingStream(_source(2048), **kw))
    detector = ww.WakeWordDetector(engine, lambda: None)
    detector.start()
    try:
        assert opened[0]["blocksize"] == 512
    finally:
        detector.stop()


# ── chunk boundaries: is anything dropped or duplicated at the seam? ─────────


def test_no_audio_is_dropped_or_duplicated_at_a_chunk_seam(monkeypatch):
    """The engine receives the device's samples exactly once, in order.

    The device produces 373-sample chunks — coprime with the 1280-sample frame,
    so the boundaries never line up — and every read has to be assembled across
    them. Concatenating what the engine was handed must reproduce the source
    exactly: a dropped remainder shows up as a gap, a re-fed one as a repeat,
    and either would put a seam in the middle of a spoken phrase.
    """
    source = _source(FRAME * 12)
    engine = _RecordingEngine()
    _fake_audio(monkeypatch, lambda **kw: _RingStream(source, **kw))
    detector = ww.WakeWordDetector(engine, lambda: None)
    detector.start()
    _drain(detector, engine, len(source))

    captured = engine.captured
    assert len(captured) >= FRAME * 11, "the fixture source was barely consumed"
    assert np.array_equal(captured, source[:len(captured)])
    assert all(len(f) == FRAME for f in engine.frames)
    # Non-vacuity: the seams have to actually be crossed for this to mean
    # anything.
    assert _RingStream.CHUNK % FRAME != 0 and FRAME % _RingStream.CHUNK != 0
    assert len(engine.frames) > 1


def test_a_short_read_loses_nothing_either(monkeypatch):
    """A device that under-delivers gives short frames, but no missing audio.

    ``read(n)`` is the only re-blocking this runtime does — it never buffers a
    remainder itself. If a read returns fewer samples than the frame, the next
    read continues from where it stopped, so the stream stays gapless; the
    engine simply sees a frame shorter than 80 ms. openWakeWord carries that
    sub-frame remainder internally (``AudioFeatures._streaming_features``
    concatenates ``raw_data_remainder`` onto the next chunk), so nothing is lost
    downstream either.
    """
    source = _source(FRAME * 8)
    engine = _RecordingEngine()
    _fake_audio(monkeypatch, lambda **kw: _ShortReadStream(source, **kw))
    detector = ww.WakeWordDetector(engine, lambda: None)
    detector.start()
    _drain(detector, engine, len(source))

    captured = engine.captured
    assert len(captured) >= FRAME * 6
    assert np.array_equal(captured, source[:len(captured)])
    # Non-vacuity: the short reads really did break the frame alignment.
    assert any(len(f) != FRAME for f in engine.frames)


def test_the_stream_is_closed_when_a_read_finally_fails(monkeypatch):
    """The read-error exit path still closes the device.

    ``_SourceExhausted`` is an OSError, so draining the fixture leaves the
    detector on exactly the branch a disconnected USB microphone takes.
    """
    source = _source(FRAME * 3)
    engine = _RecordingEngine()
    streams: list[_RingStream] = []

    def _factory(**kw):
        stream = _RingStream(source, **kw)
        streams.append(stream)
        return stream

    _fake_audio(monkeypatch, _factory)
    detector = ww.WakeWordDetector(engine, lambda: None)
    detector.start()
    _poll_until(lambda: not detector.running, "the read error to end the loop")
    assert streams[0].closed is True
    detector.stop()
    assert engine.closed is True


# ── channels ─────────────────────────────────────────────────────────────────


def test_a_two_channel_delivery_takes_channel_zero_and_mixes_nothing(monkeypatch):
    """``data[:, 0]``, asserted rather than assumed.

    ``channels=1`` is requested, so this only happens on a host that ignores
    the request — but the branch exists, and what it must NOT do is average, or
    read the interleaved buffer flat. The second channel here is a constant
    32000: an averaged or flattened frame would be visibly contaminated by it.
    """
    source = _source(FRAME * 6)
    engine = _RecordingEngine()

    def _factory(**kw):
        kw.pop("channels", None)
        return _RingStream(source, channels=2, **kw)

    _fake_audio(monkeypatch, _factory)
    detector = ww.WakeWordDetector(engine, lambda: None)
    detector.start()
    _drain(detector, engine, len(source))

    captured = engine.captured
    assert len(captured) >= FRAME * 5
    assert np.array_equal(captured, source[:len(captured)])
    assert 32000 not in set(captured.tolist())


def test_a_one_dimensional_delivery_is_passed_through(monkeypatch):
    """Frames arriving already flat must not be indexed as if 2-D."""
    engine = _RecordingEngine()

    class _FlatStream(_RingStream):
        def read(self, n):
            data, overflow = super().read(n)
            return data.reshape(-1), overflow

    source = _source(FRAME * 4)
    _fake_audio(monkeypatch, lambda **kw: _FlatStream(source, **kw))
    detector = ww.WakeWordDetector(engine, lambda: None)
    detector.start()
    _drain(detector, engine, len(source))
    assert np.array_equal(engine.captured, source[:len(engine.captured)])
    assert all(f.ndim == 1 for f in engine.frames)


# ── the int16 sample-scale contract ──────────────────────────────────────────


def test_the_frames_reaching_the_engine_are_int16(monkeypatch):
    source = _source(FRAME * 4)
    engine = _RecordingEngine()
    _fake_audio(monkeypatch, lambda **kw: _RingStream(source, **kw))
    detector = ww.WakeWordDetector(engine, lambda: None)
    detector.start()
    _drain(detector, engine, len(source))
    assert engine.frames
    assert all(str(f.dtype) == ww.FRAME_DTYPE for f in engine.frames)


@pytest.mark.parametrize("dtype", ["float32", "float64", "int32", "uint8"])
def test_only_int16_satisfies_the_contract(monkeypatch, dtype):
    monkeypatch.setattr(ww, "_warned_frame_dtype", False)
    assert ww.frame_dtype_ok(np.zeros(4, dtype=dtype)) is False


def test_frame_dtype_ok_accepts_int16_and_untyped(monkeypatch):
    monkeypatch.setattr(ww, "_warned_frame_dtype", False)
    assert ww.frame_dtype_ok(np.zeros(FRAME, dtype=np.int16)) is True
    # Plain lists have no dtype to judge; the check is about float feeds, not
    # about typing, and every test double in this suite hands over a list.
    assert ww.frame_dtype_ok([0] * FRAME) is True


def test_the_dtype_violation_is_logged_once_and_names_the_symptom(monkeypatch, caplog):
    monkeypatch.setattr(ww, "_warned_frame_dtype", False)
    with caplog.at_level(logging.ERROR, logger="tools.wake_word"):
        for _ in range(5):
            ww.frame_dtype_ok(np.zeros(FRAME, dtype=np.float32))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "the warning must not repeat 12.5 times a second"
    message = errors[0].getMessage()
    assert "32768" in message
    assert "float32" in message
    assert "int16" in message


def _scripted_openwakeword_engine(monkeypatch, score: float = 0.0, **cfg):
    """A real ``_OpenWakeWordEngine`` whose model returns a fixed score."""
    scores = itertools.repeat(score)

    class _ScriptedModel:
        def __init__(self, wakeword_models, inference_framework="onnx"):
            self.wakeword_models = list(wakeword_models)
            self.inference_framework = inference_framework
            self.models = {"hey_youtab": object()}

        def predict(self, frame):
            return {"hey_youtab": next(scores)}

        def reset(self):
            pass

    oww = types.ModuleType("openwakeword")
    oww.utils = types.SimpleNamespace(download_models=lambda names=[]: None)
    model_mod = types.ModuleType("openwakeword.model")
    model_mod.Model = _ScriptedModel
    monkeypatch.setitem(sys.modules, "openwakeword", oww)
    monkeypatch.setitem(sys.modules, "openwakeword.model", model_mod)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: None)
    monkeypatch.setattr(ww, "ensure_tflite_runtime", lambda: True)
    return ww._OpenWakeWordEngine({"provider": "openwakeword", **cfg})


def test_the_engine_boundary_reports_a_float_feed(monkeypatch, caplog):
    """``process()`` is a public boundary, and offline callers reach it directly.

    ``scripts/wakeword/evaluate_model.py`` and
    ``scripts/wakeword/verify_on_device.py`` both call ``engine.process()``
    without going through the detector, so the capture path's ``dtype="int16"``
    does not protect them.
    """
    monkeypatch.setattr(ww, "_warned_frame_dtype", False)
    engine = _scripted_openwakeword_engine(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="tools.wake_word"):
        engine.process(np.zeros(FRAME, dtype=np.float32))
    assert any("32768" in r.getMessage() for r in caplog.records)


def test_a_float_scaled_feed_is_diagnosable_rather_than_a_mystery(monkeypatch, caplog):
    """What the contract violation looks like end to end, through the real loop.

    Loud audio delivered as float32 in -1..1 has an int16 peak of 0, so the
    dead-microphone detector flags a perfectly good microphone as silent while
    the model sees a flat score band. Both readings are wrong in the same
    direction and neither says why — which is the whole reason the boundary
    logs. This pins the misdiagnosis *and* the explanation that now accompanies
    it.
    """
    monkeypatch.setattr(ww, "_warned_frame_dtype", False)
    monkeypatch.setattr(ww, "_SILENCE_ALERT_SECONDS", 0.001)
    source = np.full(FRAME * 6, 12000, dtype=np.int16)          # unmistakably loud
    assert int(np.abs(source).max()) > ww._SILENCE_PEAK, "the fixture is loud"
    engine = _scripted_openwakeword_engine(monkeypatch)
    _fake_audio(monkeypatch, lambda **kw: _FloatStream(source, **kw))
    detector = ww.WakeWordDetector(engine, lambda: None)

    with caplog.at_level(logging.ERROR, logger="tools.wake_word"):
        detector.start()
        try:
            _poll_until(
                lambda: detector.audio_silent,
                "a loud float32 feed to be misread as a silent microphone",
            )
            _poll_until(
                lambda: any("32768" in r.getMessage() for r in caplog.records),
                "the int16 contract violation to be logged",
            )
        finally:
            detector.stop()


def test_the_int16_contract_is_documented_at_the_boundary():
    """Enforced *and* written down, where the next reader will be standing."""
    assert "int16" in (ww._Engine.process.__doc__ or "")
    assert "int16" in (ww.frame_dtype_ok.__doc__ or "")
    # The rationale — the 32768x symptom — belongs beside the constant, so
    # whoever next edits the capture dtype reads why it is not a preference.
    source = inspect.getsource(ww)
    assert "32768x too quiet" in source
    assert 'dtype="int16"' in source
