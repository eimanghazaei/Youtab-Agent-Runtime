#!/usr/bin/env python3
"""Turn recorded and synthesized audio into openWakeWord feature tensors.

The classifier does not see audio. It sees the 96-dimensional embeddings that
openWakeWord's shared front end produces, sixteen frames at a time — the exact
tensor ``AudioFeatures.get_features(16)`` hands the model at inference. Fitting
on anything else would train a model for a feature space the runtime does not
use, so this stage runs the real extractors over real 2.00-second windows and
stores what comes out.

Window construction mirrors how detection actually happens. openWakeWord
rescores every 80 ms on the trailing sixteen frames, so a positive is a window
in which the phrase *finishes* near the end — that is the moment the score is
supposed to peak. Positives are placed with a small jitter around that point
rather than centred, because a model trained on centred phrases scores highest
80-300 ms after the runtime has already decided.

Augmentation is applied identically to both classes. If reverb or noise were
applied only to positives, the model could reach a low training loss by
learning "reverberant" rather than "hey youtab", and would then fire on any
reverberant speech.

Splits are disjoint by source, not by shuffling:

* synthesized speech — speakers [0, 700) train, [700, 904) evaluate
* recorded speech — Speech Commands' own ``validation_list.txt`` and
  ``testing_list.txt``, which are speaker-disjoint by construction, evaluate;
  everything else trains
* background noise — two of the six files Speech Commands ships are *generated*
  and are excluded outright; of the four real recordings, ``exercise_bike``
  trains, ``doing_the_dishes`` validates, ``running_tap`` and ``dude_miaowing``
  evaluate
* room impulse responses — independently seeded pools

so no voice, no room and no noise recording is shared between fitting and
measuring.

Recorded human speakers follow the same rule, enforced twice. A speaker's
manifest declares which side of the line it is on, and ``--human-manifest``
refuses any split but ``train`` and any manifest that does not declare itself
``train``. One speaker is a handful of voices at most, so a split that both
trains and measures on one person reports memorisation as detection — and the
sealed evaluation speaker exists precisely to be the one voice no candidate has
heard.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import wave
from pathlib import Path

import numpy as np

#: Detection window. 32,000 samples at 16 kHz produces exactly the sixteen
#: embedding frames the classifier consumes — verified, not assumed, by
#: ``test_window_length_yields_the_models_input_frames``.
WINDOW_SAMPLES = 32000
SAMPLE_RATE = 16000
FEATURE_FRAMES = 16
EMBEDDING_DIM = 96

#: Background files in Speech Commands' ``_background_noise_`` that are
#: *generated* rather than recorded. Two of its six are, and generated
#: background is prohibited exactly as generated speech is: a model that learns
#: "phrase over synthesised noise" has learned a room that does not exist.
#:
#: Named here, and counted in the stats, rather than being an invisible filter —
#: the same mechanism ``build_human_dataset.GENERATED_BACKGROUND_NAMES`` applies
#: to its ``recorded_background`` sources, so one rule holds in both builders.
GENERATED_BACKGROUND_NAMES = frozenset({"pink_noise.wav", "white_noise.wav"})

#: Real recordings held out for evaluation. Two of the four, chosen as one
#: broadband stationary source and one non-stationary source, so the held-out
#: noise is not all of one kind.
EVAL_NOISE = ("running_tap.wav", "dude_miaowing.wav")

#: Of the two real recordings evaluation does not use, this one is validation's.
#: It was two until ``pink_noise.wav`` was recognised as generated; validation
#: keeps one real room rather than one real room plus a synthesised one.
VALIDATION_NOISE = ("doing_the_dishes.wav",)

#: Speaker pools, mirrored from generate_speech so a split means the same
#: thing in both stages.
SPEAKER_POOLS = {"train": (0, 600), "validation": (600, 700), "eval": (700, 904)}

#: Signal-to-noise range for the mixed-in background, in dB. The low end is
#: deliberately hostile: 0 dB is a wake word spoken at the same level as the
#: room.
SNR_RANGE = (0.0, 25.0)

#: Fraction of windows convolved with a room impulse response. The rest stand
#: for a close-talking or headset microphone.
REVERB_FRACTION = 0.5

#: Fraction of positive windows that get an unrelated spoken word before the
#: wake phrase, so the model sees the phrase preceded by speech and not only by
#: room tone.
POSITIVE_SPEECH_PREFIX_FRACTION = 0.3

#: Where the phrase ends, relative to the window end, in seconds — i.e. how
#: much trailing context follows it inside the window.
#:
#: The lower bound is not zero, and that matters. The engine requires three
#: consecutive frames over threshold before it fires. A model trained only on
#: windows where the phrase finishes exactly at the edge scores high on one
#: frame and one frame only, because the next 80 ms of audio pushes the phrase
#: out of the trailing sixteen — so it would peak beautifully and never fire.
#:
#: 0.16-0.64 s is two to eight frames of trailing context, which teaches the
#: model to hold its score high across that whole span. That span is the
#: plateau the confirmation rule needs, and `evaluate_model.py` measures how
#: long it actually turns out to be rather than trusting this comment.
PHRASE_END_JITTER = (0.16, 0.64)

#: The split a human speaker's manifest must declare before any of its clips
#: may be read. Nothing else is ingestible on any split; a manifest marked
#: ``eval_sealed`` is the held-out speaker the whole measurement rests on.
HUMAN_TRAIN_SPLIT = "train"

#: Manifest categories that carry the wake phrase. A prefix rather than one
#: name because the recording script numbers its positive prompts
#: (``positive_neutral``, ``positive_far`` ...), and which prompts exist is the
#: session's business, not this stage's.
HUMAN_POSITIVE_PREFIX = "positive_"

#: Window category every human positive is emitted under.
HUMAN_POSITIVE_CATEGORY = "positive_human"

#: Manifest category -> window category, for the human negatives.
#:
#: Human windows keep their own category strings rather than joining
#: ``near_phrase`` or ``recorded_speech`` so `evaluate_model.py` can report
#: them apart. "The false-accept rate went down" is a different claim from "the
#: false-accept rate went down on the one person whose speech we trained on",
#: and a shared category makes the second indistinguishable from the first.
HUMAN_NEGATIVE_CATEGORIES = {
    "near_phrase": "near_phrase_human",
    "free_speech": "free_speech_human",
}


def read_wav16(path: Path) -> np.ndarray:
    """Read a 16 kHz mono 16-bit WAV as float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1:
            raise ValueError(f"{path}: expected 16 kHz mono, got "
                             f"{handle.getframerate()} Hz / {handle.getnchannels()} ch")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def build_rir_pool(count: int, seed: int) -> list[np.ndarray]:
    """Synthesize room impulse responses instead of shipping a recorded set.

    pyroomacoustics' image-source method gives a licence-free, seeded and
    arbitrarily large pool: rooms from a phone booth to a hall, absorption from
    a soft office to a tiled bathroom, and the speaker anywhere from at-the-mic
    to across the room.
    """
    import pyroomacoustics as pra  # noqa: PLC0415

    rng = np.random.default_rng(seed)
    pool: list[np.ndarray] = []
    while len(pool) < count:
        dims = rng.uniform([2.5, 2.0, 2.2], [9.0, 7.0, 3.5])
        absorption = float(rng.uniform(0.15, 0.75))
        try:
            room = pra.ShoeBox(
                dims,
                fs=SAMPLE_RATE,
                materials=pra.Material(absorption),
                max_order=8,
            )
            mic = rng.uniform([0.4, 0.4, 0.8], dims - 0.4)
            src = rng.uniform([0.4, 0.4, 0.9], dims - 0.4)
            if np.linalg.norm(mic - src) < 0.3:
                continue
            room.add_source(src)
            room.add_microphone(mic.reshape(3, 1))
            room.compute_rir()
            rir = np.asarray(room.rir[0][0], dtype=np.float32)
        except Exception:  # geometry pyroomacoustics rejects; draw again
            continue
        peak = float(np.abs(rir).max())
        if not np.isfinite(peak) or peak <= 0:
            continue
        pool.append(rir / peak)
    return pool


