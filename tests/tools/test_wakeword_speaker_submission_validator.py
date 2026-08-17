"""``validate_speaker_submission.py`` must catch every way a folder can drift
from ``SPEAKER_RECORDING_PACKAGE.md`` before that folder is ingested.

Background
----------
The package tells a speaker that our loader "refuses to guess a label" from an
unrecognised file. Without something that actually enforces that, the first
place a misnamed file, a missing section, an unsigned consent record or an
incomplete metadata form would be caught is weeks later, once five speakers'
folders are being merged into one training set -- at which point nobody can ask
the speaker what a stray file was supposed to be.

The layout under test is the one every speaker in the round uses: four entries
in the speaker folder (``originals/``, ``CONSENT.pdf``,
``RECORDING_METADATA.json``, ``SHA256SUMS``) and nothing else.

All fixtures are built under ``tmp_path`` and contain only synthetic
placeholder bytes; nothing here reads or writes a real speaker's recordings,
and the digests in the fixture ``SHA256SUMS`` are genuine digests of those
placeholder bytes rather than invented ones. The fixture's speaker label is
read out of the spec's assignment table rather than written here as a literal,
so this file contains no per-speaker record of its own.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import speaker_recording_spec as spec  # noqa: E402
import validate_speaker_submission as validator  # noqa: E402

#: The first assigned label, read from the spec so no literal per-speaker
#: record appears in this file.
FIXTURE_LABEL = spec.SPEAKER_ASSIGNMENTS[0].label

#: A label that is deliberately *not* in the assignment table.
UNASSIGNED_LABEL = "E" + "999"


def _write_audio(path: Path) -> None:
    path.write_bytes(b"placeholder bytes standing in for a real recording")


def _valid_metadata() -> dict:
    metadata = {
        "recording_date": "2026-08-17",
        "device_make_model": "Example Phone 12",
        "recording_app": "Example Voice Recorder",
        "room_name": "living room",
        "room_size_approx": "about 4 by 5 metres",
        "floor_surface": "carpet",
        "wall_surface": "drywall, one large window",
        "background_sources_present": "refrigerator hum",
        "noise_sources_used": ["tv", "kitchen"],
        "farfield_distance": "about 5 metres, adjoining room, door open",
        "consent_signed_date": "2026-08-10",
    }
    return {"speaker_id": FIXTURE_LABEL, **metadata}


def _write_metadata(root: Path, metadata: dict) -> None:
    (root / spec.METADATA_FILE).write_text(json.dumps(metadata), encoding="utf-8")


def _write_checksums(root: Path) -> None:
    """A genuine coreutils-format listing of everything else in the folder.

    Built by walking the fixture rather than by asking the validator what it
    expects, so a bug in the validator's own file walk cannot be papered over
    by a listing that agrees with it.
    """
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == spec.CHECKSUM_FILE:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}\n")
    (root / spec.CHECKSUM_FILE).write_text("".join(lines), encoding="utf-8")


def _build_valid_submission(root: Path, metadata: dict | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / spec.CONSENT_FILE).write_bytes(b"%PDF-1.4 placeholder consent scan")
    _write_metadata(root, metadata if metadata is not None else _valid_metadata())

    originals = root / spec.ORIGINALS_DIR
    originals.mkdir()

    for section in spec.POSITIVE_SECTIONS:
        directory = originals / section.directory
        directory.mkdir()
        for take in range(1, section.takes + 1):
            _write_audio(
                directory
                / f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_{take:03d}.m4a"
            )

    for source in ("tv", "kitchen"):
        section = spec.noise_section(source)
        directory = originals / section.directory
        directory.mkdir()
        for take in range(1, section.takes + 1):
            _write_audio(
                directory
                / f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_{take:03d}.m4a"
            )

    directory = originals / "near_phrase"
    directory.mkdir()
    for item in spec.NEAR_PHRASE_ITEMS:
        for take in range(1, item.takes + 1):
            _write_audio(directory / f"{item.slug}_{take:03d}.m4a")

    for section in spec.FREEFORM_SECTIONS:
        directory = originals / section.directory
        directory.mkdir()
        for take in range(1, section.min_files + 1):
            _write_audio(directory / f"{section.prefix}_{take:03d}.m4a")

    _write_checksums(root)


@pytest.fixture()
def submission(tmp_path: Path) -> Path:
    root = tmp_path / FIXTURE_LABEL
    _build_valid_submission(root)
    return root


# ── the happy path, and the shape of the folder ──────────────────────────────


def test_a_fully_compliant_submission_has_no_errors(submission: Path) -> None:
    result = validator.validate_speaker_directory(submission)
    assert result.errors == []
    assert result.warnings == []
    assert result.ok


def test_the_fixture_is_the_size_the_spec_says_a_session_produces(submission: Path) -> None:
    """Non-vacuity for everything below: the fixture is a whole submission."""
    audio = [
        path
        for path in (submission / spec.ORIGINALS_DIR).rglob("*")
        if path.is_file()
    ]
    assert len(audio) == spec.audio_files_per_speaker()


def test_missing_speaker_directory_is_reported(tmp_path: Path) -> None:
    result = validator.validate_speaker_directory(tmp_path / "does_not_exist")
    assert not result.ok
    assert any("is not a directory" in e for e in result.errors)


def test_a_folder_not_named_for_a_speaker_label_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "jane-recordings"
    _build_valid_submission(root)
    result = validator.validate_speaker_directory(root)
    assert any("has to be the speaker label alone" in e for e in result.errors)


def test_an_unrecognised_top_level_entry_is_reported(submission: Path) -> None:
    (submission / "notes_from_speaker.txt").write_text("hello", encoding="utf-8")
    result = validator.validate_speaker_directory(submission)
    assert any(
        "unrecognised entry" in e and "notes_from_speaker.txt" in e for e in result.errors
    )


def test_an_unrecognised_entry_inside_originals_is_reported(submission: Path) -> None:
    (submission / spec.ORIGINALS_DIR / "positive_whispered").mkdir()
    result = validator.validate_speaker_directory(submission)
    assert any(
        "unrecognised entry in originals/" in e and "positive_whispered" in e
        for e in result.errors
    )


def test_a_missing_originals_folder_is_reported(submission: Path) -> None:
    shutil.rmtree(submission / spec.ORIGINALS_DIR)
    result = validator.validate_speaker_directory(submission)
    assert any("missing required folder: originals/" in e for e in result.errors)


# ── consent ──────────────────────────────────────────────────────────────────


def test_a_missing_consent_record_is_reported(submission: Path) -> None:
    (submission / spec.CONSENT_FILE).unlink()
    result = validator.validate_speaker_directory(submission)
    assert any("missing CONSENT.pdf" in e for e in result.errors)


def test_an_empty_consent_record_is_reported(submission: Path) -> None:
    (submission / spec.CONSENT_FILE).write_bytes(b"")
    result = validator.validate_speaker_directory(submission)
    assert any("CONSENT.pdf is empty" in e for e in result.errors)


# ── the metadata form ────────────────────────────────────────────────────────


def test_missing_metadata_is_reported(submission: Path) -> None:
    (submission / spec.METADATA_FILE).unlink()
    result = validator.validate_speaker_directory(submission)
    assert any("missing RECORDING_METADATA.json" in e for e in result.errors)


def test_malformed_metadata_is_reported(submission: Path) -> None:
    (submission / spec.METADATA_FILE).write_text("{not valid json", encoding="utf-8")
    result = validator.validate_speaker_directory(submission)
    assert any("not well-formed JSON" in e for e in result.errors)


def test_metadata_that_is_not_an_object_is_reported(submission: Path) -> None:
    (submission / spec.METADATA_FILE).write_text("[]", encoding="utf-8")
    result = validator.validate_speaker_directory(submission)
    assert any("must contain a single JSON object" in e for e in result.errors)


def test_a_missing_required_metadata_field_is_reported(submission: Path) -> None:
    metadata = _valid_metadata()
    del metadata["device_make_model"]
    _write_metadata(submission, metadata)
    result = validator.validate_speaker_directory(submission)
    assert any("missing required field 'device_make_model'" in e for e in result.errors)


def test_an_unfilled_template_placeholder_is_reported(submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["room_name"] = "<e.g. living room>"
    _write_metadata(submission, metadata)
    result = validator.validate_speaker_directory(submission)
    assert any("template placeholder" in e for e in result.errors)


def test_an_unrecognised_metadata_field_is_reported(submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["favourite_colour"] = "blue"
    _write_metadata(submission, metadata)
    result = validator.validate_speaker_directory(submission)
    assert any("unrecognised field 'favourite_colour'" in e for e in result.errors)


def test_metadata_naming_a_different_speaker_than_the_folder_is_reported(
    submission: Path,
) -> None:
    """The mismatch that could mix a sealed voice into training."""
    metadata = _valid_metadata()
    metadata["speaker_id"] = spec.SPEAKER_ASSIGNMENTS[-1].label
    _write_metadata(submission, metadata)
    result = validator.validate_speaker_directory(submission)
    assert any("but the folder is" in e for e in result.errors)


def test_a_label_with_no_assignment_is_refused(tmp_path: Path) -> None:
    """No assignment means nothing records whether this audio may be trained on."""
    root = tmp_path / UNASSIGNED_LABEL
    metadata = _valid_metadata()
    metadata["speaker_id"] = UNASSIGNED_LABEL
    _build_valid_submission(root, metadata)
    result = validator.validate_speaker_directory(root)
    assert any("SPEAKER_ASSIGNMENTS" in e for e in result.errors)


def test_a_name_in_place_of_a_label_is_refused(submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["speaker_id"] = "jane"
    _write_metadata(submission, metadata)
    result = validator.validate_speaker_directory(submission)
    assert any("not a speaker label of the form" in e for e in result.errors)


# ── noise sources ────────────────────────────────────────────────────────────


def test_too_few_noise_sources_declared_is_reported(submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["noise_sources_used"] = ["tv"]
    _write_metadata(submission, metadata)
    result = validator.validate_speaker_directory(submission)
    assert any("at least 2 genuine" in e for e in result.errors)


def test_a_noise_source_outside_the_vocabulary_is_reported(submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["noise_sources_used"] = ["tv", "dishwasher"]
    _write_metadata(submission, metadata)
    result = validator.validate_speaker_directory(submission)
    assert any("'dishwasher'" in e and "not one of" in e for e in result.errors)


def test_a_declared_noise_source_without_a_folder_is_reported(submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["noise_sources_used"] = ["tv", "street"]
    _write_metadata(submission, metadata)
    result = validator.validate_speaker_directory(submission)
    assert any("declares noise source 'street'" in e for e in result.errors)


def test_an_undeclared_noise_folder_is_a_warning_not_an_error(submission: Path) -> None:
    section = spec.noise_section("street")
    directory = submission / spec.ORIGINALS_DIR / section.directory
    directory.mkdir()
    for take in range(1, section.takes + 1):
        _write_audio(
            directory / f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_{take:03d}.m4a"
        )
    _write_checksums(submission)

    result = validator.validate_speaker_directory(submission)
    assert result.ok, f"unexpected errors: {result.errors}"
    assert any("does not list 'street'" in w for w in result.warnings)


def test_a_submission_with_only_one_noise_folder_is_reported(submission: Path) -> None:
    shutil.rmtree(submission / spec.ORIGINALS_DIR / "positive_noise_kitchen")
    result = validator.validate_speaker_directory(submission)
    assert any("need at least 2 of" in e for e in result.errors)


# ── the recording tree ───────────────────────────────────────────────────────


def test_a_missing_positive_condition_folder_is_reported(submission: Path) -> None:
    shutil.rmtree(submission / spec.ORIGINALS_DIR / "positive_slow")
    result = validator.validate_speaker_directory(submission)
    assert any(
        "missing required folder: originals/positive_slow/" in e for e in result.errors
    )


def test_a_missing_raised_voice_farfield_folder_is_reported(submission: Path) -> None:
    """The condition a miss cannot otherwise be attributed to."""
    shutil.rmtree(submission / spec.ORIGINALS_DIR / "positive_farfield_loud")
    result = validator.validate_speaker_directory(submission)
    assert any(
        "missing required folder: originals/positive_farfield_loud/" in e
        for e in result.errors
    )


def test_insufficient_repetitions_in_a_positive_condition_is_reported(
    submission: Path,
) -> None:
    files = sorted((submission / spec.ORIGINALS_DIR / "positive_loud").glob("*.m4a"))
    files[0].unlink()
    result = validator.validate_speaker_directory(submission)
    assert any("originals/positive_loud/: found" in e for e in result.errors)


def test_a_recorder_autonamed_file_is_a_hard_error(submission: Path) -> None:
    """Many recorder apps append their own counter -- "Voice Memo 3.m4a"."""
    (submission / spec.ORIGINALS_DIR / "positive_normal" / "Voice Memo 3.m4a").write_bytes(
        b"x"
    )
    result = validator.validate_speaker_directory(submission)
    assert any(
        "unrecognised file name" in e and "Voice Memo 3.m4a" in e for e in result.errors
    )


def test_a_file_with_no_audio_extension_is_a_hard_error(submission: Path) -> None:
    directory = submission / spec.ORIGINALS_DIR / "positive_normal"
    (directory / f"{spec.WAKE_PHRASE_SLUG}_normal_006.txt").write_text("x", encoding="utf-8")
    result = validator.validate_speaker_directory(submission)
    assert any("unrecognised file name" in e and "_006.txt" in e for e in result.errors)


def test_an_unknown_near_phrase_slug_is_a_hard_error(submission: Path) -> None:
    (submission / spec.ORIGINALS_DIR / "near_phrase" / "hey-alexa_001.m4a").write_bytes(b"x")
    result = validator.validate_speaker_directory(submission)
    assert any(
        "hey-alexa" in e and "refuses to guess" in e for e in result.errors
    ), result.errors


def test_insufficient_near_phrase_repetitions_is_reported(submission: Path) -> None:
    """The minimal pair asks for five takes, not three."""
    target = sorted((submission / spec.ORIGINALS_DIR / "near_phrase").glob("hey-you-tap_*"))[0]
    target.unlink()
    result = validator.validate_speaker_directory(submission)
    assert any("hey you tap." in e and "need at least 5" in e for e in result.errors)


def test_a_missing_freespeech_folder_is_reported(submission: Path) -> None:
    shutil.rmtree(submission / spec.ORIGINALS_DIR / "negative_freespeech")
    result = validator.validate_speaker_directory(submission)
    assert any(
        "missing required folder: originals/negative_freespeech/" in e for e in result.errors
    )


def test_a_missing_background_only_folder_is_reported(submission: Path) -> None:
    shutil.rmtree(submission / spec.ORIGINALS_DIR / "background_only")
    result = validator.validate_speaker_directory(submission)
    assert any(
        "missing required folder: originals/background_only/" in e for e in result.errors
    )


def test_a_directory_where_a_take_belongs_is_reported(submission: Path) -> None:
    (submission / spec.ORIGINALS_DIR / "background_only" / "old_takes").mkdir()
    result = validator.validate_speaker_directory(submission)
    assert any("expected a file, found a directory" in e for e in result.errors)


# ── the checksum listing ─────────────────────────────────────────────────────


def test_a_missing_checksum_listing_is_reported(submission: Path) -> None:
    (submission / spec.CHECKSUM_FILE).unlink()
    result = validator.validate_speaker_directory(submission)
    assert any("missing SHA256SUMS" in e for e in result.errors)


def test_a_malformed_checksum_line_is_reported(submission: Path) -> None:
    path = submission / spec.CHECKSUM_FILE
    path.write_text(
        path.read_text(encoding="utf-8") + "not-a-digest  originals/x.m4a\n",
        encoding="utf-8",
    )
    result = validator.validate_speaker_directory(submission)
    assert any("is not coreutils format" in e for e in result.errors)


def test_a_file_missing_from_the_checksum_listing_is_reported(submission: Path) -> None:
    path = submission / spec.CHECKSUM_FILE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[1:]), encoding="utf-8")
    result = validator.validate_speaker_directory(submission)
    assert any("does not list" in e for e in result.errors)


def test_a_checksum_line_for_a_file_that_is_not_there_is_reported(submission: Path) -> None:
    path = submission / spec.CHECKSUM_FILE
    path.write_text(
        path.read_text(encoding="utf-8") + f"{'0' * 64}  originals/ghost_001.m4a\n",
        encoding="utf-8",
    )
    result = validator.validate_speaker_directory(submission)
    assert any("which is not in the folder" in e for e in result.errors)


def test_a_duplicated_checksum_line_is_reported(submission: Path) -> None:
    path = submission / spec.CHECKSUM_FILE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines) + lines[0], encoding="utf-8")
    result = validator.validate_speaker_directory(submission)
    assert any("more than once" in e for e in result.errors)


def test_the_checksum_listing_does_not_have_to_cover_itself(submission: Path) -> None:
    """A digest of a file that contains that digest is not a thing."""
    listed = (submission / spec.CHECKSUM_FILE).read_text(encoding="utf-8")
    assert spec.CHECKSUM_FILE not in listed
    assert validator.validate_speaker_directory(submission).ok


# ── the command line ─────────────────────────────────────────────────────────


def test_cli_main_returns_zero_for_a_compliant_submission(submission: Path, capsys) -> None:
    code = validator.main([str(submission)])
    assert code == 0
    assert "all required sections present" in capsys.readouterr().out


def test_cli_main_returns_nonzero_for_a_broken_submission(submission: Path, capsys) -> None:
    shutil.rmtree(submission / spec.ORIGINALS_DIR / "background_only")
    code = validator.main([str(submission)])
    assert code == 1
    out = capsys.readouterr().out
    assert "ERROR:" in out
    assert "problem(s) found" in out
