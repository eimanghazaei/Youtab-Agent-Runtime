#!/usr/bin/env python3
"""Ingest one human speaker's recordings, or refuse the whole submission.

Why
---
Round 8 is trained, selected and qualified on real recordings of real people,
and there will be five of them. Every one of those recordings is given once, by
a person who cannot be asked to do it again, and each one arrives as a folder on
a removable drive with no schema, no digest anybody has checked, and a
speaker-facing naming convention that a recorder app is free to ignore.

Between that folder and ``build_human_dataset.py`` there is exactly one step
where a mistake is still cheap: this one. So it is written to refuse rather than
to cope. The failures it exists to prevent are all quiet ones:

**A take nobody can label.** ``SPEAKER_RECORDING_PACKAGE.md`` promises the
speaker that "our loader refuses to guess a label". A file called
``Voice Memo 3.wav`` in ``positive_normal/`` is not a positive with an unknown
take number; it is a recording whose content nobody can state, and a dataset
that guessed would be teaching the model something nobody decided. Every file
under the recording tree is classified, and an unclassified one fails the
import instead of being skipped.

**A speaker in the wrong split.** The role of each label — train, validate,
sealed — was fixed before any of this audio existed, in ``round8_config.json``
and in ``speaker_recording_spec.SPEAKER_ASSIGNMENTS``. It is looked up here from
those two records and checked against each other. It is never inferred from a
path, never read out of the submission, and there is deliberately no
command-line flag that can set it: promoting a sealed voice into training spends
the only measurement nobody has tuned against, and re-recording that speaker
cannot restore it, because they would no longer be unseen.

**The same recording counted twice.** A copied file, a re-submitted folder, or
one take shared between two speakers' folders inflates a count and, across
roles, trains on the audio that is supposed to measure. A SHA-256 does not move
with a file, so the digests of everything already imported — including the two
manifests frozen before this round — are what this checks against.

**"Preserved byte-for-byte" as a claim rather than a fact.** Every original is
hashed on the drive before anything happens, verified again as it is copied,
and re-hashed on both sides *after* the report and the manifest are written. If
any digest moved, nothing is published.

**A record that reads as complete and is not.** An interrupted run leaves a
journal, some verified copies, and no manifest — never a truncated one. The
manifest is produced by ``freeze_manifest.py`` into a staging directory and
moved into place with a single atomic rename, last of everything.

What it does not do
-------------------
It does not decide what a take is worth. Nothing is trimmed, converted,
re-levelled, mixed or relabelled, and no clip is cut: segmentation into clips is
the derivation's job, downstream of a frozen original. The only judgement made
here is whether a file contains recorded sound at all, under the predeclared
rules in ``PREDECLARED_EXCLUSIONS`` — and an excluded take keeps its label, its
bytes and its place in the manifest, exactly as E001's and E002's exclusions did.
Quiet, distant, noisy, fast and clipped takes are reported and kept: those are
the conditions the detector is failing, and a pipeline that dropped them would
be measuring a room nobody records in.

Format facts and level facts are read by different code on purpose. Container,
codec, sample rate, channels and declared duration come from parsing the
container; peak, RMS, DC offset, clipping and the noise floor come from
decoding. A decoder asked for "the sample rate" will happily answer with
whatever it resampled to, and a format report contaminated by a decoder default
cannot be used to show that a recording was never transcoded.

Usage::

    import_speaker.py --speaker E003 --submission DIR --into DIR --dry-run
    import_speaker.py --speaker E003 --submission DIR --into DIR

``--dry-run`` runs every check and writes nothing; it is what the Owner runs
against the drive before anything is copied. ``--into`` is the external data
root: this refuses to read a submission from inside the repository and refuses
to write anything into it, because raw audio, a consent scan and a filled
metadata form are the three things that must never reach git or CI.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import freeze_manifest  # noqa: E402
import round8_config  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402
import validate_speaker_submission as submission_validator  # noqa: E402

#: Bumped when the report or journal body changes shape. A record carrying a
#: version this tool does not know is refused, not guessed at — same rule
#: ``freeze_manifest`` applies to a manifest.
SCHEMA_VERSION = 1

#: How this tool names itself in everything it writes.
TOOL = "scripts/wakeword/import_speaker.py"

#: What lands in the external data root, per speaker.
SPEAKER_DIR_PREFIX = "speaker_"
ORIGINALS_DIR = spec.ORIGINALS_DIR
REPORT_FILENAME = "INGESTION_REPORT.json"
MANIFEST_FILENAME = "ORIGINALS_MANIFEST.json"
JOURNAL_FILENAME = "INGESTION_JOURNAL.json"

#: In-progress copies live here, *outside* the directory that gets frozen.
#: ``freeze_manifest.scan`` walks the root it freezes and skips only dot-named
#: files, so a half-written copy inside that root would either be frozen as if
#: it were a recording or be skipped silently. Neither is acceptable, so nothing
#: unfinished is ever inside it.
STAGING_DIR = ".import-staging"

#: Held for the publish window only, in the data root, so two imports cannot
#: both read the root before either has published into it. Deliberately not held
#: across the copy: that is the long phase, an interruption there is already
#: recoverable from the journal, and a lock spanning it would turn a killed run
#: into a root nobody can import into without deleting a file by hand.
LOCK_FILENAME = ".import-lock"

#: The rate the production front end runs at (``build_dataset.SAMPLE_RATE``, and
#: ``tools/wake_word.py``'s stream rate). Nothing is resampled here; this is the
#: floor a submission has to clear, because a take captured below it cannot be
#: brought up to the feature extractor's rate without inventing content that
#: nobody said. Pinned against the pipeline's own constant by
#: ``tests/tools/test_wakeword_speaker_import.py``.
PRODUCTION_SAMPLE_RATE_HZ = 16000

#: Frame geometry and percentile for the noise-floor estimate, identical to the
#: ``segmenter`` block recorded in the two frozen human manifests
#: (``frame_ms`` 20, ``hop_ms`` 10, ``noise_pctl`` 10). Same numbers so a
#: ``noise_floor_dbfs`` in an ingestion report is directly comparable with the
#: one the earlier derivation recorded for the same kind of file.
NOISE_FRAME_MS = 20
NOISE_HOP_MS = 10
NOISE_PERCENTILE = 10

#: dBFS reported for a frame or a file with no signal in it at all. A real
#: ``-inf`` is not representable in JSON, and a sentinel that reads as a level
#: would be worse than one that obviously is not.
SILENCE_DBFS = -120.0

#: A sample is "at full scale" within this distance of 1.0 after normalisation,
#: which covers int16's ``32767/32767`` and ``-32768/32767`` alike.
FULL_SCALE_TOLERANCE = 1e-4

#: Consecutive full-scale samples that make a flat top rather than a peak.
CLIPPING_RUN_SAMPLES = 3

#: Thresholds for the findings below. Findings are reported and never exclude
#: anything; they exist so the derivation and the Owner can see a condition
#: rather than discover it in a training curve.
QUIET_PEAK_DBFS = -40.0
HIGH_NOISE_FLOOR_DBFS = -40.0
DC_OFFSET_LIMIT = 0.01
HEAVY_CLIPPING_RUNS = 10
MIN_TAKE_SECONDS = 0.25
MAX_TAKE_SECONDS = 30.0

#: The largest single take this will read into memory. Decoding expands a take
#: to roughly 15x its size on disk -- float32 samples, plus the overlapping
#: window view the noise-floor estimate builds at 16 bytes per sample -- and
#: nothing in the container parsers bounds the *real* length of a file, only a
#: header that lies about it. A recorder left running produces a multi-gigabyte
#: WAV, and reading one exhausts the machine instead of reporting a finding
#: about it. Far above any real take: MAX_TAKE_SECONDS of 48 kHz 24-bit stereo
#: is under 9 MB, and the longest continuous section either earlier speaker
#: recorded is minutes, not hours.
MAX_TAKE_BYTES = 512 * 1024 * 1024

#: How far a decoded duration may sit from the container's declared duration
#: before it is worth reporting. Encoder priming and trailing padding are
#: real and small; anything past this is worth a human look.
DURATION_TOLERANCE_S = 0.10
DURATION_TOLERANCE_FRACTION = 0.02

#: Below this fraction of the declared duration the payload is not "slightly
#: off", it is missing, which is what a truncated transfer looks like.
DURATION_TRUNCATION_FRACTION = 0.9

#: Label values, as the frozen manifests and ``build_human_dataset`` record
#: them. The word forms live in ``speaker_recording_spec``; this is the
#: translation and the only place it happens.
LABEL_VALUES: dict[str, int] = {spec.POSITIVE: 1, spec.NEGATIVE: 0}

#: ``round8_config.json``'s split names -> the role vocabulary in
#: ``speaker_recording_spec``, which is identical to ``freeze_manifest.USAGES``.
CONFIG_ROLE_TO_ROLE: dict[str, str] = {
    "train": spec.ROLE_TRAINING,
    "validation": spec.ROLE_VALIDATION,
    "sealed": spec.ROLE_SEALED,
}

#: Role -> the ``split`` string written into this round's frozen manifest. The
#: strings are ``build_human_dataset.SPEAKER_SPLITS``'s, so a manifest this tool
#: freezes declares the split that stage will accept for that speaker; the
#: agreement is asserted by this module's tests rather than by importing that
#: stage, which would pull the whole feature pipeline in for one dictionary.
ROLE_SPLITS: dict[str, str] = {
    spec.ROLE_TRAINING: "train",
    spec.ROLE_VALIDATION: "validation",
    spec.ROLE_SEALED: "qualification_sealed",
}

#: The two speakers recorded before this round, whose split names predate it.
#: E002 is the round-6/7 sealed *evaluation* speaker rather than a Round 8
#: qualification speaker, and its frozen manifest says so; renaming its split
#: here would make the record of a measurement that has already been taken
#: disagree with itself.
PRIOR_SPLITS: dict[str, str] = {"E001": "train", "E002": "eval_sealed"}

#: Every split name that may appear in a manifest already on disk, and the role
#: it means. Used when reading what is already imported — including the two
#: frozen manifests — so a digest found there can be attributed to a role.
SPLIT_ROLES: dict[str, str] = {
    "train": spec.ROLE_TRAINING,
    "validation": spec.ROLE_VALIDATION,
    "eval_sealed": spec.ROLE_SEALED,
    "qualification_sealed": spec.ROLE_SEALED,
}

#: A word that must appear in a manifest's free-text ``usage`` for it to
#: corroborate the role its ``split`` implies. The two frozen manifests spell
#: usage as a sentence ("TRAINING ONLY. ...", "SEALED EVALUATION ONLY. ...")
#: rather than as one of ``freeze_manifest.USAGES``; a manifest whose prose
#: contradicts its split is refused rather than half-believed.
ROLE_PROSE_MARKERS: dict[str, str] = {
    spec.ROLE_TRAINING: "TRAINING",
    spec.ROLE_VALIDATION: "VALIDATION",
    spec.ROLE_SEALED: "SEALED",
}


class Refused(SystemExit):
    """A fail-closed refusal.

    ``SystemExit`` so an operator gets the message and a non-zero status rather
    than a traceback, exactly as ``build_human_dataset.Refused`` does; a named
    subclass so a test can assert a refusal rather than an exit.
    """


class FormatError(ValueError):
    """The container cannot be parsed, or does not describe playable audio."""


class DecodeError(RuntimeError):
    """The payload cannot be turned into samples."""


class DecoderUnavailable(DecodeError):
    """Nothing on this machine can decode this payload.

    Kept apart from ``DecodeError`` because the two mean opposite things about
    the recording. A payload this tool cannot decode is a finding about the
    file; a decoder that is not installed is a finding about the machine, and
    excluding a take because the operator's environment is incomplete would
    hold a perfectly good recording out of a dataset and record a reason that
    was never true of it.
    """


# ── who a speaker is allowed to be ───────────────────────────────────────────


@dataclass(frozen=True)
class Assignment:
    """One speaker label and the one thing their recordings may be used for."""

    label: str
    role: str
    split: str
    why: str


def registry() -> dict[str, Assignment]:
    """Every label with a predeclared role, from two independent records.

    ``round8_config.SPLITS`` is the predeclaration — written before the audio
    existed, pinned by ``tests/tools/test_wakeword_round8_predeclaration.py``,
    and the only record that covers all seven speakers.
    ``speaker_recording_spec.SPEAKER_ASSIGNMENTS`` is what the recording package
    was built from and covers this round's five. Where both speak they must
    agree: a disagreement means one of the two documents was edited alone, and
    the safe reading of "this speaker might be sealed" is not to import them.
    """
    table: dict[str, Assignment] = {}
    for config_role, labels in round8_config.SPLITS.items():
        role = CONFIG_ROLE_TO_ROLE[config_role]
        for label in labels:
            table[label] = Assignment(
                label,
                role,
                PRIOR_SPLITS.get(label, ROLE_SPLITS[role]),
                f"round8_config.SPLITS[{config_role!r}]",
            )

    for assignment in spec.SPEAKER_ASSIGNMENTS:
        known = table.get(assignment.label)
        if known is None:
            raise Refused(
                f"{assignment.label} is assigned {assignment.role!r} in "
                "speaker_recording_spec.SPEAKER_ASSIGNMENTS but does not appear in "
                "round8_config.SPLITS. Two records of who is sealed, and one of "
                "them does not know about this speaker: refusing to pick either."
            )
        if known.role != assignment.role:
            raise Refused(
                f"{assignment.label} is {known.role!r} in round8_config.SPLITS and "
                f"{assignment.role!r} in speaker_recording_spec.SPEAKER_ASSIGNMENTS. "
                "Refusing on the disagreement itself: whichever is wrong, importing "
                "would put a voice somewhere nobody approved."
            )
        table[assignment.label] = Assignment(
            assignment.label, known.role, known.split, assignment.why
        )
    return table


def assignment(label: str) -> Assignment:
    """The predeclared role of ``label``, or a refusal naming the registry."""
    table = registry()
    try:
        return table[label]
    except KeyError:
        raise Refused(
            f"speaker {label!r} has no predeclared role ({sorted(table)}). A role is "
            "read from round8_config.SPLITS and speaker_recording_spec."
            "SPEAKER_ASSIGNMENTS, never inferred from a folder name and never "
            "supplied on the command line — a speaker nobody has decided about is "
            "one whose audio must not be ingested at all."
        ) from None


def assert_usable_for(label: str, purpose: str) -> None:
    """Refuse a speaker for a purpose their role does not permit.

    Delegates to ``freeze_manifest.assert_usable_for`` rather than restating the
    rule, so the sealed refusal an operator meets here is the same object, with
    the same message, that the dataset builder raises.
    """
    freeze_manifest.assert_usable_for(
        {"dataset": label, "usage": assignment(label).role}, purpose
    )


# ── what a recording is allowed to be called ─────────────────────────────────

#: The layout every Round 8 speaker submits, as ``SPEAKER_RECORDING_PACKAGE.md``
#: prescribes it.
LAYOUT_PACKAGE = "package"

#: The layout the two speakers recorded before the package existed used: one
#: file — or one directory of separately recorded files — per numbered session
#: section. Accepted only for those two labels, so it cannot become a way around
#: the package's section requirements.
LAYOUT_PRIOR = "prior-session"

#: Labels whose recordings predate ``SPEAKER_RECORDING_PACKAGE.md``.
PRIOR_LAYOUT_LABELS: tuple[str, ...] = ("E001", "E002")


@dataclass(frozen=True)
class Section:
    """One recognised group of originals inside the recording tree.

    ``label`` is ``None`` where the phrase in the filename decides it, which is
    the near-phrase battery: ``speaker_recording_spec.NEAR_PHRASE_ITEMS`` says
    what the detector must do with each phrase, and this module does not get a
    second opinion about it.
    """

    directory: str
    stem: re.Pattern[str] | None
    category: str
    label: int | None
    min_files: int
    required: bool
    phrase_keyed: bool
    grammar: str


@dataclass(frozen=True)
class Layout:
    """A recognised submission shape."""

    name: str
    sections: tuple[Section, ...]
    #: A directory in the tree that holds separately recorded members rather
    #: than one file. Never concatenated: each member is its own original.
    group_directories: bool
    requires_checksums: bool

    def section(self, directory: str) -> Section | None:
        for candidate in self.sections:
            if candidate.directory == directory:
                return candidate
        return None


def _take_pattern() -> str:
    return rf"[0-9]{{{spec.TAKE_DIGITS}}}"


def package_layout() -> Layout:
    """The recording package's tree, read off the spec rather than restated."""
    take = _take_pattern()
    sections: list[Section] = []

    for section in spec.POSITIVE_SECTIONS:
        stem = re.compile(
            rf"^{re.escape(spec.WAKE_PHRASE_SLUG)}_{re.escape(section.condition)}_{take}$"
        )
        sections.append(
            Section(
                section.directory,
                stem,
                section.directory,
                LABEL_VALUES[spec.POSITIVE],
                section.takes,
                True,
                False,
                f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_NNN",
            )
        )

    for source in sorted(spec.NOISE_SOURCE_VOCAB):
        section = spec.noise_section(source)
        stem = re.compile(
            rf"^{re.escape(spec.WAKE_PHRASE_SLUG)}_{re.escape(section.condition)}_{take}$"
        )
        sections.append(
            Section(
                section.directory,
                stem,
                section.directory,
                LABEL_VALUES[spec.POSITIVE],
                section.takes,
                # Individually optional: the package asks for at least
                # MIN_NOISE_SOURCES of the four, not for all of them.
                False,
                False,
                f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_NNN",
            )
        )

    sections.append(
        Section(
            "near_phrase",
            re.compile(rf"^([a-z0-9-]+)_{take}$"),
            "near_phrase",
            None,
            0,  # per-phrase counts, checked against each item's own take count
            True,
            True,
            "<phrase-slug>_NNN",
        )
    )

    freeform_categories = {"negative_freespeech": "free_speech", "background_only": "background_only"}
    for section in spec.FREEFORM_SECTIONS:
        sections.append(
            Section(
                section.directory,
                re.compile(rf"^{re.escape(section.prefix)}_{take}$"),
                freeform_categories[section.directory],
                LABEL_VALUES[spec.NEGATIVE],
                section.min_files,
                True,
                False,
                f"{section.prefix}_NNN",
            )
        )

    return Layout(LAYOUT_PACKAGE, tuple(sections), False, True)


