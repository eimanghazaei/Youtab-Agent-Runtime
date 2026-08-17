"""Non-audio, non-GUI logic for the compact recording assistant.

Everything a test can check without a microphone, a speaker or a display lives
here: the plan/section/step model, the automatic filename and directory
assignment, resumable progress that reconciles against the files actually on
disk, per-take quality inspection (empty / too short / clipped / unreadable),
the SHA-256 manifest, the metadata form, and the record/keep/redo/replay
session controller. The controller takes its "speak the prompt" and "capture
the microphone" behaviours as injected callables, so the whole flow -- and the
single most important rule, that a written take contains only the captured
human stream and never the prompt -- is exercised in tests with fakes and no
hardware.

Import-safe with no audio stack present: this module imports only the standard
library, ``numpy`` (already a project dependency) and ``speaker_recording_spec``.
``audio.py`` is where ``sounddevice`` and ``pyttsx3`` are touched, lazily.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import speaker_recording_spec as spec  # noqa: E402

from . import compact_plan
from .compact_plan import KIND_FREEFORM, KIND_NEAR_PHRASE, KIND_POSITIVE

# ── audio format the app writes ──────────────────────────────────────────────

#: The rate the app records at. Human voice, mono, 16-bit PCM. 16 kHz is the
#: production front end's rate (``import_speaker.PRODUCTION_SAMPLE_RATE_HZ``);
#: a take below it cannot be brought up to that rate without inventing content,
#: so the app refuses to write one.
SAMPLE_RATE_HZ = 16000
MIN_SAMPLE_RATE_HZ = 16000
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM
CHANNELS = 1  # mono
_INT16_FULL_SCALE = 32768.0

# ── quality thresholds, and why each is where it is ──────────────────────────
#
# These surface findings to the human ("that came out silent -- Redo?"); none of
# them ever deletes or rewrites a take. An accepted take is kept whatever these
# say, exactly as import_speaker keeps quiet/distant/noisy/clipped takes.

#: A spoken take whose RMS sits below this is silence, not a quiet delivery: a
#: muted microphone or a capture that never started. Genuinely quiet speech (the
#: ``positive_quiet`` condition) still lands tens of dB above this, so the floor
#: catches "nothing was recorded" without flagging a real quiet take. Not applied
#: to the background-only section, where near-silence is the point.
EMPTY_RMS_DBFS = -55.0

#: Shortest a discrete utterance may be before it reads as truncated. Set just
#: above import_speaker's 0.25 s hard floor, and below the length of the
#: shortest one-word near phrases ("hey.", "two.", "no."), which run ~0.3-0.5 s.
MIN_UTTERANCE_SECONDS = 0.30

#: A continuous section (free speech / background) this short is a capture that
#: failed, not merely under target. Separate from the soft "short of target"
#: finding below.
MIN_FREEFORM_SECONDS = 20.0

#: A sample is "at full scale" within this of 1.0 -- the same tolerance
#: import_speaker uses, covering int16's 32767/32768 top code.
FULL_SCALE_TOLERANCE = 1e-4

#: Consecutive full-scale samples that make a flat clipped top rather than a
#: momentary peak.
CLIP_RUN_SAMPLES = 3

#: Fraction of samples pinned at full scale (in runs) above which a take is
#: reported as clipped. 0.5 % of samples flat-topped is audibly clipped; a clean
#: take has none.
CLIP_FRACTION_THRESHOLD = 0.005

#: Below this fraction of a continuous section's target length, the take is
#: materially short of what was asked and worth a soft note -- never a redo
#: demand, because the recorder may have split one long take into several files.
SHORT_OF_TARGET_FRACTION = 0.5

#: dBFS reported for a frame with no signal at all (a real -inf is not JSON).
SILENCE_DBFS = -120.0

# ── finding codes ────────────────────────────────────────────────────────────

FINDING_UNREADABLE = "unreadable"
FINDING_EMPTY = "empty"
FINDING_TOO_SHORT = "too_short"
FINDING_CLIPPED = "clipped"
FINDING_SHORT_OF_TARGET = "short_of_target"


# ── the plan / section / step model ──────────────────────────────────────────


@dataclass(frozen=True)
class Step:
    """One take the speaker records: exactly one file, auto-named.

    The speaker never types ``stem``. It is fixed by the step's position in its
    section, in the order the plan lays the sections out, and it is built from
    the same tokens ``validate_speaker_submission.py`` matches on, so the
    written file is ingestible by name.
    """

    section_id: str
    directory: str
    stem: str
    take: int
    prompt: str
    instruction: str
    kind: str
    expect_speech: bool
    min_seconds: float
    target_seconds: float | None

    @property
    def filename(self) -> str:
        return f"{self.stem}.wav"

    @property
    def rel_path(self) -> str:
        """POSIX path inside the speaker folder, e.g. ``originals/near_phrase/hey._001.wav``."""
        return f"{spec.ORIGINALS_DIR}/{self.directory}/{self.filename}"

    @property
    def key(self) -> str:
        """Stable identity used in the progress file -- the relative path."""
        return self.rel_path

    def path(self, speaker_root: Path) -> Path:
        return speaker_root / spec.ORIGINALS_DIR / self.directory / self.filename


@dataclass(frozen=True)
class Section:
    section_id: str
    title: str
    instruction: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Plan:
    speaker: str
    sections: tuple[Section, ...]

    @property
    def steps(self) -> tuple[Step, ...]:
        return tuple(step for section in self.sections for step in section.steps)

    def step_by_key(self, key: str) -> Step | None:
        for step in self.steps:
            if step.key == key:
                return step
        return None


def _min_seconds_for(kind: str) -> float:
    return MIN_FREEFORM_SECONDS if kind == KIND_FREEFORM else MIN_UTTERANCE_SECONDS


def _steps_for_group(section_id: str, group: compact_plan.ConditionGroup) -> tuple[Step, ...]:
    """One Step per repetition, named ``<stem_prefix>_NNN`` in take order."""
    expect_speech = group.directory != "background_only"
    return tuple(
        Step(
            section_id=section_id,
            directory=group.directory,
            stem=f"{group.stem_prefix}_{take:0{spec.TAKE_DIGITS}d}",
            take=take,
            prompt=group.prompt,
            instruction=group.instruction,
            kind=group.kind,
            expect_speech=expect_speech,
            min_seconds=_min_seconds_for(group.kind),
            target_seconds=group.target_seconds,
        )
        for take in range(1, group.reps + 1)
    )


def build_plan(speaker: str) -> Plan:
    """The compact plan for ``speaker``, with every file's name already assigned."""
    sections = []
    for section_plan in compact_plan.build_sections(speaker):
        steps: list[Step] = []
        for group in section_plan.groups:
            steps.extend(_steps_for_group(section_plan.section_id, group))
        sections.append(
            Section(section_plan.section_id, section_plan.title, section_plan.instruction, tuple(steps))
        )
    return Plan(speaker, tuple(sections))


