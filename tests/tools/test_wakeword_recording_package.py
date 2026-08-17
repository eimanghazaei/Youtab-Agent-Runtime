"""The recording package has to say what it says, and say the same thing twice.

Background
----------
Two documents and one module describe this round: the speaker-facing package,
the consent record, and ``speaker_recording_spec.py``. The module is the one
machine-readable copy, and ``test_wakeword_speaker_recording_spec.py`` checks it
against ``phrases.py``. This file checks the *documents* against the module, row
for row, and then checks the paragraphs that no data structure can hold.

E002 is why. Three things were missing from that session that no amount of
re-analysis can recover:

* ``okay youtab`` was never recorded. It is the only near phrase that carries
  the real keyword under a *different carrier word*, so it is the one that
  distinguishes a model keyed on the whole phrase from a model keyed on
  "youtab" — and "okay X" is what people say out of habit from other
  assistants.
* ``hey google``, ``hey siri`` and bare ``hey`` were never recorded. All three
  are things an always-on microphone hears constantly, and all three begin
  exactly like the wake word.
* ``hey you tab`` was spoken with a pause between "you" and "tab", in every
  take. That fragments the phrase into two words the detector was never trained
  on, and it left the hardest confusion in the whole inventory — ``hey you
  tab`` (a positive) against ``hey you tap`` (a negative, one voicing feature
  away) — essentially untested.

A document can drift back into that state silently, so the tables are
machine-checked rather than trusted, and the paragraphs a hurried edit trims
first — coverage across five dimensions, the retention policy, the rule about
which pause is allowed — are asserted by name.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
PACKAGE = WAKEWORD / "SPEAKER_RECORDING_PACKAGE.md"
CONSENT = WAKEWORD / "CONSENT_RECORD_TEMPLATE.md"

sys.path.insert(0, str(WAKEWORD))

import phrases  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402

_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Every markdown table in ``text``, as (header cells, body rows)."""
    lines = text.splitlines()
    tables: list[tuple[list[str], list[list[str]]]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            line.startswith("|")
            and index + 1 < len(lines)
            and _SEPARATOR.match(lines[index + 1])
        ):
            header = _cells(line)
            rows = []
            cursor = index + 2
            while cursor < len(lines) and lines[cursor].startswith("|"):
                rows.append(_cells(lines[cursor]))
                cursor += 1
            tables.append((header, rows))
            index = cursor
        else:
            index += 1
    return tables


def _table(text: str, *required_headers: str) -> list[list[str]]:
    """The one table whose header contains every name in ``required_headers``."""
    matches = [
        rows
        for header, rows in _tables(text)
        if all(name in header for name in required_headers)
    ]
    assert len(matches) == 1, (
        f"expected exactly one table with headers {required_headers}, found {len(matches)}"
    )
    return matches[0]


def _package() -> str:
    return PACKAGE.read_text(encoding="utf-8")


def _consent() -> str:
    return CONSENT.read_text(encoding="utf-8")


def _prose(text: str) -> str:
    """Lower-cased, whitespace-collapsed, with markdown blockquote markers gone.

    A substring check against the raw text would depend on where a paragraph
    happens to wrap and on whether the sentence sits inside a ``>`` block.
    """
    return re.sub(r"\s+", " ", re.sub(r"(?m)^\s*>\s?", "", text).lower())


def _unbacktick(cell: str) -> str:
    return cell.strip().strip("`").strip()


def _spoken(text: str) -> str:
    """A phrase as the tables print it: without its terminal punctuation."""
    return text[:-1] if text.endswith((".", "!")) else text


def _contract_phrases() -> set[str]:
    return (
        {text for text, _weight in phrases.POSITIVE_SPELLINGS}
        | set(phrases.HARD_NEGATIVES)
        | set(phrases.SOFT_NEGATIVES)
        | set(phrases.COMMON_PHRASES)
        | set(phrases.CONFUSABLE_NEGATIVES)
    )


# ── the battery, in both of its tables ───────────────────────────────────────


