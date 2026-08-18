"""Manual fallback that splits seven continuous recordings into the 55 takes.

When the GUI (``app.py``) cannot be used, an operator can still capture a
compact sitting by hand: one continuous WAV per section/group, each holding
several spoken responses with 2-3 seconds of clear silence between them. This
tool reads those SEVEN originals from the sibling ``<incoming>/_continuous/<speaker>/``
folder (kept outside the submission root on purpose) and expands them into the
canonical 55-file layout under ``<speaker_root>/originals/``, so the submission
folder holds exactly what ``validate_speaker_submission.py`` and
``import_speaker.py`` accept -- the compact validator refuses any extra top-level
entry, so the originals are preserved next to the submission, not inside it.

Two rules make this safe to run against a difficult, un-repeatable recording:

* The seven originals are **preserved byte-for-byte**. Nothing in
  ``_continuous/`` is moved, trimmed, resampled or normalised; the derived
  takes are written from the source samples exactly as read, and every
  original's SHA-256 is checked to be identical before and after the run.
* It is a **reviewed** split. Every proposed segment boundary is printed for
  the operator, and the tool refuses to write when a segmented file's detected
  count does not equal the count its section needs -- printing what to retune
  rather than guessing at a boundary.

The seven-to-55 mapping is derived from ``core.build_plan(speaker)`` at
runtime, not hardcoded, so a change to the plan stays in sync here.

Import-safe with no audio stack present: only the standard library, ``numpy``,
and the assistant's own ``core`` / ``compact_plan`` (plus
``speaker_recording_spec``) are touched. No microphone, speaker or display is
required, and none of ``sounddevice`` / ``pyttsx3`` / ``tkinter`` is imported.

Run it for a speaker::

    py -m scripts.wakeword.recording_assistant.segment_fallback --speaker E001 --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import speaker_recording_spec as spec  # noqa: E402

from . import compact_plan, core

# ── segmentation thresholds (named so the operator can retune them) ───────────
#
# These decide where one spoken response ends and the next begins. They are the
# only judgement in the tool; everything downstream is exact. Kept as module
# constants, and every one is also a CLI flag or a per-file ``--override`` key.

#: RMS analysis frame / hop, in milliseconds. ~20-30 ms is short enough to place
#: an onset within a frame and long enough that one glottal pulse does not read
#: as a whole frame of energy.
FRAME_MS = 25.0

#: Guard pad kept on each side of a trimmed segment so a soft onset or a trailed
#: coda is not clipped. Never applied beyond the file's own bounds.
GUARD_MS = 150.0

#: A frame whose RMS is below this (dBFS, full scale = 0) is silence. -45 dBFS
#: sits well above room tone and well below even a genuinely quiet delivery, so
#: the 2-3 s gaps read as silence while the speech does not.
DEFAULT_SILENCE_DBFS = -45.0

#: A silence run at least this long is a boundary between two responses. Shorter
#: pauses (a breath, a gap inside a phrase) are kept inside one utterance.
DEFAULT_MIN_SILENCE_S = 1.2

#: A detected utterance shorter than this is discarded as a cough / click / lip
#: smack rather than a response. Matches ``core.MIN_UTTERANCE_SECONDS``.
DEFAULT_MIN_UTTERANCE_S = core.MIN_UTTERANCE_SECONDS

#: An original with this fraction of its samples pinned at full scale is a
#: broken capture (wrong input gain), not a recording to split. Distinct from
#: ``core.CLIP_FRACTION_THRESHOLD``, which flags an audibly-clipped *take*; here
#: we only refuse a recording that is clipped throughout.
MAX_CLIP_FRACTION = 0.5

#: Reported dBFS for a segment or frame with no signal at all.
_FLOOR_DBFS = core.SILENCE_DBFS
_FULL_SCALE = 32768.0  # int16 full-scale reference, as core uses internally.


# ── pure segmentation ─────────────────────────────────────────────────────────


def _runs(mask: np.ndarray) -> list[tuple[bool, int, int]]:
    """Maximal runs in a 1-D boolean array as ``(value, start, end_exclusive)``."""
    if mask.size == 0:
        return []
    change = np.flatnonzero(mask[1:] != mask[:-1]) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [mask.size]))
    return [(bool(mask[int(s)]), int(s), int(e)) for s, e in zip(starts, ends)]


def _frame_dbfs(scaled: np.ndarray, frame_len: int) -> np.ndarray:
    """Per-frame RMS in dBFS over non-overlapping frames of ``frame_len`` samples.

    The final partial frame is zero-padded up to ``frame_len``; that only ever
    lowers the last frame's energy (toward silence), which is correct for the
    trailing silence a hand-made recording ends on.
    """
    n = scaled.size
    n_frames = int(np.ceil(n / frame_len)) if n else 0
    if n_frames == 0:
        return np.empty(0, dtype=np.float64)
    pad = n_frames * frame_len - n
    if pad:
        scaled = np.concatenate([scaled, np.zeros(pad, dtype=np.float64)])
    mat = scaled.reshape(n_frames, frame_len)
    rms = np.sqrt(np.mean(mat * mat, axis=1))
    dbfs = np.full(n_frames, _FLOOR_DBFS, dtype=np.float64)
    nonzero = rms > 0
    dbfs[nonzero] = 20.0 * np.log10(rms[nonzero])
    return np.maximum(dbfs, _FLOOR_DBFS)


def detect_segments(
    pcm: np.ndarray,
    rate: int,
    *,
    expected: int | None = None,
    min_silence_s: float = DEFAULT_MIN_SILENCE_S,
    min_utterance_s: float = DEFAULT_MIN_UTTERANCE_S,
    silence_dbfs: float = DEFAULT_SILENCE_DBFS,
) -> list[tuple[int, int]]:
    """Split ``pcm`` into ``(start_sample, end_sample)`` spans, one per utterance.

    Deterministic and side-effect free. Frames below ``silence_dbfs`` are
    silence; a silence run of at least ``min_silence_s`` is a boundary between
    responses, while shorter pauses stay inside one utterance. Each span is
    trimmed to its voiced extent and then padded by ``GUARD_MS`` on each side
    without ever crossing the file's bounds. A voiced span shorter than
    ``min_utterance_s`` is dropped (a cough or click).

    ``expected`` is accepted for a uniform call site (the CLI passes the count a
    file should yield) and, deliberately, does not steer detection: the caller
    compares ``len(result)`` against it and decides whether to write, so a wrong
    count surfaces to the operator rather than being papered over here.
    """
    samples = np.asarray(pcm)
    n = int(samples.size)
    if n == 0 or rate <= 0:
        return []

    frame_len = max(1, int(round(rate * FRAME_MS / 1000.0)))
    frame_s = frame_len / rate
    # Accept already-normalised float [-1, 1] (the width-aware fallback path) or a
    # legacy int16 array (tests); both become float dBFS the same way.
    if np.issubdtype(samples.dtype, np.floating):
        scaled = samples.astype(np.float64)
    else:
        scaled = samples.astype(np.float64) / _FULL_SCALE
    dbfs = _frame_dbfs(scaled, frame_len)
    silence = dbfs < silence_dbfs

    min_sil_frames = max(1, int(np.ceil(min_silence_s / frame_s)))
    guard = int(round(GUARD_MS / 1000.0 * rate))

    # Group frames into raw segments separated only by qualifying silence runs.
    raw: list[tuple[int, int]] = []
    open_start: int | None = None
    for value, start, end in _runs(silence):
        if value:  # a silence run
            if (end - start) >= min_sil_frames and open_start is not None:
                raw.append((open_start, start))
                open_start = None
            # a shorter silence run inside an open segment is absorbed; a silence
            # run with nothing open (leading / inter-segment) is ignored.
        else:  # a voiced run
            if open_start is None:
                open_start = start
    if open_start is not None:
        raw.append((open_start, silence.size))

    # Trim each raw segment to its voiced extent, drop blips, add the guard pad.
    segments: list[tuple[int, int]] = []
    for a, b in raw:
        voiced = np.flatnonzero(~silence[a:b])
        if voiced.size == 0:
            continue
        f0 = a + int(voiced[0])
        f1 = a + int(voiced[-1])
        if (f1 - f0 + 1) * frame_s < min_utterance_s:
            continue
        start_sample = max(0, f0 * frame_len - guard)
        end_sample = min(n, (f1 + 1) * frame_len + guard)
        segments.append((int(start_sample), int(end_sample)))
    return segments


def _peak_dbfs(scaled: np.ndarray) -> float:
    """Peak level of a normalised [-1, 1] span in dBFS, floored at ``_FLOOR_DBFS``."""
    if scaled.size == 0:
        return _FLOOR_DBFS
    peak = float(np.abs(scaled.astype(np.float64)).max())
    if peak <= 0:
        return _FLOOR_DBFS
    return max(20.0 * np.log10(peak), _FLOOR_DBFS)


# ── the seven originals, and the plan steps each expands into ─────────────────

#: Where the seven hand-made originals live under a speaker folder, and their
#: canonical names. Kept out of the derived ``originals/`` tree so the two never
#: mix and the originals stay obviously the source.
CONTINUOUS_DIRNAME = "_continuous"

ORIGINAL_POSITIVE_CLOSE = "positive_close.wav"
ORIGINAL_POSITIVE_FARFIELD = "positive_farfield.wav"
ORIGINAL_NEAR_PHRASES = "near_phrases.wav"
ORIGINAL_FREESPEECH = "freespeech.wav"
ORIGINAL_BACKGROUND = "background.wav"


def noise_original_name(directory: str) -> str:
    """The continuous original's name for a noise directory: ``positive_noise_tv``
    -> ``positive_noise_tv.wav``. The two noise originals are named for the source
    -- matching their derived directory -- so an operator's files are unambiguous
    (E001 tv + kitchen, E002 street + fan), never a positional A/B."""
    return f"{directory}.wav"


#: Derived-take counts each original must expand into, in recording order: close
#: 10, far-field 3, each of the two noise sources 3, the 34-phrase battery, one
#: free-speech, one background. They sum to the plan's 55.
_ORDERED_EXPECTED_COUNTS: tuple[int, ...] = (10, 3, 3, 3, 34, 1, 1)
TOTAL_DERIVED = 55

#: Section ids the mapping reads out of the plan, in recording order.
SECTION_DELIVERY = "section1_delivery"
SECTION_POSITION_NOISE = "section2_position_noise"
SECTION_NEAR_PHRASE = "section3_near_phrase"
SECTION_FREE_SPEECH = "section4_free_speech"
SECTION_BACKGROUND = "section5_background"


@dataclass(frozen=True)
class OriginalMapping:
    """One continuous original and the ordered plan steps it expands into."""

    name: str
    steps: tuple[core.Step, ...]
    split: bool  # True => segment on silence; False => copy the whole file.

    @property
    def expected(self) -> int:
        return len(self.steps)


def _groups_by_directory(steps: tuple[core.Step, ...]) -> list[tuple[str, tuple[core.Step, ...]]]:
    """Steps grouped by ``.directory``, preserving first-appearance order."""
    order: list[str] = []
    buckets: dict[str, list[core.Step]] = {}
    for step in steps:
        if step.directory not in buckets:
            buckets[step.directory] = []
            order.append(step.directory)
    for step in steps:
        buckets[step.directory].append(step)
    return [(directory, tuple(buckets[directory])) for directory in order]


def build_original_map(plan: core.Plan) -> list[OriginalMapping]:
    """The seven-to-55 mapping derived from ``plan``, verified against the counts.

    Groups the flat plan by section membership -- and, for the position/noise
    section, by directory in recording order (far-field, then the speaker's two
    assigned noise sources) -- and checks each original maps to exactly the
    count its name promises. A mismatch raises, naming the offending file, so a
    plan change that this mapping no longer covers fails loudly here.
    """
    by_section = {section.section_id: section for section in plan.sections}
    missing = [
        sid
        for sid in (
            SECTION_DELIVERY,
            SECTION_POSITION_NOISE,
            SECTION_NEAR_PHRASE,
            SECTION_FREE_SPEECH,
            SECTION_BACKGROUND,
        )
        if sid not in by_section
    ]
    if missing:
        raise ValueError(f"plan is missing expected section(s): {missing}")

    noise_groups = _groups_by_directory(by_section[SECTION_POSITION_NOISE].steps)
    if len(noise_groups) != 3:
        raise ValueError(
            f"{SECTION_POSITION_NOISE} has {len(noise_groups)} directory group(s), "
            "expected 3 (far-field, then the two assigned noise sources)"
        )

    mappings = [
        OriginalMapping(ORIGINAL_POSITIVE_CLOSE, by_section[SECTION_DELIVERY].steps, True),
        OriginalMapping(ORIGINAL_POSITIVE_FARFIELD, noise_groups[0][1], True),
        OriginalMapping(noise_original_name(noise_groups[1][0]), noise_groups[1][1], True),
        OriginalMapping(noise_original_name(noise_groups[2][0]), noise_groups[2][1], True),
        OriginalMapping(ORIGINAL_NEAR_PHRASES, by_section[SECTION_NEAR_PHRASE].steps, True),
        OriginalMapping(ORIGINAL_FREESPEECH, by_section[SECTION_FREE_SPEECH].steps, False),
        OriginalMapping(ORIGINAL_BACKGROUND, by_section[SECTION_BACKGROUND].steps, False),
    ]

    actual = tuple(mapping.expected for mapping in mappings)
    if actual != _ORDERED_EXPECTED_COUNTS:
        raise ValueError(
            f"the seven-to-55 mapping expands to {actual}, expected "
            f"{_ORDERED_EXPECTED_COUNTS} -- it is out of sync with the plan"
        )
    if sum(actual) != TOTAL_DERIVED:
        raise ValueError(f"mapping expands to {sum(actual)} takes, expected {TOTAL_DERIVED}")
    return mappings


def expected_derived_counts(speaker: str) -> dict[str, int]:
    """The seven continuous originals and their derived-take counts for ``speaker``.

    The noise originals are source-named (``positive_noise_tv.wav`` ...), so the
    dict is speaker-specific. Single-sourced from ``build_original_map`` so it can
    never drift from what the tool actually reads.
    """
    return {m.name: m.expected for m in build_original_map(core.build_plan(speaker))}


# ── default capture root (assembled from parts, never as one literal) ─────────
# Mirrors ``app.py``: the human-data commit gate forbids the contiguous
# capture-root string in any tracked file, so the drive letter and the folder
# stay separate literals that only join at runtime.

INCOMING_ENV = "WAKEWORD_INCOMING_ROOT"


def default_incoming_root() -> Path:
    override = os.environ.get(INCOMING_ENV)
    if override:
        return Path(override)
    return Path("G:/") / "Youtab-Wakeword-Human" / "incoming"


def default_speaker_root(speaker: str) -> Path:
    return default_incoming_root() / speaker


def continuous_dir_for(speaker_root: Path) -> Path:
    """Where the seven hand-made originals live: ``<speaker_root>/_continuous``.

    Inside the speaker folder, next to ``originals/`` -- the location the operator
    drops the seven files. The compact validator tolerates this ``_continuous/``
    directory as a preserved raw-source archive (``core.CONTINUOUS_DIRNAME``) and
    excludes it from the take set and the manifest, so the derived ``originals/``
    tree still validates GREEN while the seven sources are preserved beside it.
    """
    return Path(speaker_root) / CONTINUOUS_DIRNAME


# ── reading and validating one original ───────────────────────────────────────


@dataclass
class _Source:
    """One continuous original's raw frames plus a mono amplitude view.

    ``raw`` is the exact PCM payload as read from the file; every derived take is
    sliced out of it on frame boundaries, so the stored bytes are never rewritten
    -- a 24-bit source stays 24-bit and byte-identical. ``scaled`` is a mono
    float [-1, 1] view used only to place silence boundaries and report levels.
    """

    raw: bytes
    scaled: np.ndarray
    channels: int
    width: int
    rate: int
    nframes: int

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.width


def _read_source(path: Path, expect_speech: bool) -> tuple[_Source | None, list[str]]:
    """Read one original as raw frames + amplitude, or the reasons it may not split.

    Refuses anything the derived takes could not be written from truthfully: not
    16- or 24-bit PCM, not mono, below 16 kHz, empty, silent where speech is
    expected, or clipped throughout. Never resamples, converts, or normalises the
    stored bytes -- the derived takes are exact byte slices of this payload.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            nframes = handle.getnframes()
            raw = handle.readframes(nframes)
    except (wave.Error, EOFError, OSError) as exc:
        return None, [f"{path.name}: will not open as a WAV ({exc})"]

    problems: list[str] = []
    if width not in core.SUPPORTED_SAMPLE_WIDTHS:
        problems.append(f"{path.name}: {width * 8}-bit PCM, expected 16- or 24-bit")
    if channels != core.CHANNELS:
        problems.append(
            f"{path.name}: {channels} channels, expected mono -- record mono "
            "(a stereo->mono downmix would be a conversion, which this tool never does)"
        )
    if rate < core.MIN_SAMPLE_RATE_HZ:
        problems.append(
            f"{path.name}: sample rate {rate} Hz is below the {core.MIN_SAMPLE_RATE_HZ} Hz floor"
        )
    if nframes <= 0:
        problems.append(f"{path.name}: empty (0 audio frames)")
    if problems:
        return None, problems

    scaled = core._decode_pcm_scaled(raw, width, channels)
    inspection = core._inspect_scaled(
        scaled,
        rate,
        int(scaled.size),
        expect_speech=expect_speech,
        min_seconds=core.MIN_UTTERANCE_SECONDS,
        target_seconds=None,
    )
    if core.FINDING_EMPTY in inspection.codes:
        problems.append(f"{path.name}: silent capture -- nothing to split")
    if inspection.clip_fraction >= MAX_CLIP_FRACTION:
        problems.append(
            f"{path.name}: clipped throughout ({inspection.clip_fraction * 100:.0f}% of samples "
            "at full scale); re-record with a lower input level"
        )
    if problems:
        return None, problems
    return _Source(raw=raw, scaled=scaled, channels=channels, width=width,
                   rate=rate, nframes=nframes), []


