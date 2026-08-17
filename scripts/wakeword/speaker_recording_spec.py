"""Machine-readable specification for the real-human speaker recording round.

``SPEAKER_RECORDING_PACKAGE.md`` tells a speaker what to record and exactly
what to name each file; ``validate_speaker_submission.py`` checks that a
submitted folder actually matches; ``CONSENT_RECORD_TEMPLATE.md`` tells the
speaker what happens to it afterwards. All three are read off this module
rather than each keeping its own copy of the phrase list, the folder names,
the take counts, the speaker assignments and the metadata fields, for the same
reason ``phrases.py`` gives for being the one place the training and
evaluation phrase inventories live: two copies of a label map drift, and a
drifted copy fails silently rather than loudly.

Nothing here is trained on. It is the checklist a real recording is held to
before it is ever considered for ``build_dataset.py``'s inputs.

The phrase taxonomy
-------------------
``phrases.py`` is the product's wake-phrase contract, and this module does not
get a second opinion about it. Every phrase in the near-phrase battery below
carries a ``contract`` field naming the ``phrases.py`` tuple that decides its
label, and ``tests/tools/test_wakeword_speaker_recording_spec.py`` checks the
claim against the real tuple. Every row is decided by the contract today.
"okay youtab.", "hey google." and "hey siri." were once in neither tuple and
carried their label here, beside the contract rather than in it; an Owner
decision has since put all three into ``phrases.HARD_NEGATIVES``, so they are
read off the contract like every other negative.

``CONTRACT_ABSENT`` and ``TAXONOMY_DECISIONS`` stay as the route for the next
phrase that arrives undecided — empty now, and required to be — because a
phrase may not be absent from ``phrases.py`` and label-free: defaulting is how
"hey you tab." — the wake word's own split-carrier spelling — was once recorded
as if it were a near miss.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# ── labels, and where a label is allowed to come from ────────────────────────

#: What the detector must do with an utterance. Not a dataset split and not a
#: folder name: the two things a recording of this phrase is evidence *for*.
POSITIVE = "positive"
NEGATIVE = "negative"
LABELS: tuple[str, ...] = (POSITIVE, NEGATIVE)

#: The ``phrases.py`` tuple a label is read off.
CONTRACT_POSITIVE = "POSITIVE_SPELLINGS"
CONTRACT_NEGATIVE = "HARD_NEGATIVES"
#: In neither tuple. Requires a ``TAXONOMY_DECISIONS`` entry, never a default.
CONTRACT_ABSENT = "not in phrases.py"
CONTRACTS: tuple[str, ...] = (CONTRACT_POSITIVE, CONTRACT_NEGATIVE, CONTRACT_ABSENT)

#: Whether a real recording of this phrase exists from E002, the one human
#: speaker recorded before this round.
#:
#: ``"no"``           the round-6/7 record states it was never recorded.
#: ``"fragmented"``   recorded, but every take was unusable for the stated
#:                    reason (a pause inside the name), so the phrase is
#:                    unmeasured in practice.
#: ``"undocumented"`` that session's record does not say, per phrase, whether
#:                    it was recorded. Treated here as unmeasured: this package
#:                    re-records every row from every speaker regardless, and
#:                    claiming coverage nobody wrote down would be a fabricated
#:                    provenance claim.
PRIOR_RECORDING_STATES: tuple[str, ...] = ("no", "fragmented", "undocumented")

# ── naming ───────────────────────────────────────────────────────────────────

#: Zero-padded take-counter width used in every filename, e.g. take 3 -> "003".
TAKE_DIGITS = 3

#: Extensions a phone or dedicated recorder can genuinely produce. Nothing is
#: ever transcoded into one of these — a recording keeps whatever container
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

#: The wake phrase itself, spelled as ``phrases.POSITIVE_SPELLINGS`` spells it.
WAKE_PHRASE = "hey youtab."

#: ...and as it appears in every positive-condition filename.
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
        raise ValueError(
            f"phrase {phrase!r} has characters slugify() cannot map cleanly to a filename"
        )
    return text.replace(" ", "-")


# ── the phrases phrases.py does not decide ───────────────────────────────────


@dataclass(frozen=True)
class TaxonomyDecision:
    """A label for a phrase the wake-phrase contract does not contain.

    ``phrases.py`` decides every row in the battery today, so this is empty.
    It is not retired: the moment a round wants a phrase that is in neither
    ``POSITIVE_SPELLINGS`` nor ``HARD_NEGATIVES``, somebody has to decide, and
    the decision has to be written down where the next round can read it — an
    unrecorded decision gets re-litigated, or worse, defaulted.

    ``basis`` is what the decision was read off — a tuple in ``phrases.py``,
    an existing entry in it, or how ``tools/wake_word.py`` actually keys
    detection. ``consequence`` is what follows for this package.

    An entry here is a stopgap and not a resting place. "okay youtab.", "hey
    google." and "hey siri." sat here for one round: each label was read off
    how the contract is constructed, and once the Owner made that reading
    binding the three moved into ``phrases.HARD_NEGATIVES`` and their entries
    left. ``test_wakeword_speaker_recording_spec.py`` requires this tuple and
    the set of ``CONTRACT_ABSENT`` rows to be equal in both directions, so an
    entry cannot outlive the row it decides, and a row cannot outlive its
    entry.
    """

    text: str
    label: str
    basis: str
    consequence: str


#: Empty, because every battery row's label is now read off ``phrases.py``.
#: The dataclass above says what goes here and when.
TAXONOMY_DECISIONS: tuple[TaxonomyDecision, ...] = ()

# ── the near-phrase battery ──────────────────────────────────────────────────


@dataclass(frozen=True)
class NearPhraseItem:
    """One row of the near-phrase battery a speaker records.

    ``label`` is what the detector must do; ``contract`` is the ``phrases.py``
    tuple that decides it, or ``CONTRACT_ABSENT`` for a phrase neither tuple
    contains, which then needs a ``TAXONOMY_DECISIONS`` entry to carry its
    label. Recording "hey you tab." as a negative or "hey you tap." as a
    positive would collect evidence for the opposite of what each phrase is;
    that is the mistake this dataclass exists to make impossible to state by
    accident, and
    ``tests/tools/test_wakeword_speaker_recording_spec.py`` cross-checks every
    label against the real tuple it claims.

    ``justification`` is why the row is in the list at all — kept per row
    rather than in one paragraph, because a row whose reason nobody can state
    is the row that gets trimmed when a session runs long. ``note`` is the
    speaker-facing instruction, and is empty where there is nothing to say
    beyond "say it normally".
    """

    text: str
    label: str
    contract: str
    confusable: bool
    takes: int
    recorded_from_e002: str
    justification: str
    note: str = ""

    @property
    def slug(self) -> str:
        return slugify(self.text)


#: Repetitions of a near-phrase item. Derivation in
#: SPEAKER_RECORDING_PACKAGE.md's "why these counts" section: a true
#: rule-of-three certification of the 2% near-miss target needs ~150 takes of
#: a single phrase, which no real speaker should be asked to say that many
#: times identically. Three is instead the floor that survives one integrity
#: exclusion and still leaves two genuine takes, and that can tell a fluke
#: from a pattern.
NEAR_PHRASE_REPS = 3

#: The three rows of the split-name minimal triple get five each instead. They
#: are the rows with *no* usable prior evidence at all — every earlier take of
#: them was fragmented by a pause inside the name — so they start from zero
#: rather than from three, and five leaves a clear majority reading after two
#: exclusions rather than a coin flip.
MINIMAL_PAIR_REPS = 5

#: The battery, in recording order. "hey you tab." and "hey you tap." are
#: placed back to back on purpose: they are a minimal pair (one is the wake
#: word, split; the other is its measured top confusion), and
#: SPEAKER_RECORDING_PACKAGE.md asks that they be recorded on the same device
#: immediately after one another. "okay youtab." and "okay tab." are adjacent
#: for the same reason — together they separate the carrier from the name.
NEAR_PHRASE_ITEMS: tuple[NearPhraseItem, ...] = (
    NearPhraseItem(
        "hey you tab.",
        POSITIVE,
        CONTRACT_POSITIVE,
        False,
        MINIMAL_PAIR_REPS,
        "fragmented",
        "This IS the wake word — the unstressed-carrier spelling, weighted 2 "
        "in phrases.POSITIVE_SPELLINGS, and half of the hardest minimal pair "
        "in the inventory. It is also the one phrase an earlier speaker lost: "
        "every take was spoken with a pause between \"you\" and \"tab\".",
        'Say it as one continuous name: no pause between "you" and "tab". A '
        "pause here is the exact defect that made this phrase nearly "
        "untestable from an earlier speaker.",
    ),
    NearPhraseItem(
        "hey you tap.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        MINIMAL_PAIR_REPS,
        "undocumented",
        "The measured top confusion for the phrase above — one voicing "
        "feature away, and it fired on the first trained model's held-out "
        "clips more than any other near miss. With the row above fragmented, "
        "the hardest confusion in the whole inventory is currently untested "
        "in both directions.",
        'Record it immediately after "hey you tab." on the same device, '
        "without changing anything in between, so the two form a clean "
        "minimal pair.",
    ),
    NearPhraseItem(
        "hey yoo tab.",
        POSITIVE,
        CONTRACT_POSITIVE,
        False,
        MINIMAL_PAIR_REPS,
        "fragmented",
        "Also the wake word — the hard-/t/ spelling, weighted 2 in "
        "phrases.POSITIVE_SPELLINGS. espeak-ng flaps the /t/ in the compound "
        "spelling and keeps it hard when the name is split; both are things "
        "people say, and this is the one that tests the split form.",
        "Same rule: one continuous name, no pause inside it.",
    ),
    NearPhraseItem(
        "okay youtab.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        False,
        NEAR_PHRASE_REPS,
        "no",
        "The only near phrase that carries the real name under a different "
        "carrier word, so it is the row that distinguishes a model keyed on "
        'the whole phrase from one keyed on "youtab" alone. People say '
        '"okay X" out of habit from other assistants, so a model that fires '
        "here fires often in the field. Never recorded before this round; "
        "phrases.HARD_NEGATIVES decides that it is a negative.",
    ),
    NearPhraseItem(
        "okay tab.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        False,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The other carrier with the bare noun, and the control for the row "
        'above: "okay youtab." minus the name. Recorded next to it so a fire '
        "can be attributed to the carrier or to the name rather than to the "
        "pair of them.",
    ),
    NearPhraseItem(
        "hey google.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        False,
        NEAR_PHRASE_REPS,
        "no",
        "A competing wake word with the same carrier and a stressed vowel in "
        "the next syllable. Anyone with another assistant in the house says "
        "this near the microphone all day. Never recorded before this round; "
        "phrases.HARD_NEGATIVES decides that it is a negative.",
    ),
    NearPhraseItem(
        "hey siri.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        False,
        NEAR_PHRASE_REPS,
        "no",
        "Same class as \"hey google.\", second carrier-plus-name shape, "
        "different stressed vowel. Never recorded before this round; "
        "phrases.HARD_NEGATIVES decides that it is a negative.",
    ),
    NearPhraseItem(
        "hey.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        False,
        NEAR_PHRASE_REPS,
        "no",
        "The carrier word alone, and the single most common thing an "
        "always-on microphone hears that begins like the wake word. It is in "
        "phrases.HARD_NEGATIVES already, so its label is not a judgement "
        "call — synthesized thousands of times, and never once recorded from "
        "a person.",
        "Say it alone, as if starting to say something and not finishing.",
    ),
    NearPhraseItem(
        "hey your tab.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured as one of the top confusions of the first trained model: the "
        'frame "hey <something> tab", which is what actually fires.',
    ),
    NearPhraseItem(
        "hey new tab.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The same measured frame with an ordinary word in it — and something "
        "a browser user says out loud.",
    ),
    NearPhraseItem(
        "hey utah.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Two syllables, same stress pattern, no /b/. Measured in the "
        "confusable set.",
    ),
    NearPhraseItem(
        "hey do tab.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The same frame with nonsense filler, which separates the frame "
        "itself from the words that happen to fill it.",
    ),
    NearPhraseItem(
        "hey stab.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Rhymes with the final syllable and has no /juː/ at all.",
    ),
    NearPhraseItem(
        "youtab.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The name with no carrier. Must not fire, or the product wakes every "
        "time someone says its name.",
    ),
    NearPhraseItem(
        "hey there.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The carrier plus the commonest thing that follows it in real speech.",
    ),
    NearPhraseItem(
        "youtab is running.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The name inside an ordinary sentence — what a user of this product "
        "says while talking *about* it.",
    ),
    NearPhraseItem(
        "hey you had.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Same onset and stress, different coda. Measured in the confusable "
        "set.",
    ),
    NearPhraseItem(
        "hey cab.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The final syllable of the name with a different onset before it.",
    ),
    NearPhraseItem(
        "hey you talk.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Same onset, different coda, and one of the measured confusions.",
    ),
    NearPhraseItem(
        "a new tab.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The frame with no carrier at all, and ordinary speech about tabs, "
        "which is what an agent's user actually talks about.",
    ),
    NearPhraseItem(
        "hey youtube.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        False,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The obvious confusion, and the one this phrase inventory was "
        "originally designed around. Measured as *not* the worst one — it "
        "fired on 1 held-out clip in 106 — which is exactly why it is worth "
        "recording: it is the row that shows the ranking was measured rather "
        "than guessed.",
    ),
    NearPhraseItem(
        "hey you.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        False,
        NEAR_PHRASE_REPS,
        "undocumented",
        "The prefix, stopped before the name. Bounds how much of the phrase "
        "the model needs before it commits.",
    ),
    NearPhraseItem(
        "eight.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured, not guessed: /eɪ/ with no /h/ onset scored 0.9985 against "
        "an operating threshold of 0.9991 and pinned that threshold by "
        "itself, which is what cost half the wake words.",
    ),
    NearPhraseItem(
        "two.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Second-highest scorer in the same measured tail (0.9832).",
    ),
    NearPhraseItem(
        "visual.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured in the same tail (0.8568); the /ʒu/ is the closest thing to "
        "/juː/ in that word list.",
    ),
    NearPhraseItem(
        "four.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured in the same tail (0.6853).",
    ),
    NearPhraseItem(
        "zero.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured in the same tail (0.5508).",
    ),
    NearPhraseItem(
        "house.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured in the same tail (0.5412).",
    ),
    NearPhraseItem(
        "down.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Round-3 evidence: 0.999997 on held-out recorded speech, the highest "
        "scoring negative of that round.",
    ),
    NearPhraseItem(
        "happy.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured: an /h/ onset that is not \"hey\" took over the tail once "
        "the model learned to require the onset (0.999995).",
    ),
    NearPhraseItem(
        "stop.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured alongside \"happy\" in the round-3 tail (0.992592).",
    ),
    NearPhraseItem(
        "yes.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured in the round-3 tail (0.987031).",
    ),
    NearPhraseItem(
        "left.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured in the round-3 tail (0.966623).",
    ),
    NearPhraseItem(
        "no.",
        NEGATIVE,
        CONTRACT_NEGATIVE,
        True,
        NEAR_PHRASE_REPS,
        "undocumented",
        "Measured in the round-3 tail (0.935737).",
    ),
)

# ── the positive conditions ──────────────────────────────────────────────────


@dataclass(frozen=True)
class PositiveSection:
    """One folder of wake-phrase takes recorded under a single condition.

    ``directory`` is the folder name inside ``originals/``; ``condition`` is
    the token that appears in the filename between the phrase slug and the
    take number. They differ only where a condition name has two words
    (``positive_farfield_loud`` / ``hey-youtab_farfield-loud_001.m4a``), which
    follows the convention the noise folders already use.
    """

    directory: str
    condition: str
    takes: int
    instruction: str


#: Repetitions per positive condition. Varied delivery, far-field and real
#: noise are the three conditions a follow-up experiment measured broken
#: (36.7%, 53.3% and 40.0% firing against 15/15 close-mic), so they carry more
#: redundancy than a near-phrase item does: five lets two exclusions happen
#: and still leaves a clear majority reading.
POSITIVE_REPS = 5

#: Delivery conditions, close to the microphone.
POSITIVE_CONDITIONS: tuple[str, ...] = ("normal", "slow", "fast", "quiet", "loud")

FARFIELD_REPS = 5

POSITIVE_SECTIONS: tuple[PositiveSection, ...] = (
    PositiveSection(
        "positive_normal",
        "normal",
        POSITIVE_REPS,
        "Your ordinary speaking voice and pace.",
    ),
    PositiveSection(
        "positive_slow",
        "slow",
        POSITIVE_REPS,
        "Noticeably slower than normal, but still one natural phrase.",
    ),
    PositiveSection(
        "positive_fast",
        "fast",
        POSITIVE_REPS,
        "Noticeably faster, as if in a hurry.",
    ),
    PositiveSection(
        "positive_quiet",
        "quiet",
        POSITIVE_REPS,
        "Genuinely quiet — as if not to wake someone; not a whisper if that "
        "feels unnatural to you.",
    ),
    PositiveSection(
        "positive_loud",
        "loud",
        POSITIVE_REPS,
        "Genuinely loud — as if calling across a room, not shouted so hard "
        "it distorts.",
    ),
    PositiveSection(
        "positive_farfield",
        "farfield",
        FARFIELD_REPS,
        "At least 5 metres away (an adjoining room with the door open works), "
        "in your normal voice and pace.",
    ),
    PositiveSection(
        "positive_farfield_loud",
        "farfield-loud",
        FARFIELD_REPS,
        "From the same distance, raised as you naturally would to be heard "
        "across that space. A raised voice changes vowel quality and not only "
        "level, so it is recorded separately — otherwise a miss cannot be "
        "attributed to the distance or to the delivery.",
    ),
)

#: A speaker records at least two of these four, each genuinely running.
NOISE_SOURCE_VOCAB = frozenset({"tv", "kitchen", "street", "fan"})
NOISE_REPS = 5
MIN_NOISE_SOURCES = 2


def noise_section(source: str) -> PositiveSection:
    """The positive section for one background-noise source."""
    if source not in NOISE_SOURCE_VOCAB:
        raise ValueError(f"{source!r} is not one of {sorted(NOISE_SOURCE_VOCAB)}")
    return PositiveSection(
        f"positive_noise_{source}",
        f"noise-{source}",
        NOISE_REPS,
        f"With the {source} genuinely running, close to the microphone as in "
        "the normal condition.",
    )


# ── the continuous sections ──────────────────────────────────────────────────


@dataclass(frozen=True)
class FreeformSection:
    """A continuous, undirected take rather than a series of repetitions.

    What is required is a file count floor, not a rep count:
    ``SPEAKER_RECORDING_PACKAGE.md`` asks for a target duration that a script
    cannot verify without opening the audio payload, which
    ``validate_speaker_submission.py`` deliberately does not do.
    """

    directory: str
    prefix: str
    min_files: int
    target_minutes: int
    instruction: str


FREESPEECH_MIN_FILES = 1
BACKGROUND_MIN_FILES = 1

FREEFORM_SECTIONS: tuple[FreeformSection, ...] = (
    FreeformSection(
        "negative_freespeech",
        "freespeech",
        FREESPEECH_MIN_FILES,
        5,
        "Talk naturally about anything, with no wake phrase and no phrase "
        "from the battery in it.",
    ),
    FreeformSection(
        "background_only",
        "background",
        BACKGROUND_MIN_FILES,
        3,
        "Your room with nobody speaking.",
    ),
)

# ── the submission layout ────────────────────────────────────────────────────

#: One speaker's folder holds exactly these four entries and nothing else.
#: ``originals/`` is the recording tree; the other three are the records that
#: make it usable. An unrecognised fifth entry is an error, not something to
#: skip: ingestion refuses to guess a label, so it also refuses to guess what
#: a stray file was for.
ORIGINALS_DIR = "originals"
CONSENT_FILE = "CONSENT.pdf"
METADATA_FILE = "RECORDING_METADATA.json"
CHECKSUM_FILE = "SHA256SUMS"
SUBMISSION_ENTRIES: tuple[str, ...] = (
    ORIGINALS_DIR,
    CONSENT_FILE,
    METADATA_FILE,
    CHECKSUM_FILE,
)

#: ``SHA256SUMS`` is coreutils format — ``<64 hex><two spaces><path>`` — the
#: same format ``freeze_manifest.py`` writes and ``sha256sum -c`` reads, with
#: paths relative to the speaker folder. Structure only: this validator does
#: not recompute a digest, because a digest recomputed on the machine that
#: wrote it proves nothing about the transfer that has not happened yet. The
#: coordinator verifies it with ``sha256sum -c`` after the transfer.
CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  (\S.*)$")

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

#: The one identifier that travels with the audio. No name, no initials, no
#: email, in any filename, folder name or metadata field. Kept as
#: ``speaker_id`` rather than something the commit gate does not recognise: a
#: filled ``RECORDING_METADATA.json`` is a speaker-keyed record, and
#: ``tests/tools/test_wakeword_no_human_data_committed.py`` should fail on it
#: if one is ever committed.
SPEAKER_ID_PATTERN = re.compile(r"^E\d{3}$")

# ── who records for what ─────────────────────────────────────────────────────

#: The three ways a recording can be consumed. Identical strings to
#: ``freeze_manifest.USAGES``, so ``--usage`` for a speaker's manifest is read
#: off this table rather than typed from memory — and ``assert_usable_for``
#: then raises rather than trusting the caller.
ROLE_TRAINING = "training"
ROLE_VALIDATION = "validation"
ROLE_SEALED = "sealed-evaluation"
ROLES: tuple[str, ...] = (ROLE_TRAINING, ROLE_VALIDATION, ROLE_SEALED)


@dataclass(frozen=True)
class SpeakerAssignment:
    """One speaker label, and the one thing their recordings may be used for.

    Fixed before any recording begins and immutable afterwards. Both
    directions of a late change are unrecoverable: promoting a sealed speaker
    into training spends the only measurement nobody has tuned against, and
    demoting a training speaker into the sealed set retro-fits a measurement
    to a model that has already seen the voice. So this table is pinned by
    ``test_wakeword_speaker_recording_spec.py`` — changing it means editing a
    test, which is a review decision rather than an edit.

    The speaker is not told their role by ``SPEAKER_RECORDING_PACKAGE.md``:
    the recording instructions are identical for every role, and knowing you
    are the final exam changes how you speak. What the speaker is told, in
    full and in writing, is on their consent form, which states the use
    category the coordinator ticked for them.
    """

    label: str
    role: str
    why: str


SPEAKER_ASSIGNMENTS: tuple[SpeakerAssignment, ...] = (
    SpeakerAssignment(
        "E003",
        ROLE_TRAINING,
        "Training. Real positives the model can learn from, which it has "
        "never had at scale.",
    ),
    SpeakerAssignment(
        "E004",
        ROLE_TRAINING,
        "Training. A second training voice, so the model is not fitting one "
        "real person's vowel space.",
    ),
    SpeakerAssignment(
        "E005",
        ROLE_VALIDATION,
        "Validation only: threshold, candidate and epoch selection. Kept off "
        "training so those three choices are made against a voice the weights "
        "have not seen, and kept off final qualification so the choices do "
        "not tune the measurement that certifies them.",
    ),
    SpeakerAssignment(
        "E006",
        ROLE_SEALED,
        "Sealed final qualification. Never shown to training or to selection; "
        "read once, when the qualification measurement is taken.",
    ),
    SpeakerAssignment(
        "E007",
        ROLE_SEALED,
        "Sealed final qualification. Two sealed voices rather than one, so the "
        "pooled sealed set clears the rule-of-three floors for both shipping "
        "targets on its own.",
    ),
)


def labels_for_role(role: str) -> tuple[str, ...]:
    """The speaker labels assigned to ``role``, in table order."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, not {role!r}")
    return tuple(a.label for a in SPEAKER_ASSIGNMENTS if a.role == role)