def test_the_speaker_facing_table_matches_the_spec_row_for_row() -> None:
    """What the speaker is asked to say, and how many times, is not a second copy."""
    rows = _table(_package(), "#", "Phrase", "Must it wake Youtab?", "Takes", "File slug")
    assert len(rows) == len(spec.NEAR_PHRASE_ITEMS), (
        f"{len(rows)} rows printed for {len(spec.NEAR_PHRASE_ITEMS)} battery items"
    )
    assert len(rows) >= 30, "the battery has shrunk to a token list"

    for number, (row, item) in enumerate(zip(rows, spec.NEAR_PHRASE_ITEMS), start=1):
        index, phrase, wake, takes, slug, note = row
        assert int(index) == number
        assert phrase.strip('"') == _spoken(item.text), (
            f"row {number} prints {phrase!r} for {item.text!r}"
        )
        expected = "**Wake word**" if item.label == spec.POSITIVE else "Should NOT wake it"
        assert wake == expected, f"row {number} ({item.text!r}) is printed as {wake!r}"
        assert int(takes) == item.takes
        assert _unbacktick(slug) == item.slug
        assert note == item.note, f"row {number} note has drifted from the spec"


def test_the_label_table_matches_the_spec_row_for_row() -> None:
    """Label, basis and justification, printed from the same table they are read from."""
    rows = _table(_package(), "label", "basis in `phrases.py`", "recorded from E002?")
    assert len(rows) == len(spec.NEAR_PHRASE_ITEMS)

    for number, (row, item) in enumerate(zip(rows, spec.NEAR_PHRASE_ITEMS), start=1):
        index, phrase, label, takes, basis, prior, why = row
        assert int(index) == number
        assert _unbacktick(phrase) == _spoken(item.text)
        assert label == item.label, f"row {number} ({item.text!r}) is printed as {label!r}"
        assert int(takes) == item.takes
        assert _unbacktick(basis) == item.contract
        assert prior == item.recorded_from_e002
        assert why == item.justification, (
            f"row {number}'s justification has drifted from the spec"
        )
        assert why.strip(), f"row {number} has no justification"


def test_every_printed_basis_agrees_with_phrases_py() -> None:
    """The drift check between this document and the contract.

    A row claiming ``HARD_NEGATIVES`` that is not in that tuple is a false
    statement about where its label came from; a row claiming to be absent from
    ``phrases.py`` when the phrase has since been added there hides the fact
    that a gap was closed. Neither is findable by reading.
    """
    rows = _table(_package(), "label", "basis in `phrases.py`", "recorded from E002?")
    contract = _contract_phrases()
    positives = {text for text, _weight in phrases.POSITIVE_SPELLINGS}

    printed: Counter[str] = Counter()
    for row, item in zip(rows, spec.NEAR_PHRASE_ITEMS):
        basis = _unbacktick(row[4])
        printed[basis] += 1
        if basis == spec.CONTRACT_ABSENT:
            assert item.text not in contract, (
                f"{item.text!r} is printed as absent from phrases.py but is in it now"
            )
        elif basis == spec.CONTRACT_POSITIVE:
            assert item.text in positives
        elif basis == spec.CONTRACT_NEGATIVE:
            assert item.text in set(phrases.HARD_NEGATIVES)
        else:
            raise AssertionError(f"{item.text!r} prints an unknown basis {basis!r}")

    # Non-vacuity: the column has to discriminate, and both of the branches it
    # can still take have to have been taken. A column printing one word for
    # every row would satisfy the loop above while checking nothing, and the
    # absent branch no longer stands in for that -- the contract decides every
    # row now.
    assert printed == Counter(item.contract for item in spec.NEAR_PHRASE_ITEMS), (
        f"the printed basis column has drifted from the spec: {printed}"
    )
    assert printed[spec.CONTRACT_POSITIVE] >= 2, (
        "no row is checked against POSITIVE_SPELLINGS, so this loop cannot catch the "
        "mislabelling it exists for"
    )
    assert printed[spec.CONTRACT_NEGATIVE] >= 30, (
        "almost no row is checked against HARD_NEGATIVES; the battery has shrunk or the "
        "column has stopped naming it"
    )
    assert printed[spec.CONTRACT_ABSENT] == 0, (
        f"{printed[spec.CONTRACT_ABSENT]} rows are printed as outside phrases.py; the "
        "contract decides every row now, so either phrases.py lost an entry or this "
        "table is stale"
    )