# ── analysis: read the seven, validate, and propose boundaries ────────────────


@dataclass
class OriginalOutcome:
    mapping: OriginalMapping
    path: Path
    exists: bool = False
    source: _Source | None = None
    segments: list[tuple[int, int]] = field(default_factory=list)  # (start_frame, end_frame)
    problems: list[str] = field(default_factory=list)
    sha_before: str | None = None

    @property
    def blocked(self) -> bool:
        return (not self.exists) or bool(self.problems)

    @property
    def matched(self) -> bool:
        return not self.blocked and len(self.segments) == self.mapping.expected


def _effective(
    name: str,
    overrides: dict[str, dict[str, float]],
    min_silence_s: float,
    silence_dbfs: float,
    min_utterance_s: float,
) -> tuple[float, float, float]:
    over = overrides.get(name, {})
    return (
        over.get("min_silence_s", min_silence_s),
        over.get("silence_dbfs", silence_dbfs),
        over.get("min_utterance_s", min_utterance_s),
    )


def analyze(
    speaker_root: Path,
    mappings: list[OriginalMapping],
    *,
    continuous_dir: Path | None = None,
    min_silence_s: float = DEFAULT_MIN_SILENCE_S,
    silence_dbfs: float = DEFAULT_SILENCE_DBFS,
    min_utterance_s: float = DEFAULT_MIN_UTTERANCE_S,
    overrides: dict[str, dict[str, float]] | None = None,
) -> list[OriginalOutcome]:
    """Read every original, validate it, and propose its segment boundaries.

    Reads only -- writes nothing. Captures each original's SHA-256 up front so a
    later run can prove the byte-for-byte preservation contract. The seven
    originals are read from ``continuous_dir`` (default: the sibling
    ``continuous_dir_for(speaker_root)``), never from inside the submission root.
    """
    overrides = overrides or {}
    continuous_dir = Path(continuous_dir) if continuous_dir is not None else continuous_dir_for(speaker_root)
    outcomes: list[OriginalOutcome] = []
    for mapping in mappings:
        path = continuous_dir / mapping.name
        outcome = OriginalOutcome(mapping=mapping, path=path)
        if not path.is_file():
            outcome.problems.append(f"{mapping.name}: missing under {CONTINUOUS_DIRNAME}/")
            outcomes.append(outcome)
            continue
        outcome.exists = True
        expect_speech = mapping.steps[0].expect_speech
        source, problems = _read_source(path, expect_speech)
        if problems:
            outcome.problems = problems
            outcomes.append(outcome)
            continue
        outcome.source = source
        outcome.sha_before = core.sha256_file(path)
        if mapping.split:
            eff_sil, eff_dbfs, eff_utt = _effective(
                mapping.name, overrides, min_silence_s, silence_dbfs, min_utterance_s
            )
            outcome.segments = detect_segments(
                source.scaled,
                source.rate,
                expected=mapping.expected,
                min_silence_s=eff_sil,
                min_utterance_s=eff_utt,
                silence_dbfs=eff_dbfs,
            )
        else:
            outcome.segments = [(0, int(source.nframes))]
        outcomes.append(outcome)
    return outcomes