# ── how much a speaker records, and why that is enough ───────────────────────

#: The shipping targets this round is sized against, from README.md's target
#: table. ``activation on recorded human speech <= 0.2 per hour`` is the third
#: target and is not sized here: a rule-of-three certification of it needs
#: ~15 hours of continuous talking, which is ``evaluate_model.py``'s job at
#: the pooled scale, not one volunteer's sitting.
FALSE_REJECT_TARGET = 0.05
NEAR_MISS_FALSE_ACCEPT_TARGET = 0.02


def rule_of_three(target_rate: float) -> int:
    """Trials a clean run needs to bound ``target_rate`` at ~95% confidence.

    Observing zero failures in *n* trials puts the 95% upper bound on the true
    rate at about 3/*n*. Inverted: certifying a target rate needs about
    3/target trials, all of them clean.
    """
    if not 0.0 < target_rate < 1.0:
        raise ValueError(f"a target rate must be in (0, 1), not {target_rate!r}")
    return math.ceil(3.0 / target_rate)


def positives_per_speaker() -> int:
    """Genuine wake-phrase utterances one speaker produces."""
    conditions = sum(section.takes for section in POSITIVE_SECTIONS)
    noise = MIN_NOISE_SOURCES * NOISE_REPS
    battery = sum(item.takes for item in NEAR_PHRASE_ITEMS if item.label == POSITIVE)
    return conditions + noise + battery


