"""``speaker_recording_spec.py`` must not contradict ``phrases.py``.

Background
----------
``phrases.py`` is the product's wake-phrase contract. The recording spec
restates part of it -- which phrases a real speaker is asked for, and whether
each one must wake the assistant -- and a restatement that drifts is worse than
no restatement at all: it collects evidence for the opposite of what a phrase
is. That is not hypothetical. "hey you tab." is the wake word's own
split-carrier spelling, in ``phrases.POSITIVE_SPELLINGS`` with weight 2, and it
has already been treated once as if it were a near miss.

So every claim the spec makes about a label is checked here against the real
tuple it names, and any phrase ``phrases.py`` does not contain is required to
carry a recorded decision -- a label, the basis it was read off, and its
consequence -- rather than a default.

There are no such phrases today. "okay youtab.", "hey google." and "hey siri."
carried a decision beside the contract for one round; an Owner decision has
since put all three into ``phrases.HARD_NEGATIVES``, so their labels are read
off the contract like every other negative. The guard is therefore run against
constructed batteries as well as the real one, so that "nothing is absent"
stays a finding rather than the reason nothing is checked.

The rest of the file pins the things a hurried edit would move quietly: the
speaker assignments (which cannot change after recording without either
spending the sealed set or retro-fitting a measurement), the take counts the
rule-of-three arithmetic depends on, and the time budget the package prints.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import phrases  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402


def _positive_spellings() -> set[str]:
    return {text for text, _weight in phrases.POSITIVE_SPELLINGS}


def _every_phrase_in_the_contract() -> set[str]:
    return (
        _positive_spellings()
        | set(phrases.HARD_NEGATIVES)
        | set(phrases.SOFT_NEGATIVES)
        | set(phrases.COMMON_PHRASES)
        | set(phrases.CONFUSABLE_NEGATIVES)
    )


# ── labels against the contract ──────────────────────────────────────────────


def test_every_near_phrase_item_has_a_valid_label_and_contract() -> None:
    assert spec.NEAR_PHRASE_ITEMS, "the near-phrase battery is empty"
    for item in spec.NEAR_PHRASE_ITEMS:
        assert item.label in spec.LABELS, f"{item.text!r} has an unknown label {item.label!r}"
        assert item.contract in spec.CONTRACTS, (
            f"{item.text!r} names an unknown contract source {item.contract!r}"
        )
        assert item.recorded_from_e002 in spec.PRIOR_RECORDING_STATES, (
            f"{item.text!r} claims prior-recording state {item.recorded_from_e002!r}, "
            f"which is not one of {spec.PRIOR_RECORDING_STATES}"
        )
        assert item.takes > 0
        assert item.justification.strip(), (
            f"{item.text!r} has no justification -- a row whose reason nobody can "
            "state is the row that gets trimmed when a session runs long"
        )


def test_the_wake_phrase_itself_is_a_positive_spelling_in_phrases_py() -> None:
    """Sections 1 to 3 record this phrase 45 times; it had better be the wake word."""
    assert spec.WAKE_PHRASE in _positive_spellings()
    assert spec.slugify(spec.WAKE_PHRASE) == spec.WAKE_PHRASE_SLUG


def test_positive_items_are_actually_positive_spellings_in_phrases_py() -> None:
    """A phrase claimed to be the wake word must really be one there."""
    real_positives = _positive_spellings()
    claimed = [item for item in spec.NEAR_PHRASE_ITEMS if item.label == spec.POSITIVE]
    assert claimed, "no item in the near-phrase battery is labelled positive"
    for item in claimed:
        assert item.contract == spec.CONTRACT_POSITIVE, (
            f"{item.text!r} is labelled positive but claims {item.contract!r} decided it"
        )
        assert item.text in real_positives, (
            f"{item.text!r} is labelled positive in the recording spec but is not in "
            "phrases.POSITIVE_SPELLINGS -- recording it would collect a positive "
            "sample under a phrase phrases.py does not recognise as the wake word"
        )


def test_contract_negative_items_are_actually_hard_negatives_in_phrases_py() -> None:
    """A phrase whose label the contract decides must be in the tuple it names."""
    real_hard_negatives = set(phrases.HARD_NEGATIVES)
    claimed = [
        item for item in spec.NEAR_PHRASE_ITEMS if item.contract == spec.CONTRACT_NEGATIVE
    ]
    assert claimed, "no item in the battery is decided by phrases.HARD_NEGATIVES"
    for item in claimed:
        assert item.label == spec.NEGATIVE
        assert item.text in real_hard_negatives, (
            f"{item.text!r} claims phrases.HARD_NEGATIVES decides it but is not in that "
            "tuple -- the two lists have drifted apart"
        )


def test_no_item_is_labelled_against_the_tuple_it_appears_in() -> None:
    """The mislabelling check in both directions, over the whole battery.

    A phrase in ``POSITIVE_SPELLINGS`` labelled negative would teach the model
    to suppress a real fire; a phrase in ``HARD_NEGATIVES`` labelled positive
    would teach it to fire on a near miss.
    """
    real_positives = _positive_spellings()
    real_hard_negatives = set(phrases.HARD_NEGATIVES)
    for item in spec.NEAR_PHRASE_ITEMS:
        if item.text in real_positives:
            assert item.label == spec.POSITIVE, (
                f"{item.text!r} is in phrases.POSITIVE_SPELLINGS but this spec labels it "
                f"{item.label!r}"
            )
        if item.text in real_hard_negatives:
            assert item.label == spec.NEGATIVE, (
                f"{item.text!r} is in phrases.HARD_NEGATIVES but this spec labels it "
                f"{item.label!r}"
            )


def test_confusable_items_are_actually_confusable_negatives_in_phrases_py() -> None:
    real_confusable = set(phrases.CONFUSABLE_NEGATIVES)
    claimed = [item for item in spec.NEAR_PHRASE_ITEMS if item.confusable]
    assert claimed, "no item in the near-phrase battery is flagged confusable"
    for item in claimed:
        assert item.contract == spec.CONTRACT_NEGATIVE, (
            f"{item.text!r} is flagged confusable but is not decided by "
            "phrases.HARD_NEGATIVES -- phrases.py only weights confusable phrases "
            "within the negative class"
        )
        assert item.text in real_confusable, (
            f"{item.text!r} is flagged confusable in the recording spec but is not in "
            "phrases.CONFUSABLE_NEGATIVES"
        )


# ── the phrases phrases.py does not decide ───────────────────────────────────

#: A phrase in no tuple in ``phrases.py``, for constructing a battery the guard
#: below has to reject. Deliberately not a plausible candidate for the
#: inventory: it is fixture text, not a suggestion.
NOVEL_PHRASE = "hey placeholder."


def _absent_rows_carry_a_recorded_decision(
    items: Sequence[spec.NearPhraseItem],
    decisions: Sequence[spec.TaxonomyDecision],
) -> int:
    """The ``CONTRACT_ABSENT`` invariant, over any battery and any decision list.

    Raises ``AssertionError`` the way a test body would, and returns how many
    rows were marked absent so a caller can assert on that too. Written as a
    function rather than inline because the real battery has nothing absent in
    it any more: the same guard has to be shown to bite on a constructed
    battery, or "no row is absent" would be indistinguishable from "no row is
    checked".

    Both directions. A row marked absent must really be absent from every tuple
    in ``phrases.py`` and must have a decision whose label agrees with its own;
    a decision must belong to a row that is still absent. A decision left
    behind after its phrase entered the contract is two records disagreeing
    about where the label came from.
    """
    contract = _every_phrase_in_the_contract()
    by_text = {decision.text: decision for decision in decisions}
    absent = [item for item in items if item.contract == spec.CONTRACT_ABSENT]

    for item in absent:
        assert item.text not in contract, (
            f"{item.text!r} is marked as absent from phrases.py but is in it now -- "
            "phrases.py has been extended and this spec's basis is stale"
        )
        assert item.text in by_text, (
            f"{item.text!r} is in neither phrases.py tuple and has no entry in "
            "TAXONOMY_DECISIONS, so its label is a default rather than a decision"
        )
        assert by_text[item.text].label == item.label, (
            f"{item.text!r} is labelled {item.label!r} in the battery and "
            f"{by_text[item.text].label!r} in TAXONOMY_DECISIONS"
        )

    assert set(by_text) == {item.text for item in absent}, (
        "every decision must belong to a battery row the contract does not decide, and "
        "every such row must have a decision; "
        f"decided={sorted(by_text)} absent={sorted(item.text for item in absent)}"
    )

    for decision in decisions:
        assert decision.label == spec.NEGATIVE, (
            f"{decision.text!r} is recorded as {decision.label!r}. A phrase the "
            "contract does not contain cannot be decided positive: a positive is a "
            "spelling POSITIVE_SPELLINGS trained the model on, and nothing else"
        )
        assert len(decision.basis) > 80, (
            f"{decision.text!r} has no real basis recorded, so the decision cannot be "
            "re-checked against the contract later"
        )
        assert len(decision.consequence) > 40, f"{decision.text!r} records no consequence"
        assert "phrases" in decision.basis or "wake_word" in decision.basis, (
            f"{decision.text!r}'s basis cites neither phrases.py nor the runtime"
        )

    return len(absent)


def test_no_battery_row_needs_a_decision_beside_the_contract() -> None:
    """No phrase gets a label by default, and none needs a decision today.

    ``phrases.py`` decides every row. That is a stronger state than the one
    ``TAXONOMY_DECISIONS`` was built for, not a reason to stop checking: the
    empty count is asserted, and the reason for it -- every row naming a real
    tuple -- is asserted alongside, so an emptiness caused by a row losing its
    ``contract`` field cannot pass as the same thing.
    """
    absent = _absent_rows_carry_a_recorded_decision(
        spec.NEAR_PHRASE_ITEMS, spec.TAXONOMY_DECISIONS
    )
    assert absent == 0, f"{absent} battery rows are marked as absent from phrases.py"
    assert spec.TAXONOMY_DECISIONS == (), (
        f"a decision is recorded beside the contract: {spec.TAXONOMY_DECISIONS!r}"
    )

    for item in spec.NEAR_PHRASE_ITEMS:
        assert item.contract in (spec.CONTRACT_POSITIVE, spec.CONTRACT_NEGATIVE), (
            f"{item.text!r} names {item.contract!r} rather than a tuple in phrases.py"
        )


def test_the_absent_row_guard_still_bites_with_nothing_left_to_guard() -> None:
    """Non-vacuity for the check above, on constructed batteries.

    ``CONTRACT_ABSENT`` is unused, not retired, so each way of getting it wrong
    still has to fail. Every case below is one that happened or nearly
    happened: a row nobody decided, a row whose basis went stale when the
    contract grew, and a decision left behind after its phrase moved into
    ``HARD_NEGATIVES``.
    """
    assert NOVEL_PHRASE not in _every_phrase_in_the_contract(), (
        f"{NOVEL_PHRASE!r} is in phrases.py now; this test needs a phrase that is not"
    )

    # The shape the mechanism exists for: outside the contract, and decided.
    assert (
        _absent_rows_carry_a_recorded_decision(
            (_constructed_row(NOVEL_PHRASE, spec.CONTRACT_ABSENT),),
            (_constructed_decision(NOVEL_PHRASE),),
        )
        == 1
    )

    # Outside the contract and undecided -- the default this guard forbids.
    with pytest.raises(AssertionError):
        _absent_rows_carry_a_recorded_decision(
            (_constructed_row(NOVEL_PHRASE, spec.CONTRACT_ABSENT),), ()
        )

    # Marked absent, but phrases.py decides it now. This is exactly what the
    # Owner decision did to "okay youtab.", and it must not pass unnoticed.
    with pytest.raises(AssertionError):
        _absent_rows_carry_a_recorded_decision(
            (_constructed_row("okay youtab.", spec.CONTRACT_ABSENT),),
            (_constructed_decision("okay youtab."),),
        )

    # A decision whose row is decided by the contract: two records disagreeing.
    with pytest.raises(AssertionError):
        _absent_rows_carry_a_recorded_decision(
            spec.NEAR_PHRASE_ITEMS, (_constructed_decision(NOVEL_PHRASE),)
        )

    # A phrase outside the contract decided *positive*, which would collect
    # positives under a spelling nothing was ever trained on.
    with pytest.raises(AssertionError):
        _absent_rows_carry_a_recorded_decision(
            (_constructed_row(NOVEL_PHRASE, spec.CONTRACT_ABSENT, spec.POSITIVE),),
            (_constructed_decision(NOVEL_PHRASE, spec.POSITIVE),),
        )


def _constructed_row(
    text: str, contract: str, label: str = spec.NEGATIVE
) -> spec.NearPhraseItem:
    return spec.NearPhraseItem(
        text,
        label,
        contract,
        False,
        spec.NEAR_PHRASE_REPS,
        "no",
        "constructed by test_wakeword_speaker_recording_spec.py; never recorded",
    )


def _constructed_decision(text: str, label: str = spec.NEGATIVE) -> spec.TaxonomyDecision:
    return spec.TaxonomyDecision(
        text,
        label,
        "constructed by test_wakeword_speaker_recording_spec.py: long enough to clear "
        "the basis floor, and it cites phrases.HARD_NEGATIVES so the citation check "
        "has something to find",
        "constructed by test_wakeword_speaker_recording_spec.py; no consequence in fact",
    )


def test_the_owner_decided_phrases_are_hard_negatives_and_never_positives() -> None:
    """The Owner decision, asserted one phrase at a time.

    These three were labelled negative beside the contract for one round and
    are in ``phrases.HARD_NEGATIVES`` now. Both halves are pinned: a future
    edit that promotes one to a positive spelling would teach the detector to
    fire on the product's name under the wrong carrier, or on another
    assistant's wake word, and it would go unnoticed in a diff of a 100-entry
    tuple.
    """
    hard_negatives = set(phrases.HARD_NEGATIVES)
    positives = _positive_spellings()
    decided = {decision.text for decision in spec.TAXONOMY_DECISIONS}
    by_text = {item.text: item for item in spec.NEAR_PHRASE_ITEMS}

    for text in ("okay youtab.", "hey google.", "hey siri."):
        assert text in hard_negatives, (
            f"{text!r} is not in phrases.HARD_NEGATIVES; the Owner decision that put it "
            "in the governed taxonomy has been reverted"
        )
        assert text not in positives, (
            f"{text!r} is in phrases.POSITIVE_SPELLINGS, which would train the detector "
            "to fire on it -- it is a phrase the product must never wake on"
        )
        item = by_text[text]
        assert item.label == spec.NEGATIVE
        assert item.contract == spec.CONTRACT_NEGATIVE, (
            f"{text!r} is in HARD_NEGATIVES but its battery row still claims "
            f"{item.contract!r} decides it"
        )
        assert text not in decided, (
            f"{text!r} is decided by the contract and also carries a TAXONOMY_DECISIONS "
            "entry; the entry is a stale second record of the same label"
        )


def test_no_hard_negative_contains_a_spelling_of_the_wake_phrase() -> None:
    """``HARD_NEGATIVES``'s own documented invariant, made executable.

    ``phrases.py`` states it in prose: nothing in that tuple *contains* the
    wake phrase, because labelling an utterance that includes "hey youtab" as a
    negative teaches the model to suppress a real fire. The name on its own is
    not the wake phrase -- the tuple has carried "youtab.", "open youtab." and
    now "okay youtab." -- so what this checks is the carrier *and* the name.
    """
    spellings = {_words(text) for text in _positive_spellings()}
    assert spellings, "no positive spellings to check against"

    for negative in phrases.HARD_NEGATIVES:
        for spelling in spellings:
            assert spelling not in _words(negative), (
                f"{negative!r} is in phrases.HARD_NEGATIVES and contains the wake "
                f"phrase spelling {spelling!r}; training on it as a negative teaches "
                "the model to suppress a real fire"
            )

    # Non-vacuity: the same check on a phrase that does contain the wake word.
    assert any(spelling in _words("hey youtab, hold on.") for spelling in spellings)


def _words(phrase: str) -> str:
    """A phrase reduced to its spoken words, for substring comparison.

    Terminal "." or "!" and internal commas are punctuation a speaker does not
    say, so "hey, youtab." and "hey youtab!" both reduce to "hey youtab" and
    both are compared against.
    """
    text = phrase.strip()
    if text.endswith((".", "!")):
        text = text[:-1]
    return " ".join(text.replace(",", " ").lower().split())


def test_bare_hey_is_decided_by_the_contract_and_not_by_us() -> None:
    """Bare "hey" is in HARD_NEGATIVES already; only its recording was missing.

    It is easy to file it with the three decided phrases, because all four were
    absent from the *recorded* set. It is not absent from the contract, and
    saying so keeps a decision that was never needed from being invented.
    """
    item = next(item for item in spec.NEAR_PHRASE_ITEMS if item.text == "hey.")
    assert item.label == spec.NEGATIVE
    assert item.contract == spec.CONTRACT_NEGATIVE
    assert "hey." in phrases.HARD_NEGATIVES
    assert item.text not in {decision.text for decision in spec.TAXONOMY_DECISIONS}
    assert item.recorded_from_e002 == "no", (
        "bare \"hey\" was never recorded from a person; that is the gap this row closes"
    )


# ── the rows this round exists for ───────────────────────────────────────────


def test_hey_you_tab_is_positive_and_hey_you_tap_is_negative() -> None:
    """The mislabelling incident, asserted one phrase at a time."""
    by_text = {item.text: item for item in spec.NEAR_PHRASE_ITEMS}
    assert by_text["hey you tab."].label == spec.POSITIVE, (
        '"hey you tab." is the wake word\'s split-carrier spelling, not a near miss'
    )
    assert by_text["hey you tab."].contract == spec.CONTRACT_POSITIVE
    assert by_text["hey you tap."].label == spec.NEGATIVE, (
        '"hey you tap." is the measured top confusion, not a spelling of the wake word'
    )
    assert by_text["hey you tap."].contract == spec.CONTRACT_NEGATIVE


def test_the_minimal_pair_is_recorded_back_to_back() -> None:
    """"hey you tab." and "hey you tap." must be adjacent, tab immediately before tap."""
    texts = [item.text for item in spec.NEAR_PHRASE_ITEMS]
    tab_index = texts.index("hey you tab.")
    tap_index = texts.index("hey you tap.")
    assert tap_index == tab_index + 1, (
        '"hey you tap." must immediately follow "hey you tab." so a speaker records the '
        f"minimal pair back to back; found them at positions {tab_index} and {tap_index}"
    )


def test_the_carrier_control_follows_the_carrier_gap() -> None:
    """"okay tab." is what makes "okay youtab." attributable to the name."""
    texts = [item.text for item in spec.NEAR_PHRASE_ITEMS]
    assert texts.index("okay tab.") == texts.index("okay youtab.") + 1


def test_the_four_gaps_from_the_earlier_session_are_all_in_the_battery() -> None:
    by_text = {item.text: item for item in spec.NEAR_PHRASE_ITEMS}
    for text in ("okay youtab.", "hey google.", "hey siri.", "hey."):
        assert text in by_text, f"{text!r} is missing -- it was missing from E002 too"
        assert by_text[text].label == spec.NEGATIVE
        assert by_text[text].takes >= spec.NEAR_PHRASE_REPS
        assert by_text[text].recorded_from_e002 == "no"


def test_the_split_name_rows_are_the_only_fragmented_ones_and_get_more_takes() -> None:
    fragmented = [
        item for item in spec.NEAR_PHRASE_ITEMS if item.recorded_from_e002 == "fragmented"
    ]
    assert {item.text for item in fragmented} == {"hey you tab.", "hey yoo tab."}
    for item in fragmented:
        assert item.takes == spec.MINIMAL_PAIR_REPS, (
            f"{item.text!r} has no usable prior evidence at all, so it starts from "
            f"{spec.MINIMAL_PAIR_REPS} takes rather than {spec.NEAR_PHRASE_REPS}"
        )


def test_no_pause_inside_the_name_is_called_out_for_every_split_positive() -> None:
    for text in ("hey you tab.", "hey yoo tab."):
        item = next(item for item in spec.NEAR_PHRASE_ITEMS if item.text == text)
        assert "pause" in item.note.lower(), (
            f"{text!r} is a split spelling of the wake word and must warn against a pause "
            "inside the name in its recording note"
        )


def test_bare_hey_is_included_as_its_own_item() -> None:
    texts = {item.text for item in spec.NEAR_PHRASE_ITEMS}
    assert "hey." in texts, 'bare "hey" must be its own recorded item'


# ── naming ───────────────────────────────────────────────────────────────────


def test_near_phrase_slugs_are_unique() -> None:
    slugs = [item.slug for item in spec.NEAR_PHRASE_ITEMS]
    assert len(slugs) == len(set(slugs)), f"duplicate slugs would collide on disk: {slugs}"


def test_every_slug_is_produced_by_slugify_of_its_own_text() -> None:
    for item in spec.NEAR_PHRASE_ITEMS:
        assert item.slug == spec.slugify(item.text)


def test_slugify_refuses_a_phrase_it_cannot_map_cleanly() -> None:
    """A collision-prone slug is refused rather than produced.

    ``phrases.py`` contains spellings with internal punctuation
    (``"hey, youtab."``) which would slug to the same name as ``"hey
    youtab."``; anything of that shape has to raise, not silently collide.
    """
    with pytest.raises(ValueError):
        spec.slugify("hey, youtab.")


def test_positive_section_names_are_distinct_and_slug_shaped() -> None:
    directories = [section.directory for section in spec.POSITIVE_SECTIONS]
    conditions = [section.condition for section in spec.POSITIVE_SECTIONS]
    assert len(set(directories)) == len(directories)
    assert len(set(conditions)) == len(conditions)
    for section in spec.POSITIVE_SECTIONS:
        assert section.directory.startswith("positive_")
        assert section.takes > 0
        assert section.instruction.strip()
        assert not set(section.condition) - set("abcdefghijklmnopqrstuvwxyz-")


def test_noise_sections_are_built_only_from_the_declared_vocabulary() -> None:
    assert spec.NOISE_SOURCE_VOCAB == {"tv", "kitchen", "street", "fan"}
    for source in sorted(spec.NOISE_SOURCE_VOCAB):
        section = spec.noise_section(source)
        assert section.directory == f"positive_noise_{source}"
        assert section.condition == f"noise-{source}"
        assert section.takes == spec.NOISE_REPS
    with pytest.raises(ValueError):
        spec.noise_section("dishwasher")


def test_freeform_sections_ask_for_a_file_floor_not_a_duration() -> None:
    directories = {section.directory for section in spec.FREEFORM_SECTIONS}
    assert directories == {"negative_freespeech", "background_only"}
    for section in spec.FREEFORM_SECTIONS:
        assert section.min_files > 0
        assert section.target_minutes > 0
        assert section.instruction.strip()


def test_reps_and_thresholds_are_positive() -> None:
    for value in (
        spec.NEAR_PHRASE_REPS,
        spec.MINIMAL_PAIR_REPS,
        spec.POSITIVE_REPS,
        spec.FARFIELD_REPS,
        spec.NOISE_REPS,
        spec.MIN_NOISE_SOURCES,
        spec.FREESPEECH_MIN_FILES,
        spec.BACKGROUND_MIN_FILES,
        spec.TAKE_DIGITS,
    ):
        assert isinstance(value, int) and value > 0


# ── the submission layout ────────────────────────────────────────────────────


def test_the_submission_holds_exactly_three_named_entries() -> None:
    assert spec.SUBMISSION_ENTRIES == (
        "originals",
        "RECORDING_METADATA.json",
        "SHA256SUMS",
    )
    assert len(set(spec.SUBMISSION_ENTRIES)) == len(spec.SUBMISSION_ENTRIES)
    # The consent document is gone from the technical pipeline entirely: it is
    # not a submission entry, and CONSENT_FILE no longer exists on the spec.
    assert "CONSENT.pdf" not in spec.SUBMISSION_ENTRIES
    assert not hasattr(spec, "CONSENT_FILE")


def test_authorization_is_a_non_identifying_project_level_fact() -> None:
    # Authorization = membership of the trusted speaker registry, stated as a
    # non-identifying project-level fact: it affirms no person, no signature,
    # no date and no uploaded document.
    text = spec.PROJECT_RECORDING_AUTHORIZATION.lower()
    assert "authorization" in text
    assert "names no person" in text
    assert "no signature or date" in text
    assert "no uploaded document" in text
    # It must not embed an actual date (a consent date) or an identifier.
    import re as _re

    assert not _re.search(r"\d{4}-\d{2}-\d{2}", spec.PROJECT_RECORDING_AUTHORIZATION)
    assert not spec.SPEAKER_ID_PATTERN.search(spec.PROJECT_RECORDING_AUTHORIZATION)


def test_private_documents_are_named_but_never_required() -> None:
    # CONSENT.pdf is the Owner's private document: tolerated but outside the
    # pipeline. It must never be a submission entry the pipeline requires.
    assert "CONSENT.pdf" in spec.PRIVATE_DOC_NAMES
    assert not (set(spec.PRIVATE_DOC_NAMES) & set(spec.SUBMISSION_ENTRIES))


def test_the_checksum_line_pattern_reads_coreutils_output() -> None:
    digest = "0" * 64
    match = spec.CHECKSUM_LINE.match(f"{digest}  originals/near_phrase/hey_001.m4a")
    assert match and match.group(2) == "originals/near_phrase/hey_001.m4a"

    for bad in (
        f"{digest} originals/one-space.m4a",  # coreutils writes two spaces
        f"{'0' * 63}  originals/short-digest.m4a",
        f"{'A' * 64}  originals/upper-case.m4a",
        f"{digest}  ",
    ):
        assert not spec.CHECKSUM_LINE.match(bad), f"accepted a malformed line: {bad!r}"


def test_the_metadata_form_keeps_the_speaker_label_and_nothing_identifying() -> None:
    diagnostic = set(spec.DIAGNOSTIC_METADATA_FIELDS)
    optional = set(spec.OPTIONAL_METADATA_FIELDS)
    assert not (diagnostic & optional), "a field cannot be both diagnostic and optional"
    assert set(spec.ALL_METADATA_FIELDS) == diagnostic | optional
    assert "speaker_id" in diagnostic
    # The consent date is gone from the diagnostic metadata entirely: the form
    # is recording-environment diagnostics only, never paperwork.
    assert "consent_signed_date" not in spec.ALL_METADATA_FIELDS
    for forbidden in ("name", "email", "phone", "address", "age", "gender"):
        assert forbidden not in spec.ALL_METADATA_FIELDS, (
            f"the form asks for {forbidden!r}, which is identity data the recordings "
            "deliberately do not carry"
        )


def test_the_metadata_template_matches_the_required_fields() -> None:
    """The file a speaker copies has to ask for exactly what is validated."""
    template = json.loads(
        (WAKEWORD / "RECORDING_METADATA.template.json").read_text(encoding="utf-8")
    )
    assert tuple(template) == spec.ALL_METADATA_FIELDS
    for name, value in template.items():
        if isinstance(value, list):
            assert value and all(str(v).startswith("<") for v in value)
        else:
            assert value.startswith("<"), (
                f"{name!r} in the template is pre-filled with {value!r}; every field has "
                "to be answered by the speaker"
            )


def test_a_speaker_label_is_the_only_identifier_that_travels() -> None:
    assert spec.SPEAKER_ID_PATTERN.match("E003")
    for bad in ("e003", "E03", "E0034", "E003 ", "jane", "E003-take2"):
        assert not spec.SPEAKER_ID_PATTERN.match(bad), f"accepted {bad!r} as a label"


# ── who records for what ─────────────────────────────────────────────────────


def test_the_speaker_assignments_are_exactly_the_ones_agreed() -> None:
    """Pinned on purpose: changing this table has to be a review decision.

    The five recording-round speakers come first, in order: two training voices,
    one validation voice, two sealed. Promoting a sealed speaker into training
    spends the only measurement nobody has tuned against; demoting a training
    speaker into the sealed set retro-fits a measurement to a model that has
    already seen that voice. Neither is recoverable by re-recording, because the
    speaker would no longer be unseen.

    E001 and E002 -- the two-speaker compact-round humans -- are appended at the
    end so those five keep their positions. They record through
    ``recording_assistant.compact_plan`` rather than the five-speaker package, so
    they carry no coverage cell and exist here only to give ``import_speaker.py``
    a training-eligibility record. E002 is validation and consumed: reassigned
    from the round-6/7 sealed evaluation holdout, it is no longer eligible as a
    sealed final holdout (recorded in
    ``build_human_dataset.CONSUMED_FOR_VALIDATION``).
    """
    actual = tuple((a.label, a.role) for a in spec.SPEAKER_ASSIGNMENTS)
    assert actual == (
        ("E003", "training"),
        ("E004", "training"),
        ("E005", "validation"),
        ("E006", "sealed-evaluation"),
        ("E007", "sealed-evaluation"),
        ("E001", "training"),
        ("E002", "validation"),
    ), f"the speaker assignments changed: {actual!r}"

    for assignment in spec.SPEAKER_ASSIGNMENTS:
        assert spec.SPEAKER_ID_PATTERN.match(assignment.label)
        assert assignment.role in spec.ROLES
        assert assignment.why.strip(), f"{assignment.label} has no recorded reason"


def test_every_role_is_used_and_the_labels_are_unique() -> None:
    labels = [a.label for a in spec.SPEAKER_ASSIGNMENTS]
    assert len(set(labels)) == len(labels)
    for role in spec.ROLES:
        assert spec.labels_for_role(role), f"no speaker is assigned to {role!r}"
    assert spec.labels_for_role(spec.ROLE_SEALED) == ("E006", "E007")
    with pytest.raises(ValueError):
        spec.labels_for_role("training-ish")


def test_the_roles_are_the_usages_freeze_manifest_enforces() -> None:
    """A role that ``freeze_manifest.py`` does not know cannot be enforced."""
    import freeze_manifest

    assert set(spec.ROLES) == set(freeze_manifest.USAGES)


# ── coverage assignments (G3) ────────────────────────────────────────────────


def test_every_recording_round_speaker_has_a_fully_filled_coverage_cell() -> None:
    """Guidance, but complete guidance: no recording-round speaker defaults a
    dimension.

    An empty cell is exactly the gap this table exists to close -- an
    unassigned dimension is the one five independent recorders all fill the same
    way. Each cell names a device from the vocabulary, a far-field distance, at
    least ``MIN_NOISE_SOURCES`` of the four noise sources, a pitch band and a
    reason.

    Coverage is a five-speaker table. E001 and E002 -- the two compact-round
    humans -- are assigned a role but take their noise plan from
    ``recording_assistant.compact_plan``, not this table, so they deliberately
    carry no cell here. Every *other* assigned speaker, the five recording-round
    ones, must have exactly one fully filled cell.
    """
    compact_humans = {"E001", "E002"}
    assigned = {a.label for a in spec.SPEAKER_ASSIGNMENTS}
    covered = {c.label for c in spec.COVERAGE_ASSIGNMENTS}
    assert covered <= assigned, (
        f"coverage names {sorted(covered - assigned)}, which are not in "
        "SPEAKER_ASSIGNMENTS"
    )
    assert assigned - covered == compact_humans, (
        "coverage must cover every recording-round speaker and only them; "
        f"uncovered={sorted(assigned - covered)}"
    )
    assert len(covered) == len(spec.COVERAGE_ASSIGNMENTS), "a speaker has two coverage cells"

    for cell in spec.COVERAGE_ASSIGNMENTS:
        assert spec.SPEAKER_ID_PATTERN.match(cell.label)
        assert cell.device in spec.COVERAGE_DEVICE_VOCAB, (
            f"{cell.label} names device {cell.device!r}, not one of "
            f"{sorted(spec.COVERAGE_DEVICE_VOCAB)}"
        )
        assert cell.farfield_distance.strip(), f"{cell.label} has no far-field distance"
        assert cell.pitch_band.strip(), f"{cell.label} has no pitch band"
        assert cell.why.strip(), f"{cell.label} has no recorded reason"

        assert set(cell.noise_sources) <= spec.NOISE_SOURCE_VOCAB, (
            f"{cell.label} names a noise source outside {sorted(spec.NOISE_SOURCE_VOCAB)}"
        )
        assert len(set(cell.noise_sources)) >= spec.MIN_NOISE_SOURCES, (
            f"{cell.label} names fewer than {spec.MIN_NOISE_SOURCES} distinct noise sources"
        )


def test_the_coverage_assignments_actually_spread_the_dimensions() -> None:
    """Non-vacuity: the point of the table is diversity, not just completeness.

    A table that named "phone, living room, tv" for all five would pass the
    completeness check above while defeating the reason the table exists.
    """
    cells = spec.COVERAGE_ASSIGNMENTS

    # All three devices are used.
    assert {c.device for c in cells} == spec.COVERAGE_DEVICE_VOCAB

    # The noise pairs are distinct, and every one of the four sources is covered
    # by at least two speakers.
    pairs = [frozenset(c.noise_sources) for c in cells]
    assert len(set(pairs)) == len(pairs), f"two speakers share a noise pair: {pairs}"
    from collections import Counter

    counts = Counter(source for c in cells for source in set(c.noise_sources))
    assert set(counts) == spec.NOISE_SOURCE_VOCAB, (
        f"a noise source is never assigned: {sorted(spec.NOISE_SOURCE_VOCAB - set(counts))}"
    )
    assert min(counts.values()) >= 2, f"a noise source is covered only once: {counts}"

    # The pitch bands are distinct, so the five span the range rather than pile
    # up in the middle the synthetic pool already over-represents.
    bands = [c.pitch_band for c in cells]
    assert len(set(bands)) == len(bands), f"two speakers fill the same pitch band: {bands}"


def test_coverage_for_raises_for_an_unassigned_label() -> None:
    assert spec.coverage_for("E003").label == "E003"
    with pytest.raises(ValueError):
        spec.coverage_for("E999")


# ── the arithmetic the counts are sized by ───────────────────────────────────


def test_the_rule_of_three_is_arithmetic_and_not_a_stored_number() -> None:
    assert spec.rule_of_three(spec.FALSE_REJECT_TARGET) == 60
    assert spec.rule_of_three(spec.NEAR_MISS_FALSE_ACCEPT_TARGET) == 150
    assert spec.rule_of_three(0.5) == 6
    for bad in (0.0, 1.0, -0.1, 2.0):
        with pytest.raises(ValueError):
            spec.rule_of_three(bad)


def test_the_per_speaker_counts_are_what_the_package_prints() -> None:
    assert spec.positives_per_speaker() == 55
    assert spec.near_phrase_negatives_per_speaker() == 98
    assert spec.audio_files_per_speaker() == 155
    assert sum(item.takes for item in spec.NEAR_PHRASE_ITEMS) == 108


def test_the_sealed_speakers_alone_clear_both_rule_of_three_floors() -> None:
    """The reason two speakers are sealed rather than one.

    Certification is a claim about the sealed set: it is the only measurement
    nothing has been tuned against. If the pooled sealed set did not clear the
    floors, the qualification claim would need speakers whose audio had already
    been used for something else.
    """
    sealed = spec.labels_for_role(spec.ROLE_SEALED)
    assert len(sealed) == 2

    positives = len(sealed) * spec.positives_per_speaker()
    negatives = len(sealed) * spec.near_phrase_negatives_per_speaker()
    assert positives >= spec.rule_of_three(spec.FALSE_REJECT_TARGET), (
        f"{positives} sealed positives cannot bound a "
        f"{spec.FALSE_REJECT_TARGET:.0%} false-reject rate"
    )
    assert negatives >= spec.rule_of_three(spec.NEAR_MISS_FALSE_ACCEPT_TARGET), (
        f"{negatives} sealed near-phrase utterances cannot bound a "
        f"{spec.NEAR_MISS_FALSE_ACCEPT_TARGET:.0%} near-miss false-accept rate"
    )

    # And no single speaker can: the claim is made across speakers, which is
    # why one sealed voice would not have been enough.
    assert spec.positives_per_speaker() < spec.rule_of_three(spec.FALSE_REJECT_TARGET)


# ── the session ──────────────────────────────────────────────────────────────


def test_the_time_budget_adds_up_and_covers_every_section() -> None:
    assert spec.SESSION_MINUTES == sum(row.minutes for row in spec.TIME_BUDGET)
    assert spec.SESSION_MINUTES == 71
    for row in spec.TIME_BUDGET:
        assert row.activity.strip()
        assert row.minutes > 0

    activities = " | ".join(row.activity for row in spec.TIME_BUDGET)
    for section in ("1.", "2a.", "2b.", "3.", "4.", "5.", "6."):
        assert section in activities, f"the time budget has no row for section {section}"

    # The take counts quoted in the budget are the spec's own, not a stale copy.
    details = {row.activity: row.detail for row in spec.TIME_BUDGET}
    assert details["4. Near-phrase battery"] == (
        f"{sum(item.takes for item in spec.NEAR_PHRASE_ITEMS)} takes"
    )


def test_minimum_recording_minutes_is_the_numbered_rows_only() -> None:
    """The floor a rushed session cannot go below: every take, no paperwork."""
    numbered = sum(row.minutes for row in spec.TIME_BUDGET if row.activity[:1].isdigit())
    paperwork = sum(row.minutes for row in spec.TIME_BUDGET if not row.activity[:1].isdigit())

    assert spec.MINIMUM_RECORDING_MINUTES == numbered
    assert spec.MINIMUM_RECORDING_MINUTES == 51
    assert spec.MINIMUM_RECORDING_MINUTES + paperwork == spec.SESSION_MINUTES
    assert spec.MINIMUM_RECORDING_MINUTES < spec.SESSION_MINUTES


# ── automatic filenames ───────────────────────────────────────────────────────


def test_expected_stems_for_a_positive_section_are_slug_condition_and_take() -> None:
    section = spec.POSITIVE_SECTIONS[0]
    stems = spec.expected_stems_for_positive_section(section)
    assert len(stems) == section.takes
    assert stems[0] == f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_001"
    assert stems[-1] == f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_{section.takes:03d}"
    assert len(set(stems)) == len(stems)


def test_expected_stems_for_a_noise_section_use_the_noise_condition_token() -> None:
    section = spec.noise_section("tv")
    stems = spec.expected_stems_for_positive_section(section)
    assert stems == tuple(
        f"{spec.WAKE_PHRASE_SLUG}_noise-tv_{take:03d}" for take in range(1, section.takes + 1)
    )


def test_expected_stems_for_near_phrase_is_108_long_and_in_battery_order() -> None:
    stems = spec.expected_stems_for_near_phrase()
    assert len(stems) == sum(item.takes for item in spec.NEAR_PHRASE_ITEMS) == 108
    assert len(set(stems)) == len(stems), "two rows produced colliding stems"

    first_item = spec.NEAR_PHRASE_ITEMS[0]
    second_item = spec.NEAR_PHRASE_ITEMS[1]
    last_item = spec.NEAR_PHRASE_ITEMS[-1]
    assert stems[0] == f"{first_item.slug}_001"
    assert stems[first_item.takes - 1] == f"{first_item.slug}_{first_item.takes:03d}"
    assert stems[first_item.takes] == f"{second_item.slug}_001"
    assert stems[-1] == f"{last_item.slug}_{last_item.takes:03d}"


def test_expected_stems_for_freeform_is_sized_by_the_actual_file_count() -> None:
    section = spec.FREEFORM_SECTIONS[0]
    assert spec.expected_stems_for_freeform(section, 1) == (f"{section.prefix}_001",)
    assert spec.expected_stems_for_freeform(section, 3) == (
        f"{section.prefix}_001",
        f"{section.prefix}_002",
        f"{section.prefix}_003",
    )
    with pytest.raises(ValueError):
        spec.expected_stems_for_freeform(section, 0)