class Augmenter:
    """Gain, reverb and additive background, drawn from a seeded stream."""

    def __init__(self, noise_files: list[Path], rir_pool: list[np.ndarray], seed: int):
        self._rng = np.random.default_rng(seed)
        self._rir = rir_pool
        self._noise = [read_wav16(p) for p in noise_files]
        if not self._noise:
            raise ValueError("no background recordings supplied")

    def background(self, length: int) -> np.ndarray:
        """A random slice of a random background recording."""
        track = self._noise[int(self._rng.integers(len(self._noise)))]
        if track.size <= length:
            return np.tile(track, int(np.ceil(length / track.size)))[:length].copy()
        start = int(self._rng.integers(track.size - length))
        return track[start : start + length].copy()

    def apply(self, window: np.ndarray) -> tuple[np.ndarray, dict]:
        """Reverberate (sometimes), mix background, and set the level.

        Returns the audio and the settings it was made with. Recording them is
        what lets the test fixture assert it holds genuinely hard cases —
        "a positive at 3 dB SNR in a reverberant room still fires" is a claim
        about a specific window, and without the parameters there is no way to
        tell that window from a clean one.
        """
        out = window
        reverberated = self._rng.random() < REVERB_FRACTION and bool(self._rir)
        if reverberated:
            rir = self._rir[int(self._rng.integers(len(self._rir)))]
            out = np.convolve(out, rir)[: window.size].astype(np.float32)

        speech_rms = float(np.sqrt(np.mean(out**2)))
        noise = self.background(window.size)
        noise_rms = float(np.sqrt(np.mean(noise**2)))
        snr_db: float | None = None
        if speech_rms > 1e-6 and noise_rms > 1e-6:
            snr_db = float(self._rng.uniform(*SNR_RANGE))
            scale = speech_rms / (noise_rms * (10.0 ** (snr_db / 20.0)))
            out = out + noise * scale
        else:
            out = out + noise * float(self._rng.uniform(0.05, 0.5))

        peak_dbfs = float(self._rng.uniform(-24.0, -3.0))
        peak = float(np.abs(out).max())
        if peak > 0:
            out = out * (10.0 ** (peak_dbfs / 20.0) / peak)
        params = {"reverb": reverberated, "snr_db": snr_db, "peak_dbfs": peak_dbfs}
        return np.clip(out, -1.0, 1.0).astype(np.float32), params


