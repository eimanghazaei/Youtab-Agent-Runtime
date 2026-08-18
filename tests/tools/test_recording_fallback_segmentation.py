"""Behavioural tests for the manual segment-fallback splitter.

``recording_assistant.segment_fallback`` turns seven hand-made continuous
recordings into the compact plan's 55 canonical takes when the GUI cannot be
used. Everything it decides that a machine can check without a microphone lives
here: the pure silence-based segmenter (right counts, blips dropped, and a
recording without enough silence caught rather than mis-split), the
seven-to-55 mapping staying in sync with the plan, and an end-to-end split on a
temporary ``_continuous/`` folder that writes every canonical file, leaves the
originals byte-for-byte identical, and copies the two continuous sections whole.

All fixtures live under ``tmp_path``; nothing here reads or writes a real
recording, and no per-speaker record is committed. The two compact-plan labels
are bare literals, never in the ``speaker_id = E0nn`` shape the no-human-data
gate forbids in tracked text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import recording_assistant.compact_plan as compact_plan  # noqa: E402
import recording_assistant.core as core  # noqa: E402
import recording_assistant.segment_fallback as sf  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402

FIRST_COMPACT = "E001"
SECOND_COMPACT = "E002"

RATE = core.SAMPLE_RATE_HZ


# ── synthesis helpers ────────────────────────────────────────────────────────


def tone(duration_s: float, amp: float = 0.3, freq: float = 220.0, rate: int = RATE) -> np.ndarray:
    n = max(int(round(duration_s * rate)), 0)
    t = np.arange(n) / rate
    return core.to_int16(amp * np.sin(2 * np.pi * freq * t))


def silence(duration_s: float, rate: int = RATE) -> np.ndarray:
    return np.zeros(int(round(duration_s * rate)), dtype=np.int16)


def continuous(
    n_segments: int,
    *,
    seg_s: float = 0.8,
    gap_s: float = 2.5,
    pad_s: float = 0.5,
    rate: int = RATE,
) -> np.ndarray:
    """``n_segments`` tones, each separated by ``gap_s`` of clear silence."""
    parts = [silence(pad_s, rate)]
    for index in range(n_segments):
        parts.append(tone(seg_s, rate=rate))
        parts.append(silence(gap_s if index < n_segments - 1 else pad_s, rate))
    return np.concatenate(parts).astype(np.int16)


# ── the pure segmenter ────────────────────────────────────────────────────────


def test_detect_segments_finds_exactly_the_expected_counts():
    for expected in (10, 3, 34):
        pcm = continuous(expected)
        segments = sf.detect_segments(pcm, RATE)
        assert len(segments) == expected, f"{expected}-segment file split into {len(segments)}"
        # spans are ordered, within bounds and non-overlapping.
        assert all(0 <= s < e <= pcm.size for s, e in segments)
        assert all(segments[i][1] <= segments[i + 1][0] for i in range(len(segments) - 1))


def test_detect_segments_drops_short_blips_between_real_utterances():
    # three genuine ~0.8 s responses with two ~50 ms clicks between them, every
    # boundary a clean 2.5 s of silence. Only the three responses survive.
    parts = [silence(0.5)]
    for chunk in (tone(0.8), tone(0.05), tone(0.8), tone(0.05), tone(0.8)):
        parts.append(chunk)
        parts.append(silence(2.5))
    pcm = np.concatenate(parts).astype(np.int16)
    assert len(sf.detect_segments(pcm, RATE)) == 3


def test_a_recording_without_enough_silence_yields_the_wrong_count():
    # ten responses separated by only 0.5 s -- below the 1.2 s boundary -- so
    # they merge into one span. The count is wrong, which is what makes the
    # tool refuse to write rather than mis-split.
    parts = [silence(0.5)]
    for _ in range(10):
        parts.append(tone(0.8))
        parts.append(silence(0.5))
    pcm = np.concatenate(parts).astype(np.int16)
    segments = sf.detect_segments(pcm, RATE, expected=10)
    assert len(segments) != 10
    assert len(segments) == 1


def test_detect_segments_is_deterministic():
    pcm = continuous(5)
    assert sf.detect_segments(pcm, RATE) == sf.detect_segments(pcm, RATE)


def test_empty_input_yields_no_segments():
    assert sf.detect_segments(np.zeros(0, dtype=np.int16), RATE) == []
    assert sf.detect_segments(silence(3.0), RATE) == []


# ── the seven-to-55 mapping ───────────────────────────────────────────────────


def test_mapping_yields_the_exact_counts_for_both_speakers():
    for speaker in (FIRST_COMPACT, SECOND_COMPACT):
        plan = core.build_plan(speaker)
        mappings = sf.build_original_map(plan)
        assert [m.name for m in mappings] == [
            sf.ORIGINAL_POSITIVE_CLOSE,
            sf.ORIGINAL_POSITIVE_FARFIELD,
            sf.ORIGINAL_POSITIVE_NOISE_A,
            sf.ORIGINAL_POSITIVE_NOISE_B,
            sf.ORIGINAL_NEAR_PHRASES,
            sf.ORIGINAL_FREESPEECH,
            sf.ORIGINAL_BACKGROUND,
        ]
        assert [m.expected for m in mappings] == [10, 3, 3, 3, 34, 1, 1]
        assert sum(m.expected for m in mappings) == 55
        assert [m.split for m in mappings] == [True, True, True, True, True, False, False]


def test_every_mapping_target_is_a_real_plan_step_and_covers_all_55():
    plan = core.build_plan(FIRST_COMPACT)
    mappings = sf.build_original_map(plan)
    real = {step.rel_path for step in plan.steps}
    covered = [step.rel_path for m in mappings for step in m.steps]
    assert all(rel in real for rel in covered), "a mapping names a step outside the plan"
    assert sorted(covered) == sorted(real), "mapping and plan cover different steps"
    assert len(covered) == 55 and len(set(covered)) == 55, "a step is covered twice or missed"


def test_expected_counts_constant_matches_the_mapping():
    assert sf.TOTAL_DERIVED == 55
    assert sf.EXPECTED_DERIVED_COUNTS == {
        sf.ORIGINAL_POSITIVE_CLOSE: 10,
        sf.ORIGINAL_POSITIVE_FARFIELD: 3,
        sf.ORIGINAL_POSITIVE_NOISE_A: 3,
        sf.ORIGINAL_POSITIVE_NOISE_B: 3,
        sf.ORIGINAL_NEAR_PHRASES: 34,
        sf.ORIGINAL_FREESPEECH: 1,
        sf.ORIGINAL_BACKGROUND: 1,
    }


# ── end to end on a temporary _continuous/ folder ─────────────────────────────


def _make_continuous(root: Path) -> tuple[Path, dict[str, np.ndarray]]:
    """Write the seven originals to the sibling continuous folder; return samples."""
    cdir = sf.continuous_dir_for(root)
    cdir.mkdir(parents=True, exist_ok=True)
    sources: dict[str, np.ndarray] = {}
    for name, count in sf.EXPECTED_DERIVED_COUNTS.items():
        if name == sf.ORIGINAL_FREESPEECH:
            samples = tone(2.0)  # one continuous take, copied whole
        elif name == sf.ORIGINAL_BACKGROUND:
            samples = tone(2.0, amp=0.1)  # quiet room tone, no speech expected
        else:
            samples = continuous(count)
        core.write_wave(cdir / name, samples, RATE)
        sources[name] = samples
    return cdir, sources


def test_end_to_end_writes_all_55_and_preserves_the_originals(tmp_path):
    root = tmp_path / FIRST_COMPACT
    cdir, sources = _make_continuous(root)
    before = {name: core.sha256_file(cdir / name) for name in sources}

    rc = sf.main(["--speaker", FIRST_COMPACT, "--root", str(root), "--yes"])
    assert rc == 0

    plan = core.build_plan(FIRST_COMPACT)
    present = [step for step in plan.steps if step.path(root).is_file()]
    assert len(present) == 55, f"only {len(present)} of 55 derived takes were written"

    # originals are byte-for-byte identical to how they went in.
    after = {name: core.sha256_file(cdir / name) for name in sources}
    assert after == before, "the segmenter changed an original"

    # a representative derived take clears core's own per-take inspection.
    step0 = plan.steps[0]
    pcm, rate = core.read_wave(step0.path(root))
    assert core.inspect_step_pcm(step0, pcm, rate).ok

    # both manifests were emitted.
    assert (root / spec.CHECKSUM_FILE).is_file()
    assert (cdir / spec.CHECKSUM_FILE).is_file()


def test_submission_root_stays_clean_for_the_compact_validator(tmp_path):
    # The seven originals are preserved in a sibling folder, never inside the
    # submission root, so the derived tree is exactly what the compact validator
    # accepts (it refuses any unrecognised top-level entry).
    root = tmp_path / FIRST_COMPACT
    _make_continuous(root)
    assert sf.main(["--speaker", FIRST_COMPACT, "--root", str(root), "--yes"]) == 0

    assert sf.continuous_dir_for(root).is_dir(), "originals must be preserved next to the submission"
    top = sorted(p.name for p in root.iterdir())
    assert top == sorted([spec.ORIGINALS_DIR, spec.CHECKSUM_FILE]), (
        f"submission root has stray entries: {top}"
    )

    # the derived manifest lists exactly the 55 takes, all under originals/.
    listed = [
        line.split("  ", 1)[1]
        for line in (root / spec.CHECKSUM_FILE).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(listed) == 55
    assert all(rel.startswith(spec.ORIGINALS_DIR + "/") for rel in listed)

    # the validator raises no "unrecognised entry" / _continuous complaint.
    plan = core.build_plan(FIRST_COMPACT)
    result = core.validate_compact_submission(root, plan)
    assert not any(
        "unrecognised entry" in e or sf.CONTINUOUS_DIRNAME in e for e in result.errors
    ), f"validator tripped on a preserved-originals artifact: {result.errors}"


def test_freespeech_and_background_are_copied_whole(tmp_path):
    root = tmp_path / FIRST_COMPACT
    _cdir, sources = _make_continuous(root)
    assert sf.main(["--speaker", FIRST_COMPACT, "--root", str(root), "--yes"]) == 0

    plan = core.build_plan(FIRST_COMPACT)
    free_step = next(s for s in plan.steps if s.directory == "negative_freespeech")
    bg_step = next(s for s in plan.steps if s.directory == "background_only")

    free_pcm, _ = core.read_wave(free_step.path(root))
    bg_pcm, _ = core.read_wave(bg_step.path(root))
    assert np.array_equal(free_pcm, sources[sf.ORIGINAL_FREESPEECH])
    assert np.array_equal(bg_pcm, sources[sf.ORIGINAL_BACKGROUND])


def test_a_count_mismatch_refuses_to_write(tmp_path):
    root = tmp_path / SECOND_COMPACT
    cdir = sf.continuous_dir_for(root)
    cdir.mkdir(parents=True, exist_ok=True)
    for name, count in sf.EXPECTED_DERIVED_COUNTS.items():
        if name == sf.ORIGINAL_POSITIVE_CLOSE:
            # ten responses with only 0.5 s between them: detected as one span.
            parts = [silence(0.5)]
            for _ in range(count):
                parts.append(tone(0.8))
                parts.append(silence(0.5))
            samples = np.concatenate(parts).astype(np.int16)
        elif name in (sf.ORIGINAL_FREESPEECH, sf.ORIGINAL_BACKGROUND):
            samples = tone(2.0, amp=0.1 if name == sf.ORIGINAL_BACKGROUND else 0.3)
        else:
            samples = continuous(count)
        core.write_wave(cdir / name, samples, RATE)

    rc = sf.main(["--speaker", SECOND_COMPACT, "--root", str(root), "--yes"])
    assert rc == 1, "a mismatched split must be refused"
    assert not (root / "originals").exists(), "nothing may be written on a refusal"


def test_dry_run_writes_nothing(tmp_path):
    root = tmp_path / FIRST_COMPACT
    _make_continuous(root)
    rc = sf.main(["--speaker", FIRST_COMPACT, "--root", str(root), "--dry-run"])
    assert rc == 0
    assert not (root / "originals").exists()


def test_unknown_speaker_is_refused(tmp_path):
    assert sf.main(["--speaker", "E999", "--root", str(tmp_path)]) == 2
