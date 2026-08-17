#!/usr/bin/env python3
"""Check a submitted speaker recording folder before it is handed over.

``SPEAKER_RECORDING_PACKAGE.md`` tells a speaker exactly what files to
produce and what to name them, and says plainly that our loader refuses to
guess a label from an unrecognized file. This is that check, run before a
folder leaves the speaker's machine rather than after: it catches a missing
section, a mistyped folder name, or a file a recorder app auto-renamed with
its own counter (many do) while the person who can still fix it is in the
room -- instead of weeks later, when several people's folders are being
assembled into one training set and nobody remembers whose third take of
"hey utah" a stray file was.

It checks structure and naming only, and deliberately never opens an audio
file's payload. Duration and clipping are for the speaker's own recorder to
show and the checklist in SPEAKER_RECORDING_PACKAGE.md to prompt about; this
round's whole premise is that a raw recording is never trimmed, converted or
otherwise touched before real labelling and exclusion decisions are made on
our side, and a validator that inspects audio content would be a first step
down that road.
"""

from __future__ import annotations

import argparse
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
class ValidationResult:
    """Every problem found, collected rather than stopping at the first.

    A speaker (or their coordinator) fixing a folder needs the whole list in
    one pass, not one error per run -- re-running after every single fix is
    exactly the friction that turns "record it again" into "delete the hard
    one and hope nobody notices".
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _extension_ok(name: str) -> bool:
    return Path(name).suffix.lower() in spec.AUDIO_EXTENSIONS


def _check_condition_dir(
    root: Path,
    dirname: str,
    slug: str,
    condition: str,
    min_reps: int,
    result: ValidationResult,
) -> None:
    """A folder holding exactly one ``<slug>_<condition>_NNN`` series."""
    directory = root / dirname
    if not directory.is_dir():
        result.errors.append(f"missing required folder: {dirname}/")
        return

    pattern = re.compile(rf"^{re.escape(slug)}_{re.escape(condition)}_{_TAKE_PATTERN}$")
    matched = 0
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            result.errors.append(f"{dirname}/{entry.name}: expected a file, found a directory")
            continue
        if not pattern.match(entry.stem) or not _extension_ok(entry.name):
            result.errors.append(
                f"{dirname}/{entry.name}: unrecognized file name; expected "
                f"{slug}_{condition}_NNN with a recorder audio extension"
            )
            continue
        matched += 1

    if matched < min_reps:
        result.errors.append(
            f"{dirname}/: found {matched} correctly named recording(s), need at least {min_reps}"
        )


def _check_near_phrase_dir(root: Path, result: ValidationResult) -> None:
    directory = root / "near_phrase"
    if not directory.is_dir():
        result.errors.append("missing required folder: near_phrase/")
        return

    known = {item.slug: item for item in spec.NEAR_PHRASE_ITEMS}
    counts: dict[str, int] = dict.fromkeys(known, 0)
    pattern = re.compile(rf"^({_SLUG_PATTERN})_({_TAKE_PATTERN})$")

    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            result.errors.append(f"near_phrase/{entry.name}: expected a file, found a directory")
            continue
        match = pattern.match(entry.stem)
        if not match or not _extension_ok(entry.name):
            result.errors.append(
                f"near_phrase/{entry.name}: unrecognized file name; expected "
                "<phrase-slug>_NNN with a recorder audio extension"
            )
            continue
        slug = match.group(1)
        if slug not in known:
            result.errors.append(
                f"near_phrase/{entry.name}: {slug!r} is not one of the near-phrase "
                "slugs in SPEAKER_RECORDING_PACKAGE.md -- this loader refuses to "
                "guess which phrase a file belongs to"
            )
            continue
        counts[slug] += 1

    for slug, item in known.items():
        if counts[slug] < spec.NEAR_PHRASE_REPS:
            result.errors.append(
                f"near_phrase/: only {counts[slug]} take(s) of {item.text!r} ({slug}), "
                f"need at least {spec.NEAR_PHRASE_REPS}"
            )


def _check_freeform_dir(
    root: Path, dirname: str, prefix: str, min_files: int, result: ValidationResult
) -> None:
    """A folder holding one or more continuous ``<prefix>_NNN`` takes."""
    directory = root / dirname
    if not directory.is_dir():
        result.errors.append(f"missing required folder: {dirname}/")
        return

    pattern = re.compile(rf"^{re.escape(prefix)}_{_TAKE_PATTERN}$")
    matched = 0
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            result.errors.append(f"{dirname}/{entry.name}: expected a file, found a directory")
            continue
        if not pattern.match(entry.stem) or not _extension_ok(entry.name):
            result.errors.append(
                f"{dirname}/{entry.name}: unrecognized file name; expected "
                f"{prefix}_NNN with a recorder audio extension"
            )
            continue
        matched += 1

    if matched < min_files:
        result.errors.append(f"{dirname}/: found {matched} recording(s), need at least {min_files}")


def _check_metadata(root: Path, result: ValidationResult) -> None:
    path = root / "metadata.json"
    if not path.is_file():
        result.errors.append("missing metadata.json")
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result.errors.append(f"metadata.json is not well-formed JSON: {exc}")
        return
    if not isinstance(data, dict):
        result.errors.append("metadata.json must contain a single JSON object")
        return

    for name in spec.REQUIRED_METADATA_FIELDS:
        if name not in data:
            result.errors.append(f"metadata.json is missing required field {name!r}")
            continue
        if name == "noise_sources_used":
            continue  # a list, checked separately below
        value = data[name]
        if not isinstance(value, str) or not value.strip():
            result.errors.append(f"metadata.json field {name!r} must be a non-empty string")
        elif value.strip().startswith("<"):
            result.errors.append(
                f"metadata.json field {name!r} still has the template placeholder "
                "text in it; fill in a real answer"
            )

    for name in data:
        if name not in spec.ALL_METADATA_FIELDS:
            result.errors.append(f"metadata.json has an unrecognized field {name!r}")

    noise = data.get("noise_sources_used")
    valid_noise: set[str] = set()
    if not isinstance(noise, list) or not noise:
        result.errors.append("metadata.json field 'noise_sources_used' must be a non-empty list")
    else:
        for source in noise:
            if source in spec.NOISE_SOURCE_VOCAB:
                valid_noise.add(source)
            else:
                result.errors.append(
                    f"metadata.json lists noise source {source!r}, which is not one "
                    f"of {sorted(spec.NOISE_SOURCE_VOCAB)}"
                )
        if len(valid_noise) < spec.MIN_NOISE_SOURCES:
            result.errors.append(
                f"metadata.json must list at least {spec.MIN_NOISE_SOURCES} genuine "
                "noise sources"
            )

    for source in sorted(spec.NOISE_SOURCE_VOCAB):
        declared = source in valid_noise
        has_dir = (root / f"positive_noise_{source}").is_dir()
        if declared and not has_dir:
            result.errors.append(
                f"metadata.json declares noise source {source!r} but "
                f"positive_noise_{source}/ does not exist"
            )
        elif has_dir and not declared:
            result.warnings.append(
                f"positive_noise_{source}/ exists but metadata.json does not list "
                f"{source!r} in noise_sources_used"
            )


def validate_speaker_directory(root: Path) -> ValidationResult:
    """Check ``root`` -- one speaker's submission folder -- against the spec."""
    result = ValidationResult()
    if not root.is_dir():
        result.errors.append(f"{root} is not a directory")
        return result

    _check_metadata(root, result)

    for condition in spec.POSITIVE_CONDITIONS:
        _check_condition_dir(
            root,
            f"positive_{condition}",
            spec.WAKE_PHRASE_SLUG,
            condition,
            spec.POSITIVE_REPS,
            result,
        )

    _check_condition_dir(
        root,
        f"positive_{spec.FARFIELD_CONDITION}",
        spec.WAKE_PHRASE_SLUG,
        spec.FARFIELD_CONDITION,
        spec.FARFIELD_REPS,
        result,
    )

    present_noise_sources = [
        source
        for source in sorted(spec.NOISE_SOURCE_VOCAB)
        if (root / f"positive_noise_{source}").is_dir()
    ]
    if len(present_noise_sources) < spec.MIN_NOISE_SOURCES:
        result.errors.append(
            f"found only {len(present_noise_sources)} positive_noise_* folder(s), "
            f"need at least {spec.MIN_NOISE_SOURCES} of {sorted(spec.NOISE_SOURCE_VOCAB)}"
        )
    for source in present_noise_sources:
        _check_condition_dir(
            root,
            f"positive_noise_{source}",
            spec.WAKE_PHRASE_SLUG,
            f"noise-{source}",
            spec.NOISE_REPS,
            result,
        )

    _check_near_phrase_dir(root, result)
    _check_freeform_dir(
        root, "negative_freespeech", "freespeech", spec.FREESPEECH_MIN_FILES, result
    )
    _check_freeform_dir(root, "background_only", "background", spec.BACKGROUND_MIN_FILES, result)

    known_entries = {
        "metadata.json",
        *(f"positive_{c}" for c in spec.POSITIVE_CONDITIONS),
        f"positive_{spec.FARFIELD_CONDITION}",
        *(f"positive_noise_{s}" for s in spec.NOISE_SOURCE_VOCAB),
        "near_phrase",
        "negative_freespeech",
        "background_only",
    }
    for entry in root.iterdir():
        if entry.name not in known_entries:
            result.errors.append(f"unrecognized entry in {root}: {entry.name}")

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "speaker_dir", type=Path, help="a submitted speaker folder, e.g. <speaker-id>/"
    )
    args = parser.parse_args(argv)

    result = validate_speaker_directory(args.speaker_dir)
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    for error in result.errors:
        print(f"ERROR: {error}")

    if result.ok:
        print(f"{args.speaker_dir}: all required sections present and well-named")
        return 0
    print(f"{args.speaker_dir}: {len(result.errors)} problem(s) found")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
