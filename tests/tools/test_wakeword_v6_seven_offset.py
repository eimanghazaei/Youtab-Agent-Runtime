"""V6: the Round 8 builder is raised to the predeclared seven-offset design.

Round 8's predeclaration (``round8_config.json``) fixes a seven-offset
phrase-anchored windowing grid — offsets on frames [2..8] at the runtime's
0.08 s frame — and projects ``near_phrase_human`` as ``utterances x 7``, with
``loss.negative_weight`` derived from those counts. The Owner decision for V6
was to keep that predeclaration and raise ``build_human_dataset`` to it, not to
lower the spec to the builder's old four-offset, tiled-near-phrase behaviour.

This file proves the builder now produces exactly the predeclared windowing:

* ``TRAILING_OFFSETS_S`` is the seven-offset ladder that lands on frames [2..8];
* a positive utterance and a ``near_phrase_human`` utterance are BOTH
  phrase-anchored with that ladder (seven windows each), while a continuous
  negative is tiled by clip length; and
* ``round8_config.builder_windowing_divergence`` is empty on the committed
  config and is folded into ``round8_config.check`` so the two can never
  silently drift again.

Each assertion is paired, in the docstrings, with the one-line mutation that
would let the old behaviour back in — every one of which was applied, observed
red and reverted (see the change's mutation log). Hermetic: every WAV is
generated under ``tmp_path``; nothing here reads a real recording.
"""

from __future__ import annotations

import copy
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import build_dataset  # noqa: E402
import build_human_dataset as round8  # noqa: E402
import round8_config as r8cfg  # noqa: E402

#: The builder source, so the emit loop's windowing predicate can be checked to
#: be the same one ``windows_for`` plans with — the invariant the preallocating
#: sink depends on. Mirrors the ``MODULE_SOURCE`` convention in
#: ``test_wakeword_round8_human_only.py``.
BUILDER_SOURCE = (WAKEWORD / "build_human_dataset.py").read_text(encoding="utf-8")

POSITIVE = "positive_human"
NEAR = "near_phrase_human"
CONTINUOUS = "recorded_speech"


# ── fixtures ─────────────────────────────────────────────────────────────────