def test_the_gaps_from_the_earlier_session_are_all_in_the_list() -> None:
    """The whole reason this package exists, asserted one phrase at a time."""
    rows = _table(_package(), "label", "basis in `phrases.py`", "recorded from E002?")
    by_phrase = {_unbacktick(row[1]): row for row in rows}

    for phrase in ("okay youtab", "hey google", "hey siri", "hey"):
        assert phrase in by_phrase, f"{phrase!r} is missing — it was missing from E002 too"
        assert by_phrase[phrase][2] == "negative", f"{phrase!r} is not labelled a negative"
        assert int(by_phrase[phrase][3]) >= 3, f"{phrase!r} has too few takes to mean anything"
        assert by_phrase[phrase][5] == "no", (
            f"{phrase!r} is not marked as never recorded, which is the finding"
        )

    # `hey you tab` is a POSITIVE spelling, not a near phrase -- it is one of the
    # orthographies the model is trained to fire on. Getting this wrong in either
    # direction would invert the whole test.
    assert by_phrase["hey you tab"][2] == "positive"
    assert "hey you tab." in {text for text, _ in phrases.POSITIVE_SPELLINGS}
    # And its minimal pair has to be recorded too, or the confusion is still
    # untested: one voicing feature separates them.
    assert by_phrase["hey you tap"][2] == "negative"


def test_the_per_speaker_counts_still_clear_the_floors_the_arithmetic_needs() -> None:
    """36 positives and 40 near-phrase utterances per speaker were the floor.

    They came from what four speakers had to demonstrate between them. This
    package records more of both, and the floors are kept here so a future trim
    has something to fail against.
    """
    assert spec.positives_per_speaker() >= 36, "too few positives to bound 5%"
    assert spec.near_phrase_negatives_per_speaker() >= 40, "too few near phrases to bound 2%"


def test_the_three_phrases_that_were_missing_are_hard_negatives_now() -> None:
    """The Owner decision, checked against the contract and against the document.

    These three were in no tuple in ``phrases.py`` when this package was
    written, so they carried a label decided beside the contract. An Owner
    decision made that reading binding, and the package has to say so: a
    document still describing them as outside the contract is a second record
    disagreeing with the first.
    """
    hard_negatives = set(phrases.HARD_NEGATIVES)
    positives = {text for text, _weight in phrases.POSITIVE_SPELLINGS}
    collapsed = re.sub(r"\s+", " ", _package())

    for text in ("okay youtab.", "hey google.", "hey siri."):
        assert text in hard_negatives, f"{text!r} is no longer in phrases.HARD_NEGATIVES"
        assert text not in positives, (
            f"{text!r} is a positive spelling now, which would train the detector to "
            "fire on it"
        )

    assert "all three are in `phrases.HARD_NEGATIVES`" in collapsed, (
        "the package does not say the three phrases are in the contract now, so a "
        "coordinator reading it still thinks their labels were decided beside it"
    )
    assert "`TAXONOMY_DECISIONS` is empty" in collapsed
    assert spec.TAXONOMY_DECISIONS == (), (
        f"the document says TAXONOMY_DECISIONS is empty and it is not: "
        f"{spec.TAXONOMY_DECISIONS!r}"
    )

    # The carrier word alone was always in phrases.py; it was the recording that
    # was missing, not the label. Keeping these two facts apart is the point.
    assert "hey." in hard_negatives


# ── the paragraphs no table can hold ─────────────────────────────────────────


def test_the_instructions_forbid_pausing_inside_the_name() -> None:
    """The E002 defect that cost the hardest confusion.

    Not a general "speak clearly" line: the document has to say which pause is
    wrong, because one of the trained spellings (`hey, youtab`) *does* have a
    pause in it and telling speakers never to pause would break that one.
    """
    # Whitespace-collapsed: the rule spans a line break in the rendered
    # markdown, and a substring check against the raw text would depend on
    # where the paragraph happens to wrap.
    lowered = re.sub(r"\s+", " ", _package().lower())

    assert "pause" in lowered
    assert "as one unit" in lowered, "the rule is not stated in a form a speaker can follow"
    assert "a pause *inside the name* is not" in lowered
    # The allowed pause is called out, so the instruction is precise rather
    # than just strict.
    assert "after *hey* is fine" in lowered
    # And the failure is attributed, so nobody removes the rule as pedantry.
    assert "e002" in lowered