# ── wave IO: the app writes only the captured human stream ───────────────────


def validate_sample_rate(rate: int) -> None:
    if rate < MIN_SAMPLE_RATE_HZ:
        raise ValueError(
            f"sample rate {rate} Hz is below the {MIN_SAMPLE_RATE_HZ} Hz floor; "
            "the app records at 16 kHz or higher and never resamples a take up"
        )


def to_int16(pcm) -> np.ndarray:
    """A mono int16 view of captured samples, accepting int16 or float [-1, 1]."""
    array = np.asarray(pcm)
    if array.ndim > 1:
        array = array.reshape(-1)
    if array.dtype == np.int16:
        return np.ascontiguousarray(array)
    if np.issubdtype(array.dtype, np.floating):
        clipped = np.clip(array, -1.0, 1.0)
        return np.ascontiguousarray(np.round(clipped * (_INT16_FULL_SCALE - 1)).astype(np.int16))
    return np.ascontiguousarray(array.astype(np.int16))


def write_wave(path: Path, pcm, rate: int = SAMPLE_RATE_HZ) -> None:
    """Write ``pcm`` to ``path`` as mono 16-bit PCM WAV -- and nothing else.

    Only the samples handed in are written. There is no path by which the TTS
    prompt, or any generated audio, reaches this function: the take on disk is
    exactly the captured stream. This is the single most important audio rule in
    the project, and it is enforced by this function doing one thing.
    """
    validate_sample_rate(rate)
    samples = to_int16(pcm)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())


