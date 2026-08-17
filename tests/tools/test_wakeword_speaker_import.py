"""``import_speaker.py`` is the last place a mistake about a real recording is
still cheap, so every refusal in it has a test and every refusal has a control.

Background
----------
Five human speakers will each record one session, once. Between the folder that
comes off the drive and ``build_human_dataset.py`` there is exactly one step —
this one — and the failures it exists to stop are all silent: a take nobody can
label, a sealed voice imported into training, one recording counted twice, a
"preserved byte-for-byte" claim nobody measured, and an interrupted run that
leaves a record reading as complete.

Everything here is hermetic. Submissions are built under ``tmp_path`` from
generated WAVs whose payload is a shaped tone plus dither — no speech, no TTS,
and not one byte of anybody's recording. The two frozen human manifests are
touched by a single test that is skipped unless ``YOUTAB_WAKEWORD_FROZEN_HUMAN``
points at the data root, and that test reads *metadata only*: split, usage, the
section names and the per-original digests. It never opens audio, never scores
anything and never writes.

Two conventions this file follows on purpose:

* Speaker labels are read out of the spec and the predeclared config rather than
  typed, and directory names are assembled from a prefix plus a label. The commit
  gate (``test_wakeword_no_human_data_committed.py``) fails on tracked text that
  looks like a speaker-keyed record or a speaker directory, and it should — this
  file must not be the exception that teaches people to relax it.
* The two cross-module agreements this tool depends on (the production sample
  rate, and the split name each speaker's manifest has to declare) are checked by
  reading the other module's source with ``ast`` instead of importing it. That is
  the same trick ``round8_config.py`` uses to avoid pulling numpy and the whole
  feature pipeline in for one constant, and it makes drift loud.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import freeze_manifest  # noqa: E402
import import_speaker as imp  # noqa: E402
import round8_config  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402

SCRIPT = WAKEWORD / "import_speaker.py"

#: Labels, read from the registries rather than written here.
TRAIN_LABEL = spec.labels_for_role(spec.ROLE_TRAINING)[0]
SECOND_TRAIN_LABEL = spec.labels_for_role(spec.ROLE_TRAINING)[1]
VALIDATION_LABEL = spec.labels_for_role(spec.ROLE_VALIDATION)[0]
SEALED_LABEL = spec.labels_for_role(spec.ROLE_SEALED)[0]
PRIOR_TRAIN_LABEL = imp.PRIOR_LAYOUT_LABELS[0]
PRIOR_SEALED_LABEL = imp.PRIOR_LAYOUT_LABELS[1]
UNASSIGNED_LABEL = "E" + "999"

#: SHA-256 of the two frozen manifest files, as recorded in the work package
#: that commissioned this tool. Pinned so the compatibility evidence names the
#: exact bytes it was taken against; the manifests themselves stay on local disk.
FROZEN_MANIFEST_SHA256 = {
    PRIOR_TRAIN_LABEL: "04ad3488617dc9424458513a4d65b9eabfed8c3275fc632bb4f909143765d84d",
    PRIOR_SEALED_LABEL: "a7b1dfa50da9be166bbfe1ce94a11e6091f4caa2787f83e5ede08ce9cf6836bc",
}

#: Where the frozen human data root is, when a run has one. Absent in CI, which
#: is the point: nothing here needs it.
FROZEN_ROOT_ENV = "YOUTAB_WAKEWORD_FROZEN_HUMAN"

RATE = 16000
TAKE_SECONDS = 0.6
CONTINUOUS_SECONDS = 2.0


# ── generated audio ──────────────────────────────────────────────────────────


def _samples(seed: str, seconds: float, rate: int, peak: float) -> np.ndarray:
    """A shaped tone plus dither, unique per file.

    Unique matters: two byte-identical files are a duplicate, which this tool
    refuses, and a fixture that produced them everywhere would fail for the
    right reason at the wrong time. The leading and trailing quiet is what gives
    the noise-floor estimate something to measure.
    """
    generator = np.random.default_rng(
        int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")
    )
    count = int(rate * seconds)
    time_axis = np.arange(count) / rate
    tone = np.sin(2 * np.pi * generator.uniform(180.0, 320.0) * time_axis)
    envelope = np.ones(count)
    edge = max(int(rate * 0.08), 1)
    envelope[:edge] = 0.0
    envelope[-edge:] = 0.0
    dither = generator.normal(0.0, 1e-4, count)
    return np.clip(peak * tone * envelope + dither, -1.0, 1.0)


def write_wav(
    path: Path,
    *,
    seconds: float = TAKE_SECONDS,
    rate: int = RATE,
    channels: int = 1,
    peak: float = 0.3,
    silent: bool = False,
    truncate_bytes: int = 0,
    empty_payload: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        np.zeros(int(rate * seconds))
        if silent
        else _samples(path.as_posix(), seconds, rate, peak)
    )
    frames = np.round(payload * 32767).astype("<i2")
    if channels > 1:
        frames = np.repeat(frames[:, None], channels, axis=1).ravel()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"" if empty_payload else frames.tobytes())
    if truncate_bytes:
        raw = path.read_bytes()
        path.write_bytes(raw[: len(raw) - truncate_bytes])


# ── submissions ──────────────────────────────────────────────────────────────


def valid_metadata(label: str) -> dict:
    # Dates far from today on purpose: one test asserts that no value from this
    # form appears in a report, and today's date legitimately appears in it as
    # the run's timestamp.
    metadata = {
        "recording_date": "2024-02-29",
        "device_make_model": "Example Phone 12",
        "recording_app": "Example Voice Recorder",
        "room_name": "living room",
        "room_size_approx": "about 4 by 5 metres",
        "floor_surface": "carpet",
        "wall_surface": "drywall, one large window",
        "background_sources_present": "refrigerator hum",
        "noise_sources_used": ["tv", "kitchen"],
        "farfield_distance": "about 5 metres, adjoining room, door open",
        "consent_signed_date": "2024-02-20",
    }
    return {"speaker_id": label, **metadata}


def write_metadata(root: Path, metadata: dict) -> None:
    (root / spec.METADATA_FILE).write_text(json.dumps(metadata, indent=1), encoding="utf-8")


def write_checksums(root: Path) -> None:
    """A genuine coreutils listing of everything else in the folder."""
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == spec.CHECKSUM_FILE:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}\n")
    (root / spec.CHECKSUM_FILE).write_text("".join(lines), encoding="utf-8")


def build_package_submission(root: Path, label: str) -> Path:
    """One complete submission in the layout the recording package prescribes."""
    root.mkdir(parents=True, exist_ok=True)
    (root / spec.CONSENT_FILE).write_bytes(b"%PDF-1.4 placeholder consent scan")
    write_metadata(root, valid_metadata(label))

    originals = root / spec.ORIGINALS_DIR
    for section in spec.POSITIVE_SECTIONS:
        for take in range(1, section.takes + 1):
            write_wav(
                originals
                / section.directory
                / f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_{take:03d}.wav"
            )
    for source in sorted(valid_metadata(label)["noise_sources_used"]):
        section = spec.noise_section(source)
        for take in range(1, section.takes + 1):
            write_wav(
                originals
                / section.directory
                / f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_{take:03d}.wav"
            )
    for item in spec.NEAR_PHRASE_ITEMS:
        for take in range(1, item.takes + 1):
            write_wav(originals / "near_phrase" / f"{item.slug}_{take:03d}.wav")
    for section in spec.FREEFORM_SECTIONS:
        for take in range(1, section.min_files + 1):
            write_wav(
                originals / section.directory / f"{section.prefix}_{take:03d}.wav",
                seconds=CONTINUOUS_SECONDS,
            )
    write_checksums(root)
    return root


#: The session sections the two earlier speakers recorded into, by category.
PRIOR_FLAT_SECTIONS = (
    "01_close_normal",
    "02_speed_volume",
    "03_far_field",
    "04_real_noise",
    "05_near_phrases",
    "06_free_speech",
)

#: How many near phrases E002 recorded as separate files in its own
#: subdirectory. The hard requirement is that they stay separate.
PRIOR_GROUP_PHRASES = 12


def build_prior_flat_submission(root: Path, label: str) -> Path:
    """The flat session layout: one continuous recording per section."""
    root.mkdir(parents=True, exist_ok=True)
    (root / spec.CONSENT_FILE).write_bytes(b"%PDF-1.4 placeholder consent scan")
    write_metadata(root, valid_metadata(label))
    for section in PRIOR_FLAT_SECTIONS:
        write_wav(root / f"{section}.wav", seconds=CONTINUOUS_SECONDS)
    return root


def build_prior_group_submission(root: Path, label: str) -> Path:
    """The session layout with the near phrases recorded one file per phrase."""
    root.mkdir(parents=True, exist_ok=True)
    (root / spec.CONSENT_FILE).write_bytes(b"%PDF-1.4 placeholder consent scan")
    write_metadata(root, valid_metadata(label))
    originals = root / spec.ORIGINALS_DIR
    for section in PRIOR_FLAT_SECTIONS:
        if section == "05_near_phrases":
            continue
        write_wav(originals / f"{section}.wav", seconds=CONTINUOUS_SECONDS)
    for item in spec.NEAR_PHRASE_ITEMS[:PRIOR_GROUP_PHRASES]:
        name = item.text.rstrip(".").capitalize()
        write_wav(originals / "05_near_phrases" / f"{name}.wav")
    write_checksums(root)
    return root


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    """One complete package submission, built once and copied per test."""
    root = tmp_path_factory.mktemp("template")
    return build_package_submission(root / TRAIN_LABEL, TRAIN_LABEL)


@pytest.fixture()
def submission(template: Path, tmp_path: Path) -> Path:
    target = tmp_path / TRAIN_LABEL
    shutil.copytree(template, target)
    return target


@pytest.fixture()
def into(tmp_path: Path) -> Path:
    root = tmp_path / "human"
    root.mkdir()
    return root


def row_for(state: imp.Plan, path: str) -> imp.Original:
    for row in state.originals:
        if row.path == path:
            return row
    raise AssertionError(f"{path} was not discovered")


def problems_of(state: imp.Plan, name: str) -> tuple[str, ...]:
    for check in state.checks:
        if check.name == name:
            return check.problems
    raise AssertionError(f"no check named {name}")


def failed(state: imp.Plan) -> set[str]:
    return {check.name for check in state.failures}


# ── the happy path, and what a report has to contain ─────────────────────────


def test_a_complete_submission_passes_every_check(submission: Path, into: Path) -> None:
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert state.ok, {check.name: check.problems for check in state.failures}
    assert len(state.originals) == spec.audio_files_per_speaker()


def test_the_report_carries_every_fact_the_work_package_asks_for(
    submission: Path, into: Path
) -> None:
    """Per file: container, codec, rate, channels, duration, levels, noise floor."""
    state = imp.plan(TRAIN_LABEL, submission, into)
    body = imp.execute(state, into)

    assert body["speaker"] == TRAIN_LABEL
    assert body["role"] == spec.ROLE_TRAINING
    assert body["usage"] in freeze_manifest.USAGES
    assert len(body["originals"]) == spec.audio_files_per_speaker()

    row = next(r for r in body["originals"] if r["section"] == "positive_normal")
    assert row["format"] == {
        "container": "riff-wave",
        "codec": "pcm_s16le",
        "sample_rate_hz": RATE,
        "channels": 1,
        "bits_per_sample": 16,
        "duration_s": TAKE_SECONDS,
        "read_by": "container-parser:riff",
    }
    for key in (
        "samples",
        "channels",
        "sample_rate_hz",
        "duration_s",
        "peak_dbfs",
        "rms_dbfs",
        "dc_offset",
        "full_scale_samples",
        "clipping_runs",
        "longest_clipping_run_samples",
        "noise_floor_dbfs",
        "decoded_by",
    ):
        assert key in row["levels"], key
    assert row["levels"]["peak_dbfs"] == pytest.approx(-10.46, abs=0.5)
    assert row["levels"]["noise_floor_dbfs"] < -60
    assert row["sha256"] == freeze_manifest.sha256_file(
        submission / spec.ORIGINALS_DIR / row["path"]
    )


def test_the_frozen_manifest_records_the_role_and_verifies_against_the_copies(
    submission: Path, into: Path
) -> None:
    state = imp.plan(TRAIN_LABEL, submission, into)
    imp.execute(state, into)

    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    manifest_path = derived / imp.MANIFEST_FILENAME
    manifest = freeze_manifest.load(manifest_path)
    assert manifest["dataset"] == TRAIN_LABEL
    assert manifest["usage"] == spec.ROLE_TRAINING
    assert manifest["split"] == "train"
    assert manifest["file_count"] == spec.audio_files_per_speaker()

    # The freezer's own verification, over the tree this tool published.
    report = freeze_manifest.verify(manifest_path, derived / imp.ORIGINALS_DIR, strict=True)
    assert report["passed"], report
    assert (derived / "ORIGINALS_MANIFEST.SHA256SUMS").is_file()
    assert (derived / "ORIGINALS_MANIFEST.json.sha256").is_file()


def test_every_original_is_preserved_byte_for_byte_and_reverified_after_the_write(
    submission: Path, into: Path
) -> None:
    """The claim is measured after the derived work, not asserted before it."""
    state = imp.plan(TRAIN_LABEL, submission, into)
    body = imp.execute(state, into)
    destination = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL) / imp.ORIGINALS_DIR

    assert body["preservation"]["byte_for_byte"] is True
    assert body["preservation"]["copies_reverified"] == len(state.originals)
    assert body["preservation"]["sources_reverified"] == len(state.originals)
    for row in state.originals:
        assert (destination / row.path).read_bytes() == (
            submission / spec.ORIGINALS_DIR / row.path
        ).read_bytes()


def test_a_source_edited_during_the_import_publishes_nothing(
    submission: Path, into: Path, monkeypatch
) -> None:
    state = imp.plan(TRAIN_LABEL, submission, into)
    target = submission / spec.ORIGINALS_DIR / state.originals[0].path

    real = imp._copy_originals

    def edit_then_copy(*args, **kwargs):
        result = real(*args, **kwargs)
        write_wav(target, peak=0.9)  # the drive changed under the run
        return result

    monkeypatch.setattr(imp, "_copy_originals", edit_then_copy)
    with pytest.raises(imp.Refused, match="no longer hash"):
        imp.execute(state, into)
    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    assert not (derived / imp.MANIFEST_FILENAME).exists()
    assert not (derived / imp.REPORT_FILENAME).exists()


# ── the report is immutable, and says nothing it should not ──────────────────


def test_re_running_a_finished_import_does_not_rewrite_the_report(
    submission: Path, into: Path
) -> None:
    state = imp.plan(TRAIN_LABEL, submission, into)
    first = imp.execute(state, into)
    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    before = (derived / imp.REPORT_FILENAME).read_bytes()

    # The duplicate check is what an operator meets; reach past it to prove the
    # report itself refuses to be rewritten with identical content.
    (derived / imp.MANIFEST_FILENAME).unlink()
    again = imp.plan(TRAIN_LABEL, submission, into)
    second = imp.execute(again, into)

    assert second["content_sha256"] == first["content_sha256"]
    assert (derived / imp.REPORT_FILENAME).read_bytes() == before


def test_a_report_describing_a_different_import_is_refused_not_overwritten(
    submission: Path, into: Path
) -> None:
    state = imp.plan(TRAIN_LABEL, submission, into)
    imp.execute(state, into)
    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    # Reach past the duplicate-speaker refusal, which is what an operator meets
    # first, to the report's own immutability underneath it.
    (derived / imp.MANIFEST_FILENAME).unlink()

    # A different submission for the same label: one take re-recorded.
    write_wav(submission / spec.ORIGINALS_DIR / "positive_normal" / (
        f"{spec.WAKE_PHRASE_SLUG}_normal_001.wav"
    ), peak=0.5)
    write_checksums(submission)

    changed = imp.plan(TRAIN_LABEL, submission, into)
    with pytest.raises(imp.Refused, match="already describes a different import"):
        imp.execute(changed, into)


def test_the_report_names_no_host_path_and_no_identity_beyond_the_label(
    submission: Path, into: Path
) -> None:
    state = imp.plan(TRAIN_LABEL, submission, into)
    body = imp.execute(state, into)
    text = json.dumps(body)

    assert imp._privacy_problems(body) == []
    assert str(submission) not in text
    assert str(into) not in text
    assert "/mnt/" not in text
    # The metadata form is free text a person filled in. Its digest travels; its
    # prose does not.
    for value in valid_metadata(TRAIN_LABEL).values():
        if isinstance(value, str) and value != TRAIN_LABEL:
            assert value not in text
    assert body["metadata"]["sha256"] == freeze_manifest.sha256_file(
        submission / spec.METADATA_FILE
    )
    assert body["consent"]["sha256"] == freeze_manifest.sha256_file(
        submission / spec.CONSENT_FILE
    )


def test_the_privacy_scan_recognises_a_host_path_in_a_report() -> None:
    """Control: the scan above proves nothing if it matches nothing."""
    assert imp._privacy_problems({"root": "/mnt/g/some-capture-root"})
    assert imp._privacy_problems({"rows": [{"path": "C:\\takes\\01.wav"}]})
    assert imp._privacy_problems({"root": "/home/someone/audio"})
    assert imp._privacy_problems({"row": {"deep": {"p": "\\\\host\\share\\x"}}})
    assert imp._privacy_problems({"path": "originals/positive_normal/x.wav"}) == []


def test_no_end_of_an_import_may_be_inside_the_repository(
    submission: Path, into: Path
) -> None:
    # Reading from the checkout is refused outright: a submission there is
    # already one `git add -A` from being committed.
    with pytest.raises(imp.Refused, match="inside the repository"):
        imp.plan(TRAIN_LABEL, REPO / "scripts" / "wakeword", into)

    # Writing into it fails the privacy check, so a dry run says so instead of
    # raising, and the write phase refuses again before it copies anything.
    state = imp.plan(TRAIN_LABEL, submission, REPO / "scripts")
    assert "privacy" in failed(state)
    assert any("inside the repository" in problem for problem in problems_of(state, "privacy"))
    with pytest.raises(imp.Refused):
        imp.execute(state, REPO / "scripts")
    assert not (REPO / "scripts" / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)).exists()


def test_the_consent_file_is_never_copied_into_the_data_root(
    submission: Path, into: Path
) -> None:
    """Its digest is evidence; the scan itself stays where it was signed."""
    state = imp.plan(TRAIN_LABEL, submission, into)
    imp.execute(state, into)
    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    written = {path.name for path in derived.rglob("*") if path.is_file()}
    assert spec.CONSENT_FILE not in written
    assert spec.METADATA_FILE not in written


# ── discovery refuses rather than skips ──────────────────────────────────────


def test_a_file_that_matches_no_section_is_refused(submission: Path, into: Path) -> None:
    (submission / spec.ORIGINALS_DIR / "extra_takes").mkdir()
    write_wav(submission / spec.ORIGINALS_DIR / "extra_takes" / "take_001.wav")
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "discovery" in failed(state)
    assert any("extra_takes" in problem for problem in problems_of(state, "discovery"))


def test_a_recorder_autonamed_file_is_refused(submission: Path, into: Path) -> None:
    """Many recorder apps append their own counter: "Voice Memo 3"."""
    write_wav(submission / spec.ORIGINALS_DIR / "positive_normal" / "Voice Memo 3.wav")
    state = imp.plan(TRAIN_LABEL, submission, into)
    problems = problems_of(state, "discovery")
    assert any("Voice Memo 3.wav" in problem and "unrecognised" in problem for problem in problems)


def test_a_file_with_no_audio_extension_is_refused(submission: Path, into: Path) -> None:
    (submission / spec.ORIGINALS_DIR / "positive_normal" / "notes.txt").write_text(
        "the third take was better", encoding="utf-8"
    )
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert any(".txt" in problem for problem in problems_of(state, "discovery"))


def test_a_near_phrase_slug_outside_the_battery_is_refused_not_guessed(
    submission: Path, into: Path
) -> None:
    write_wav(submission / spec.ORIGINALS_DIR / "near_phrase" / "hey-alexa_001.wav")
    state = imp.plan(TRAIN_LABEL, submission, into)
    problems = problems_of(state, "discovery")
    assert any("hey-alexa" in problem and "refuses to guess" in problem for problem in problems)


def test_sync_client_litter_is_named_rather_than_silently_dropped(
    submission: Path, into: Path
) -> None:
    """Recognised by the same list the freezer skips, and reported either way.

    What is discovered here and what ``freeze_manifest`` records have to be the
    same set of files, so litter is skipped identically and named in the report
    rather than dropped. The package layout is stricter than that on its own
    account — ``validate_speaker_submission`` refuses any file in a condition
    folder that is not a correctly named take, litter included — so the import
    still fails, on that check and not by silently ignoring a file.
    """
    (submission / spec.ORIGINALS_DIR / "positive_normal" / ".DS_Store").write_bytes(b"junk")
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "positive_normal/.DS_Store" in state.litter
    assert len(state.originals) == spec.audio_files_per_speaker()
    assert failed(state) and failed(state) <= {"submission_layout", "transfer_digests"}


def test_litter_in_the_older_layout_is_not_copied_and_does_not_stop_the_import(
    tmp_path: Path, into: Path
) -> None:
    root = build_prior_flat_submission(tmp_path / PRIOR_TRAIN_LABEL, PRIOR_TRAIN_LABEL)
    (root / ".DS_Store").write_bytes(b"junk")
    state = imp.plan(PRIOR_TRAIN_LABEL, root, into)
    assert ".DS_Store" in state.litter
    assert state.ok, {check.name: check.problems for check in state.failures}
    imp.execute(state, into)
    destination = into / (imp.SPEAKER_DIR_PREFIX + PRIOR_TRAIN_LABEL) / imp.ORIGINALS_DIR
    assert not (destination / ".DS_Store").exists()
    manifest = freeze_manifest.load(destination.parent / imp.MANIFEST_FILENAME)
    assert manifest["file_count"] == len(state.originals)


def test_a_missing_section_is_refused(submission: Path, into: Path) -> None:
    shutil.rmtree(submission / spec.ORIGINALS_DIR / "positive_farfield_loud")
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "sections" in failed(state)
    assert any(
        "positive_farfield_loud" in problem for problem in problems_of(state, "sections")
    )


def test_too_few_takes_in_a_section_is_refused(submission: Path, into: Path) -> None:
    directory = submission / spec.ORIGINALS_DIR / "positive_loud"
    sorted(directory.glob("*.wav"))[0].unlink()
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert any("positive_loud/" in problem for problem in problems_of(state, "sections"))


def test_too_few_noise_sources_is_refused(submission: Path, into: Path) -> None:
    shutil.rmtree(submission / spec.ORIGINALS_DIR / "positive_noise_kitchen")
    metadata = valid_metadata(TRAIN_LABEL)
    metadata["noise_sources_used"] = ["tv"]
    write_metadata(submission, metadata)
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert any("noise" in problem for problem in problems_of(state, "sections"))


def test_a_section_satisfied_only_by_a_dead_take_is_refused(
    submission: Path, into: Path
) -> None:
    """An excluded take must not count toward a section's required takes."""
    directory = submission / spec.ORIGINALS_DIR / "positive_quiet"
    for path in sorted(directory.glob("*.wav")):
        write_wav(path, silent=True)
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert any("positive_quiet/" in problem for problem in problems_of(state, "sections"))