#: The round-6/7 session sections, as the two frozen manifests' ``labels``
#: blocks name them. ``03_far_field`` and ``03_far_angle`` are the same section
#: under the two names those sessions used.
PRIOR_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("01_close_normal", "positive_close", spec.POSITIVE),
    ("02_speed_volume", "positive_speed_volume", spec.POSITIVE),
    ("03_far_field", "positive_far_field", spec.POSITIVE),
    ("03_far_angle", "positive_far_field", spec.POSITIVE),
    ("04_real_noise", "positive_noise", spec.POSITIVE),
    ("05_near_phrases", "near_phrase", spec.NEGATIVE),
    ("06_free_speech", "free_speech", spec.NEGATIVE),
)

#: Categories a prior-layout submission has to contain at least one usable
#: recording of. Stated as categories rather than directories because the
#: far-field section has two spellings.
PRIOR_REQUIRED_CATEGORIES: tuple[str, ...] = (
    "positive_close",
    "positive_speed_volume",
    "positive_far_field",
    "positive_noise",
    "near_phrase",
    "free_speech",
)


def prior_layout() -> Layout:
    """The session tree E001 and E002 were recorded into.

    Kept so the two frozen manifests can be shown to be ingestible by the same
    code path that ingests this round — a compatibility claim that is checkable
    rather than asserted. Their near-phrase section is the reason
    ``group_directories`` exists: E002 recorded twelve near phrases as twelve
    separate files inside ``05_near_phrases/``, and concatenating them to fit a
    one-file-per-section shape would destroy the per-phrase attribution that
    makes them evidence.
    """
    sections = tuple(
        Section(
            directory,
            re.compile(rf"^{re.escape(directory)}$"),
            category,
            LABEL_VALUES[word],
            1,
            False,  # completeness is checked per category, not per directory
            category == "near_phrase",
            f"{directory}.<ext>, or {directory}/<phrase>.<ext>",
        )
        for directory, category, word in PRIOR_SECTIONS
    )
    return Layout(LAYOUT_PRIOR, sections, True, False)


def layouts_for(label: str) -> tuple[Layout, ...]:
    """The layouts ``label`` may submit in.

    A Round 8 speaker may only use the package layout. Allowing the older one
    would let an operator satisfy an import with four files while the near-phrase
    battery, the two far-field conditions and the noise sections — the whole
    reason this round exists — went unrecorded.
    """
    if label in PRIOR_LAYOUT_LABELS:
        return (prior_layout(), package_layout())
    return (package_layout(),)


# ── format facts, read from the container and from nothing else ───────────────


@dataclass(frozen=True)
class FormatFacts:
    """What the container itself says about the audio in it."""

    container: str
    codec: str
    sample_rate_hz: int
    channels: int
    bits_per_sample: int | None
    duration_s: float | None
    read_by: str
    notes: tuple[str, ...] = ()

    def as_json(self) -> dict:
        return {
            "container": self.container,
            "codec": self.codec,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "bits_per_sample": self.bits_per_sample,
            "duration_s": None if self.duration_s is None else round(self.duration_s, 3),
            "read_by": self.read_by,
        }


#: Container family -> the extensions a recorder writes it under. Dispatch is by
#: magic bytes, not by extension; this table is what makes a *disagreement*
#: between the two visible, which is what a renamed file looks like.
CONTAINER_EXTENSIONS: dict[str, frozenset[str]] = {
    "riff-wave": frozenset({".wav", ".wave"}),
    "iso-bmff": frozenset({".m4a", ".mp4", ".m4b", ".3gp", ".3gpp", ".aac", ".mov"}),
    "caf": frozenset({".caf"}),
    "flac": frozenset({".flac"}),
    "aiff": frozenset({".aiff", ".aif", ".aifc"}),
}

#: Extensions ``speaker_recording_spec`` accepts that no parser here reads.
#: Named rather than lumped into "unknown" so the refusal can say what to do.
UNPARSED_EXTENSIONS: frozenset[str] = frozenset(
    spec.AUDIO_EXTENSIONS
    - {ext for exts in CONTAINER_EXTENSIONS.values() for ext in exts}
)

_WAVE_FORMAT_PCM = 0x0001
_WAVE_FORMAT_FLOAT = 0x0003
_WAVE_FORMAT_ALAW = 0x0006
_WAVE_FORMAT_MULAW = 0x0007
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE

_WAVE_FORMAT_NAMES: dict[int, str] = {
    _WAVE_FORMAT_ALAW: "a_law",
    _WAVE_FORMAT_MULAW: "mu_law",
    0x0002: "ms_adpcm",
    0x0011: "ima_adpcm",
    0x0031: "gsm_ms",
    0x0055: "mp3",
    0x2000: "ac3",
}


@dataclass(frozen=True)
class _Riff:
    """A parsed RIFF/WAVE header, plus where the payload is."""

    tag: int
    channels: int
    sample_rate_hz: int
    bits_per_sample: int
    block_align: int
    data_offset: int
    data_bytes: int
    notes: tuple[str, ...]


def _read_riff(path: Path) -> _Riff:
    size = path.stat().st_size
    notes: list[str] = []
    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise FormatError("not a RIFF/WAVE container")
        declared = struct.unpack("<I", header[4:8])[0] + 8
        if declared != size:
            notes.append(f"the RIFF header claims {declared} bytes and the file is {size}")

        fmt: bytes | None = None
        data: tuple[int, int] | None = None
        position = 12
        while position + 8 <= size:
            handle.seek(position)
            head = handle.read(8)
            if len(head) < 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", head)
            body = position + 8
            if chunk_id == b"fmt ":
                fmt = handle.read(min(chunk_size, 40))
            elif chunk_id == b"data":
                data = (body, chunk_size)
            position = body + chunk_size + (chunk_size & 1)

    if fmt is None or len(fmt) < 16:
        raise FormatError("the RIFF container has no usable 'fmt ' chunk")
    if data is None:
        raise FormatError("the RIFF container has no 'data' chunk")

    tag, channels, rate, _, block_align, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == _WAVE_FORMAT_EXTENSIBLE:
        if len(fmt) < 40:
            raise FormatError("WAVE_FORMAT_EXTENSIBLE with no sub-format GUID")
        tag = struct.unpack("<H", fmt[24:26])[0]

    offset, declared_bytes = data
    available = max(size - offset, 0)
    if declared_bytes > available:
        raise FormatError(
            f"the 'data' chunk claims {declared_bytes} bytes and only {available} "
            "are present: the file is truncated"
        )
    return _Riff(tag, channels, rate, bits, block_align, offset, declared_bytes, tuple(notes))


