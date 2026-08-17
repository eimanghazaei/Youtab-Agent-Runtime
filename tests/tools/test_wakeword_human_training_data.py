"""Real recorded speech may enter the wake-word dataset in exactly one way.

Background
----------
Round 6 adds a real person's recordings to the training data. Everything about
that is a chance to invalidate the experiment quietly, because none of these
failures raise anything on their own -- they produce a dataset that builds, a
model that trains, and a number that means something other than what it says:

* **A human speaker on the wrong split.** One person is a handful of voices at
  most. A validation or evaluation split carrying that speaker scores the model
  against a voice it was fitted on, and the false-reject rate drops for a
  reason that does not survive contact with a second person.
* **The sealed speaker ingested.** One derived speaker is held back as the only
  measurement of whether the model generalises to a real voice. Train on it
  once and every later number about it describes memorisation. Nothing
  downstream can detect that it happened, which is why ``build_dataset``
  refuses a manifest that declares any split but ``train`` on *any* ``--split``
  rather than trusting the caller to pass the right one.
* **``excluded`` ignored.** Clips somebody listened to and rejected -- clipped,
  coughed through, the wrong phrase -- are marked in the manifest. Re-deriving
  that judgement from durations or transcripts silently overrules it.
* **Human windows sharing a synthetic category.** "The false-accept rate went
  down" and "the false-accept rate went down on the one person we trained on"
  are different claims, and a shared category string makes the second
  indistinguishable from the first in ``evaluate_model.py``'s per-category
  table.
* **A group id shared with a synthetic clip.** Group ids keep every window
  derived from one utterance on one side of a split; a collision lets an
  augmented copy of a training clip be validated on.
* **A preallocation that forgets the new windows.** ``FeatureSink.begin`` sizes
  one array up front from a count computed before any window exists. Too small
  aborts the run; too large leaves zero-filled rows that train as silent
  negatives.
* **Generated background mixed in as if it were a room.** Two of the six files
  Speech Commands ships in ``_background_noise_`` are synthesised, not recorded,
  and a model fitted over synthesised noise has learned a room that does not
  exist. ``VALIDATION_NOISE`` named one of them for seven rounds while every
  document said generated audio was prohibited, so the rule is now a constant the
  builder reads and a test here, on every split.

Every one of those is a guard in ``build_dataset.py`` and a test here.

These tests are hermetic. They build a miniature TTS tree, a miniature Speech
Commands tree and a synthetic human manifest over ``tmp_path``, and generate
every WAV. The real human recordings are not in this repository and must never
be: nothing here reads them, and the manifest fixtures describe a speaker who
does not exist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import build_dataset  # noqa: E402

# The miniature corpus, sized so every expected count below is a literal rather
# than a formula that could agree with a wrong implementation.
POSITIVE_CLIPS = 2
HARDNEG_CLIPS = 1
RECORDED_CLIPS = 3
NOISE_ONLY_WINDOWS = 2
POSITIVE_REPEATS = 1
HARD_NEGATIVE_REPEATS = 1

#: Windows a synthetic-only build of this corpus produces.
BASELINE_WINDOWS = (
    POSITIVE_CLIPS * POSITIVE_REPEATS
    + HARDNEG_CLIPS * HARD_NEGATIVE_REPEATS
    + RECORDED_CLIPS
    + NOISE_ONLY_WINDOWS
)

#: Source utterances a synthetic-only build counts (one group id each; the
#: background-only windows take one apiece).
BASELINE_GROUPS = (
    POSITIVE_CLIPS + HARDNEG_CLIPS + RECORDED_CLIPS + NOISE_ONLY_WINDOWS
)

#: The stats keys a build produced before human data existed. Asserted as a
#: whole set, so a key appearing on the no-manifest path fails here.
BASELINE_STATS_KEYS = frozenset(
    {
        "split",
        "seed",
        "windows",
        "positives",
        "negatives",
        "synthesized_positive_clips",
        "recorded_negative_clips",
        "background_recordings",
        "generated_background_excluded",
        "impulse_responses",
        "window_seconds",
        "source_utterances",
        "categories",
    }
)

#: Every file Speech Commands ships in ``_background_noise_``. Four are real
#: recordings and two — ``pink_noise.wav`` and ``white_noise.wav`` — are
#: generated. All six are written into the fixture, because "the generated ones
#: are excluded" is only a claim if they were there to exclude.
BACKGROUND_FILES = (
    "doing_the_dishes.wav",
    "dude_miaowing.wav",
    "exercise_bike.wav",
    "pink_noise.wav",
    "running_tap.wav",
    "white_noise.wav",
)

#: What each split's background pool must be once the generated files are out:
#: one real recording for train, one for validation, two held back to evaluate.
BACKGROUND_BY_SPLIT = {
    "train": ["exercise_bike.wav"],
    "validation": ["doing_the_dishes.wav"],
    "eval": ["dude_miaowing.wav", "running_tap.wav"],
}

SPEAKER = "human-r6-a"

#: Two usable positives, two usable negatives, two excluded clips. The excluded
#: ones are written to disk like any other, so a build that ingests them fails
#: on the count rather than on a missing file.
HUMAN_CLIPS = (
    {"clip": "clips/pos_01.wav", "category": "positive_neutral", "excluded": False},
    {"clip": "clips/pos_02.wav", "category": "positive_far", "excluded": False},
    {"clip": "clips/pos_03.wav", "category": "positive_neutral", "excluded": True},
    {"clip": "clips/near_01.wav", "category": "near_phrase", "excluded": False},
    {"clip": "clips/near_02.wav", "category": "near_phrase", "excluded": True},
    {"clip": "clips/free_01.wav", "category": "free_speech", "excluded": False},
)
USABLE_HUMAN_POSITIVES = 2
USABLE_HUMAN_NEGATIVES = 2
EXCLUDED_HUMAN_CLIPS = 2
USABLE_HUMAN_SOURCES = {
    "clips/pos_01.wav",
    "clips/pos_02.wav",
    "clips/near_01.wav",
    "clips/free_01.wav",
}
EXCLUDED_HUMAN_SOURCES = {"clips/pos_03.wav", "clips/near_02.wav"}


class RecordingSink:
    """Stands in for ``FeatureSink``: keeps windows instead of embedding them.

    The real sink loads openWakeWord's ONNX front end and runs two models over
    every window. That is not what these tests are about and neither the
    package nor its pinned models belong in a unit test's environment.

    The one behaviour worth mirroring exactly is the preallocation contract,
    because that is the part human windows can break: the real sink raises when
    more windows arrive than ``begin`` was told to expect, and raises again at
    ``finish`` when fewer did. Reproducing both here means an off-by-N in the
    planned count fails in this file instead of eight hours into a pipeline
    run.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self.planned: int | None = None
        self.windows: list[np.ndarray] = []
        self.labels: list[int] = []
        self.groups: list[int] = []
        self.categories: list[str] = []
        self.settings: list[dict] = []

    def begin(self, total: int) -> None:
        self.planned = total

    def add(self, window: np.ndarray, label: int, group: int, category: str,
            params: dict) -> None:
        if self.planned is None:
            raise AssertionError("add() before begin(); the real sink has no array yet")
        if len(self.labels) >= self.planned:
            raise AssertionError(
                f"emitted more windows than planned ({len(self.labels) + 1} > "
                f"{self.planned}); the preallocation is short"
            )
        self.windows.append(window)
        self.labels.append(label)
        self.groups.append(group)
        self.categories.append(category)
        self.settings.append(params)

    def finish(self) -> None:
        if self.planned != len(self.labels):
            raise AssertionError(
                f"planned {self.planned} windows but emitted {len(self.labels)}; "
                f"the surplus would ship as zero-filled rows"
            )

    @property
    def features(self) -> np.ndarray:
        """What ``main()`` saves. Waveforms here, since nothing embedded them."""
        if not self.windows:
            return np.zeros((0, build_dataset.WINDOW_SAMPLES), dtype=np.int16)
        return np.stack(self.windows)


