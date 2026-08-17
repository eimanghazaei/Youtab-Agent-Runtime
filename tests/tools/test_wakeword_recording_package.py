"""The recording package has to close the gaps that made E002 half-useful.

Background
----------
E002 is a real speaker, recorded once, and three things were missing from the
session that no amount of re-analysis can recover:

* ``okay youtab`` was never recorded. It is the only near phrase that carries
  the real keyword under a *different carrier word*, so it is the one that
  distinguishes a model keyed on the whole phrase from a model keyed on
  "youtab" — and "okay X" is what people say out of habit from other
  assistants.
* ``hey google`` and bare ``hey`` were never recorded. Both are things an
  always-on microphone hears constantly, and both begin exactly like the wake
  word.
* ``hey you tab`` was spoken with a pause between "you" and "tab", in every
  take. That fragments the phrase into two words the detector was never trained
  on, and it left the hardest confusion in the whole inventory — ``hey you
  tab`` (a positive) against ``hey you tap`` (a negative, one voicing feature
  away) — essentially untested.

A document can drift back into that state silently, so the take list is
machine-checked rather than trusted: every prompt is cross-referenced against
``scripts/wakeword/phrases.py``, and the four gaps above are asserted by name.

The other thing checked here is that the package still *says* the things it has
to say — coverage across five dimensions, and a consent record that covers
purpose, storage, publication and withdrawal. Those are the parts a hurried
edit trims first.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
PACKAGE = WAKEWORD / "SPEAKER_RECORDING_PACKAGE.md"
CONSENT = WAKEWORD / "CONSENT_RECORD_TEMPLATE.md"

sys.path.insert(0, str(WAKEWORD))

import phrases  # noqa: E402

#: Rows of the take-list table: `| n | \`prompt\` | class | takes | yes/no | why |`
_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*`([^`]+)`[^|]*\|\s*(positive|near)\s*\|\s*(\d+)\s*\|"
    r"\s*\*{0,2}(yes|no|n/a)\*{0,2}\s*\|(.*)\|\s*$",
    re.MULTILINE,
)


def _normalise(text: str) -> str:
    """Compare phrases by what is said, not by punctuation."""
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def _inventory() -> set[str]:
    """Every phrase the pipeline synthesizes, normalised."""
    spoken = [text for text, _weight in phrases.POSITIVE_SPELLINGS]
    spoken += list(phrases.HARD_NEGATIVES)
    spoken += list(phrases.SOFT_NEGATIVES)
    spoken += list(phrases.COMMON_PHRASES)
    spoken += list(phrases.CONFUSABLE_NEGATIVES)
    return {_normalise(text) for text in spoken}


def _rows() -> list[dict]:
    text = PACKAGE.read_text(encoding="utf-8")
    rows = [
        {
            "n": int(match.group(1)),
            "prompt": match.group(2).strip(),
            "class": match.group(3),
            "takes": int(match.group(4)),
            "synthesized": match.group(5),
            "why": match.group(6).strip(),
        }
        for match in _ROW.finditer(text)
    ]
    return rows


# ── the take list ────────────────────────────────────────────────────────────


def test_the_take_list_parses_and_is_not_a_token_list() -> None:
    """Non-vacuity for everything below, and a floor on the session itself.

    Every assertion in this file reads the parsed rows. A regex that stopped
    matching would make all of them pass over an empty list.
    """
    rows = _rows()
    assert len(rows) >= 20, f"only {len(rows)} take-list rows parsed"

    positives = [row for row in rows if row["class"] == "positive"]
    near = [row for row in rows if row["class"] == "near"]
    assert positives and near

    # The counts the package's own arithmetic depends on: 36 positives and 40
    # near-phrase utterances per speaker are what make four speakers enough to
    # demonstrate 5% false rejects and 2% near-miss false accepts.
    assert sum(row["takes"] for row in positives) >= 36, "too few positives to bound 5%"
    assert sum(row["takes"] for row in near) >= 40, "too few near phrases to bound 2%"
    for row in rows:
        assert row["why"].strip(), f"row {row['n']} has no justification"


def test_the_four_gaps_from_e002_are_all_in_the_list() -> None:
    """The whole reason this package exists, asserted one phrase at a time."""
    by_prompt = {_normalise(row["prompt"]): row for row in _rows()}

    for prompt in ("okay youtab", "hey google", "hey"):
        assert prompt in by_prompt, f"{prompt!r} is missing — it was missing from E002 too"
        assert by_prompt[prompt]["class"] == "near", f"{prompt!r} is not marked as a near phrase"
        assert by_prompt[prompt]["takes"] >= 2, f"{prompt!r} has too few takes to mean anything"

    # `hey you tab` is a POSITIVE spelling, not a near phrase -- it is one of
    # the orthographies the model is trained to fire on. Getting this wrong in
    # either direction would invert the whole test.
    assert "hey you tab" in by_prompt
    assert by_prompt["hey you tab"]["class"] == "positive"
    assert _normalise("hey you tab.") in {
        _normalise(text) for text, _ in phrases.POSITIVE_SPELLINGS
    }

    # And its minimal pair has to be recorded too, or the confusion is still
    # untested: one voicing feature separates them.
    assert "hey you tap" in by_prompt
    assert by_prompt["hey you tap"]["class"] == "near"


def test_the_instructions_forbid_pausing_inside_the_name() -> None:
    """The E002 defect that cost the hardest confusion.

    Not a general "speak clearly" line: the document has to say which pause is
    wrong, because one of the trained spellings (`hey, youtab`) *does* have a
    pause in it and telling speakers never to pause would break that one.
    """
    # Whitespace-collapsed: the rule spans a line break in the rendered
    # markdown, and a substring check against the raw text would depend on
    # where the paragraph happens to wrap.
    lowered = re.sub(r"\s+", " ", PACKAGE.read_text(encoding="utf-8").lower())

    assert "pause" in lowered
    assert "as one unit" in lowered, "the rule is not stated in a form a speaker can follow"
    assert "a pause *inside the name* is not" in lowered
    # The allowed pause is called out, so the instruction is precise rather
    # than just strict.
    assert "after *hey* is fine" in lowered
    # And the failure is attributed, so nobody removes the rule as pedantry.
    assert "e002" in lowered

    row = next(row for row in _rows() if _normalise(row["prompt"]) == "hey you tab")
    assert "pausing" in row["why"].lower() or "pause" in row["why"].lower(), (
        "the `hey you tab` row does not carry the warning where it will be read"
    )


def test_every_prompt_is_correctly_marked_against_the_synthesis_inventory() -> None:
    """The drift check between this document and phrases.py.

    A prompt marked "yes" that training has never synthesized is a false claim
    about coverage. A prompt marked "no" that *is* in the inventory hides the
    fact that the gap has been closed. Both are the kind of error nobody finds
    by reading.
    """
    inventory = _inventory()
    assert len(inventory) > 100, f"the phrase inventory reads as {len(inventory)} phrases"

    wrong: list[str] = []
    marked_no = 0
    for row in _rows():
        if row["synthesized"] == "n/a":
            continue  # free conversation, which is not a phrase
        present = _normalise(row["prompt"]) in inventory
        claimed = row["synthesized"] == "yes"
        if present != claimed:
            wrong.append(
                f"{row['prompt']!r} marked {row['synthesized']!r} but "
                f"{'is' if present else 'is not'} in phrases.py"
            )
        marked_no += int(not claimed)
    assert not wrong, "\n  ".join(wrong)

    # Non-vacuity: the column has to discriminate. If every row said "yes" the
    # check above would pass while proving nothing about the gaps.
    assert marked_no >= 3, "no prompt is marked as outside the synthesis inventory"


def test_the_evaluation_only_phrases_are_the_ones_that_were_missing() -> None:
    """`okay youtab` and `hey google` are not in phrases.py, and that is a finding.

    They are recorded here as evaluation-only rather than quietly added to the
    synthesis inventory: adding a phrase changes what the next training run
    fits, which is a decision for whoever owns that run. This test pins the
    current state so the decision is visible rather than implied.
    """
    inventory = _inventory()
    assert "okay youtab" not in inventory, (
        "`okay youtab` is now synthesized. Good — but move it to `yes` in the "
        "package's table, and say so, rather than leaving two records disagreeing."
    )
    assert "hey google" not in inventory
    # The carrier word alone IS synthesized; it was the recording that was
    # missing, not the phrase. Keeping these two facts apart is the point.
    assert "hey" in inventory


# ── coverage ─────────────────────────────────────────────────────────────────


def test_the_package_covers_every_dimension_that_changes_the_signal() -> None:
    text = PACKAGE.read_text(encoding="utf-8").lower()

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
    text = PACKAGE.read_text(encoding="utf-8")
    assert "freeze_manifest.py" in text
    assert "--usage sealed-evaluation" in text or "sealed-evaluation" in text
    assert "never inside this repository" in text.lower()
    # Cloud sync is the leak that actually happens: a voice memo is on somebody
    # else's server before the session ends.
    lowered = text.lower()
    assert "icloud" in lowered and "cloud sync" in lowered


# ── consent ──────────────────────────────────────────────────────────────────


def test_the_consent_record_covers_purpose_storage_publication_and_withdrawal() -> None:
    text = CONSENT.read_text(encoding="utf-8")
    lowered = text.lower()

    # Purpose, and which of the two purposes -- because "training" and "sealed
    # evaluation" are materially different things to agree to.
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


def test_a_filled_consent_record_cannot_be_committed() -> None:
    """The template is tracked; a filled one carries the speaker's name.

    The filled file is the only artifact in this whole workflow that contains
    personal data by design, so the naming convention the template mandates has
    to be one the commit gate rejects.
    """
    text = CONSENT.read_text(encoding="utf-8")
    assert "consent-record-" in text, "the template does not mandate a naming convention"

    gate = (
        REPO / "tests" / "tools" / "test_wakeword_no_human_data_committed.py"
    ).read_text(encoding="utf-8")
    assert "consent-record" in gate, (
        "the commit gate does not know about consent records, so the naming "
        "convention the template mandates protects nothing"
    )