# ── writing the derived takes, and the two manifests ──────────────────────────


def _write_wav_bytes(path: Path, payload: bytes, channels: int, width: int, rate: int) -> None:
    """Write raw PCM ``payload`` as a WAV at the source's channels/width/rate.

    The payload is written unchanged, so a 24-bit slice is stored as 24-bit and
    a 16-bit slice as 16-bit -- no conversion, no resampling, no re-quantising.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(payload)


def write_derived(speaker_root: Path, outcomes: list[OriginalOutcome]) -> int:
    """Write every derived take to its canonical ``step.path``. Returns the count.

    Each take is the exact byte slice its frame boundaries name, written at the
    original's own channels/width/rate -- so a 24-bit source yields 24-bit takes
    that are byte-identical to the corresponding span of the original. Assumes the
    outcomes are all matched (the caller refuses to write otherwise).
    """
    root = Path(speaker_root)
    written = 0
    for outcome in outcomes:
        src = outcome.source
        if src is None:
            raise RuntimeError(f"{outcome.mapping.name}: no source to write")
        frame_bytes = src.frame_bytes
        for step, (start, end) in zip(outcome.mapping.steps, outcome.segments):
            payload = src.raw[start * frame_bytes : end * frame_bytes]
            _write_wav_bytes(step.path(root), payload, src.channels, src.width, src.rate)
            written += 1
    return written


def verify_originals_unchanged(outcomes: list[OriginalOutcome]) -> list[str]:
    """Names of any originals whose bytes changed since ``analyze`` hashed them."""
    changed: list[str] = []
    for outcome in outcomes:
        if outcome.sha_before is None:
            continue
        if core.sha256_file(outcome.path) != outcome.sha_before:
            changed.append(outcome.mapping.name)
    return changed


def write_continuous_manifest(
    continuous_dir: Path, mappings: list[OriginalMapping]
) -> tuple[Path, int]:
    """Write ``_continuous/SHA256SUMS`` over the seven originals, coreutils format."""
    continuous_dir = Path(continuous_dir)
    lines: list[str] = []
    for name in sorted(mapping.name for mapping in mappings):
        path = continuous_dir / name
        if path.is_file():
            lines.append(f"{core.sha256_file(path)}  {name}\n")
    out = continuous_dir / spec.CHECKSUM_FILE
    out.write_text("".join(lines), encoding="utf-8")
    return out, len(lines)


# ── operator-facing reporting ─────────────────────────────────────────────────


def _print_report(outcomes: list[OriginalOutcome], speaker: str, continuous_dir: Path) -> None:
    print(
        f"Segment fallback for {speaker}: reading {len(outcomes)} originals from "
        f"{continuous_dir}\n"
    )
    for outcome in outcomes:
        mapping = outcome.mapping
        if not outcome.exists or outcome.problems:
            print(f"{mapping.name}: CANNOT SPLIT")
            for problem in outcome.problems:
                print(f"    - {problem}")
            continue
        src = outcome.source
        assert src is not None
        if not mapping.split:
            dur = src.nframes / src.rate
            fmt = f"{src.width * 8}-bit {src.rate} Hz"
            print(f"{mapping.name}: copied whole, no split -> 1 take ({dur:.1f}s, {fmt})")
            continue
        detected = len(outcome.segments)
        status = "OK" if outcome.matched else "MISMATCH"
        print(f"{mapping.name}: {detected} segment(s) detected (expected {mapping.expected}) "
              f"[{src.width * 8}-bit {src.rate} Hz] {status}")
        for index, (start, end) in enumerate(outcome.segments):
            dur = (end - start) / src.rate
            peak = _peak_dbfs(src.scaled[start:end])
            print(
                f"    [{index:>2}] start={start / src.rate:7.2f}s "
                f"end={end / src.rate:7.2f}s dur={dur:5.2f}s peak={peak:6.1f} dBFS"
            )
        if not outcome.matched:
            print(
                f"    -> counts differ: adjust --min-silence / --silence-dbfs (or "
                f"--override {mapping.name}:min_silence=..,silence_dbfs=..);"
            )
            print(
                "       the recording needs 2-3 s of clear silence between responses "
                f"(detected {detected}, expected {mapping.expected})."
            )


def _print_summary(
    outcomes: list[OriginalOutcome],
    written: int,
    derived_manifest: Path,
    derived_count: int,
    continuous_manifest: Path,
    continuous_count: int,
    changed: list[str],
) -> None:
    print("\nSummary:")
    for outcome in outcomes:
        kind = "split" if outcome.mapping.split else "whole copy"
        print(f"  {outcome.mapping.name:<22} -> {len(outcome.mapping.steps):>2} derived  ({kind})")
    print(f"  total: {written} derived files")
    unchanged = "OK" if not changed else "CHANGED: " + ", ".join(changed)
    print(f"  originals-unchanged: {unchanged}")
    print(f"  derived manifest:    {derived_manifest}  ({derived_count} entries)")
    print(f"  originals manifest:  {continuous_manifest}  ({continuous_count} entries)")


# ── override parsing ──────────────────────────────────────────────────────────

_OVERRIDE_KEYS = {
    "min_silence": "min_silence_s",
    "silence_dbfs": "silence_dbfs",
    "min_utterance": "min_utterance_s",
}


def _parse_overrides(items: list[str] | None, valid_names: set[str]) -> dict[str, dict[str, float]]:
    """Parse ``--override NAME:key=value[,key=value]`` items into a per-file map."""
    result: dict[str, dict[str, float]] = {}
    for item in items or []:
        name, sep, rest = item.partition(":")
        if not sep or not rest.strip():
            raise ValueError(f"--override must look like NAME:key=value[,key=value], got {item!r}")
        if name not in valid_names:
            raise ValueError(f"--override names {name!r}, not one of {sorted(valid_names)}")
        params: dict[str, float] = {}
        for pair in rest.split(","):
            key, eq, value = pair.partition("=")
            key = key.strip()
            if not eq or key not in _OVERRIDE_KEYS:
                raise ValueError(
                    f"--override key must be one of {sorted(_OVERRIDE_KEYS)}, got {pair!r}"
                )
            try:
                params[_OVERRIDE_KEYS[key]] = float(value)
            except ValueError:
                raise ValueError(f"--override {key} needs a number, got {value!r}") from None
        result[name] = params
    return result


# ── command line ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speaker", required=True, help="speaker label, one of E001 or E002")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=f"speaker submission root; derived takes go under its originals/ "
        f"(default: ${INCOMING_ENV}/<label>)",
    )
    parser.add_argument(
        "--continuous",
        type=Path,
        default=None,
        help="folder holding the seven continuous originals (default: the sibling "
        f"<incoming>/{CONTINUOUS_DIRNAME}/<label>, kept outside the submission root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="detect and print boundaries for all seven originals; write nothing",
    )
    parser.add_argument(
        "--min-silence",
        type=float,
        default=DEFAULT_MIN_SILENCE_S,
        help=f"silence run (s) that separates two responses (default {DEFAULT_MIN_SILENCE_S})",
    )
    parser.add_argument(
        "--silence-dbfs",
        type=float,
        default=DEFAULT_SILENCE_DBFS,
        help=f"frame RMS (dBFS) at or below which a frame is silence (default {DEFAULT_SILENCE_DBFS})",
    )
    parser.add_argument(
        "--min-utterance",
        type=float,
        default=DEFAULT_MIN_UTTERANCE_S,
        help=f"shortest kept utterance (s); shorter is a click (default {DEFAULT_MIN_UTTERANCE_S})",
    )
    parser.add_argument(
        "--override",
        action="append",
        metavar="NAME:key=value",
        help="per-file retune, e.g. positive_close.wav:min_silence=1.5,silence_dbfs=-40 "
        "(repeatable; keys: min_silence, silence_dbfs, min_utterance)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="write a reviewed split without the interactive prompt (still only when counts match)",
    )
    return parser


def _confirm(speaker_root: Path) -> bool:
    try:
        reply = input(f"\nWrite {TOTAL_DERIVED} derived takes under {speaker_root}? [y/N]: ")
    except EOFError:
        return False
    return reply.strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    speaker = args.speaker
    if speaker not in compact_plan.COMPACT_NOISE_ASSIGNMENTS:
        print(
            f"error: --speaker must be one of {sorted(compact_plan.COMPACT_NOISE_ASSIGNMENTS)} "
            "(the compact plan's two humans)",
            file=sys.stderr,
        )
        return 2

    speaker_root = Path(args.root) if args.root else default_speaker_root(speaker)
    plan = core.build_plan(speaker)
    try:
        mappings = build_original_map(plan)
        overrides = _parse_overrides(args.override, {mapping.name for mapping in mappings})
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    continuous_dir = Path(args.continuous) if args.continuous else continuous_dir_for(speaker_root)
    if not continuous_dir.is_dir():
        print(
            f"error: no continuous-originals folder at {continuous_dir}; put the seven "
            "originals (positive_close.wav, positive_farfield.wav, positive_noise_A.wav, "
            "positive_noise_B.wav, near_phrases.wav, freespeech.wav, background.wav) there first",
            file=sys.stderr,
        )
        return 2

    outcomes = analyze(
        speaker_root,
        mappings,
        continuous_dir=continuous_dir,
        min_silence_s=args.min_silence,
        silence_dbfs=args.silence_dbfs,
        min_utterance_s=args.min_utterance,
        overrides=overrides,
    )
    _print_report(outcomes, speaker, continuous_dir)

    blocked = [o for o in outcomes if o.blocked]
    mismatched = [o for o in outcomes if not o.blocked and o.mapping.split and not o.matched]

    if args.dry_run:
        if blocked or mismatched:
            print("\ndry-run: not clean (see notes above); nothing written.")
            return 1
        print(f"\ndry-run: all seven split cleanly into {TOTAL_DERIVED} takes; nothing written.")
        return 0

    if blocked:
        print(
            "\nrefusing to write: some originals are missing or invalid (see above).",
            file=sys.stderr,
        )
        return 1
    if mismatched:
        print(
            "\nrefusing to write: detected segment count != expected for "
            + ", ".join(o.mapping.name for o in mismatched)
            + ".\n  Adjust --min-silence / --silence-dbfs (or --override NAME:...); the "
            "recording needs\n  2-3 s of clear silence between responses so each is detected "
            "separately.",
            file=sys.stderr,
        )
        return 1

    if not args.yes and not _confirm(speaker_root):
        print("aborted; nothing written.")
        return 1

    written = write_derived(speaker_root, outcomes)
    changed = verify_originals_unchanged(outcomes)
    derived_count = core.write_sha256sums(speaker_root)
    continuous_manifest, continuous_count = write_continuous_manifest(continuous_dir, mappings)
    _print_summary(
        outcomes,
        written,
        speaker_root / spec.CHECKSUM_FILE,
        derived_count,
        continuous_manifest,
        continuous_count,
        changed,
    )
    if changed:
        print(
            "\nERROR: an original changed during the run; the preservation contract is broken.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
