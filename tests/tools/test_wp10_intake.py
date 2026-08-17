"""WP-10: the two ends of an import, oversized media, and what a report carries."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wakeword"))

import freeze_manifest  # noqa: E402
import import_speaker as imp  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402

from tests.tools.test_wakeword_speaker_import import (  # noqa: E402
    TRAIN_LABEL,
    build_package_submission,
    write_wav,
)


def test_a_destination_inside_the_submission_is_refused(tmp_path):
    """--into pointed at the submission puts the only copy on the drive it came
    from, and the report still claims byte-for-byte preservation of it."""
    submission = build_package_submission(tmp_path / TRAIN_LABEL, TRAIN_LABEL)
    with pytest.raises(imp.Refused):
        imp.plan(TRAIN_LABEL, submission, submission)


def test_a_destination_that_is_already_the_submission_is_refused(tmp_path):
    """The mirror case: every original is found present, so nothing is copied,
    and the report reads as a completed import of a copy that does not exist."""
    drive = tmp_path / "drive"
    drive.mkdir()
    submission = build_package_submission(drive / ("speaker_" + TRAIN_LABEL), TRAIN_LABEL)
    with pytest.raises(imp.Refused):
        imp.plan(TRAIN_LABEL, submission, drive)


def test_a_take_past_the_ceiling_is_refused_before_it_is_decoded(tmp_path, monkeypatch):
    """Decoding expands a take by roughly 15x and nothing bounds a real length."""
    monkeypatch.setattr(imp, "MAX_TAKE_BYTES", 8192)
    write_wav(tmp_path / "take.wav", seconds=1.0)
    assert (tmp_path / "take.wav").stat().st_size > 8192
    row = imp.Original(
        "take.wav", "positive_normal", "positive_normal", 1, "", False, "",
        0, "", None, None, (), (), False, "", "",
    )
    described = imp.describe(tmp_path, row)
    assert described.problems, "an oversized take was decoded rather than refused"
    assert described.levels is None
    assert str(imp.MAX_TAKE_BYTES) in described.problems[0]


def test_an_operator_note_naming_a_host_path_never_reaches_a_report(tmp_path):
    """--note is free text and the report is what a decision is attached to.

    The leading anchor in the privacy scan only saw a path at the start of a
    string, and only /mnt and a drive letter were treated as host-shaped.
    """
    submission = build_package_submission(tmp_path / TRAIN_LABEL, TRAIN_LABEL)
    into = tmp_path / "data"
    into.mkdir()
    state = imp.plan(TRAIN_LABEL, submission, into)
    leak = "staged from " + "/home/owner/Wakeword-Human/" + "E0" + "03/originals"
    body = imp.report_body(state, note=leak, run={"planned": True})
    assert imp._privacy_problems(body), "a host path in --note passed the privacy gate"
    with pytest.raises(imp.Refused):
        imp.execute(state, into, note=leak)


def test_the_privacy_scan_walks_dictionary_keys():
    """A body keyed by path reads as structure; a leaf-only walk misses it."""
    assert imp._privacy_problems({"transfer": {"/home/owner/drive/take_001.wav": "0" * 64}})


def test_the_privacy_scan_still_accepts_a_legitimate_report(tmp_path):
    """Non-vacuity: the widened scan must not refuse what the tool itself writes."""
    submission = build_package_submission(tmp_path / TRAIN_LABEL, TRAIN_LABEL)
    into = tmp_path / "data"
    into.mkdir()
    state = imp.plan(TRAIN_LABEL, submission, into)
    body = imp.report_body(state, note="round 8 intake", run={"planned": True})
    assert imp._privacy_problems(body) == []


def test_the_freeze_error_names_only_a_basename(tmp_path):
    """freeze_manifest promises nothing it prints carries an absolute path, and
    the unmounted drive is the first thing that makes it print one."""
    missing = tmp_path / "Wakeword-Human" / ("speaker_" + "E0" + "09") / spec.ORIGINALS_DIR
    with pytest.raises(NotADirectoryError) as caught:
        freeze_manifest.scan(missing)
    assert str(missing) not in str(caught.value)
    assert missing.name in str(caught.value)


def test_a_control_character_in_a_path_keeps_the_sidecar_parseable(tmp_path):
    """SHA256SUMS is the independent check and has to survive one odd name.

    GNU coreutils escapes a backslash and a newline in the filename and prefixes
    the whole line with a backslash; sha256sum -c reverses exactly that. Raw
    bytes produce a listing with more lines than the manifest has files.
    """
    out = tmp_path / "M.json"
    out.write_text("{}", encoding="utf-8")
    files = [
        {"path": "positive_normal/a_001.wav", "sha256": "0" * 64, "bytes": 1},
        {"path": "positive_normal/a_002" + chr(10) + ".wav", "sha256": "1" * 64, "bytes": 1},
        {"path": "positive_normal/a_003" + chr(92) + "b.wav", "sha256": "2" * 64, "bytes": 1},
    ]
    sums, _self = freeze_manifest._write_sidecars(out, {"files": files})
    lines = sums.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(files), lines
    assert lines[1].startswith(chr(92)) and lines[1].endswith("a_002" + chr(92) + "n.wav")
    assert lines[2].startswith(chr(92)) and lines[2].endswith("a_003" + chr(92) * 2 + "b.wav")
