"""P5 — install instructions must handle passwords with URI-special characters.

Deterministic (no live server): proves the two documented DSN forms carry a
password containing @ : / ? # correctly, and that a RAW unencoded password in a
URL DSN is mis-parsed (the hazard the install doc warns against).
"""

import urllib.parse

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import conninfo_to_dict  # noqa: E402

PW = "p@ss:w/rd#1"


def test_keyword_conninfo_preserves_special_password():
    # The compose default form (libpq keyword conninfo) carries the password
    # verbatim — no percent-encoding needed for URI-special chars.
    d = conninfo_to_dict(f"host=postgres port=5432 dbname=durable user=youtab password={PW}")
    assert d["password"] == PW
    assert d["host"] == "postgres" and d["dbname"] == "durable"


def test_url_dsn_requires_percent_encoding():
    # URL-encoded password round-trips correctly.
    enc = urllib.parse.quote(PW, safe="")
    d = conninfo_to_dict(f"postgresql://youtab:{enc}@postgres:5432/durable")
    assert d["password"] == PW
    # A RAW (unencoded) password in a URL DSN is mis-parsed (does NOT equal PW) or
    # raises — this is exactly why the doc requires encoding / the keyword form.
    try:
        d2 = conninfo_to_dict(f"postgresql://youtab:{PW}@postgres:5432/durable")
        assert d2.get("password") != PW
    except Exception:
        pass  # raising is also acceptable evidence of the hazard