def test_the_instructions_forbid_deleting_a_take_or_manufacturing_a_condition() -> None:
    """Two ways a session quietly becomes worthless than it looks.

    A difficult take deleted is the most useful recording in the folder thrown
    away; a condition produced by processing an existing file is a measurement
    of the processing.
    """
    lowered = re.sub(r"\s+", " ", _package().lower())

    assert "one condition, one genuine recording" in lowered
    assert "never take a normal recording and turn the volume down in an app" in lowered
    assert "never generate it by editing another file" in lowered
    assert "nothing is trimmed, converted, normalised, denoised, deleted, or re-recorded" in (
        lowered
    )
    assert "do not delete the one you paused in" in lowered
    assert "leave it in" in lowered


def test_the_package_covers_every_dimension_that_changes_the_signal() -> None:
    text = _package().lower()

    required = {
        "device": ("phone", "laptop", "headset"),
        "distance": ("close", "2–3 m"),
        "accent and first language": ("first language", "english varieties"),
        "pitch range": ("hz",),
        "noise condition": ("quiet", "television", "kitchen"),
    }
    for dimension, levels in required.items():
        assert dimension in text, f"the coverage table has no {dimension!r} row"
        for level in levels:
            assert level in text, f"{dimension}: {level!r} is not covered"

    # Each dimension has to say why it is there. A coverage table without
    # justifications is a checklist somebody will trim.
    for marker in ("direct-to-reverberant", "mel spectrogram", "beamforming"):
        assert marker in text, f"the coverage table lost its justification: {marker!r}"


def test_the_package_says_where_the_audio_lives_and_how_it_is_frozen() -> None:
    text = _package()
    assert "freeze_manifest.py" in text
    assert "sealed-evaluation" in text
    assert "never inside this repository" in text.lower()
    assert "validate_speaker_submission.py" in text
    # Cloud sync is the leak that actually happens: a voice memo is on somebody
    # else's server before the session ends.
    lowered = text.lower()
    assert "icloud" in lowered and "cloud sync" in lowered


def test_the_time_budget_is_printed_from_the_spec_and_adds_up() -> None:
    rows = _table(_package(), "Section", "What", "Time")
    budget = list(spec.TIME_BUDGET)
    assert len(rows) == len(budget) + 1, "the printed budget is missing its total row"

    for row, expected in zip(rows, budget):
        assert row[0] == expected.activity
        assert row[1] == expected.detail
        assert row[2] == f"{expected.minutes} min"

    total = rows[-1]
    assert total[0] == "**Total**"
    assert total[1] == f"**{spec.audio_files_per_speaker()} audio files**"
    assert total[2] == f"**≈ {spec.SESSION_MINUTES} min**"


def test_the_wake_phrase_conditions_are_printed_from_the_spec() -> None:
    rows = _table(_package(), "Folder", "Filename", "Takes", "How")
    assert len(rows) == len(spec.POSITIVE_SECTIONS)
    for row, section in zip(rows, spec.POSITIVE_SECTIONS):
        assert _unbacktick(row[0]) == f"{section.directory}/"
        assert _unbacktick(row[1]) == f"{spec.WAKE_PHRASE_SLUG}_{section.condition}_NNN.ext"
        assert int(row[2]) == section.takes
        assert row[3] == section.instruction


def test_the_upload_structure_is_the_four_entry_layout() -> None:
    text = _package()
    for entry in spec.SUBMISSION_ENTRIES:
        assert entry in text, f"the layout does not mention {entry!r}"
    assert f"{spec.ORIGINALS_DIR}/positive_<condition>/" in text
    assert f"{spec.ORIGINALS_DIR}/near_phrase/" in text
    assert "E003/" in text, "the layout is not shown for a real speaker label"
    # And the rule that makes the layout enforceable rather than advisory.
    lowered = re.sub(r"\s+", " ", text.lower())
    assert "an unrecognised name is treated as an error, not a guess" in lowered
    assert "there is no fifth entry" in lowered


