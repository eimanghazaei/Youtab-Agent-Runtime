"""WP-10: the in-place rename of originals that cannot be recorded again."""

from __future__ import annotations

import os
import sys
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wakeword"))

import speaker_recording_spec as spec  # noqa: E402
import validate_speaker_submission as validator  # noqa: E402

LABEL = "E0" + "03"


def _take(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(np.zeros(1600, dtype="<i2").tobytes())
    os.utime(path, (mtime, mtime))


def test_sync_client_litter_never_takes_a_take_number(tmp_path):
    """A .DS_Store counted as a file shifts every real take by one number.

    The rename is in place, on recordings nobody can make again, and the
    recorder own filenames are gone afterwards with no record of them anywhere
    -- so a shifted numbering cannot be undone or even diagnosed. macOS writes
    .DS_Store into every folder it opens, so this is the normal case.
    """
    section = spec.POSITIVE_SECTIONS[0]
    stems = spec.expected_stems_for_positive_section(section)
    root = tmp_path / LABEL
    directory = root / spec.ORIGINALS_DIR / section.directory
    directory.mkdir(parents=True)
    (directory / ".DS_Store").write_bytes(b"\x00\x01macos")
    os.utime(directory / ".DS_Store", (1, 1))
    for index in range(len(stems) - 1):
        _take(directory / ("memo_" + str(index) + ".m4a"), 10 + index)

    actions, result = validator.rename_plan(root)
    assert not actions, [(a.old_path.name, a.new_name) for a in actions]
    assert result.errors, "a folder one take short was auto-named anyway"
    assert (directory / ".DS_Store").is_file()


def test_an_appledouble_file_does_not_shift_the_freeform_takes(tmp_path):
    """In a freeform section the file count decides how many stems exist, so a
    counted artefact is guaranteed to take a slot from a real recording.

    macOS writes an AppleDouble ._name beside every file it copies onto exFAT,
    which is how a removable drive handed between two people is formatted.
    """
    section = spec.FREEFORM_SECTIONS[0]
    root = tmp_path / LABEL
    directory = root / spec.ORIGINALS_DIR / section.directory
    directory.mkdir(parents=True)
    (directory / ".DS_Store").write_bytes(b"ds")
    os.utime(directory / ".DS_Store", (5, 5))
    (directory / "._speech.m4a").write_bytes(b"AppleDouble")
    os.utime(directory / "._speech.m4a", (6, 6))
    _take(directory / "speech.m4a", 20)

    actions, result = validator.rename_plan(root)
    assert not result.errors, result.errors
    moved = {action.old_path.name: action.new_name for action in actions}
    assert moved == {"speech.m4a": section.prefix + "_001.m4a"}, moved

    validator.apply_rename_plan(actions)
    assert (directory / (section.prefix + "_001.m4a")).is_file()
    assert (directory / ".DS_Store").is_file()
    assert (directory / "._speech.m4a").is_file()


def test_a_file_nobody_can_classify_stops_the_rename(tmp_path):
    """Not silently skipped either: the folder is left exactly as it was."""
    section = spec.FREEFORM_SECTIONS[0]
    root = tmp_path / LABEL
    directory = root / spec.ORIGINALS_DIR / section.directory
    directory.mkdir(parents=True)
    _take(directory / "speech.m4a", 20)
    (directory / "notes.txt").write_text("my notes", encoding="utf-8")

    actions, result = validator.rename_plan(root)
    assert result.errors
    assert not actions
    assert (directory / "speech.m4a").is_file()
