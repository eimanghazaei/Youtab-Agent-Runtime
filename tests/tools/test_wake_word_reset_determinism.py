"""Scoring must be reproducible: the same audio must score the same, always.

openWakeWord 0.6.0 primes its feature buffer with random noise — both in
``AudioFeatures.__init__`` and in ``AudioFeatures.reset()``:

    self.feature_buffer = self._get_embeddings(
        np.random.randint(-1000, 1000, 16000*4).astype(np.int16))

The classifier reads the last sixteen frames of that buffer, so until real audio
displaces the prime, every score depends on an unseeded random draw. Measured on
60 held-out negative windows, two passes over identical audio disagreed on 28 of
them, worst case by 0.031 — and ``evaluate_model.py`` calls ``reset()`` before
every clip, which is what put that noise into every round's per-frame scores and
into the ONNX/tflite parity figure that compares them.

``_OpenWakeWordEngine`` now overwrites that buffer with a fixed prime after every
reset and at construction. These tests hold that line.

What is real here and what is not
---------------------------------
Real: the shipped ``hey_youtab.onnx`` and ``hey_youtab.tflite``, executed by
``onnxruntime`` and ``ai_edge_litert``; the committed front-end feature fixtures;
and ``_OpenWakeWordEngine.process``/``reset`` themselves — the code this
repository ships.

Stood in: openWakeWord's ``Model`` and ``AudioFeatures``. The package is not
installed in CI (the workflow installs ``.[dev,web,slack]``, not the ``wake``
extra) and its melspectrogram/embedding models are downloaded at first use
rather than committed, so the front end cannot run here. The stand-in therefore
reproduces the one behaviour under test — the random re-prime at utils.py:178 —
faithfully, and ``test_stand_in_really_is_random`` proves it does, because a
stand-in that quietly primed deterministically would make every other test in
this file pass for free.

Both backends are imported unconditionally, matching
``test_wake_word_model_assets.py``: ``onnxruntime`` arrives as a transitive of
the branding gate's OCR engine and ``ai-edge-litert`` is an explicit workflow
step, so a missing one is a broken environment and skipping would hide the only
determinism check on a shipped binary.
"""

from __future__ import annotations

import ast
import sys
import types
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pytest
from ai_edge_litert.interpreter import Interpreter

import tools.wake_word as ww

FIXTURES = Path(__file__).parents[1] / "fixtures" / "wakeword"
WAKEWORDS = Path(ww.__file__).parent / "wakewords"

FRAME = ww._OpenWakeWordEngine.frame_length      # 1280 samples, 80 ms
FEATURE_FRAMES = 16                              # what the classifier reads
EMBEDDING_DIM = 96

#: Frames the stand-in front end returns for four seconds of audio. Upstream's
#: real front end yields ~41 for the same input; only "more than sixteen"
#: matters, so the classifier always has a full window to read.
PRIME_FRAMES = 41

FRAMEWORKS = ("onnx", "tflite")


# ── the real backends, exactly as test_wake_word_model_assets.py drives them ──

def _onnx_scorer():
    session = ort.InferenceSession(
        str(WAKEWORDS / "hey_youtab.onnx"), providers=["CPUExecutionProvider"]
    )
    name = session.get_inputs()[0].name

    def score(window: np.ndarray) -> float:
        return float(session.run(None, {name: window[None, ...]})[0][0][0])

    return score


def _tflite_scorer():
    interpreter = Interpreter(model_path=str(WAKEWORDS / "hey_youtab.tflite"))
    interpreter.allocate_tensors()
    in_index = interpreter.get_input_details()[0]["index"]
    out_index = interpreter.get_output_details()[0]["index"]

    def score(window: np.ndarray) -> float:
        interpreter.set_tensor(in_index, window[None, ...].astype(np.float32))
        interpreter.invoke()
        return float(interpreter.get_tensor(out_index)[0][0])

    return score


def _scorer(framework: str):
    return _onnx_scorer() if framework == "onnx" else _tflite_scorer()


# ── stand-in front end ────────────────────────────────────────────────────────