def test_the_rule_of_three_numbers_in_the_document_come_from_the_spec() -> None:
    """The sizing claim is arithmetic, and the arithmetic is checked elsewhere."""
    rows = _table(_package(), "target", "trials a clean run needs")
    assert len(rows) == 2

    sealed = len(spec.labels_for_role(spec.ROLE_SEALED))
    expected = {
        f"{spec.rule_of_three(spec.FALSE_REJECT_TARGET)} positives": (
            f"{spec.positives_per_speaker()} × {sealed} = "
            f"**{sealed * spec.positives_per_speaker()}**"
        ),
        f"{spec.rule_of_three(spec.NEAR_MISS_FALSE_ACCEPT_TARGET)} near-phrase utterances": (
            f"{spec.near_phrase_negatives_per_speaker()} × {sealed} = "
            f"**{sealed * spec.near_phrase_negatives_per_speaker()}**"
        ),
    }
    printed = {row[1]: row[2] for row in rows}
    assert printed == expected, f"the printed arithmetic has drifted: {printed}"


# ── the assignments, and what the speaker is told about them ─────────────────


def test_the_document_states_the_assignment_policy_and_where_the_table_lives() -> None:
    lowered = re.sub(r"\s+", " ", _package().lower())
    assert "before any recording begins" in lowered
    assert "speaker_assignments" in lowered, (
        "the document does not say where the assignment table actually lives, so a "
        "coordinator has nowhere to look it up"
    )
    assert "never changes afterwards" in lowered
    assert "your own consent form" in lowered or "on your own consent form" in lowered


def test_the_document_does_not_tell_a_speaker_which_split_they_are_in() -> None:
    """The instructions are identical for every role, and stay that way.

    Knowing you are the final exam changes how you speak, so the per-speaker
    map is kept in ``speaker_recording_spec.py`` -- which coordinators read and
    speakers are not handed -- and each speaker learns their own use category
    from their own consent form, where consent requires it. This checks the
    document never pairs a label with the role assigned to it.
    """
    text = _package()
    markers = {
        spec.ROLE_TRAINING: "training",
        spec.ROLE_VALIDATION: "validation",
        spec.ROLE_SEALED: "sealed",
    }
    paragraphs = re.split(r"\n\s*\n", text)

    for assignment in spec.SPEAKER_ASSIGNMENTS:
        marker = markers[assignment.role]
        for paragraph in paragraphs:
            if assignment.label not in paragraph:
                continue
            assert marker not in paragraph.lower(), (
                f"a paragraph names {assignment.label} and its own role ({marker!r}), "
                f"which tells that speaker which split they are in:\n{paragraph}"
            )

    # Non-vacuity: the labels and the role words are both in the document, so
    # the loop above is looking at something.
    assert any(a.label in text for a in spec.SPEAKER_ASSIGNMENTS)
    for marker in markers.values():
        assert marker in text.lower()


# ── labels decided beside the contract, and the ones folded into it ──────────


def test_a_decision_recorded_beside_the_contract_is_written_out_in_full() -> None:
    """The shape a ``TAXONOMY_DECISIONS`` entry has to be published in.

    Vacuous today, and asserted to be: the tuple is empty, which
    ``test_the_three_phrases_that_were_missing_are_hard_negatives_now`` checks
    against the document rather than leaving to be inferred. The requirement is
    kept because the mechanism is kept -- the next phrase that arrives in
    neither tuple has to be published with its basis and its consequence, not
    labelled in a table and left there.
    """
    text = _package()
    assert spec.TAXONOMY_DECISIONS == (), (
        "a decision is recorded beside the contract again; it needs a published "
        f"heading, basis and consequence: {spec.TAXONOMY_DECISIONS!r}"
    )
    for decision in spec.TAXONOMY_DECISIONS:
        phrase = _spoken(decision.text)
        heading = re.search(
            rf"^### Decision \d+ — `{re.escape(decision.text)}` → \*\*{decision.label}\*\*",
            text,
            re.MULTILINE,
        )
        assert heading, f"{phrase!r} has no decision heading in the package"
        assert "**Basis.**" in text
        assert "**Consequence.**" in text