def _riff_facts(path: Path) -> FormatFacts:
    riff = _read_riff(path)
    if riff.channels < 1:
        raise FormatError("the 'fmt ' chunk declares no channels")
    if riff.tag == _WAVE_FORMAT_PCM:
        codec = "pcm_u8" if riff.bits_per_sample == 8 else f"pcm_s{riff.bits_per_sample}le"
    elif riff.tag == _WAVE_FORMAT_FLOAT:
        codec = f"pcm_f{riff.bits_per_sample}le"
    else:
        codec = _WAVE_FORMAT_NAMES.get(riff.tag, f"wave_format_0x{riff.tag:04x}")

    duration: float | None = None
    if riff.tag in (_WAVE_FORMAT_PCM, _WAVE_FORMAT_FLOAT) and riff.bits_per_sample:
        frame = riff.channels * (riff.bits_per_sample // 8)
        if frame and riff.sample_rate_hz:
            duration = riff.data_bytes / frame / riff.sample_rate_hz
    return FormatFacts(
        "riff-wave",
        codec,
        riff.sample_rate_hz,
        riff.channels,
        riff.bits_per_sample or None,
        duration,
        "container-parser:riff",
        riff.notes,
    )


_BMFF_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl"})


def _bmff_boxes(handle, start: int, end: int):
    """Yield ``(kind, body_offset, box_end)`` for the boxes between two offsets."""
    position = start
    while position + 8 <= end:
        handle.seek(position)
        head = handle.read(8)
        if len(head) < 8:
            return
        box_size, kind = struct.unpack(">I4s", head)
        body = position + 8
        if box_size == 1:
            extra = handle.read(8)
            if len(extra) < 8:
                return
            box_size = struct.unpack(">Q", extra)[0]
            body = position + 16
        elif box_size == 0:
            box_size = end - position
        if box_size < 8 or position + box_size > end:
            raise FormatError(f"box {kind!r} runs past the end of the file")
        yield kind, body, position + box_size
        position += box_size


def _bmff_find(handle, start: int, end: int, wanted: bytes) -> list[tuple[int, int]]:
    """Every ``wanted`` box under ``start``, descending only into containers."""
    found: list[tuple[int, int]] = []
    for kind, body, box_end in _bmff_boxes(handle, start, end):
        if kind == wanted:
            found.append((body, box_end))
        elif kind in _BMFF_CONTAINERS:
            found.extend(_bmff_find(handle, body, box_end, wanted))
    return found


def _bmff_facts(path: Path) -> FormatFacts:
    size = path.stat().st_size
    notes: list[str] = []
    with path.open("rb") as handle:
        handle.seek(4)
        brand = handle.read(8)[4:8].decode("ascii", "replace").strip()
        traks = _bmff_find(handle, 0, size, b"trak")
        if not traks:
            raise FormatError("the ISO-BMFF container has no track")

        for start, end in traks:
            handlers = _bmff_find(handle, start, end, b"hdlr")
            if not handlers:
                continue
            body, _ = handlers[0]
            handle.seek(body + 8)
            if handle.read(4) != b"soun":
                continue

            mdhd = _bmff_find(handle, start, end, b"mdhd")
            stsd = _bmff_find(handle, start, end, b"stsd")
            if not mdhd or not stsd:
                raise FormatError("the audio track has no 'mdhd' or no 'stsd' box")

            handle.seek(mdhd[0][0])
            version = handle.read(4)[0]
            if version == 1:
                _, _, timescale, duration = struct.unpack(">QQIQ", handle.read(28))
            else:
                _, _, timescale, duration = struct.unpack(">IIII", handle.read(16))

            handle.seek(stsd[0][0] + 8)
            entry = handle.read(36)
            if len(entry) < 36:
                raise FormatError("the 'stsd' sample entry is truncated")
            codec = entry[4:8].decode("ascii", "replace")
            entry_version = struct.unpack(">H", entry[16:18])[0]
            if entry_version > 1:
                raise FormatError(
                    f"audio sample entry version {entry_version} is not parsed here; "
                    "the format facts would have to come from a decoder"
                )
            channels, bits = struct.unpack(">HH", entry[24:28])
            stsd_rate = struct.unpack(">I", entry[32:36])[0] >> 16

            rate = timescale or stsd_rate
            if stsd_rate and timescale and stsd_rate != timescale:
                notes.append(
                    f"the sample entry declares {stsd_rate} Hz and the media header "
                    f"{timescale} Hz; the media header is used"
                )
            seconds = duration / timescale if timescale else None
            return FormatFacts(
                "iso-bmff",
                codec,
                rate,
                channels,
                bits or None,
                seconds,
                f"container-parser:iso-bmff({brand})",
                tuple(notes),
            )
    raise FormatError("the ISO-BMFF container has no audio track")


def _flac_facts(path: Path) -> FormatFacts:
    with path.open("rb") as handle:
        if handle.read(4) != b"fLaC":
            raise FormatError("not a FLAC stream")
        header = handle.read(4)
        if len(header) < 4:
            raise FormatError("the FLAC metadata block header is truncated")
        if header[0] & 0x7F != 0:
            raise FormatError("the first FLAC metadata block is not STREAMINFO")
        info = handle.read(34)
    if len(info) < 34:
        raise FormatError("the FLAC STREAMINFO block is truncated")
    packed = int.from_bytes(info[10:18], "big")
    rate = packed >> 44
    channels = ((packed >> 41) & 0x7) + 1
    bits = ((packed >> 36) & 0x1F) + 1
    total = packed & 0xFFFFFFFFF
    if not rate:
        raise FormatError("the FLAC STREAMINFO block declares a 0 Hz sample rate")
    return FormatFacts(
        "flac",
        "flac",
        rate,
        channels,
        bits,
        (total / rate) if total else None,
        "container-parser:flac",
    )


def _caf_facts(path: Path) -> FormatFacts:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if handle.read(4) != b"caff":
            raise FormatError("not a CAF container")
        handle.seek(8)
        desc: tuple | None = None
        frames: int | None = None
        data_bytes = 0
        position = 8
        while position + 12 <= size:
            handle.seek(position)
            head = handle.read(12)
            if len(head) < 12:
                break
            chunk_type, chunk_size = struct.unpack(">4sq", head)
            body = position + 12
            if chunk_size < 0:
                chunk_size = size - body
            if chunk_type == b"desc":
                desc = struct.unpack(">d4sIIIII", handle.read(32))
            elif chunk_type == b"pakt":
                packet = handle.read(24)
                if len(packet) >= 16:
                    frames = struct.unpack(">qq", packet[:16])[1]
            elif chunk_type == b"data":
                data_bytes = max(chunk_size - 4, 0)
            position = body + chunk_size

    if desc is None:
        raise FormatError("the CAF container has no 'desc' chunk")
    rate, format_id, _, bytes_per_packet, frames_per_packet, channels, bits = desc
    if not rate or channels < 1:
        raise FormatError("the CAF 'desc' chunk declares no rate or no channel")
    seconds: float | None = None
    if frames is not None:
        seconds = frames / rate
    elif bytes_per_packet and frames_per_packet:
        seconds = data_bytes / bytes_per_packet * frames_per_packet / rate
    return FormatFacts(
        "caf",
        format_id.decode("ascii", "replace").strip(),
        int(round(rate)),
        channels,
        bits or None,
        seconds,
        "container-parser:caf",
    )


def _extended80(raw: bytes) -> float:
    """The 80-bit float AIFF stores a sample rate in."""
    exponent = struct.unpack(">H", raw[0:2])[0]
    mantissa = struct.unpack(">Q", raw[2:10])[0]
    sign = -1.0 if exponent & 0x8000 else 1.0
    exponent &= 0x7FFF
    if exponent == 0 and mantissa == 0:
        return 0.0
    return sign * mantissa * 2.0 ** (exponent - 16383 - 63)


@dataclass(frozen=True)
class _Aiff:
    channels: int
    frames: int
    bits_per_sample: int
    sample_rate_hz: int
    codec: str
    data_offset: int
    data_bytes: int


def _read_aiff(path: Path) -> _Aiff:
    size = path.stat().st_size
    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) < 12 or header[0:4] != b"FORM" or header[8:12] not in (b"AIFF", b"AIFC"):
            raise FormatError("not an AIFF/AIFC container")
        comm: bytes | None = None
        ssnd: tuple[int, int] | None = None
        position = 12
        while position + 8 <= size:
            handle.seek(position)
            head = handle.read(8)
            if len(head) < 8:
                break
            chunk_id, chunk_size = struct.unpack(">4sI", head)
            body = position + 8
            if chunk_id == b"COMM":
                comm = handle.read(min(chunk_size, 40))
            elif chunk_id == b"SSND":
                offset = struct.unpack(">I", handle.read(8)[:4])[0]
                ssnd = (body + 8 + offset, max(chunk_size - 8 - offset, 0))
            position = body + chunk_size + (chunk_size & 1)

    if comm is None or len(comm) < 18:
        raise FormatError("the AIFF container has no usable 'COMM' chunk")
    if ssnd is None:
        raise FormatError("the AIFF container has no 'SSND' chunk")
    channels, frames, bits = struct.unpack(">HIH", comm[:8])
    rate = _extended80(comm[8:18])
    codec = "pcm_be"
    if len(comm) >= 22:
        codec = comm[18:22].decode("ascii", "replace").strip()
    offset, declared = ssnd
    available = max(size - offset, 0)
    if declared > available:
        raise FormatError(
            f"the 'SSND' chunk claims {declared} bytes and only {available} are "
            "present: the file is truncated"
        )
    return _Aiff(channels, frames, bits, int(round(rate)), codec, offset, declared)


def _aiff_facts(path: Path) -> FormatFacts:
    aiff = _read_aiff(path)
    if aiff.channels < 1 or not aiff.sample_rate_hz:
        raise FormatError("the 'COMM' chunk declares no channels or a 0 Hz rate")
    return FormatFacts(
        "aiff",
        aiff.codec,
        aiff.sample_rate_hz,
        aiff.channels,
        aiff.bits_per_sample or None,
        aiff.frames / aiff.sample_rate_hz,
        "container-parser:aiff",
    )


#: Magic bytes -> the parser that reads that family's header. Dispatch on
#: content rather than on the extension: an ``.m4a`` renamed to ``.wav`` is a
#: file whose name lies about what is in it, and the point of parsing the
#: container is to notice.
_MAGIC_PARSERS: tuple[tuple[int, bytes, str], ...] = (
    (0, b"RIFF", "riff-wave"),
    (4, b"ftyp", "iso-bmff"),
    (0, b"fLaC", "flac"),
    (0, b"caff", "caf"),
    (0, b"FORM", "aiff"),
)

_PARSERS = {
    "riff-wave": _riff_facts,
    "iso-bmff": _bmff_facts,
    "flac": _flac_facts,
    "caf": _caf_facts,
    "aiff": _aiff_facts,
}


def container_family(path: Path) -> str:
    """The container family of ``path``, from its first bytes."""
    with path.open("rb") as handle:
        head = handle.read(16)
    for offset, magic, family in _MAGIC_PARSERS:
        if head[offset : offset + len(magic)] == magic:
            return family
    suffix = path.suffix.lower()
    known = (
        f" {suffix} is a container speaker_recording_spec accepts and this tool has "
        "no parser for; the recording is not wrong, this tool is incomplete for it."
        if suffix in UNPARSED_EXTENSIONS
        else ""
    )
    raise FormatError(
        f"the first bytes match no container this tool parses ({sorted(_PARSERS)})."
        + known
        + " Refusing rather than describing the format from a decoder's opinion of "
        "it: a decoder answers with what it converted to, which is exactly the "
        "claim a format report is supposed to be able to check"
    )


def format_facts(path: Path) -> FormatFacts:
    """Container, codec, rate, channels and declared duration — no decoder.

    Reading these from a decoder is the mistake this separation exists to make
    impossible: a decoder configured to deliver 16 kHz mono float will report
    16 kHz mono float for a 48 kHz stereo recording, and the resulting report
    would "prove" a transcode that never happened while hiding one that did.
    """
    family = container_family(path)
    facts = _PARSERS[family](path)
    suffix = path.suffix.lower()
    expected = CONTAINER_EXTENSIONS.get(family, frozenset())
    if suffix and suffix not in expected:
        raise FormatError(
            f"the file is named {suffix!r} and the container is {family!r} "
            f"({sorted(expected)}). A recording whose name disagrees with its "
            "content has been renamed or half-converted, and neither can be "
            "ingested as if it were the original"
        )
    return facts


# ── level facts, from decoding and nothing else ───────────────────────────────


@dataclass(frozen=True)
class LevelFacts:
    """What the samples say, once something has decoded them."""

    samples: int
    channels: int
    sample_rate_hz: int
    duration_s: float
    peak_dbfs: float
    rms_dbfs: float
    dc_offset: float
    full_scale_samples: int
    clipping_runs: int
    longest_clipping_run: int
    noise_floor_dbfs: float
    decoded_by: str

    def as_json(self) -> dict:
        return {
            "samples": self.samples,
            "channels": self.channels,
            "sample_rate_hz": self.sample_rate_hz,
            "duration_s": round(self.duration_s, 3),
            "peak_dbfs": round(self.peak_dbfs, 2),
            "rms_dbfs": round(self.rms_dbfs, 2),
            "dc_offset": round(self.dc_offset, 6),
            "full_scale_samples": self.full_scale_samples,
            "clipping_runs": self.clipping_runs,
            "longest_clipping_run_samples": self.longest_clipping_run,
            "noise_floor_dbfs": round(self.noise_floor_dbfs, 2),
            "decoded_by": self.decoded_by,
        }


def _dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return SILENCE_DBFS
    return max(float(20.0 * np.log10(amplitude)), SILENCE_DBFS)


_RIFF_DTYPES: dict[tuple[int, int], tuple[str, float, float]] = {
    (_WAVE_FORMAT_PCM, 8): ("<u1", 127.0, 128.0),
    (_WAVE_FORMAT_PCM, 16): ("<i2", 32767.0, 0.0),
    (_WAVE_FORMAT_PCM, 32): ("<i4", 2147483647.0, 0.0),
    (_WAVE_FORMAT_FLOAT, 32): ("<f4", 1.0, 0.0),
    (_WAVE_FORMAT_FLOAT, 64): ("<f8", 1.0, 0.0),
}


def _decode_riff(path: Path) -> tuple[np.ndarray, int, str]:
    riff = _read_riff(path)
    with path.open("rb") as handle:
        handle.seek(riff.data_offset)
        payload = handle.read(riff.data_bytes)

    if riff.tag == _WAVE_FORMAT_PCM and riff.bits_per_sample == 24:
        raw = np.frombuffer(payload[: len(payload) - len(payload) % 3], dtype=np.uint8)
        triples = raw.reshape(-1, 3).astype(np.int32)
        packed = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        signed = np.where(packed & 0x800000, packed - 0x1000000, packed)
        flat = signed.astype(np.float32) / 8388607.0
    else:
        try:
            dtype, scale, bias = _RIFF_DTYPES[(riff.tag, riff.bits_per_sample)]
        except KeyError:
            raise DecodeError(
                f"WAVE format 0x{riff.tag:04x} at {riff.bits_per_sample} bits is not "
                "decoded natively"
            ) from None
        width = np.dtype(dtype).itemsize
        usable = len(payload) - len(payload) % width
        flat = np.frombuffer(payload[:usable], dtype=dtype).astype(np.float32)
        flat = (flat - bias) / scale

    channels = max(riff.channels, 1)
    frames = flat.size // channels
    samples = flat[: frames * channels].reshape(frames, channels)
    return samples, riff.sample_rate_hz, "container-payload:riff-pcm"