def read_wave(path: Path) -> tuple[np.ndarray, int]:
    """Samples (int16, mono) and the sample rate. Raises on an unreadable file."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != SAMPLE_WIDTH_BYTES:
        raise wave.Error(f"expected 16-bit PCM, found {width * 8}-bit")
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return np.ascontiguousarray(samples), rate


# ── per-take quality inspection ──────────────────────────────────────────────


@dataclass
class Finding:
    code: str
    message: str
    blocking: bool  # True => the app suggests a Redo


@dataclass
class TakeInspection:
    findings: list[Finding] = field(default_factory=list)
    readable: bool = True
    samples: int = 0
    sample_rate_hz: int = 0
    duration_s: float = 0.0
    peak_dbfs: float = SILENCE_DBFS
    rms_dbfs: float = SILENCE_DBFS
    clip_fraction: float = 0.0

    @property
    def ok(self) -> bool:
        """No blocking finding -- safe to Keep without a prompt to redo."""
        return not any(finding.blocking for finding in self.findings)

    @property
    def codes(self) -> list[str]:
        return [finding.code for finding in self.findings]


def _dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return SILENCE_DBFS
    return max(float(20.0 * np.log10(amplitude)), SILENCE_DBFS)


def _clip_fraction(magnitude: np.ndarray) -> float:
    """Fraction of samples that sit in a full-scale run of >= CLIP_RUN_SAMPLES."""
    if magnitude.size == 0:
        return 0.0
    at_full = magnitude >= (1.0 - FULL_SCALE_TOLERANCE)
    if not at_full.any():
        return 0.0
    padded = np.concatenate(([False], at_full, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = edges[0::2], edges[1::2]
    lengths = ends - starts
    clipped = int(lengths[lengths >= CLIP_RUN_SAMPLES].sum())
    return clipped / magnitude.size


def inspect_pcm(
    pcm,
    rate: int,
    *,
    expect_speech: bool = True,
    min_seconds: float = MIN_UTTERANCE_SECONDS,
    target_seconds: float | None = None,
) -> TakeInspection:
    """Findings and measurements for an in-memory capture. Never deletes anything."""
    samples = to_int16(pcm)
    inspection = TakeInspection(sample_rate_hz=rate, samples=int(samples.size))
    if samples.size == 0 or rate <= 0:
        inspection.findings.append(
            Finding(FINDING_EMPTY, "that came out empty -- nothing was captured. Redo?", True)
        )
        return inspection

    scaled = samples.astype(np.float64) / _INT16_FULL_SCALE
    magnitude = np.abs(scaled)
    peak = float(magnitude.max())
    rms = float(np.sqrt(np.mean(np.square(scaled))))
    inspection.duration_s = samples.size / rate
    inspection.peak_dbfs = _dbfs(peak)
    inspection.rms_dbfs = _dbfs(rms)
    inspection.clip_fraction = _clip_fraction(magnitude)

    if expect_speech and inspection.rms_dbfs <= EMPTY_RMS_DBFS:
        inspection.findings.append(
            Finding(FINDING_EMPTY, "that came out silent -- was the microphone muted? Redo?", True)
        )
    if inspection.duration_s < min_seconds:
        inspection.findings.append(
            Finding(
                FINDING_TOO_SHORT,
                f"that was very short ({inspection.duration_s:.2f}s) -- Redo?",
                True,
            )
        )
    if inspection.clip_fraction > CLIP_FRACTION_THRESHOLD:
        inspection.findings.append(
            Finding(
                FINDING_CLIPPED,
                f"that clipped ({inspection.clip_fraction * 100:.1f}% of samples at full "
                "scale) -- move back or lower the input and Redo?",
                True,
            )
        )
    if target_seconds and inspection.duration_s < target_seconds * SHORT_OF_TARGET_FRACTION:
        inspection.findings.append(
            Finding(
                FINDING_SHORT_OF_TARGET,
                f"that is well under the ~{target_seconds / 60:.0f} min target "
                f"({inspection.duration_s:.0f}s); it is kept, but longer would help.",
                False,
            )
        )
    return inspection


def inspect_wav(
    path: Path,
    *,
    expect_speech: bool = True,
    min_seconds: float = MIN_UTTERANCE_SECONDS,
    target_seconds: float | None = None,
) -> TakeInspection:
    """Inspect a take on disk. An unopenable/unparsable WAV is reported, not raised."""
    try:
        samples, rate = read_wave(Path(path))
    except (wave.Error, EOFError, OSError, ValueError):
        inspection = TakeInspection(readable=False)
        inspection.findings.append(
            Finding(FINDING_UNREADABLE, "that file will not open as audio -- Redo?", True)
        )
        return inspection
    return inspect_pcm(
        samples,
        rate,
        expect_speech=expect_speech,
        min_seconds=min_seconds,
        target_seconds=target_seconds,
    )


def inspect_step_pcm(step: Step, pcm, rate: int) -> TakeInspection:
    return inspect_pcm(
        pcm,
        rate,
        expect_speech=step.expect_speech,
        min_seconds=step.min_seconds,
        target_seconds=step.target_seconds,
    )


# ── SHA-256 manifest (coreutils format, exactly what the validator reads) ─────


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def submission_files(root: Path) -> list[str]:
    """Every file in the speaker folder except SHA256SUMS, POSIX-relative, sorted.

    The same walk ``validate_speaker_submission.py`` verifies a manifest
    against, so what this lists and what the validator later checks cannot
    disagree.
    """
    out: list[str] = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == spec.CHECKSUM_FILE:
            continue
        out.append(rel)
    return out


def write_sha256sums(root: Path) -> int:
    """Write ``root/SHA256SUMS`` over every other file, coreutils format.

    ``<64 hex><two spaces><relative path>``. Returns the number of files listed.
    Not the transfer check -- a digest taken here says nothing about a copy that
    has not happened; the coordinator runs ``sha256sum -c`` after the transfer.
    """
    root = Path(root)
    lines = [f"{sha256_file(root / rel)}  {rel}\n" for rel in submission_files(root)]
    (root / spec.CHECKSUM_FILE).write_text("".join(lines), encoding="utf-8")
    return len(lines)


# ── the device / environment metadata form ───────────────────────────────────

#: Fields the app can fill on the speaker's behalf without asking; the rest are
#: the human's to describe, and are left as placeholders in a draft.
_PLACEHOLDER = {
    "device_make_model": "<the phone or computer you recorded on, e.g. iPhone 13>",
    "recording_app": "<the app you recorded with, e.g. the recording assistant>",
    "room_name": "<the room, e.g. living room>",
    "room_size_approx": "<roughly how big, e.g. about 4 by 5 metres>",
    "floor_surface": "<e.g. carpet, tile, wood>",
    "wall_surface": "<e.g. drywall, brick, one large window>",
    "background_sources_present": "<anything usually making noise here, even quietly>",
    "farfield_distance": "<how far you stood for the far-field section>",
    "consent_signed_date": "<YYYY-MM-DD, the day you signed the consent form>",
}


def _today() -> str:
    return _dt.date.today().isoformat()


def draft_metadata(speaker: str, noise_sources: tuple[str, ...] | list[str]) -> dict:
    """A metadata form pre-filled with what the app knows, placeholders for the rest.

    The validator rejects the placeholder text, on purpose: a draft is not a
    finished form, and the human (or coordinator) fills the ``<...>`` fields
    before handoff.
    """
    data = {name: _PLACEHOLDER.get(name, "") for name in spec.REQUIRED_METADATA_FIELDS}
    data["speaker_id"] = speaker
    data["recording_date"] = _today()
    data["noise_sources_used"] = list(noise_sources)
    data["notes"] = ""
    return data


def build_metadata(
    speaker: str,
    noise_sources: tuple[str, ...] | list[str],
    answers: dict | None = None,
) -> dict:
    """A metadata form with the human's answers merged over the draft."""
    data = draft_metadata(speaker, noise_sources)
    if answers:
        for name, value in answers.items():
            if name in spec.ALL_METADATA_FIELDS:
                data[name] = value
    data["speaker_id"] = speaker
    data["noise_sources_used"] = list(noise_sources)
    return data


