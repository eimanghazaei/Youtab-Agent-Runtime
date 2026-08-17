#!/usr/bin/env python3
"""Round 8: build from real recordings only, or refuse to build at all.

Background
----------
Rounds 1-7 were fitted on synthesized speech and not one of them qualified.
Synthetic training is retired: Round 8 and every later candidate in this phase
is trained, validated and qualified exclusively on real human recordings and
real recorded environmental audio. Nothing synthetic ships, and nothing
synthetic is a starting point.

That decision is not something a dataset builder can hold in a comment. Every
prohibited input — a TTS clip, a voice-converted speaker, a generated impulse
response, a pitch-shifted copy, a synthetic tensor from an earlier round — is
one that produces a dataset that builds, a model that trains, and a number that
describes something other than what it says. So this stage is written to fail
closed: an input it cannot *prove* is a real recording is refused, and the
refusals are hard stops rather than warnings.

Why a separate module rather than a mode of ``build_dataset.py``
---------------------------------------------------------------
``build_dataset.py`` is synthetic-first by construction. It requires
``--tts-root``, reads TTS manifests, synthesizes room impulse responses with
pyroomacoustics, mixes background noise at a drawn SNR and re-levels every
window to a drawn peak. A gated mode inside that module would leave every
prohibited operation one flag, one default and one refactor away, and the
guard would be a check on *flags* rather than on the absence of a capability.

Here the capability is absent. This module never imports the augmenter, never
builds an impulse response, never mixes two recordings together and has no code
path that can. The prohibition is structural, which is the only form of it that
survives somebody adding a convenience flag in six months.

What it does reuse, rather than reimplement, from ``build_dataset``:
``read_wav16`` (so the accepted audio format is identical), ``sha256_file``,
the window geometry constants, and ``FeatureSink`` — so the feature space the
Round 8 classifier is fitted on is byte-for-byte the production front end over
production-length windows. The output file names are the same too, so
``train_model.py`` consumes this without changing.

Allowed processing, and nothing else
------------------------------------
Decode; the fixed resample and mono fold the derivation already applied;
deterministic trimming, padding and window extraction; the production feature
extractor; the fixed int16 scaling the runtime expects. None of those invents a
speech condition or acoustically alters a speaker.

That is why window placement here is a fixed ladder of trailing offsets rather
than ``build_dataset``'s seeded jitter, and why there is no augmentation at all.
The recordings already carry real rooms, real distances and real background,
because the recording package prescribes them; a room this stage invented would
be a room nobody was ever in.

The manifest contract
---------------------
A human speaker's manifest is accepted only if all of the following hold. Each
one is a way a manifest can look valid and mean something else:

* the speaker is in ``SPEAKER_SPLITS`` — an explicit registry, never inferred
  from a filename, because a filename is the one part of a dataset anybody can
  retype;
* the manifest is frozen: a ``MANIFEST.json.sha256`` sidecar exists and matches
  the bytes on disk, so "approved" refers to one exact derivation;
* the declared split equals the registry's split for that speaker, checked in
  both directions;
* the speaker is not sealed for a split this build may not touch;
* every clip row's own ``speaker`` and ``split`` agree with the header;
* every clip category is in ``HUMAN_CATEGORIES`` and its declared ``label``
  matches that table;
* every clip carries provenance — a ``source_file`` present in the manifest's
  own ``files`` list, with a matching ``source_sha256`` — and the bytes on disk
  hash to the recorded digest;
* every reference is *direct*: a clip, a full-length decode and a corpus file
  are named relative to the manifest's own directory, and a reference that is
  absolute, climbs out with ``..`` or arrives through a symlink is refused,
  because the bytes it reaches are not the bytes anybody froze;
* nothing the build touches is a known retired synthetic artifact by content —
  see below.

A category rename cannot buy admission. ``positive_human`` is checked for
provenance exactly as ``positive`` is refused by name: the guard that stops a
synthetic clip is the hash chain back to a real recording, not the string.

Content addressing, and the attack that needed it
-------------------------------------------------
Path rules and manifest corroboration are both defeated by a copy. Take one
synthetic wav out of ``data/tts``, put it in a directory named after an approved
human category, write a *fresh* manifest that hashes the copy correctly and
gives it an innocent ``source_file`` with an innocent ``source_sha256``, freeze
it. Every name reads clean, every hash self-checks, and the build succeeded —
measured, not assumed; see
``tests/tools/test_wakeword_synthetic_relocation_guard.py``.

A SHA-256 does not move with the file. So ``retired_synthetic_artifacts.json``
records the digests of known-synthetic artifacts and every clip, every
corroborating digest, every corpus file and every initialization checkpoint is
looked up in it. A byte-identical copy of anything *listed there* is refused
wherever it sits and whatever it is called, and a missing or unreadable registry
is a refusal rather than a pass — a guard that fails open is a guard that reports a clean build on the day
it breaks.

What that registry covers, and what it does not, is written into the registry
itself. The honest limit, in numbers: the retired TTS corpora are 207,300 clips
and 90 of them are listed -- 0.043%, a documented sample and not corpus
coverage, because a hash list of the rest is not something a source repository
can carry. For clips the primary guard is therefore still the provenance chain
above; content addressing closes rename-and-move only on the artifacts that can
be enumerated — every rejected candidate checkpoint and export, every
synthetic feature tensor, the synthetic-era detector this repository ships, and
the committed synthetic fixtures.

Usage::

    build_human_dataset.py --split train --out DIR \\
        --source human=/path/speaker/MANIFEST.json \\
        --source speech_commands=/path/sc.manifest.json \\
        --source recorded_background=/path/rooms.manifest.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import wave
from collections import Counter, defaultdict
from pathlib import Path, PureWindowsPath

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import assets  # noqa: E402
import build_dataset  # noqa: E402
import freeze_manifest  # noqa: E402

WINDOW_SAMPLES = build_dataset.WINDOW_SAMPLES
SAMPLE_RATE = build_dataset.SAMPLE_RATE

#: Speaker -> the split that speaker is bound to. The binding is one-way and
#: only ever *loses* privilege: a speaker who has been fitted on cannot become a
#: measurement, and a sealed speaker may be spent down into a lesser role by an
#: explicit Owner decision but can never be re-sealed. The reverse — quietly
#: promoting a fitted-on voice back into a seal — is what ``SEALED_SPLITS`` and
#: ``assert_consumed_speakers_not_sealed`` refuse.
#:
#: E002 was the round-6/7 sealed *evaluation* holdout. With only two human
#: speakers ever recorded for the sealed set, the Owner reassigned E002 from
#: ``eval_sealed`` to ``validation`` so a real held-out voice exists for
#: threshold/candidate/epoch selection; the decision is recorded, and made
#: irreversible, in ``CONSUMED_FOR_VALIDATION`` below. The sealed *final*
#: qualification holdout is now E006/E007 only, and E002 can never be counted
#: back into it. E001 stays train; E006/E007 stay sealed.
#:
#: An explicit table rather than a rule over filenames. A rule would assign a
#: split to a speaker nobody has decided about yet, which is how the one
#: unheard voice gets trained on by a run that looked correct.
SPEAKER_SPLITS: dict[str, str] = {
    "E001": "train",
    "E002": "validation",  # reassigned from eval_sealed; consumed, see below
    "E003": "train",
    "E004": "train",
    "E005": "validation",
    "E006": "qualification_sealed",
    "E007": "qualification_sealed",
}

#: Registry splits whose speakers are sealed. Nothing fitted on, nothing
#: threshold-selected on, nothing mined for hard negatives. ``eval_sealed`` is
#: kept defined even though no speaker is bound to it any more: it is still the
#: split name E002's frozen round-6/7 manifest carries, it is still the name
#: ``build_dataset``/``import_speaker`` read that historical manifest under, and
#: dropping it would silently make a re-appearance of an ``eval_sealed`` speaker
#: look like an unknown split rather than a sealed one.
SEALED_SPLITS = frozenset({"eval_sealed", "qualification_sealed"})

#: Speakers formally *consumed* for a non-sealed role by an Owner decision, and
#: therefore barred from ever being treated as sealed again. This is the durable,
#: machine-readable record of a one-way split move: ``assert_consumed_speakers_not_sealed``
#: turns any later edit that binds one of these back to a ``SEALED_SPLITS`` value
#: into a hard refusal at import time, so the consumption cannot be undone by a
#: quiet change to ``SPEAKER_SPLITS`` alone. The same record is mirrored in
#: ``round8_config.json`` (``splits.consumed``) and ``round8_config.py``.
CONSUMED_FOR_VALIDATION: dict[str, dict] = {
    "E002": {
        "for": "validation",
        "no_longer_sealed_holdout": True,
        "reason": (
            "only two human speakers available; Owner reassigned E002 from "
            "sealed evaluation to validation"
        ),
        "authorized": "owner",
    },
}


def assert_consumed_speakers_not_sealed(
    splits: dict[str, str] = SPEAKER_SPLITS,
    consumed: dict[str, dict] = CONSUMED_FOR_VALIDATION,
) -> None:
    """Refuse if any consumed speaker has been (re-)bound to a sealed split.

    The safety net that makes a consumption irreversible in code. A speaker
    recorded in ``CONSUMED_FOR_VALIDATION`` was spent for a non-sealed role by an
    explicit Owner decision; re-sealing it would retro-fit a "measured on a voice
    nothing was fitted on" claim onto a voice that has now been used for
    selection. That is exactly the silent regression the seal exists to prevent,
    so it is a hard refusal rather than a warning.

    Runs at import and is callable from a test with a mutated registry, so the
    proof that re-sealing E002 trips is a unit test, not a code review.
    """
    for speaker, record in consumed.items():
        if speaker not in splits:
            raise Refused(
                f"{speaker} is recorded as consumed-for-{record.get('for')!r} but is "
                f"absent from SPEAKER_SPLITS. A consumed speaker's binding is what the "
                "consumption is a promise about; it cannot simply disappear."
            )
        split = splits[speaker]
        if split in SEALED_SPLITS:
            raise Refused(
                f"{speaker} is bound to the sealed split {split!r} but is recorded in "
                f"CONSUMED_FOR_VALIDATION as spent for {record.get('for')!r} "
                f"({record.get('reason')}). A speaker used for a non-sealed role has "
                "been seen by selection; re-sealing it would claim an unseen-voice "
                "measurement that is no longer true. This move is one-way by Owner "
                "decision and is refused here."
            )


#: ``--split`` -> the one registry split whose speakers it may ingest.
#:
#: ``eval_sealed`` is deliberately not a value here, and now has no speaker bound
#: to it: E002 was the sealed evaluation speaker but the Owner reassigned it to
#: ``validation`` (see ``CONSUMED_FOR_VALIDATION``). As a validation speaker E002
#: *is* buildable into a validation tensor for threshold/candidate/epoch
#: selection, on the ``validation`` split below. It remains un-buildable for
#: training (validation is not training) and for qualification (that is E006 and
#: E007's sealed job); the general sealed guard in ``load_human_source`` keeps
#: enforcing both for every speaker still bound to a ``SEALED_SPLITS`` value.
BUILDABLE_SPLITS: dict[str, str] = {
    "train": "train",
    "validation": "validation",
    "qualification": "qualification_sealed",
}

#: ``--split`` -> the ``freeze_manifest`` purpose a recorded corpus must permit.
#: This is what makes ``assert_usable_for`` refuse a corpus the model was fitted
#: on being used to qualify it.
CORPUS_PURPOSE: dict[str, str] = {
    "train": "training",
    "validation": "validation",
    "qualification": "evaluation",
}

#: Declared human clip category -> the label it must carry. A closed table, and
#: the label is cross-checked rather than derived: a near miss filed as a
#: positive teaches the model to fire on the speaker's ordinary talking, and a
#: positive filed as a negative teaches it not to fire on the wake phrase.
#:
#: Every name ends in ``_human`` so a Round 8 dataset is self-describing in the
#: per-category tables ``evaluate_model.py`` prints. A manifest may declare
#: either these or the derivation's own names; see DERIVED_CATEGORY_ALIASES.
HUMAN_CATEGORIES: dict[str, int] = {
    "positive_human": 1,
    "near_phrase_human": 0,
    "free_speech_human": 0,
}

#: The dataset name every ``positive_<condition>`` collapses to.
HUMAN_POSITIVE_CATEGORY = "positive_human"

#: Category names the *derivation* writes, mapped to the dataset names this
#: stage emits. ``human_derive.py`` labels a real speaker's clips
#: ``near_phrase`` / ``free_speech`` / ``positive_<condition>``, and
#: ``build_dataset.load_human_clips`` already maps those to the ``_human``
#: names. This stage does the same rather than demanding the manifest be
#: rewritten.
#:
#: Refusing ``near_phrase`` *by name* was the first design here, and it was
#: wrong in a way worth recording: it made the frozen sealed-evaluation
#: manifest un-ingestible. That manifest's digest is what a qualification
#: result is traced to, so it cannot be regenerated to satisfy a naming rule —
#: and a rule that forces a sealed set to be rewritten has broken the thing it
#: was protecting.
#:
#: Nothing is weakened by accepting the name, because the name was never the
#: guard. Every row goes through ``_verify_provenance`` unconditionally, and a
#: synthetic clip declaring ``near_phrase`` is refused there, on where its
#: audio came from. The mutation test for that is the control: renaming a
#: synthetic clip to a human category must still be refused.
DERIVED_CATEGORY_ALIASES = {
    "near_phrase": "near_phrase_human",
    "free_speech": "free_speech_human",
}

#: A derived positive is ``positive_close``, ``positive_far_field``, and so on.
#: Mirrors ``build_dataset.HUMAN_POSITIVE_PREFIX``; the bare word ``positive``
#: is NOT covered, because that is the synthetic group's name and no derivation
#: emits it.
DERIVED_POSITIVE_PREFIX = "positive_"

#: What to tell an operator whose manifest declares something this stage cannot
#: place at all.
MANIFEST_CATEGORY_NOTE = (
    "Round 8 accepts the '_human' dataset names "
    f"({sorted(HUMAN_CATEGORIES)}) and the derivation's own names "
    f"({sorted(DERIVED_CATEGORY_ALIASES)}, plus "
    f"{DERIVED_POSITIVE_PREFIX}<condition>), which are normalised to them. "
    "Provenance, not the category string, is what decides whether a row is "
    "real."
)

#: Category names from the synthetic era with NO real-derivation meaning. A row
#: carrying one of these was written by the TTS pipeline or by a tool that does
#: not know this contract, and neither is ingestible. ``near_phrase`` and
#: ``free_speech`` are deliberately absent — see DERIVED_CATEGORY_ALIASES.
SYNTHETIC_CATEGORIES = frozenset(
    {
        "positive",
        "hardneg",
        "confusable",
        "softneg",
        "common",
        "common_speech",
        "synthesized_speech",
    }
)

#: Directory layouts the synthetic era's data lives in. ``data/features``
#: covers every previous round's tensor directory (``data/features``,
#: ``data/features_r6c1``, ``data/features_r7``) and ``data/tts`` covers the
#: generated clip tree.
#:
#: These are matched against *filesystem paths* — a manifest to read, ``--out``,
#: a checkpoint — so they are deliberately layout-specific. A one-word marker
#: here would refuse any working directory whose name happened to contain it,
#: and a guard that fires on the shape of somebody's scratch directory teaches
#: people to route around it.
SYNTHETIC_TREE_MARKERS: tuple[str, ...] = ("data/features", "data/tts", "/tts/")

#: Markers for a *recording reference* inside a manifest: a clip path or a
#: ``source_file``. Those strings are short, relative, and written by the
#: derivation rather than by whoever ran the command, so the generator and TTS
#: corpus names can be matched without false positives. A synthetic clip copied
#: out of those trees keeps its name, and this is what recognises it.
SYNTHETIC_SOURCE_MARKERS: tuple[str, ...] = (
    *SYNTHETIC_TREE_MARKERS,
    "piper",
    "libritts",
    "vctk",
    "synthesized",
    "synthetic",
    "tts_",
)

#: Background recordings that are *generated*, not recorded. Speech Commands'
#: ``_background_noise_`` directory is six files and two of them are synthesised
#: noise, which the Owner decision prohibits as squarely as it prohibits TTS
#: speech. They are named here so their exclusion is a decision with a count in
#: the stats rather than a filter nobody can see.
GENERATED_BACKGROUND_NAMES = frozenset({"pink_noise.wav", "white_noise.wav"})

#: Source type -> the ``assets.Source`` key that records its licence, and the
#: window category its samples are emitted under. A closed registry: an
#: unrecognised ``--source`` type is refused rather than skipped, because a
#: skipped source is a build that trains on part of its inputs and reports all
#: of them.
CORPUS_TYPES: dict[str, tuple[str, str]] = {
    "speech_commands": ("speech-commands", "recorded_speech"),
    "common_voice": ("common-voice", "recorded_speech"),
    "recorded_background": ("speech-commands", "background_only"),
}

#: Every source type ``--source`` accepts.
SOURCE_TYPES: tuple[str, ...] = ("human", *sorted(CORPUS_TYPES))

#: Trailing context, in seconds, for a phrase-anchored window: how much audio
#: follows the end of the clip inside the window.
#:
#: A fixed ladder, not a draw. The engine fires only after three consecutive
#: frames over threshold, so a phrase that finishes exactly at the window edge
#: scores high on one frame and never wakes; two to eight frames of trailing
#: context is the plateau the confirmation rule needs. Enumerating the ladder
#: gives the same coverage as ``build_dataset``'s jitter while staying inside
#: deterministic window extraction — nothing here draws a number.
#:
#: One value per frame across that plateau: at the runtime's 0.08 s frame
#: (``_OpenWakeWordEngine.frame_length`` = 1280 samples / SAMPLE_RATE 16 kHz)
#: the seven offsets land on frames [2, 3, 4, 5, 6, 7, 8], which is exactly
#: ``round8_config``'s ``window_construction.phrase_anchored_offsets_frames``.
#: ``round8_config.builder_windowing_divergence`` reconciles this ladder against
#: that predeclaration, so shortening it (e.g. back to the four-offset
#: [2, 4, 6, 8] ladder) makes ``round8_config --check`` fail (V6).
TRAILING_OFFSETS_S: tuple[float, ...] = (0.16, 0.24, 0.32, 0.40, 0.48, 0.56, 0.64)

#: The label-0 categories whose windows are phrase-anchored with
#: ``TRAILING_OFFSETS_S`` rather than tiled. A near-miss carries the wake
#: phrase, so — exactly like a positive — its score has to peak on the phrase
#: end, and that only happens when the window is anchored to that end by the
#: same trailing-offset ladder. A continuous negative (room tone, unrelated
#: speech, ``free_speech_human``) has no single phrase to anchor to and is tiled
#: across its whole length instead.
#:
#: Both ``windows_for`` and the ``build_windows`` emit loop branch on this set,
#: so it is the one place that decides phrase-anchoring vs tiling for a negative;
#: ``round8_config.builder_windowing_divergence`` reads it to keep the
#: predeclared ``near_phrase_human`` projection (utterances x offsets) and this
#: windowing choice from silently drifting apart (V6).
PHRASE_ANCHORED_NEGATIVES: frozenset[str] = frozenset({"near_phrase_human"})

#: Written beside the tensors, and required beside any checkpoint this phase is
#: allowed to initialise from.
CONTRACT_FILENAME = "DATASET_CONTRACT.json"

#: The content-addressed registry of retired synthetic artifacts: SHA-256 plus a
#: name and a kind, and never a byte of audio.
#:
#: Committed data rather than a table in this module, for two reasons. It has to
#: be reviewable as data — a hash that changed is a one-line diff — and it has to
#: be *absent-able*: a registry that cannot be read is a refusal, which is a
#: state a module constant cannot be in.
RETIRED_ARTIFACTS_FILENAME = "retired_synthetic_artifacts.json"

#: Beside this module, so it travels with the stage that enforces it and is
#: loadable with no training tree, no network and no dataset mounted.
RETIRED_ARTIFACTS_PATH = Path(__file__).resolve().parent / RETIRED_ARTIFACTS_FILENAME

#: The registry layout this module knows how to read. A registry written under a
#: different schema verifies the wrong thing, exactly as a corpus manifest does.
RETIRED_ARTIFACTS_SCHEMA_VERSION = 1

#: Parsed registries, keyed by path, size and mtime.
#:
#: The registry is consulted per clip, per corroborating digest and per corpus
#: file, so parsing it each time would be thousands of re-reads of one file in a
#: real build. Keying on the stat rather than on the path alone means a registry
#: that was rewritten — by an editor, or by a test proving what a broken one
#: does — is re-read rather than remembered.
_RETIRED_ARTIFACT_CACHE: dict[tuple[str, int, int], dict[str, dict]] = {}

#: Flags that must abort rather than be ignored. Ignoring one is worse than
#: rejecting it: a sweep arm that passed ``--rir-count 200`` and got a dataset
#: with no impulse responses in it would be recorded as an arm that had them.
REFUSED_FLAGS: dict[str, str] = {
    "--tts-root": "synthesized speech is retired; there is no TTS tree to read",
    "--positive-repeats": "synthetic positives no longer exist",
    "--hard-negative-repeats": "synthetic hard negatives no longer exist",
    "--confusable-repeats": "synthetic confusables no longer exist",
    "--softneg-repeats": "synthetic soft negatives no longer exist",
    "--recorded-negatives": "recorded negatives arrive as --source speech_commands=",
    "--noise-only": "room tone arrives as --source recorded_background=",
    "--rir-count": "generated room impulse responses are prohibited",
    "--reverb-fraction": "artificial reverberation is prohibited",
    "--snr-range": "artificially mixed speech-noise composites are prohibited",
    "--gain": "gain augmentation is prohibited",
    "--pitch-shift": "pitch shifting is prohibited",
    "--time-stretch": "time stretching is prohibited",
    "--speed-perturb": "speed augmentation is prohibited",
    "--augment": "no augmentation of any kind is applied to real recordings",
    "--voice-conversion": "voice conversion is prohibited",
    "--speaker-mix": "artificial speaker mixing is prohibited",
    "--piper-checkpoint": "TTS generation is retired",
    "--libritts-root": "TTS voices are prohibited as a data source",
    "--vctk-root": "TTS voices are prohibited as a data source",
    "--synthetic-positives": "synthetic positives are prohibited",
    "--synthetic-negatives": "synthetic negatives are prohibited",
    "--features": "a previous feature tensor is never read back in",
    "--reuse-features": "a previous feature tensor is never read back in",
    "--init-checkpoint": "a synthetic candidate is not a starting point",
    "--init-from": "a synthetic candidate is not a starting point",
    "--warm-start": "a synthetic candidate is not a starting point",
    "--human-manifest": "retired Round 6 flag; pass --source human=MANIFEST.json",
    "--human-positive-repeats": (
        "retired Round 6 flag; positives are windowed by --positive-windows"
    ),
}


class Refused(SystemExit):
    """A fail-closed refusal.

    ``SystemExit`` so an operator running this from a shell gets the message and
    a non-zero status without a traceback, exactly as ``build_dataset`` does its
    refusals. A named subclass so a test can assert that a build was refused
    rather than that it happened to exit.
    """


#: Enforce the consumption record at import: importing this module with a
#: registry that has E002 (or any consumed speaker) re-sealed is a hard failure,
#: not a build-time one, so the regression cannot even load. ``Refused`` is only
#: defined here, which is why the check runs after the class rather than beside
#: the registry it guards.
assert_consumed_speakers_not_sealed()


@dataclasses.dataclass(frozen=True)
class Sample:
    """One verified real recording, and the evidence that it is one.

    Every field is carried through to the per-window record on disk. A sample
    that reached this dataclass has had its bytes hashed against a manifest; the
    fields are what makes that checkable afterwards instead of asserted.
    """

    path: Path
    category: str
    label: int
    speaker: str
    split: str
    source_type: str
    #: The original recording this was derived from, as the manifest names it.
    source_file: str
    #: That original's SHA-256, as recorded and cross-checked in the manifest.
    source_sha256: str
    #: SHA-256 of the derived file, verified against the manifest.
    sha256: str
    licence: str
    #: SHA-256 of the manifest that vouched for this sample.
    manifest_sha256: str


# ── refusals that do not need a manifest ─────────────────────────────────────


def refuse_prohibited_flags(argv: list[str]) -> None:
    """Abort on any retired or synthetic-data flag, before anything is parsed.

    argparse would reject an unknown option too, but with "unrecognized
    arguments" and nothing about why. The point of an explicit table is that
    passing ``--rir-count`` says what is prohibited and that it cannot be turned
    back on here.
    """
    for token in argv:
        flag = token.split("=", 1)[0]
        if flag in REFUSED_FLAGS:
            raise Refused(
                f"{flag} is refused: {REFUSED_FLAGS[flag]}.\n"
                "Round 8 is trained, validated and qualified on real human "
                "recordings and real recorded environmental audio only. This "
                "flag is not ignored and it is not overridable."
            )


def _marker(value: str | Path, markers: tuple[str, ...]) -> str | None:
    haystack = str(value).replace("\\", "/").lower()
    for marker in markers:
        if marker in haystack:
            return marker
    return None


def synthetic_tree_marker(path: str | Path) -> str | None:
    """The synthetic-era tree ``path`` is inside, or None."""
    return _marker(path, SYNTHETIC_TREE_MARKERS)


def synthetic_source_marker(name: str) -> str | None:
    """The marker that makes a manifest's recording reference generated, or None."""
    return _marker(name, SYNTHETIC_SOURCE_MARKERS)


