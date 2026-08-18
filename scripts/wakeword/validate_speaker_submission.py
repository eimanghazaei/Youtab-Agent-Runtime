#!/usr/bin/env python3
"""Check one speaker's submitted folder against the recording package.

``SPEAKER_RECORDING_PACKAGE.md`` tells a speaker exactly what files to produce
and what to name them, and says plainly that our loader refuses to guess a
label from an unrecognised file. This is that check, run on the assembled
folder while the person who can still fix it is reachable -- instead of weeks
later, when five people's folders are being assembled into one training set
and nobody remembers whose third take of "hey utah" a stray file was.

What it validates is the layout the package mandates, which is identical for
every speaker in the round::

    E003/
      originals/                 the recording tree, exactly as the recorder wrote it
      RECORDING_METADATA.json    the device and environment form (diagnostic only)
      SHA256SUMS                 coreutils-format digest of every other file, from the coordinator

Three entries, and any other is an error rather than a skip. Refusing an
unrecognised file is the same rule ingestion follows: a file whose label
nobody can state is not a file with an unknown label, it is a file that must
not enter a dataset. Authorization to use the recordings is not a file in this
folder: it is the project-level registry fact (membership of
``speaker_recording_spec.SPEAKER_ASSIGNMENTS``), so no consent document is
required, checked or hashed here.

It checks structure and naming only, and deliberately never opens an audio
file's payload. Duration and clipping are for the speaker's own recorder to
show and the checklist in SPEAKER_RECORDING_PACKAGE.md to prompt about; this
round's whole premise is that a raw recording is never trimmed, converted or
otherwise touched before real labelling and exclusion decisions are made on
our side, and a validator that inspects audio content would be a first step
down that road. ``SHA256SUMS`` is checked as a *listing* -- well-formed lines
covering every file exactly once -- and not by recomputing digests: a digest
recomputed on the machine that wrote it says nothing about the transfer that
has not happened yet. ``sha256sum -c SHA256SUMS``, run by the coordinator
after the transfer, is what says that.

Run with no flags, this is the upload-verification command a speaker or their
coordinator runs before handing a folder over. Its output has two parts: every
problem found (as before), and a completion summary -- one line per section
showing how many takes were accepted against how many are required, so a
shortfall reads as numbers to go back and record rather than a wall of error
text.

Run with ``--rename plan`` or ``--rename apply``, it does something different:
it mechanically renames whatever a recorder app called its files into the
exact names this package specifies, using the order the files were recorded in
(their modification time) rather than asking anyone to type a slug. See
``rename_plan()`` below for how that ordering is decided and where it refuses
to guess.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import speaker_recording_spec as spec  # noqa: E402

_TAKE_PATTERN = rf"[0-9]{{{spec.TAKE_DIGITS}}}"
_SLUG_PATTERN = r"[a-z0-9-]+"


@dataclass
class SectionStatus:
    """One accepted/required count -- one line of the completion summary.

    Counts, not a pass/fail flag: "3 of 5 near-phrase takes" tells a speaker
    which two to go back and record, where a bare "incomplete" would not.
    """

    label: str
    have: int
    need: int

    @property
    def complete(self) -> bool:
        return self.have >= self.need


@dataclass
class ValidationResult:
    """Every problem found, collected rather than stopping at the first.

    A speaker (or their coordinator) fixing a folder needs the whole list in
    one pass, not one error per run -- re-running after every single fix is
    exactly the friction that turns "record it again" into "delete the hard
    one and hope nobody notices".

    ``sections`` is the same walk, kept as counts instead of sentences, so it
    can drive a completion summary (``format_completion_summary``) alongside
    the error list rather than instead of it.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sections: list[SectionStatus] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _extension_ok(name: str) -> bool:
    return Path(name).suffix.lower() in spec.AUDIO_EXTENSIONS


