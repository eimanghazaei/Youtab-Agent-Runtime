"""The compact two-speaker ingestion profile in ``import_speaker.py``.

Background
----------
Two of the seven Round 8 speakers record a *compact* sitting rather than the full
recording package: the same directory tree and the same filename grammar, but the
smaller per-section take counts of the governed compact plan
(``recording_assistant.compact_plan``). The profile is registry-gated -- a label
is eligible for the compact layout only because it appears in
``compact_plan.COMPACT_NOISE_ASSIGNMENTS``, never because of a flag or a folder
name -- so it can offer a smaller round to the two compact speakers without ever
weakening the requirements the five full-round speakers are held to.

These tests prove exactly that boundary, positively and negatively, at the seam
``import_speaker`` owns:

* a complete compact package for each compact speaker passes every check and
  resolves to that speaker's fixed role (training / validation), and to nothing
  else;
* a full-round speaker still has only the package layout, and a compact-sized
  tree handed to one is refused on its counts;
* an incomplete compact package -- a delivery shortfall, a dropped near phrase,
  a missing assigned noise source -- is refused, and restoring the take passes;
* the profile cannot be spoofed: the compact layout cannot be *requested* for a
  full-round speaker, a metadata ``speaker_id`` that disagrees with the import
  label is refused, and asking for the package layout on a compact folder selects
  the stricter layout and then fails its bigger counts -- the flag can only
  tighten, never grant a weaker profile;
* a compact take handed in as mono 24-bit PCM is read and kept as 24-bit; and
* the manual segment-fallback path leaves the seven continuous originals in a
  ``_continuous/`` archive beside the submission, which ingestion never walks,
  never manifests, and never touches.

Conventions this file follows (both from
``tests/tools/test_wakeword_speaker_import.py``, which it reuses):

* Everything is hermetic -- submissions are built under ``tmp_path`` from
  generated tones plus dither, no speech and not one byte of anyone's recording.
* Speaker labels are read out of the registries rather than typed, and directory
  names are assembled from ``import_speaker.SPEAKER_DIR_PREFIX`` plus a label, so
  the human-data commit gate (``test_wakeword_no_human_data_committed.py``) has
  no speaker-keyed record or speaker directory to catch here.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import freeze_manifest  # noqa: E402
import import_speaker as imp  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402
from recording_assistant import compact_plan, core, segment_fallback  # noqa: E402

# The pure builders and assertions the full-round test already owns: the tone
# generator, the 16-bit writer, the metadata/checksum writers, the full package
# builder, and the plan-inspection helpers. Reused rather than re-copied so a
# change to how a valid submission is built lands in one place.
from tests.tools.test_wakeword_speaker_import import (  # noqa: E402
    _samples,
    build_package_submission,
    failed,
    problems_of,
    row_for,
    valid_metadata,
    write_checksums,
    write_metadata,
    write_wav,
    RATE,
    TAKE_SECONDS,
)

# ── who the compact speakers are, read from the registry alone ───────────────

#: The two compact-round labels, and which is which role -- both resolved through
#: the registry, never typed. One is training-only, the other validation-only.
COMPACT_LABELS = imp.COMPACT_LAYOUT_LABELS
COMPACT_TRAINING = next(
    label for label in COMPACT_LABELS if imp.assignment(label).role == spec.ROLE_TRAINING
)
COMPACT_VALIDATION = next(
    label for label in COMPACT_LABELS if imp.assignment(label).role == spec.ROLE_VALIDATION
)

#: A full-round training speaker: assigned training in the spec, but *not* a
#: compact-round label, so it may only ever submit the full package.
FULL_ROUND_LABEL = next(
    label
    for label in spec.labels_for_role(spec.ROLE_TRAINING)
    if label not in COMPACT_LABELS
)

CONSENT_BYTES = b"%PDF-1.4 placeholder consent scan"

#: Long enough to clear ``core.MIN_FREEFORM_SECONDS`` (20 s), so a continuous
#: section take is a real capture rather than a stub.
FREEFORM_SECONDS = core.MIN_FREEFORM_SECONDS + 1.0


# ── generated audio: 16- or 24-bit, distinct per file ────────────────────────


def _write_pcm24(path: Path, payload: np.ndarray, rate: int) -> None:
    """Write a mono float payload as 24-bit little-endian PCM WAV.

    The samples are quantised to signed 24-bit and packed as the low three bytes
    of each little-endian int32 -- exactly the container ``import_speaker`` decodes
    natively as ``pcm_s24le``. Nothing here is ever converted to 16-bit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ints = np.clip(np.round(payload * (2**23 - 1)), -(2**23), 2**23 - 1).astype("<i4")
    frames = np.frombuffer(ints.tobytes(), dtype=np.uint8).reshape(-1, 4)[:, :3].tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(3)
        handle.setframerate(rate)
        handle.writeframes(frames)