def refuse_synthetic_tree(path: str | Path, what: str) -> None:
    """Refuse a filesystem path inside a synthetic tree, in either direction.

    Applied to every input path *and* to ``--out``: reading a previous round's
    tensor would train on synthesized speech, and writing into that directory
    would leave a Round 8 dataset indistinguishable from the synthetic one
    beside it.

    Judged as written *and* as resolved, because a marker in a string is only a
    marker in the string somebody typed. Point ``inbox/E00x`` at a speaker
    directory inside ``data/tts`` and the manifest path, every clip path under it
    and ``--out`` all read clean while every byte comes out of the retired tree.
    ``resolve_within``'s symlink walk cannot see that one either: it stops at the
    root it is handed, and the root *is* the link.
    """
    _refuse(path, synthetic_tree_marker(path), what)
    resolved = Path(path).resolve()
    if resolved != Path(path):
        _refuse(resolved, synthetic_tree_marker(resolved), f"the resolved {what}")


def refuse_synthetic_source(name: str, what: str) -> None:
    """Refuse a manifest reference to a generated recording."""
    _refuse(name, synthetic_source_marker(name), what)


def _refuse(value: str | Path, marker: str | None, what: str) -> None:
    if marker is not None:
        raise Refused(
            f"{what} {value} matches the synthetic-era marker {marker!r}. "
            "Previous rounds' feature tensors and generated clip trees are not "
            "readable, extendable or writable from this stage: every sample in "
            "a Round 8 dataset has to trace back to a recording of a person or "
            "a room."
        )