def test_the_basis_for_each_governed_addition_survived_the_move() -> None:
    """Why those three are in ``HARD_NEGATIVES`` has to stay readable.

    The labels are read off the contract now, so the package no longer records
    a decision. It still has to record the *reason*, because that reason is now
    the reason three entries exist in the contract, and an entry whose reason
    nobody can state is the entry a future trim removes.
    """
    collapsed = re.sub(r"\s+", " ", _package().lower())

    # How the runtime actually keys detection, which is what makes the carrier
    # part of the trained phrase rather than decoration around it.
    assert "purely cosmetic; engine keys detection" in collapsed, (
        "the okay-youtab basis does not cite how the runtime actually keys detection"
    )
    # And why adding it does not breach the HARD_NEGATIVES invariant.
    assert "it contains the name, not the carrier-plus-name phrase" in collapsed
    # The competing-assistant class, for the other two.
    assert "competing assistant" in collapsed
    assert "talking to a different device" in collapsed

    # The CONFUSABLE_NEGATIVES decision, which went the other way and therefore
    # needs its reason written down more than the additions do.
    assert "none of the three is in `phrases.confusable_negatives`" in collapsed
    assert "has ever been measured" in collapsed
    for text in ("okay youtab.", "hey google.", "hey siri."):
        assert text not in set(phrases.CONFUSABLE_NEGATIVES), (
            f"{text!r} is in phrases.CONFUSABLE_NEGATIVES but the package says none of "
            "the three is, and nothing has measured them"
        )


def test_the_split_carrier_positive_is_explained_and_not_just_labelled() -> None:
    """The mislabelling incident has to be explained where it can be read."""
    collapsed = re.sub(r"\s+", " ", _package().lower())
    assert "`hey you tab.` is a positive, not a near miss" in collapsed
    assert "suppress a real fire" in collapsed
    assert "bare `hey.` is already decided" in collapsed


# ── consent ──────────────────────────────────────────────────────────────────


def test_the_consent_record_covers_purpose_storage_publication_and_withdrawal() -> None:
    text = _consent()
    lowered = text.lower()

    # Purpose, and which purpose -- because "training" and "sealed evaluation"
    # are materially different things to agree to.
    assert "what it will be used for" in lowered
    assert "training" in lowered and "sealed" in lowered

    # Storage and publication.
    assert "local disk" in lowered
    assert "never committed" in lowered
    assert "never uploaded" in lowered
    assert "no cloud storage" in lowered or "no cloud" in lowered

    # Withdrawal: a route, a deadline, and what it cannot undo.
    assert "how to withdraw" in lowered
    assert "no reason is needed" in lowered
    assert re.search(r"within \*\*7 days\*\*|within 7 days", lowered), (
        "withdrawal has no deadline, which makes it a sentiment rather than a commitment"
    )
    assert "cannot undo" in lowered

    # Retention, because "until we are done" is not a period.
    assert "24 months" in lowered

    # And the fields somebody actually has to fill in.
    for field in ("name", "speaker label", "signature", "contact"):
        assert field in lowered, f"the record has no {field} field"


def test_the_consent_form_offers_exactly_the_three_uses_a_speaker_can_be_assigned() -> None:
    """One tick box per role, so the honest answer is available for every speaker."""
    lowered = _consent().lower()
    boxes = re.findall(r"^- \[ \] \*\*(.+?)\.\*\*", _consent(), re.MULTILINE)
    assert len(boxes) == len(spec.ROLES), f"the form offers {boxes}"
    assert "training" in lowered
    assert "validation" in lowered
    assert "sealed final evaluation" in lowered
    # And a coordinator, not the speaker, is the one who ticks it.
    assert "coordinator initials" in lowered
    assert "tick exactly one box" in lowered


