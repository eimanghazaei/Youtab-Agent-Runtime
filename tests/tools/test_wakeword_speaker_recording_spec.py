"""``speaker_recording_spec.py`` must not contradict ``phrases.py``.

Background
----------
Speaker E002's package fragmented "hey you tab" -- the wake word's own
split-carrier spelling -- into two words with a pause between them, which is
a real defect: recording *the wake word itself* as if it were a negative,
or recording its measured top confusion ("hey you tap") as if it were a
positive, would train the opposite of what each phrase is. Round 8's package
exists partly to fix that, so this file is the test that a classification in
``speaker_recording_spec.NEAR_PHRASE_ITEMS`` cannot silently invert: every
item not marked ``"NEW"`` is checked against the real
``phrases.POSITIVE_SPELLINGS`` / ``phrases.HARD_NEGATIVES`` /
``phrases.CONFUSABLE_NEGATIVES`` tuples, not against a second, hand-copied
list that could drift from them.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import phrases  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402


def test_every_near_phrase_item_has_a_valid_classification() -> None:
    for item in spec.NEAR_PHRASE_ITEMS:
        assert item.classification in {"POSITIVE_SPELLINGS", "HARD_NEGATIVES", "NEW"}, (
            f"{item.text!r} has an unrecognized classification {item.classification!r}"
        )


def test_positive_spelling_items_are_actually_positive_spellings_in_phrases_py() -> None:
    """A phrase claimed to be the wake word must really be one there."""
    real_positives = {text for text, _weight in phrases.POSITIVE_SPELLINGS}
    claimed = [item for item in spec.NEAR_PHRASE_ITEMS if item.classification == "POSITIVE_SPELLINGS"]
    assert claimed, "no item in the near-phrase battery is classified POSITIVE_SPELLINGS"
    for item in claimed:
        assert item.text in real_positives, (
            f"{item.text!r} is classified POSITIVE_SPELLINGS in the recording spec but is "
            f"not in phrases.POSITIVE_SPELLINGS -- recording it would collect a positive "
            f"sample under a phrase phrases.py does not recognize as the wake word"
        )


def test_hard_negative_items_are_actually_hard_negatives_in_phrases_py() -> None:
    """A phrase claimed to be a near miss must really be one there, unless it is new."""
    real_hard_negatives = set(phrases.HARD_NEGATIVES)
    claimed = [item for item in spec.NEAR_PHRASE_ITEMS if item.classification == "HARD_NEGATIVES"]
    assert claimed, "no item in the near-phrase battery is classified HARD_NEGATIVES"
    for item in claimed:
        assert item.text in real_hard_negatives, (
            f"{item.text!r} is classified HARD_NEGATIVES in the recording spec but is not "
            f"in phrases.HARD_NEGATIVES -- the two lists have drifted apart"
        )


def test_confusable_items_are_actually_confusable_negatives_in_phrases_py() -> None:
    real_confusable = set(phrases.CONFUSABLE_NEGATIVES)
    claimed = [item for item in spec.NEAR_PHRASE_ITEMS if item.confusable]
    assert claimed, "no item in the near-phrase battery is flagged confusable"
    for item in claimed:
        assert item.classification == "HARD_NEGATIVES", (
            f"{item.text!r} is flagged confusable but is not classified HARD_NEGATIVES -- "
            "phrases.py only weights confusable phrases within the negative class"
        )
        assert item.text in real_confusable, (
            f"{item.text!r} is flagged confusable in the recording spec but is not in "
            f"phrases.CONFUSABLE_NEGATIVES"
        )


def test_new_items_carry_the_two_measured_e002_gaps() -> None:
    """The two phrases this round adds are exactly the ones phrases.py lacks."""
    new_texts = {item.text for item in spec.NEAR_PHRASE_ITEMS if item.classification == "NEW"}
    assert new_texts == {"okay youtab.", "hey google."}, (
        f"expected exactly the two measured gaps to be marked NEW, got {new_texts!r}"
    )
    already_present = {text for text, _weight in phrases.POSITIVE_SPELLINGS} | set(
        phrases.HARD_NEGATIVES
    )
    for text in new_texts:
        assert text not in already_present, (
            f"{text!r} is marked NEW but is already in phrases.py -- phrases.py has been "
            "extended and this spec's classification is now stale"
        )


def test_hey_you_tab_is_positive_and_hey_you_tap_is_negative() -> None:
    """The exact classification the brief for this round calls out by name."""
    by_text = {item.text: item for item in spec.NEAR_PHRASE_ITEMS}
    assert by_text["hey you tab."].classification == "POSITIVE_SPELLINGS", (
        '"hey you tab." is the wake word\'s split-carrier spelling, not a near miss'
    )
    assert by_text["hey you tap."].classification == "HARD_NEGATIVES", (
        '"hey you tap." is the measured top confusion, not a spelling of the wake word'
    )


def test_the_minimal_pair_is_recorded_back_to_back() -> None:
    """"hey you tab." and "hey you tap." must be adjacent, tab immediately before tap."""
    texts = [item.text for item in spec.NEAR_PHRASE_ITEMS]
    tab_index = texts.index("hey you tab.")
    tap_index = texts.index("hey you tap.")
    assert tap_index == tab_index + 1, (
        '"hey you tap." must immediately follow "hey you tab." so a speaker records the '
        f"minimal pair back to back; found them at positions {tab_index} and {tap_index}"
    )


def test_bare_hey_is_included_as_its_own_item() -> None:
    texts = {item.text for item in spec.NEAR_PHRASE_ITEMS}
    assert "hey." in texts, "bare \"hey\" must be its own recorded item"


def test_no_pause_inside_the_name_is_called_out_for_both_split_positives() -> None:
    for text in ("hey you tab.", "hey yoo tab."):
        item = next(i for i in spec.NEAR_PHRASE_ITEMS if i.text == text)
        assert "pause" in item.note.lower(), (
            f"{text!r} is a split spelling of the wake word and must warn against a pause "
            "inside the name in its recording note"
        )


def test_near_phrase_slugs_are_unique() -> None:
    slugs = [item.slug for item in spec.NEAR_PHRASE_ITEMS]
    assert len(slugs) == len(set(slugs)), f"duplicate slugs would collide on disk: {slugs}"


def test_every_slug_is_produced_by_slugify_of_its_own_text() -> None:
    for item in spec.NEAR_PHRASE_ITEMS:
        assert item.slug == spec.slugify(item.text)


def test_reps_and_thresholds_are_positive() -> None:
    for value in (
        spec.NEAR_PHRASE_REPS,
        spec.POSITIVE_REPS,
        spec.FARFIELD_REPS,
        spec.NOISE_REPS,
        spec.MIN_NOISE_SOURCES,
        spec.FREESPEECH_MIN_FILES,
        spec.BACKGROUND_MIN_FILES,
    ):
        assert isinstance(value, int) and value > 0


def test_noise_source_vocabulary_matches_the_brief() -> None:
    assert spec.NOISE_SOURCE_VOCAB == {"tv", "kitchen", "street", "fan"}


def test_metadata_field_lists_do_not_overlap() -> None:
    required = set(spec.REQUIRED_METADATA_FIELDS)
    optional = set(spec.OPTIONAL_METADATA_FIELDS)
    assert not (required & optional), "a field cannot be both required and optional"
    assert set(spec.ALL_METADATA_FIELDS) == required | optional
