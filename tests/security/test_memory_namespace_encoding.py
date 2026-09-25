"""Adversarial coverage for collision-free memory namespace encoding (SEC-9 #7).

The previous encoding replaced every disallowed character with ``_``, which was
not injective — ``tenant/a`` and ``tenant?a`` (and ``Tenant`` vs ``tenant`` on a
case-insensitive filesystem, and NFC vs NFD forms) collapsed to one directory,
crossing tenant boundaries. These tests prove the versioned, digest-backed
encoding is injective across every collision class while preserving traversal
containment and Windows device-name safety, on both Linux and Windows path
semantics.
"""

from __future__ import annotations

import importlib
import unicodedata

import pytest

mt = importlib.import_module("tools.memory_tool")

enc = mt.encode_namespace_component

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@pytest.fixture(autouse=True)
def _clear_ns(monkeypatch):
    token = mt._MEMORY_NAMESPACE.set(None)
    monkeypatch.delenv(mt.MEMORY_NAMESPACE_ENV, raising=False)
    yield
    mt._MEMORY_NAMESPACE.reset(token)


def test_versioned_and_lowercase():
    # Explicit version marker + lowercase-only output (case-fold-safe).
    out = enc("Tenant/Alpha")
    assert out.startswith(f"{mt._NAMESPACE_ENC_VERSION}-")
    assert out == out.lower()
    assert "/" not in out and "\\" not in out and ".." not in out


def test_slash_vs_question_no_collision():
    # The finding's canonical example.
    assert enc("tenant/a") != enc("tenant?a")


def test_disallowed_char_variants_distinct():
    variants = ["acme prod", "acme:prod", "acme/prod", "acme?prod", "acme_prod"]
    encoded = [enc(v) for v in variants]
    assert len(set(encoded)) == len(variants), encoded


def test_case_fold_no_collision_case_insensitive_fs():
    # Distinct encoded tokens; because the whole encoding is lowercase, a
    # case-insensitive filesystem (Windows/macOS) still resolves them to
    # DIFFERENT real paths (folding is a no-op on already-lowercase names).
    a, b = enc("Tenant"), enc("tenant")
    assert a != b
    assert a.lower() != b.lower()  # differ beyond case => survive case-fold


def test_reserved_device_name_escaped():
    for name in ("CON", "con", "PRN", "NUL", "COM1", "LPT9", "aux"):
        out = enc(name)
        stem = out.split(".", 1)[0]
        assert stem.upper() not in _WINDOWS_RESERVED, (name, out)


def test_unicode_nfc_nfd_same_tenant_one_dir():
    # café composed (U+00E9) vs decomposed (e + U+0301) -> SAME directory.
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    assert unicodedata.normalize("NFC", decomposed) == composed
    assert enc(composed) == enc(decomposed)


def test_unicode_distinct_tenants_no_collapse():
    # Two distinct non-ASCII ids must not both collapse to one "_"-style dir.
    assert enc("café") != enc("naïve")
    assert enc("тенант-1") != enc("тенант-2")


def test_traversal_still_neutralized():
    with mt.memory_namespace_scope("../../etc", "../evil"):
        resolved = mt.get_memory_dir()
    base = mt.get_youtab_home() / "memories"
    assert base in resolved.parents or resolved == base
    assert ".." not in resolved.parts
    # Only the namespace components appended under <base> are our concern; the
    # absolute prefix (e.g. a Windows drive anchor ``C:\``) is not.
    ns_parts = resolved.parts[len(base.parts):]
    for part in ns_parts:
        assert "/" not in part and "\\" not in part and part != ".."


def test_absolute_path_contained():
    for tenant in ("/etc/passwd", "C:\\Windows\\System32"):
        with mt.memory_namespace_scope(tenant, "ws"):
            resolved = mt.get_memory_dir()
        base = mt.get_youtab_home() / "memories"
        assert base in resolved.parents
        assert ".." not in resolved.parts


def test_legacy_ambiguous_dir_not_served_cross_tenant():
    # The old scheme mapped "acme/prod" and "acme?prod" to the SAME dir
    # "acme_prod". The new scheme must NOT resolve either tenant onto that
    # ambiguous legacy directory — legacy dirs are orphaned/quarantined, never
    # silently served across tenants.
    legacy = "acme_prod"
    for tenant in ("acme/prod", "acme?prod"):
        assert enc(tenant) != legacy
    # And the two formerly-colliding ids now get distinct dirs.
    assert enc("acme/prod") != enc("acme?prod")


def test_standalone_unset_is_flat_path():
    assert mt.get_memory_dir() == mt.get_youtab_home() / "memories"


def test_encoding_is_deterministic():
    assert enc("tenant-alpha") == enc("tenant-alpha")