def _ingestible_extensions() -> frozenset[str]:
    """Extensions ingestion can actually read, from ``import_speaker``'s own table.

    Derived from ``import_speaker.CONTAINER_EXTENSIONS`` -- the container
    families that tool dispatches on by magic bytes -- rather than kept as a
    second copy here, so the format a speaker is held to at handover cannot
    drift from the format the loader will accept. Imported inside the function
    on purpose: ``import_speaker`` imports this module at its own top, and
    reading its table at *our* import time would read a half-initialised module
    mid-cycle.
    """
    import import_speaker

    return frozenset(
        ext for exts in import_speaker.CONTAINER_EXTENSIONS.values() for ext in exts
    )


def _unreadable_format_error(name: str) -> str | None:
    """A refusal for a real audio file in a format ingestion cannot read, else ``None``.

    ``spec.AUDIO_EXTENSIONS`` exists only to catch a name with no audio
    extension at all, and its own docstring says it is not a format gate. The
    format gate is here, and it is derived from what ingestion actually reads: a
    file whose extension is a genuine recorder format (``.opus``, ``.ogg``,
    ``.amr``, ``.mp3``, ``.webm``) that ``import_speaker`` has no parser for is
    refused locally, now, with the speaker still at the microphone and able to
    change the recorder's setting and record again -- rather than weeks later,
    when the loader rejects the folder and nobody can ask the speaker to redo a
    session they have long since finished. A hard error, not a warning: an
    unreadable take cannot enter the dataset, and pretending otherwise defers
    the same refusal to a point where it can no longer be fixed.
    """
    suffix = Path(name).suffix.lower()
    if suffix not in spec.AUDIO_EXTENSIONS:
        return None  # not an audio extension at all -- the naming check reports it
    if suffix in _ingestible_extensions():
        return None  # a container the loader can read
    return (
        f"its {suffix} format is one some recorder apps write but ingestion cannot "
        "read, and nothing here converts a recording to another format. Set the "
        "recording app to record in M4A/AAC (or WAV) and record this again before "
        f"handing the folder over -- a {suffix} file is a re-record, not an ingest"
    )


def _assigned_labels() -> tuple[str, ...]:
    return tuple(a.label for a in spec.SPEAKER_ASSIGNMENTS)


def _check_condition_dir(
    originals: Path,
    section: spec.PositiveSection,
    result: ValidationResult,
) -> None:
    """A folder holding exactly one ``<slug>_<condition>_NNN`` series."""
    directory = originals / section.directory
    rel = f"{spec.ORIGINALS_DIR}/{section.directory}"
    if not directory.is_dir():
        result.errors.append(f"missing required folder: {rel}/")
        result.sections.append(SectionStatus(rel, 0, section.takes))
        return

    pattern = re.compile(
        rf"^{re.escape(spec.WAKE_PHRASE_SLUG)}_{re.escape(section.condition)}_{_TAKE_PATTERN}$"
    )
    matched = 0
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            result.errors.append(f"{rel}/{entry.name}: expected a file, found a directory")
            continue
        fmt_error = _unreadable_format_error(entry.name)
        if fmt_error is not None:
            result.errors.append(f"{rel}/{entry.name}: {fmt_error}")
            continue
        if not pattern.match(entry.stem) or not _extension_ok(entry.name):
            result.errors.append(
                f"{rel}/{entry.name}: unrecognised file name; expected "
                f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_NNN with a recorder "
                "audio extension"
            )
            continue
        matched += 1

    if matched < section.takes:
        result.errors.append(
            f"{rel}/: found {matched} correctly named recording(s), need at least {section.takes}"
        )
    result.sections.append(SectionStatus(rel, matched, section.takes))