def write_take(path: Path, *, seconds: float = TAKE_SECONDS, width: int = 2, peak: float = 0.3) -> None:
    """One discrete take, 16- or 24-bit, with audio unique to its path.

    Distinct audio matters: two byte-identical takes are a duplicate the reuse
    check refuses, so both writers seed the tone from the file's own path.
    """
    if width == 2:
        write_wav(path, seconds=seconds, peak=peak)  # the full-round test's 16-bit writer
        return
    _write_pcm24(path, _samples(path.as_posix(), seconds, RATE, peak), RATE)


def _voiced(seed: str, seconds: float, rate: int, peak: float) -> np.ndarray:
    """A single voiced blob, unique per ``seed`` -- the payload of one response."""
    generator = np.random.default_rng(
        int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")
    )
    count = int(rate * seconds)
    time_axis = np.arange(count) / rate
    tone = np.sin(2 * np.pi * generator.uniform(180.0, 320.0) * time_axis)
    dither = generator.normal(0.0, 1e-4, count)
    return np.clip(peak * tone + dither, -1.0, 1.0)


# ── (A) direct builder: the canonical 55-file compact tree ───────────────────


def compact_metadata(label: str) -> dict:
    """``valid_metadata`` adapted to the compact plan's assigned noise sources."""
    metadata = valid_metadata(label)
    metadata["noise_sources_used"] = list(compact_plan.noise_sources_for(label))
    return metadata


def _compact_positive_sections(noise_sources: tuple[str, ...]) -> list[tuple[str, str, int]]:
    """``(directory, condition, min_files)`` for every compact positive section.

    Read off the governed compact counts: five close-mic deliveries x2, far-field
    x3, and each of the two assigned noise sources x3 -- the exact shape
    ``import_speaker.compact_layout`` builds, assembled here from the same plan
    constants so the fixture and the code under test cannot drift.
    """
    by_directory = {section.directory: section for section in spec.POSITIVE_SECTIONS}
    sections: list[tuple[str, str, int]] = []
    for directory in compact_plan.COMPACT_DELIVERY_DIRECTORIES:
        base = by_directory[directory]
        sections.append((base.directory, base.condition, compact_plan.COMPACT_DELIVERY_REPS))
    farfield = by_directory["positive_farfield"]
    sections.append((farfield.directory, farfield.condition, compact_plan.COMPACT_FARFIELD_REPS))
    for source in noise_sources:
        noise = spec.noise_section(source)
        sections.append((noise.directory, noise.condition, compact_plan.COMPACT_NOISE_REPS))
    return sections


def _write_compact_originals(root: Path, noise_sources: tuple[str, ...], *, width: int) -> None:
    """Write the 55-file compact recording tree under ``root/originals/``."""
    originals = root / spec.ORIGINALS_DIR
    for directory, condition, reps in _compact_positive_sections(noise_sources):
        for take in range(1, reps + 1):
            write_take(
                originals / directory / f"{spec.WAKE_PHRASE_SLUG}_{condition}_{take:03d}.wav",
                width=width,
            )
    for item in spec.NEAR_PHRASE_ITEMS:
        for take in range(1, compact_plan.COMPACT_NEAR_PHRASE_REPS + 1):
            write_take(originals / "near_phrase" / f"{item.slug}_{take:03d}.wav", width=width)
    for section in spec.FREEFORM_SECTIONS:
        for take in range(1, compact_plan.COMPACT_FREEFORM_FILES + 1):
            write_take(
                originals / section.directory / f"{section.prefix}_{take:03d}.wav",
                seconds=FREEFORM_SECONDS,
                width=width,
            )