def _decode_aiff(path: Path) -> tuple[np.ndarray, int, str]:
    aiff = _read_aiff(path)
    if aiff.codec not in ("pcm_be", "NONE", "sowt", "fl32", "FL32"):
        raise DecodeError(f"AIFF compression {aiff.codec!r} is not decoded natively")
    with path.open("rb") as handle:
        handle.seek(aiff.data_offset)
        payload = handle.read(aiff.data_bytes)
    endian = "<" if aiff.codec == "sowt" else ">"
    if aiff.codec in ("fl32", "FL32"):
        dtype, scale = f"{endian}f4", 1.0
    elif aiff.bits_per_sample == 16:
        dtype, scale = f"{endian}i2", 32767.0
    elif aiff.bits_per_sample == 32:
        dtype, scale = f"{endian}i4", 2147483647.0
    elif aiff.bits_per_sample == 8:
        dtype, scale = "i1", 127.0
    else:
        raise DecodeError(f"{aiff.bits_per_sample}-bit AIFF is not decoded natively")
    width = np.dtype(dtype).itemsize
    usable = len(payload) - len(payload) % width
    flat = np.frombuffer(payload[:usable], dtype=dtype).astype(np.float32) / scale
    channels = max(aiff.channels, 1)
    frames = flat.size // channels
    return (
        flat[: frames * channels].reshape(frames, channels),
        aiff.sample_rate_hz,
        "container-payload:aiff-pcm",
    )


#: pyav sample-format name -> the divisor that puts it in [-1, 1], and the bias
#: to remove first. ``u8`` is the only unsigned one.
_AV_SCALES: dict[str, tuple[float, float]] = {
    "u8": (127.0, 128.0),
    "s16": (32767.0, 0.0),
    "s32": (2147483647.0, 0.0),
    "flt": (1.0, 0.0),
    "dbl": (1.0, 0.0),
}


def _import_av():
    # Imported here rather than at the top: pyav is not a dependency of this
    # repository, and a WAVE submission must be ingestible without it.
    try:
        import av
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise DecoderUnavailable(
            "this payload needs a decoder and pyav is not installed in this "
            "environment. Level facts (peak, RMS, clipping, noise floor) cannot be "
            "reported without decoding, and reporting them from the container's "
            "declared values would be inventing measurements. Install pyav in the "
            "ingestion environment, or submit a container this tool decodes "
            "natively (WAVE PCM, AIFF PCM)"
        ) from exc
    return av


def _decode_av(path: Path) -> tuple[np.ndarray, int, str]:
    """Decode a compressed payload at its native rate and layout.

    No resampler and no format conversion: what is measured has to be what was
    recorded, and a resampler in this path would silently change the peak, the
    clipping count and the noise floor of every file it touched.
    """
    av = _import_av()
    blocks: list[np.ndarray] = []
    rate = 0
    codec = "unknown"
    try:
        with av.open(str(path)) as container:
            streams = container.streams.audio
            if not streams:
                raise DecodeError("the container has no audio stream")
            stream = streams[0]
            codec = stream.codec_context.name
            for frame in container.decode(stream):
                array = frame.to_ndarray()
                rate = frame.sample_rate or rate
                channels = max(frame.layout.nb_channels, 1)
                if array.ndim == 1:
                    array = array.reshape(1, -1)
                # ``to_ndarray`` returns (channels, samples) for a planar format
                # and (1, samples * channels) interleaved for a packed one.
                # Reading a packed frame as planar would fold two channels into
                # one signal and report a peak that is not in the recording.
                if frame.format is not None and frame.format.is_planar:
                    block = array.T
                else:
                    block = array.reshape(-1, channels)
                name = frame.format.name.rstrip("p") if frame.format else "flt"
                scale, bias = _AV_SCALES.get(name, (1.0, 0.0))
                blocks.append((block.astype(np.float32) - bias) / scale)
    except DecodeError:
        raise
    except Exception as exc:  # pyav raises a family of its own error types
        raise DecodeError(f"{type(exc).__name__}: {exc}") from exc

    if not blocks:
        return np.zeros((0, 1), dtype=np.float32), rate, f"pyav {av.__version__} ({codec})"
    return np.concatenate(blocks, axis=0), rate, f"pyav {av.__version__} ({codec})"


def decode(path: Path, facts: FormatFacts) -> tuple[np.ndarray, int, str]:
    """Samples in [-1, 1] as ``(frames, channels)``, plus the decoded rate."""
    if facts.container == "riff-wave":
        try:
            return _decode_riff(path)
        except DecodeError:
            return _decode_av(path)
    if facts.container == "aiff":
        try:
            return _decode_aiff(path)
        except DecodeError:
            return _decode_av(path)
    return _decode_av(path)


def level_facts(samples: np.ndarray, rate: int, decoded_by: str) -> LevelFacts:
    """Peak, RMS, DC offset, clipping and noise floor of a decoded payload.

    Clipping is counted on the per-frame maximum across channels, because a flat
    top on one channel is clipping of the take; RMS and DC offset are computed
    over every sample. The noise floor is the 10th-percentile frame RMS over
    20 ms frames at a 10 ms hop — the same geometry and percentile the earlier
    derivation recorded as ``noise_floor_dbfs``, so the two are comparable.
    """
    frames = int(samples.shape[0])
    channels = int(samples.shape[1]) if samples.ndim == 2 else 1
    if frames == 0:
        return LevelFacts(
            0, channels, rate, 0.0, SILENCE_DBFS, SILENCE_DBFS, 0.0, 0, 0, 0,
            SILENCE_DBFS, decoded_by,
        )

    flat = samples.astype(np.float32, copy=False)
    magnitude = np.abs(flat).max(axis=1)
    peak = float(magnitude.max())
    rms = float(np.sqrt(np.mean(np.square(flat, dtype=np.float64))))
    dc = float(np.mean(flat, dtype=np.float64))

    at_full_scale = magnitude >= (1.0 - FULL_SCALE_TOLERANCE)
    full_scale_samples = int(at_full_scale.sum())
    runs, longest = _runs(at_full_scale, CLIPPING_RUN_SAMPLES)

    mono = flat.mean(axis=1)
    floor = _noise_floor_dbfs(mono, rate)

    return LevelFacts(
        frames,
        channels,
        rate,
        frames / rate if rate else 0.0,
        _dbfs(peak),
        _dbfs(rms),
        dc,
        full_scale_samples,
        runs,
        longest,
        floor,
        decoded_by,
    )


def _runs(flags: np.ndarray, minimum: int) -> tuple[int, int]:
    """How many runs of ``True`` reach ``minimum``, and the longest one."""
    if not flags.any():
        return 0, 0
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = edges[0::2], edges[1::2]
    lengths = ends - starts
    return int((lengths >= minimum).sum()), int(lengths.max())


def _noise_floor_dbfs(mono: np.ndarray, rate: int) -> float:
    if not rate:
        return SILENCE_DBFS
    frame = max(int(rate * NOISE_FRAME_MS / 1000), 1)
    hop = max(int(rate * NOISE_HOP_MS / 1000), 1)
    if mono.size < frame:
        return _dbfs(float(np.sqrt(np.mean(np.square(mono, dtype=np.float64)))))
    count = 1 + (mono.size - frame) // hop
    windows = np.lib.stride_tricks.as_strided(
        mono, shape=(count, frame), strides=(mono.strides[0] * hop, mono.strides[0])
    )
    energies = np.sqrt(np.mean(np.square(windows, dtype=np.float64), axis=1))
    return _dbfs(float(np.percentile(energies, NOISE_PERCENTILE)))


# ── what may be excluded, decided in advance ─────────────────────────────────


@dataclass(frozen=True)
class ExclusionRule:
    """One predeclared reason a recording may be held out of a dataset."""

    code: str
    stage: str
    applies_to: str
    test: str
    why: str

    def as_json(self) -> dict:
        return {
            "code": self.code,
            "stage": self.stage,
            "applies_to": self.applies_to,
            "test": self.test,
            "why": self.why,
        }


#: The exclusion vocabulary, fixed before the recordings arrive.
#:
#: Two stages, because two different things are knowable at two different
#: times. ``ingestion`` rules are decidable from the audio alone and are applied
#: here. ``adjudication`` rules need a transcript, which this tool does not
#: produce and must not guess at; they are recorded in every report so the rule
#: that held four of E001's takes out of training is a predeclaration rather
#: than something invented later, when the numbers are visible.
#:
#: An excluded take is kept: same bytes, same label, same place in the frozen
#: manifest, plus a rule code and a reason. This is E001's and E002's policy
#: verbatim — "kept in the manifest with their original label; excluded from
#: training by the ``excluded`` flag, never deleted and never relabelled".
PREDECLARED_EXCLUSIONS: tuple[ExclusionRule, ...] = (
    ExclusionRule(
        "undecodable_payload",
        "ingestion",
        "any",
        "the container parses and no decoder can turn the payload into samples",
        "A file nobody can decode has no content to label. It is kept and frozen "
        "so the failure is attributable to these exact bytes rather than to a "
        "file somebody deleted.",
    ),
    ExclusionRule(
        "no_audio_payload",
        "ingestion",
        "any",
        "the payload decodes to zero samples",
        "An empty take is a recorder that never started. Counting it toward a "
        "section's required takes would let a section be satisfied by nothing.",
    ),
    ExclusionRule(
        "digital_silence",
        "ingestion",
        "any",
        "every decoded sample is zero",
        "A muted microphone, not a quiet delivery: there is no sound in the file "
        "at all. This is the ingestion-time form of E001's 'reports non-speech' "
        "rule, and it is the only silence rule here — a quiet take is a quiet "
        "take and is kept.",
    ),
    ExclusionRule(
        "positive_without_transcript",
        "adjudication",
        spec.POSITIVE,
        "an automatic transcript of the clip is empty",
        "E001's precedent: three of its four exclusions were positives whose "
        "transcript came back empty, which is evidence that the take does not "
        "contain the phrase it is filed as. Needs a transcriber, so it is "
        "decided by the derivation and recorded there — never here, and never "
        "from level facts alone.",
    ),
    ExclusionRule(
        "positive_reports_non_speech",
        "adjudication",
        spec.POSITIVE,
        "the transcriber reports the clip as non-speech",
        "E001's fourth exclusion. Same evidence class as an empty transcript and "
        "the same stage.",
    ),
)

#: Reasons that are explicitly *not* grounds for exclusion, with why not. This
#: is not decoration: every one of them was a condition an earlier round
#: measured the detector failing, and a pipeline that quietly dropped them would
#: report a model that works on audio nobody actually produces.
NEVER_EXCLUDED: tuple[tuple[str, str], ...] = (
    (
        "quiet",
        "The package asks for a genuinely quiet delivery on purpose; dropping "
        "quiet takes would delete a condition the detector has to survive.",
    ),
    (
        "distant",
        "Far-field is the condition a follow-up experiment measured at 53.3% "
        "firing against 15/15 close-mic. It is the point of the recording, not "
        "a defect in it.",
    ),
    (
        "noisy",
        "Real background is prescribed, and a high noise floor is what a room "
        "with the television on sounds like.",
    ),
    (
        "fast",
        "Speed is one of the five delivery conditions. A fast take is a fast "
        "take.",
    ),
    (
        "clipped",
        "A loud or clipped take is a real recording of a real delivery. The "
        "clipping runs and the count of full-scale samples are reported so the "
        "derivation can decide with the number in front of it.",
    ),
    (
        "short or long",
        "Duration is reported and never adjudicated here. A take shorter than "
        "the phrase is a finding for the derivation, which has the transcript.",
    ),
)

#: Findings a recording can carry. None of them excludes anything.
FINDINGS: dict[str, str] = {
    "quiet_take": f"peak below {QUIET_PEAK_DBFS} dBFS",
    "clipping": f"at least one run of {CLIPPING_RUN_SAMPLES} samples at full scale",
    "heavy_clipping": f"more than {HEAVY_CLIPPING_RUNS} clipping runs",
    "high_noise_floor": f"noise floor above {HIGH_NOISE_FLOOR_DBFS} dBFS",
    "dc_offset": f"mean sample value beyond {DC_OFFSET_LIMIT}",
    "short_take": f"shorter than {MIN_TAKE_SECONDS} s",
    "long_take": f"longer than {MAX_TAKE_SECONDS} s (not applied to the continuous sections)",
    "multichannel": "more than one channel; the derivation folds to mono",
    "below_production_rate": f"sampled below {PRODUCTION_SAMPLE_RATE_HZ} Hz",
    "duration_disagreement": "the decoded duration differs from the container's",
    "container_note": "the container parser recorded something about the header",
}

#: Sections whose takes are one continuous recording rather than one utterance,
#: so the "long take" finding does not apply to them.
CONTINUOUS_CATEGORIES: frozenset[str] = frozenset({"free_speech", "background_only"})


# ── one discovered recording ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Original:
    """One recording, as discovered, hashed, described and adjudicated."""

    path: str
    section: str
    category: str
    label: int
    phrase: str
    contested: bool
    contested_reason: str
    bytes: int
    sha256: str
    format: FormatFacts | None
    levels: LevelFacts | None
    findings: tuple[str, ...]
    problems: tuple[str, ...]
    excluded: bool
    exclusion_rule: str
    exclusion_reason: str

    @property
    def usable(self) -> bool:
        return not self.excluded and not self.problems

    def as_json(self) -> dict:
        row = {
            "path": self.path,
            "section": self.section,
            "category": self.category,
            "label": self.label,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "findings": list(self.findings),
            "excluded": self.excluded,
        }
        if self.phrase:
            row["phrase"] = self.phrase
        if self.contested:
            row["contested"] = True
            row["contested_reason"] = self.contested_reason
        if self.format is not None:
            row["format"] = self.format.as_json()
        if self.levels is not None:
            row["levels"] = self.levels.as_json()
        if self.excluded:
            row["exclusion_rule"] = self.exclusion_rule
            row["exclusion_reason"] = self.exclusion_reason
        if self.problems:
            row["problems"] = list(self.problems)
        return row


@dataclass(frozen=True)
class Check:
    """One requirement, and whether this submission meets it."""

    name: str
    title: str
    ok: bool
    detail: str
    problems: tuple[str, ...] = ()

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "ok": self.ok,
            "detail": self.detail,
            "problems": list(self.problems),
        }