def test_the_retention_policy_is_written_and_data_minimising() -> None:
    """The section that used to be a deliberate blank."""
    collapsed = _prose(_consent())

    # Minimisation: the label travels, identity does not.
    assert "data minimisation first" in collapsed
    assert "the dataset holds the speaker label and nothing that identifies the person" in (
        collapsed
    )
    for absent in ("no date of birth", "no address", "no employer"):
        assert absent in collapsed, f"the form does not rule out {absent!r}"

    # A period, tied to use rather than to a calendar nobody checks.
    assert "governed use" in collapsed
    assert "24 months" in collapsed
    assert "destroyed together with them" in collapsed

    # Protection at rest, and outside git.
    assert "encrypted and access-controlled" in collapsed
    assert "outside any code repository" in collapsed

    # Withdrawal is a documented process with a deadline and an honest limit.
    assert "within 7 days" in collapsed
    assert "what withdrawal cannot undo" in collapsed
    assert "retrain" in collapsed

    # Pending legal review, and explicitly not a blocker.
    assert "pending legal review" in collapsed
    assert "in force as written" in collapsed
    assert "nothing in this round waits on that review" in collapsed
    assert "no speaker is asked to sign a blank" in collapsed


def test_a_filled_consent_record_cannot_be_committed() -> None:
    """The template is tracked; a filled one carries the speaker's name.

    The filled file is the only artifact in this whole workflow that contains
    personal data by design, so the naming convention the template mandates has
    to be one the commit gate rejects.
    """
    text = _consent()
    assert "consent-record-" in text, "the template does not mandate a naming convention"

    gate = (
        REPO / "tests" / "tools" / "test_wakeword_no_human_data_committed.py"
    ).read_text(encoding="utf-8")
    assert "consent-record" in gate, (
        "the commit gate does not know about consent records, so the naming "
        "convention the template mandates protects nothing"
    )


def test_the_consent_form_and_the_package_agree_on_the_handoff() -> None:
    """The signed form travels separately and is filed into the archive.

    Both statements are true and they look contradictory, so both documents have
    to make the distinction rather than one of them stating half of it.
    """
    package = re.sub(r"\s+", " ", _package().lower())
    consent = re.sub(r"\s+", " ", _consent().lower())

    assert "hand your **signed consent form separately** from the audio" in package
    assert "does **not** travel with the audio" in consent
    assert spec.CONSENT_FILE.lower() in package
    assert spec.CONSENT_FILE.lower() in consent
    assert "separate in transit; bound together, under access control, at rest" in consent


# ── the one-page quick start ─────────────────────────────────────────────────


def test_the_package_has_a_standalone_one_page_quick_start() -> None:
    """The concise page a speaker actually reads, ahead of the detailed reference.

    It has to cover the four things asked of it -- what to say, how many
    times, where it goes, what not to do -- and it has to come before the
    detailed reference starts, so it is genuinely the first thing read rather
    than a summary bolted on at the end.
    """
    text = _package()
    lowered = re.sub(r"\s+", " ", text.lower())

    quick_start = text.index("## Quick start")
    part_a = text.index("# Part A")
    who_for = text.index("## Who this document is for")
    assert quick_start < who_for < part_a, (
        "the quick start has to come before the detailed reference, not after it"
    )

    assert '"hey youtab."' in lowered, "the quick start does not say what to say"
    assert "≈155\nfiles" in text or "≈155 files" in lowered.replace("\n", " "), (
        "the quick start does not say how many files"
    )
    assert "originals/" in text[quick_start:part_a]
    assert "consent.pdf" in text[quick_start:part_a].lower()
    assert "don't pause between" in lowered
    assert "don't delete, trim, denoise or re-record" in lowered
    assert "don't put your name, initials or email" in lowered
    assert "validate_speaker_submission.py" in text[quick_start:part_a]


def test_the_quick_start_does_not_use_a_markdown_table() -> None:
    """A pipe table here would risk colliding with a header set another test matches.

    ``_table()`` asserts exactly one table matches a given header set; a new
    table sharing headers with an existing one would make that assertion fail
    somewhere else in this file. The quick start uses a fenced code block and
    plain lists instead, on purpose.
    """
    text = _package()
    quick_start = text.index("## Quick start")
    who_for = text.index("## Who this document is for")
    section = text[quick_start:who_for]
    assert not _tables(section), "the quick start should not introduce a markdown table"