def build_compact_submission(root: Path, label: str, *, width: int = 2) -> Path:
    """A complete, correct compact package for a compact-round ``label``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / spec.CONSENT_FILE).write_bytes(CONSENT_BYTES)
    write_metadata(root, compact_metadata(label))
    _write_compact_originals(root, compact_plan.noise_sources_for(label), width=width)
    write_checksums(root)
    return root


def build_compact_sized_package(root: Path, label: str, noise_sources: tuple[str, ...]) -> Path:
    """A package-shaped tree carrying the *compact* counts, for a full-round label.

    A full-round speaker has no compact noise assignment, so its two noise sources
    are supplied explicitly (from its coverage cell). The tree is named the
    canonical package way but holds the small compact counts -- which is exactly
    what the full round must reject.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / spec.CONSENT_FILE).write_bytes(CONSENT_BYTES)
    metadata = valid_metadata(label)
    metadata["noise_sources_used"] = list(noise_sources)
    write_metadata(root, metadata)
    _write_compact_originals(root, noise_sources, width=2)
    write_checksums(root)
    return root


# ── (B) segment-fallback builder: seven continuous originals -> 55 takes ──────


def _write_continuous_original(path: Path, mapping: "segment_fallback.OriginalMapping") -> None:
    """Synthesize one continuous 24-bit original the fallback splits into takes.

    A ``split`` original carries ``mapping.expected`` voiced blobs separated by
    clear silence (each blob distinct, so no two derived takes are byte-identical);
    a whole-copy original (free speech, background) is one continuous take. Written
    24-bit mono so the derived takes are exact 24-bit slices of it.
    """
    rate = RATE
    expect_speech = mapping.steps[0].expect_speech
    peak = 0.3 if expect_speech else 0.12
    if not mapping.split:
        payload = _voiced(f"{path.name}:whole", 1.2, rate, peak)
    else:
        parts = [np.zeros(int(rate * 0.5))]
        for index in range(mapping.expected):
            parts.append(_voiced(f"{path.name}:{index}", 0.6, rate, peak))
            parts.append(np.zeros(int(rate * 1.6)))  # a gap wider than the split's min-silence
        payload = np.concatenate(parts)
    _write_pcm24(path, payload, rate)


def build_via_segment_fallback(root: Path, label: str) -> Path:
    """Build a compact submission through the reviewed manual split path.

    Drops the seven continuous 24-bit originals into ``root/_continuous/``, runs
    the fallback to expand them into the 55 canonical takes under ``originals/``,
    adds the consent scan, and finalises the metadata + manifest. Leaves the seven
    originals preserved in ``_continuous/`` beside the submission.
    """
    root.mkdir(parents=True, exist_ok=True)
    continuous = segment_fallback.continuous_dir_for(root)
    continuous.mkdir(parents=True, exist_ok=True)
    mappings = segment_fallback.build_original_map(core.build_plan(label))
    for mapping in mappings:
        _write_continuous_original(continuous / mapping.name, mapping)

    exit_code = segment_fallback.main(["--speaker", label, "--root", str(root), "--yes"])
    assert exit_code == 0, "the reviewed split did not write cleanly"

    (root / spec.CONSENT_FILE).write_bytes(CONSENT_BYTES)
    # Real answers, so the finalised metadata form clears the placeholder check.
    core.finalize_submission(core.build_plan(label), root, answers=valid_metadata(label))
    return root


@pytest.fixture()
def into(tmp_path: Path) -> Path:
    """A fresh external data root to import into."""
    root = tmp_path / "human"
    root.mkdir()
    return root


# ── 1 & 2: each compact speaker imports as exactly one role ──────────────────


def test_e001_compact_package_imports_only_as_training(tmp_path: Path, into: Path) -> None:
    """The training compact speaker: complete package passes, and only as training."""
    submission = build_compact_submission(tmp_path / COMPACT_TRAINING, COMPACT_TRAINING)
    state = imp.plan(COMPACT_TRAINING, submission, into)

    assert state.ok, {check.name: check.problems for check in state.failures}
    assert failed(state) == set()
    assert state.layout.name == imp.LAYOUT_COMPACT
    assert state.assignment.role == spec.ROLE_TRAINING
    assert len(state.originals) == 55

    # Usable for training; refused for the split its role does not permit.
    imp.assert_usable_for(COMPACT_TRAINING, "training")
    with pytest.raises(ValueError):
        imp.assert_usable_for(COMPACT_TRAINING, "validation")


