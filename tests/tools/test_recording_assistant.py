"""Behavioural tests for ``scripts/wakeword/recording_assistant``.

Everything the recording assistant does that a machine can check without a
microphone, a display or a real speaker is exercised here: the compact plan and
its counts, the automatic filename/directory assignment (checked against the
very regexes ``validate_speaker_submission.py`` and ``import_speaker.py`` use),
per-take quality inspection, the SHA-256 manifest, the metadata form, resumable
progress reconciled against disk, and the record/keep/redo/replay/pause/resume
controller -- including the load-bearing rule that a written take contains only
the captured stream and never the guidance prompt.

Two integration tests close the loop against the real validator: a full-spec
folder assembled through ``core``'s writers validates green, and a compact
folder is shown to carry only the *intended* shortfalls (fewer takes, the
dropped far-field-loud section, and a speaker outside this round's assignment
table) with no naming, format, checksum or metadata error.

All fixtures live under ``tmp_path``; nothing here reads or writes a real
recording, and no per-speaker record is committed.
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import recording_assistant.compact_plan as compact_plan  # noqa: E402
import recording_assistant.core as core  # noqa: E402
import import_speaker  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402
import validate_speaker_submission as validator  # noqa: E402

# Labels named without the "speaker_id = E0nn" shape the no-human-data gate
# forbids in tracked text: the two compact-plan humans as bare literals, and the
# assigned label read out of the spec table (as the validator's own test does).
FIRST_COMPACT = "E001"
SECOND_COMPACT = "E002"
ASSIGNED = spec.SPEAKER_ASSIGNMENTS[0].label  # first training label, e.g. E003


# ── synthesis helpers ────────────────────────────────────────────────────────


def synth_sine(duration_s: float, rate: int = core.SAMPLE_RATE_HZ, amp: float = 0.3,
               freq: float = 220.0) -> np.ndarray:
    n = max(int(duration_s * rate), 0)
    t = np.arange(n) / rate
    return core.to_int16(amp * np.sin(2 * np.pi * freq * t))


def synth_clipped(duration_s: float, rate: int = core.SAMPLE_RATE_HZ) -> np.ndarray:
    """A tone driven well past full scale, then clamped -- audible clipping."""
    n = int(duration_s * rate)
    t = np.arange(n) / rate
    return core.to_int16(np.clip(3.0 * np.sin(2 * np.pi * 220.0 * t), -1.0, 1.0))


def synth_silence(duration_s: float, rate: int = core.SAMPLE_RATE_HZ) -> np.ndarray:
    return np.zeros(int(duration_s * rate), dtype=np.int16)


# ── plan structure and the compact counts ────────────────────────────────────


def test_compact_plan_has_five_sections_and_the_exact_counts():
    plan = core.build_plan(FIRST_COMPACT)
    assert [s.section_id for s in plan.sections] == [
        "section1_delivery",
        "section2_position_noise",
        "section3_near_phrase",
        "section4_free_speech",
        "section5_background",
    ]
    counts = [len(s.steps) for s in plan.sections]
    assert counts == [10, 9, 34, 1, 1]
    assert len(plan.steps) == 55


def test_noise_sources_are_split_between_the_two_speakers():
    e1_dirs = {s.directory for s in core.build_plan(FIRST_COMPACT).steps}
    e2_dirs = {s.directory for s in core.build_plan(SECOND_COMPACT).steps}
    assert "positive_noise_tv" in e1_dirs and "positive_noise_kitchen" in e1_dirs
    assert "positive_noise_street" not in e1_dirs and "positive_noise_fan" not in e1_dirs
    assert "positive_noise_street" in e2_dirs and "positive_noise_fan" in e2_dirs


def test_a_speaker_with_no_compact_assignment_is_refused():
    with pytest.raises(ValueError):
        core.build_plan(ASSIGNED)  # assigned to the full round, not the compact one


def test_three_example_paths_are_exactly_as_documented():
    steps = {s.rel_path for s in core.build_plan(FIRST_COMPACT).steps}
    assert "originals/positive_normal/hey-youtab_normal_001.wav" in steps
    assert "originals/positive_noise_tv/hey-youtab_noise-tv_001.wav" in steps
    assert "originals/near_phrase/hey-you-tab_001.wav" in steps
    assert "originals/negative_freespeech/freespeech_001.wav" in steps
    assert "originals/background_only/background_001.wav" in steps


# ── filename/dir assignment matches what ingestion recognises ────────────────


def test_every_generated_filename_is_recognised_by_import_speaker_layout():
    """Each step's name matches the section regex ``import_speaker`` dispatches on.

    This is the ingestibility contract: a produced file is never a name the
    loader would refuse to classify.
    """
    layout = import_speaker.package_layout()
    known_slugs = {item.slug for item in spec.NEAR_PHRASE_ITEMS}
    for label in (FIRST_COMPACT, SECOND_COMPACT):
        for step in core.build_plan(label).steps:
            section = layout.section(step.directory)
            assert section is not None, f"{step.directory} is not a known section"
            assert section.stem is not None
            match = section.stem.match(step.stem)
            assert match, f"{step.stem!r} not recognised in {step.directory}"
            if step.directory == "near_phrase":
                assert match.group(1) in known_slugs


def test_filenames_also_match_the_validators_condition_and_freeform_shapes():
    plan = core.build_plan(FIRST_COMPACT)
    take = f"[0-9]{{{spec.TAKE_DIGITS}}}"
    import re

    for step in plan.steps:
        if step.kind == core.KIND_POSITIVE:
            # hey-youtab_<condition>_NNN
            assert re.match(rf"^{spec.WAKE_PHRASE_SLUG}_[a-z-]+_{take}$", step.stem)
        elif step.kind == core.KIND_FREEFORM:
            assert re.match(rf"^[a-z]+_{take}$", step.stem)
        else:
            assert re.match(rf"^[a-z0-9-]+_{take}$", step.stem)


# ── wave IO and the 16 kHz floor ─────────────────────────────────────────────


def test_sample_rate_floor_is_enforced():
    with pytest.raises(ValueError):
        core.validate_sample_rate(8000)
    core.validate_sample_rate(16000)
    core.validate_sample_rate(48000)


def test_write_wave_refuses_below_16k(tmp_path):
    with pytest.raises(ValueError):
        core.write_wave(tmp_path / "x.wav", synth_sine(0.2), rate=8000)


def test_write_then_read_wave_round_trips(tmp_path):
    pcm = synth_sine(0.5)
    path = tmp_path / "t.wav"
    core.write_wave(path, pcm, core.SAMPLE_RATE_HZ)
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == core.SAMPLE_RATE_HZ
    back, rate = core.read_wave(path)
    assert rate == core.SAMPLE_RATE_HZ
    assert np.array_equal(back, pcm)


# ── quality inspection: findings, never deletions ────────────────────────────


def test_good_take_has_no_blocking_finding():
    inspection = core.inspect_pcm(synth_sine(0.8), core.SAMPLE_RATE_HZ)
    assert inspection.ok
    assert inspection.readable
    assert inspection.findings == []


def test_empty_capture_is_flagged():
    inspection = core.inspect_pcm(np.zeros(0, dtype=np.int16), core.SAMPLE_RATE_HZ)
    assert not inspection.ok
    assert core.FINDING_EMPTY in inspection.codes


def test_silent_take_is_flagged_as_empty():
    inspection = core.inspect_pcm(synth_silence(0.8), core.SAMPLE_RATE_HZ)
    assert core.FINDING_EMPTY in inspection.codes
    assert not inspection.ok


def test_background_section_does_not_flag_near_silence():
    quiet = core.to_int16(np.full(core.SAMPLE_RATE_HZ, 0.0005))
    inspection = core.inspect_pcm(quiet, core.SAMPLE_RATE_HZ, expect_speech=False)
    assert core.FINDING_EMPTY not in inspection.codes


def test_too_short_take_is_flagged():
    inspection = core.inspect_pcm(synth_sine(0.1), core.SAMPLE_RATE_HZ)
    assert core.FINDING_TOO_SHORT in inspection.codes
    assert not inspection.ok


def test_clipped_take_is_flagged():
    inspection = core.inspect_pcm(synth_clipped(0.8), core.SAMPLE_RATE_HZ)
    assert core.FINDING_CLIPPED in inspection.codes
    assert inspection.clip_fraction > core.CLIP_FRACTION_THRESHOLD


def test_unreadable_wav_is_reported_not_raised(tmp_path):
    bad = tmp_path / "broken.wav"
    bad.write_bytes(b"this is not a RIFF/WAVE file at all")
    inspection = core.inspect_wav(bad)
    assert not inspection.readable
    assert core.FINDING_UNREADABLE in inspection.codes


def test_short_of_target_is_a_soft_note_not_a_redo():
    inspection = core.inspect_pcm(
        synth_sine(10.0), core.SAMPLE_RATE_HZ, expect_speech=True,
        min_seconds=core.MIN_FREEFORM_SECONDS, target_seconds=150,
    )
    assert core.FINDING_SHORT_OF_TARGET in inspection.codes
    # It is under the hard freeform floor too here, but the target note itself
    # is non-blocking by construction.
    soft = [f for f in inspection.findings if f.code == core.FINDING_SHORT_OF_TARGET]
    assert soft and soft[0].blocking is False


def test_inspect_wav_on_a_real_take_is_ok(tmp_path):
    path = tmp_path / "ok.wav"
    core.write_wave(path, synth_sine(0.8), core.SAMPLE_RATE_HZ)
    inspection = core.inspect_wav(path)
    assert inspection.ok and inspection.readable


# ── SHA-256 manifest ─────────────────────────────────────────────────────────


def test_manifest_is_coreutils_format_and_complete(tmp_path):
    (tmp_path / "originals").mkdir()
    core.write_wave(tmp_path / "originals" / "a.wav", synth_sine(0.2))
    (tmp_path / "RECORDING_METADATA.json").write_text("{}", encoding="utf-8")
    count = core.write_sha256sums(tmp_path)
    assert count == 2

    lines = (tmp_path / spec.CHECKSUM_FILE).read_text(encoding="utf-8").splitlines()
    listed = []
    for line in lines:
        match = spec.CHECKSUM_LINE.match(line)
        assert match, f"not coreutils format: {line!r}"
        listed.append(match.group(2))
    assert "SHA256SUMS" not in listed
    assert set(listed) == {"originals/a.wav", "RECORDING_METADATA.json"}
    # digests are the genuine digests of the bytes
    for rel in listed:
        assert core.sha256_file(tmp_path / rel) in (tmp_path / spec.CHECKSUM_FILE).read_text(
            encoding="utf-8"
        )


# ── metadata form ────────────────────────────────────────────────────────────


def test_draft_metadata_has_all_required_fields_with_placeholders():
    data = core.draft_metadata(FIRST_COMPACT, ("tv", "kitchen"))
    for field_name in spec.REQUIRED_METADATA_FIELDS:
        assert field_name in data
    assert data["speaker_id"] == FIRST_COMPACT
    assert data["noise_sources_used"] == ["tv", "kitchen"]
    # human-described fields are still placeholders in a draft
    assert data["room_name"].startswith("<")


def test_build_metadata_merges_answers_over_the_draft(tmp_path):
    answers = _synthetic_answers()
    data = core.build_metadata(FIRST_COMPACT, ("tv", "kitchen"), answers)
    assert data["room_name"] == answers["room_name"]
    assert data["speaker_id"] == FIRST_COMPACT
    core.write_metadata(tmp_path, data)
    reloaded = json.loads((tmp_path / spec.METADATA_FILE).read_text(encoding="utf-8"))
    assert reloaded["device_make_model"] == answers["device_make_model"]


# ── resumable progress, reconciled against disk ──────────────────────────────


def test_fresh_progress_starts_at_the_first_step(tmp_path):
    plan = core.build_plan(FIRST_COMPACT)
    progress = core.load_progress(plan, tmp_path)
    assert progress.completed == set()
    assert core.next_step(plan, progress) is plan.steps[0]


def test_progress_is_reconciled_from_the_files_on_disk(tmp_path):
    plan = core.build_plan(FIRST_COMPACT)
    first, second = plan.steps[0], plan.steps[1]
    core.write_wave(first.path(tmp_path), synth_sine(0.5))
    core.write_wave(second.path(tmp_path), synth_sine(0.5))
    progress = core.load_progress(plan, tmp_path)
    assert first.key in progress.completed and second.key in progress.completed
    assert core.next_step(plan, progress) is plan.steps[2]


def test_state_file_claiming_a_missing_take_loses_to_the_disk(tmp_path):
    plan = core.build_plan(FIRST_COMPACT)
    # A state file that claims two steps done, but only one take exists on disk.
    core.write_wave(plan.steps[0].path(tmp_path), synth_sine(0.5))
    core.save_progress(
        core.Progress(FIRST_COMPACT, {plan.steps[0].key, plan.steps[1].key}), tmp_path
    )
    progress = core.load_progress(plan, tmp_path)
    assert plan.steps[0].key in progress.completed
    assert plan.steps[1].key not in progress.completed  # re-recorded, not skipped


def test_section_status_counts(tmp_path):
    plan = core.build_plan(FIRST_COMPACT)
    core.write_wave(plan.sections[0].steps[0].path(tmp_path), synth_sine(0.5))
    progress = core.load_progress(plan, tmp_path)
    statuses = {s.section_id: s for s in core.section_status(plan, progress)}
    assert statuses["section1_delivery"].have == 1
    assert statuses["section1_delivery"].need == 10
    assert not statuses["section1_delivery"].complete


# ── the session controller, driven with fakes (no hardware) ──────────────────


class _Recorder:
    """A fake speak/capture pair that logs call order and returns a fixed take."""

    def __init__(self, pcm: np.ndarray):
        self.pcm = pcm
        self.calls: list[str] = []
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.calls.append("speak")
        self.spoken.append(text)

    def capture(self, step):
        self.calls.append("capture")
        return self.pcm, core.SAMPLE_RATE_HZ


def test_record_speaks_the_prompt_fully_before_capturing(tmp_path):
    rec = _Recorder(synth_sine(0.8))
    session = core.Session(core.build_plan(FIRST_COMPACT), tmp_path,
                           speak=rec.speak, capture=rec.capture)
    session.record()
    # speak returns (blocking) before capture begins: the mic is enabled only
    # after the prompt finishes, and the two never overlap.
    assert rec.calls == ["speak", "capture"]


def test_kept_take_on_disk_is_exactly_the_captured_stream_never_the_prompt(tmp_path):
    captured = synth_sine(0.8, amp=0.25, freq=330.0)
    rec = _Recorder(captured)
    session = core.Session(core.build_plan(FIRST_COMPACT), tmp_path,
                           speak=rec.speak, capture=rec.capture)
    step = session.current_step
    session.record()
    session.keep()

    on_disk, rate = core.read_wave(step.path(tmp_path))
    assert rate == core.SAMPLE_RATE_HZ
    assert np.array_equal(on_disk, captured)  # only the human stream, byte for byte

    # and the guidance text is nowhere in the file
    prompt = rec.spoken[0].encode("utf-8")
    assert prompt not in step.path(tmp_path).read_bytes()
    assert len(prompt) > 0


def test_redo_discards_the_unkept_take_and_re_records_the_same_step(tmp_path):
    rec = _Recorder(synth_sine(0.8))
    session = core.Session(core.build_plan(FIRST_COMPACT), tmp_path,
                           speak=rec.speak, capture=rec.capture)
    step = session.current_step
    session.record()
    session.redo()
    assert session.pending is None
    assert not step.path(tmp_path).exists()  # nothing was written
    assert session.current_step is step  # same step comes up again


def test_keep_never_touches_a_take_that_was_already_accepted(tmp_path):
    rec = _Recorder(synth_sine(0.8))
    session = core.Session(core.build_plan(FIRST_COMPACT), tmp_path,
                           speak=rec.speak, capture=rec.capture)
    step = session.current_step
    session.record()
    session.keep()
    written = step.path(tmp_path).read_bytes()
    # move on; a later redo of the *next* step must not rewrite the kept one
    session.record()
    session.redo()
    assert step.path(tmp_path).read_bytes() == written


def test_replay_returns_pending_then_last_kept(tmp_path):
    a = synth_sine(0.8, freq=200.0)
    rec = _Recorder(a)
    session = core.Session(core.build_plan(FIRST_COMPACT), tmp_path,
                           speak=rec.speak, capture=rec.capture)
    session.record()
    pcm, _ = session.replay()
    assert np.array_equal(pcm, a)
    session.keep()
    pcm2, _ = session.replay()  # nothing pending now -> last kept
    assert np.array_equal(pcm2, a)


def test_pause_blocks_recording_until_resume(tmp_path):
    rec = _Recorder(synth_sine(0.8))
    session = core.Session(core.build_plan(FIRST_COMPACT), tmp_path,
                           speak=rec.speak, capture=rec.capture)
    session.pause()
    assert session.paused
    with pytest.raises(RuntimeError):
        session.record()
    session.resume()
    assert not session.paused
    session.record()  # works now


def test_tts_toggle_off_skips_speaking(tmp_path):
    rec = _Recorder(synth_sine(0.8))
    session = core.Session(core.build_plan(FIRST_COMPACT), tmp_path,
                           speak=rec.speak, capture=rec.capture, tts_enabled=False)
    session.record()
    assert rec.calls == ["capture"]


def test_a_relaunched_session_resumes_where_the_files_stopped(tmp_path):
    plan = core.build_plan(FIRST_COMPACT)
    rec = _Recorder(synth_sine(0.6))
    first = core.Session(plan, tmp_path, speak=rec.speak, capture=rec.capture)
    first.record()
    first.keep()
    done_key = plan.steps[0].key

    second = core.Session(plan, tmp_path, speak=rec.speak, capture=rec.capture)
    assert done_key in second.progress.completed
    assert second.current_step is plan.steps[1]


def test_compose_guidance_wording_per_kind():
    plan = core.build_plan(FIRST_COMPACT)
    positive = next(s for s in plan.steps if s.kind == core.KIND_POSITIVE)
    near = next(s for s in plan.steps if s.kind == core.KIND_NEAR_PHRASE)
    free = next(s for s in plan.steps if s.kind == core.KIND_FREEFORM)
    assert spec.WAKE_PHRASE in core.compose_guidance(positive)
    assert near.prompt in core.compose_guidance(near)
    assert core.compose_guidance(free)


# ── integration: the produced folder against the real validator ──────────────


def _synthetic_answers() -> dict:
    """Non-placeholder answers so a finalized folder passes the metadata check."""
    return {
        "device_make_model": "a test recorder",
        "recording_app": "recording assistant self-test",
        "room_name": "a test room",
        "room_size_approx": "about 4 by 5 metres",
        "floor_surface": "wood",
        "wall_surface": "drywall",
        "background_sources_present": "a refrigerator hum",
        "farfield_distance": "about 5 metres, next room, door open",
        "consent_signed_date": "2020-01-01",
        "notes": "synthetic fixture, no human recorded",
    }


def _drive_compact_session(root: Path, label: str) -> core.Plan:
    plan = core.build_plan(label)
    rec = _Recorder(synth_sine(0.8))
    session = core.Session(plan, root, speak=rec.speak, capture=rec.capture)
    while not session.done:
        session.record()
        session.keep()
    return plan


def test_compact_folder_carries_only_the_intended_shortfalls(tmp_path):
    root = tmp_path / FIRST_COMPACT
    _drive_compact_session(root, FIRST_COMPACT)
    (root / spec.CONSENT_FILE).write_bytes(b"%PDF-1.4 synthetic consent placeholder")
    plan = core.build_plan(FIRST_COMPACT)
    core.finalize_submission(plan, root, _synthetic_answers())

    result = validator.validate_speaker_directory(root)

    def allowed(msg: str) -> bool:
        return (
            "need at least" in msg
            or "positive_farfield_loud" in msg
            or "SPEAKER_ASSIGNMENTS" in msg
        )

    unexpected = [e for e in result.errors if not allowed(e)]
    assert not unexpected, f"unexpected validator errors: {unexpected}"
    # non-vacuity: the intended shortfalls really are reported
    assert any("SPEAKER_ASSIGNMENTS" in e for e in result.errors)
    assert any("need at least" in e for e in result.errors)
    # and nothing about a bad name/format/checksum slipped through
    for needle in ("unrecognised", "not coreutils", "does not list", "not well-formed"):
        assert not any(needle in e for e in result.errors)


def _write_full_spec_folder(root: Path, label: str) -> None:
    originals = root / spec.ORIGINALS_DIR
    for section in spec.POSITIVE_SECTIONS:
        for stem in spec.expected_stems_for_positive_section(section):
            core.write_wave(originals / section.directory / f"{stem}.wav", synth_sine(0.5))
    noise_sources = spec.coverage_for(label).noise_sources
    for source in noise_sources:
        section = spec.noise_section(source)
        for stem in spec.expected_stems_for_positive_section(section):
            core.write_wave(originals / section.directory / f"{stem}.wav", synth_sine(0.5))
    for stem in spec.expected_stems_for_near_phrase():
        core.write_wave(originals / "near_phrase" / f"{stem}.wav", synth_sine(0.5))
    for section in spec.FREEFORM_SECTIONS:
        for stem in spec.expected_stems_for_freeform(section, section.min_files):
            core.write_wave(originals / section.directory / f"{stem}.wav", synth_sine(0.5))

    (root / spec.CONSENT_FILE).write_bytes(b"%PDF-1.4 synthetic consent placeholder")
    core.write_metadata(root, core.build_metadata(label, noise_sources, _synthetic_answers()))
    core.write_sha256sums(root)


def test_full_spec_folder_built_through_core_writers_validates_green(tmp_path):
    root = tmp_path / ASSIGNED
    _write_full_spec_folder(root, ASSIGNED)
    result = validator.validate_speaker_directory(root)
    assert result.ok, f"validator errors: {result.errors}"
    assert not result.warnings, f"validator warnings: {result.warnings}"