def _check_near_phrase_dir(originals: Path, result: ValidationResult) -> None:
    directory = originals / "near_phrase"
    rel = f"{spec.ORIGINALS_DIR}/near_phrase"
    if not directory.is_dir():
        result.errors.append(f"missing required folder: {rel}/")
        for item in spec.NEAR_PHRASE_ITEMS:
            result.sections.append(SectionStatus(f"{rel}/{item.slug}", 0, item.takes))
        return

    known = {item.slug: item for item in spec.NEAR_PHRASE_ITEMS}
    counts: dict[str, int] = dict.fromkeys(known, 0)
    pattern = re.compile(rf"^({_SLUG_PATTERN})_({_TAKE_PATTERN})$")

    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            result.errors.append(f"{rel}/{entry.name}: expected a file, found a directory")
            continue
        fmt_error = _unreadable_format_error(entry.name)
        if fmt_error is not None:
            result.errors.append(f"{rel}/{entry.name}: {fmt_error}")
            continue
        match = pattern.match(entry.stem)
        if not match or not _extension_ok(entry.name):
            result.errors.append(
                f"{rel}/{entry.name}: unrecognised file name; expected "
                "<phrase-slug>_NNN with a recorder audio extension"
            )
            continue
        slug = match.group(1)
        if slug not in known:
            result.errors.append(
                f"{rel}/{entry.name}: {slug!r} is not one of the near-phrase "
                "slugs in SPEAKER_RECORDING_PACKAGE.md -- this loader refuses to "
                "guess which phrase a file belongs to"
            )
            continue
        counts[slug] += 1

    for slug, item in known.items():
        if counts[slug] < item.takes:
            result.errors.append(
                f"{rel}/: only {counts[slug]} take(s) of {item.text!r} ({slug}), "
                f"need at least {item.takes}"
            )
        result.sections.append(SectionStatus(f"{rel}/{slug}", counts[slug], item.takes))


def _check_freeform_dir(
    originals: Path, section: spec.FreeformSection, result: ValidationResult
) -> None:
    """A folder holding one or more continuous ``<prefix>_NNN`` takes."""
    directory = originals / section.directory
    rel = f"{spec.ORIGINALS_DIR}/{section.directory}"
    if not directory.is_dir():
        result.errors.append(f"missing required folder: {rel}/")
        result.sections.append(SectionStatus(rel, 0, section.min_files))
        return

    pattern = re.compile(rf"^{re.escape(section.prefix)}_{_TAKE_PATTERN}$")
    matched = 0
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            result.errors.append(f"{rel}/{entry.name}: expected a file, found a directory")
            continue
        fmt_error = _unreadable_format_error(entry.name)
        if fmt_error is not None:
            result.errors.append(f"{rel}/{entry.name}: {fmt_error}")
            continue
        if not pattern.match(entry.stem) or not _extension_ok(entry.name):
            result.errors.append(
                f"{rel}/{entry.name}: unrecognised file name; expected "
                f"{section.prefix}_NNN with a recorder audio extension"
            )
            continue
        matched += 1

    if matched < section.min_files:
        result.errors.append(
            f"{rel}/: found {matched} recording(s), need at least {section.min_files}"
        )
    result.sections.append(SectionStatus(rel, matched, section.min_files))


def _check_metadata(root: Path, result: ValidationResult) -> None:
    """Read the device/environment form for diagnostics -- never a gate.

    The form is recorded when present because it describes the recording
    environment, but a missing, partial, blank or malformed
    ``RECORDING_METADATA.json`` is not an error and never blocks acceptance:
    acceptance depends on audio, labels, role separation and checksums, not on
    paperwork. The count of filled diagnostic fields is surfaced as a section
    for the operator's information only. speaker_id and noise-source values are
    the registry's and the audio's to decide, not the form's, so a form that
    disagrees is not treated as authoritative and does not block. This function
    appends nothing to ``result.errors``.
    """
    path = root / spec.METADATA_FILE
    needed = len(spec.DIAGNOSTIC_METADATA_FIELDS)
    label = f"{spec.METADATA_FILE} fields (diagnostic)"

    data: object = None
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None  # a present-but-unreadable form is diagnostic-only

    if not isinstance(data, dict):
        result.sections.append(SectionStatus(label, 0, needed))
        return

    filled = 0
    for name in spec.DIAGNOSTIC_METADATA_FIELDS:
        value = data.get(name)
        if name == "noise_sources_used":
            if isinstance(value, list) and value:
                filled += 1
            continue
        if isinstance(value, str) and value.strip() and not value.strip().startswith("<"):
            filled += 1
    result.sections.append(SectionStatus(label, filled, needed))