# ── labels come from the spec ────────────────────────────────────────────────


def test_every_label_is_the_one_the_spec_gives_that_phrase(
    submission: Path, into: Path
) -> None:
    state = imp.plan(TRAIN_LABEL, submission, into)
    for item in spec.NEAR_PHRASE_ITEMS:
        row = row_for(state, f"near_phrase/{item.slug}_001.wav")
        assert row.label == imp.LABEL_VALUES[item.label], item.text
        assert row.phrase == item.text
        assert row.category == "near_phrase"
    for section in spec.POSITIVE_SECTIONS:
        row = row_for(
            state, f"{section.directory}/{spec.WAKE_PHRASE_SLUG}_{section.condition}_001.wav"
        )
        assert row.label == imp.LABEL_VALUES[spec.POSITIVE]
        assert row.category == section.directory


def test_the_wake_phrase_recorded_inside_the_battery_is_still_a_positive(
    submission: Path, into: Path
) -> None:
    """The split spellings of the wake word are positives, and the spec says so."""
    positives = [
        item for item in spec.NEAR_PHRASE_ITEMS if item.label == spec.POSITIVE
    ]
    assert positives, "the battery no longer contains the wake word's own spellings"
    state = imp.plan(TRAIN_LABEL, submission, into)
    for item in positives:
        assert row_for(state, f"near_phrase/{item.slug}_001.wav").label == 1