@dataclass
class Plan:
    """Everything the write phase needs, and every verdict on the submission."""

    label: str
    assignment: Assignment
    layout: Layout
    submission: Path
    originals_root: Path
    submission_name: str
    originals: tuple[Original, ...] = ()
    litter: tuple[str, ...] = ()
    consent: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    checksums: dict = field(default_factory=dict)
    checks: tuple[Check, ...] = ()

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.ok)


# ── discovery ────────────────────────────────────────────────────────────────


def _originals_root(submission: Path) -> Path:
    """Where the recordings are: ``originals/`` when there is one.

    E002's submission put the tree under ``originals/`` and E001's did not.
    Both are read; nothing is guessed beyond the presence of the directory the
    package mandates.
    """
    nested = submission / ORIGINALS_DIR
    return nested if nested.is_dir() else submission


def _phrase_items() -> dict[str, spec.NearPhraseItem]:
    return {item.slug: item for item in spec.NEAR_PHRASE_ITEMS}


def _slug_of(stem: str) -> str | None:
    try:
        return spec.slugify(stem.strip().lower())
    except ValueError:
        return None


def discover(state: Plan) -> tuple[list[Original], list[str], list[str]]:
    """Classify every file under the recording tree, refusing what it cannot.

    Returns the classified recordings, the sync-client litter that is recognised
    and deliberately not copied, and the problems. A file that matches no
    section is a problem and not a skip: the package promises the speaker that
    nothing is guessed, and a recording whose label nobody can state is not a
    recording with an unknown label.
    """
    root = state.originals_root
    layout = state.layout
    problems: list[str] = []
    litter: list[str] = []
    rows: list[Original] = []
    items = _phrase_items()
    records = set(spec.SUBMISSION_ENTRIES) - {ORIGINALS_DIR}

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            if not _known_directory(relative, layout):
                problems.append(
                    f"{relative}/: not a section of the {layout.name} layout "
                    f"({sorted(section.directory for section in layout.sections)}); "
                    "ingestion refuses to guess what a directory it does not know holds"
                )
            continue
        if root == state.submission and relative in records:
            continue  # the records that travel with the tree, not recordings
        if path.name in freeze_manifest._SKIP_NAMES or path.name.startswith("."):
            # Recognised as editor and sync-client litter by the same list the
            # freezer skips, so what is discovered and what is frozen cannot
            # disagree. Named in the report rather than silently dropped.
            litter.append(relative)
            continue

        classified = _classify(relative, layout, items)
        if isinstance(classified, str):
            problems.append(classified)
            continue
        rows.append(classified)

    return rows, litter, problems


def _known_directory(relative: str, layout: Layout) -> bool:
    parts = relative.split("/")
    if len(parts) == 1:
        return layout.section(parts[0]) is not None
    return False


#: How the phrase in a filename was arrived at, which decides whether a phrase
#: is looked up at all. ``section`` means the file *is* the section — one
#: continuous recording of many phrases, as E001's session produced — so there
#: is no per-file phrase to look up.
_FROM_SECTION = "section"
_FROM_PATTERN = "pattern"
_FROM_FREE_TEXT = "free-text"


def _classify(
    relative: str, layout: Layout, items: dict[str, spec.NearPhraseItem]
) -> Original | str:
    """One file's section, category, label and phrase, or why it has none."""
    parts = relative.split("/")
    stem = Path(parts[-1]).stem
    suffix = Path(parts[-1]).suffix.lower()

    if suffix not in spec.AUDIO_EXTENSIONS:
        return (
            f"{relative}: {suffix or 'no extension'} is not a recorder audio "
            f"extension ({sorted(spec.AUDIO_EXTENSIONS)}); a file that is not a "
            "recording has no place in the recording tree"
        )

    if len(parts) == 1:
        section = layout.section(stem)
        if section is None:
            return (
                f"{relative}: no section of the {layout.name} layout is named "
                f"{stem!r}, and a recording at the top of the tree has to be one. "
                "Refusing to guess which section it belongs to"
            )
        return _row(relative, section, stem, items, _FROM_SECTION)

    if len(parts) != 2:
        return (
            f"{relative}: the recording tree is one level deep; a recording "
            "nested deeper than a section directory cannot be attributed to a "
            "section"
        )

    section = layout.section(parts[0])
    if section is None:
        return (
            f"{relative}: {parts[0]!r} is not a section of the {layout.name} "
            f"layout; ingestion refuses to guess what it holds"
        )
    if layout.group_directories:
        # The older session layout had no naming grammar inside a section: E002's
        # twelve near phrases are twelve separately recorded files named after
        # the phrase itself. The name still has to resolve to a phrase for a
        # phrase-keyed section, which is the check below.
        return _row(relative, section, stem, items, _FROM_FREE_TEXT)
    if section.stem is None or not section.stem.match(stem):
        return (
            f"{relative}: unrecognised file name; the package asks for "
            f"{section.grammar} in {section.directory}/. A recorder's own name "
            "for a file says nothing about what was said into it, so this is "
            "refused rather than renamed"
        )
    return _row(relative, section, stem, items, _FROM_PATTERN)


def _row(
    relative: str,
    section: Section,
    stem: str,
    items: dict[str, spec.NearPhraseItem],
    source: str,
) -> Original | str:
    """Build the classified row, resolving a phrase-keyed section's label."""
    phrase = ""
    contested = False
    reason = ""
    label = section.label

    if section.phrase_keyed and source != _FROM_SECTION:
        if source == _FROM_PATTERN and section.stem is not None:
            slug = section.stem.match(stem).group(1)
        else:
            slug = _slug_of(stem) or ""
        item = items.get(slug)
        if item is None:
            return (
                f"{relative}: {slug or stem!r} is not one of the near-phrase slugs in "
                "speaker_recording_spec.NEAR_PHRASE_ITEMS "
                f"({len(items)} phrases). This loader refuses to guess which phrase "
                "a file holds: a near miss filed as a positive teaches the detector "
                "to fire on ordinary speech, and a positive filed as a near miss "
                "teaches it not to fire on the wake phrase"
            )
        phrase = item.text
        item_label = LABEL_VALUES[item.label]
        if label is None:
            label = item_label
        elif item_label != label:
            # E002's precedent, and its exact policy: the section says what the
            # take was recorded as, the phrase inventory says what the detector
            # must do, and where they disagree the take keeps its section's
            # label and is reported as contested. Never relabelled here.
            contested = True
            reason = (
                f"speaker_recording_spec labels {item.text!r} {item.label!r} and this "
                f"section records it as label {label}. Kept with the section's label "
                "and reported, as E002's contested near phrases were: relabelling a "
                "recording after the fact rewrites what the speaker was asked to say"
            )

    if label is None:  # unreachable for the layouts here; a guard, not a default
        return f"{relative}: no label could be decided for this section"

    return Original(
        relative, section.directory, section.category, label, phrase, contested, reason,
        0, "", None, None, (), (), False, "", "",
    )


# ── describing and adjudicating one recording ────────────────────────────────


def describe(root: Path, row: Original) -> Original:
    """Hash the bytes, read the container, decode the payload, apply the rules."""
    path = root / row.path
    size = path.stat().st_size
    digest = freeze_manifest.sha256_file(path)
    problems: list[str] = []
    findings: list[str] = []

    if size > MAX_TAKE_BYTES:
        # Refused rather than excluded: an exclusion keeps a take in the manifest
        # under a predeclared rule, and "too big for this machine to read" is a
        # question for a person rather than a property of the recording.
        return _replace(
            row,
            bytes=size,
            sha256=digest,
            problems=(
                f"{row.path}: {size} bytes, past the {MAX_TAKE_BYTES}-byte ceiling "
                "this tool will decode. Decoding expands a take by roughly 15x, so "
                "reading this one would exhaust the machine rather than report "
                "anything about it. A file this size is a recorder left running "
                "rather than a take",
            ),
        )

    try:
        facts = format_facts(path)
    except FormatError as exc:
        return _replace(
            row,
            bytes=size,
            sha256=digest,
            problems=(f"{row.path}: {exc}",),
        )

    if facts.notes:
        findings.append("container_note")
    if facts.sample_rate_hz < PRODUCTION_SAMPLE_RATE_HZ:
        problems.append(
            f"{row.path}: recorded at {facts.sample_rate_hz} Hz, below the "
            f"{PRODUCTION_SAMPLE_RATE_HZ} Hz the production front end runs at. "
            "Nothing here resamples upward: the missing band cannot be recovered, "
            "and a take that cannot reach the feature extractor's rate is a "
            "re-record rather than an ingest"
        )
        findings.append("below_production_rate")
    if facts.channels > 1:
        findings.append("multichannel")

    excluded_rule = ""
    excluded_reason = ""
    levels: LevelFacts | None = None
    try:
        samples, rate, decoded_by = decode(path, facts)
        levels = level_facts(samples, rate or facts.sample_rate_hz, decoded_by)
    except DecoderUnavailable as exc:
        problems.append(f"{row.path}: {exc}")
    except DecodeError as exc:
        excluded_rule = "undecodable_payload"
        excluded_reason = str(exc)

    if levels is not None:
        if levels.samples == 0:
            excluded_rule = "no_audio_payload"
            excluded_reason = "the payload decoded to zero samples"
        elif levels.peak_dbfs <= SILENCE_DBFS:
            excluded_rule = "digital_silence"
            excluded_reason = "every decoded sample is zero"

        if levels.sample_rate_hz != facts.sample_rate_hz:
            problems.append(
                f"{row.path}: the container declares {facts.sample_rate_hz} Hz and "
                f"the decoder produced {levels.sample_rate_hz} Hz. One of the two is "
                "wrong about this file, and a level report measured at the wrong "
                "rate describes a recording nobody made"
            )
        findings.extend(_level_findings(row, facts, levels))
        if facts.duration_s and levels.duration_s < facts.duration_s * DURATION_TRUNCATION_FRACTION:
            problems.append(
                f"{row.path}: the container declares {facts.duration_s:.2f} s and only "
                f"{levels.duration_s:.2f} s decoded. A payload this far short of its "
                "own header is a truncated or damaged transfer"
            )

    return _replace(
        row,
        bytes=size,
        sha256=digest,
        format=facts,
        levels=levels,
        findings=tuple(dict.fromkeys(findings)),
        problems=tuple(problems),
        excluded=bool(excluded_rule),
        exclusion_rule=excluded_rule,
        exclusion_reason=excluded_reason,
    )


def _level_findings(row: Original, facts: FormatFacts, levels: LevelFacts) -> list[str]:
    findings: list[str] = []
    if levels.peak_dbfs < QUIET_PEAK_DBFS:
        findings.append("quiet_take")
    if levels.clipping_runs:
        findings.append("clipping")
    if levels.clipping_runs > HEAVY_CLIPPING_RUNS:
        findings.append("heavy_clipping")
    if levels.noise_floor_dbfs > HIGH_NOISE_FLOOR_DBFS:
        findings.append("high_noise_floor")
    if abs(levels.dc_offset) > DC_OFFSET_LIMIT:
        findings.append("dc_offset")
    if levels.duration_s < MIN_TAKE_SECONDS:
        findings.append("short_take")
    if (
        row.category not in CONTINUOUS_CATEGORIES
        and levels.duration_s > MAX_TAKE_SECONDS
    ):
        findings.append("long_take")
    if facts.duration_s:
        tolerance = max(
            DURATION_TOLERANCE_S, facts.duration_s * DURATION_TOLERANCE_FRACTION
        )
        if abs(levels.duration_s - facts.duration_s) > tolerance:
            findings.append("duration_disagreement")
    return findings


def _replace(row: Original, **changes) -> Original:
    values = {
        "path": row.path,
        "section": row.section,
        "category": row.category,
        "label": row.label,
        "phrase": row.phrase,
        "contested": row.contested,
        "contested_reason": row.contested_reason,
        "bytes": row.bytes,
        "sha256": row.sha256,
        "format": row.format,
        "levels": row.levels,
        "findings": row.findings,
        "problems": row.problems,
        "excluded": row.excluded,
        "exclusion_rule": row.exclusion_rule,
        "exclusion_reason": row.exclusion_reason,
    }
    values.update(changes)
    return Original(**values)


# ── the records that travel with the recordings ──────────────────────────────


def _consent_state(submission: Path) -> tuple[dict, list[str]]:
    path = submission / spec.CONSENT_FILE
    if not path.is_file():
        return {"file": spec.CONSENT_FILE, "present": False}, [
            f"missing {spec.CONSENT_FILE}: nothing binds these recordings to a "
            "consent that covers them, and consent is not something ingestion can "
            "assume was obtained"
        ]
    size = path.stat().st_size
    state = {
        "file": spec.CONSENT_FILE,
        "present": True,
        "bytes": size,
        "sha256": freeze_manifest.sha256_file(path),
    }
    if size == 0:
        return state, [f"{spec.CONSENT_FILE} is empty"]
    return state, []