def _submission_files(root: Path) -> list[str]:
    """Every file in the submission except ``SHA256SUMS``, POSIX-relative.

    The Owner's private documents (``PRIVATE_DOC_NAMES``) are excluded: the
    pipeline never hashes or lists them, so a stray ``CONSENT.pdf`` is tolerated
    without ever reaching the manifest or being flagged as unlisted.
    """
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == spec.CHECKSUM_FILE:
            continue
        if path.name in spec.PRIVATE_DOC_NAMES:
            continue
        out.append(rel)
    return out


def _check_checksums(root: Path, result: ValidationResult) -> None:
    """``SHA256SUMS`` must list every other file in the folder exactly once."""
    path = root / spec.CHECKSUM_FILE
    if not path.is_file():
        result.errors.append(
            f"missing {spec.CHECKSUM_FILE}: without it the folder cannot be shown to have "
            "survived the transfer intact"
        )
        result.sections.append(
            SectionStatus(spec.CHECKSUM_FILE, 0, len(_submission_files(root)))
        )
        return

    listed: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        match = spec.CHECKSUM_LINE.match(line)
        if not match:
            result.errors.append(
                f"{spec.CHECKSUM_FILE} line {number} is not coreutils format "
                "(64 lowercase hex digits, two spaces, then the path): "
                f"{line.strip()!r}"
            )
            continue
        listed.append(match.group(2))

    duplicates = sorted({name for name in listed if listed.count(name) > 1})
    for name in duplicates:
        result.errors.append(f"{spec.CHECKSUM_FILE} lists {name!r} more than once")

    present = set(_submission_files(root))
    listed_set = set(listed)
    for name in sorted(present - listed_set):
        result.errors.append(f"{spec.CHECKSUM_FILE} does not list {name!r}")
    for name in sorted(listed_set - present):
        result.errors.append(
            f"{spec.CHECKSUM_FILE} lists {name!r}, which is not in the folder"
        )
    result.sections.append(
        SectionStatus(spec.CHECKSUM_FILE, len(present & listed_set), len(present))
    )


def write_checksums(root: Path) -> int:
    """Write ``root/SHA256SUMS`` over every other file, in coreutils format.

    Returns how many files were listed. The listing is built from the same walk
    ``_check_checksums`` verifies against (``_submission_files``), so what this
    writes and what the validator later reads cannot disagree -- the failure a
    hand-typed ``find | xargs sha256sum`` invites is skipping a dotfile the walk
    counts, or counting one it skips, and then the folder fails its own listing.
    Portable where a raw ``find -printf`` is not: BSD/macOS ``find`` has no
    ``-printf``, and this is the machine a drive handed between two people is
    plugged into.

    This is *not* the transfer check. A digest taken here, on the machine that
    holds the files, says nothing about a copy that has not happened yet, which
    is exactly why ``_check_checksums`` refuses to recompute one. ``sha256sum -c
    SHA256SUMS``, run by the coordinator after the transfer, is what says the
    bytes arrived intact.
    """
    lines = [
        f"{hashlib.sha256((root / rel).read_bytes()).hexdigest()}  {rel}\n"
        for rel in _submission_files(root)
    ]
    (root / spec.CHECKSUM_FILE).write_text("".join(lines), encoding="utf-8")
    return len(lines)


