"""Every external input has to be attributable, and stay attributable.

Background
----------
``tools/wakewords/hey_youtab.onnx`` and ``.tflite`` are redistributed. They are
derived works of openWakeWord's front end and of speech from two CC BY 4.0
corpora, and both licences require attribution — which the model card can only
provide for inputs somebody wrote down.

Writing them down once is not the hard part. Keeping the record complete is:
``silero_vad.onnx`` was added to the pinned set to stop a mid-evaluation
network race, and its licence row was never added to the model card, so the
generated table described five of the six pinned files for as long as that
went unnoticed. Nothing failed. That is the failure mode this file is about —
an unattributed input is invisible until somebody asks a legal question.

Three claims are checked:

1. every pinned ``Asset`` names a ``Source`` that exists, and does not
   contradict it;
2. every ``Source`` in use records a version, a licence and an attribution,
   and every source *not* in use records why;
3. no pipeline script mentions a corpus the table does not account for — the
   check that makes adding an unattributed dataset fail here rather than in a
   licence review.

Claim 3 is why MUSAN is in ``SOURCES`` at all. It is not used, and an absent
corpus and a refused corpus are indistinguishable in a dependency list.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import assets  # noqa: E402

MODEL_CARD = REPO / "tools" / "wakewords" / "MODEL_CARD.md"
README = WAKEWORD / "README.md"

#: Corpora and generators a speech pipeline plausibly reaches for. If one of
#: these turns up in a pipeline script it must be accounted for in SOURCES —
#: as in-use, planned or declined. The list is not a claim to be exhaustive;
#: it is the set of names worth catching automatically, and an unlisted corpus
#: still has to survive the review of the diff that adds it.
KNOWN_CORPORA: dict[str, str] = {
    "musan": "musan",
    "librispeech": "",
    "libritts": "libritts-r",
    "voxceleb": "",
    "common voice": "common-voice",
    "common_voice": "common-voice",
    "commonvoice": "common-voice",
    "audioset": "",
    "urbansound": "",
    "fsd50k": "",
    "tedlium": "",
    "gigaspeech": "",
    "speech_commands": "speech-commands",
    "speech commands": "speech-commands",
    "pyroomacoustics": "pyroomacoustics",
    "espeak": "espeak-ng",
    "silero": "silero-vad",
}

VALID_STATUS = {"in-use", "planned", "declined"}


def _sources_by_key() -> dict[str, assets.Source]:
    return {source.key: source for source in assets.SOURCES}


# ── the pinned files ─────────────────────────────────────────────────────────


def test_every_pinned_asset_resolves_to_a_recorded_source() -> None:
    # Non-vacuity: seven pinned files and nine recorded sources. A table that
    # emptied itself would satisfy every "for asset in ALL_ASSETS" below.
    assert len(assets.ALL_ASSETS) == 7, [a.name for a in assets.ALL_ASSETS]
    assert len(assets.SOURCES) >= 8

    for asset in assets.ALL_ASSETS:
        source = assets.source_for(asset)  # raises with an actionable message
        assert source.status == "in-use", (
            f"{asset.name} is pinned and downloaded, but its source "
            f"{source.key!r} is recorded as {source.status!r}"
        )
        assert asset.name in source.provides, (
            f"{source.key} does not list {asset.name} in `provides`; the "
            "mapping is only checked in one direction otherwise"
        )


def test_no_source_claims_to_provide_a_file_that_is_not_pinned() -> None:
    """The other direction. A stale `provides` entry is a false attribution."""
    pinned = {asset.name for asset in assets.ALL_ASSETS}
    for source in assets.SOURCES:
        stale = set(source.provides) - pinned
        assert not stale, f"{source.key} claims to provide unpinned files: {sorted(stale)}"
        if source.status != "in-use":
            assert not source.provides, (
                f"{source.key} is {source.status!r} but claims to provide "
                f"{source.provides}; a corpus that is not used cannot be "
                "supplying bytes"
            )


def test_an_asset_licence_never_contradicts_its_source() -> None:
    """Wording may differ; the licence itself may not.

    ``silero_vad.onnx`` is "MIT (Silero VAD…), redistributed by openWakeWord"
    and its source is plain "MIT" — a longer sentence about the same terms is
    fine. "Apache-2.0" against an MIT source is not.
    """
    for asset in assets.ALL_ASSETS:
        source = assets.source_for(asset)
        assert asset.license.startswith(source.license), (
            f"{asset.name} is licensed {asset.license!r} but its source "
            f"{source.key} is {source.license!r}"
        )


# ── the record itself ────────────────────────────────────────────────────────


def test_every_source_records_a_licence_a_version_and_an_attribution() -> None:
    seen_keys = set()
    for source in assets.SOURCES:
        assert source.key not in seen_keys, f"duplicate source key {source.key!r}"
        seen_keys.add(source.key)
        assert source.status in VALID_STATUS, f"{source.key}: status {source.status!r}"
        for field in ("name", "kind", "origin", "version", "license", "license_url", "attribution"):
            assert getattr(source, field).strip(), f"{source.key} has no {field}"
        assert source.origin.startswith("https://"), f"{source.key}: {source.origin}"
        assert source.notes.strip(), (
            f"{source.key} has no notes. Every entry here needs one: an in-use "
            "source has to say what it contributes, and a planned or declined "
            "one has to say why it is not being used."
        )
    assert len(seen_keys) == len(assets.SOURCES)


def test_an_unpinned_version_has_to_say_so_and_say_what_it_costs() -> None:
    """A vague version is worse than a stated absence — it reads as a pin.

    The clone of piper-sample-generator and the pip installs of pyroomacoustics
    and espeak-ng are genuinely unpinned. That is a real exposure and it is
    allowed to exist; what is not allowed is for it to be invisible.
    """
    unpinned = [s for s in assets.SOURCES if "unpinned" in s.version.lower()]
    # Non-vacuity: if this found nothing, the rule below would be untested and
    # the three known-unpinned sources would have silently acquired pins.
    assert len(unpinned) >= 3, [s.key for s in unpinned]
    for source in unpinned:
        assert len(source.notes) > 80, (
            f"{source.key} is unpinned but its notes do not explain the exposure"
        )

    vague = [
        s.key
        for s in assets.SOURCES
        if s.version.strip().lower() in {"latest", "main", "master", "head", "unknown", "n/a"}
    ]
    assert not vague, f"these sources record a version that is not one: {vague}"


def test_a_source_that_is_not_used_says_why_and_claims_nothing() -> None:
    """MUSAN is the case this is written for.

    It is the obvious corpus for background noise, it is not used, and without
    a record that reads as an oversight. The entry also must not assert a
    licence as verified: nothing here has ever fetched the corpus.
    """
    declined = {s.key: s for s in assets.SOURCES if s.status == "declined"}
    assert "musan" in declined, (
        "MUSAN is not recorded. It is the corpus a reader will expect to find, "
        "and its absence needs to be a decision rather than a gap."
    )
    musan = declined["musan"]
    assert not musan.provides
    assert "not used" in musan.notes.lower() or "no script references it" in musan.notes.lower()
    assert "not independently verified" in musan.license.lower(), (
        "the MUSAN licence is quoted from its publisher and has never been "
        "checked against a downloaded copy; the record has to say so rather "
        "than assert a licence this repository cannot substantiate"
    )

    planned = [s for s in assets.SOURCES if s.status == "planned"]
    for source in planned:
        assert "owner" in source.notes.lower() or "token" in source.notes.lower(), (
            f"{source.key} is planned but nothing says what is blocking it"
        )


# ── the documents that quote the record ──────────────────────────────────────


def _card_licence_rows() -> dict[str, tuple[str, str]]:
    """`| \\`name\\` | licence | \\`sha…\\` | attribution |` rows, by filename."""
    text = MODEL_CARD.read_text(encoding="utf-8")
    section = text.split("## Licences", 1)
    assert len(section) == 2, "MODEL_CARD.md has no Licences section; re-anchor this test"
    rows = {}
    for line in section[1].splitlines():
        match = re.match(r"\|\s*`([^`]+)`\s*\|([^|]+)\|\s*`([0-9a-f]+)…`\s*\|", line)
        if match:
            rows[match.group(1)] = (match.group(2).strip(), match.group(3))
    return rows


def test_the_model_card_attributes_every_pinned_input() -> None:
    """The card is the artifact's licence notice, so it has to be complete.

    It is generated from ``ALL_ASSETS`` by ``write_model_card.py``, which means
    this also catches a card that has drifted behind the asset table — exactly
    what happened when ``silero_vad.onnx`` was pinned.
    """
    rows = _card_licence_rows()
    assert len(rows) >= 7, f"only {len(rows)} licence rows parsed out of the card"

    for asset in assets.ALL_ASSETS:
        assert asset.name in rows, (
            f"{asset.name} is fetched by the pipeline and has no row in the "
            "model card's licence table. Regenerate the card with "
            "write_model_card.py, or add the row it would have written."
        )
        licence, digest = rows[asset.name]
        assert licence == asset.license, f"{asset.name}: card says {licence!r}"
        assert asset.sha256.startswith(digest), (
            f"{asset.name}: the card's hash prefix {digest} does not match the "
            f"pinned {asset.sha256[:16]}"
        )


def test_the_readme_provenance_table_names_every_source_in_use() -> None:
    text = README.read_text(encoding="utf-8").lower()
    assert "## data provenance" in text or "data provenance" in text
    for source in assets.SOURCES:
        if source.status != "in-use":
            continue
        assert source.name.lower() in text, (
            f"{source.name} is an input to the pipeline and is not mentioned in "
            "scripts/wakeword/README.md"
        )
    # MUSAN's absence is a claim the README makes, so the README has to make it.
    assert "musan" in text, (
        "the README does not mention MUSAN. Readers expect it; say it is not "
        "used rather than leaving them to infer it."
    )


# ── the guard against a silent addition ──────────────────────────────────────


def _pipeline_sources() -> dict[str, str]:
    """Every script the pipeline runs, as lower-cased text."""
    files = sorted(WAKEWORD.glob("*.py")) + sorted(WAKEWORD.glob("*.sh"))
    return {path.name: path.read_text(encoding="utf-8").lower() for path in files}


def test_no_pipeline_script_uses_a_corpus_the_record_does_not_account_for() -> None:
    """An unattributed corpus must fail here, not in a licence review.

    Adding a dataset is one line of a download URL. This is what makes that
    line stop until somebody has written down whose data it is and under what
    terms.
    """
    scripts = _pipeline_sources()

    # Non-vacuity, twice over: the files have to have been read, and the
    # scanner has to be able to find a corpus that is definitely there.
    assert len(scripts) >= 8, sorted(scripts)
    assert any("speech_commands" in text for text in scripts.values()), (
        "the scanner cannot find Speech Commands, which every run downloads; "
        "it would not find an unattributed corpus either"
    )

    known = _sources_by_key()
    unaccounted: list[str] = []
    for filename, text in scripts.items():
        for corpus, key in KNOWN_CORPORA.items():
            if corpus not in text:
                continue
            if not key or key not in known:
                unaccounted.append(f"{filename}: {corpus}")
    assert not unaccounted, (
        "these pipeline scripts reference a corpus with no entry in "
        "assets.SOURCES:\n  " + "\n  ".join(sorted(unaccounted))
    )


def test_the_corpus_scanner_would_catch_an_unrecorded_dataset() -> None:
    """Control. The test above passes when nothing is wrong *and* when it is blind."""
    known = _sources_by_key()
    pretend = "download the musan corpus and the voxceleb corpus".lower()
    caught = [
        corpus
        for corpus, key in KNOWN_CORPORA.items()
        if corpus in pretend and (not key or key not in known)
    ]
    assert "voxceleb" in caught, "an unrecorded corpus would not be flagged"
    assert "musan" not in caught, "MUSAN is recorded, so it must not be flagged"