# ── pronunciation guidance ───────────────────────────────────────────────────


def test_the_package_has_pronunciation_guidance_that_does_not_change_the_phrase() -> None:
    text = _package()
    lowered = re.sub(r"\s+", " ", text.lower())

    assert "pronunciation notes" in lowered
    assert "the phrase itself never changes" in lowered
    assert '"hey youtab,"' in lowered
    assert "your own accent is exactly what this round needs" in lowered
    assert "do not try to imitate a reference recording" in lowered

    # Grounded in phrases.py's own documented IPA, not independently invented.
    phrases_text = (WAKEWORD / "phrases.py").read_text(encoding="utf-8")
    transcriptions = [
        match for match in re.findall(r"``(h[^`]+)``", phrases_text) if "j" in match
    ]
    assert len(transcriptions) == 2, (
        f"expected the two documented IPA transcriptions in phrases.py, found {transcriptions}"
    )
    for transcription in transcriptions:
        assert transcription in text, (
            f"the pronunciation notes do not quote phrases.py's own {transcription!r}"
        )

    # A handful of different first-language backgrounds, not just English ones.
    for language in ("farsi", "spanish", "mandarin", "hindi", "arabic", "french"):
        assert language in lowered, f"the pronunciation guide has no entry for {language!r}"


def test_the_pronunciation_table_does_not_collide_with_another_tables_headers() -> None:
    rows = _table(_package(), "First language", "A rough guide")
    assert len(rows) >= 5


# ── minimum vs expected time ─────────────────────────────────────────────────


def test_the_minimum_vs_expected_time_is_printed_and_matches_the_spec() -> None:
    text = _package()
    assert f"**≈ {spec.MINIMUM_RECORDING_MINUTES} min minimum**" in text
    assert f"**≈ {spec.SESSION_MINUTES} min expected**" in text
    assert spec.MINIMUM_RECORDING_MINUTES < spec.SESSION_MINUTES

    lowered = re.sub(r"\s+", " ", text.lower())
    assert "minimum vs expected" in lowered
    assert "not slack inside it" in lowered


# ── the automatic renamer ────────────────────────────────────────────────────


def test_the_package_documents_the_automatic_renamer() -> None:
    text = _package()
    lowered = re.sub(r"\s+", " ", text.lower())

    assert "--rename plan" in text
    assert "--rename apply" in text
    assert "do not have to type those names by hand" in lowered
    assert "leaves it\nuntouched rather than guessing which file is which" in text or (
        "untouched rather than guessing which file is which" in lowered
    )
    # Mentioned in both halves of the document: the speaker-facing reference
    # and the coordinator's assembly steps.
    assert lowered.count("--rename") >= 4


def test_the_prose_count_of_wake_word_rows_matches_the_table() -> None:
    """A speaker reads the sentence, not the table, and counts on it.

    The prose said "Three of the phrases below -- marked **Wake word**" while the
    table marked two. Someone following the instructions would look for a third
    positive that does not exist, and either invent one or stop trusting the
    document. It drifted because the number was spelled out in prose and the rows
    were somewhere else, so nothing tied them together.

    Derived rather than pinned: this reads the spelled-out word and the row count
    and compares them, so adding or removing a positive row fails here instead of
    leaving a sentence that quietly disagrees.
    """
    text = PACKAGE.read_text(encoding="utf-8")

    marker = "**Wake word**"
    rows = [line for line in text.splitlines()
            if line.startswith("|") and marker in line]
    # Non-vacuity: if the marker or the table shape ever changes, this test must
    # fail loudly rather than compare zero against a word it also cannot find.
    assert rows, f"no table row carries {marker}; re-anchor this test"

    words = {
        1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five",
        6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten",
    }
    expected = words[len(rows)]
    sentence = f"{expected} of the phrases below — marked {marker} —"
    assert sentence in text, (
        f"{len(rows)} table row(s) are marked {marker}, so the prose should read "
        f"{expected!r}. A speaker counts on that sentence and will look for a "
        f"positive that is not there."
    )
