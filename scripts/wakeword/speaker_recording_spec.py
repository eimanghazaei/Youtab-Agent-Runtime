"""Shared specification for the round-8 real-human wake-word recording package.

``SPEAKER_RECORDING_PACKAGE.md`` tells a speaker what to record and exactly
what to name each file; ``validate_speaker_submission.py`` checks that a
submitted folder actually matches. Both are read off this module rather than
each keeping its own copy of the phrase list, the folder names, the take
counts and the metadata fields, for the same reason ``phrases.py`` gives for
being the one place the training and evaluation phrase inventories live: two
copies of a label map drift, and a drifted copy fails silently rather than
loudly. The prose in the package and the regexes in the validator come from
this one table, so they cannot describe two different things.

Nothing here is trained on. It is the checklist a real recording is held to
before it is ever considered for ``build_dataset.py``'s inputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Zero-padded take-counter width used in every filename, e.g. take 3 -> "003".
TAKE_DIGITS = 3

#: Extensions a phone or dedicated recorder can genuinely produce. Nothing is
#: ever transcoded into one of these -- a recording keeps whatever container
#: its app wrote. This set exists only to catch a name with no audio
#: extension at all (a typo, a half-renamed file), not to enforce a format.
AUDIO_EXTENSIONS = frozenset(
    {
        ".wav",
        ".m4a",
        ".caf",
        ".aac",
        ".amr",
        ".3gp",
        ".3gpp",
        ".mp4",
        ".mp3",
        ".ogg",
        ".opus",
        ".flac",
        ".aiff",
        ".webm",
    }
)

#: The wake phrase itself, as it appears in every positive-condition filename.
WAKE_PHRASE_SLUG = "hey-youtab"


def slugify(phrase: str) -> str:
    """Turn a phrase into the folder-safe name used in its filename.

    Strips one trailing "." or "!", lowercases, and turns spaces into
    hyphens. Every phrase in ``NEAR_PHRASE_ITEMS`` is plain lowercase words
    and spaces once its terminal punctuation is gone, so this covers all of
    them; anything that would not round-trip cleanly raises rather than
    producing a slug two different phrases could collide on.
    ``test_wakeword_speaker_recording_spec.py`` checks every item's
    ``.slug`` was actually produced by this function, so the printed table in
    the package and the name a speaker types cannot quietly disagree.
    """
    text = phrase.strip()
    if text.endswith((".", "!")):
        text = text[:-1]
    text = text.strip().lower()
    if not re.fullmatch(r"[a-z0-9 ]+", text):
        raise ValueError(f"phrase {phrase!r} has characters slugify() cannot map cleanly to a filename")
    return text.replace(" ", "-")


@dataclass(frozen=True)
class NearPhraseItem:
    """One row of the near-phrase battery a speaker records.

    ``classification`` names the ``phrases.py`` tuple this phrase belongs to
    -- ``"POSITIVE_SPELLINGS"`` or ``"HARD_NEGATIVES"`` -- or ``"NEW"`` for
    the two phrases this round adds that are not in ``phrases.py`` yet.
    Recording "hey you tab" as a negative or "hey you tap" as a positive
    would train the opposite of what each phrase is; that is exactly the
    mistake this dataclass exists to make impossible to state by accident,
    and ``test_wakeword_speaker_recording_spec.py`` cross-checks every
    non-``"NEW"`` entry against the real ``phrases.py`` tuple it claims.
    """

    text: str
    classification: str
    confusable: bool
    note: str

    @property
    def slug(self) -> str:
        return slugify(self.text)


#: The near-phrase battery, in recording order. "hey you tab." and
#: "hey you tap." are placed back to back on purpose: they are a minimal
#: pair (one is the wake word, split; the other is its measured top
#: confusion), and SPEAKER_RECORDING_PACKAGE.md asks that they be recorded on
#: the same device immediately after one another.
NEAR_PHRASE_ITEMS: tuple[NearPhraseItem, ...] = (
    NearPhraseItem(
        "hey you tab.",
        "POSITIVE_SPELLINGS",
        False,
        'This IS the wake word -- its unstressed-carrier spelling, not a near '
        'miss. Say it as one continuous name: no pause between "you" and '
        '"tab". A pause here is the exact defect that made this phrase '
        "nearly untestable from an earlier speaker.",
    ),
    NearPhraseItem(
        "hey you tap.",
        "HARD_NEGATIVES",
        True,
        'The measured top confusion for the phrase above: one voicing '
        "feature away, and it fired on the model's held-out synthesized "
        "clips more than any other near miss. Record it immediately after "
        '"hey you tab." on the same device so the two form a clean minimal '
        "pair.",
    ),
    NearPhraseItem(
        "hey yoo tab.",
        "POSITIVE_SPELLINGS",
        False,
        "Also the wake word -- the hard-/t/ spelling. Same rule: one "
        "continuous name, no pause inside it.",
    ),
    NearPhraseItem(
        "okay youtab.",
        "NEW",
        False,
        "Not in scripts/wakeword/phrases.py yet. Carries the real name under "
        "a different carrier word than \"hey\" -- never recorded before, so "
        "this failure mode has never been measured.",
    ),
    NearPhraseItem(
        "hey google.",
        "NEW",
        False,
        "Not in scripts/wakeword/phrases.py yet. The carrier word \"hey\" "
        "with a different, well-known name.",
    ),
    NearPhraseItem(
        "hey.",
        "HARD_NEGATIVES",
        False,
        "The carrier word on its own. Already in phrases.py's synthesized "
        "set but never recorded as a real, standalone utterance before this "
        "round.",
    ),
    NearPhraseItem("hey your tab.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("hey new tab.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("hey utah.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("hey do tab.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("hey stab.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("youtab.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("hey there.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("youtab is running.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("hey you had.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("hey cab.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("hey you talk.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("a new tab.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("eight.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("two.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("visual.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("four.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("zero.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("house.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("down.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("happy.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("stop.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("yes.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("left.", "HARD_NEGATIVES", True, ""),
    NearPhraseItem("no.", "HARD_NEGATIVES", True, ""),
)

#: Repetitions of each near-phrase item. Derivation in
#: SPEAKER_RECORDING_PACKAGE.md's "why these counts" section: a true
#: rule-of-three certification of the 2% near-miss target needs ~150 takes of
#: a single phrase, which no real speaker should be asked to say that many
#: times identically. Three is instead the floor that survives one
#: integrity exclusion and still leaves two genuine takes, and that can tell
#: a fluke from a pattern.
NEAR_PHRASE_REPS = 3

#: Delivery conditions recorded of the plain wake phrase. These, far-field
#: and real noise are the three conditions an earlier follow-up experiment
#: measured broken (36.7%, 53.3%, 40.0% firing vs. 15/15 close-mic), so they
#: carry more redundancy than a near-phrase item does.
POSITIVE_CONDITIONS = ("normal", "slow", "fast", "quiet", "loud")
POSITIVE_REPS = 5

FARFIELD_CONDITION = "farfield"
FARFIELD_REPS = 5

#: A speaker records at least two of these four, each genuinely running.
NOISE_SOURCE_VOCAB = frozenset({"tv", "kitchen", "street", "fan"})
NOISE_REPS = 5
MIN_NOISE_SOURCES = 2

#: negative_freespeech and background_only are continuous, undirected takes,
#: not discrete repetitions, so what is required is a file count floor, not a
#: rep count -- SPEAKER_RECORDING_PACKAGE.md asks for target durations
#: (about 5 and 3 minutes) that a script cannot verify without opening the
#: audio payload, which validate_speaker_submission.py deliberately does not
#: do.
FREESPEECH_MIN_FILES = 1
BACKGROUND_MIN_FILES = 1

#: Device / environment metadata every submission carries alongside the
#: audio. Free-text strings on purpose: a non-technical speaker fills this in
#: by hand, so it asks for plain descriptions ("about 4 by 5 metres"), not a
#: fixed unit or enum that would need further parsing.
REQUIRED_METADATA_FIELDS: tuple[str, ...] = (
    "speaker_id",
    "recording_date",
    "device_make_model",
    "recording_app",
    "room_name",
    "room_size_approx",
    "floor_surface",
    "wall_surface",
    "background_sources_present",
    "noise_sources_used",
    "farfield_distance",
    "consent_signed_date",
)

#: May be present and blank, but is not required to be present at all.
OPTIONAL_METADATA_FIELDS: tuple[str, ...] = ("notes",)

ALL_METADATA_FIELDS: tuple[str, ...] = REQUIRED_METADATA_FIELDS + OPTIONAL_METADATA_FIELDS