def near_phrase_negatives_per_speaker() -> int:
    """Near-phrase utterances one speaker produces that must not fire."""
    return sum(item.takes for item in NEAR_PHRASE_ITEMS if item.label == NEGATIVE)


def audio_files_per_speaker() -> int:
    """Every audio file a complete submission contains."""
    discrete = positives_per_speaker() + near_phrase_negatives_per_speaker()
    continuous = sum(section.min_files for section in FREEFORM_SECTIONS)
    return discrete + continuous


# ── the session ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimeBudgetRow:
    """One line of the session's time estimate."""

    activity: str
    detail: str
    minutes: int


#: The estimate printed in SPEAKER_RECORDING_PACKAGE.md, which is checked
#: against this table row for row. Per-take rates are the ones the earlier
#: 65-minute estimate used — about 0.2 min for a near-phrase take, more for
#: a condition that needs setting up — so the total moves only when the take
#: counts move, which is the point of keeping it here.
TIME_BUDGET: tuple[TimeBudgetRow, ...] = (
    TimeBudgetRow("Read-through and setup", "", 5),
    TimeBudgetRow("Consent form", "", 5),
    TimeBudgetRow("Device and environment form", "", 5),
    TimeBudgetRow("1. Wake phrase, five deliveries", "25 takes", 7),
    TimeBudgetRow("2a. Far-field, normal voice", "5 takes", 4),
    TimeBudgetRow("2b. Far-field, raised voice", "5 takes", 2),
    TimeBudgetRow("3. Real background noise", "10 takes", 6),
    TimeBudgetRow("4. Near-phrase battery", "108 takes", 22),
    TimeBudgetRow("5. Free-speech negative", "1 continuous take, ~5 min", 6),
    TimeBudgetRow("6. Background-only", "1 continuous take, ~3 min", 4),
    TimeBudgetRow("Self-check and handoff prep", "", 5),
)