def _embed(x) -> np.ndarray:
    """Deterministic function of the audio, shaped like real embeddings.

    It only has to be (a) deterministic in its input and (b) different for
    different input — that is what makes the random prime observable downstream.
    """
    a = np.asarray(x, dtype=np.float64)
    chunks = np.array_split(a, PRIME_FRAMES)
    base = np.array([c.mean() if c.size else 0.0 for c in chunks])
    cols = np.arange(EMBEDDING_DIM, dtype=np.float64)
    return ((base[:, None] / 1000.0) + (cols[None, :] / 10000.0)).astype(np.float32)


def _embed_frame(frame) -> np.ndarray:
    a = np.asarray(frame, dtype=np.float64)
    cols = np.arange(EMBEDDING_DIM, dtype=np.float64)
    return ((a.mean() / 1000.0) + cols / 10000.0).astype(np.float32)


class _FakeAudioFeatures:
    """Mirrors openwakeword.utils.AudioFeatures 0.6.0 where it matters.

    The random re-prime is the point: it is copied from upstream verbatim, off
    the *global* numpy RNG, so that anything downstream which fails to overwrite
    it produces different scores on every run.
    """

    def __init__(self) -> None:
        self.reset()

    def _get_embeddings(self, x, **_kw) -> np.ndarray:
        return _embed(x)

    def reset(self) -> None:
        self.feature_buffer = self._get_embeddings(
            np.random.randint(-1000, 1000, 16000 * 4).astype(np.int16)
        )


class _FakeModel:
    """openWakeWord's Model, reduced to what the engine touches.

    ``predict`` streams like the real one — one embedding appended per 80 ms
    frame, classifier over the trailing sixteen — and the classifier is the real
    shipped artifact on the real backend.
    """

    def __init__(self, wakeword_models, inference_framework="onnx"):
        self.wakeword_models = list(wakeword_models)
        self.inference_framework = inference_framework
        self.models = {"hey_youtab": object()}
        self.preprocessor = _FakeAudioFeatures()
        self.prediction_buffer = defaultdict(lambda: deque(maxlen=30))
        self._score = _scorer(inference_framework)
        self.invocations = 0

    def reset(self) -> None:
        self.prediction_buffer = defaultdict(lambda: deque(maxlen=30))
        self.preprocessor.reset()

    def predict(self, frame):
        self.preprocessor.feature_buffer = np.vstack(
            [self.preprocessor.feature_buffer, _embed_frame(frame)[None, :]]
        )
        window = np.ascontiguousarray(
            self.preprocessor.feature_buffer[-FEATURE_FRAMES:], dtype=np.float32
        )
        score = self._score(window)
        self.invocations += 1
        self.prediction_buffer["hey_youtab"].append(score)
        return {"hey_youtab": score}


def _engine(monkeypatch, framework: str) -> ww._OpenWakeWordEngine:
    oww = types.ModuleType("openwakeword")
    oww.utils = types.SimpleNamespace(download_models=lambda names=[]: None)
    model_mod = types.ModuleType("openwakeword.model")
    model_mod.Model = _FakeModel
    monkeypatch.setitem(sys.modules, "openwakeword", oww)
    monkeypatch.setitem(sys.modules, "openwakeword.model", model_mod)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: None)
    # Without this the engine falls back to onnx when no tflite runtime is
    # importable -- which would make every "tflite" case here secretly run onnx
    # and report a green tflite determinism proof for a backend it never touched.
    monkeypatch.setattr(ww, "ensure_tflite_runtime", lambda: True)
    engine = ww._OpenWakeWordEngine(
        {"provider": "openwakeword",
         "openwakeword": {"inference_framework": framework}}
    )
    # The downgrade above is silent, so assert the backend really is the one asked
    # for before any test draws a conclusion from it.
    assert engine._model.inference_framework == framework
    return engine


# ── audio ─────────────────────────────────────────────────────────────────────

def _clip(seed: int, frames: int = 25) -> np.ndarray:
    """A fixed int16 clip. Local Generator: never the global RNG."""
    rng = np.random.default_rng(seed)
    return rng.integers(-8000, 8000, frames * FRAME, dtype=np.int16)


