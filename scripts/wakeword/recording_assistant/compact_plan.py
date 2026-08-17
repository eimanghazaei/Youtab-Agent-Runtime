"""The compact "Hey Youtab" recording plan, as data.

The phrase *set* is single-sourced from ``speaker_recording_spec`` -- this
module reads the canonical positive conditions, the 34-row near-phrase battery
and the two continuous sections off that module and does not keep a second copy
of any phrase. What it owns is the compact *repetition counts*: a short,
guided sitting a volunteer can finish in roughly 12-16 minutes, rather than the
full round's ~65-minute package. Every directory name and filename token still
comes from ``speaker_recording_spec`` so a folder recorded from this plan is
named exactly the way ``validate_speaker_submission.py`` and ``import_speaker.py``
expect -- the counts are smaller, the naming is identical.

``core.py`` turns the section/group data here into concrete recording steps
(one file each) and assigns their filenames and directories. Nothing in this
module touches the filesystem, imports an audio stack, or knows about a GUI.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import speaker_recording_spec as spec  # noqa: E402

# ── step kinds ───────────────────────────────────────────────────────────────
# Kept here (the data module) rather than in ``core`` so ``core`` can import
# them without ``compact_plan`` having to import ``core`` back.

KIND_POSITIVE = "positive"
KIND_NEAR_PHRASE = "near_phrase"
KIND_FREEFORM = "freeform"

# ── the compact counts this module exists to own ─────────────────────────────

#: Section 1: each of the five close-mic delivery conditions, twice.
COMPACT_DELIVERY_REPS = 2

#: Section 2: far-field, three takes.
COMPACT_FARFIELD_REPS = 3

#: Section 2: each assigned real-noise source, three takes.
COMPACT_NOISE_REPS = 3

#: Section 3: every near-phrase row, once. Spoken naturally, no pause inside the
#: name.
COMPACT_NEAR_PHRASE_REPS = 1

#: Sections 4 and 5: one continuous file each.
COMPACT_FREEFORM_FILES = 1

#: The two real-noise sources each speaker records, splitting the four sources
#: in ``speaker_recording_spec.NOISE_SOURCE_VOCAB`` across the two humans so the
#: pair together cover all four. Both are genuine ``positive_noise_<source>``
#: directories the validator already knows.
COMPACT_NOISE_ASSIGNMENTS: dict[str, tuple[str, ...]] = {
    "E001": ("tv", "kitchen"),
    "E002": ("street", "fan"),
}

#: The five close-mic delivery conditions, in recording order. Read as a subset
#: of ``speaker_recording_spec.POSITIVE_SECTIONS`` (which also carries the two
#: far-field conditions); the compact plan keeps only ``positive_farfield`` from
#: the far-field pair and drops ``positive_farfield_loud`` to stay in budget.
COMPACT_DELIVERY_DIRECTORIES: tuple[str, ...] = (
    "positive_normal",
    "positive_slow",
    "positive_fast",
    "positive_quiet",
    "positive_loud",
)

#: Target lengths for the two continuous sections, in seconds. These are the
#: "aim for about this long" targets shown to the speaker, not hard gates: the
#: quality check in ``core`` reports a take that falls well short so nobody
#: discovers it after the speaker has gone, but never deletes one.
FREESPEECH_TARGET_SECONDS = 150  # ~2.5 min, inside the 2-3 min ask
BACKGROUND_TARGET_SECONDS = 90  # ~1.5 min, inside the 1-2 min ask


# ── section / group model (plain data) ───────────────────────────────────────


@dataclass(frozen=True)
class ConditionGroup:
    """A run of takes of one condition or one phrase, inside a section.

    ``stem_prefix`` is the filename stem without the take number: ``core``
    appends ``_NNN`` to it. It is built from the same tokens the validator's
    regexes are built from -- ``speaker_recording_spec.WAKE_PHRASE_SLUG`` for a
    positive condition, a near-phrase item's ``.slug``, or a continuous
    section's ``prefix`` -- so a produced filename matches by construction.
    """

    directory: str
    stem_prefix: str
    reps: int
    prompt: str
    instruction: str
    kind: str
    target_seconds: float | None = None


@dataclass(frozen=True)
class SectionPlan:
    """One independently-resumable section of the sitting."""

    section_id: str
    title: str
    instruction: str
    groups: tuple[ConditionGroup, ...] = field(default_factory=tuple)


_POSITIVE_SECTIONS_BY_DIR = {s.directory: s for s in spec.POSITIVE_SECTIONS}
_FREEFORM_SECTIONS_BY_DIR = {s.directory: s for s in spec.FREEFORM_SECTIONS}


def noise_sources_for(speaker: str) -> tuple[str, ...]:
    """The two real-noise sources ``speaker`` records, or a refusal.

    The compact plan is defined for the two humans in
    ``COMPACT_NOISE_ASSIGNMENTS``; any other label has no noise assignment here
    and is refused rather than silently given an empty noise section.
    """
    try:
        return COMPACT_NOISE_ASSIGNMENTS[speaker]
    except KeyError:
        raise ValueError(
            f"{speaker!r} has no compact-plan noise assignment "
            f"({sorted(COMPACT_NOISE_ASSIGNMENTS)}); the compact recording "
            "assistant is set up for those speakers"
        ) from None


def _delivery_section() -> SectionPlan:
    groups = []
    for directory in COMPACT_DELIVERY_DIRECTORIES:
        section = _POSITIVE_SECTIONS_BY_DIR[directory]
        groups.append(
            ConditionGroup(
                directory=section.directory,
                stem_prefix=f"{spec.WAKE_PHRASE_SLUG}_{section.condition}",
                reps=COMPACT_DELIVERY_REPS,
                prompt=spec.WAKE_PHRASE,
                instruction=section.instruction,
                kind=KIND_POSITIVE,
            )
        )
    return SectionPlan(
        "section1_delivery",
        'Section 1 - "Hey Youtab", close, delivery variations',
        'Say "hey youtab" once per take, close to the microphone. The wording '
        "on screen changes how to say it; the phrase itself never changes.",
        tuple(groups),
    )


def _position_noise_section(speaker: str) -> SectionPlan:
    farfield = _POSITIVE_SECTIONS_BY_DIR["positive_farfield"]
    groups = [
        ConditionGroup(
            directory=farfield.directory,
            stem_prefix=f"{spec.WAKE_PHRASE_SLUG}_{farfield.condition}",
            reps=COMPACT_FARFIELD_REPS,
            prompt=spec.WAKE_PHRASE,
            instruction=farfield.instruction,
            kind=KIND_POSITIVE,
        )
    ]
    for source in noise_sources_for(speaker):
        section = spec.noise_section(source)
        groups.append(
            ConditionGroup(
                directory=section.directory,
                stem_prefix=f"{spec.WAKE_PHRASE_SLUG}_{section.condition}",
                reps=COMPACT_NOISE_REPS,
                prompt=spec.WAKE_PHRASE,
                instruction=section.instruction,
                kind=KIND_POSITIVE,
            )
        )
    return SectionPlan(
        "section2_position_noise",
        "Section 2 - Position and real background noise",
        "Far-field first, then each noise source with it genuinely running.",
        tuple(groups),
    )


def _near_phrase_section() -> SectionPlan:
    groups = tuple(
        ConditionGroup(
            directory="near_phrase",
            stem_prefix=item.slug,
            reps=COMPACT_NEAR_PHRASE_REPS,
            prompt=item.text,
            instruction=item.note,
            kind=KIND_NEAR_PHRASE,
        )
        for item in spec.NEAR_PHRASE_ITEMS
    )
    return SectionPlan(
        "section3_near_phrase",
        "Section 3 - Near-phrase battery",
        "One phrase at a time, said naturally as one continuous phrase with no "
        "pause inside a name.",
        groups,
    )


def _freeform_section(section_id: str, title: str, directory: str, target: float) -> SectionPlan:
    section = _FREEFORM_SECTIONS_BY_DIR[directory]
    group = ConditionGroup(
        directory=section.directory,
        stem_prefix=section.prefix,
        reps=COMPACT_FREEFORM_FILES,
        prompt=section.instruction,
        instruction=section.instruction,
        kind=KIND_FREEFORM,
        target_seconds=target,
    )
    return SectionPlan(section_id, title, section.instruction, (group,))


def build_sections(speaker: str) -> tuple[SectionPlan, ...]:
    """The five compact sections for ``speaker``, in recording order."""
    return (
        _delivery_section(),
        _position_noise_section(speaker),
        _near_phrase_section(),
        _freeform_section(
            "section4_free_speech",
            "Section 4 - Free speech (no wake phrase)",
            "negative_freespeech",
            FREESPEECH_TARGET_SECONDS,
        ),
        _freeform_section(
            "section5_background",
            "Section 5 - Background only (no speech)",
            "background_only",
            BACKGROUND_TARGET_SECONDS,
        ),
    )