def _metadata_state(submission: Path, label: str) -> tuple[dict, list[str]]:
    """Parse the device form and check every required field is really there.

    Only closed-vocabulary values are copied into the report. The form is
    free text a person fills in by hand, and a report is a thing that gets
    attached to other things: the digest proves which form was read without
    carrying its prose anywhere.
    """
    path = submission / spec.METADATA_FILE
    state: dict = {"file": spec.METADATA_FILE, "present": path.is_file()}
    if not path.is_file():
        return state, [f"missing {spec.METADATA_FILE}"]

    state["sha256"] = freeze_manifest.sha256_file(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return state, [f"{spec.METADATA_FILE} is not well-formed JSON: {exc}"]
    if not isinstance(data, dict):
        return state, [f"{spec.METADATA_FILE} must contain a single JSON object"]

    problems: list[str] = []
    present: list[str] = []
    for name in spec.REQUIRED_METADATA_FIELDS:
        if name not in data:
            problems.append(f"{spec.METADATA_FILE} is missing required field {name!r}")
            continue
        value = data[name]
        if name == "noise_sources_used":
            if not isinstance(value, list) or not value:
                problems.append(
                    f"{spec.METADATA_FILE} field 'noise_sources_used' must be a "
                    "non-empty list"
                )
                continue
            unknown = [item for item in value if item not in spec.NOISE_SOURCE_VOCAB]
            if unknown:
                problems.append(
                    f"{spec.METADATA_FILE} lists noise source(s) {unknown!r} outside "
                    f"{sorted(spec.NOISE_SOURCE_VOCAB)}"
                )
            state["noise_sources_used"] = sorted(
                {item for item in value if item in spec.NOISE_SOURCE_VOCAB}
            )
            present.append(name)
            continue
        if not isinstance(value, str) or not value.strip():
            problems.append(
                f"{spec.METADATA_FILE} field {name!r} must be a non-empty string"
            )
            continue
        if value.strip().startswith("<"):
            problems.append(
                f"{spec.METADATA_FILE} field {name!r} still holds the template "
                "placeholder text"
            )
            continue
        present.append(name)

    declared = data.get("speaker_id")
    if isinstance(declared, str) and declared.strip() and declared.strip() != label:
        problems.append(
            f"{spec.METADATA_FILE} names {declared.strip()!r} and this import is for "
            f"{label}. Guessing which is right is how a sealed voice reaches training"
        )

    state["required_fields_present"] = present
    return state, problems


def _checksum_state(state: Plan) -> tuple[dict, list[str]]:
    """Verify the coordinator's ``SHA256SUMS`` rather than only parsing it.

    ``validate_speaker_submission`` checks the listing's *structure* and says
    plainly that a digest recomputed on the machine that wrote it proves nothing
    about a transfer that has not happened yet. This runs after the transfer,
    which is the moment the digests mean something, and every original has been
    hashed here anyway.
    """
    path = state.submission / spec.CHECKSUM_FILE
    report: dict = {"file": spec.CHECKSUM_FILE, "present": path.is_file()}
    if not path.is_file():
        if state.layout.requires_checksums:
            return report, [
                f"missing {spec.CHECKSUM_FILE}: without it the folder cannot be shown "
                "to have survived the transfer intact"
            ]
        return report, []

    listed: dict[str, str] = {}
    problems: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        match = spec.CHECKSUM_LINE.match(line)
        if not match:
            problems.append(
                f"{spec.CHECKSUM_FILE} line {number} is not coreutils format"
            )
            continue
        listed[match.group(2)] = match.group(1)

    prefix = "" if state.originals_root == state.submission else f"{ORIGINALS_DIR}/"
    # The consent scan and the metadata form are in the listing too, and both
    # have already been hashed here. A tampered consent record is exactly as
    # serious as a tampered take, so it is checked with the same digest.
    expected: dict[str, str] = {
        f"{prefix}{row.path}": row.sha256 for row in state.originals
    }
    for record in (state.consent, state.metadata):
        if record.get("sha256"):
            expected[record["file"]] = record["sha256"]

    mismatched: list[str] = []
    unlisted: list[str] = []
    verified = 0
    for name, digest in sorted(expected.items()):
        recorded = listed.get(name)
        if recorded is None:
            unlisted.append(name)
            continue
        if recorded != digest:
            mismatched.append(name)
            continue
        verified += 1

    for name in sorted(unlisted):
        problems.append(f"{spec.CHECKSUM_FILE} does not list {name!r}")
    for name in sorted(mismatched):
        problems.append(
            f"{spec.CHECKSUM_FILE} records a different digest for {name!r}: the bytes "
            "on this machine are not the bytes that were hashed at the other end"
        )

    report.update(
        {
            "lines": len(listed),
            "checked": len(expected),
            "verified": verified,
            "mismatched": sorted(mismatched),
        }
    )
    return report, problems


# ── what is already imported ─────────────────────────────────────────────────


def imported_originals(into: Path) -> dict[str, tuple[str, str, str]]:
    """SHA-256 -> ``(label, role, where)`` for everything already ingested.

    Reads both manifest shapes that exist beside each other in the data root:
    the originals manifest this tool freezes (``files[].sha256``) and the
    derivation manifests frozen before this round (``files[].source_sha256``).
    Content addressing is the only check a rename and a re-copy cannot get
    past — a digest does not move with a file.
    """
    known: dict[str, tuple[str, str, str]] = {}
    if not into.is_dir():
        return known
    for directory in sorted(into.iterdir()):
        if not directory.is_dir() or not directory.name.startswith(SPEAKER_DIR_PREFIX):
            continue
        for filename in (MANIFEST_FILENAME, "MANIFEST.json"):
            path = directory / filename
            if not path.is_file():
                continue
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise Refused(
                    f"{directory.name}/{filename} cannot be read ({exc}). Refusing to "
                    "import: a manifest that cannot be parsed is a speaker whose "
                    "originals cannot be checked against this submission, and a "
                    "reuse check that silently reads nothing refuses nothing"
                ) from exc
            label = manifest.get("speaker") or manifest.get("dataset") or directory.name
            role = _manifest_role(f"{directory.name}/{filename}", manifest)
            for entry in manifest.get("files") or ():
                if not isinstance(entry, dict):
                    continue
                for key in ("sha256", "source_sha256"):
                    digest = entry.get(key)
                    if isinstance(digest, str) and len(digest) == 64:
                        known.setdefault(digest.lower(), (label, role, filename))
    return known


def _manifest_role(where: str, manifest: dict) -> str:
    """The role a manifest on disk records, from its split and its usage."""
    usage = manifest.get("usage")
    split = manifest.get("split")
    if isinstance(usage, str) and usage in freeze_manifest.USAGES:
        return usage
    if not isinstance(split, str) or split not in SPLIT_ROLES:
        raise Refused(
            f"{where} records split={split!r} and usage={usage!r}, and neither says "
            f"what the speaker may be used for ({sorted(SPLIT_ROLES)}). Refusing: an "
            "unattributable manifest beside this one means a reuse check cannot say "
            "whether a shared recording came from a sealed voice"
        )
    role = SPLIT_ROLES[split]
    marker = ROLE_PROSE_MARKERS[role]
    if isinstance(usage, str) and usage and marker not in usage.upper():
        raise Refused(
            f"{where} declares split={split!r} (which is {role}) and usage={usage!r}, "
            f"which does not mention {marker}. Refusing on the disagreement rather "
            "than believing half of a record of what a voice may be used for"
        )
    return role


# ── the plan, and every verdict in it ────────────────────────────────────────


def _choose_layout(label: str, root: Path, requested: str) -> Layout:
    """The layout to read the tree as, refusing one this label may not use."""
    allowed = layouts_for(label)
    if requested != "auto":
        for layout in allowed:
            if layout.name.startswith(requested):
                return layout
        raise Refused(
            f"speaker {label} may not submit in the {requested!r} layout "
            f"({[layout.name for layout in allowed]}). The older session layout is "
            "accepted only for the two speakers recorded before "
            "SPEAKER_RECORDING_PACKAGE.md existed; allowing it here would let an "
            "import be satisfied by four files while the near-phrase battery, both "
            "far-field conditions and the noise sections went unrecorded"
        )

    entries = {entry.name for entry in root.iterdir()} if root.is_dir() else set()
    best, score = allowed[0], -1
    for layout in allowed:
        recognised = len({name for name in entries if layout.section(Path(name).stem)})
        if recognised > score:
            best, score = layout, recognised
    return best


def plan(
    label: str,
    submission: Path,
    into: Path,
    *,
    layout: str = "auto",
) -> Plan:
    """Read the submission, check everything, and decide nothing else.

    Every verdict is a ``Check`` rather than an exception, because the Owner
    running ``--dry-run`` against a drive needs the whole list in one pass. The
    exceptions left are the ones where there is nothing to collect: an unknown
    speaker, a submission that is not there, a manifest beside this one that
    cannot be read.
    """
    where = assignment(label)
    _refuse_repo_path(submission, "a submission")
    if not submission.is_dir():
        raise Refused(f"the submission {submission.name!r} is not a directory")
    _refuse_label_mismatch(label, submission)

    _refuse_overlapping_ends(submission, into / f"{SPEAKER_DIR_PREFIX}{label}")

    root = _originals_root(submission)
    chosen = _choose_layout(label, root, layout)
    state = Plan(label, where, chosen, submission, root, submission.name)

    rows, litter, discovery_problems = discover(state)
    described = [describe(root, row) for row in rows]
    state.originals = tuple(described)
    state.litter = tuple(litter)

    consent, consent_problems = _consent_state(submission)
    metadata, metadata_problems = _metadata_state(submission, label)
    state.consent = consent
    state.metadata = metadata
    # After the two records above: the transfer check verifies their digests too.
    checksums, checksum_problems = _checksum_state(state)
    state.checksums = checksums

    checks: list[Check] = [
        Check(
            "role",
            "the role comes from the predeclared registry and cannot be overridden",
            True,
            f"{label} is {where.role} (split {where.split}), from {where.why}",
        ),
        _discovery_check(state, discovery_problems),
        _format_check(state),
        _levels_check(state),
        _labels_check(state),
        _sections_check(state),
        _submission_layout_check(state),
        Check(
            "consent",
            f"{spec.CONSENT_FILE} is present and not empty",
            not consent_problems,
            f"{consent.get('bytes', 0)} bytes" if not consent_problems else "",
            tuple(consent_problems),
        ),
        Check(
            "metadata",
            f"{spec.METADATA_FILE} parses and carries every required field",
            not metadata_problems,
            f"{len(metadata.get('required_fields_present', []))}/"
            f"{len(spec.REQUIRED_METADATA_FIELDS)} required fields",
            tuple(metadata_problems),
        ),
        Check(
            "transfer_digests",
            f"{spec.CHECKSUM_FILE} is verified against the bytes that arrived",
            not checksum_problems,
            f"{checksums.get('verified', 0)} of {checksums.get('checked', 0)} listed "
            "files verified"
            if checksums.get("present")
            else "no listing in this layout",
            tuple(checksum_problems),
        ),
        _exclusion_check(state),
        _duplicate_check(state, into),
        _reuse_check(state, into),
    ]
    # The privacy check reads the report body, and the body carries the checks —
    # including their messages, which is where a stray path would end up. So it
    # runs over a plan that already holds every other verdict.
    state.checks = tuple(checks)
    checks.append(_privacy_check(state, into))
    state.checks = tuple(checks)
    return state


def _refuse_label_mismatch(label: str, submission: Path) -> None:
    """A folder named for another speaker is refused, not reinterpreted.

    The role is never read off a path — that is what the registry is for — but a
    folder whose own name states a *different* label than the one being imported
    is two records disagreeing about whose voice this is, and one of the two
    would put a speaker in the wrong split.
    """
    name = submission.name
    stated = name[len(SPEAKER_DIR_PREFIX) :] if name.startswith(SPEAKER_DIR_PREFIX) else name
    if spec.SPEAKER_ID_PATTERN.match(stated) and stated != label:
        raise Refused(
            f"the submission folder states {stated!r} and this import is for {label}. "
            "Refusing rather than preferring either: the label decides whether this "
            "audio trains a model or is held back to measure one"
        )


def _discovery_check(state: Plan, problems: list[str]) -> Check:
    return Check(
        "discovery",
        "every file in the recording tree is classified, none skipped",
        not problems,
        f"{len(state.originals)} recordings classified in the {state.layout.name} "
        f"layout, {len(problems)} refused, {len(state.litter)} recognised litter "
        "file(s) not copied",
        tuple(problems),
    )


def _format_check(state: Plan) -> Check:
    problems = tuple(problem for row in state.originals for problem in row.problems)
    described = [row for row in state.originals if row.format is not None]
    readers = sorted({row.format.read_by for row in described if row.format})
    return Check(
        "format",
        "container facts are read from the container, not from a decoder",
        not problems,
        f"{len(described)}/{len(state.originals)} headers parsed by {readers}",
        problems,
    )


def _levels_check(state: Plan) -> Check:
    missing = [row.path for row in state.originals if row.levels is None]
    decoders = sorted({row.levels.decoded_by for row in state.originals if row.levels})
    return Check(
        "levels",
        "peak, RMS, DC offset, clipping and noise floor are measured for every take",
        not missing,
        f"{len(state.originals) - len(missing)} decoded by {decoders}",
        tuple(f"{path}: no level facts could be measured" for path in missing),
    )


def _labels_check(state: Plan) -> Check:
    positives = sum(1 for row in state.originals if row.label == LABEL_VALUES[spec.POSITIVE])
    contested = [row.path for row in state.originals if row.contested]
    return Check(
        "labels",
        "every label is decided by speaker_recording_spec, never by this tool",
        True,
        f"{positives} positive, {len(state.originals) - positives} negative, "
        f"{len(contested)} contested",
    )


def _sections_check(state: Plan) -> Check:
    found: dict[str, list[Original]] = {}
    for row in state.originals:
        found.setdefault(row.section, []).append(row)
    problems = (
        _package_completeness(state, found)
        if state.layout.name == LAYOUT_PACKAGE
        else _prior_completeness(state)
    )
    return Check(
        "sections",
        "every required section is present with enough usable takes",
        not problems,
        f"{len(found)} section(s) present",
        tuple(problems),
    )


def _package_completeness(state: Plan, found: dict[str, list[Original]]) -> list[str]:
    problems: list[str] = []
    for section in state.layout.sections:
        rows = found.get(section.directory, [])
        usable = [row for row in rows if row.usable]
        if section.required and not rows:
            problems.append(f"missing required section {section.directory}/")
            continue
        if rows and len(usable) < section.min_files:
            problems.append(
                f"{section.directory}/: {len(usable)} usable take(s) of "
                f"{len(rows)} present, need at least {section.min_files}. An "
                "excluded take does not count toward a section: a section "
                "satisfied by silence is a section that was not recorded"
            )

    items = _phrase_items()
    counts: dict[str, int] = dict.fromkeys(items, 0)
    for row in found.get("near_phrase", []):
        if row.usable and row.phrase:
            counts[spec.slugify(row.phrase)] += 1
    for slug, item in items.items():
        if counts[slug] < item.takes:
            problems.append(
                f"near_phrase/: {counts[slug]} usable take(s) of {item.text!r}, "
                f"need at least {item.takes}"
            )

    noise = [
        section
        for section in state.layout.sections
        if section.directory.startswith("positive_noise_") and found.get(section.directory)
    ]
    if len(noise) < spec.MIN_NOISE_SOURCES:
        problems.append(
            f"{len(noise)} background-noise section(s) present, need at least "
            f"{spec.MIN_NOISE_SOURCES} of "
            f"{sorted('positive_noise_' + source for source in spec.NOISE_SOURCE_VOCAB)}"
        )
    return problems


def _prior_completeness(state: Plan) -> list[str]:
    present = {row.category for row in state.originals if row.usable}
    return [
        f"no usable recording in the {category} section, which the session layout "
        "requires"
        for category in PRIOR_REQUIRED_CATEGORIES
        if category not in present
    ]


def _submission_layout_check(state: Plan) -> Check:
    """Fold in ``validate_speaker_submission``'s verdict on the package layout.

    That module owns the question "does this folder match the package?" — the
    four permitted entries, the naming grammar, the noise declarations, the
    structure of the checksum listing. Restating its rules here would produce a
    second copy to drift; the older session layout is not folded in because that
    module does not model it, and the completeness check above is what covers it.
    """
    if state.layout.name != LAYOUT_PACKAGE:
        return Check(
            "submission_layout",
            "the folder matches SPEAKER_RECORDING_PACKAGE.md",
            True,
            f"not applied: the {state.layout.name} layout predates the package",
        )
    result = submission_validator.validate_speaker_directory(state.submission)
    return Check(
        "submission_layout",
        "the folder matches SPEAKER_RECORDING_PACKAGE.md",
        result.ok,
        f"{len(result.warnings)} warning(s)",
        tuple(result.errors),
    )


def _exclusion_check(state: Plan) -> Check:
    codes = {rule.code for rule in PREDECLARED_EXCLUSIONS if rule.stage == "ingestion"}
    problems: list[str] = []
    for row in state.originals:
        if not row.excluded:
            continue
        if row.exclusion_rule not in codes:
            problems.append(
                f"{row.path}: excluded under {row.exclusion_rule!r}, which is not a "
                f"predeclared ingestion rule ({sorted(codes)})"
            )
        if not row.exclusion_reason:
            problems.append(f"{row.path}: excluded with no reason recorded")
    excluded = [row for row in state.originals if row.excluded]
    return Check(
        "exclusions",
        "every exclusion cites a predeclared rule, keeps its label and its bytes",
        not problems,
        f"{len(excluded)} excluded of {len(state.originals)}, all kept and frozen",
        tuple(problems),
    )


def _duplicate_check(state: Plan, into: Path) -> Check:
    manifest = into / f"{SPEAKER_DIR_PREFIX}{state.label}" / MANIFEST_FILENAME
    frozen = into / f"{SPEAKER_DIR_PREFIX}{state.label}" / "MANIFEST.json"
    existing = [path.name for path in (manifest, frozen) if path.is_file()]
    return Check(
        "duplicate",
        "this speaker has not been imported already",
        not existing,
        "no manifest for this speaker in the data root" if not existing else "",
        tuple(
            f"{name} already exists for {state.label}. Re-importing would either "
            "overwrite a frozen record or double-count a voice; a corrected "
            "submission is a new decision, not a rerun"
            for name in existing
        ),
    )


def _reuse_check(state: Plan, into: Path) -> Check:
    known = imported_originals(into)
    problems: list[str] = []
    seen: dict[str, str] = {}
    for row in state.originals:
        if not row.sha256:
            continue
        first = seen.setdefault(row.sha256, row.path)
        if first != row.path:
            problems.append(
                f"{row.path} is byte-identical to {first} in this submission. Two "
                "names for one recording double-count it and one of the two labels "
                "is wrong"
            )
        found = known.get(row.sha256)
        if found is None:
            continue
        other_label, other_role, source = found
        if other_label == state.label:
            problems.append(
                f"{row.path} is already imported for {state.label} (in {source})"
            )
            continue
        sealed = other_role == spec.ROLE_SEALED or state.assignment.role == spec.ROLE_SEALED
        problems.append(
            f"{row.path} has the same SHA-256 as a recording already imported for "
            f"{other_label} ({other_role}, {source})."
            + (
                " That crosses a seal: the same audio on both sides of it means the "
                "measurement has already seen what it is measuring."
                if sealed
                else " One recording cannot belong to two speakers."
            )
        )
    return Check(
        "reuse",
        "no original is reused within, across speakers, or across roles",
        not problems,
        f"{len(known)} digest(s) already imported, none of them here",
        tuple(problems),
    )


def _privacy_check(state: Plan, into: Path) -> Check:
    problems: list[str] = []
    try:
        _refuse_repo_path(into, "the data root")
    except Refused as exc:
        problems.append(str(exc))
    body = report_body(state, run={"planned": True})
    problems.extend(_privacy_problems(body))
    return Check(
        "privacy",
        "nothing leaves the machine: no host path, no identity beyond the label",
        not problems,
        "the report carries relative paths, digests and the label only",
        tuple(problems),
    )


def _refuse_repo_path(path: Path, what: str) -> None:
    """Neither end of an import may be inside the checkout.

    ``freeze_manifest`` refuses to write a manifest into the repository because
    a manifest lists the filenames of private speech. The same rule, one step
    earlier and in both directions: a submission read from inside the checkout is
    already one ``git add -A`` from being committed, and a data root inside it
    would put raw audio there by design.
    """
    repo = Path(__file__).resolve().parents[2]
    resolved = path.resolve()
    if resolved == repo or repo in resolved.parents:
        raise Refused(
            f"refusing {what} inside the repository ({resolved.name}). Recorded "
            "speech, a consent scan and a filled metadata form are the three things "
            "that must never reach git or CI; keep them on external local disk"
        )


def _refuse_overlapping_ends(submission: Path, derived: Path) -> None:
    """An import has to have two ends.

    --into pointed at the submission itself makes this speaker destination a
    subdirectory of the folder being read: the copy of recordings that cannot be
    made again lands on the same removable drive, inside the tree it was copied
    from, and one drive failure loses both while the report says
    byte_for_byte: True. The mirror case -- a destination that *is* the
    submission -- is worse, because every original is found already present at
    its target, so nothing is copied at all and the report still reads as a
    completed import of a preserved copy that does not exist.
    """
    left, right = submission.resolve(), derived.resolve()
    if left == right or right.is_relative_to(left) or left.is_relative_to(right):
        raise Refused(
            f"the submission {left.name!r} and this speaker destination "
            f"{right.name!r} are the same directory, or one is inside the other. "
            "An import copies from one tree into another: a copy inside the folder "
            "it came from is not a second copy of an irreplaceable recording, and "
            "a destination that is already the source reports every original as "
            "present without copying one"
        )


_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{1,2})")