def _check_originals(root: Path, result: ValidationResult) -> None:
    originals = root / spec.ORIGINALS_DIR
    if not originals.is_dir():
        result.errors.append(f"missing required folder: {spec.ORIGINALS_DIR}/")
        return

    for section in spec.POSITIVE_SECTIONS:
        _check_condition_dir(originals, section, result)

    present_noise_sources = [
        source
        for source in sorted(spec.NOISE_SOURCE_VOCAB)
        if (originals / f"positive_noise_{source}").is_dir()
    ]
    if len(present_noise_sources) < spec.MIN_NOISE_SOURCES:
        result.errors.append(
            f"found only {len(present_noise_sources)} {spec.ORIGINALS_DIR}/positive_noise_* "
            f"folder(s), need at least {spec.MIN_NOISE_SOURCES} of "
            f"{sorted(spec.NOISE_SOURCE_VOCAB)}"
        )
    for source in present_noise_sources:
        _check_condition_dir(originals, spec.noise_section(source), result)

    _check_near_phrase_dir(originals, result)
    for section in spec.FREEFORM_SECTIONS:
        _check_freeform_dir(originals, section, result)

    known = {
        *(section.directory for section in spec.POSITIVE_SECTIONS),
        *(f"positive_noise_{source}" for source in spec.NOISE_SOURCE_VOCAB),
        "near_phrase",
        *(section.directory for section in spec.FREEFORM_SECTIONS),
    }
    for entry in sorted(originals.iterdir()):
        if entry.name not in known:
            result.errors.append(
                f"unrecognised entry in {spec.ORIGINALS_DIR}/: {entry.name} -- ingestion "
                "refuses to guess what a file or folder it does not know is for"
            )


def validate_speaker_directory(root: Path) -> ValidationResult:
    """Check ``root`` -- one speaker's submission folder -- against the spec."""
    result = ValidationResult()
    if not root.is_dir():
        result.errors.append(f"{root} is not a directory")
        return result

    if not spec.SPEAKER_ID_PATTERN.match(root.name):
        result.errors.append(
            f"the submission folder is named {root.name!r}; it has to be the speaker "
            "label alone (E0nn), because that label is the only identifier allowed to "
            "travel with the audio"
        )

    # PRIVATE_DOC_NAMES (e.g. CONSENT.pdf) are tolerated so a stray private
    # document does not become an "unrecognised entry" error. They are never
    # ingested, hashed or reported -- their presence is simply not an error.
    tolerated_entries = set(spec.SUBMISSION_ENTRIES) | set(spec.PRIVATE_DOC_NAMES)
    for entry in sorted(root.iterdir()):
        if entry.name not in tolerated_entries:
            result.errors.append(
                f"unrecognised entry in {root.name}/: {entry.name} -- the submission holds "
                f"exactly {', '.join(spec.SUBMISSION_ENTRIES)} and nothing else"
            )

    _check_metadata(root, result)
    _check_originals(root, result)
    _check_checksums(root, result)
    return result


# ── completion summary ───────────────────────────────────────────────────────


def format_completion_summary(root: Path, result: ValidationResult) -> str:
    """A per-section accepted/required breakdown -- the completion summary.

    ``result.errors`` already says what is wrong, as sentences; this turns the
    same walk into counts, because "3 problems found" does not tell a speaker
    which three takes to go back and record, and a coordinator deciding
    whether five folders are ready to merge needs the shape of what is short
    at a glance rather than a wall of error text.
    """
    lines = [f"Completion summary for {root.name}:"]
    if not result.sections:
        lines.append("  (nothing could be counted -- see the errors above)")
        return "\n".join(lines)

    for section in sorted(result.sections, key=lambda item: item.label):
        marker = "ok" if section.complete else "MISSING"
        lines.append(f"  {section.label:<48} {section.have:>3}/{section.need:<3}  {marker}")

    have = sum(section.have for section in result.sections)
    need = sum(section.need for section in result.sections)
    lines.append(f"  {'TOTAL':<48} {have:>3}/{need:<3}")
    return "\n".join(lines)


# ── automatic renaming ───────────────────────────────────────────────────────


@dataclass
class RenameAction:
    """One file, and the canonical name it should have.

    ``changed`` is false for a file that is already correctly named, so a plan
    or an apply only ever reports the files that actually need to move.
    """

    old_path: Path
    new_name: str

    @property
    def changed(self) -> bool:
        return self.old_path.name != self.new_name


#: Sync-client and editor litter, by the same list freeze_manifest skips and
#: import_speaker records as litter, so what is given a take number here and
#: what is copied there cannot disagree.
LITTER_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", spec.CHECKSUM_FILE})