def _score_clip(engine, clip: np.ndarray) -> list[float]:
    """Stream a clip through the product runtime after a reset, per-frame."""
    engine.reset()
    label = engine._labels[0]
    out = []
    for f in range(len(clip) // FRAME):
        engine.process(clip[f * FRAME:(f + 1) * FRAME])
        out.append(float(engine._model.prediction_buffer[label][-1]))
    return out


# ── the stand-in must actually be random, or nothing below means anything ─────

def test_stand_in_really_is_random():
    prep = _FakeAudioFeatures()
    first = prep.feature_buffer.copy()
    prep.reset()
    second = prep.feature_buffer.copy()
    assert not np.array_equal(first, second), (
        "the stand-in front end primes deterministically, so every determinism "
        "test in this file would pass without the fix under test"
    )


# ── 1. the same clip scores identically across repeated resets ────────────────

@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_same_clip_scores_bit_identical_across_resets(monkeypatch, framework):
    engine = _engine(monkeypatch, framework)
    clip = _clip(11)
    runs = [_score_clip(engine, clip) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    # Not all-zero or all-identical-per-frame: a constant score would satisfy
    # equality while proving nothing about the classifier having run.
    assert len(set(runs[0])) > 1


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_reset_restores_the_same_feature_buffer_every_time(monkeypatch, framework):
    engine = _engine(monkeypatch, framework)
    engine.reset()
    first = engine._model.preprocessor.feature_buffer.copy()
    for f in range(25):                       # stream real audio over the prime
        engine.process(_clip(12)[f * FRAME:(f + 1) * FRAME])
    engine.reset()
    assert np.array_equal(first, engine._model.preprocessor.feature_buffer)


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_repeated_resets_are_bit_identical(monkeypatch, framework):
    """Bit-for-bit, not merely equal.

    ``np.array_equal`` compares values and would accept a buffer that had been
    reconstructed at a different dtype or in a different memory layout — either
    of which changes what the classifier reads. The prime is a cached array
    copied on every reset, so the bytes should be identical, and asserting the
    bytes is what says so.
    """
    engine = _engine(monkeypatch, framework)
    snapshots = []
    for _ in range(3):
        engine.reset()
        buffer = engine._model.preprocessor.feature_buffer
        snapshots.append((buffer.dtype.str, buffer.shape, buffer.tobytes()))
        for f in range(20):                   # displace the prime with real audio
            engine.process(_clip(13)[f * FRAME:(f + 1) * FRAME])

    assert snapshots[0] == snapshots[1] == snapshots[2]
    # Non-vacuity: an empty or single-value buffer would be trivially identical.
    assert snapshots[0][1][0] >= FEATURE_FRAMES
    assert len(set(np.frombuffer(snapshots[0][2], dtype=snapshots[0][0]))) > 1


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_the_cached_prime_is_never_the_buffer_that_gets_streamed_into(
    monkeypatch, framework
):
    """The cache must be copied out, not handed out.

    openWakeWord treats ``feature_buffer`` as its own working storage: 0.6.0
    rebinds it with ``np.vstack`` per frame and re-slices it when it exceeds
    ``feature_buffer_max_len``, so today it does not write into the array it was
    given. That is an implementation detail of one version, not a promise. If
    reset installed the cache object itself, whether the second reset restored
    the same bytes as the first would depend on whether the version of upstream
    in use happens to append in place — which is the determinism bug again, one
    level down, and invisible to a single-reset test.
    """
    engine = _engine(monkeypatch, framework)
    engine.reset()
    assert engine._prime_cache is not None
    assert engine._model.preprocessor.feature_buffer is not engine._prime_cache

    before = engine._prime_cache.tobytes()
    for f in range(20):
        engine.process(_clip(14)[f * FRAME:(f + 1) * FRAME])
    assert engine._prime_cache.tobytes() == before, "streaming audio mutated the cache"

    engine.reset()
    assert engine._model.preprocessor.feature_buffer.tobytes() == before


# ── 2. evaluation order does not affect scores ────────────────────────────────

@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_evaluation_order_does_not_change_scores(monkeypatch, framework):
    engine = _engine(monkeypatch, framework)
    clips = {name: _clip(seed) for name, seed in
             (("a", 21), ("b", 22), ("c", 23))}

    forward = {name: _score_clip(engine, clip) for name, clip in clips.items()}
    reverse = {name: _score_clip(engine, clips[name])
               for name in reversed(list(clips))}

    assert forward == reverse
    # Different clips must score differently, otherwise order-independence is
    # trivially true because everything scores the same.
    assert forward["a"] != forward["b"]


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_two_engines_in_one_process_agree(monkeypatch, framework):
    # A second engine built after the first used to inherit a different random
    # prime, so two runtimes in one process disagreed about the same audio.
    first = _engine(monkeypatch, framework)
    second = _engine(monkeypatch, framework)
    clip = _clip(31)
    assert _score_clip(first, clip) == _score_clip(second, clip)


# ── 3 & 4. each real backend is deterministic on its own ──────────────────────

def _fixture_features() -> np.ndarray:
    return np.load(FIXTURES / "samples.npz")["features"].astype(np.float32)


def test_onnx_backend_is_deterministic():
    features = _fixture_features()
    a, b = _onnx_scorer(), _onnx_scorer()
    first = [a(f) for f in features]
    second = [b(f) for f in features]
    assert first == second
    assert len(set(first)) > 1


def test_tflite_backend_is_deterministic():
    features = _fixture_features()
    a, b = _tflite_scorer(), _tflite_scorer()
    first = [a(f) for f in features]
    second = [b(f) for f in features]
    assert first == second
    assert len(set(first)) > 1


# ── 5. both backends run through the real product runtime ─────────────────────

@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_backend_executes_through_the_product_runtime(monkeypatch, framework):
    engine = _engine(monkeypatch, framework)
    clip = _clip(41)
    frames = len(clip) // FRAME

    scores = _score_clip(engine, clip)

    # The engine's own process() drove every frame...
    assert engine._model.invocations == frames
    # ...and the numbers came out of the shipped artifact on this backend, not a
    # stub: replay the same trailing window through a freshly built real scorer.
    engine.reset()
    replay = _FakeAudioFeatures()
    replay.feature_buffer = engine._model.preprocessor.feature_buffer.copy()
    for f in range(frames):
        replay.feature_buffer = np.vstack(
            [replay.feature_buffer, _embed_frame(clip[f * FRAME:(f + 1) * FRAME])[None, :]]
        )
    window = np.ascontiguousarray(
        replay.feature_buffer[-FEATURE_FRAMES:], dtype=np.float32)
    assert _scorer(framework)(window) == scores[-1]
    assert 0.0 <= min(scores) and max(scores) <= 1.0


def test_the_two_backends_are_not_the_same_file():
    onnx = (WAKEWORDS / "hey_youtab.onnx").read_bytes()
    tflite = (WAKEWORDS / "hey_youtab.tflite").read_bytes()
    assert onnx != tflite and len(onnx) > 1024 and len(tflite) > 1024


# ── the constraints on HOW determinism was achieved ───────────────────────────

@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_priming_is_reported_as_deterministic(monkeypatch, framework):
    engine = _engine(monkeypatch, framework)
    assert engine._prime_deterministic is True


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_priming_does_not_touch_the_global_rng(monkeypatch, framework):
    # A process-wide seed was explicitly ruled out: it would reach every other
    # numpy user in the process. The fix must draw from a local Generator, so
    # priming has to leave the global RNG state untouched.
    engine = _engine(monkeypatch, framework)
    before = np.random.get_state()
    engine._prime_features()
    after = np.random.get_state()
    assert before[0] == after[0]
    assert np.array_equal(before[1], after[1])
    assert before[2:] == after[2:]


def _dotted(node) -> str:
    """`a.b.c` for an attribute/name chain, else ''."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def test_no_global_seeding_in_the_wake_word_module():
    # Parsed, not grepped. The module's own comment explains why
    # `np.random.seed()` is the wrong fix, so a substring search matches that
    # prose and fails on a module that is in fact correct -- which is exactly
    # what the first version of this test did.
    tree = ast.parse(Path(ww.__file__).read_text(encoding="utf-8"))
    seeds = [
        _dotted(n.func) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _dotted(n.func).endswith("random.seed")
    ]
    assert seeds == [], f"global RNG seeding in tools/wake_word.py: {seeds}"

    # And the prime must come from a local Generator seeded with the constant.
    local = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _dotted(n.func).endswith("default_rng")
        and any(isinstance(a, ast.Name) and a.id == "_RESET_PRIME_SEED"
                for a in n.args)
    ]
    assert local, "no np.random.default_rng(_RESET_PRIME_SEED) call found"
