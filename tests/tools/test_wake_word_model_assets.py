"""The shipped "hey youtab" model must be the model, and must still work.

``test_wake_word.py`` covers the engine around the artifact — config
resolution, framework selection, the fire/cooldown loop — with the model
stubbed out. This file covers the artifact itself: that both framework files
ship, that they are the exact bytes their recorded measurements were taken
against, that each declares the tensor shapes openWakeWord reads back, that the
two backends agree, and that on real held-out audio the phrase scores over the
default threshold while near misses and ordinary speech score under it.

Both backends are imported unconditionally rather than through
``importorskip``. ``onnxruntime`` reaches CI as a transitive of the branding
gate's OCR engine and ``ai-edge-litert`` is installed by an explicit workflow
step, so a missing one is a broken environment, and a broken environment that
silently skips the only test of a shipped binary is worse than a red run.

The fixtures are openWakeWord front-end features, not waveforms, because the
front end is downloaded at runtime rather than committed. Each one was produced
by running the real melspectrogram and embedding models over the committed WAV
beside it, at the position the streaming engine scores when the phrase
finishes. Everything downstream of that point — the part this repository
actually ships — runs here for real, on both backends.

Provenance for every sample is in ``tests/fixtures/wakeword/samples.json``. The
voices are from the evaluation speaker pool, which training never saw.
"""

from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pytest
from ai_edge_litert.interpreter import Interpreter

import tools.wake_word as ww

FIXTURES = Path(__file__).parents[1] / "fixtures" / "wakeword"
WAKEWORDS = Path(ww.__file__).parent / "wakewords"

#: The runtime's default `wake_word.sensitivity`. Scores are compared against
#: this raw value, so it is what the artifact has to be good at.
THRESHOLD = ww._DEFAULTS["sensitivity"]

#: Backends must not merely both work — they must agree. openWakeWord's two
#: front ends differ slightly, but the classifier is one set of weights and its
#: two encodings have to produce the same number on the same input.
PARITY_TOLERANCE = 1e-4


def _load_samples() -> dict:
    payload = np.load(FIXTURES / "samples.npz")
    meta = json.loads((FIXTURES / "samples.json").read_text(encoding="utf-8"))
    return {
        "features": payload["features"].astype(np.float32),
        "labels": payload["labels"],
        "categories": [str(c) for c in payload["categories"]],
        "names": [str(n) for n in payload["names"]],
        "meta": meta,
    }


def _onnx_scores(features: np.ndarray) -> np.ndarray:
    session = ort.InferenceSession(
        str(WAKEWORDS / "hey_youtab.onnx"), providers=["CPUExecutionProvider"]
    )
    name = session.get_inputs()[0].name
    return np.array(
        [float(session.run(None, {name: f[None, ...]})[0][0][0]) for f in features],
        dtype=np.float64,
    )


def _tflite_scores(features: np.ndarray) -> np.ndarray:
    interpreter = Interpreter(model_path=str(WAKEWORDS / "hey_youtab.tflite"))
    interpreter.allocate_tensors()
    in_index = interpreter.get_input_details()[0]["index"]
    out_index = interpreter.get_output_details()[0]["index"]
    scores = []
    for f in features:
        interpreter.set_tensor(in_index, f[None, ...].astype(np.float32))
        interpreter.invoke()
        scores.append(float(interpreter.get_tensor(out_index)[0][0]))
    return np.array(scores, dtype=np.float64)


# ── the artifacts themselves ─────────────────────────────────────────────