def _partition(directory: Path) -> tuple[list[Path], list[str], list[str]]:
    """Recordings, recognised litter, and files nobody here can classify.

    A take number is a claim about *which* recording a file is, and auto-naming
    assigns it by position. So a file that is not a recording does not merely
    acquire a canonical name of its own: it shifts every real take onto the
    wrong take number -- and the rename is in place, on originals that cannot be
    recorded again, with the recorder own names gone afterwards and no record of
    them anywhere.

    This is the normal case rather than the exotic one. macOS writes
    .DS_Store into every folder it opens and an AppleDouble ._name.m4a
    beside every file it copies onto exFAT, which is how a removable drive
    handed between two people is formatted.
    """
    recordings: list[Path] = []
    litter: list[str] = []
    strangers: list[str] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        if entry.name in LITTER_NAMES or entry.name.startswith("."):
            litter.append(entry.name)
        elif entry.suffix.lower() in spec.AUDIO_EXTENSIONS:
            recordings.append(entry)
        else:
            strangers.append(entry.name)
    return recordings, litter, strangers


def _ordered_files(directory: Path) -> list[Path]:
    """Recordings directly inside directory, oldest recording first.

    Sorted by modification time -- the order a recorder actually wrote them in
    -- with the original filename as a tie-breaker, so the order stays the
    same across repeated runs even if a transfer flattens timestamps to the
    same second.
    """
    return sorted(
        _partition(directory)[0],
        key=lambda entry: (entry.stat().st_mtime, entry.name),
    )


def _plan_for_directory(
    directory: Path, rel: str, stems: tuple[str, ...], result: ValidationResult
) -> list[RenameAction]:
    """The rename for one folder, or an error if its file count cannot be trusted.

    A count that does not match what the folder requires is refused rather
    than guessed at: renaming file 4 of 3 would assign a take number to a file
    that should not be in this folder at all -- the same "never guess a label"
    rule ``validate_speaker_directory`` already follows for a misnamed file.
    """
    _recordings, litter, strangers = _partition(directory)
    if strangers:
        result.errors.append(
            f"{rel}/: {sorted(strangers)} is not a recorder audio file "
            f"({sorted(spec.AUDIO_EXTENSIONS)}) and is not recognised litter. "
            "Auto-naming hands out take numbers by position, so a file nobody can "
            "classify pushes every real take onto the wrong number -- and the "
            "rename is in place, on recordings that cannot be made again. Remove "
            "it, or name the folder by hand"
        )
        return []
    if litter:
        result.warnings.append(f"{rel}/: ignoring {sorted(litter)} when numbering takes")
    files = _ordered_files(directory)
    if len(files) != len(stems):
        result.errors.append(
            f"{rel}/: {len(files)} file(s) found but {len(stems)} expected -- record "
            "the missing take(s) (or remove the extra file) before auto-naming; this "
            "will not guess which file is which"
        )
        return []
    return [RenameAction(path, f"{stem}{path.suffix}") for path, stem in zip(files, stems)]


