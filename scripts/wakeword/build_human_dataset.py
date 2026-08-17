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
  hash to the recorded digest.

A category rename cannot buy admission. ``positive_human`` is checked for
provenance exactly as ``positive`` is refused by name: the guard that stops a
synthetic clip is the hash chain back to a real recording, not the string.

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
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import assets  # noqa: E402
import build_dataset  # noqa: E402
import freeze_manifest  # noqa: E402

WINDOW_SAMPLES = build_dataset.WINDOW_SAMPLES
SAMPLE_RATE = build_dataset.SAMPLE_RATE

#: Speaker -> the split that speaker is permanently bound to. Immutable: a
#: speaker who has ever been fitted on cannot become a measurement, and a
#: speaker held back to measure with is worth exactly its unseenness.
#:
#: An explicit table rather than a rule over filenames. A rule would assign a
#: split to a speaker nobody has decided about yet, which is how the one
#: unheard voice gets trained on by a run that looked correct.
SPEAKER_SPLITS: dict[str, str] = {
    "E001": "train",
    "E002": "eval_sealed",
    "E003": "train",
    "E004": "train",
    "E005": "validation",
    "E006": "qualification_sealed",
    "E007": "qualification_sealed",
}

#: Registry splits whose speakers are sealed. Nothing fitted on, nothing
#: threshold-selected on, nothing mined for hard negatives.
SEALED_SPLITS = frozenset({"eval_sealed", "qualification_sealed"})

#: ``--split`` -> the one registry split whose speakers it may ingest.
#:
#: ``eval_sealed`` is deliberately not a value here. E002 is the sealed
#: evaluation speaker and this stage cannot build it into any tensor at all;
#: it is measured by streaming its audio through the runtime engine in
#: ``evaluate_model.py``, which is the only use that does not spend it.
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
#: per-category tables ``evaluate_model.py`` prints. The derivation must emit
#: these exact strings; see MANIFEST_CATEGORY_NOTE.
HUMAN_CATEGORIES: dict[str, int] = {
    "positive_human": 1,
    "near_phrase_human": 0,
    "free_speech_human": 0,
}

#: What to tell an operator whose manifest still carries the derivation's
#: original category names.
MANIFEST_CATEGORY_NOTE = (
    "Round 8 manifests must declare the '_human' category names "
    f"({sorted(HUMAN_CATEGORIES)}); re-derive the speaker rather than editing "
    "the manifest, because the manifest digest is what a candidate is traced to."
)

#: Category names from the synthetic era. Every one of these is a hard error
#: wherever it is declared, including inside an otherwise valid human manifest:
#: ``near_phrase`` and ``positive`` are what the TTS groups were called, so a
#: row carrying one is either a synthetic clip or a manifest written by a tool
#: that does not know this contract. Neither is ingestible.
SYNTHETIC_CATEGORIES = frozenset(
    {
        "positive",
        "near_phrase",
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

#: Trailing context, in seconds, for a positive window: how much audio follows
#: the end of the clip inside the window.
#:
#: A fixed ladder, not a draw. The engine fires only after three consecutive
#: frames over threshold, so a phrase that finishes exactly at the window edge
#: scores high on one frame and never wakes; two to eight frames of trailing
#: context is the plateau the confirmation rule needs. Enumerating the ladder
#: gives the same coverage as ``build_dataset``'s jitter while staying inside
#: deterministic window extraction — nothing here draws a number.
TRAILING_OFFSETS_S: tuple[float, ...] = (0.16, 0.32, 0.48, 0.64)

#: Written beside the tensors, and required beside any checkpoint this phase is
#: allowed to initialise from.
CONTRACT_FILENAME = "DATASET_CONTRACT.json"

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
    """
    _refuse(path, synthetic_tree_marker(path), what)


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


def refuse_synthetic_initialization(checkpoint: Path) -> None:
    """Refuse a checkpoint that is not provably from a human-only dataset.

    A synthetic candidate used as a starting point is a synthetic model with
    some real data fine-tuned onto it, which is the thing that is retired — and
    it is invisible afterwards, because the artifact looks like any other.

    Path shape alone cannot decide this, so the test is evidence: the checkpoint
    must sit beside the ``DATASET_CONTRACT.json`` that this stage writes, and
    that contract must assert zero synthetic samples. Absence is a refusal, not
    a benefit of the doubt.
    """
    refuse_synthetic_tree(checkpoint, "initialization checkpoint")
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
    except (OSError, json.JSONDecodeError) as exc:
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


def _hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def _read_manifest(path: Path, what: str) -> tuple[dict, str]:
    """Parse a manifest and return it with the SHA-256 of its bytes."""
    refuse_synthetic_tree(path, f"{what} manifest")
    if not path.is_file():
        raise Refused(f"{what} manifest {path} does not exist")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
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

    Two independent failures. A synthetic-era name is refused outright, because
    those are what the TTS groups were called and no real derivation emits one.
    A label that disagrees with the category table is refused too: the label is
    what training optimises, and a near miss carrying label 1 is a lesson to
    fire on the speaker's ordinary speech.
    """
    if not isinstance(category, str) or not category:
        raise Refused(f"{where} declares no category")
    if category in SYNTHETIC_CATEGORIES:
        raise Refused(
            f"{where} declares the synthetic-era category {category!r}, which "
            f"is refused wherever it appears. {MANIFEST_CATEGORY_NOTE}"
        )
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
    counts = {"originals_verified": 0, "originals_offline": 0}
    for name, entry in sorted(index.items()):
        original = root / name if root is not None else None
        if original is not None and original.is_file():
            actual = build_dataset.sha256_file(original)
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
        decoded = path.parent / relative
        if not decoded.is_file():
            raise Refused(
                f"{path}: {relative} is missing, so original {name} cannot be "
                "verified at either layer."
            )
        actual = build_dataset.sha256_file(decoded)
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
        audio = path.parent / relative
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
        actual = build_dataset.sha256_file(audio)
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
    for entry in entries:
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not _hex64(digest):
            raise Refused(
                f"{path}: a file entry records path={relative!r} "
                f"sha256={digest!r}; both are required and the hash must be one."
            )
        name = Path(relative).name
        if kind == "recorded_background" and name in GENERATED_BACKGROUND_NAMES:
            # Named, counted and excluded rather than quietly filtered: two of
            # Speech Commands' six background files are synthesised noise, and
            # generated background is prohibited exactly as generated speech is.
            generated.append(relative)
            continue
        refuse_synthetic_source(relative, f"{path}: {kind} file")
        audio = root / relative
        if not audio.is_file():
            raise Refused(f"{path}: {relative} is listed but missing under {root}")
        actual = build_dataset.sha256_file(audio)
        if actual != digest:
            raise Refused(
                f"{path}: {relative} hashes to {actual}, not the frozen "
                f"{digest}. Refusing to build from a corpus that has drifted "
                "from the manifest measurements refer to."
            )
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
    candidates = [path.parent / name]
    if path.parent.name == name:
        candidates.append(path.parent)
    for candidate in candidates:
        if candidate.is_dir():
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
    """
    if sample.category in SYNTHETIC_CATEGORIES:
        return f"synthetic-era category {sample.category!r}"
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
    """
    if sample.label == 1:
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
            if sample.label == 1:
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