# ── the records that travel with the recordings ──────────────────────────────


def test_a_missing_consent_record_is_refused(submission: Path, into: Path) -> None:
    (submission / spec.CONSENT_FILE).unlink()
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "consent" in failed(state)


def test_an_empty_consent_record_is_refused(submission: Path, into: Path) -> None:
    (submission / spec.CONSENT_FILE).write_bytes(b"")
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "consent" in failed(state)
    assert any("empty" in problem for problem in problems_of(state, "consent"))


def test_metadata_that_does_not_parse_is_refused(submission: Path, into: Path) -> None:
    (submission / spec.METADATA_FILE).write_text("{not json", encoding="utf-8")
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "metadata" in failed(state)
    assert any("well-formed" in problem for problem in problems_of(state, "metadata"))


def test_a_missing_required_metadata_field_is_refused(
    submission: Path, into: Path
) -> None:
    metadata = valid_metadata(TRAIN_LABEL)
    del metadata["device_make_model"]
    write_metadata(submission, metadata)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert any("device_make_model" in problem for problem in problems_of(state, "metadata"))


def test_an_unfilled_template_placeholder_is_refused(submission: Path, into: Path) -> None:
    metadata = valid_metadata(TRAIN_LABEL)
    metadata["room_name"] = "<e.g. living room>"
    write_metadata(submission, metadata)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert any("placeholder" in problem for problem in problems_of(state, "metadata"))


