"""``validate_speaker_submission.py`` must catch every way a folder can drift
from ``SPEAKER_RECORDING_PACKAGE.md`` before that folder is handed over.

Background
----------
The package tells a speaker that our loader "refuses to guess a label" from
an unrecognized file. Without something that actually enforces that before
upload, the first place a misnamed file, a missing section or an incomplete
metadata form would be caught is weeks later, once several speakers'
folders are being merged into one training set -- at which point nobody can
ask the speaker what a stray file was supposed to be.

All fixtures are built under ``tmp_path`` and contain only synthetic
placeholder bytes; nothing here reads or writes a real speaker's recordings.
The metadata fixture uses ``"E999"``, a speaker id that is not one of the
seven assigned for this round, precisely so this file has nothing that reads
as a real per-speaker record.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import speaker_recording_spec as spec  # noqa: E402
import validate_speaker_submission as validator  # noqa: E402


def _write_audio(path: Path) -> None:
    path.write_bytes(b"placeholder bytes standing in for a real recording")


def _valid_metadata() -> dict:
    return {
        "speaker_id": "E999",
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


def _build_valid_submission(root: Path, metadata: dict | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.json").write_text(
        json.dumps(metadata if metadata is not None else _valid_metadata()), encoding="utf-8"
    )

    for condition in spec.POSITIVE_CONDITIONS:
        d = root / f"positive_{condition}"
        d.mkdir()
        for take in range(1, spec.POSITIVE_REPS + 1):
            _write_audio(d / f"{spec.WAKE_PHRASE_SLUG}_{condition}_{take:03d}.m4a")

    d = root / f"positive_{spec.FARFIELD_CONDITION}"
    d.mkdir()
    for take in range(1, spec.FARFIELD_REPS + 1):
        _write_audio(d / f"{spec.WAKE_PHRASE_SLUG}_{spec.FARFIELD_CONDITION}_{take:03d}.m4a")

    for source in ("tv", "kitchen"):
        d = root / f"positive_noise_{source}"
        d.mkdir()
        for take in range(1, spec.NOISE_REPS + 1):
            _write_audio(d / f"{spec.WAKE_PHRASE_SLUG}_noise-{source}_{take:03d}.m4a")

    d = root / "near_phrase"
    d.mkdir()
    for item in spec.NEAR_PHRASE_ITEMS:
        for take in range(1, spec.NEAR_PHRASE_REPS + 1):
            _write_audio(d / f"{item.slug}_{take:03d}.m4a")

    d = root / "negative_freespeech"
    d.mkdir()
    _write_audio(d / "freespeech_001.m4a")

    d = root / "background_only"
    d.mkdir()
    _write_audio(d / "background_001.m4a")


@pytest.fixture()
def valid_submission(tmp_path: Path) -> Path:
    root = tmp_path / "E999"
    _build_valid_submission(root)
    return root


def test_a_fully_compliant_submission_has_no_errors(valid_submission: Path) -> None:
    result = validator.validate_speaker_directory(valid_submission)
    assert result.errors == []
    assert result.ok


def test_missing_speaker_directory_is_reported(tmp_path: Path) -> None:
    result = validator.validate_speaker_directory(tmp_path / "does_not_exist")
    assert not result.ok
    assert any("is not a directory" in e for e in result.errors)


def test_missing_metadata_json_is_reported(valid_submission: Path) -> None:
    (valid_submission / "metadata.json").unlink()
    result = validator.validate_speaker_directory(valid_submission)
    assert any("missing metadata.json" in e for e in result.errors)


def test_malformed_metadata_json_is_reported(valid_submission: Path) -> None:
    (valid_submission / "metadata.json").write_text("{not valid json", encoding="utf-8")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("not well-formed JSON" in e for e in result.errors)


def test_missing_required_metadata_field_is_reported(valid_submission: Path) -> None:
    metadata = _valid_metadata()
    del metadata["device_make_model"]
    (valid_submission / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("missing required field 'device_make_model'" in e for e in result.errors)


def test_unfilled_template_placeholder_is_reported(valid_submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["room_name"] = "<e.g. living room>"
    (valid_submission / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("template placeholder" in e for e in result.errors)


def test_unrecognized_metadata_field_is_reported(valid_submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["favourite_colour"] = "blue"
    (valid_submission / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("unrecognized field 'favourite_colour'" in e for e in result.errors)


def test_too_few_noise_sources_declared_is_reported(valid_submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["noise_sources_used"] = ["tv"]
    (valid_submission / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("at least 2 genuine" in e for e in result.errors)


def test_noise_source_not_in_vocabulary_is_reported(valid_submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["noise_sources_used"] = ["tv", "dishwasher"]
    (valid_submission / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("'dishwasher'" in e and "not one of" in e for e in result.errors)


def test_declared_noise_source_without_a_matching_folder_is_reported(valid_submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["noise_sources_used"] = ["tv", "street"]
    (valid_submission / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("declares noise source 'street'" in e for e in result.errors)


def test_undeclared_noise_folder_is_a_warning_not_an_error(valid_submission: Path) -> None:
    metadata = _valid_metadata()
    metadata["noise_sources_used"] = ["tv", "kitchen", "street"]
    (valid_submission / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    d = valid_submission / "positive_noise_street"
    d.mkdir()
    for take in range(1, spec.NOISE_REPS + 1):
        _write_audio(d / f"{spec.WAKE_PHRASE_SLUG}_noise-street_{take:03d}.m4a")
    result = validator.validate_speaker_directory(valid_submission)
    assert result.ok, f"unexpected errors: {result.errors}"


def test_missing_positive_condition_folder_is_reported(valid_submission: Path) -> None:
    _rmtree(valid_submission / "positive_slow")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("missing required folder: positive_slow/" in e for e in result.errors)


def test_insufficient_repetitions_in_a_positive_condition_is_reported(valid_submission: Path) -> None:
    files = sorted((valid_submission / "positive_loud").glob("*.m4a"))
    files[0].unlink()
    result = validator.validate_speaker_directory(valid_submission)
    assert any("positive_loud/: found" in e for e in result.errors)


def test_a_recorder_autonamed_file_is_a_hard_error(valid_submission: Path) -> None:
    """Many recorder apps append their own counter -- "Voice Memo 3.m4a"."""
    (valid_submission / "positive_normal" / "Voice Memo 3.m4a").write_bytes(b"x")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("unrecognized file name" in e and "Voice Memo 3.m4a" in e for e in result.errors)


def test_unknown_near_phrase_slug_is_a_hard_error(valid_submission: Path) -> None:
    (valid_submission / "near_phrase" / "hey-youtube_001.m4a").write_bytes(b"x")
    result = validator.validate_speaker_directory(valid_submission)
    assert any(
        "hey-youtube" in e and "refuses to guess" in e for e in result.errors
    ), result.errors


def test_insufficient_near_phrase_repetitions_is_reported(valid_submission: Path) -> None:
    target = next(iter(sorted((valid_submission / "near_phrase").glob("hey-you-tap_*.m4a"))))
    target.unlink()
    result = validator.validate_speaker_directory(valid_submission)
    assert any("hey you tap." in e for e in result.errors)


def test_missing_negative_freespeech_folder_is_reported(valid_submission: Path) -> None:
    _rmtree(valid_submission / "negative_freespeech")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("missing required folder: negative_freespeech/" in e for e in result.errors)


def test_missing_background_only_folder_is_reported(valid_submission: Path) -> None:
    _rmtree(valid_submission / "background_only")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("missing required folder: background_only/" in e for e in result.errors)


def test_unrecognized_top_level_entry_is_reported(valid_submission: Path) -> None:
    (valid_submission / "notes_from_speaker.txt").write_text("hello", encoding="utf-8")
    result = validator.validate_speaker_directory(valid_submission)
    assert any("unrecognized entry" in e and "notes_from_speaker.txt" in e for e in result.errors)


def test_cli_main_returns_zero_for_a_compliant_submission(valid_submission: Path, capsys) -> None:
    code = validator.main([str(valid_submission)])
    assert code == 0
    assert "all required sections present" in capsys.readouterr().out


def test_cli_main_returns_nonzero_for_a_broken_submission(valid_submission: Path, capsys) -> None:
    _rmtree(valid_submission / "background_only")
    code = validator.main([str(valid_submission)])
    assert code == 1
    out = capsys.readouterr().out
    assert "ERROR:" in out
    assert "problem(s) found" in out


def _rmtree(path: Path) -> None:
    for entry in sorted(path.iterdir()):
        entry.unlink()
    path.rmdir()
