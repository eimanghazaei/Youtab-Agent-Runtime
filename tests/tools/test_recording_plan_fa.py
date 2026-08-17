"""The Persian compact plan must not drift from the canonical near-phrase list.

`RECORDING_PLAN_FA.md` reprints the 34-row near-phrase battery for the two
humans. The battery is maintained in one place -- `speaker_recording_spec`'s
`NEAR_PHRASE_ITEMS` -- so a copy in a document is a copy that can rot: a phrase
added, removed or re-spelled in the registry leaves the plan telling a recorder
to say something the pipeline no longer expects. This asserts the document
carries exactly the canonical set, so the drift fails here instead of at a
recording session nobody can redo.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WAKEWORD = Path(__file__).resolve().parents[2] / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import speaker_recording_spec as spec  # noqa: E402

PLAN = WAKEWORD / "RECORDING_PLAN_FA.md"


def _near_phrase_section(text: str) -> str:
    """Just the near-phrase table region, so incidental back-ticked phrases
    elsewhere in the document cannot pad or mask the comparison."""
    after = text.split("بخش ۳", 1)
    assert len(after) == 2, "RECORDING_PLAN_FA.md has no near-phrase section header"
    return after[1].split("بخش ۴", 1)[0]


def test_the_plan_reprints_exactly_the_canonical_near_phrase_set() -> None:
    region = _near_phrase_section(PLAN.read_text(encoding="utf-8"))
    printed = set(re.findall(r"`([a-z][^`]*\.)`", region))
    canonical = {item.text for item in spec.NEAR_PHRASE_ITEMS}
    missing = canonical - printed
    extra = printed - canonical
    assert not missing, f"plan is missing canonical near phrases: {sorted(missing)}"
    assert not extra, f"plan lists phrases not in the registry: {sorted(extra)}"


def test_the_two_wake_spellings_are_marked_in_the_plan() -> None:
    region = _near_phrase_section(PLAN.read_text(encoding="utf-8"))
    wake = [item.text for item in spec.NEAR_PHRASE_ITEMS if item.label == "positive"]
    assert wake, "no positive spelling in the battery; re-anchor this test"
    for phrase in wake:
        line = next(
            (ln for ln in region.splitlines() if f"`{phrase}`" in ln), None
        )
        assert line is not None, f"{phrase!r} is not in the plan's near-phrase table"
        assert "[wake]" in line, f"{phrase!r} is a wake spelling but not marked [wake]"