def test_metadata_naming_another_speaker_is_refused(submission: Path, into: Path) -> None:
    metadata = valid_metadata(TRAIN_LABEL)
    metadata["speaker_id"] = SEALED_LABEL
    write_metadata(submission, metadata)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "metadata" in failed(state)


def test_a_folder_named_for_another_speaker_is_refused(
    template: Path, tmp_path: Path, into: Path
) -> None:
    target = tmp_path / SEALED_LABEL
    shutil.copytree(template, target)
    with pytest.raises(imp.Refused, match="states"):
        imp.plan(TRAIN_LABEL, target, into)


def test_the_transfer_listing_is_verified_and_not_only_parsed(
    submission: Path, into: Path
) -> None:
    """A digest recomputed where it was written proves nothing; this runs after."""
    path = submission / spec.CHECKSUM_FILE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    broken = f"{'0' * 64}  {lines[0].split('  ', 1)[1]}"
    path.write_text("".join([broken, *lines[1:]]), encoding="utf-8")
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "transfer_digests" in failed(state)
    assert any("different digest" in problem for problem in problems_of(state, "transfer_digests"))


def test_a_missing_transfer_listing_is_refused_for_this_round(
    submission: Path, into: Path
) -> None:
    (submission / spec.CHECKSUM_FILE).unlink()
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "transfer_digests" in failed(state)


# ── format facts, and their separation from the decoder ──────────────────────


def test_the_format_report_is_produced_without_any_decoder(
    submission: Path, into: Path, monkeypatch
) -> None:
    """A decoder answers with what it converted to; a format report must not."""
    def refuse_to_decode(*args, **kwargs):
        raise AssertionError("format facts must not come from a decoder")

    monkeypatch.setattr(imp, "decode", refuse_to_decode)
    path = submission / spec.ORIGINALS_DIR / "positive_normal" / (
        f"{spec.WAKE_PHRASE_SLUG}_normal_001.wav"
    )
    facts = imp.format_facts(path)
    assert facts.read_by.startswith("container-parser:")
    assert facts.sample_rate_hz == RATE
    assert facts.channels == 1
    assert facts.duration_s == pytest.approx(TAKE_SECONDS)


def test_a_decoder_that_disagrees_with_the_container_is_refused(
    submission: Path, into: Path, monkeypatch
) -> None:
    real = imp.decode

    def wrong_rate(path: Path, facts):
        samples, _, decoded_by = real(path, facts)
        return samples, 8000, decoded_by

    monkeypatch.setattr(imp, "decode", wrong_rate)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "format" in failed(state)
    assert any(
        "the decoder produced" in problem for problem in problems_of(state, "format")
    )


def test_a_take_below_the_production_sample_rate_is_refused(
    submission: Path, into: Path
) -> None:
    target = submission / spec.ORIGINALS_DIR / "positive_normal" / (
        f"{spec.WAKE_PHRASE_SLUG}_normal_002.wav"
    )
    write_wav(target, rate=8000)
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "format" in failed(state)
    assert any("8000 Hz" in problem for problem in problems_of(state, "format"))
    assert "below_production_rate" in row_for(state, "positive_normal/" + target.name).findings


def test_a_truncated_container_is_refused_as_corruption(
    submission: Path, into: Path
) -> None:
    target = submission / spec.ORIGINALS_DIR / "positive_slow" / (
        f"{spec.WAKE_PHRASE_SLUG}_slow_003.wav"
    )
    write_wav(target, truncate_bytes=4000)
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "format" in failed(state)
    assert any("truncated" in problem for problem in problems_of(state, "format"))


def test_a_renamed_container_is_refused(submission: Path, into: Path) -> None:
    """An ``.m4a`` saved as ``.wav`` is a file whose name lies about its content."""
    directory = submission / spec.ORIGINALS_DIR / "positive_fast"
    target = directory / f"{spec.WAKE_PHRASE_SLUG}_fast_004.wav"
    body = bytearray(target.read_bytes())
    body[4:8] = b"ftyp"
    body[0:4] = b"\x00\x00\x00\x18"
    target.write_bytes(bytes(body))
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "format" in failed(state)


def test_a_container_no_parser_reads_is_refused_rather_than_described(
    submission: Path, into: Path
) -> None:
    target = submission / spec.ORIGINALS_DIR / "positive_fast" / (
        f"{spec.WAKE_PHRASE_SLUG}_fast_005.wav"
    )
    target.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 512)  # an MP3 frame header
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert any("no container this tool parses" in p for p in problems_of(state, "format"))


#: One row per container family this tool parses, other than WAVE: the codec to
#: encode a generated tone with, and where the level facts have to come from.
#: A phone writes the first of these; the rest are in the spec's extension list,
#: so a submission can arrive in any of them.
CONTAINER_CASES = (
    ("take.m4a", "aac", "iso-bmff", "pyav "),
    ("take.flac", "flac", "flac", "pyav "),
    ("take.aiff", "pcm_s16be", "aiff", "container-payload:aiff-pcm"),
    ("take.caf", "pcm_s16le", "caf", "pyav "),
)


def require_pyav():
    """The decoder, or a failure naming the command that installs it.

    This was ``pytest.importorskip`` while pyav was in no dependency group at
    all, and a skip was the honest thing then. It is no longer: pyav is pinned in
    the ``wakeword-ingest`` extra (``uv sync --extra wakeword-ingest``), which
    exists precisely so the machine an operator ingests a drive on is installed
    from the project's own metadata. A skip here would be indistinguishable from
    a pass on the one machine where the decoder has to be present — the one with
    a speaker's only recording plugged into it.

    ``tests/tools/test_wakeword_compressed_ingestion.py`` is where the pin, the
    lock entry and the format submissions actually arrive in are asserted.
    """
    try:
        import av
    except ImportError as exc:  # pragma: no cover - the state the pin forbids
        pytest.fail(
            "pyav is not importable. It is a pinned dependency of the "
            "[wakeword-ingest] extra, not a host detail: install it with "
            f"`uv sync --extra wakeword-ingest`. ImportError: {exc}"
        )
    return av


