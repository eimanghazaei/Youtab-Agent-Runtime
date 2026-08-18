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
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM -- what the in-app recorder writes.
#: Sample widths the *validation and inspection* path accepts. The GUI recorder
#: still writes 16-bit, but a take handed in from an external device may be
#: 24-bit PCM (3 bytes); both are honoured, and neither is ever converted.
SUPPORTED_SAMPLE_WIDTHS = (2, 3)  # 16-bit and 24-bit PCM
CHANNELS = 1  # mono
_INT16_FULL_SCALE = 32768.0

#: A raw-source directory that may sit next to ``originals/`` inside a speaker
#: folder: the seven continuous recordings a manual submission is split from.
#: Preserved, never ingested, and tolerated by the compact validator -- it is a
#: source archive, not part of the submission's take set.
CONTINUOUS_DIRNAME = "_continuous"

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


def _decode_pcm_scaled(raw: bytes, width: int, channels: int) -> np.ndarray:
    """Interleaved 16- or 24-bit PCM bytes -> mono float64 in [-1, 1]."""
    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / _INT16_FULL_SCALE
    elif width == 3:
        # 24-bit little-endian signed: rebuild each 3-byte sample as int32.
        byts = np.frombuffer(raw, dtype=np.uint8)
        usable = byts.size - (byts.size % 3)
        triples = byts[:usable].reshape(-1, 3).astype(np.int32)
        ints = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        ints = np.where(ints >= (1 << 23), ints - (1 << 24), ints)
        samples = ints.astype(np.float64) / float(1 << 23)
    else:  # pragma: no cover - guarded by read_scaled_any
        raise wave.Error(f"unsupported sample width {width * 8}-bit")
    if channels > 1:
        usable = samples.size - (samples.size % channels)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples)


def read_scaled_any(path: Path) -> tuple[np.ndarray, int, int]:
    """Read a 16- or 24-bit PCM WAV as mono float64 in [-1, 1]; return (samples, rate, width).

    The amplitude-only reader used by inspection and by the manual fallback: it
    decodes level for the silence / clipping / duration findings without forcing a
    bit depth and without writing anything. A 24-bit source is *never* rewritten
    through this -- the fallback slices the original bytes for its takes, so the
    stored audio stays byte-identical.
    """
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width not in SUPPORTED_SAMPLE_WIDTHS:
        raise wave.Error(f"expected 16- or 24-bit PCM, found {width * 8}-bit")
    return _decode_pcm_scaled(raw, width, channels), rate, width


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
    """Findings and measurements for an in-memory capture. Never deletes anything.

    Accepts the recorder's native 16-bit take and normalises it; the width-aware
    path (``inspect_wav`` on a 16- or 24-bit file) shares the same findings via
    ``_inspect_scaled``, so a 24-bit take is judged by identical rules.
    """
    samples = to_int16(pcm)
    scaled = samples.astype(np.float64) / _INT16_FULL_SCALE
    return _inspect_scaled(
        scaled,
        rate,
        int(samples.size),
        expect_speech=expect_speech,
        min_seconds=min_seconds,
        target_seconds=target_seconds,
    )