def place_at_end(utterance: np.ndarray, rng: random.Random, prefix: np.ndarray | None) -> np.ndarray:
    """A window whose utterance finishes just before the trailing edge."""
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
    tail = int(rng.uniform(*PHRASE_END_JITTER) * SAMPLE_RATE)
    end = WINDOW_SAMPLES - tail
    clip = utterance[-WINDOW_SAMPLES:]
    start = max(0, end - clip.size)
    window[start:end] = clip[-(end - start) :]
    if prefix is not None and start > 0:
        room = min(start, prefix.size)
        gap = int(rng.uniform(0.05, 0.30) * SAMPLE_RATE)
        head = max(0, start - gap - room)
        window[head : head + room] = prefix[-room:]
    return window


def place_anywhere(utterance: np.ndarray, rng: random.Random) -> np.ndarray:
    """A window with the utterance dropped in at a random offset."""
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
    clip = utterance[-WINDOW_SAMPLES:]
    if clip.size >= WINDOW_SAMPLES:
        return clip[:WINDOW_SAMPLES].astype(np.float32)
    start = rng.randrange(0, WINDOW_SAMPLES - clip.size)
    window[start : start + clip.size] = clip
    return window


def _speaker_bucket(path: Path) -> int:
    """0-99 bucket from a hash of the Speech Commands speaker id.

    The dataset's own convention: the id is the filename up to `_nohash_`, and
    partitioning on a hash of it keeps every utterance by one speaker together.
    """
    speaker = path.name.split("_nohash_")[0]
    return int(hashlib.sha1(speaker.encode("utf-8")).hexdigest(), 16) % 100