@pytest.mark.parametrize(("name", "codec", "container", "decoder"), CONTAINER_CASES)
def test_the_container_parsers_read_what_a_real_encoder_wrote(
    tmp_path: Path, name: str, codec: str, container: str, decoder: str
) -> None:
    """Real coverage for the non-WAVE parsers, on generated payloads only.

    Encoded here with pyav from a generated tone, so each header parser is
    exercised against a file an actual muxer wrote rather than against bytes this
    test hand-assembled — a hand-assembled fixture would only prove the parser
    agrees with the test's idea of the format.
    """
    av = require_pyav()
    source = tmp_path / "generated.wav"
    write_wav(source, seconds=1.0, rate=48000)
    target = tmp_path / name
    with av.open(str(source)) as reader, av.open(str(target), "w") as writer:
        stream = writer.add_stream(codec, rate=48000)
        for frame in reader.decode(reader.streams.audio[0]):
            frame.pts = None
            for packet in stream.encode(frame):
                writer.mux(packet)
        for packet in stream.encode(None):
            writer.mux(packet)

    facts = imp.format_facts(target)
    assert facts.container == container
    assert facts.sample_rate_hz == 48000
    assert facts.read_by.startswith("container-parser:")
    samples, rate, decoded_by = imp.decode(target, facts)
    levels = imp.level_facts(samples, rate, decoded_by)
    assert decoded_by.startswith(decoder)
    # The header the parser read and the payload the decoder produced have to
    # describe the same recording. Asserted rather than assumed: the channel
    # count is the field an encoder is most likely to have chosen itself.
    assert facts.channels == levels.channels
    assert facts.sample_rate_hz == levels.sample_rate_hz
    assert facts.duration_s == pytest.approx(levels.duration_s, abs=0.05)
    assert levels.samples > 0
    assert -30 < levels.peak_dbfs < 0


# ── levels: reported, and never a reason to drop a recording ─────────────────


def test_a_clipped_take_is_reported_and_kept(submission: Path, into: Path) -> None:
    target = submission / spec.ORIGINALS_DIR / "positive_loud" / (
        f"{spec.WAKE_PHRASE_SLUG}_loud_001.wav"
    )
    write_wav(target, peak=4.0)  # driven well past full scale
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    row = row_for(state, "positive_loud/" + target.name)
    assert "clipping" in row.findings
    assert row.levels is not None and row.levels.clipping_runs > 0
    assert row.levels.full_scale_samples > 0
    assert not row.excluded
    assert state.ok, {check.name: check.problems for check in state.failures}


def test_quiet_distant_noisy_and_fast_takes_are_never_excluded(
    submission: Path, into: Path
) -> None:
    """E001's precedent, made mechanical: none of these is grounds for exclusion."""
    originals = submission / spec.ORIGINALS_DIR
    quiet = originals / "positive_quiet" / f"{spec.WAKE_PHRASE_SLUG}_quiet_001.wav"
    write_wav(quiet, peak=0.002)
    far = originals / "positive_farfield" / f"{spec.WAKE_PHRASE_SLUG}_farfield_001.wav"
    write_wav(far, peak=0.004)
    fast = originals / "positive_fast" / f"{spec.WAKE_PHRASE_SLUG}_fast_001.wav"
    write_wav(fast, seconds=0.3)
    write_checksums(submission)

    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "quiet_take" in row_for(state, "positive_quiet/" + quiet.name).findings
    assert not row_for(state, "positive_quiet/" + quiet.name).excluded
    assert not row_for(state, "positive_farfield/" + far.name).excluded
    assert not row_for(state, "positive_fast/" + fast.name).excluded
    assert state.ok, {check.name: check.problems for check in state.failures}
    assert [row for row in state.originals if row.excluded] == []


def test_digital_silence_is_excluded_with_its_rule_and_still_kept(
    submission: Path, into: Path
) -> None:
    directory = submission / spec.ORIGINALS_DIR / "near_phrase"
    target = directory / "hey-google_001.wav"
    write_wav(target, silent=True)
    # A spare take of the same phrase, so the section still has its three usable
    # ones: the point here is the exclusion, not the section rule.
    write_wav(directory / "hey-google_004.wav")
    write_checksums(submission)

    state = imp.plan(TRAIN_LABEL, submission, into)
    row = row_for(state, "near_phrase/" + target.name)
    assert row.excluded
    assert row.exclusion_rule == "digital_silence"
    assert row.exclusion_reason
    # Kept: same label, same category, same bytes, still frozen.
    assert row.label == imp.LABEL_VALUES[spec.NEGATIVE]
    assert row.category == "near_phrase"
    assert state.ok, {check.name: check.problems for check in state.failures}

    body = imp.execute(state, into)
    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    assert (derived / imp.ORIGINALS_DIR / row.path).read_bytes() == target.read_bytes()
    manifest = freeze_manifest.load(derived / imp.MANIFEST_FILENAME)
    assert row.path in {entry["path"] for entry in manifest["files"]}
    assert body["exclusions"]["excluded"] == [
        {
            "path": row.path,
            "rule": "digital_silence",
            "reason": row.exclusion_reason,
            "label": 0,
            "category": "near_phrase",
        }
    ]


def test_a_take_with_no_payload_is_excluded_under_its_own_rule(
    submission: Path, into: Path
) -> None:
    target = submission / spec.ORIGINALS_DIR / "near_phrase" / "hey-siri_001.wav"
    write_wav(target, empty_payload=True)
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    row = row_for(state, "near_phrase/" + target.name)
    assert row.exclusion_rule == "no_audio_payload"


def test_an_undecodable_payload_is_excluded_and_a_missing_decoder_is_not(
    submission: Path, into: Path, monkeypatch
) -> None:
    """The two failures mean opposite things and must not share a verdict."""
    path = "positive_normal/" + f"{spec.WAKE_PHRASE_SLUG}_normal_003.wav"
    real = imp.decode

    def fail_one(target: Path, facts):
        if target.as_posix().endswith(path):
            raise imp.DecodeError("simulated codec failure")
        return real(target, facts)

    monkeypatch.setattr(imp, "decode", fail_one)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert row_for(state, path).exclusion_rule == "undecodable_payload"

    def no_decoder(target: Path, facts):
        if target.as_posix().endswith(path):
            raise imp.DecoderUnavailable("pyav is not installed")
        return real(target, facts)

    monkeypatch.setattr(imp, "decode", no_decoder)
    state = imp.plan(TRAIN_LABEL, submission, into)
    row = row_for(state, path)
    assert not row.excluded
    assert row.problems
    assert "levels" in failed(state)


def test_every_exclusion_cites_a_predeclared_ingestion_rule() -> None:
    codes = {rule.code for rule in imp.PREDECLARED_EXCLUSIONS}
    assert {"undecodable_payload", "no_audio_payload", "digital_silence"} <= codes
    stages = {rule.stage for rule in imp.PREDECLARED_EXCLUSIONS}
    assert stages == {"ingestion", "adjudication"}
    # E001's precedent is predeclared here and applied by the derivation, which
    # is the only stage that has a transcript.
    adjudication = [r for r in imp.PREDECLARED_EXCLUSIONS if r.stage == "adjudication"]
    assert [rule.applies_to for rule in adjudication] == [spec.POSITIVE, spec.POSITIVE]
    assert {condition for condition, _ in imp.NEVER_EXCLUDED} >= {
        "quiet",
        "distant",
        "noisy",
        "fast",
        "clipped",
    }


def test_an_exclusion_outside_the_predeclared_table_fails_the_import(
    submission: Path, into: Path, monkeypatch
) -> None:
    """Control for the exclusion check: an invented reason must not pass."""
    real = imp.describe

    def invent(root: Path, row):
        described = real(root, row)
        if row.path.startswith("positive_normal/"):
            return imp._replace(
                described, excluded=True, exclusion_rule="sounded_bad", exclusion_reason="",
            )
        return described

    monkeypatch.setattr(imp, "describe", invent)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "exclusions" in failed(state)
    assert any("sounded_bad" in problem for problem in problems_of(state, "exclusions"))


# ── roles are looked up, never inferred, never overridable ───────────────────


def test_the_registry_agrees_with_both_predeclarations() -> None:
    table = imp.registry()
    assert set(table) == {label for labels in round8_config.SPLITS.values() for label in labels}
    for entry in spec.SPEAKER_ASSIGNMENTS:
        assert table[entry.label].role == entry.role
    for role, labels in round8_config.SPLITS.items():
        for label in labels:
            assert table[label].role == imp.CONFIG_ROLE_TO_ROLE[role]
    assert set(imp.ROLE_SPLITS) == set(spec.ROLES)
    assert set(spec.ROLES) == set(freeze_manifest.USAGES)