SESSION_MINUTES = sum(row.minutes for row in TIME_BUDGET)

# ── automatic filenames ───────────────────────────────────────────────────────
#
# A speaker never has to type a slug like ``hey-your-tab_003.m4a`` by hand and
# risk getting it wrong: the position of a file inside its section, in the
# order it was recorded, already determines its canonical name, because every
# section's name and order is fixed by this module.
# ``validate_speaker_submission.py``'s ``--rename`` mode turns this into a
# tool that sorts a folder's files by modification time and assigns the name
# at that position -- never by reading the audio, and never by asking anyone
# to spell a slug.


def expected_stems_for_positive_section(section: PositiveSection) -> tuple[str, ...]:
    """Canonical filename stems (no extension), in take order, for one condition folder."""
    return tuple(
        f"{WAKE_PHRASE_SLUG}_{section.condition}_{take:0{TAKE_DIGITS}d}"
        for take in range(1, section.takes + 1)
    )


def expected_stems_for_near_phrase() -> tuple[str, ...]:
    """Canonical stems for every take of the near-phrase battery, as one flat series.

    In ``NEAR_PHRASE_ITEMS`` order -- the same order
    ``SPEAKER_RECORDING_PACKAGE.md`` asks a speaker to record in -- so a
    ``near_phrase/`` folder recorded straight through, in order, can be
    auto-named from recording order alone.
    """
    stems: list[str] = []
    for item in NEAR_PHRASE_ITEMS:
        stems.extend(
            f"{item.slug}_{take:0{TAKE_DIGITS}d}" for take in range(1, item.takes + 1)
        )
    return tuple(stems)


def expected_stems_for_freeform(section: FreeformSection, count: int) -> tuple[str, ...]:
    """Canonical stems for a continuous section, sized to the files actually present.

    A freeform section asks for a minimum file count, not an exact one -- a
    recorder app may split one long recording into several files -- so this is
    sized by ``count`` rather than by ``section.min_files``.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    return tuple(f"{section.prefix}_{take:0{TAKE_DIGITS}d}" for take in range(1, count + 1))


# ── minimum vs expected time ─────────────────────────────────────────────────
#
# Every ``TIME_BUDGET`` row whose activity is *not* numbered ("Read-through and
# setup", "Consent form", "Device and environment form", "Self-check and
# handoff prep") is paperwork around the session, not a take that has to be
# performed. The numbered rows ("1." through "6.") are the recording itself:
# every required take, once, back to back. That is the floor a rushed session
# cannot go below without skipping something required -- as distinct from
# ``SESSION_MINUTES``, the realistic total including the paperwork around it.

MINIMUM_RECORDING_MINUTES = sum(
    row.minutes for row in TIME_BUDGET if row.activity[:1].isdigit()
)