def speech_commands_split(root: Path) -> tuple[list[Path], list[Path]]:
    """(train, evaluate) recorded-negative paths using the dataset's own lists.

    The published lists are assigned by a hash of the speaker id, so a speaker
    who appears in one never appears in the other.
    """
    held: set[str] = set()
    for name in ("validation_list.txt", "testing_list.txt"):
        held.update(
            line.strip()
            for line in (root / name).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    train: list[Path] = []
    evaluate: list[Path] = []
    for wav in sorted(root.glob("*/*.wav")):
        if wav.parent.name.startswith("_"):
            continue
        (evaluate if f"{wav.parent.name}/{wav.name}" in held else train).append(wav)
    return train, evaluate


def _source(path: Path) -> str:
    """`directory/file`, because every TTS group numbers its clips from zero.

    A bare `000000.wav` appears in six directories, so a manifest keyed on it
    silently joins the wrong text to the wrong window -- which is exactly what
    happened the first time the false accepts were broken down by phrase, and
    it produced a table showing the *positive* spellings as the top confusions.
    """
    return f"{path.parent.name}/{path.name}"


def _tts_clips(directory: Path, speakers: tuple[int, int] | None = None) -> list[Path]:
    """Clips in ``directory``, optionally restricted to a speaker range.

    The filter is what lets an earlier round's clips be reused after the
    speaker pools were re-cut. Those were drawn from [0, 700) before a
    validation pool was carved out of the top of that range; keeping only the
    clips whose *both* mixed speakers fall inside the new training range
    reuses about three quarters of them without leaking a validation voice
    into training. Both speakers, not either: the generator interpolates
    between the pair, so a clip mixing a training voice with a validation one
    is partly a validation voice.
    """
    if not directory.exists():
        return []
    manifest = directory / "manifest.jsonl"
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line]
    if speakers is not None:
        lo, hi = speakers
        rows = [
            row
            for row in rows
            if lo <= row.get("speaker_1", -1) < hi and lo <= row.get("speaker_2", -1) < hi
        ]
    return [directory / row["file"] for row in rows]