def test_e002_compact_package_imports_only_as_validation(tmp_path: Path, into: Path) -> None:
    """The validation compact speaker: complete package passes, and only as validation."""
    submission = build_compact_submission(tmp_path / COMPACT_VALIDATION, COMPACT_VALIDATION)
    state = imp.plan(COMPACT_VALIDATION, submission, into)

    assert state.ok, {check.name: check.problems for check in state.failures}
    assert failed(state) == set()
    assert state.layout.name == imp.LAYOUT_COMPACT
    assert state.assignment.role == spec.ROLE_VALIDATION
    assert len(state.originals) == 55

    imp.assert_usable_for(COMPACT_VALIDATION, "validation")
    with pytest.raises(ValueError):
        imp.assert_usable_for(COMPACT_VALIDATION, "training")


# ── 3: the full round is untouched ───────────────────────────────────────────


def test_a_full_round_speaker_has_only_the_package_layout(tmp_path: Path, into: Path) -> None:
    """A full-round speaker keeps exactly one layout, and no compact profile.

    ``layouts_for`` offers it the package alone, ``compact_layout`` refuses it
    outright, and a genuine full package still passes every check as before -- the
    compact work added a profile beside the full round rather than under it.
    """
    assert [layout.name for layout in imp.layouts_for(FULL_ROUND_LABEL)] == [imp.LAYOUT_PACKAGE]
    with pytest.raises(imp.Refused):
        imp.compact_layout(FULL_ROUND_LABEL)

    package = build_package_submission(tmp_path / FULL_ROUND_LABEL, FULL_ROUND_LABEL)
    state = imp.plan(FULL_ROUND_LABEL, package, into)
    assert state.ok, {check.name: check.problems for check in state.failures}
    assert state.layout.name == imp.LAYOUT_PACKAGE


def test_a_compact_sized_tree_is_refused_for_a_full_round_speaker(
    tmp_path: Path, into: Path
) -> None:
    """The compact counts are rejected for a speaker held to the full round.

    The tree is named the canonical package way but carries the small compact
    counts (two deliveries per condition, one take per near phrase, no
    ``positive_farfield_loud``). A full-round speaker auto-resolves to the package
    layout, whose completeness check fails on those counts.
    """
    noise_sources = tuple(sorted(spec.coverage_for(FULL_ROUND_LABEL).noise_sources))
    submission = build_compact_sized_package(tmp_path / FULL_ROUND_LABEL, FULL_ROUND_LABEL, noise_sources)
    state = imp.plan(FULL_ROUND_LABEL, submission, into)

    assert state.layout.name == imp.LAYOUT_PACKAGE
    assert "sections" in failed(state), failed(state)


# ── 4: an incomplete compact package is refused ──────────────────────────────


def test_an_incomplete_compact_package_is_refused_and_restoring_it_passes(
    tmp_path: Path, into: Path
) -> None:
    """Every kind of compact shortfall fails the sections check; a whole one passes.

    Three ways a compact sitting can come up short -- a delivery condition with one
    take instead of two, a dropped near phrase, a missing assigned noise source --
    each fail on ``sections``, and restoring the delivery take makes the same
    package pass. An excluded/absent take never counts toward its section.
    """
    label = COMPACT_TRAINING
    tv_directory = "positive_noise_" + compact_plan.noise_sources_for(label)[0]
    first_phrase = spec.NEAR_PHRASE_ITEMS[0].slug

    shortfalls = {
        # A delivery condition one take short of its floor of two.
        "delivery": (
            lambda root: (root / spec.ORIGINALS_DIR / "positive_normal"
                          / f"{spec.WAKE_PHRASE_SLUG}_normal_002.wav").unlink(),
            "positive_normal/",
        ),
        # A near phrase with no take at all.
        "near_phrase": (
            lambda root: (root / spec.ORIGINALS_DIR / "near_phrase"
                          / f"{first_phrase}_001.wav").unlink(),
            "near_phrase/",
        ),
        # An assigned noise source missing entirely.
        "noise_source": (
            lambda root: shutil.rmtree(root / spec.ORIGINALS_DIR / tv_directory),
            tv_directory,
        ),
    }

    for name, (break_it, needle) in shortfalls.items():
        submission = build_compact_submission(tmp_path / f"missing_{name}" / label, label)
        break_it(submission)
        write_checksums(submission)  # so only the sections check, not the transfer, complains
        state = imp.plan(label, submission, tmp_path / f"into_{name}")
        assert "sections" in failed(state), name
        assert any(needle in problem for problem in problems_of(state, "sections")), name

    # Control: the same package, complete, passes -- the shortfall was the fault.
    restored = build_compact_submission(tmp_path / "restored" / label, label)
    assert imp.plan(label, restored, into).ok


# ── 5: the profile cannot be spoofed ─────────────────────────────────────────