#: A POSIX absolute path anywhere in a string, not only at its start. The
#: anchor above misses the shape a leak actually has -- an --note or an
#: OSError message that names a path mid-sentence -- and outside /mnt
#: (a Linux CI box, a mac, /media, /home) nothing else here caught one.
_EMBEDDED_POSIX_PATH = re.compile(r"(?<![\w.])/[\w.@+-]+/[\w.@+-]+")


def _privacy_problems(body: dict) -> list[str]:
    """Every value in a report body that would leak a path or an identity."""
    problems: list[str] = []

    def text(node: str, trail: str) -> None:
        if _ABSOLUTE_PATH.match(node):
            problems.append(f"{trail} is an absolute path")
        elif "/mnt/" in node or re.search(r"[A-Za-z]:[\\/]", node):
            problems.append(f"{trail} names a host path")
        elif _EMBEDDED_POSIX_PATH.search(node):
            problems.append(f"{trail} names a host path")

    def walk(node, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                # The key as well as the value: a body keyed by path reads as
                # structure rather than as data, and a scan that only visited
                # leaves would publish a whole capture tree as dictionary keys.
                if isinstance(key, str):
                    text(key, f"{trail}.<key>")
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")
        elif isinstance(node, str):
            text(node, trail)

    walk(body, "report")
    return problems


# ── the report ───────────────────────────────────────────────────────────────


def _canonical(body: dict) -> bytes:
    """The bytes a record's identity is computed over.

    Sorted keys, no incidental whitespace, non-ASCII kept as itself — the same
    rules ``freeze_manifest`` hashes a manifest under, so a digest in a report
    and a digest in a manifest mean the same kind of thing.
    """
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


#: Report fields outside the content digest: when the run happened, and what the
#: run itself had left to do. A resumed run copies fewer files than the run it is
#: finishing and must still produce the same content digest, or immutability and
#: resumability would contradict each other. What the run *found* — every digest,
#: every level fact, every exclusion, and the byte-for-byte verdict — is inside
#: the digest, because that is what the report is evidence of.
VOLATILE_REPORT_FIELDS: frozenset[str] = frozenset(
    {"created_utc", "content_sha256", "run"}
)


def content_digest(body: dict) -> str:
    return hashlib.sha256(
        _canonical({k: v for k, v in body.items() if k not in VOLATILE_REPORT_FIELDS})
    ).hexdigest()


def report_body(
    state: Plan,
    *,
    note: str = "",
    preservation: dict | None = None,
    run: dict | None = None,
) -> dict:
    """The ingestion report: what arrived, what it is, and what was decided.

    Relative paths, digests, counts, level facts and the speaker's label. No
    absolute path (a report is meant to be attachable to a decision; the drive
    it was read from is not), no transcript, no audio, and nothing from the
    metadata form beyond its digest and the closed-vocabulary noise sources.
    """
    originals = state.originals
    excluded = [row for row in originals if row.excluded]
    body: dict = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "speaker": state.label,
        "role": state.assignment.role,
        "split": state.assignment.split,
        "usage": state.assignment.role,
        "role_source": state.assignment.why,
        "role_registry": [
            "round8_config.SPLITS",
            "speaker_recording_spec.SPEAKER_ASSIGNMENTS",
        ],
        "layout": state.layout.name,
        "submission_root_name": state.submission_name,
        "derived_root_name": f"{SPEAKER_DIR_PREFIX}{state.label}",
        "originals_dir": ORIGINALS_DIR,
        "manifest": MANIFEST_FILENAME,
        "note": note,
        "counts": {
            "originals": len(originals),
            "usable": sum(1 for row in originals if row.usable),
            "excluded": len(excluded),
            "positive": sum(1 for row in originals if row.label == 1),
            "negative": sum(1 for row in originals if row.label == 0),
            "contested": sum(1 for row in originals if row.contested),
            "bytes": sum(row.bytes for row in originals),
            "audio_seconds": round(
                sum(row.levels.duration_s for row in originals if row.levels), 2
            ),
        },
        "sections": _section_rows(state),
        "originals": [row.as_json() for row in originals],
        "not_copied": list(state.litter),
        "exclusions": {
            "policy": (
                "kept in the manifest with their original label and hashes; held out "
                "of a dataset by the 'excluded' flag, never deleted and never "
                "relabelled"
            ),
            "rules": [rule.as_json() for rule in PREDECLARED_EXCLUSIONS],
            "never_excluded": [
                {"condition": condition, "why": why} for condition, why in NEVER_EXCLUDED
            ],
            "excluded": [
                {
                    "path": row.path,
                    "rule": row.exclusion_rule,
                    "reason": row.exclusion_reason,
                    "label": row.label,
                    "category": row.category,
                }
                for row in excluded
            ],
        },
        "findings_vocabulary": FINDINGS,
        "consent": state.consent,
        "metadata": state.metadata,
        "transfer_checksums": state.checksums,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "container_readers": sorted(_PARSERS),
        },
        "checks": [check.as_json() for check in state.checks],
        "preservation": preservation or {},
        "run": run or {},
    }
    body["content_sha256"] = content_digest(body)
    return body


def _section_rows(state: Plan) -> list[dict]:
    rows: dict[str, dict] = {}
    for row in state.originals:
        entry = rows.setdefault(
            row.section,
            {
                "directory": row.section,
                "category": row.category,
                "label": row.label,
                "files": 0,
                "usable": 0,
                "excluded": 0,
            },
        )
        entry["files"] += 1
        entry["usable"] += int(row.usable)
        entry["excluded"] += int(row.excluded)
    return [rows[key] for key in sorted(rows)]