def _hex64(value: object) -> bool:
    """A SHA-256 in the one spelling everything here records them in.

    Used by the registry loader as well as by the manifest checks, which is why
    it sits with the refusals that need no manifest.
    """
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def load_retired_artifacts(path: Path | None = None) -> dict[str, dict]:
    """SHA-256 -> the registry's record of that retired synthetic artifact.

    Fails closed on every way of not having a registry, because each of them is
    a state in which the build would otherwise proceed with the content check
    silently doing nothing: the file missing, unreadable, not JSON, written in
    something other than UTF-8, written under another schema, carrying no
    artifacts, carrying an entry with no hash, or listing one hash twice under
    two names. The last one matters more than it
    looks: a duplicate means one of the two names is wrong, and a registry
    nobody can trust the names in is a registry whose refusals nobody acts on.

    ``Refused`` rather than a warning. A registry that fails open is worse than
    no registry at all — it reports a clean build on the day it breaks.
    """
    path = RETIRED_ARTIFACTS_PATH if path is None else path
    try:
        stat = path.stat()
    except OSError as exc:
        raise Refused(
            f"the retired-artifact registry {path} cannot be read ({exc}). "
            "Refusing to build: without it nothing checks whether a clip is a "
            "renamed copy of retired synthetic material, and a build that "
            "cannot perform that check must not report having performed it."
        ) from exc

    key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _RETIRED_ARTIFACT_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(
            f"the retired-artifact registry {path} is not readable as JSON "
            f"({exc}). A registry that cannot be parsed is not a registry."
        ) from exc
    if not isinstance(body, dict):
        raise Refused(f"{path} does not contain a registry object")
    version = body.get("schema_version")
    if version != RETIRED_ARTIFACTS_SCHEMA_VERSION:
        raise Refused(
            f"{path} is schema_version {version!r}; this stage reads "
            f"{RETIRED_ARTIFACTS_SCHEMA_VERSION}. A registry read under the "
            "wrong schema checks the wrong field and refuses nothing."
        )
    rows = body.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise Refused(
            f"{path} lists no artifacts, so every content check in this build "
            "would pass by finding nothing. An empty registry is a refusal: it "
            "is indistinguishable from a registry whose contents were dropped."
        )

    index: dict[str, dict] = {}
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise Refused(f"{path}: artifacts[{position}] is not an object")
        digest = row.get("sha256")
        if not _hex64(digest):
            raise Refused(
                f"{path}: artifacts[{position}] records sha256={digest!r}, "
                "which is not a SHA-256. An entry that cannot be compared "
                "against a file is an entry that refuses nothing."
            )
        digest = digest.lower()
        for field in ("kind", "name"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise Refused(
                    f"{path}: artifacts[{position}] ({digest[:12]}) has no "
                    f"{field!r}. A refusal has to be able to say what it "
                    "recognised, or nobody can act on it."
                )
        if digest in index:
            raise Refused(
                f"{path}: {digest} is listed twice, as {index[digest]['name']!r} "
                f"and {row['name']!r}. One of those names is wrong."
            )
        index[digest] = row

    _RETIRED_ARTIFACT_CACHE[key] = index
    return index


def retired_artifact(digest: str, registry: dict[str, dict] | None = None) -> dict | None:
    """The registry's record of ``digest``, or None. Loads the registry if needed."""
    if registry is None:
        registry = load_retired_artifacts()
    if not isinstance(digest, str):
        return None
    return registry.get(digest.lower())


def refuse_retired_artifact(
    digest: str, what: str, registry: dict[str, dict] | None = None
) -> None:
    """Refuse a digest the registry knows, wherever the bytes now live.

    This is the guard a rename and a move cannot get past, and the only one that
    is not a statement about a name. Renaming a synthetic clip to
    ``positive_human``, copying it out of ``data/tts`` into a directory called
    ``clips/positive_human`` and writing a fresh manifest that hashes the copy
    correctly changes every string and no byte.
    """
    entry = retired_artifact(digest, registry)
    if entry is None:
        return
    raise Refused(
        f"{what} is a retired synthetic artifact. Its SHA-256 {digest} is "
        f"recorded in {RETIRED_ARTIFACTS_FILENAME} as {entry['name']!r} "
        f"({entry['kind']}). Round 8 trains, validates and qualifies on real "
        "recordings only; renaming retired material or moving it to an "
        "innocent path changes what it is called and not what it is."
    )


def refuse_retired_artifact_bytes(
    path: Path, what: str, registry: dict[str, dict] | None = None
) -> str:
    """Hash the bytes on disk and refuse them if the registry knows them.

    Returns the digest, so a caller that also has a recorded digest to check
    does not hash the same file twice.
    """
    digest = build_dataset.sha256_file(path)
    refuse_retired_artifact(digest, f"{what} {path}", registry)
    return digest


def refuse_escaping_name(name: str, what: str) -> Path:
    """A manifest reference has to be relative and has to stay inside its root.

    An absolute reference ignores the root entirely and a ``..`` climbs out of
    it, so either one reaches bytes that are not part of the frozen derivation
    while the manifest still reads like a self-contained record of one. The
    derivation writes ``path.relative_to(root).as_posix()`` for every reference
    it records, so nothing legitimate is refused here.

    Judged as a Windows path *as well as* a native one, because the platform the
    build runs on must not decide what the guard sees. ``PureWindowsPath``
    recognises a drive letter and a UNC root, and splits on both separators — so
    ``C:/x``, ``\\\\host\\share\\x`` and ``..\\x`` are refused on Linux too,
    where the native ``Path`` reads all three as one ordinary relative name.
    """
    candidate = Path(name)
    windows = PureWindowsPath(name)
    if candidate.is_absolute() or candidate.root or candidate.drive or windows.drive:
        raise Refused(
            f"{what} {name!r} is an absolute path. Every reference in a manifest "
            "is relative to that manifest's own root; an absolute one points "
            "outside the derivation that was frozen and hash-verified."
        )
    if ".." in windows.parts:
        raise Refused(
            f"{what} {name!r} climbs out of its own directory with '..'. A "
            "reference that leaves the frozen tree reaches bytes nobody froze, "
            "which is how retired material is read without ever being named."
        )
    return candidate


def resolve_within(name: str, base: Path, what: str) -> Path:
    """``base``/``name``, refusing anything that reaches outside ``base``.

    Three independent checks, because indirection has three shapes and only the
    first is visible in the manifest:

    * the reference itself is absolute or contains ``..`` (``refuse_escaping_name``);
    * a component of the path is a symlink — the manifest reads as a relative
      reference into its own tree and the bytes hashed are somewhere else
      entirely;
    * the fully resolved path is not under ``base`` anyway, which is the backstop
      for whatever the first two do not model (a junction, a mount point, a
      platform that spells indirection differently).

    The symlink walk is not redundant with the resolve: a symlink whose target
    happens to be inside ``base`` passes containment, and it is still a
    reference to bytes the freeze does not cover.
    """
    target = base / refuse_escaping_name(name, what)

    probe = target
    while probe != base and probe.parent != probe:
        if probe.is_symlink():
            raise Refused(
                f"{what} {name!r} is reached through the symlink {probe}. A "
                "manifest names files in its own frozen tree; a link means the "
                "bytes that get hashed and trained on are chosen by whoever "
                "created the link, not by the derivation that was adjudicated."
            )
        probe = probe.parent

    if not target.resolve().is_relative_to(base.resolve()):
        raise Refused(
            f"{what} {name!r} resolves to {target.resolve()}, which is outside "
            f"{base}. Refusing: a reference that leaves its own root is not "
            "part of the derivation the manifest's digest stands for."
        )
    return target


def refuse_synthetic_initialization(checkpoint: Path) -> None:
    """Refuse a checkpoint that is not provably from a human-only dataset.

    A synthetic candidate used as a starting point is a synthetic model with
    some real data fine-tuned onto it, which is the thing that is retired — and
    it is invisible afterwards, because the artifact looks like any other.

    Path shape alone cannot decide this, so the test is evidence: the checkpoint
    must sit beside the ``DATASET_CONTRACT.json`` that this stage writes, and
    that contract must assert zero synthetic samples. Absence is a refusal, not
    a benefit of the doubt.

    Evidence beside the file is still evidence *about the file*, and both halves
    of it are chosen by whoever laid the directory out: copy
    ``candidates/r7/checkpoint.pt`` into a clean directory as
    ``round8_pretrained_init.pt``, write a contract next to it that says
    ``human_only``, and the path check and the contract check both pass on an
    artifact fitted entirely on synthesized speech. The content address is what
    recognises it, and it runs before the contract is read — the bytes are judged
    before anything a copy can bring along with them is.
    """
    refuse_synthetic_tree(checkpoint, "initialization checkpoint")
    if not checkpoint.is_file():
        raise Refused(
            f"{checkpoint} is not a file, so nothing about it can be checked. "
            "An initialization this stage cannot hash is one it cannot show is "
            "not a retired candidate."
        )
    refuse_retired_artifact_bytes(checkpoint, "initialization checkpoint")
    contract_path = checkpoint.parent / CONTRACT_FILENAME
    if not contract_path.is_file():
        raise Refused(
            f"{checkpoint} has no {CONTRACT_FILENAME} beside it, so nothing "
            "says what it was fitted on. Rounds 1-7 are synthetic and are not "
            "startable from; a Round 8 candidate carries the contract of the "
            "dataset it saw."
        )
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(f"{contract_path} is not readable as JSON: {exc}") from exc
    if contract.get("human_only") is not True or contract.get("synthetic_samples") != 0:
        raise Refused(
            f"{contract_path} does not assert a human-only dataset "
            f"(human_only={contract.get('human_only')!r}, "
            f"synthetic_samples={contract.get('synthetic_samples')!r}). "
            "Initialising from it would carry synthetic training into Round 8."
        )


def registry_split(speaker: str) -> str:
    """The approved split for ``speaker``, or a refusal naming the registry."""
    try:
        return SPEAKER_SPLITS[speaker]
    except KeyError:
        raise Refused(
            f"speaker {speaker!r} is not in the approved registry "
            f"({sorted(SPEAKER_SPLITS)}). A speaker's split is a decision "
            "recorded here before any of their audio is read, never inferred "
            "from a path or a manifest."
        ) from None


def licence_for(source_key: str) -> str:
    """The recorded licence for a corpus, refusing one that was declined.

    ``assets.SOURCES`` is where this repository writes down whose data it uses
    and under what terms, and ``tests/tools/test_wakeword_asset_licences.py``
    keeps it complete. Reading the licence from there rather than restating it
    means a corpus cannot enter the Round 8 dataset without appearing in the
    provenance table the model card is generated from.
    """
    for source in assets.SOURCES:
        if source.key != source_key:
            continue
        if source.status == "declined":
            raise Refused(
                f"{source.name} is recorded as declined in assets.SOURCES "
                f"({source.notes.strip()[:120]}), so it cannot supply samples."
            )
        return f"{source.license} ({source.name} {source.version}, {source.status})"
    raise Refused(
        f"no assets.SOURCES entry for {source_key!r}; a corpus whose licence "
        "nobody wrote down cannot be used"
    )


# ── the human manifest ───────────────────────────────────────────────────────


def _read_manifest(path: Path, what: str) -> tuple[dict, str]:
    """Parse a manifest and return it with the SHA-256 of its bytes."""
    refuse_synthetic_tree(path, f"{what} manifest")
    if not path.is_file():
        raise Refused(f"{what} manifest {path} does not exist")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise Refused(
            f"{what} manifest {path} is not text at all ({exc}), so it is not a "
            "manifest. A submission delivered as a zip or a tar, or a tensor "
            "handed over as one, is not unpacked here: this stage has no unpacker, "
            "because an archive's members are chosen at extraction time and "
            "nothing frozen covers them. Extract it, freeze the extracted tree, "
            "and pass that manifest."
        ) from exc
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Refused(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise Refused(f"{path} does not contain a manifest object")
    return manifest, build_dataset.sha256_file(path)


def _require_frozen(path: Path, digest: str) -> None:
    """The manifest must carry its own digest, and match it.

    "Approved" has to mean one exact derivation. A manifest with no frozen
    digest beside it is a file that can be edited between the decision and the
    build — a take un-excluded, a category renamed, a hash corrected — and every
    one of those edits is invisible afterwards.
    """
    # The deriver writes this sidecar in the same step that records the
    # exclusions, so "frozen" and "adjudicated" are the same moment.
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise Refused(
            f"{path} has no {sidecar.name} beside it, so it is not frozen. "
            "Freeze the derivation before building from it; an unfrozen "
            "manifest cannot be the thing a candidate is traced back to."
        )
    recorded = sidecar.read_text(encoding="utf-8").split()
    if not recorded or recorded[0].lower() != digest:
        raise Refused(
            f"{sidecar.name} records {recorded[0] if recorded else '<empty>'} "
            f"but {path.name} hashes to {digest}. Refusing to build from a "
            "manifest that has changed since it was frozen."
        )


def _check_category(category: object, label: object, where: str) -> tuple[str, int]:
    """The declared category and label, or a refusal.

    The derivation's own names are normalised to the dataset names first, so a
    speaker does not have to be re-derived — and a frozen sealed manifest does
    not have to be rewritten — to satisfy a naming convention. What makes that
    safe is that the name was never the guard: every row is provenance-verified
    unconditionally, so a synthetic clip wearing a human category is refused on
    where its audio came from.

    Two independent failures remain. A synthetic-era name with no real-
    derivation meaning is refused outright. A label that disagrees with the
    category table is refused too: the label is what training optimises, and a
    near miss carrying label 1 is a lesson to fire on the speaker's ordinary
    speech.
    """
    if not isinstance(category, str) or not category:
        raise Refused(f"{where} declares no category")
    if category in SYNTHETIC_CATEGORIES:
        raise Refused(
            f"{where} declares the synthetic-era category {category!r}, which "
            f"is refused wherever it appears. {MANIFEST_CATEGORY_NOTE}"
        )
    if category in DERIVED_CATEGORY_ALIASES:
        category = DERIVED_CATEGORY_ALIASES[category]
    elif category.startswith(DERIVED_POSITIVE_PREFIX):
        category = HUMAN_POSITIVE_CATEGORY
    if category not in HUMAN_CATEGORIES:
        raise Refused(
            f"{where} declares category {category!r}, which is not in the "
            f"approved table {sorted(HUMAN_CATEGORIES)}. An unrecognised "
            "category is refused rather than skipped: skipping it would train "
            "on part of a manifest and report all of it."
        )
    expected = HUMAN_CATEGORIES[category]
    if label != expected:
        raise Refused(
            f"{where} is category {category!r} but declares label {label!r}; "
            f"that category is label {expected}. Refusing rather than "
            "preferring one of two disagreeing records of what was said."
        )
    return category, expected


def _originals_index(manifest: dict, path: Path) -> dict[str, dict]:
    """``source_file`` -> the manifest's own record of that original.

    The per-original list is the second half of the provenance chain. Without
    it a clip row's ``source_sha256`` is a number the row asserts about itself,
    which a fabricated row asserts just as easily.
    """
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise Refused(
            f"{path} has no 'files' list, so no clip in it can be corroborated "
            "against a record of the recording it came from."
        )
    index: dict[str, dict] = {}
    registry = load_retired_artifacts()
    for position, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise Refused(f"{path}: files[{position}] is not an object")
        name = entry.get("source_file")
        digest = entry.get("source_sha256")
        if not isinstance(name, str) or not name:
            raise Refused(f"{path}: files[{position}] has no 'source_file'")
        if not _hex64(digest):
            raise Refused(
                f"{path}: files[{position}] ({name}) records "
                f"source_sha256={digest!r}, which is not a SHA-256. An original "
                "without a hash is an original nobody can verify."
            )
        refuse_synthetic_source(name, f"{path}: files[{position}] source_file")
        # A ``source_file`` is a name inside the capture root, written by the
        # derivation as ``relative_to(root)``. An absolute one or a ``..`` names
        # a recording outside the session that was consented and adjudicated.
        refuse_escaping_name(name, f"{path}: files[{position}] source_file")
        # The recording this clip claims to come from is itself a known retired
        # artifact: the provenance chain is intact and points at the wrong thing.
        # Checked on the declared digest because the original is often not
        # mounted, and a declared digest is a claim the manifest cannot withdraw.
        refuse_retired_artifact(
            digest, f"{path}: files[{position}] source_file {name}", registry
        )
        if name in index:
            raise Refused(f"{path}: files lists {name!r} twice")
        index[name] = entry
    return index


def verify_originals(manifest: dict, path: Path, index: dict[str, dict]) -> dict[str, int]:
    """Hash every original the manifest claims, and fail closed on a mismatch.

    The originals live on the capture machine and are usually not mounted when
    a dataset is built, so there are two layers and both are checked:

    * if the recording is reachable under the manifest's ``source_dir``, its
      bytes are hashed against ``source_sha256``;
    * otherwise the full-length 16 kHz decode the derivation wrote — a
      one-to-one derivation of that original, with its own recorded digest — is
      hashed against ``full_16k_sha256``.

    A ``files`` entry that offers neither is refused. Reporting the split
    between the two is the point: "12 originals verified, 0 offline" and "0
    verified, 12 offline" are different claims and a build that could not tell
    them apart would let a missing capture drive read as a clean verification.
    """
    source_dir = manifest.get("source_dir")
    root = Path(source_dir) if isinstance(source_dir, str) and source_dir else None
    if root is not None:
        # ``source_dir`` is the one input path this stage is handed *inside* a
        # manifest rather than on a command line, and it was the one path nothing
        # judged. A manifest naming a directory in ``data/tts`` as its capture
        # root has its originals hashed out of the retired tree and then reports
        # ``originals_verified: N`` -- a positive verification claim, made
        # against synthesized speech, in the field that is supposed to be the
        # evidence that a person was recorded.
        refuse_synthetic_tree(root, f"{path}: source_dir")
    registry = load_retired_artifacts()
    counts = {"originals_verified": 0, "originals_offline": 0}
    for name, entry in sorted(index.items()):
        # ``source_dir`` is the capture root and is legitimately somewhere else
        # on the machine, so containment here is inside *it*, not inside the
        # manifest's directory: an original still may not be reached by climbing
        # out of the session or through a link into a retired tree.
        original = None
        if root is not None:
            original = resolve_within(name, root, f"{path}: original")
        if original is not None and original.is_file():
            actual = refuse_retired_artifact_bytes(
                original, f"{path}: original", registry
            )
            if actual != entry["source_sha256"]:
                raise Refused(
                    f"{path}: the original {name} hashes to {actual}, not the "
                    f"recorded {entry['source_sha256']}. Refusing to build: the "
                    "recording changed after it was derived, so nothing about "
                    "the derived clips describes what was actually said."
                )
            counts["originals_verified"] += 1
            continue

        relative = entry.get("full_16k")
        digest = entry.get("full_16k_sha256")
        if not isinstance(relative, str) or not _hex64(digest):
            raise Refused(
                f"{path}: original {name} is not reachable and the manifest "
                "records no verifiable full-length decode of it "
                "('full_16k' + 'full_16k_sha256'). Nothing on this machine can "
                "establish that the derived clips came from a real recording."
            )
        decoded = resolve_within(relative, path.parent, f"{path}: full_16k")
        if not decoded.is_file():
            raise Refused(
                f"{path}: {relative} is missing, so original {name} cannot be "
                "verified at either layer."
            )
        actual = refuse_retired_artifact_bytes(decoded, f"{path}: full_16k", registry)
        if actual != digest:
            raise Refused(
                f"{path}: {relative} hashes to {actual}, not the recorded "
                f"{digest}. The derived tree changed after it was frozen."
            )
        counts["originals_offline"] += 1
    return counts


def load_human_source(
    path: Path, split: str
) -> tuple[list[Sample], dict]:
    """Every usable clip of one approved speaker, with its provenance verified.

    Returns the samples in manifest order and a report of what was read. Order
    is the manifest's, not the filesystem's, so two builds of one manifest emit
    windows in the same sequence.
    """
    manifest, manifest_sha256 = _read_manifest(path, "human")
    _require_frozen(path, manifest_sha256)

    for field in ("speaker", "split", "usage", "clips", "files"):
        if field not in manifest:
            raise Refused(f"{path}: manifest has no {field!r} field")

    speaker = manifest["speaker"]
    if not isinstance(speaker, str):
        raise Refused(f"{path}: 'speaker' is {speaker!r}, not a name")
    approved = registry_split(speaker)
    declared = manifest["split"]
    if declared != approved:
        raise Refused(
            f"{path}: speaker {speaker} declares split {declared!r} but the "
            f"registry binds it to {approved!r}. Refusing on the disagreement "
            "itself rather than preferring either: whichever is wrong, the "
            "build would put the speaker somewhere nobody approved."
        )
    if approved in SEALED_SPLITS and split in ("train", "validation"):
        raise Refused(
            f"{path}: speaker {speaker} is sealed ({approved}) and --split "
            f"{split} would fit on it or select against it. The seal is the "
            "only measurement of whether the model generalises to a voice it "
            "has never heard; it is not overridable from the command line, on "
            "any combination of flags."
        )
    required = BUILDABLE_SPLITS[split]
    if approved != required:
        sealed = " (and it is sealed)" if approved in SEALED_SPLITS else ""
        raise Refused(
            f"{path}: speaker {speaker} is a {approved!r} speaker{sealed}, so "
            f"--split {split} may not ingest it; that split takes {required!r} "
            "speakers only."
        )

    index = _originals_index(manifest, path)
    report = {
        "type": "human",
        "manifest": path.name,
        "manifest_sha256": manifest_sha256,
        "speaker": speaker,
        "split": declared,
        "usage": manifest["usage"],
        "clips_in_manifest": len(manifest["clips"]),
        "originals": len(index),
        **verify_originals(manifest, path, index),
    }

    licence = "consented human recording, retained on local disk only"
    registry = load_retired_artifacts()
    samples: list[Sample] = []
    excluded = 0
    for position, clip in enumerate(manifest["clips"]):
        where = f"{path}: clips[{position}]"
        if not isinstance(clip, dict):
            raise Refused(f"{where} is not an object")

        # `excluded` is a listener's judgement about a take -- clipped, coughed
        # through, the wrong phrase. A missing flag reads as false and a string
        # reads as true whatever it spells, so neither is accepted.
        flag = clip.get("excluded")
        if not isinstance(flag, bool):
            raise Refused(
                f"{where} has excluded={flag!r}, which is not a boolean. That "
                "flag is the only thing keeping a rejected take out of the "
                "dataset."
            )
        if flag:
            excluded += 1
            continue

        # `clip`, not `path`: the deriver names the key after what it holds, and
        # reading the wrong key has already cost one defect.
        relative = clip.get("clip")
        if not isinstance(relative, str) or not relative:
            raise Refused(f"{where} has no 'clip' path")
        audio = resolve_within(relative, path.parent, f"{where} clip")
        if not audio.is_file():
            raise Refused(
                f"{where} points at {audio}, which is not a file. Clip paths "
                "are relative to the manifest's own directory."
            )
        refuse_synthetic_source(relative, f"{where} clip")

        if clip.get("speaker") != speaker or clip.get("split") != declared:
            raise Refused(
                f"{where} declares speaker {clip.get('speaker')!r} / split "
                f"{clip.get('split')!r}, but the manifest is {speaker} / "
                f"{declared!r}. One row from another speaker's manifest is how "
                "a speaker ends up on two sides of a split inside one build."
            )

        category, label = _check_category(clip.get("category"), clip.get("label"), where)
        source_file, source_sha256 = _verify_provenance(clip, index, where)

        digest = clip.get("sha256")
        if not _hex64(digest):
            raise Refused(
                f"{where} records sha256={digest!r}, which is not a SHA-256. "
                "A clip nobody can verify is a clip nobody can attribute."
            )
        # Hashed once: the same digest answers "are these the adjudicated bytes"
        # and "are these retired synthetic bytes". The content check runs on what
        # is actually on disk rather than on what the manifest claims, so a
        # manifest that lies about its own clip cannot route around it.
        actual = refuse_retired_artifact_bytes(audio, f"{where} clip", registry)
        if actual != digest:
            raise Refused(
                f"{where}: {relative} hashes to {actual}, not the recorded "
                f"{digest}. Refusing: the audio on disk is not the audio the "
                "manifest describes."
            )

        samples.append(
            Sample(
                path=audio,
                category=category,
                label=label,
                speaker=speaker,
                split=declared,
                source_type="human",
                source_file=source_file,
                source_sha256=source_sha256,
                sha256=digest,
                licence=licence,
                manifest_sha256=manifest_sha256,
            )
        )

    report["clips_excluded"] = excluded
    report["clips_used"] = len(samples)
    if not samples:
        raise Refused(
            f"{path}: every clip is excluded, so this source contributes "
            "nothing. A source that supplies no sample is a command-line "
            "mistake, not a configuration."
        )
    return samples, report


def _verify_provenance(clip: dict, index: dict[str, dict], where: str) -> tuple[str, str]:
    """The recording a clip came from, corroborated by the manifest's own list.

    This is the guard a category rename cannot get past. ``positive_human`` on a
    row whose ``source_file`` is a TTS clip, or whose ``source_sha256`` matches
    nothing in ``files``, is refused here — the name was never what was
    checked.
    """
    source_file = clip.get("source_file")
    source_sha256 = clip.get("source_sha256")
    if not isinstance(source_file, str) or not source_file:
        raise Refused(
            f"{where} names no 'source_file', so nothing says which recording "
            "it came from. Provenance is not optional: a sample without it "
            "cannot be shown to be a recording of a person at all."
        )
    if not _hex64(source_sha256):
        raise Refused(
            f"{where} records source_sha256={source_sha256!r}, which is not a "
            "SHA-256. A provenance claim with no hash in it is a filename."
        )
    refuse_synthetic_source(source_file, f"{where} source_file")
    entry = index.get(source_file)
    if entry is None:
        raise Refused(
            f"{where} claims to come from {source_file!r}, which the manifest's "
            "own 'files' list does not contain. Refusing: an uncorroborated "
            "provenance line is exactly what a synthetic clip relabelled to "
            "look human would carry."
        )
    if entry["source_sha256"] != source_sha256:
        raise Refused(
            f"{where} records source_sha256={source_sha256} for {source_file!r} "
            f"but 'files' records {entry['source_sha256']}. The two halves of "
            "the provenance chain disagree."
        )
    return source_file, source_sha256


# ── recorded corpora ─────────────────────────────────────────────────────────


def load_corpus_source(
    kind: str, path: Path, split: str
) -> tuple[list[Sample], dict]:
    """A frozen recorded corpus, as real negatives with verified hashes.

    Speech Commands and a governed Common Voice subset are recordings of real
    people in real rooms, and they are the only negatives Round 8 has. They are
    negatives *only*: neither corpus is consented as a positive speaker for this
    phrase, and presenting one as a positive would be a claim about a person who
    never said the wake word.

    The manifest is ``freeze_manifest.py``'s, which is what makes the usage gate
    available: ``assert_usable_for`` refuses a corpus the model was fitted on
    being used to qualify it.
    """
    source_key, category = CORPUS_TYPES[kind]
    licence = licence_for(source_key)
    manifest, manifest_sha256 = _read_manifest(path, kind)
    version = manifest.get("schema_version")
    if version != freeze_manifest.SCHEMA_VERSION:
        raise Refused(
            f"{path} is schema_version {version!r}; freeze_manifest.py writes "
            f"and reads {freeze_manifest.SCHEMA_VERSION}. A manifest read under "
            "the wrong schema verifies the wrong thing."
        )
    # A human manifest is frozen by a sidecar. A corpus manifest has no sidecar
    # and no second record of it anywhere, so the digest it carries over its own
    # canonical body is the only thing between a frozen corpus and an edited one:
    # flipping ``usage`` from ``sealed-evaluation`` to ``training``, adding a file
    # to the list or correcting a digest is otherwise invisible. Computed with
    # ``freeze_manifest``'s own canonicalisation, so this cannot disagree with the
    # tool that wrote the field.
    recorded = manifest.get("manifest_sha256")
    computed = freeze_manifest.manifest_digest(manifest)
    if recorded != computed:
        raise Refused(
            f"{path} records manifest_sha256={recorded!r} but its own body "
            f"hashes to {computed}. Refusing to build from a corpus manifest that "
            "has changed since it was frozen: the usage gate below is worth "
            "exactly what the record it reads is worth."
        )

    try:
        # The usage gate, not a reimplementation of it: this is what refuses a
        # corpus the model was fitted on being used to qualify it.
        freeze_manifest.assert_usable_for(manifest, CORPUS_PURPOSE[split])
    except (ValueError, freeze_manifest.SealedDatasetError) as exc:
        raise Refused(f"{path}: {exc}") from exc

    root = _corpus_root(path, manifest)
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise Refused(f"{path}: the frozen manifest lists no files")

    samples: list[Sample] = []
    generated: list[str] = []
    registry = load_retired_artifacts()
    for entry in entries:
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not _hex64(digest):
            raise Refused(
                f"{path}: a file entry records path={relative!r} "
                f"sha256={digest!r}; both are required and the hash must be one."
            )
        refuse_synthetic_source(relative, f"{path}: {kind} file")
        audio = resolve_within(relative, root, f"{path}: {kind} file")
        if not audio.is_file():
            raise Refused(f"{path}: {relative} is listed but missing under {root}")
        # A recorded corpus is the other place a synthetic wav can be filed under
        # an innocent name: freeze a directory of them and the manifest is a
        # perfectly consistent record of the wrong audio.
        actual = refuse_retired_artifact_bytes(audio, f"{path}: {kind} file", registry)
        if actual != digest:
            raise Refused(
                f"{path}: {relative} hashes to {actual}, not the frozen "
                f"{digest}. Refusing to build from a corpus that has drifted "
                "from the manifest measurements refer to."
            )
        if kind == "recorded_background" and audio.name in GENERATED_BACKGROUND_NAMES:
            # Named, counted and excluded rather than quietly filtered: two of
            # Speech Commands' six background files are synthesised noise, and
            # generated background is prohibited exactly as generated speech is.
            #
            # Excluded *after* the file has been resolved and hashed, not instead
            # of resolving and hashing it. Deciding on the name first made the
            # name a way to skip the content check: a retired artifact delivered
            # as ``pink_noise.wav`` was dropped-and-counted, and the build then
            # reported ``synthetic_samples: 0`` having never hashed it.
            generated.append(relative)
            continue
        samples.append(
            Sample(
                path=audio,
                category=category,
                label=0,
                speaker=f"corpus:{kind}",
                split=split,
                source_type=kind,
                source_file=relative,
                source_sha256=digest,
                sha256=digest,
                licence=licence,
                manifest_sha256=manifest_sha256,
            )
        )

    if not samples:
        raise Refused(
            f"{path}: no usable file left after excluding "
            f"{len(generated)} generated recording(s). A source that supplies "
            "nothing is a mistake rather than a configuration."
        )
    report = {
        "type": kind,
        "manifest": path.name,
        "manifest_sha256": manifest_sha256,
        "dataset": manifest.get("dataset"),
        "corpus_split": manifest.get("split"),
        "usage": manifest.get("usage"),
        "licence": licence,
        "files_listed": len(entries),
        "files_used": len(samples),
        "generated_excluded": sorted(generated),
    }
    return samples, report


def _corpus_root(path: Path, manifest: dict) -> Path:
    """Where a frozen corpus's files actually are.

    ``freeze_manifest`` records only the root's basename, deliberately — a
    manifest travels with reports and an absolute capture path does not. So the
    root is derived from where the manifest sits, and both conventions the tool
    itself produces are tried before giving up with the paths it looked in.
    """
    name = manifest.get("root_name")
    if not isinstance(name, str) or not name:
        raise Refused(f"{path}: the frozen manifest records no 'root_name'")
    # ``freeze_manifest`` records ``root.resolve().name``: one component, never a
    # path. Anything else is a reference that leaves the manifest's own directory
    # -- and it is the *root*, so every per-file containment check below would
    # then be performed against a tree the manifest was never frozen over.
    # ``root_name: "../../data/tts/positive_close"`` reads as a frozen,
    # internally consistent, fully hash-verified manifest of synthesized speech.
    refuse_escaping_name(name, f"{path}: root_name")
    if len(PureWindowsPath(name).parts) != 1:
        raise Refused(
            f"{path}: root_name {name!r} is not a single directory name. "
            "freeze_manifest.py records the dataset root's basename and nothing "
            "else, so a root_name carrying a separator names a directory "
            "somebody chose after the freeze rather than the one that was frozen."
        )
    candidates = [path.parent / name]
    if path.parent.name == name:
        candidates.append(path.parent)
    for candidate in candidates:
        if candidate.is_dir():
            refuse_synthetic_tree(candidate, f"{path}: the corpus root")
            return candidate
    raise Refused(
        f"{path}: the dataset root {name!r} is not beside the manifest. Looked "
        f"in {[str(c) for c in candidates]}. Manifests are written beside the "
        "data they freeze."
    )


# ── the build ────────────────────────────────────────────────────────────────


def parse_source(spec: str) -> tuple[str, Path]:
    """``TYPE=PATH``, refusing a type this stage does not know.

    Unknown is refused, never skipped. A skipped source produces a dataset that
    is missing a whole class of negatives while the command line says otherwise.
    """
    kind, _, value = spec.partition("=")
    if not value:
        raise Refused(f"--source {spec!r} is not TYPE=PATH")
    if kind not in SOURCE_TYPES:
        raise Refused(
            f"--source type {kind!r} is not recognised. Known types: "
            f"{list(SOURCE_TYPES)}. An unrecognised source is refused rather "
            "than ignored, because a source silently dropped is a dataset "
            "nobody can reconstruct from the command that built it."
        )
    return kind, Path(value)


def load_sources(specs: list[str], split: str) -> tuple[list[Sample], list[dict]]:
    """Every source, in command-line order, with all of them verified."""
    if split not in BUILDABLE_SPLITS:
        raise Refused(
            f"--split {split!r} is not one of {sorted(BUILDABLE_SPLITS)}. "
            "The sealed evaluation speaker is not buildable into a tensor by "
            "this stage at all; it is measured by streaming its audio through "
            "the runtime engine."
        )
    if not specs:
        raise Refused("no --source given; there is nothing to build from")

    samples: list[Sample] = []
    reports: list[dict] = []
    for spec in specs:
        kind, path = parse_source(spec)
        if kind == "human":
            loaded, report = load_human_source(path, split)
        else:
            loaded, report = load_corpus_source(kind, path, split)
        samples.extend(loaded)
        reports.append(report)

    assert_one_split_per_speaker(samples)
    assert_no_duplicate_speakers(reports)
    return samples, reports


def assert_one_split_per_speaker(samples: list[Sample]) -> None:
    """No speaker may appear under two splits inside one build.

    The registry makes this unreachable through the ordinary path, which is
    exactly why it is asserted rather than reasoned about: it is the backstop
    for a future source loader that derives a split from something other than
    the registry. One person is a handful of voices at most, so a build that
    both fits on and measures against them reports memorisation as detection.
    """
    splits: dict[str, set[str]] = defaultdict(set)
    for sample in samples:
        splits[sample.speaker].add(sample.split)
    crossing = {name: sorted(seen) for name, seen in splits.items() if len(seen) > 1}
    if crossing:
        raise Refused(
            f"these speakers appear under more than one split in one build: "
            f"{crossing}. A speaker's split assignment is immutable."
        )


def assert_no_duplicate_speakers(reports: list[dict]) -> None:
    """One speaker, one manifest, per build.

    Two derivations of one person would double their weight in the dataset
    while the per-speaker counts read as two speakers.
    """
    seen: dict[str, str] = {}
    for report in reports:
        if report["type"] != "human":
            continue
        speaker = report["speaker"]
        if speaker in seen:
            raise Refused(
                f"speaker {speaker} is supplied twice, by {seen[speaker]} and "
                f"{report['manifest_sha256'][:12]}. One speaker counted twice "
                "is one voice weighted as two."
            )
        seen[speaker] = report["manifest_sha256"][:12]


def is_synthetic(sample: Sample) -> str | None:
    """Why ``sample`` is synthetic, or None. Re-checked, never remembered.

    The final assertion runs this over what was actually emitted, so the
    zero-synthetic claim in the stats is a measurement of the dataset on disk
    rather than a restatement of the checks that were supposed to have run.

    The registry lookups are part of that measurement rather than a repeat of
    the loader's: ``synthetic_samples: 0`` is a claim about the emitted windows,
    and it should be false if a single one of them is content-addressed retired
    material by either its own bytes or the recording it names.
    """
    if sample.category in SYNTHETIC_CATEGORIES:
        return f"synthetic-era category {sample.category!r}"
    for digest, what in (
        (sample.sha256, "the clip"),
        (sample.source_sha256, "the recording it came from"),
    ):
        entry = retired_artifact(digest)
        if entry is not None:
            return (
                f"{what} is the retired synthetic artifact {entry['name']!r} "
                f"({entry['kind']}) by SHA-256 {digest}"
            )
    if not sample.source_file:
        return "no source recording"
    if not _hex64(sample.source_sha256):
        return "no verified hash for the source recording"
    if not _hex64(sample.sha256):
        return "no verified hash for the audio"
    if not sample.licence:
        return "no recorded licence"
    for value, what, marker in (
        (sample.source_file, "source_file", synthetic_source_marker(sample.source_file)),
        (sample.path, "path", synthetic_tree_marker(sample.path)),
    ):
        if marker is not None:
            return f"{what} {value} matches the synthetic marker {marker!r}"
    return None


def assert_real_provenance(samples: list[Sample]) -> None:
    """Every sample, or the build stops. Checked before a window is emitted."""
    offences = {str(s.path): why for s in samples if (why := is_synthetic(s))}
    if offences:
        listed = "\n  ".join(f"{path}: {why}" for path, why in sorted(offences.items()))
        raise Refused(
            "these samples cannot be shown to be real recordings:\n  " + listed
        )


def frame_count(path: Path) -> int:
    """Sample count from the WAV header, without reading the audio.

    A non-WAV file is a refusal with an instruction rather than a traceback:
    Common Voice ships MP3, and the decode to the pipeline's 16 kHz mono PCM is
    a separate step whose output is what gets frozen and built from.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes()
    except (wave.Error, EOFError) as exc:
        raise Refused(
            f"{path} is not a readable WAV ({exc}). This stage takes 16 kHz "
            "mono 16-bit PCM, the format the runtime's feature extractor is "
            "fed; decode the recordings to it and freeze that tree instead."
        ) from exc


def windows_for(sample: Sample, positive_windows: int, negative_windows: int) -> int:
    """How many windows one sample yields. Known before any audio is read.

    ``FeatureSink`` preallocates from the sum of these, refuses to grow, and
    refuses to finish short — so a term that disagrees with what the emitter
    does aborts the run instead of leaving zero-filled rows that train as
    silent negatives.

    A positive and a phrase-anchored negative (``PHRASE_ANCHORED_NEGATIVES``,
    the near-miss) both yield ``positive_windows`` windows — one per trailing
    offset. A continuous negative tiles, a count set by its own length and
    capped at ``negative_windows``. The emit loop in ``build_windows`` branches
    on the same predicate, so this term is exactly what it produces.
    """
    if sample.label == 1 or sample.category in PHRASE_ANCHORED_NEGATIVES:
        return positive_windows
    tiles = max(1, frame_count(sample.path) // WINDOW_SAMPLES)
    return min(negative_windows, tiles)


def place_at_end(audio: np.ndarray, trailing_s: float) -> np.ndarray:
    """The clip finishing ``trailing_s`` before the window's trailing edge.

    Deterministic trimming and padding: the clip is not resampled, filtered,
    levelled or mixed with anything. Audio longer than the window keeps its
    tail, because the end of the phrase is what the score is supposed to peak
    on.
    """
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
    end = WINDOW_SAMPLES - int(round(trailing_s * SAMPLE_RATE))
    clip = audio[-WINDOW_SAMPLES:]
    start = max(0, end - clip.size)
    window[start:end] = clip[-(end - start):]
    return window


def tile(audio: np.ndarray, index: int) -> np.ndarray:
    """The ``index``-th non-overlapping window of ``audio``, zero-padded.

    Non-overlapping and from the start, so a recording contributes each second
    of itself once. Overlapping tiles would put near-duplicates of one moment
    in the dataset and count them as independent negatives.
    """
    offset = index * WINDOW_SAMPLES
    chunk = audio[offset : offset + WINDOW_SAMPLES]
    if chunk.size == WINDOW_SAMPLES:
        return chunk.astype(np.float32)
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
    window[: chunk.size] = chunk
    return window


def build_windows(args: argparse.Namespace, sink) -> dict:
    """Emit every window for one split, then prove the dataset is human-only.

    Order of operations matters. Every source is verified before the sink is
    told a total, so a refusal costs nothing and cannot leave a half-written
    dataset; and the zero-synthetic assertion runs over the emitted records
    afterwards, so it describes the dataset rather than the intention.
    """
    if args.positive_windows < 1 or args.positive_windows > len(TRAILING_OFFSETS_S):
        raise Refused(
            f"--positive-windows must be 1..{len(TRAILING_OFFSETS_S)} (the "
            f"trailing-offset ladder is {TRAILING_OFFSETS_S}), got "
            f"{args.positive_windows}"
        )
    if args.negative_windows < 1:
        raise Refused(f"--negative-windows must be >= 1, got {args.negative_windows}")
    refuse_synthetic_tree(args.out, "--out")

    samples, reports = load_sources(args.source, args.split)
    assert_real_provenance(samples)

    planned = [windows_for(s, args.positive_windows, args.negative_windows) for s in samples]
    sink.begin(sum(planned))

    emitted: list[Sample] = []
    group_id = 0
    for sample, count in zip(samples, planned):
        audio = build_dataset.read_wav16(sample.path)
        # One group id per source recording, so every window derived from one
        # utterance stays on one side of any later split of this dataset.
        group_id += 1
        for index in range(count):
            if sample.label == 1 or sample.category in PHRASE_ANCHORED_NEGATIVES:
                window = place_at_end(audio, TRAILING_OFFSETS_S[index])
            else:
                window = tile(audio, index)
            record = {
                "source": sample.source_file,
                "clip": sample.path.name,
                "clip_sha256": sample.sha256,
                "source_sha256": sample.source_sha256,
                "source_type": sample.source_type,
                "speaker": sample.speaker,
                "split": sample.split,
                "category": sample.category,
                "label": sample.label,
                "licence": sample.licence,
                "manifest_sha256": sample.manifest_sha256,
                "window": index,
            }
            sink.add(
                (window * 32767.0).astype(np.int16),
                sample.label,
                group_id,
                sample.category,
                record,
            )
            emitted.append(sample)

    return summarise(args, sink, emitted, reports)


def summarise(
    args: argparse.Namespace, sink, emitted: list[Sample], reports: list[dict]
) -> dict:
    """The stats, including the zero-synthetic assertion computed from emission.

    Counted off ``emitted`` — one entry per window actually handed to the sink —
    rather than off the plan, so this cannot agree with the intention while
    disagreeing with the dataset.
    """
    if len(emitted) != len(sink.labels):
        raise Refused(
            f"{len(emitted)} provenance records for {len(sink.labels)} windows; "
            "the dataset and its provenance have drifted apart."
        )

    synthetic = {str(s.path): why for s in emitted if (why := is_synthetic(s))}
    if synthetic:
        listed = "\n  ".join(f"{path}: {why}" for path, why in sorted(synthetic.items()))
        raise Refused(
            f"{len(synthetic)} emitted window(s) are not real recordings:\n  "
            + listed
            + "\n\nRefusing to write a dataset that would be reported as "
            "human-only."
        )

    by_speaker: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    by_split: Counter[str] = Counter()
    windows_by_speaker: Counter[str] = Counter()
    for sample in dict.fromkeys(emitted):
        by_speaker[sample.speaker] += 1
        by_category[sample.category] += 1
        by_split[sample.split] += 1
    for sample in emitted:
        windows_by_speaker[sample.speaker] += 1

    contract = {
        "round": 8,
        "human_only": True,
        # Computed above from the emitted records; the assertion is that this
        # equals zero, and the number is what proves it was measured.
        "synthetic_samples": len(synthetic),
        "split": args.split,
        "sources": [
            {
                "type": report["type"],
                "manifest": report["manifest"],
                "manifest_sha256": report["manifest_sha256"],
            }
            for report in reports
        ],
    }
    return {
        "split": args.split,
        "round": 8,
        "human_only": True,
        "synthetic_samples": len(synthetic),
        "windows": len(sink.labels),
        "positives": int(sum(sink.labels)),
        "negatives": int(len(sink.labels) - sum(sink.labels)),
        "source_recordings": len(set(emitted)),
        "window_seconds": WINDOW_SAMPLES / SAMPLE_RATE,
        "positive_windows_per_clip": args.positive_windows,
        "negative_windows_per_clip": args.negative_windows,
        "trailing_offsets_s": list(TRAILING_OFFSETS_S),
        "samples_by_speaker": dict(sorted(by_speaker.items())),
        "samples_by_category": dict(sorted(by_category.items())),
        "samples_by_split": dict(sorted(by_split.items())),
        "windows_by_speaker": dict(sorted(windows_by_speaker.items())),
        "windows_by_category": {
            name: sink.categories.count(name) for name in sorted(set(sink.categories))
        },
        "sources": reports,
        "contract": contract,
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    refuse_prohibited_flags(argv)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        # Not `default=[]`: argparse's append action mutates the default object
        # itself, so a second call in one process would inherit the first call's
        # sources -- which in a sweep is one arm silently building another's data.
        default=None,
        metavar="TYPE=PATH",
        help=f"repeatable; TYPE is one of {list(SOURCE_TYPES)} and PATH is its "
             "frozen manifest",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--split", required=True, choices=sorted(BUILDABLE_SPLITS))
    parser.add_argument(
        "--positive-windows",
        type=int,
        default=len(TRAILING_OFFSETS_S),
        help=f"windows per positive clip, one per trailing offset in "
             f"{TRAILING_OFFSETS_S}",
    )
    parser.add_argument(
        "--negative-windows",
        type=int,
        default=1,
        help="at most this many non-overlapping windows per negative recording",
    )
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--chunk", type=int, default=2048)
    parser.add_argument("--ncpu", type=int, default=4)
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="also write the raw windows, for listening",
    )
    args = parser.parse_args(argv)
    args.source = list(args.source or [])

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"building the {args.split} split from real recordings only")
    sink = build_dataset.FeatureSink(
        args.out, args.split, args.keep_audio, args.chunk, args.batch_size, args.ncpu
    )
    stats = build_windows(args, sink)
    sink.finish()
    print_report(stats)

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
    (args.out / CONTRACT_FILENAME).write_text(
        json.dumps(stats["contract"], indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}/x_{args.split}.npy")
    return 0


def print_report(stats: dict) -> None:
    """Counts by speaker, by category and by split, on stdout.

    Printed as well as persisted because the shape of the dataset is the first
    thing that is wrong when a source was misconfigured, and nobody opens the
    stats file for a run they think succeeded.
    """
    print(f"  {stats['windows']} windows "
          f"({stats['positives']} positive / {stats['negatives']} negative)")
    print(f"  synthetic_samples: {stats['synthetic_samples']}")
    for label, key in (
        ("by speaker", "samples_by_speaker"),
        ("by category", "samples_by_category"),
        ("by split", "samples_by_split"),
    ):
        print(f"  source recordings {label}:")
        for name, count in stats[key].items():
            print(f"    {name:34s} {count:7d}")


if __name__ == "__main__":
    raise SystemExit(main())