def test_the_compact_layout_cannot_be_requested_for_a_full_round_speaker(
    tmp_path: Path,
) -> None:
    """(a) A caller flag cannot hand a full-round speaker the smaller profile.

    ``_choose_layout`` refuses a requested layout that the label's registry entry
    does not allow, so ``--layout compact`` on a full-round speaker is refused
    before any tree is read -- the compact profile is never reachable by request.
    """
    with pytest.raises(imp.Refused):
        imp._choose_layout(FULL_ROUND_LABEL, tmp_path, "compact")


def test_metadata_naming_another_speaker_is_refused(tmp_path: Path, into: Path) -> None:
    """(b) A folder whose metadata ``speaker_id`` disagrees with the import label.

    The role is decided by the import label alone; a submission whose
    ``RECORDING_METADATA.json`` states a *different* speaker is two records
    disagreeing about whose voice this is, and the metadata check refuses it rather
    than believing either.
    """
    # A neutrally named folder, so the metadata guard is what fails (not the
    # folder-name guard, which would fire first on a speaker-named folder).
    submission = build_compact_submission(tmp_path / "handoff", COMPACT_TRAINING)
    spoofed = compact_metadata(COMPACT_TRAINING)
    spoofed["speaker_id"] = COMPACT_VALIDATION  # names the other compact speaker
    write_metadata(submission, spoofed)
    write_checksums(submission)

    state = imp.plan(COMPACT_TRAINING, submission, into)
    assert "metadata" in failed(state)
    assert any(
        COMPACT_VALIDATION in problem for problem in problems_of(state, "metadata")
    )


def test_requesting_the_package_layout_only_tightens_a_compact_folder(
    tmp_path: Path, into: Path
) -> None:
    """(c) ``--layout package`` on a compact folder selects the stricter layout.

    A compact speaker may use the package layout too -- it is the *stricter* of the
    two. Asking for it on a compact-sized folder resolves to the package layout and
    then fails its larger counts: the flag can tighten the profile, never loosen
    it into something a compact tree would satisfy that the full round would not.
    """
    submission = build_compact_submission(tmp_path / COMPACT_TRAINING, COMPACT_TRAINING)
    state = imp.plan(COMPACT_TRAINING, submission, into, layout="package")

    assert state.layout.name == imp.LAYOUT_PACKAGE
    assert "sections" in failed(state), failed(state)

    # And the same folder read as its own (compact) profile passes -- proof the
    # failure above is the layout being tightened, not a broken submission.
    ok_state = imp.plan(COMPACT_TRAINING, submission, tmp_path / "into_compact")
    assert ok_state.layout.name == imp.LAYOUT_COMPACT
    assert ok_state.ok, {check.name: check.problems for check in ok_state.failures}


# ── 6: mono 24-bit PCM is read and kept as 24-bit ────────────────────────────


def test_mono_24bit_pcm_is_accepted(tmp_path: Path, into: Path) -> None:
    """A compact package handed in as mono 24-bit PCM passes and is read as 24-bit.

    An external device may deliver 24-bit takes; nothing here converts them. The
    container facts are read from the header -- 24 bits per sample, three bytes
    wide, ``pcm_s24le`` -- and the whole submission still passes every check.
    """
    submission = build_compact_submission(tmp_path / COMPACT_TRAINING, COMPACT_TRAINING, width=3)
    state = imp.plan(COMPACT_TRAINING, submission, into)

    assert state.ok, {check.name: check.problems for check in state.failures}
    assert "format" in {check.name for check in state.checks}
    assert "format" not in failed(state)

    row = row_for(state, f"positive_normal/{spec.WAKE_PHRASE_SLUG}_normal_001.wav")
    assert row.format is not None
    assert row.format.bits_per_sample == 24  # three-byte samples, read from the container
    assert row.format.codec == "pcm_s24le"


# ── 7 & 8: the continuous originals are ignored, unmanifested, and untouched ──