# ── writing, atomically and once ─────────────────────────────────────────────


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` so no reader ever sees half of it."""
    # Unique per writer, not per path: two writers sharing one temporary name
    # means the first os.replace consumes the file the second staged, and the
    # second then fails with a bare FileNotFoundError after its own caller has
    # been told the write succeeded. Process *and* thread, because two imports
    # are two processes and a test that drives them is two threads.
    temporary = path.with_name(
        f"{path.name}.writing.{os.getpid()}.{threading.get_ident()}"
    )
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _copy_and_hash(source: Path, target: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as reader, target.open("wb") as writer:
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest()


def _plan_digest(state: Plan) -> str:
    """A digest of the work, so a resumed run can prove it is the same work."""
    return hashlib.sha256(
        _canonical(
            {
                "schema_version": SCHEMA_VERSION,
                "speaker": state.label,
                "role": state.assignment.role,
                "split": state.assignment.split,
                "layout": state.layout.name,
                "originals": {row.path: row.sha256 for row in state.originals},
                "excluded": {
                    row.path: row.exclusion_rule for row in state.originals if row.excluded
                },
            }
        )
    ).hexdigest()


def _open_journal(derived: Path, state: Plan, digest: str) -> dict:
    """Start or resume the journal, refusing to resume different work.

    The journal is what makes an interrupted import recoverable *and* safe: it
    records the exact digests the run was going to copy, so a rerun after a
    crash can tell "finish this" from "the drive changed underneath me", and the
    second one is a refusal. Without it, a resumed run would happily copy a
    re-exported take into a tree the earlier half of the run had already hashed.
    """
    path = derived / JOURNAL_FILENAME
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        # Only an *unfinished* journal binds this run. A finished one describes an
        # import that already has a report and a manifest, and those two refuse to
        # be replaced on their own terms, with better messages.
        if (
            existing.get("state") == "copying"
            and existing.get("plan_sha256")
            and existing["plan_sha256"] != digest
        ):
            raise Refused(
                f"{JOURNAL_FILENAME} records an interrupted import of different "
                f"work (plan {existing['plan_sha256'][:12]}, this run "
                f"{digest[:12]}). Refusing to finish one submission with another's "
                "files. Move the partial import aside deliberately if the "
                "submission really did change"
            )
    journal = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "speaker": state.label,
        "role": state.assignment.role,
        "split": state.assignment.split,
        "layout": state.layout.name,
        "submission_root_name": state.submission_name,
        "plan_sha256": digest,
        "state": "copying",
        "updated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "originals": {row.path: row.sha256 for row in state.originals},
    }
    derived.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, json.dumps(journal, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return journal


@contextlib.contextmanager
def _hold_data_root(into: Path):
    """Serialise the publish window against one data root, or refuse.

    O_CREAT | O_EXCL rather than an advisory lock: a data root is a
    removable drive, which arrives as exFAT under drvfs or as a network mount,
    and flock on those is either unsupported or silently a no-op. A lock
    that does nothing is worse than no lock, because it reads in the source as
    though the race were handled.

    Never broken automatically. Breaking a lock is the one action that
    reintroduces exactly the race this exists to stop, so a stale one is named
    and left for a person to remove.
    """
    path = into / LOCK_FILENAME
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Refused(
            f"another import holds {LOCK_FILENAME} in this data root. Two imports "
            "that both read the root before either publishes each see a root "
            "without the other files in it, so one recording present in two "
            "submissions is frozen under both speakers -- and if either of them "
            "is sealed, that seal has been spent and re-recording cannot restore "
            f"it. Wait for the other import; if none is running, remove "
            f"{LOCK_FILENAME} deliberately"
        ) from None
    try:
        os.write(handle, f"{os.getpid()}\n".encode("utf-8"))
        os.close(handle)
        yield
    finally:
        path.unlink(missing_ok=True)


def _refuse_a_root_that_moved(state: Plan, into: Path) -> None:
    """Re-adjudicate reuse and duplication against the root as it is now.

    Everything plan concluded about what is already imported was true when
    it read the root. Between then and here another import may have published,
    or the same operator may have imported the other speaker in a second
    terminal. Re-reading costs one pass over the manifests, and it is the only
    verdict whose staleness cannot be undone afterwards: a digest frozen under
    two speakers on opposite sides of a seal is not repaired by deleting a
    file, because the sealed speaker has now been seen.
    """
    for check in (_duplicate_check(state, into), _reuse_check(state, into)):
        if not check.ok:
            raise Refused(
                "refusing to publish: the data root changed after this import was "
                f"planned and {check.name!r} no longer holds: {list(check.problems)}"
            )


def _copy_originals(state: Plan, staging: Path, destination: Path) -> dict:
    """Copy every original, verifying each one before it is published.

    A copy is written into the staging directory, hashed as it is written,
    checked against the digest taken from the drive, and only then moved into
    place with one atomic rename. So the frozen tree never contains a partial
    file, and an interruption costs at most the file that was in flight.
    """
    copied = resumed = 0
    for row in state.originals:
        target = destination / row.path
        if target.is_file() and freeze_manifest.sha256_file(target) == row.sha256:
            resumed += 1
            continue
        temporary = staging / row.path
        temporary.parent.mkdir(parents=True, exist_ok=True)
        digest = _copy_and_hash(state.originals_root / row.path, temporary)
        if digest != row.sha256:
            raise Refused(
                f"{row.path} hashed {digest} on the way in and {row.sha256} on the "
                "drive. Refusing to publish a copy that is not the recording: "
                "nothing that follows would describe the audio anybody consented to"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, target)
        copied += 1
    return {"copied": copied, "already_present": resumed}


def _reverify(state: Plan, destination: Path) -> dict:
    """Re-hash both sides after the derived work, or publish nothing.

    "Preserved byte-for-byte" is a claim about the state of the drive *after*
    the tool ran, so it is measured then. Both sides, because they fail
    differently: a changed source means the drive was edited or is failing, and
    a changed copy means this tool or the filesystem under it did something to
    the bytes.
    """
    source_changed: list[str] = []
    copy_changed: list[str] = []
    for row in state.originals:
        if freeze_manifest.sha256_file(state.originals_root / row.path) != row.sha256:
            source_changed.append(row.path)
        if freeze_manifest.sha256_file(destination / row.path) != row.sha256:
            copy_changed.append(row.path)
    if source_changed or copy_changed:
        raise Refused(
            "the originals no longer hash to what they hashed at the start of this "
            f"import (submitted: {source_changed[:5]}, copied: {copy_changed[:5]}). "
            "Nothing has been published"
        )
    return {
        "originals_hashed": len(state.originals),
        "sources_reverified": len(state.originals),
        "copies_reverified": len(state.originals),
        "byte_for_byte": True,
    }


def _publish_report(staging: Path, derived: Path, body: dict) -> dict:
    """Publish the report once, and never a different one under the same name.

    Content-addressed, so re-running a completed import is a no-op rather than a
    rewrite, and a report whose *content* changed is a refusal: the report is
    what a later decision about this speaker refers to, and silently replacing
    it is the same as not having written one.
    """
    final = derived / REPORT_FILENAME
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if final.is_file():
        try:
            existing = json.loads(final.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise Refused(
                f"{REPORT_FILENAME} exists and cannot be read ({exc}). Refusing to "
                "replace a record that might describe a different import"
            ) from exc
        if existing.get("content_sha256") == body["content_sha256"]:
            return existing
        raise Refused(
            f"{REPORT_FILENAME} already describes a different import of "
            f"{body['speaker']} (recorded {existing.get('content_sha256', '')[:12]}, "
            f"this run {body['content_sha256'][:12]}). Refusing to overwrite it. If "
            "the submission legitimately changed, that is a new import decision and "
            "the old report is what an earlier one referred to"
        )
    staged = staging / REPORT_FILENAME
    _write_atomic(staged, payload)
    os.replace(staged, final)
    _write_atomic(
        derived / (REPORT_FILENAME + ".sha256"),
        f"{freeze_manifest.sha256_file(final)}  {REPORT_FILENAME}\n".encode("utf-8"),
    )
    return body


def _publish_manifest(state: Plan, staging: Path, derived: Path, originals_root: Path,
                      report_digest: str) -> dict:
    """Freeze the originals, then move the manifest into place last.

    ``freeze_manifest`` does the freezing — hash every file, hash the list,
    record split and usage, refuse to overwrite a differing manifest — and it
    writes into the staging directory so that the only thing that ever appears
    at the manifest's real name is a complete file, put there by one atomic
    rename after its sidecars. An interrupted import therefore has no manifest
    at all rather than one that reads as complete, which is the difference
    between "resume this" and "this speaker is imported".
    """
    staged = staging / MANIFEST_FILENAME
    sidecars = (
        staged.with_name(staged.stem + ".SHA256SUMS"),
        staged.with_name(staged.name + ".sha256"),
    )
    # Staging is scratch space, and a manifest left in it by an interrupted run
    # would make ``freeze`` return early as "already frozen" without rewriting
    # the sidecars — and skip the re-hash of the tree, which is the part worth
    # repeating after a crash.
    for leftover in (staged, *sidecars):
        leftover.unlink(missing_ok=True)
    fresh = freeze_manifest.freeze(
        originals_root,
        staged,
        dataset=state.label,
        split=state.assignment.split,
        usage=state.assignment.role,
        note=f"originals ingested by {TOOL}; ingestion report {report_digest}",
    )
    listed = {entry["path"] for entry in fresh["files"]}
    planned = {row.path for row in state.originals}
    if listed != planned:
        raise Refused(
            "the frozen manifest and this import disagree about which files exist "
            f"(only frozen: {sorted(listed - planned)[:5]}, only planned: "
            f"{sorted(planned - listed)[:5]})"
        )
    for name in (*sidecars, staged):  # the manifest last: it is what says "imported"
        os.replace(name, derived / name.name)
    return fresh


def execute(state: Plan, into: Path, *, note: str = "") -> dict:
    """Copy, verify, report and freeze — in the one order that is recoverable."""
    if not state.ok:
        raise Refused(
            "refusing to write anything: "
            + "; ".join(check.name for check in state.failures)
        )
    _refuse_repo_path(into, "the data root")
    if not into.is_dir():
        raise Refused(f"the data root {into.name!r} is not a directory")

    derived = into / f"{SPEAKER_DIR_PREFIX}{state.label}"
    _refuse_overlapping_ends(state.submission, derived)
    originals_root = derived / ORIGINALS_DIR
    # One staging directory per run, not one per speaker. Two imports of the
    # same speaker share derived, so a fixed name means both stage a copy at
    # the same path and the first os.replace consumes the file the second
    # staged. It also narrows the cleanup below to exactly what this run wrote.
    staging = derived / f"{STAGING_DIR}-{os.getpid()}-{threading.get_ident()}"
    if (derived / MANIFEST_FILENAME).is_file():
        raise Refused(
            f"{MANIFEST_FILENAME} already exists for {state.label}; this speaker is "
            "imported"
        )

    digest = _plan_digest(state)
    journal = _open_journal(derived, state, digest)
    staging.mkdir(parents=True, exist_ok=True)
    originals_root.mkdir(parents=True, exist_ok=True)

    copies = _copy_originals(state, staging, originals_root)
    preservation = _reverify(state, originals_root)

    body = report_body(state, note=note, preservation=preservation, run=copies)
    problems = _privacy_problems(body)
    if problems:
        raise Refused(
            "refusing to write a report that would leak a host path or an "
            f"identity: {problems}"
        )
    # The publish window, and only it, is serialised against the data root. The
    # reuse verdict is re-taken inside the lock: it is the one that reads the
    # whole root, and the only one that cannot be corrected afterwards.
    with _hold_data_root(into):
        _refuse_a_root_that_moved(state, into)
        published = _publish_report(staging, derived, body)
        fresh = _publish_manifest(
            state, staging, derived, originals_root, published["content_sha256"]
        )

    journal.update(
        {
            "state": "published",
            "updated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "report_content_sha256": published["content_sha256"],
            "manifest_sha256": fresh["manifest_sha256"],
        }
    )
    _write_atomic(
        derived / JOURNAL_FILENAME,
        json.dumps(journal, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    shutil.rmtree(staging, ignore_errors=True)
    return published


# ── the command line ─────────────────────────────────────────────────────────


def _print_checks(state: Plan) -> None:
    for check in state.checks:
        print(f"{'PASS' if check.ok else 'FAIL'}  {check.title}")
        if check.detail:
            print(f"        {check.detail}")
        for problem in check.problems[:20]:
            print(f"        - {problem}")
        if len(check.problems) > 20:
            print(f"        - ... {len(check.problems) - 20} more")


def _print_summary(state: Plan, body: dict) -> None:
    counts = body["counts"]
    print(
        f"{state.label}  {counts['originals']} originals, {counts['usable']} usable, "
        f"{counts['excluded']} excluded, {counts['contested']} contested"
    )
    print(f"  role={body['role']} split={body['split']} usage={body['usage']}")
    print(f"  report  {body['content_sha256']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest one speaker's submitted recordings, or refuse them."
    )
    parser.add_argument("--speaker", required=True, help="the predeclared label, e.g. E003")
    parser.add_argument(
        "--submission", required=True, type=Path, help="the assembled speaker folder"
    )
    parser.add_argument(
        "--into",
        required=True,
        type=Path,
        help="the external data root; never inside the repository",
    )
    parser.add_argument(
        "--layout",
        default="auto",
        choices=("auto", LAYOUT_PACKAGE, "prior"),
        help="how to read the recording tree (the older layout is accepted only for "
        "the speakers recorded before the package existed)",
    )
    parser.add_argument("--note", default="", help="recorded in the report and the manifest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and write nothing",
    )
    args = parser.parse_args(argv)

    state = plan(args.speaker, args.submission, args.into, layout=args.layout)
    _print_checks(state)

    if args.dry_run:
        # No ``preservation`` block: nothing was copied, so there is nothing to
        # claim was preserved. A dry run reports what it found, not what a real
        # run would have verified.
        body = report_body(state, note=args.note, run={"dry_run": True})
        if state.ok:
            _print_summary(state, body)
            print("DRY RUN: every requirement passed; nothing was written")
            return 0
        print(f"DRY RUN: {len(state.failures)} requirement(s) failed; nothing was written")
        return 1

    if not state.ok:
        print(f"REFUSED: {len(state.failures)} requirement(s) failed; nothing was written")
        return 1

    body = execute(state, args.into, note=args.note)
    _print_summary(state, body)
    print(f"  wrote {REPORT_FILENAME}, {MANIFEST_FILENAME} and its sidecars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