def sha256_file(path: Path) -> str:
    """The manifest's own hash, so a candidate can name the data it saw.

    A speaker's clips are re-derived — takes get re-cut, a bad one gets
    excluded — and every one of those edits produces a different training set
    under the same file name. Recording the hash is what makes "this model was
    fit on that human data" checkable a month later instead of asserted.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_human_clips(
    manifest_path: Path, split: str
) -> tuple[dict, list[tuple[Path, str, int]]]:
    """A human speaker's manifest, reduced to the clips this split may train on.

    Returns the parsed manifest and one ``(path, window category, label)`` per
    usable clip, in manifest order. Paths are resolved against the manifest's
    own directory, which is what the manifest stores them relative to.

    Every rejection here is a hard stop rather than a warning. Each one is a
    way the experiment reads as valid while measuring something else:

    * a human speaker on the validation or evaluation side scores the model
      against a voice it was fitted on;
    * a manifest that declares a split other than ``train`` is the sealed
      evaluation speaker, whose whole purpose is to be unheard;
    * an ``excluded`` clip is a take somebody listened to and rejected —
      clipped, coughed through, wrong phrase — and re-deriving that judgement
      from durations or transcripts would quietly overrule it;
    * a category this stage does not recognise would otherwise be dropped, and
      the run would train on part of a manifest while reporting all of it.
    """
    if split != HUMAN_TRAIN_SPLIT:
        raise SystemExit(
            f"--human-manifest is {HUMAN_TRAIN_SPLIT}-only, but --split {split} "
            "was requested. One person supplies a handful of voices at most, so "
            "a validation or evaluation split carrying them measures the model "
            "against a voice it was fitted on and reports that as a result."
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in ("speaker", "split", "clips"):
        if field not in manifest:
            raise SystemExit(f"{manifest_path}: manifest has no {field!r} field")

    declared = manifest["split"]
    if declared != HUMAN_TRAIN_SPLIT:
        raise SystemExit(
            f"{manifest_path}: this manifest declares split {declared!r}, not "
            f"{HUMAN_TRAIN_SPLIT!r}, so it is refused on every split. A speaker "
            "sealed for evaluation is the only measurement of whether the model "
            "generalises to a real voice; train on it and every later number "
            "about it describes what the model memorised. The seal is not "
            "overridable from the command line -- re-derive the speaker as a "
            "training speaker if that is genuinely what is wanted."
        )

    usable: list[tuple[Path, str, int]] = []
    for index, clip in enumerate(manifest["clips"]):
        excluded = clip.get("excluded")
        if not isinstance(excluded, bool):
            raise SystemExit(
                f"{manifest_path}: clips[{index}] has excluded={excluded!r}, "
                "which is not a boolean. That flag is the only thing keeping a "
                "rejected take out of training, and a missing one reads as "
                "false while a string reads as true whatever it spells."
            )
        if excluded:
            continue

        # `clip`, not `path`: the deriver names this key after what it holds,
        # and a manifest is only ever written by that deriver.
        relative = clip.get("clip")
        if not relative:
            raise SystemExit(f"{manifest_path}: clips[{index}] has no 'clip'")
        path = manifest_path.parent / relative
        if not path.is_file():
            raise SystemExit(
                f"{manifest_path}: clips[{index}] points at {path}, which is "
                "not a file. Paths are relative to the manifest's own directory."
            )

        # Category decides the label, so an unrecognised one cannot be guessed
        # at: calling an unknown category a negative would put the phrase in
        # the negatives if it ever named a new positive prompt.
        declared_category = clip.get("category")
        category, label = None, 0
        if isinstance(declared_category, str):
            if declared_category.startswith(HUMAN_POSITIVE_PREFIX):
                category, label = HUMAN_POSITIVE_CATEGORY, 1
            elif declared_category in HUMAN_NEGATIVE_CATEGORIES:
                category, label = HUMAN_NEGATIVE_CATEGORIES[declared_category], 0
        if category is None:
            raise SystemExit(
                f"{manifest_path}: clips[{index}] has category "
                f"{declared_category!r}, which this stage cannot label. Expected "
                f"{HUMAN_POSITIVE_PREFIX}* or one of "
                f"{sorted(HUMAN_NEGATIVE_CATEGORIES)}."
            )
        usable.append((path, category, label))
    return manifest, usable


def build_windows(args: argparse.Namespace, sink: "FeatureSink") -> dict:
    """Emit every labelled window for one split into ``sink``.

    Streamed rather than returned. The training split is 124,000 windows of
    two seconds each -- 7.9 GB as int16, and `np.stack` on a list that size
    needs the same again while it copies, which is more memory than the
    machine this was built on has. Handing each window to a sink that
    extracts its features and drops the audio keeps the peak at one chunk.
    """
    rng = random.Random(args.seed)
    tts = Path(args.tts_root)
    suffix = args.split
    speakers = SPEAKER_POOLS[args.split]

    def clips(group: str) -> list[Path]:
        """Clips for ``group``, from this split's directories and speakers.

        A split reads its own directory first; where an earlier round only
        produced a `_train` directory, it also reads that one under the
        speaker filter, so nothing has to be regenerated to honour a re-cut
        pool.
        """
        seen: list[Path] = []
        # `<group>_<split>` plus any `<group>_<split>_<tag>` siblings, so a
        # later round can add clips -- a new accent set, more of a phrase that
        # was measured to confuse -- without renumbering an existing directory.
        for directory in sorted(tts.glob(f"{group}_{suffix}")) + sorted(
            tts.glob(f"{group}_{suffix}_*")
        ):
            seen.extend(_tts_clips(directory, speakers))
        return seen

    # Read the human manifest before anything expensive happens. Every check
    # inside it is cheap and every one of them is fatal, so finding out after
    # the impulse-response pool has been synthesized and the sink has
    # preallocated is minutes wasted for no reason.
    human_repeats = args.human_positive_repeats
    if human_repeats < 0:
        raise SystemExit(
            f"--human-positive-repeats must be >= 0, got {human_repeats}. A "
            "negative count would emit nothing at all, which is the control "
            "arm of the sweep wearing another arm's name."
        )
    if args.human_manifest is None:
        if human_repeats > 0:
            raise SystemExit(
                f"--human-positive-repeats {human_repeats} was given without "
                "--human-manifest, so no human clip would be read. That builds "
                "a synthetic-only dataset under a name that says otherwise, and "
                "the sweep would show human positives making no difference."
            )
        human_manifest: dict | None = None
        human: list[tuple[Path, str, int]] = []
    else:
        human_manifest, human = load_human_clips(Path(args.human_manifest), args.split)

    sc_root = Path(args.speech_commands)
    sc_train, sc_eval = speech_commands_split(sc_root)
    if args.split == "eval":
        recorded = sc_eval
    else:
        # Training and validation share the recorded-negative pool by
        # partitioning it on a hash of the speaker id, the same way Speech
        # Commands partitions its own lists, so no speaker crosses the line.
        recorded = [p for p in sc_train if (_speaker_bucket(p) < 85) == (args.split == "train")]

    noise_dir = sc_root / "_background_noise_"
    all_noise = sorted(p for p in noise_dir.glob("*.wav"))
    # Generated noise is removed before any split is cut, so no split can be
    # given it by an ordering accident, and the drop is reported rather than
    # inferred from a shorter list.
    generated_noise = [p for p in all_noise if p.name in GENERATED_BACKGROUND_NAMES]
    real_noise = [p for p in all_noise if p.name not in GENERATED_BACKGROUND_NAMES]
    noise_files = [p for p in real_noise if (p.name in EVAL_NOISE) == (args.split == "eval")]
    if args.split == "validation":
        # Validation gets its own share of the real non-evaluation recordings,
        # so a threshold chosen on it is not chosen against the same room tone
        # the model trained in.
        noise_files = [p for p in noise_files if p.name in VALIDATION_NOISE]
    elif args.split == "train":
        noise_files = [p for p in noise_files if p.name not in VALIDATION_NOISE]

    rir_pool = build_rir_pool(args.rir_count, seed=args.seed + 7919)
    aug = Augmenter(noise_files, rir_pool, seed=args.seed + 104729)

    group_id = 0
    category = "positive"
    prefix_pool = [p for p in recorded[:: max(1, len(recorded) // 4000)]][:4000]

    # Every window derived from one source utterance shares a group id, so a
    # validation split can keep augmented copies of the same recording on the
    # same side of the line. Splitting those at random would let the model see
    # a reverberated copy of a clip it is then validated on.
    #
    # `category` records what each window is, so a false accept can be
    # attributed to a near miss rather than averaged into one number with room
    # tone. Both are read out of the enclosing scope by `emit`.
    def emit(window: np.ndarray, label: int, source: str) -> None:
        augmented, params = aug.apply(window)
        params["source"] = source
        params["category"] = category
        params["label"] = label
        sink.add((augmented * 32767.0).astype(np.int16), label, group_id, category, params)

    positives = clips("positive")
    wanted = args.recorded_negatives if args.recorded_negatives > 0 else len(recorded)
    chosen = recorded if wanted >= len(recorded) else rng.sample(recorded, wanted)
    # Human negatives are emitted once each whatever the positive repeat count
    # is, so they are counted at one apiece here too. The sink preallocates
    # from this number and refuses to grow, so a term missing from this sum
    # aborts the run at the overflow -- and a term too large leaves zero-filled
    # rows that train as silent negatives.
    human_windows = sum(human_repeats if label == 1 else 1 for _, _, label in human)
    # The total is knowable before a single window exists, which is what lets
    # the sink preallocate instead of growing.
    sink.begin(
        len(positives) * args.positive_repeats
        + len(clips("hardneg")) * args.hard_negative_repeats
        + len(clips("confusable")) * args.confusable_repeats
        + len(clips("softneg"))
        + len(clips("common"))
        + len(chosen)
        + args.noise_only
        + human_windows
    )
    category = "positive"
    for path in positives:
        audio = read_wav16(path)
        group_id += 1
        for _ in range(args.positive_repeats):
            prefix = None
            if prefix_pool and rng.random() < POSITIVE_SPEECH_PREFIX_FRACTION:
                prefix = read_wav16(prefix_pool[rng.randrange(len(prefix_pool))])
            emit(place_at_end(audio, rng, prefix), 1, _source(path))

    # Recorded human clips, positive and negative, take the same placement,
    # prefix draw and augmentation as the synthesized ones directly above. A
    # separate path would give the model a second way to tell the two apart --
    # "the clean-sounding voice is the real one" is a rule that fits the
    # training set perfectly and means nothing at a microphone.
    #
    # Their group ids come from the same counter, so no human window ever
    # shares a group with a synthesized one and every window derived from one
    # utterance stays together.
    for path, human_category, label in human:
        audio = read_wav16(path)
        category = human_category
        group_id += 1
        # The repeat count is a positives-only dial. Repeating the negatives
        # alongside them would move two variables per sweep step, and the
        # question the sweep asks is how much of one voice's *positive* speech
        # the model needs.
        for _ in range(human_repeats if label == 1 else 1):
            prefix = None
            if label == 1 and prefix_pool and rng.random() < POSITIVE_SPEECH_PREFIX_FRACTION:
                prefix = read_wav16(prefix_pool[rng.randrange(len(prefix_pool))])
            emit(place_at_end(audio, rng, prefix), label, _source(path))

    for group, repeats in (
        ("hardneg", args.hard_negative_repeats),
        ("confusable", args.confusable_repeats),
        ("softneg", 1),
        ("common", 1),
    ):
        category = {
            "softneg": "synthesized_speech",
            "common": "common_speech",
        }.get(group, "near_phrase")
        for path in clips(group):
            audio = read_wav16(path)
            group_id += 1
            for _ in range(repeats):
                # Near misses are placed at the trailing edge too. A negative
                # that only ever appears mid-window would let the model reject
                # it on position rather than on what was said.
                emit(place_at_end(audio, rng, None), 0, _source(path))

    category = "recorded_speech"
    for path in chosen:
        audio = read_wav16(path)
        group_id += 1
        if rng.random() < 0.35 and len(chosen) > 1:
            other = read_wav16(chosen[rng.randrange(len(chosen))])
            gap = np.zeros(int(rng.uniform(0.0, 0.25) * SAMPLE_RATE), dtype=np.float32)
            audio = np.concatenate([audio, gap, other])
        emit(place_anywhere(audio, rng), 0, _source(path))

    category = "background_only"
    for _ in range(args.noise_only):
        group_id += 1
        emit(np.zeros(WINDOW_SAMPLES, dtype=np.float32), 0, "background")

    stats = {
        "split": args.split,
        "seed": args.seed,
        "windows": len(sink.labels),
        "positives": int(sum(sink.labels)),
        "negatives": int(len(sink.labels) - sum(sink.labels)),
        "synthesized_positive_clips": len(positives),
        "recorded_negative_clips": len(chosen),
        "background_recordings": [p.name for p in noise_files],
        "generated_background_excluded": [p.name for p in generated_noise],
        "impulse_responses": len(rir_pool),
        "window_seconds": WINDOW_SAMPLES / SAMPLE_RATE,
        "source_utterances": group_id,
        "categories": {
            name: sink.categories.count(name) for name in sorted(set(sink.categories))
        },
    }
    if human_manifest is not None:
        # Counted off what the sink actually holds, not off the plan above, so
        # this cannot agree with the preallocation while disagreeing with the
        # dataset. The hash and the speaker are what tie a trained candidate to
        # one exact derivation of one person's recordings.
        stats["human_manifest"] = {
            "path": str(args.human_manifest),
            "sha256": sha256_file(Path(args.human_manifest)),
            "speaker": human_manifest["speaker"],
            "split": human_manifest["split"],
            "clips_in_manifest": len(human_manifest["clips"]),
            "clips_excluded": len(human_manifest["clips"]) - len(human),
            "clips_used": len(human),
            "positive_repeats": human_repeats,
            "windows": human_windows,
            "windows_by_category": {
                name: sink.categories.count(name)
                for name in sorted({name for _, name, _ in human})
            },
        }
    return stats


class FeatureSink:
    """Turns a stream of audio windows into feature tensors on the fly.

    Holds one chunk of audio at a time. The features themselves are small --
    16x96 floats per window, about 6 KB against 64 KB of audio -- so those are
    kept in one preallocated array, and the waveforms are written straight to a
    disk-backed array when the caller asks to keep them.
    """

    def __init__(self, out: Path, split: str, keep_audio: bool, chunk: int,
                 batch_size: int, ncpu: int):
        from openwakeword.utils import AudioFeatures  # noqa: PLC0415

        self._front_end = AudioFeatures(inference_framework="onnx", ncpu=ncpu)
        self._out = out
        self._split = split
        self._keep_audio = keep_audio
        self._chunk = chunk
        self._batch_size = batch_size
        self._ncpu = ncpu
        self._buffer: list[np.ndarray] = []
        self._written = 0
        self._total = 0
        self.features: np.ndarray | None = None
        self.audio: np.ndarray | None = None
        self.labels: list[int] = []
        self.groups: list[int] = []
        self.categories: list[str] = []
        self.settings: list[dict] = []

    def begin(self, total: int) -> None:
        self._total = total
        self.features = np.empty((total, FEATURE_FRAMES, EMBEDDING_DIM), dtype=np.float32)
        if self._keep_audio:
            self.audio = np.lib.format.open_memmap(
                self._out / f"audio_{self._split}.npy",
                mode="w+",
                dtype=np.int16,
                shape=(total, WINDOW_SAMPLES),
            )
        print(f"  {total} windows planned")

    def add(self, window: np.ndarray, label: int, group: int, category: str,
            params: dict) -> None:
        self._buffer.append(window)
        self.labels.append(label)
        self.groups.append(group)
        self.categories.append(category)
        self.settings.append(params)
        if len(self._buffer) >= self._chunk:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        chunk = np.stack(self._buffer)
        self._buffer = []
        embedded = self._front_end.embed_clips(
            chunk, batch_size=self._batch_size, ncpu=self._ncpu
        )
        if embedded.shape[1:] != (FEATURE_FRAMES, EMBEDDING_DIM):
            raise RuntimeError(
                f"front end produced {embedded.shape[1:]}, expected "
                f"({FEATURE_FRAMES}, {EMBEDDING_DIM}) -- window length is wrong"
            )
        stop = self._written + len(chunk)
        if self.features is None or stop > self._total:
            raise RuntimeError(
                f"emitted more windows than planned ({stop} > {self._total})"
            )
        self.features[self._written : stop] = embedded
        if self.audio is not None:
            self.audio[self._written : stop] = chunk
        self._written = stop
        print(f"  features {self._written}/{self._total}", end="\r", flush=True)

    def finish(self) -> None:
        self._flush()
        print()
        if self._written != self._total:
            raise RuntimeError(
                f"planned {self._total} windows but emitted {self._written}"
            )
        if self.audio is not None:
            self.audio.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tts-root", type=Path, required=True)
    parser.add_argument("--speech-commands", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--split", required=True, choices=("train", "validation", "eval"))
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--positive-repeats", type=int, default=2)
    parser.add_argument("--hard-negative-repeats", type=int, default=2)
    parser.add_argument(
        "--confusable-repeats",
        type=int,
        default=2,
        help="windows per clip in the measured-confusable set (may be absent)",
    )
    parser.add_argument(
        "--recorded-negatives",
        type=int,
        default=50000,
        help="0 uses every clip on this side of the split",
    )
    parser.add_argument(
        "--human-manifest",
        type=Path,
        default=None,
        help="MANIFEST.json of a derived human speaker; --split train only, and "
             "only for a manifest that declares itself a training speaker",
    )
    parser.add_argument(
        "--human-positive-repeats",
        type=int,
        default=0,
        help="windows per usable human positive clip (0 disables them). Human "
             "negatives are always emitted once each, whatever this is set to",
    )
    parser.add_argument("--noise-only", type=int, default=4000)
    parser.add_argument("--rir-count", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument(
        "--chunk",
        type=int,
        default=2048,
        help="windows held in memory at once (2048 is ~130 MB of audio)",
    )
    parser.add_argument("--ncpu", type=int, default=4)
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="also write the raw windows, for listening or for the sample set",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"building {args.split} windows")
    sink = FeatureSink(
        args.out, args.split, args.keep_audio, args.chunk, args.batch_size, args.ncpu
    )
    stats = build_windows(args, sink)
    sink.finish()
    print(f"  {stats['windows']} windows "
          f"({stats['positives']} positive / {stats['negatives']} negative)")

    np.save(args.out / f"x_{args.split}.npy", sink.features)
    np.save(args.out / f"y_{args.split}.npy", np.array(sink.labels, dtype=np.uint8))
    np.save(args.out / f"groups_{args.split}.npy", np.array(sink.groups, dtype=np.int32))
    (args.out / f"categories_{args.split}.json").write_text(
        json.dumps(sink.categories), encoding="utf-8"
    )
    (args.out / f"settings_{args.split}.json").write_text(
        json.dumps(sink.settings), encoding="utf-8"
    )
    (args.out / f"stats_{args.split}.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}/x_{args.split}.npy {sink.features.shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