def _write_wav(path: Path, seconds: float, seed: int) -> Path:
    """A 16 kHz mono 16-bit WAV of deterministic noise.

    Noise rather than silence: ``Augmenter.apply`` divides by the window's RMS
    to hit a signal-to-noise ratio, and a silent clip takes the other branch,
    so silent fixtures would exercise a path the real corpus never does.
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


def _tts_group(root: Path, group: str, count: int, seed: int) -> None:
    """One `<group>_train` directory with the manifest ``_tts_clips`` reads.

    Both mixed speakers sit inside the training pool, which is the filter
    ``_tts_clips`` applies; a clip mixing in a validation voice would be
    dropped and the expected counts here would silently be wrong.
    """
    directory = root / f"{group}_train"
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(count):
        name = f"{index:06d}.wav"
        _write_wav(directory / name, seconds=0.6, seed=seed + index)
        rows.append({"file": name, "speaker_1": 3, "speaker_2": 11})
    (directory / "manifest.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


def _speech_commands(root: Path) -> None:
    """A miniature Speech Commands tree: recorded negatives and room tone."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "validation_list.txt").write_text("", encoding="utf-8")
    (root / "testing_list.txt").write_text("", encoding="utf-8")

    # Speaker ids are picked by the dataset's own hash bucket so that all of
    # them land on the training side of the partition. Left to chance, some
    # would fall in the validation share and RECORDED_CLIPS would no longer be
    # the number of recorded windows a train build emits.
    picked = 0
    candidate = 0
    while picked < RECORDED_CLIPS:
        name = f"speaker{candidate:04d}_nohash_0.wav"
        candidate += 1
        if build_dataset._speaker_bucket(Path(name)) >= 85:
            continue
        _write_wav(root / "yes" / name, seconds=0.4, seed=500 + picked)
        picked += 1

    # The whole `_background_noise_` directory, generated files included. Every
    # split must end up with at least one real recording, because the Augmenter
    # refuses to build with no background recording at all -- and the two
    # generated files must end up in no split, which is what the tests below
    # measure rather than assume.
    for index, name in enumerate(BACKGROUND_FILES):
        _write_wav(root / "_background_noise_" / name, seconds=3.0, seed=77 + index)