def _inspect_scaled(
    scaled: np.ndarray,
    rate: int,
    n_samples: int,
    *,
    expect_speech: bool,
    min_seconds: float,
    target_seconds: float | None,
) -> TakeInspection:
    """Shared findings logic over mono float samples in [-1, 1], any bit depth."""
    inspection = TakeInspection(sample_rate_hz=rate, samples=int(n_samples))
    if n_samples == 0 or rate <= 0:
        inspection.findings.append(
            Finding(FINDING_EMPTY, "that came out empty -- nothing was captured. Redo?", True)
        )
        return inspection

    magnitude = np.abs(scaled)
    peak = float(magnitude.max())
    rms = float(np.sqrt(np.mean(np.square(scaled))))
    inspection.duration_s = n_samples / rate
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
    """Inspect a take on disk (16- or 24-bit PCM). Unopenable is reported, not raised."""
    try:
        scaled, rate, _width = read_scaled_any(Path(path))
    except (wave.Error, EOFError, OSError, ValueError):
        inspection = TakeInspection(readable=False)
        inspection.findings.append(
            Finding(FINDING_UNREADABLE, "that file will not open as audio -- Redo?", True)
        )
        return inspection
    return _inspect_scaled(
        scaled,
        rate,
        int(scaled.size),
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
    disagree. The ``_continuous/`` raw-source archive (present only for a manual
    submission) is excluded: it is preserved next to the takes, not part of them.
    """
    out: list[str] = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == spec.CHECKSUM_FILE or rel.startswith(f"{CONTINUOUS_DIRNAME}/"):
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


# ── the compact validator (the operator's one-click check) ───────────────────
#
# ``validate_speaker_submission.py`` is the E003-E007 round's validator: it holds
# a folder to the *full* package (canonical take counts, both far-field
# conditions, the E003-E007 assignment table) and never opens the audio. Run
# against a correct compact E001/E002 folder it reports dozens of "shortfalls"
# that are not faults, which would bury a real one.
#
# This is the compact round's validator instead. It holds a folder to the
# *compact plan* the assistant recorded it from, so a complete, correct compact
# submission is GREEN -- and, because this is a recording-time tool on the same
# machine that just captured the audio, it *does* open every take to catch a
# silent, too-short, clipped, unreadable, wrong-format or sub-16 kHz recording
# that a listing-only check cannot see. E001 and E002 are both valid here.


@dataclass
class CompactValidation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sections: list[SectionStatus] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


_FINDING_PHRASE = {
    FINDING_UNREADABLE: "will not open as audio (unreadable); re-record this take",
    FINDING_EMPTY: "silent / empty take; re-record this take",
    FINDING_TOO_SHORT: "too short; re-record this take",
    FINDING_CLIPPED: "clipped (input too hot); move back or lower the input and re-record",
}


def _compact_submission_files(root: Path) -> list[str]:
    """Files in the submission except SHA256SUMS, the progress dotfile, and _continuous/."""
    out: list[str] = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in (spec.CHECKSUM_FILE, STATE_FILENAME):
            continue
        if rel.startswith(f"{CONTINUOUS_DIRNAME}/"):
            continue  # raw source archive, preserved but not part of the take set
        out.append(rel)
    return out


def _take_problems(path: Path, step: Step) -> list[str]:
    """Format and content problems with one recorded take, or an empty list."""
    rel = step.rel_path
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
    except (wave.Error, EOFError, OSError):
        return [f"{rel}: {_FINDING_PHRASE[FINDING_UNREADABLE]}"]

    problems: list[str] = []
    if width not in SUPPORTED_SAMPLE_WIDTHS:
        problems.append(f"{rel}: {width * 8}-bit PCM, expected 16- or 24-bit")
    if channels != CHANNELS:
        problems.append(f"{rel}: {channels} channels, expected mono")
    if rate < MIN_SAMPLE_RATE_HZ:
        problems.append(f"{rel}: sample rate {rate} Hz is below the 16 kHz floor")

    # Content findings only make sense once the container is a supported PCM width.
    if width in SUPPORTED_SAMPLE_WIDTHS:
        inspection = inspect_wav(
            path,
            expect_speech=step.expect_speech,
            min_seconds=step.min_seconds,
            target_seconds=step.target_seconds,
        )
        for finding in inspection.findings:
            if finding.blocking:
                phrase = _FINDING_PHRASE.get(finding.code, finding.message)
                problems.append(f"{rel}: {phrase}")
    return problems


def _check_compact_originals(root: Path, plan: Plan, result: CompactValidation) -> None:
    originals = root / spec.ORIGINALS_DIR
    if not originals.is_dir():
        result.errors.append(f"missing required folder: {spec.ORIGINALS_DIR}/")
        return

    expected_by_rel = {step.rel_path: step for step in plan.steps}
    expected_dirs = {step.directory for step in plan.steps}

    # Every planned take must be present, well-formed and not a rejected capture.
    for step in plan.steps:
        path = step.path(root)
        if not path.is_file():
            result.errors.append(f"missing take: {step.rel_path}")
            continue
        if path.stat().st_size == 0:
            result.errors.append(f"{step.rel_path}: empty file (0 bytes); re-record this take")
            continue
        result.errors.extend(_take_problems(path, step))

    # Nothing else may be in the tree: an extra or misnamed file is a fault
    # because auto-naming hands out take numbers by position.
    for entry in sorted(originals.iterdir()):
        if entry.is_dir():
            if entry.name not in expected_dirs:
                result.errors.append(
                    f"unrecognised folder in {spec.ORIGINALS_DIR}/: {entry.name}/ "
                    "(not part of this speaker's compact plan)"
                )
                continue
            for member in sorted(entry.iterdir()):
                rel = f"{spec.ORIGINALS_DIR}/{entry.name}/{member.name}"
                if member.is_dir():
                    result.errors.append(f"{rel}: expected a file, found a folder")
                elif rel not in expected_by_rel:
                    result.errors.append(
                        f"unexpected file not in the plan: {rel} (a misnamed or extra take)"
                    )
        elif entry.is_file():
            result.errors.append(
                f"unexpected file directly in {spec.ORIGINALS_DIR}/: {entry.name}"
            )


def _check_compact_consent(root: Path, result: CompactValidation) -> None:
    path = root / spec.CONSENT_FILE
    if not path.is_file():
        result.errors.append(
            f"missing {spec.CONSENT_FILE}: the signed consent record has to be in the "
            "folder before the recordings can be handed over"
        )
        return
    if path.stat().st_size == 0:
        result.errors.append(f"{spec.CONSENT_FILE} is empty")


def _check_compact_metadata(root: Path, plan: Plan, result: CompactValidation) -> None:
    path = root / spec.METADATA_FILE
    if not path.is_file():
        result.errors.append(f"missing {spec.METADATA_FILE}")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result.errors.append(f"{spec.METADATA_FILE} is not well-formed JSON: {exc}")
        return
    if not isinstance(data, dict):
        result.errors.append(f"{spec.METADATA_FILE} must contain a single JSON object")
        return

    for name in spec.REQUIRED_METADATA_FIELDS:
        if name not in data:
            result.errors.append(f"{spec.METADATA_FILE} is missing required field {name!r}")
            continue
        if name == "noise_sources_used":
            continue  # checked below
        value = data[name]
        if not isinstance(value, str) or not value.strip():
            result.errors.append(
                f"{spec.METADATA_FILE} field {name!r} must be a non-empty string"
            )
        elif value.strip().startswith("<"):
            result.errors.append(
                f"{spec.METADATA_FILE} field {name!r} still has the template placeholder "
                "in it; fill in a real answer"
            )

    for name in data:
        if name not in spec.ALL_METADATA_FIELDS:
            result.errors.append(f"{spec.METADATA_FILE} has an unrecognised field {name!r}")

    declared = data.get("speaker_id")
    if isinstance(declared, str) and declared.strip() and not declared.strip().startswith("<"):
        if declared.strip() != plan.speaker:
            result.errors.append(
                f"{spec.METADATA_FILE} names {declared.strip()!r} but the folder is "
                f"{plan.speaker!r}"
            )

    expected_noise = set(compact_plan.noise_sources_for(plan.speaker))
    noise = data.get("noise_sources_used")
    if not isinstance(noise, list) or not noise:
        result.errors.append(
            f"{spec.METADATA_FILE} field 'noise_sources_used' must be a non-empty list"
        )
    elif set(noise) != expected_noise:
        result.errors.append(
            f"{spec.METADATA_FILE} 'noise_sources_used' is {sorted(noise)} but this "
            f"speaker's compact plan records {sorted(expected_noise)}"
        )


def _check_compact_checksums(root: Path, result: CompactValidation) -> None:
    path = root / spec.CHECKSUM_FILE
    if not path.is_file():
        result.errors.append(
            f"missing {spec.CHECKSUM_FILE}: without it the folder cannot be shown to have "
            "survived the transfer intact"
        )
        return

    listed: dict[str, str] = {}
    seen: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        match = spec.CHECKSUM_LINE.match(line)
        if not match:
            result.errors.append(
                f"{spec.CHECKSUM_FILE} line {number} is not coreutils format "
                f"(64 hex, two spaces, path): {line.strip()!r}"
            )
            continue
        digest, rel = match.group(1), match.group(2)
        seen.append(rel)
        listed[rel] = digest

    for rel in sorted({name for name in seen if seen.count(name) > 1}):
        result.errors.append(f"{spec.CHECKSUM_FILE} lists {rel!r} more than once")

    present = set(_compact_submission_files(root))
    # Unlike the full round's listing-only check, verify the digests here: this
    # runs on the machine that wrote the files, and a wrong digest means the
    # manifest and the bytes already disagree before any transfer.
    for rel, digest in listed.items():
        target = root / rel
        if not target.is_file():
            result.errors.append(
                f"{spec.CHECKSUM_FILE} lists {rel!r}, which is not in the folder"
            )
        elif sha256_file(target) != digest:
            result.errors.append(
                f"{spec.CHECKSUM_FILE} records a different digest for {rel!r}: the bytes "
                "changed since the manifest was written"
            )
    for rel in sorted(present - set(listed)):
        result.errors.append(f"{spec.CHECKSUM_FILE} does not list {rel!r}")


def validate_compact_submission(root: Path, plan: Plan) -> CompactValidation:
    """Validate a compact speaker folder against ``plan``. GREEN when complete.

    Returns a result whose ``ok`` is True, with no errors and no warnings, for a
    complete and correct compact submission: exactly the plan's takes present and
    correctly named, every WAV mono/16-bit/>=16 kHz and free of a blocking
    quality finding, ``RECORDING_METADATA.json`` complete, ``CONSENT.pdf``
    present, and ``SHA256SUMS`` present and internally consistent. Every reported
    error is a genuine problem the operator can act on -- there is no
    canonical-count, dropped-section or assignment-table noise.
    """
    result = CompactValidation()
    root = Path(root)
    if not root.is_dir():
        result.errors.append(f"{root} is not a directory")
        return result

    if plan.speaker not in compact_plan.COMPACT_NOISE_ASSIGNMENTS:
        result.errors.append(
            f"{plan.speaker!r} is not a compact-round speaker "
            f"({sorted(compact_plan.COMPACT_NOISE_ASSIGNMENTS)})"
        )
    if root.name != plan.speaker:
        result.errors.append(
            f"the folder is named {root.name!r} but the plan is for {plan.speaker!r}"
        )

    tolerated = set(spec.SUBMISSION_ENTRIES) | {STATE_FILENAME, CONTINUOUS_DIRNAME}
    for entry in sorted(root.iterdir()):
        if entry.name not in tolerated:
            result.errors.append(
                f"unrecognised entry in {root.name}/: {entry.name} -- the submission holds "
                f"exactly {', '.join(spec.SUBMISSION_ENTRIES)} (plus an optional "
                f"{CONTINUOUS_DIRNAME}/ raw-source archive) and nothing else"
            )

    _check_compact_originals(root, plan, result)
    _check_compact_consent(root, result)
    _check_compact_metadata(root, plan, result)
    _check_compact_checksums(root, result)

    result.sections = section_status(plan, load_progress(plan, root))
    return result


def format_compact_report(result: CompactValidation, plan: Plan) -> str:
    """A short operator-facing summary: GREEN, or only the real problems."""
    lines = [f"Compact submission check for {plan.speaker}:"]
    for status in result.sections:
        marker = "ok" if status.complete else "incomplete"
        short = status.title.split(" - ")[0]
        lines.append(f"  {short:<12} {status.have:>3}/{status.need:<3}  {marker}")
    if result.ok:
        lines.append("  GREEN: complete and correct -- ready for handoff.")
    else:
        lines.append(f"  {len(result.errors)} problem(s) to fix:")
        lines.extend(f"    - {error}" for error in result.errors)
    for warning in result.warnings:
        lines.append(f"  note: {warning}")
    return "\n".join(lines)