def rename_plan(root: Path) -> tuple[list[RenameAction], ValidationResult]:
    """The mechanical rename every ``originals/`` folder needs, from recording order.

    Every file is renamed from whatever the recorder called it into the exact
    name ``SPEAKER_RECORDING_PACKAGE.md`` specifies, using the order it was
    recorded in -- file modification time -- rather than asking anyone to type
    a slug. A folder whose file count does not match what it requires is
    reported in the returned ``ValidationResult`` and left untouched; a folder
    that does not exist yet is silently skipped (``validate_speaker_directory``
    is what reports a missing folder).
    """
    result = ValidationResult()
    if not root.is_dir():
        result.errors.append(f"{root} is not a directory")
        return [], result

    originals = root / spec.ORIGINALS_DIR
    if not originals.is_dir():
        result.errors.append(f"missing required folder: {spec.ORIGINALS_DIR}/")
        return [], result

    actions: list[RenameAction] = []

    for section in spec.POSITIVE_SECTIONS:
        directory = originals / section.directory
        if directory.is_dir():
            rel = f"{spec.ORIGINALS_DIR}/{section.directory}"
            stems = spec.expected_stems_for_positive_section(section)
            actions += _plan_for_directory(directory, rel, stems, result)

    for source in sorted(spec.NOISE_SOURCE_VOCAB):
        section = spec.noise_section(source)
        directory = originals / section.directory
        if directory.is_dir():
            rel = f"{spec.ORIGINALS_DIR}/{section.directory}"
            stems = spec.expected_stems_for_positive_section(section)
            actions += _plan_for_directory(directory, rel, stems, result)

    near_phrase_dir = originals / "near_phrase"
    if near_phrase_dir.is_dir():
        stems = spec.expected_stems_for_near_phrase()
        actions += _plan_for_directory(
            near_phrase_dir, f"{spec.ORIGINALS_DIR}/near_phrase", stems, result
        )

    for section in spec.FREEFORM_SECTIONS:
        directory = originals / section.directory
        if directory.is_dir():
            # The same partition as every other section. Here the count decides
            # how many stems exist, so a counted sync artefact does not merely
            # take a slot from a real take -- it guarantees one.
            count = len(_partition(directory)[0])
            if count >= 1:
                rel = f"{spec.ORIGINALS_DIR}/{section.directory}"
                stems = spec.expected_stems_for_freeform(section, count)
                actions += _plan_for_directory(directory, rel, stems, result)

    return actions, result


def apply_rename_plan(actions: list[RenameAction]) -> int:
    """Perform every rename that actually changes a name. Returns how many moved.

    Two passes, through a temporary name first: a folder that is already
    partly canonical (take 3 needs to become take 1, say) would otherwise risk
    two files colliding on the same name mid-rename.
    """
    changed = [action for action in actions if action.changed]
    staged: list[tuple[Path, Path]] = []
    for index, action in enumerate(changed):
        temp = action.old_path.with_name(f".rename-tmp-{index}{action.old_path.suffix}")
        action.old_path.rename(temp)
        staged.append((temp, action.old_path.parent / action.new_name))
    for temp, final in staged:
        temp.rename(final)
    return len(changed)


# ── the command line ─────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "speaker_dir", type=Path, help="one assembled speaker folder, e.g. E003/"
    )
    parser.add_argument(
        "--rename",
        choices=("plan", "apply"),
        default=None,
        help=(
            "instead of validating, mechanically rename every file under "
            "originals/ from recording order (oldest first) to the exact name "
            "SPEAKER_RECORDING_PACKAGE.md specifies -- no slug typed by hand. "
            "'plan' prints what would change without touching a file; 'apply' "
            "renames. Run the command again with no --rename afterwards to validate."
        ),
    )
    parser.add_argument(
        "--write-checksums",
        action="store_true",
        help=(
            "instead of validating, write SHA256SUMS over every other file in the "
            "folder, in the coreutils format the validator and sha256sum -c read. "
            "Run it once the files are named, and never as the transfer check -- "
            "that is sha256sum -c after the copy."
        ),
    )
    args = parser.parse_args(argv)

    if args.write_checksums:
        count = write_checksums(args.speaker_dir)
        print(f"wrote {spec.CHECKSUM_FILE} covering {count} file(s)")
        return 0

    if args.rename:
        actions, result = rename_plan(args.speaker_dir)
        for error in result.errors:
            print(f"ERROR: {error}")
        changed = [action for action in actions if action.changed]
        if args.rename == "plan":
            for action in changed:
                print(f"RENAME: {action.old_path.name} -> {action.new_name}")
            if not changed:
                print("nothing to rename -- every file already has its canonical name")
        else:
            count = apply_rename_plan(actions)
            print(f"renamed {count} file(s)")
        if result.errors:
            print(f"{args.speaker_dir}: {len(result.errors)} folder(s) could not be auto-named")
            return 1
        return 0

    result = validate_speaker_directory(args.speaker_dir)
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    for error in result.errors:
        print(f"ERROR: {error}")
    print(format_completion_summary(args.speaker_dir, result))

    if result.ok:
        print(f"{args.speaker_dir}: all required sections present and well-named")
        return 0
    print(f"{args.speaker_dir}: {len(result.errors)} problem(s) found")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