def write_metadata(root: Path, data: dict) -> None:
    (Path(root) / spec.METADATA_FILE).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# ── resumable progress, reconciled against the files on disk ─────────────────

#: Kept as a dotfile in the speaker folder during recording, so the folder the
#: validator later sees (originals/, CONSENT.pdf, RECORDING_METADATA.json,
#: SHA256SUMS -- and nothing else) is clean: ``finalize_submission`` removes this
#: before writing the manifest. On G:, never in git.
STATE_FILENAME = ".recording_state.json"
STATE_SCHEMA = 1


@dataclass
class Progress:
    speaker: str
    completed: set[str] = field(default_factory=set)


def state_path(speaker_root: Path) -> Path:
    return Path(speaker_root) / STATE_FILENAME


def _completed_on_disk(plan: Plan, speaker_root: Path) -> set[str]:
    """The steps whose take actually exists on disk with real bytes.

    Disk is the source of truth for "done": a resumed session continues where
    the *files* stopped, not where a possibly-stale state note claims they did.
    """
    done: set[str] = set()
    for step in plan.steps:
        path = step.path(speaker_root)
        if path.is_file() and path.stat().st_size > 0:
            done.add(step.key)
    return done


def load_progress(plan: Plan, speaker_root: Path) -> Progress:
    """Progress for ``plan`` under ``speaker_root``, reconciled against disk.

    A relaunch reads the state file if there is one, then intersects it with
    what is genuinely on disk and adds anything found on disk the note missed --
    so a state file that disagrees with the folder loses to the folder, and a
    deleted take is re-recorded rather than skipped.
    """
    speaker_root = Path(speaker_root)
    recorded: set[str] = set()
    path = state_path(speaker_root)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                recorded = {str(key) for key in data.get("completed", [])}
        except (json.JSONDecodeError, OSError):
            recorded = set()

    valid_keys = {step.key for step in plan.steps}
    on_disk = _completed_on_disk(plan, speaker_root)
    completed = (recorded & valid_keys & on_disk) | on_disk
    return Progress(plan.speaker, completed)


