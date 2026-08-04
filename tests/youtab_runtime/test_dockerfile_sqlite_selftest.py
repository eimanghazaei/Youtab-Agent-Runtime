"""The Dockerfile's SQLite self-test must actually pass against real SQLite.

The image build verifies its patched SQLite by creating an FTS5 trigram table,
inserting one row and asserting a substring search finds it. The probe term was
an interior trigram of the *retired* product name. When the name changed, the
inserted value was updated and the search term was not, so the assertion looked
correct and could never hold: the row no longer contained the trigram being
searched for, the count came back 0, and the build aborted at that layer.

Nothing caught it, because the only thing that ran it was a full image build.
This test extracts both halves out of the Dockerfile and runs them against the
sqlite3 the tests already have, so the mismatch fails in the ordinary suite
instead of in a container build nobody runs before pushing.

It deliberately reads the Dockerfile rather than restating its values. A test
carrying its own copy of the insert and the probe would keep passing after
someone edited the Dockerfile, which is the failure it exists to prevent.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def _selftest_terms() -> tuple[str, str]:
    """Return (inserted_value, match_term) as written in the Dockerfile."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    inserted = re.search(r"INSERT INTO docs VALUES \('([^']+)'\)", text)
    probed = re.search(r"FROM docs WHERE docs MATCH '([^']+)'", text)
    assert inserted, "could not find the self-test INSERT in the Dockerfile"
    assert probed, "could not find the self-test MATCH in the Dockerfile"
    return inserted.group(1), probed.group(1)


@pytest.fixture(scope="module")
def trigram_db():
    db = sqlite3.connect(":memory:")
    try:
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='trigram')"
        )
    except sqlite3.OperationalError as exc:  # pragma: no cover - build-dependent
        pytest.skip(f"this sqlite3 has no FTS5 trigram tokenizer: {exc}")
    yield db
    db.close()


def test_the_probe_matches_the_row_the_selftest_inserts(trigram_db):
    """The exact assertion the image build makes, run here instead."""
    inserted, term = _selftest_terms()
    trigram_db.execute("INSERT INTO docs VALUES (?)", (inserted,))
    count = trigram_db.execute(
        "SELECT count(*) FROM docs WHERE docs MATCH ?", (term,)
    ).fetchone()[0]
    assert count == 1, (
        f"the Dockerfile inserts {inserted!r} then searches for {term!r}, "
        "which finds nothing — the image build aborts at that layer"
    )


def test_the_probe_is_a_substring_of_the_inserted_value():
    """States the invariant directly, so the failure message names the cause.

    The test above proves the build breaks; this one says why in one line, and
    it holds regardless of whether the local sqlite3 has the tokenizer at all.
    """
    inserted, term = _selftest_terms()
    assert term in inserted, (
        f"{term!r} does not occur in {inserted!r} — renaming the inserted value "
        "without updating the probe is exactly how this broke before"
    )


def test_the_probe_is_not_a_prefix():
    """A trigram tokenizer is only being exercised by an interior substring.

    A prefix match would also succeed under ordinary prefix indexing, so a
    probe anchored at position 0 would pass without proving the trigram
    tokenizer works — which is the entire point of the self-test.
    """
    inserted, term = _selftest_terms()
    assert not inserted.startswith(term), (
        f"{term!r} is a prefix of {inserted!r}; the self-test would pass "
        "without demonstrating trigram tokenization"
    )