def _write_wav(path: Path, seconds: float, seed: int) -> Path:
    """A deterministic 16 kHz mono 16-bit PCM WAV — a plain recorded-style clip.

    A test fixture only: noise, not speech, generated from a fixed seed so a
    window quietly replaced by zeros is visible in the byte comparisons below.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    samples = rng.normal(0.0, 0.2, int(seconds * build_dataset.SAMPLE_RATE)) * 32767.0
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(build_dataset.SAMPLE_RATE)
        handle.writeframes(np.clip(samples, -32768, 32767).astype("<i2").tobytes())
    return path


def _sample(path: Path, category: str, label: int) -> round8.Sample:
    """A ``Sample`` carrying just enough to drive the windowing functions.

    ``windows_for`` and the emit loop use ``path``, ``label`` and ``category``;
    the provenance fields are unread here and are filled with placeholders.
    """
    return round8.Sample(
        path=path,
        category=category,
        label=label,
        speaker="fixture",
        split="train",
        source_type="human",
        source_file="fixture.m4a",
        source_sha256="0" * 64,
        sha256="0" * 64,
        licence="fixture",
        manifest_sha256="0" * 64,
    )


def _emit(sample: round8.Sample, count: int) -> list[np.ndarray]:
    """The build's per-window decision, reproduced with the module's own parts.

    Kept byte-for-byte in step with ``build_windows`` by the source assertion in
    ``test_the_emit_loop_and_windows_for_share_one_predicate``: the same
    ``place_at_end`` / ``tile`` primitives and the same anchoring predicate.
    """
    audio = build_dataset.read_wav16(sample.path)
    windows: list[np.ndarray] = []
    for index in range(count):
        if sample.label == 1 or sample.category in round8.PHRASE_ANCHORED_NEGATIVES:
            windows.append(round8.place_at_end(audio, round8.TRAILING_OFFSETS_S[index]))
        else:
            windows.append(round8.tile(audio, index))
    return windows


# ── 1. the ladder lands on frames [2..8] ─────────────────────────────────────


def test_trailing_offsets_are_seven_and_land_on_frames_two_through_eight() -> None:
    """Seven offsets, one per frame across the [2..8] plateau.

    Mutation: reverting ``TRAILING_OFFSETS_S`` to the four-offset ladder
    ``(0.16, 0.32, 0.48, 0.64)`` drops this to four frames [2, 4, 6, 8] and
    fails here (and the divergence check below).
    """
    ladder = round8.TRAILING_OFFSETS_S
    assert len(ladder) == 7

    # The runtime frame, read from the contract check() ties to the engine:
    # 1280 samples / 16 kHz = 0.08 s.
    frame_samples = r8cfg.load()["runtime_contract"]["frame_length_samples"]
    frame_seconds = frame_samples / build_dataset.SAMPLE_RATE
    assert frame_seconds == pytest.approx(0.08)

    frames = [round(offset / frame_seconds) for offset in ladder]
    assert frames == [2, 3, 4, 5, 6, 7, 8]
    # And that is exactly what the predeclaration fixes.
    assert frames == r8cfg.load()["window_construction"]["phrase_anchored_offsets_frames"]


# ── 2. positives and near-phrase anchor; continuous negatives tile ────────────


def test_windowing_by_category_on_a_real_wav(tmp_path: Path) -> None:
    """A positive and a near-phrase anchor to seven windows; a corpus clip tiles.

    Mutation: dropping ``near_phrase_human`` from ``PHRASE_ANCHORED_NEGATIVES``
    (or removing the ``or sample.category in ...`` arm) tiles the 0.9 s
    near-phrase clip into a single window, failing the ``NEAR`` assertions here
    and the projection divergence below.
    """
    positive = _sample(_write_wav(tmp_path / "p.wav", 0.7, seed=1), POSITIVE, 1)
    near = _sample(_write_wav(tmp_path / "n.wav", 0.9, seed=2), NEAR, 0)
    # 4.5 s of continuous audio: two whole 2 s windows, so it tiles into two.
    cont = _sample(_write_wav(tmp_path / "c.wav", 4.5, seed=3), CONTINUOUS, 0)

    positive_windows, negative_windows = 7, 3

    # windows_for: positive and near both yield the full ladder; the continuous
    # negative yields min(negative_windows, whole 2 s tiles) = min(3, 2) = 2.
    assert round8.windows_for(positive, positive_windows, negative_windows) == 7
    assert round8.windows_for(near, positive_windows, negative_windows) == 7
    assert round8.windows_for(cont, positive_windows, negative_windows) == 2

    # The near-phrase is anchored, not tiled: every window is a place_at_end at
    # the ladder offset, and differs from what tiling would have produced.
    near_audio = build_dataset.read_wav16(near.path)
    near_windows = _emit(near, round8.windows_for(near, positive_windows, negative_windows))
    assert len(near_windows) == 7
    for index, window in enumerate(near_windows):
        expected = round8.place_at_end(near_audio, round8.TRAILING_OFFSETS_S[index])
        assert np.array_equal(window, expected)
    assert not np.array_equal(near_windows[0], round8.tile(near_audio, 0))

    # The continuous negative is tiled, not anchored.
    cont_audio = build_dataset.read_wav16(cont.path)
    cont_windows = _emit(cont, round8.windows_for(cont, positive_windows, negative_windows))
    assert len(cont_windows) == 2
    for index, window in enumerate(cont_windows):
        assert np.array_equal(window, round8.tile(cont_audio, index))


def test_the_emit_loop_and_windows_for_share_one_predicate(tmp_path: Path) -> None:
    """The preallocation invariant: the emitter and the planner never disagree.

    ``FeatureSink`` is told the total from ``windows_for`` before any window
    exists and refuses to grow or to finish short, so the two must key on the
    same phrase-anchoring predicate. Asserted on the source because a divergence
    would otherwise only surface hours into a real build.
    """
    # Both windows_for and the emit loop use this exact line — hence two hits.
    predicate = "sample.label == 1 or sample.category in PHRASE_ANCHORED_NEGATIVES"
    assert BUILDER_SOURCE.count(predicate) == 2

    # And the reproduction agrees with windows_for for each windowing kind.
    for seconds, category, label, planned in (
        (0.7, POSITIVE, 1, 7),
        (0.9, NEAR, 0, 7),
        (4.5, CONTINUOUS, 0, 2),
    ):
        sample = _sample(
            _write_wav(tmp_path / f"{category}.wav", seconds, seed=int(seconds * 100)),
            category,
            label,
        )
        planned_count = round8.windows_for(sample, 7, 3)
        assert planned_count == planned
        assert len(_emit(sample, planned_count)) == planned_count


# ── 3. the divergence is empty and folded into check() ───────────────────────


def test_builder_windowing_divergence_is_empty_and_folded_into_check() -> None:
    """The predeclaration and the real builder now agree, and check() enforces it.

    Mutations: any of the four V6 reverts (shorten the ladder, drop the
    near-phrase from ``PHRASE_ANCHORED_NEGATIVES``) reopens the divergence, which
    is now part of ``check`` and so fails ``--check`` — not only the dedicated
    ``--check-builder-windowing`` report.
    """
    assert round8.PHRASE_ANCHORED_NEGATIVES == frozenset({NEAR})
    assert r8cfg.builder_windowing_divergence() == []
    # The committed config passes check() with the divergence folded in.
    assert r8cfg.check(r8cfg.load()) == []

    # Folding has teeth: a projection that disagrees with the builder's
    # phrase-anchoring makes check() carry the exact divergence message.
    drifted = copy.deepcopy(r8cfg.load())
    for split in ("train", "validation"):
        drifted["dataset_projection"][split]["near_phrase_human"]["windows"] += 1
    divergence = r8cfg.builder_windowing_divergence(drifted)
    assert divergence, "the divergence did not fire on a builder-disagreeing projection"
    problems = r8cfg.check(drifted)
    assert all(message in problems for message in divergence), (
        "builder_windowing_divergence is not folded into check()"
    )