def save_progress(progress: Progress, speaker_root: Path) -> None:
    payload = {
        "schema": STATE_SCHEMA,
        "speaker_id": progress.speaker,
        "updated": _dt.datetime.now().isoformat(timespec="seconds"),
        "completed": sorted(progress.completed),
    }
    path = state_path(Path(speaker_root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def next_step(plan: Plan, progress: Progress) -> Step | None:
    """The first step, in plan order, whose take is not yet done."""
    for step in plan.steps:
        if step.key not in progress.completed:
            return step
    return None


def remaining_steps(plan: Plan, progress: Progress) -> list[Step]:
    return [step for step in plan.steps if step.key not in progress.completed]


@dataclass
class SectionStatus:
    section_id: str
    title: str
    have: int
    need: int

    @property
    def complete(self) -> bool:
        return self.have >= self.need


def section_status(plan: Plan, progress: Progress) -> list[SectionStatus]:
    out: list[SectionStatus] = []
    for section in plan.sections:
        have = sum(1 for step in section.steps if step.key in progress.completed)
        out.append(SectionStatus(section.section_id, section.title, have, len(section.steps)))
    return out


# ── the record / keep / redo / replay / pause / resume controller ────────────


@dataclass
class PendingTake:
    step: Step
    pcm: np.ndarray
    rate: int
    inspection: TakeInspection


class Session:
    """Drives one speaker's sitting over injected speak/capture callables.

    ``speak(text)`` guides the human and must *return before* ``capture`` is
    called: the microphone is enabled only after the prompt finishes, so the
    two never overlap and the TTS is never in the recording. ``capture(step)``
    returns ``(pcm, rate)`` -- the human stream. Neither is imported here; the
    GUI wires the real ones from ``audio.py`` and a test wires fakes, which is
    how the "prompt is never recorded" rule is proven without a microphone.
    """

    def __init__(
        self,
        plan: Plan,
        speaker_root: Path,
        *,
        speak=None,
        capture=None,
        tts_enabled: bool = True,
    ) -> None:
        self.plan = plan
        self.root = Path(speaker_root)
        self._speak = speak or (lambda text: None)
        self._capture = capture
        self.tts_enabled = tts_enabled
        self.progress = load_progress(plan, self.root)
        self._pending: PendingTake | None = None
        self._last_kept: PendingTake | None = None
        self._paused = False

    # -- navigation --
    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def pending(self) -> PendingTake | None:
        return self._pending

    @property
    def current_step(self) -> Step | None:
        """The step being worked on: the pending one, else the next not-done one."""
        if self._pending is not None:
            return self._pending.step
        return next_step(self.plan, self.progress)

    @property
    def done(self) -> bool:
        return self.current_step is None

    def guidance_text(self, step: Step) -> str:
        return compose_guidance(step)

    # -- the buttons --
    def record(self):
        """Guide, then capture the current step. Sequential: speak fully, then record.

        Returns the take's ``TakeInspection`` so the app can surface a finding
        ("that came out silent -- Redo?") without ever acting on it automatically.
        """
        if self._paused:
            raise RuntimeError("the session is paused; resume before recording")
        if self._capture is None:
            raise RuntimeError("no capture backend wired into this session")
        step = self.current_step
        if step is None:
            raise RuntimeError("every step is already recorded")
        if self.tts_enabled:
            self._speak(self.guidance_text(step))  # blocking; returns before capture
        pcm, rate = self._capture(step)
        samples = to_int16(pcm)
        inspection = inspect_step_pcm(step, samples, rate)
        self._pending = PendingTake(step, samples, rate, inspection)
        return inspection

    def keep(self) -> Step:
        """Accept the pending take: write it once, mark the step done, persist.

        Written exactly once and never rewritten, normalised, trimmed or
        resampled afterwards -- the original is preserved as captured.
        """
        if self._pending is None:
            raise RuntimeError("there is no captured take to keep")
        take = self._pending
        write_wave(take.step.path(self.root), take.pcm, take.rate)
        self.progress.completed.add(take.step.key)
        save_progress(self.progress, self.root)
        self._last_kept = take
        self._pending = None
        return take.step

    def redo(self) -> None:
        """Discard the just-captured, *un-kept* take so the same step is re-recorded.

        Only ever throws away the current unaccepted capture. A take the human
        already chose to Keep is on disk and is never touched here.
        """
        self._pending = None

    def replay(self) -> tuple[np.ndarray, int]:
        """The samples to play back: the pending take, else the last kept one."""
        source = self._pending or self._last_kept
        if source is None:
            raise RuntimeError("nothing has been captured yet to replay")
        return source.pcm, source.rate

    def pause(self) -> None:
        self._paused = True
        save_progress(self.progress, self.root)

    def resume(self) -> None:
        self._paused = False


def compose_guidance(step: Step) -> str:
    """The spoken/shown guidance for a step. Never written to a recording."""
    if step.kind == KIND_NEAR_PHRASE:
        base = f'Say, once, naturally: {step.prompt}'
    elif step.kind == KIND_FREEFORM:
        base = step.prompt
    else:
        base = f'Say: {step.prompt}'
    return f"{base} ({step.instruction})" if step.instruction else base


# ── finalisation: turn a recorded folder into a submittable one ──────────────


@dataclass
class FinalizeResult:
    metadata_path: Path
    checksum_count: int
    consent_present: bool
    state_removed: bool


def finalize_submission(
    plan: Plan,
    speaker_root: Path,
    answers: dict | None = None,
) -> FinalizeResult:
    """Write the metadata form and the manifest; clear the recording state file.

    Leaves the speaker folder holding exactly the four entries the validator
    accepts (``originals/``, ``CONSENT.pdf``, ``RECORDING_METADATA.json``,
    ``SHA256SUMS``) -- the transient progress dotfile is removed first, and the
    manifest is written last so it covers the metadata form. The signed
    ``CONSENT.pdf`` is the coordinator's to place; its presence is reported, not
    fabricated.
    """
    root = Path(speaker_root)
    state = state_path(root)
    state_removed = False
    if state.exists():
        state.unlink()
        state_removed = True

    noise_sources = compact_plan.noise_sources_for(plan.speaker)
    write_metadata(root, build_metadata(plan.speaker, noise_sources, answers))

    count = write_sha256sums(root)
    return FinalizeResult(
        metadata_path=root / spec.METADATA_FILE,
        checksum_count=count,
        consent_present=(root / spec.CONSENT_FILE).is_file(),
        state_removed=state_removed,
    )