def test_continuous_sources_are_ignored_and_kept_out_of_the_manifest(
    tmp_path: Path, into: Path
) -> None:
    """A ``_continuous/`` archive beside the takes is never walked or manifested.

    The manual split leaves the seven continuous originals in ``root/_continuous/``,
    next to ``originals/``. Ingestion discovers only ``originals/``, so no classified
    recording comes from ``_continuous/``, the submission passes every check, and the
    verified transfer listing names no ``_continuous/`` path.
    """
    root = build_via_segment_fallback(tmp_path / COMPACT_TRAINING, COMPACT_TRAINING)
    assert segment_fallback.continuous_dir_for(root).is_dir()

    state = imp.plan(COMPACT_TRAINING, root, into)
    assert state.ok, {check.name: check.problems for check in state.failures}
    assert state.layout.name == imp.LAYOUT_COMPACT
    assert len(state.originals) == 55
    assert all(not row.path.startswith("_continuous") for row in state.originals)

    # The listing ingestion verified over the arrived bytes names no raw source.
    checksums = (root / spec.CHECKSUM_FILE).read_text(encoding="utf-8")
    assert "_continuous" not in checksums

    imp.execute(state, into)
    published = (into / (imp.SPEAKER_DIR_PREFIX + COMPACT_TRAINING)
                 / "ORIGINALS_MANIFEST.SHA256SUMS").read_text(encoding="utf-8")
    assert "_continuous" not in published


def test_continuous_originals_are_preserved_byte_for_byte(
    tmp_path: Path, into: Path
) -> None:
    """Import never touches the seven continuous originals.

    Their SHA-256 is identical before and after a full import, and all seven are
    still present: the raw source archive is preserved beside the submission, not
    consumed by it.
    """
    root = build_via_segment_fallback(tmp_path / COMPACT_TRAINING, COMPACT_TRAINING)
    continuous = segment_fallback.continuous_dir_for(root)
    sources = sorted(continuous.glob("*.wav"))
    assert len(sources) == 7

    before = {path.name: freeze_manifest.sha256_file(path) for path in sources}
    imp.execute(imp.plan(COMPACT_TRAINING, root, into), into)
    after = {path.name: freeze_manifest.sha256_file(path) for path in sources}

    assert after == before
    assert len(sorted(continuous.glob("*.wav"))) == 7


# ── _extras: preserved, reported, excluded, never silently consumed ───────────


def _write_extra_clip(path: Path) -> None:
    """A short, distinct 24-bit mono spare clip (not a full take set)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 48000
    t = np.arange(int(1.2 * rate)) / rate
    payload = np.round(0.3 * (2**23 - 1) * np.sin(2 * np.pi * 190.0 * t)).astype(np.int64)
    _write_pcm24(path, payload, rate)


def test_extras_are_tolerated_reported_preserved_and_excluded(
    tmp_path: Path, into: Path
) -> None:
    """An ``_extras/`` spare beside the package is kept, reported, and never ingested.

    The operator left an out-of-plan clip in ``_extras/``. The compact validator
    tolerates the directory (no unrecognised-entry) and reports it; ingestion
    classifies only ``originals/``, reports the extra as preserved-not-ingested,
    keeps it out of the verified and published manifests, and leaves its bytes
    untouched.
    """
    root = build_compact_submission(tmp_path / COMPACT_TRAINING, COMPACT_TRAINING)
    extra = root / core.EXTRAS_DIRNAME / "positive_close.wav"
    _write_extra_clip(extra)
    extra_sha = freeze_manifest.sha256_file(extra)

    # compact validator: tolerates and reports _extras, still GREEN.
    result = core.validate_compact_submission(root, core.build_plan(COMPACT_TRAINING))
    assert result.ok, result.errors
    assert not any("unrecognised entry" in error for error in result.errors)
    assert any(warning.startswith(core.EXTRAS_DIRNAME + "/") for warning in result.warnings)

    # ingestion: passes, classifies only originals, reports the extra, excludes it.
    state = imp.plan(COMPACT_TRAINING, root, into)
    assert state.ok, {check.name: check.problems for check in state.failures}
    assert all(not row.path.startswith(core.EXTRAS_DIRNAME) for row in state.originals)
    preserved = next(check for check in state.checks if check.name == "preserved_sources")
    assert preserved.ok
    assert f"{core.EXTRAS_DIRNAME}/positive_close.wav" in preserved.detail
    assert core.EXTRAS_DIRNAME not in (root / spec.CHECKSUM_FILE).read_text(encoding="utf-8")

    # execute: the extra is byte-identical afterwards and absent from the frozen manifest.
    imp.execute(state, into)
    assert freeze_manifest.sha256_file(extra) == extra_sha
    published = (
        into / (imp.SPEAKER_DIR_PREFIX + COMPACT_TRAINING) / "ORIGINALS_MANIFEST.SHA256SUMS"
    ).read_text(encoding="utf-8")
    assert core.EXTRAS_DIRNAME not in published