def _write_human_manifest(
    root: Path,
    *,
    split: str = "train",
    clips: tuple[dict, ...] = HUMAN_CLIPS,
    speaker: str = SPEAKER,
    indent: int | None = None,
) -> Path:
    """A derived speaker's MANIFEST.json, with a WAV behind every clip.

    The manifest sits in its own directory and stores clip paths relative to
    it, which is how the real derivation writes them. Nothing here ever chdirs,
    so a build that resolved those paths against the working directory instead
    would fail to find a single clip.
    """
    root.mkdir(parents=True, exist_ok=True)
    for index, clip in enumerate(clips):
        if "clip" in clip:
            _write_wav(root / clip["clip"], seconds=0.7, seed=900 + index)
    manifest = {"speaker": speaker, "split": split, "clips": list(clips)}
    path = root / "MANIFEST.json"
    path.write_text(json.dumps(manifest, indent=indent), encoding="utf-8")
    return path


class Corpus:
    """The fixture's inputs, plus an argument builder for ``build_windows``."""

    def __init__(self, tmp_path: Path) -> None:
        self.tts = tmp_path / "tts"
        self.speech_commands = tmp_path / "speech_commands"
        self.human_root = tmp_path / "human"
        self.out = tmp_path / "features"

    def args(self, **overrides) -> argparse.Namespace:
        values = {
            "tts_root": self.tts,
            "speech_commands": self.speech_commands,
            "out": self.out,
            "split": "train",
            "seed": 1234,
            "positive_repeats": POSITIVE_REPEATS,
            "hard_negative_repeats": HARD_NEGATIVE_REPEATS,
            "confusable_repeats": 1,
            "recorded_negatives": 0,
            "noise_only": NOISE_ONLY_WINDOWS,
            "rir_count": 0,
            "batch_size": 8,
            "chunk": 8,
            "ncpu": 1,
            "keep_audio": False,
            "human_manifest": None,
            "human_positive_repeats": 0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def build(self, **overrides) -> tuple[RecordingSink, dict]:
        """Build one split into a recording sink and check the preallocation."""
        sink = RecordingSink()
        stats = build_dataset.build_windows(self.args(**overrides), sink)
        sink.finish()
        return sink, stats

    def with_human(self, repeats: int = 2, **overrides) -> tuple[RecordingSink, dict]:
        manifest = overrides.pop("manifest", self.human_root / "MANIFEST.json")
        return self.build(
            human_manifest=manifest, human_positive_repeats=repeats, **overrides
        )


@pytest.fixture()
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Corpus:
    # pyroomacoustics synthesizes the impulse responses and is a training-host
    # dependency, not a test one. An empty pool is a legitimate configuration
    # (`--rir-count 0`) and orthogonal to everything asserted here.
    monkeypatch.setattr(build_dataset, "build_rir_pool", lambda count, seed: [])

    corpus = Corpus(tmp_path)
    _tts_group(corpus.tts, "positive", POSITIVE_CLIPS, seed=100)
    _tts_group(corpus.tts, "hardneg", HARDNEG_CLIPS, seed=200)
    _speech_commands(corpus.speech_commands)
    _write_human_manifest(corpus.human_root)
    return corpus


def _human(sink: RecordingSink) -> list[tuple[int, int, str, dict]]:
    """(index, label, category, params) for every human window in emission order."""
    return [
        (index, label, category, params)
        for index, (label, category, params) in enumerate(
            zip(sink.labels, sink.categories, sink.settings)
        )
        if category.endswith("_human")
    ]


# ── the split guards ────────────────────────────────────────────────────────


@pytest.mark.parametrize("split", ["validation", "eval"])
def test_human_manifest_is_refused_outside_the_train_split(
    corpus: Corpus, split: str
) -> None:
    """Human recordings are training data or they are nothing.

    Warn-and-continue would be worse than useless here: the run finishes, the
    numbers look better, and the reason is that the model was measured on the
    voice it was fitted on.
    """
    sink = RecordingSink()
    with pytest.raises(SystemExit, match=r"train-only"):
        build_dataset.build_windows(
            corpus.args(
                split=split,
                human_manifest=corpus.human_root / "MANIFEST.json",
                human_positive_repeats=2,
            ),
            sink,
        )

    # The guard has to fire before anything is planned or emitted, not after a
    # dataset has been half-written.
    assert sink.planned is None and sink.labels == []


def test_a_sealed_manifest_is_refused_even_on_the_train_split(corpus: Corpus) -> None:
    """The seal is the experiment's only held-out real voice.

    ``--split train`` is exactly the invocation that would ingest it, so the
    manifest's own declaration is checked independently of the flag. A speaker
    derived as ``eval_sealed`` must be un-ingestible, full stop.
    """
    sealed = _write_human_manifest(
        corpus.human_root / "sealed", split="eval_sealed", speaker="human-r6-sealed"
    )
    sink = RecordingSink()

    with pytest.raises(SystemExit) as raised:
        build_dataset.build_windows(
            corpus.args(human_manifest=sealed, human_positive_repeats=2), sink
        )

    message = str(raised.value)
    assert "sealed" in message, (
        "the refusal must say what it is protecting; a bare 'wrong split' "
        f"reads as a typo to fix rather than a seal to leave alone: {message!r}"
    )
    assert "eval_sealed" in message and str(sealed) in message
    assert sink.planned is None and sink.labels == []

    # Non-vacuity: the same manifest, differing only in its declared split, is
    # ingested happily -- so the refusal above is the seal and not the fixture
    # being unreadable.
    trainable = _write_human_manifest(corpus.human_root / "trainable", split="train")
    ok_sink, _ = corpus.build(human_manifest=trainable, human_positive_repeats=1)
    assert _human(ok_sink)


# ── excluded, categories and labels ─────────────────────────────────────────


def test_excluded_clips_are_skipped_and_the_rest_are_kept(corpus: Corpus) -> None:
    """``excluded`` is the only thing keeping a rejected take out of training."""
    for clip in HUMAN_CLIPS:
        assert (corpus.human_root / clip["clip"]).is_file(), (
            "every fixture clip must exist on disk, or 'skipped' cannot be "
            "told apart from 'not found'"
        )

    sink, stats = corpus.with_human(repeats=1)
    sources = {params["source"] for _, _, _, params in _human(sink)}

    assert sources == USABLE_HUMAN_SOURCES
    assert sources.isdisjoint(EXCLUDED_HUMAN_SOURCES)
    assert stats["human_manifest"]["clips_used"] == len(HUMAN_CLIPS) - EXCLUDED_HUMAN_CLIPS
    assert stats["human_manifest"]["clips_excluded"] == EXCLUDED_HUMAN_CLIPS


def test_human_windows_carry_their_own_categories_and_labels(corpus: Corpus) -> None:
    """Categories are what let evaluation attribute a result to the human data.

    Labels matter twice over: a near miss or a stretch of free speech filed as
    a positive teaches the model to fire on the speaker's ordinary talking.
    """
    sink, _ = corpus.with_human(repeats=1)

    labels_by_category: dict[str, set[int]] = defaultdict(set)
    sources_by_category: dict[str, set[str]] = defaultdict(set)
    for _, label, category, params in _human(sink):
        labels_by_category[category].add(label)
        sources_by_category[category].add(params["source"])

    assert labels_by_category == {
        "positive_human": {1},
        "near_phrase_human": {0},
        "free_speech_human": {0},
    }
    # The mapping is per manifest category, not per label: both negatives are
    # label 0 and they must still land in different buckets.
    assert sources_by_category["positive_human"] == {
        "clips/pos_01.wav",
        "clips/pos_02.wav",
    }
    assert sources_by_category["near_phrase_human"] == {"clips/near_01.wav"}
    assert sources_by_category["free_speech_human"] == {"clips/free_01.wav"}

    # Human windows must not be filed under a synthetic category, which is what
    # would make them invisible in the per-category table.
    assert "positive" not in sources_by_category
    assert set(sink.categories) >= {
        "positive",
        "near_phrase",
        "recorded_speech",
        "background_only",
    }, "the synthetic categories must still be produced alongside the human ones"


def test_an_unrecognised_manifest_category_is_a_hard_error(corpus: Corpus) -> None:
    """Skipping it would train on part of a manifest and report all of it."""
    odd = _write_human_manifest(
        corpus.human_root / "odd",
        clips=(
            {"clip": "clips/pos_01.wav", "category": "positive_neutral", "excluded": False},
            {"clip": "clips/other.wav", "category": "whisper", "excluded": False},
        ),
    )
    sink = RecordingSink()

    with pytest.raises(SystemExit, match=r"cannot label"):
        build_dataset.build_windows(
            corpus.args(human_manifest=odd, human_positive_repeats=1), sink
        )

    assert sink.planned is None and sink.labels == []


# ── group ids ───────────────────────────────────────────────────────────────


def test_group_ids_are_shared_within_a_clip_and_never_shared_with_synthetic(
    corpus: Corpus,
) -> None:
    """Groups keep every window of one utterance on one side of a split.

    A human clip whose windows scattered across group ids -- or worse, one that
    reused a synthetic clip's id -- would let a reverberated copy of a training
    window be validated on.
    """
    repeats = 4
    sink, _ = corpus.with_human(repeats=repeats)

    human_indexes = {index for index, _, _, _ in _human(sink)}
    human_groups = {sink.groups[i] for i in human_indexes}
    synthetic_groups = {
        group for i, group in enumerate(sink.groups) if i not in human_indexes
    }

    assert human_groups & synthetic_groups == set(), (
        "a human window shares a group id with a synthesized one"
    )

    groups_per_source: dict[str, set[int]] = defaultdict(set)
    windows_per_source: dict[str, int] = defaultdict(int)
    for index, _, _, params in _human(sink):
        groups_per_source[params["source"]].add(sink.groups[index])
        windows_per_source[params["source"]] += 1

    assert len(human_groups) == USABLE_HUMAN_POSITIVES + USABLE_HUMAN_NEGATIVES
    for source, groups in groups_per_source.items():
        assert len(groups) == 1, f"{source} was split across group ids {sorted(groups)}"

    # Non-vacuity: "one group per source" is trivially true if each source
    # produced a single window. The positives are repeated, so some source must
    # have produced several windows under that one id.
    assert max(windows_per_source.values()) == repeats


# ── repeat counts ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("repeats", [1, 2, 5])
def test_the_repeat_count_applies_to_human_positives_only(
    corpus: Corpus, repeats: int
) -> None:
    """Human negatives are emitted once each, whatever the positives do.

    Tying them together would move two variables per sweep step: more of the
    speaker's positives *and* more of their ordinary speech, with no way to
    tell afterwards which one moved the result.
    """
    sink, _ = corpus.with_human(repeats=repeats)

    assert sink.categories.count("positive_human") == USABLE_HUMAN_POSITIVES * repeats
    assert sink.categories.count("near_phrase_human") == 1
    assert sink.categories.count("free_speech_human") == 1


def test_zero_repeats_keeps_the_human_negatives(corpus: Corpus) -> None:
    """The control arm of the sweep: this speaker's negatives, none of their
    positives. It is a configuration the sweep actually runs, not a degenerate
    one, so it must build rather than raise."""
    sink, stats = corpus.with_human(repeats=0)

    assert sink.categories.count("positive_human") == 0
    assert sink.categories.count("near_phrase_human") == 1
    assert sink.categories.count("free_speech_human") == 1
    assert stats["human_manifest"]["windows"] == USABLE_HUMAN_NEGATIVES


@pytest.mark.parametrize("repeats", [0, 1, 4])
def test_the_preallocation_counts_the_human_windows(
    corpus: Corpus, repeats: int
) -> None:
    """``sink.begin`` is told the total before a single window exists.

    Forget the human terms and the real sink raises part-way through; count
    them twice and it finishes short, leaving zero-filled rows that train as
    silent negatives. ``RecordingSink`` reproduces both failures, so this holds
    the arithmetic rather than the sink's tolerance for it.
    """
    sink, _ = corpus.with_human(repeats=repeats)
    expected = BASELINE_WINDOWS + USABLE_HUMAN_POSITIVES * repeats + USABLE_HUMAN_NEGATIVES

    assert sink.planned == expected
    assert len(sink.labels) == expected


# ── augmentation parity ─────────────────────────────────────────────────────


def test_human_positives_go_through_the_same_augmentation(corpus: Corpus) -> None:
    """Same placement, same background, same levelling as a synthetic positive.

    A bypass here is the classic shortcut feature: if the real voice is the
    only clean, unlevelled audio in the set, "clean" separates the classes
    perfectly during training and means nothing at a microphone.
    """
    sink, _ = corpus.with_human(repeats=2)

    for index, _, category, params in _human(sink):
        assert set(params) >= {"reverb", "snr_db", "peak_dbfs", "source", "category", "label"}
        assert params["category"] == category
        assert -24.0 <= params["peak_dbfs"] <= -3.0
        assert params["snr_db"] is not None and 0.0 <= params["snr_db"] <= 25.0

        # The window is 2 s and the clip is 0.7 s placed at the trailing edge,
        # so its first half second is silence until the background is mixed in.
        # Non-silent there is direct evidence the augmenter ran on this window
        # rather than the parameters being recorded beside untouched audio.
        window = sink.windows[index]
        assert window.shape == (build_dataset.WINDOW_SAMPLES,)
        assert int(np.abs(window[:8000]).max()) > 0

    # Human positives finish near the trailing edge like synthetic ones: the
    # last frames of the window must carry the signal.
    for index, label, _, _ in _human(sink):
        if label == 1:
            assert int(np.abs(sink.windows[index][-4000:]).max()) > 0


# ── background noise: real recordings only ──────────────────────────────────


def test_the_split_constants_name_no_generated_background() -> None:
    """The rule at its source, before any split is cut.

    ``VALIDATION_NOISE`` named ``pink_noise.wav`` for seven rounds while every
    document said generated background was prohibited, because nothing connected
    the sentence to the tuple. This is that connection: put a generated name back
    into either constant and this fails.
    """
    generated = build_dataset.GENERATED_BACKGROUND_NAMES
    assert generated == frozenset({"pink_noise.wav", "white_noise.wav"})
    assert generated < set(BACKGROUND_FILES), "the fixture no longer ships what it excludes"

    for name in ("VALIDATION_NOISE", "EVAL_NOISE"):
        named = set(getattr(build_dataset, name))
        assert not named & generated, f"build_dataset.{name} names generated noise"
        assert named <= set(BACKGROUND_FILES) - generated, (
            f"build_dataset.{name} names something that is not a real recording"
        )
    # Together the two constants have to leave the train split a real recording,
    # or "no generated noise" would be satisfied by having no noise at all.
    assert set(BACKGROUND_FILES) - generated - set(build_dataset.EVAL_NOISE) - set(
        build_dataset.VALIDATION_NOISE
    ) == {"exercise_bike.wav"}


@pytest.mark.parametrize("split", sorted(BACKGROUND_BY_SPLIT))
def test_no_split_is_handed_generated_background(corpus: Corpus, split: str) -> None:
    """Measured off a build, not off the constants.

    Train and validation are cut by two different filters and evaluation by a
    third, so "excluded" has to be checked on each: an exclusion applied to one
    branch of that ``if`` is exactly the shape of the bug being fixed.
    """
    _, stats = corpus.build(split=split)

    assert stats["background_recordings"] == BACKGROUND_BY_SPLIT[split]
    assert not set(stats["background_recordings"]) & set(
        build_dataset.GENERATED_BACKGROUND_NAMES
    )
    # Named and counted rather than quietly filtered, the same way
    # ``build_human_dataset`` reports the background it drops -- a shorter list is
    # not evidence of a decision.
    assert stats["generated_background_excluded"] == ["pink_noise.wav", "white_noise.wav"]


def test_dropping_the_generated_files_cost_one_recording_each_side(corpus: Corpus) -> None:
    """What the correction actually cost, asserted rather than remembered.

    Train and validation each used to hold two background recordings, one of them
    generated. Each now holds one real recording; evaluation is untouched at two.
    Four real recordings is the whole pool, so no real audio was lost -- but a
    single-room validation background is a real reduction in what target 4 is
    measured on, and it is bounded here so it cannot be walked back quietly.
    """
    per_split = {
        split: corpus.build(split=split)[1]["background_recordings"]
        for split in BACKGROUND_BY_SPLIT
    }

    assert [len(names) for names in (per_split["train"], per_split["validation"])] == [1, 1]
    assert len(per_split["eval"]) == 2
    pooled = [name for names in per_split.values() for name in names]
    assert sorted(pooled) == sorted(set(BACKGROUND_FILES) - build_dataset.GENERATED_BACKGROUND_NAMES)
    assert len(pooled) == len(set(pooled)), "a recording is used by two splits"


# ── stats ───────────────────────────────────────────────────────────────────


def test_stats_json_records_the_manifest_hash_speaker_and_counts(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate must be traceable to the exact human data it saw.

    Run through ``main()`` rather than ``build_windows`` so this covers the
    whole path a sweep uses: the two new flags reaching argparse, and the stats
    file actually landing on disk with the extra keys in it.
    """
    monkeypatch.setattr(build_dataset, "FeatureSink", RecordingSink)
    manifest = corpus.human_root / "MANIFEST.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_dataset.py",
            "--tts-root", str(corpus.tts),
            "--speech-commands", str(corpus.speech_commands),
            "--out", str(corpus.out),
            "--split", "train",
            "--seed", "1234",
            "--positive-repeats", str(POSITIVE_REPEATS),
            "--hard-negative-repeats", str(HARD_NEGATIVE_REPEATS),
            "--recorded-negatives", "0",
            "--noise-only", str(NOISE_ONLY_WINDOWS),
            "--rir-count", "0",
            "--human-manifest", str(manifest),
            "--human-positive-repeats", "2",
        ],
    )

    assert build_dataset.main() == 0

    stats = json.loads((corpus.out / "stats_train.json").read_text(encoding="utf-8"))
    human = stats["human_manifest"]

    # Hashed independently of the implementation, over the bytes on disk.
    assert human["sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert human["speaker"] == SPEAKER
    assert human["split"] == "train"
    assert human["positive_repeats"] == 2
    assert human["windows_by_category"] == {
        "free_speech_human": 1,
        "near_phrase_human": 1,
        "positive_human": USABLE_HUMAN_POSITIVES * 2,
    }
    # The per-category counts must agree with the dataset that was written, not
    # merely with the plan that produced it.
    for name, count in human["windows_by_category"].items():
        assert stats["categories"][name] == count

    # Non-vacuity for the hash: the same clips re-derived into a byte-different
    # manifest must hash differently, or the field records a constant and
    # traces nothing.
    reformatted = _write_human_manifest(corpus.human_root, indent=4)
    assert reformatted == manifest
    _, restated = corpus.with_human(repeats=2)
    assert restated["human_manifest"]["sha256"] != human["sha256"]
    assert restated["human_manifest"]["windows_by_category"] == human["windows_by_category"]


# ── the flag being absent ───────────────────────────────────────────────────


def test_without_the_flag_the_build_is_exactly_what_it_was(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No manifest, no change: not one extra window, group, category or key.

    The human path is proved unreached rather than merely quiet -- a stats key
    or a consumed random draw on the default path would change every dataset
    this repository has already built.
    """
    calls: list[tuple[Path, str]] = []
    real_loader = build_dataset.load_human_clips

    def spy(manifest_path: Path, split: str):
        calls.append((manifest_path, split))
        return real_loader(manifest_path, split)

    monkeypatch.setattr(build_dataset, "load_human_clips", spy)

    sink, stats = corpus.build()

    assert calls == [], "the human loader ran on a build that passed no manifest"
    assert set(stats) == BASELINE_STATS_KEYS
    assert [c for c in sink.categories if c.endswith("_human")] == []
    assert sink.planned == BASELINE_WINDOWS == len(sink.labels)
    assert stats["source_utterances"] == BASELINE_GROUPS
    assert sorted(set(sink.groups)) == list(range(1, BASELINE_GROUPS + 1))

    # Non-vacuity: the identical corpus and seed, with the flag, does reach the
    # loader and does add windows. Without this the assertions above would pass
    # against a feature that was never wired up at all.
    human_sink, human_stats = corpus.with_human(repeats=3)
    assert len(calls) == 1
    assert "human_manifest" in human_stats
    assert len(human_sink.labels) > len(sink.labels)
    assert human_sink.categories.count("positive_human") == USABLE_HUMAN_POSITIVES * 3


def test_repeats_without_a_manifest_is_refused(corpus: Corpus) -> None:
    """A sweep arm that names human positives and reads none of them.

    Silently building the synthetic-only dataset instead is how a sweep comes
    back reporting that human positives made no difference.
    """
    sink = RecordingSink()
    with pytest.raises(SystemExit, match=r"--human-manifest"):
        build_dataset.build_windows(corpus.args(human_positive_repeats=4), sink)
    assert sink.planned is None and sink.labels == []


def test_a_negative_repeat_count_is_refused(corpus: Corpus) -> None:
    """``range(-1)`` is empty, so this would quietly build the control arm."""
    sink = RecordingSink()
    with pytest.raises(SystemExit, match=r">= 0"):
        build_dataset.build_windows(
            corpus.args(
                human_manifest=corpus.human_root / "MANIFEST.json",
                human_positive_repeats=-1,
            ),
            sink,
        )
    assert sink.planned is None and sink.labels == []