def test_a_label_with_no_predeclared_role_is_refused(
    template: Path, tmp_path: Path, into: Path
) -> None:
    target = tmp_path / UNASSIGNED_LABEL
    shutil.copytree(template, target)
    metadata = valid_metadata(UNASSIGNED_LABEL)
    write_metadata(target, metadata)
    write_checksums(target)
    with pytest.raises(imp.Refused, match="no predeclared role"):
        imp.plan(UNASSIGNED_LABEL, target, into)


def test_a_sealed_speaker_is_refused_for_anything_but_a_sealed_use() -> None:
    imp.assert_usable_for(TRAIN_LABEL, "training")
    imp.assert_usable_for(VALIDATION_LABEL, "validation")
    imp.assert_usable_for(SEALED_LABEL, "evaluation")
    for purpose in ("training", "validation"):
        with pytest.raises(freeze_manifest.SealedDatasetError):
            imp.assert_usable_for(SEALED_LABEL, purpose)
    with pytest.raises(ValueError):
        imp.assert_usable_for(TRAIN_LABEL, "evaluation")


def test_the_command_line_cannot_set_a_role_a_split_or_a_usage(capsys) -> None:
    with pytest.raises(SystemExit):
        imp.main(["--help"])
    helptext = capsys.readouterr().out
    for flag in ("--role", "--split", "--usage", "--sealed", "--force"):
        assert flag not in helptext
    assert "--dry-run" in helptext


def test_the_role_is_written_into_the_manifest_the_registry_decided(
    submission: Path, into: Path
) -> None:
    # Built for the validation speaker, so the split differs from the train one.
    target = submission.parent / VALIDATION_LABEL
    build_package_submission(target, VALIDATION_LABEL)
    state = imp.plan(VALIDATION_LABEL, target, into)
    assert state.ok, {check.name: check.problems for check in state.failures}
    imp.execute(state, into)
    manifest = freeze_manifest.load(
        into / (imp.SPEAKER_DIR_PREFIX + VALIDATION_LABEL) / imp.MANIFEST_FILENAME
    )
    assert manifest["usage"] == spec.ROLE_VALIDATION
    assert manifest["split"] == "validation"
    freeze_manifest.assert_usable_for(manifest, "validation")
    with pytest.raises(ValueError):
        freeze_manifest.assert_usable_for(manifest, "training")


# ── nothing is imported twice, and nothing is shared ────────────────────────


def test_importing_the_same_speaker_twice_is_refused(
    submission: Path, into: Path
) -> None:
    imp.execute(imp.plan(TRAIN_LABEL, submission, into), into)
    again = imp.plan(TRAIN_LABEL, submission, into)
    assert "duplicate" in failed(again)
    with pytest.raises(imp.Refused):
        imp.execute(again, into)


def test_a_frozen_derivation_manifest_also_counts_as_imported(
    submission: Path, into: Path
) -> None:
    """The derivation's ``MANIFEST.json`` lands in the same directory."""
    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    derived.mkdir()
    (derived / "MANIFEST.json").write_text(
        json.dumps({"split": "train", "usage": "TRAINING ONLY.", "files": []}),
        encoding="utf-8",
    )
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "duplicate" in failed(state)


def test_the_same_recording_under_two_names_is_refused(
    submission: Path, into: Path
) -> None:
    directory = submission / spec.ORIGINALS_DIR / "near_phrase"
    shutil.copyfile(directory / "hey-there_001.wav", directory / "hey-there_004.wav")
    write_checksums(submission)
    state = imp.plan(TRAIN_LABEL, submission, into)
    assert "reuse" in failed(state)
    assert any("byte-identical" in problem for problem in problems_of(state, "reuse"))


def test_a_recording_already_imported_for_another_speaker_is_refused(
    submission: Path, into: Path, tmp_path: Path
) -> None:
    imp.execute(imp.plan(TRAIN_LABEL, submission, into), into)

    second = tmp_path / SECOND_TRAIN_LABEL
    build_package_submission(second, SECOND_TRAIN_LABEL)
    shared = second / spec.ORIGINALS_DIR / "positive_normal" / (
        f"{spec.WAKE_PHRASE_SLUG}_normal_001.wav"
    )
    shutil.copyfile(
        submission / spec.ORIGINALS_DIR / "positive_normal" / shared.name, shared
    )
    write_checksums(second)

    state = imp.plan(SECOND_TRAIN_LABEL, second, into)
    assert "reuse" in failed(state)
    problems = problems_of(state, "reuse")
    assert any(TRAIN_LABEL in problem for problem in problems)


def test_a_recording_shared_across_a_seal_says_so(
    submission: Path, into: Path, tmp_path: Path
) -> None:
    imp.execute(imp.plan(TRAIN_LABEL, submission, into), into)

    sealed = tmp_path / SEALED_LABEL
    build_package_submission(sealed, SEALED_LABEL)
    shared = sealed / spec.ORIGINALS_DIR / "positive_normal" / (
        f"{spec.WAKE_PHRASE_SLUG}_normal_002.wav"
    )
    shutil.copyfile(
        submission / spec.ORIGINALS_DIR / "positive_normal" / shared.name, shared
    )
    write_checksums(sealed)

    state = imp.plan(SEALED_LABEL, sealed, into)
    assert "reuse" in failed(state)
    assert any("crosses a seal" in problem for problem in problems_of(state, "reuse"))


def test_a_manifest_beside_this_one_that_cannot_be_attributed_is_refused(
    submission: Path, into: Path
) -> None:
    other = into / (imp.SPEAKER_DIR_PREFIX + SEALED_LABEL)
    other.mkdir()
    (other / "MANIFEST.json").write_text(
        json.dumps({"split": "some_new_split", "files": []}), encoding="utf-8"
    )
    with pytest.raises(imp.Refused, match="what the speaker may be used for"):
        imp.plan(TRAIN_LABEL, submission, into)