class TestTheArtifactsShip:
    def test_both_frameworks_resolve_to_a_shipped_file(self):
        # _bundled_wakeword_path is what the engine calls when the config names
        # the default; if it pointed anywhere else the model would be dead
        # weight in the wheel.
        for framework in ("onnx", "tflite"):
            path = Path(ww._bundled_wakeword_path(framework))
            assert path.parent == WAKEWORDS
            assert path.is_file(), path
            assert path.stat().st_size > 100_000, path

    def test_committed_hashes_match_the_shipped_bytes(self):
        """SHA256SUMS is what binds the measurements to these exact files.

        Every number in ``tools/wakewords/MODEL_CARD.md`` was measured against
        the bytes named here. Replacing an artifact without rerunning the
        pipeline breaks this test rather than silently invalidating the card.
        """
        recorded = {}
        for line in (WAKEWORDS / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, name = line.split()
                recorded[name] = digest
        assert set(recorded) == {"hey_youtab.onnx", "hey_youtab.tflite"}
        for name, digest in recorded.items():
            actual = hashlib.sha256((WAKEWORDS / name).read_bytes()).hexdigest()
            assert actual == digest, f"{name} does not match its recorded hash"

    def test_onnx_declares_the_shapes_openwakeword_reads_back(self):
        # openwakeword.model.Model takes the frame count from input.shape[1]
        # and the class count from output.shape[1]. A model that disagrees
        # loads and then scores garbage.
        session = ort.InferenceSession(
            str(WAKEWORDS / "hey_youtab.onnx"), providers=["CPUExecutionProvider"]
        )
        assert list(session.get_inputs()[0].shape) == [1, 16, 96]
        assert list(session.get_outputs()[0].shape) == [1, 1]

    def test_tflite_declares_the_shapes_openwakeword_reads_back(self):
        interpreter = Interpreter(model_path=str(WAKEWORDS / "hey_youtab.tflite"))
        interpreter.allocate_tensors()
        assert list(interpreter.get_input_details()[0]["shape"]) == [1, 16, 96]
        assert list(interpreter.get_output_details()[0]["shape"]) == [1, 1]


# ── what the model does ──────────────────────────────────────────────────


class TestTheModelDetectsTheWakePhrase:
    @pytest.fixture(scope="class")
    def samples(self):
        return _load_samples()

    @pytest.fixture(scope="class")
    def scores(self, samples):
        return {
            "onnx": _onnx_scores(samples["features"]),
            "tflite": _tflite_scores(samples["features"]),
        }

    def test_the_fixture_covers_every_condition_that_matters(self, samples):
        # A behavioural assertion is only as good as what it is asserted over.
        # If a later change trims the fixture down to easy cases the tests
        # below would keep passing while proving less, so the coverage is
        # itself asserted.
        present = set(samples["categories"])
        assert {"positive", "near_phrase", "recorded_speech", "background_only"} <= present
        assert int((samples["labels"] == 1).sum()) >= 8
        assert int((samples["labels"] == 0).sum()) >= 12

        rows = samples["meta"]["samples"]
        conditions = {row["condition"] for row in rows}
        # A wake word that only works in a quiet room is not a wake word. The
        # fixture has to contain positives recorded against loud backgrounds
        # and positives heard across a reverberant room, or "it detects the
        # phrase" is a claim about ideal conditions only.
        assert {"positive_noisy", "positive_reverberant", "positive_clean"} <= conditions
        noisy = [r for r in rows if r["condition"] == "positive_noisy"]
        assert len(noisy) >= 4
        assert all(r["snr_db"] <= samples["meta"]["noisy_snr_threshold_db"] for r in noisy)
        assert any(r["snr_db"] <= 3.0 for r in noisy), (
            "no positive at 3 dB SNR or worse — the fixture's hard end is not hard"
        )
        # Near misses must be several different phrases, not one repeated.
        near = {r["spoken_text"] for r in rows if r["condition"] == "near_phrase"}
        assert len(near) >= 6, f"near misses cover only {near}"

    @pytest.mark.parametrize("framework", ("onnx", "tflite"))
    def test_the_wake_phrase_scores_over_the_default_threshold(
        self, samples, scores, framework
    ):
        positives = samples["labels"] == 1
        got = scores[framework][positives]
        missed = [
            f"{name} ({score:.3f})"
            for name, score, keep in zip(samples["names"], scores[framework], positives)
            if keep and score < THRESHOLD
        ]
        assert not missed, f"{framework}: wake phrase not detected in {missed}"
        assert got.min() >= THRESHOLD

    @pytest.mark.parametrize("framework", ("onnx", "tflite"))
    def test_near_misses_and_ordinary_speech_stay_under_it(
        self, samples, scores, framework
    ):
        negatives = samples["labels"] == 0
        fired = [
            f"{name} [{cat}] ({score:.3f})"
            for name, cat, score, keep in zip(
                samples["names"], samples["categories"], scores[framework], negatives
            )
            if keep and score >= THRESHOLD
        ]
        assert not fired, f"{framework}: false accept on {fired}"

    @pytest.mark.parametrize("framework", ("onnx", "tflite"))
    def test_it_separates_the_two_classes_by_a_margin(self, samples, scores, framework):
        # Clearing the threshold is not enough on its own: a model whose
        # positives sit at 0.61 and negatives at 0.59 passes the two tests
        # above and is useless in a real room, where a little noise moves a
        # score by more than that.
        got = scores[framework]
        lowest_positive = got[samples["labels"] == 1].min()
        highest_negative = got[samples["labels"] == 0].max()
        assert lowest_positive - highest_negative > 0.25, (
            f"{framework}: only {lowest_positive - highest_negative:.3f} between "
            f"the weakest detection ({lowest_positive:.3f}) and the strongest "
            f"false accept ({highest_negative:.3f})"
        )

    def test_both_backends_agree_on_every_sample(self, samples, scores):
        delta = np.abs(scores["onnx"] - scores["tflite"])
        worst = int(delta.argmax())
        assert delta.max() <= PARITY_TOLERANCE, (
            f"backends disagree by {delta.max():.2e} on {samples['names'][worst]} "
            f"(onnx {scores['onnx'][worst]:.6f}, tflite {scores['tflite'][worst]:.6f})"
        )


# ── the fixture is real audio, not a synthetic tensor ────────────────────


class TestTheFixtureIsAuditable:
    def test_every_sample_keeps_the_audio_it_was_derived_from(self):
        samples = _load_samples()
        for name in samples["names"]:
            wav = FIXTURES / "audio" / name
            assert wav.is_file(), f"{name} has no committed audio"
            with wave.open(str(wav), "rb") as handle:
                assert handle.getframerate() == 16000
                assert handle.getnchannels() == 1
                assert handle.getsampwidth() == 2
                # 2.00 s is the window that yields the model's sixteen frames.
                assert handle.getnframes() == 32000

    def test_the_provenance_record_describes_every_sample(self):
        samples = _load_samples()
        described = {row["file"]: row for row in samples["meta"]["samples"]}
        assert set(described) == set(samples["names"])
        for name, category, label in zip(
            samples["names"], samples["categories"], samples["labels"]
        ):
            row = described[name]
            assert row["category"] == category
            assert row["label"] == int(label)
            assert row["source"], f"{name} has no recorded source"
        assert samples["meta"]["speaker_pool"] == "evaluation"