def test_a_manifest_whose_split_and_usage_disagree_is_refused(
    submission: Path, into: Path
) -> None:
    other = into / (imp.SPEAKER_DIR_PREFIX + SEALED_LABEL)
    other.mkdir()
    (other / "MANIFEST.json").write_text(
        json.dumps(
            {
                "split": "qualification_sealed",
                "usage": "TRAINING ONLY. Never validation.",
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(imp.Refused, match="does not mention"):
        imp.plan(TRAIN_LABEL, submission, into)


# ── resumability ─────────────────────────────────────────────────────────────


def test_an_interrupted_copy_leaves_no_manifest_and_the_rerun_finishes_it(
    submission: Path, into: Path, monkeypatch
) -> None:
    state = imp.plan(TRAIN_LABEL, submission, into)
    real = imp._copy_and_hash
    calls = {"n": 0}

    def interrupt_after_three(source: Path, target: Path) -> str:
        if calls["n"] >= 3:
            raise KeyboardInterrupt("simulated interruption")
        calls["n"] += 1
        return real(source, target)

    monkeypatch.setattr(imp, "_copy_and_hash", interrupt_after_three)
    with pytest.raises(KeyboardInterrupt):
        imp.execute(state, into)

    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    assert not (derived / imp.MANIFEST_FILENAME).exists()
    assert not (derived / imp.REPORT_FILENAME).exists()
    journal = json.loads((derived / imp.JOURNAL_FILENAME).read_text(encoding="utf-8"))
    assert journal["state"] == "copying"

    published = [
        path
        for path in (derived / imp.ORIGINALS_DIR).rglob("*")
        if path.is_file()
    ]
    assert len(published) == 3
    # Nothing partial reached the tree that gets frozen.
    planned = {row.path: row.sha256 for row in state.originals}
    for path in published:
        relative = path.relative_to(derived / imp.ORIGINALS_DIR).as_posix()
        assert freeze_manifest.sha256_file(path) == planned[relative]

    monkeypatch.setattr(imp, "_copy_and_hash", real)
    resumed = imp.plan(TRAIN_LABEL, submission, into)
    assert resumed.ok, {check.name: check.problems for check in resumed.failures}
    body = imp.execute(resumed, into)
    assert body["run"]["already_present"] == 3
    assert body["run"]["copied"] == len(state.originals) - 3
    assert body["preservation"]["byte_for_byte"] is True
    assert freeze_manifest.verify(
        derived / imp.MANIFEST_FILENAME, derived / imp.ORIGINALS_DIR, strict=True
    )["passed"]
    assert (
        json.loads((derived / imp.JOURNAL_FILENAME).read_text(encoding="utf-8"))["state"]
        == "published"
    )


def test_a_crash_before_the_manifest_is_published_leaves_none_at_all(
    submission: Path, into: Path, monkeypatch
) -> None:
    """The manifest is the file that says "this speaker is imported".

    It is frozen into staging and moved into place by one atomic rename, after
    its sidecars, so a crash anywhere in the write phase leaves no manifest
    rather than one that reads as complete. If it were frozen straight to its
    real name, this crash would leave a manifest behind and the rerun below
    would refuse the speaker as already imported — permanently.
    """
    state = imp.plan(TRAIN_LABEL, submission, into)
    real = os.replace

    def crash_on_the_manifest(source, destination):
        if Path(destination).name == imp.MANIFEST_FILENAME:
            raise OSError("simulated crash while publishing")
        return real(source, destination)

    monkeypatch.setattr(imp.os, "replace", crash_on_the_manifest)
    with pytest.raises(OSError, match="simulated crash"):
        imp.execute(state, into)

    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    assert not (derived / imp.MANIFEST_FILENAME).exists()

    monkeypatch.setattr(imp.os, "replace", real)
    resumed = imp.plan(TRAIN_LABEL, submission, into)
    assert "duplicate" not in failed(resumed), problems_of(resumed, "duplicate")
    imp.execute(resumed, into)
    assert freeze_manifest.verify(
        derived / imp.MANIFEST_FILENAME, derived / imp.ORIGINALS_DIR, strict=True
    )["passed"]


def test_a_killed_process_leaves_nothing_that_reads_as_complete(
    submission: Path, into: Path
) -> None:
    """The same invariant, against a real SIGKILL rather than an exception."""
    process = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--speaker",
            TRAIN_LABEL,
            "--submission",
            str(submission),
            "--into",
            str(into),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    derived = into / (imp.SPEAKER_DIR_PREFIX + TRAIN_LABEL)
    originals = derived / imp.ORIGINALS_DIR
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if originals.is_dir() and any(
                path.is_file() for path in originals.rglob("*")
            ):
                break
            time.sleep(0.02)
    finally:
        process.kill()
        process.wait(timeout=30)

    assert not (derived / imp.MANIFEST_FILENAME).exists(), (
        "a killed import left a manifest, which reads as a completed import"
    )
    for path in originals.rglob("*") if originals.is_dir() else ():
        if path.is_file():
            assert path.suffix == ".wav", f"a partial file was published: {path.name}"

    resumed = imp.plan(TRAIN_LABEL, submission, into)
    assert resumed.ok, {check.name: check.problems for check in resumed.failures}
    body = imp.execute(resumed, into)
    assert body["preservation"]["byte_for_byte"] is True
    assert freeze_manifest.verify(
        derived / imp.MANIFEST_FILENAME, derived / imp.ORIGINALS_DIR, strict=True
    )["passed"]


def test_resuming_with_a_different_submission_is_refused(
    submission: Path, into: Path, monkeypatch
) -> None:
    state = imp.plan(TRAIN_LABEL, submission, into)
    real = imp._copy_and_hash
    calls = {"n": 0}

    def interrupt_after_two(source: Path, target: Path) -> str:
        if calls["n"] >= 2:
            raise KeyboardInterrupt("simulated interruption")
        calls["n"] += 1
        return real(source, target)

    monkeypatch.setattr(imp, "_copy_and_hash", interrupt_after_two)
    with pytest.raises(KeyboardInterrupt):
        imp.execute(state, into)
    monkeypatch.setattr(imp, "_copy_and_hash", real)

    write_wav(
        submission / spec.ORIGINALS_DIR / "positive_slow" / (
            f"{spec.WAKE_PHRASE_SLUG}_slow_001.wav"
        ),
        peak=0.7,
    )
    write_checksums(submission)
    changed = imp.plan(TRAIN_LABEL, submission, into)
    with pytest.raises(imp.Refused, match="interrupted import of different work"):
        imp.execute(changed, into)


# ── the two speakers recorded before this round ─────────────────────────────


def test_the_flat_session_layout_imports_as_train_only(
    tmp_path: Path, into: Path
) -> None:
    """E001's shape: one continuous recording per section, no ``originals/``."""
    root = build_prior_flat_submission(tmp_path / PRIOR_TRAIN_LABEL, PRIOR_TRAIN_LABEL)
    state = imp.plan(PRIOR_TRAIN_LABEL, root, into)
    assert state.layout.name == imp.LAYOUT_PRIOR
    assert state.ok, {check.name: check.problems for check in state.failures}
    assert len(state.originals) == len(PRIOR_FLAT_SECTIONS)
    assert {row.category for row in state.originals} == set(imp.PRIOR_REQUIRED_CATEGORIES)

    imp.execute(state, into)
    manifest = freeze_manifest.load(
        into / (imp.SPEAKER_DIR_PREFIX + PRIOR_TRAIN_LABEL) / imp.MANIFEST_FILENAME
    )
    assert manifest["split"] == "train"
    assert manifest["usage"] == spec.ROLE_TRAINING
    freeze_manifest.assert_usable_for(manifest, "training")
    for purpose in ("validation", "evaluation"):
        with pytest.raises(ValueError):
            freeze_manifest.assert_usable_for(manifest, purpose)


def test_the_sealed_speaker_is_recognised_as_sealed_and_refused_for_any_other_use(
    tmp_path: Path, into: Path
) -> None:
    root = build_prior_group_submission(tmp_path / PRIOR_SEALED_LABEL, PRIOR_SEALED_LABEL)
    state = imp.plan(PRIOR_SEALED_LABEL, root, into)
    assert state.assignment.role == spec.ROLE_SEALED
    assert state.assignment.split == "eval_sealed"
    assert state.ok, {check.name: check.problems for check in state.failures}

    imp.execute(state, into)
    manifest = freeze_manifest.load(
        into / (imp.SPEAKER_DIR_PREFIX + PRIOR_SEALED_LABEL) / imp.MANIFEST_FILENAME
    )
    assert manifest["usage"] == spec.ROLE_SEALED
    freeze_manifest.assert_usable_for(manifest, "evaluation")
    for purpose in ("training", "validation"):
        with pytest.raises(freeze_manifest.SealedDatasetError):
            freeze_manifest.assert_usable_for(manifest, purpose)
        with pytest.raises(freeze_manifest.SealedDatasetError):
            imp.assert_usable_for(PRIOR_SEALED_LABEL, purpose)


def test_separately_recorded_near_phrases_stay_separate_files(
    tmp_path: Path, into: Path
) -> None:
    """The hard requirement: a subdirectory of takes is never concatenated."""
    root = build_prior_group_submission(tmp_path / PRIOR_SEALED_LABEL, PRIOR_SEALED_LABEL)
    state = imp.plan(PRIOR_SEALED_LABEL, root, into)
    members = [row for row in state.originals if row.section == "05_near_phrases"]
    assert len(members) == PRIOR_GROUP_PHRASES
    assert len({row.path for row in members}) == PRIOR_GROUP_PHRASES
    assert len({row.sha256 for row in members}) == PRIOR_GROUP_PHRASES
    assert all(row.category == "near_phrase" for row in members)
    assert all(row.phrase for row in members)

    imp.execute(state, into)
    derived = into / (imp.SPEAKER_DIR_PREFIX + PRIOR_SEALED_LABEL)
    frozen = {
        entry["path"]
        for entry in freeze_manifest.load(derived / imp.MANIFEST_FILENAME)["files"]
        if entry["path"].startswith("05_near_phrases/")
    }
    assert len(frozen) == PRIOR_GROUP_PHRASES
    for row in members:
        assert (derived / imp.ORIGINALS_DIR / row.path).is_file()


def test_a_positive_phrase_inside_the_near_miss_section_is_contested_not_relabelled(
    tmp_path: Path, into: Path
) -> None:
    """E002's precedent, and its policy: never deleted, never relabelled."""
    root = build_prior_group_submission(tmp_path / PRIOR_SEALED_LABEL, PRIOR_SEALED_LABEL)
    state = imp.plan(PRIOR_SEALED_LABEL, root, into)
    contested = [row for row in state.originals if row.contested]
    positives = [
        item
        for item in spec.NEAR_PHRASE_ITEMS[:PRIOR_GROUP_PHRASES]
        if item.label == spec.POSITIVE
    ]
    assert len(contested) == len(positives) > 0
    for row in contested:
        assert row.label == imp.LABEL_VALUES[spec.NEGATIVE]  # the section's label, kept
        assert not row.excluded
        assert "speaker_recording_spec" in row.contested_reason


def test_an_off_battery_phrase_name_is_refused_rather_than_guessed(
    tmp_path: Path, into: Path
) -> None:
    """Why E002's own twelve names could not be re-ingested under this grammar.

    Half of that session's near phrases ("Hey uta", "Open YouTube") are not rows
    of the battery the spec now fixes, and this tool has no way to decide what
    the detector must do with them. It says so instead of choosing.
    """
    root = build_prior_group_submission(tmp_path / PRIOR_SEALED_LABEL, PRIOR_SEALED_LABEL)
    write_wav(root / spec.ORIGINALS_DIR / "05_near_phrases" / "Hey uta.wav")
    write_checksums(root)
    state = imp.plan(PRIOR_SEALED_LABEL, root, into)
    assert "discovery" in failed(state)
    assert any("hey-uta" in problem for problem in problems_of(state, "discovery"))


def test_a_round_eight_speaker_may_not_use_the_older_layout(
    tmp_path: Path, into: Path
) -> None:
    root = build_prior_flat_submission(tmp_path / TRAIN_LABEL, TRAIN_LABEL)
    with pytest.raises(imp.Refused, match="may not submit"):
        imp.plan(TRAIN_LABEL, root, into, layout="prior")
    # ...and auto-detection does not hand it to them either.
    state = imp.plan(TRAIN_LABEL, root, into)
    assert state.layout.name == imp.LAYOUT_PACKAGE
    assert not state.ok


# ── agreement with the modules downstream ───────────────────────────────────


def _module_constant(path: Path, name: str):
    """Read one module-level constant without importing the module.

    ``round8_config.py`` reads ``build_dataset.PHRASE_END_JITTER`` this way and
    says why: importing that module pulls numpy and the whole feature pipeline in
    for one value. Same reason here, plus one more — those modules belong to
    other work packages, and a test that imports them fails for their reasons
    rather than for ours.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} no longer defines {name}")


def test_the_production_sample_rate_is_the_pipeline_s_own() -> None:
    assert imp.PRODUCTION_SAMPLE_RATE_HZ == _module_constant(
        WAKEWORD / "build_dataset.py", "SAMPLE_RATE"
    )


def test_every_split_written_here_is_one_the_dataset_builder_accepts() -> None:
    """A manifest declaring a split that stage does not bind is unbuildable."""
    downstream = _module_constant(WAKEWORD / "build_human_dataset.py", "SPEAKER_SPLITS")
    table = imp.registry()
    assert set(downstream) == set(table)
    for label, split in downstream.items():
        assert table[label].split == split, label
    assert set(imp.SPLIT_ROLES) >= set(downstream.values())


def test_the_frozen_human_manifests_agree_with_this_registry() -> None:
    """Compatibility with the two manifests frozen before this round.

    Metadata only: split, usage, the section names their ``labels`` block uses,
    and the digests of the originals they were derived from. No audio is opened,
    nothing is scored, and nothing is written — the sealed speaker's manifest is
    read for the same reason its split is quoted in ``round8_config.json``.

    Skipped without ``YOUTAB_WAKEWORD_FROZEN_HUMAN``, which is how CI runs.
    """
    root = os.environ.get(FROZEN_ROOT_ENV)
    if not root:
        pytest.skip(f"{FROZEN_ROOT_ENV} is not set; the frozen data root is local-only")
    data = Path(root)
    layout = imp.prior_layout()
    table = imp.registry()

    for label, expected_digest in FROZEN_MANIFEST_SHA256.items():
        path = data / (imp.SPEAKER_DIR_PREFIX + label) / "MANIFEST.json"
        assert path.is_file(), path.name
        assert freeze_manifest.sha256_file(path) == expected_digest, (
            f"the frozen manifest for {label} is not the one this evidence was taken "
            "against"
        )
        manifest = json.loads(path.read_text(encoding="utf-8"))

        # The registry, the manifest and the role reader all say the same thing.
        assert manifest["speaker"] == label
        assert manifest["split"] == table[label].split
        assert imp._manifest_role(path.name, manifest) == table[label].role

        # Every section that manifest was derived from is one this tool knows,
        # so the same discovery code would classify that tree.
        for section in manifest["labels"]:
            stem = Path(section).stem
            assert layout.section(stem) is not None, section

        # Every original is a top-level section file or a member of a group
        # directory, and the members are separate files rather than one
        # concatenation.
        assert manifest["files"], "the manifest lists no originals"
        members: dict[str, set[str]] = {}
        for entry in manifest["files"]:
            source = entry["source_file"]
            parts = source.split("/")
            assert len(parts) <= 2, source
            assert layout.section(Path(parts[0]).stem) is not None, source
            if len(parts) == 2:
                members.setdefault(parts[0], set()).add(parts[1])
        assert len(manifest["files"]) == len(
            {entry["source_sha256"] for entry in manifest["files"]}
        )

        if label == PRIOR_TRAIN_LABEL:
            # Flat: one continuous recording per section, no subdirectory.
            assert members == {}
        else:
            # The sealed speaker recorded its near phrases one file per phrase.
            # Supporting that without concatenating them is the requirement this
            # tool's group-directory handling exists for.
            assert set(members) == {"05_near_phrases"}
            assert len(members["05_near_phrases"]) == PRIOR_GROUP_PHRASES

    # The reuse ledger reads both of them, and every digest is attributed.
    known = imp.imported_originals(data)
    assert known
    assert {role for _, role, _ in known.values()} == {
        table[PRIOR_TRAIN_LABEL].role,
        table[PRIOR_SEALED_LABEL].role,
    }


def test_the_ledger_reads_both_manifest_shapes(submission: Path, into: Path) -> None:
    """Hermetic stand-in for the test above: the two shapes, side by side."""
    imp.execute(imp.plan(TRAIN_LABEL, submission, into), into)
    prior = into / (imp.SPEAKER_DIR_PREFIX + PRIOR_SEALED_LABEL)
    prior.mkdir()
    digest = "b" * 64
    (prior / "MANIFEST.json").write_text(
        json.dumps(
            {
                "speaker": PRIOR_SEALED_LABEL,
                "split": "eval_sealed",
                "usage": "SEALED EVALUATION ONLY. Never training.",
                "files": [{"source_file": "01_close_normal.m4a", "source_sha256": digest}],
            }
        ),
        encoding="utf-8",
    )
    known = imp.imported_originals(into)
    assert known[digest] == (PRIOR_SEALED_LABEL, spec.ROLE_SEALED, "MANIFEST.json")
    imported = {
        row.sha256 for row in imp.plan(TRAIN_LABEL, submission, into).originals
    }
    assert imported <= set(known)
    assert all(known[sha][1] == spec.ROLE_TRAINING for sha in imported)


# ── the command line ────────────────────────────────────────────────────────


def test_dry_run_writes_nothing_and_reports_every_requirement(
    submission: Path, into: Path, capsys
) -> None:
    code = imp.main(
        [
            "--speaker",
            TRAIN_LABEL,
            "--submission",
            str(submission),
            "--into",
            str(into),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out and "nothing was written" in out
    names = {check.name for check in imp.plan(TRAIN_LABEL, submission, into).checks}
    assert len(names) >= 13
    assert out.count("PASS") == len(names)
    assert list(into.iterdir()) == []


def test_dry_run_fails_loudly_when_a_requirement_fails(
    submission: Path, into: Path, capsys
) -> None:
    (submission / spec.CONSENT_FILE).unlink()
    code = imp.main(
        [
            "--speaker",
            TRAIN_LABEL,
            "--submission",
            str(submission),
            "--into",
            str(into),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "requirement(s) failed" in out
    assert list(into.iterdir()) == []


def test_the_command_imports_and_then_refuses_a_second_run(
    submission: Path, into: Path, capsys
) -> None:
    arguments = [
        "--speaker",
        TRAIN_LABEL,
        "--submission",
        str(submission),
        "--into",
        str(into),
    ]
    assert imp.main(arguments) == 0
    first = capsys.readouterr().out
    assert imp.MANIFEST_FILENAME in first
    assert imp.main(arguments) == 1
    assert "REFUSED" in capsys.readouterr().out
