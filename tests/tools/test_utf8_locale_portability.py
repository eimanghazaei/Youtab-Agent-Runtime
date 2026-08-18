"""Deterministic UTF-8 portability tests under a simulated cp1252 default locale.

Windows installs with a Western ANSI code page default ``open()`` /
``Path.read_text()`` / ``Path.write_text()`` to **cp1252** when no ``encoding=``
argument is given. UTF-8 file content then either raises (an undefined cp1252
byte) or *silently mojibakes* (every byte maps to some cp1252 character). This
was the source of three separate Windows-sandbox regressions in this repo and is
the reason ``ruff`` enforces ``PLW1514`` (see ``[tool.ruff.lint]`` in
``pyproject.toml``) and why the portability sweep adds ``encoding="utf-8"`` to
every text I/O site.

These tests pin that failure mode **deterministically on any host** — including
Linux CI (and our ``PYTHONUTF8=1`` runner) where cp1252 is not the effective
default — by intercepting the text-mode default at the ``Path.read_text`` /
``Path.write_text`` / ``open`` boundary. They prove:

* explicit-UTF-8 reads/writes never raise and never mojibake;
* CJK, Persian, Arabic, accented Latin and emoji round-trip byte-for-byte;
* intentionally non-UTF-8 *binary* fixtures stay byte-exact via explicit
  binary handling (``read_bytes``/``write_bytes``) and are unaffected by the
  simulated locale;
* a text reader and writer that share ``utf-8`` are compatible, and a
  mismatched codec corrupts — so the pairing must be consistent;
* the ``encoding="utf-8"`` kwarg is *load-bearing*: removing it from a
  representative read or write turns the assertion RED under the simulated
  locale (an in-test mutation control).
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest import mock

import pytest

# Non-ASCII coverage: accented Latin, CJK, Persian, Arabic, emoji (incl. a ZWJ
# flag sequence). Each is a distinct decode/encode hazard under cp1252.
SAMPLES = {
    "accented": "café résumé naïve Grüße",
    "cjk": "日本語 中文 한국어",
    "persian": "سلام دنیا — یوتب",
    "arabic": "مرحبا بالعالم",
    "emoji": "🚀🔥✅🇮🇷",
    "mixed": "café — 日本語 — سلام — مرحبا — 🚀",
}
FULL = " ".join(SAMPLES.values())


@contextlib.contextmanager
def simulated_locale_default(encoding: str):
    """Force the *text-mode default* encoding, emulating a non-UTF-8 OS locale.

    This intercepts at the ``Path.read_text``/``Path.write_text`` and
    ``open`` boundary — where a caller-supplied ``encoding=None`` (i.e. "no
    explicit encoding") is still visible — and substitutes the given codec.
    Intercepting *before* ``io.text_encoding`` runs makes the simulation
    independent of the host's UTF-8 mode: the canonical runner and CI set
    ``PYTHONUTF8=1``, under which a bare read would otherwise silently resolve
    to utf-8 and mask the very defect these tests pin. Real end-user Windows
    runtime does **not** set ``PYTHONUTF8``, so cp1252 is the true default
    there — which is what we reproduce here, deterministically, on any host.

    Binary opens (``"b"`` in mode) and any call that passes an explicit
    ``encoding=`` are never rewritten.
    """
    real_open = io.open
    real_read_text = Path.read_text
    real_write_text = Path.write_text
    _enc = [encoding]

    def op(file, mode="r", buffering=-1, encoding=None, *args, **kwargs):
        if "b" not in mode and encoding in (None, "locale"):
            encoding = _enc[0]
        return real_open(file, mode, buffering, encoding, *args, **kwargs)

    def rt(self, encoding=None, *args, **kwargs):
        if encoding is None:
            encoding = _enc[0]
        return real_read_text(self, encoding, *args, **kwargs)

    def wt(self, data, encoding=None, *args, **kwargs):
        if encoding is None:
            encoding = _enc[0]
        return real_write_text(self, data, encoding, *args, **kwargs)

    with mock.patch("io.open", op), mock.patch("builtins.open", op), \
            mock.patch.object(Path, "read_text", rt), \
            mock.patch.object(Path, "write_text", wt):
        yield


# --- representative production patterns (also the mutation targets) ----------
def representative_write(path: Path, text: str) -> None:
    """Mirror of the repo's text-write convention (the sweep's target shape)."""
    path.write_text(text, encoding="utf-8")


def representative_read(path: Path) -> str:
    """Mirror of the repo's text-read convention (the sweep's target shape)."""
    return path.read_text(encoding="utf-8")


class TestCodecInvariants:
    def test_utf8_roundtrips_but_cp1252_does_not(self):
        raw = FULL.encode("utf-8")
        assert raw.decode("utf-8") == FULL
        # cp1252 either refuses an undefined byte or yields corrupted text.
        try:
            assert raw.decode("cp1252") != FULL
        except UnicodeDecodeError:
            pass


class TestReadsDoNotRaise:
    @pytest.mark.parametrize("name", list(SAMPLES))
    def test_utf8_read_write_roundtrip_under_cp1252(self, tmp_path, name):
        text = SAMPLES[name]
        p = tmp_path / f"{name}.txt"
        with simulated_locale_default("cp1252"):
            representative_write(p, text)   # explicit utf-8 write — must not raise
            got = representative_read(p)     # explicit utf-8 read — must not raise
        assert got == text                   # no mojibake
        assert p.read_bytes() == text.encode("utf-8")  # on-disk bytes are real utf-8


class TestNoMojibake:
    def test_bare_read_under_cp1252_corrupts_or_raises(self, tmp_path):
        """The defect the sweep fixes: a bare read of utf-8 content fails."""
        p = tmp_path / "x.txt"
        p.write_text(FULL, encoding="utf-8")
        with simulated_locale_default("cp1252"):
            try:
                got = p.read_text()          # BARE — resolves to cp1252 default
            except UnicodeDecodeError:
                return                        # hard failure: acceptable proof
        assert got != FULL                    # otherwise it silently mojibaked


class TestBinaryFixturesUnaffected:
    def test_non_utf8_binary_roundtrips_via_explicit_binary(self, tmp_path):
        # Every byte value incl. cp1252-undefined ones (0x81/0x8d/0x8f/0x90/0x9d).
        blob = bytes(range(256)) + b"\xff\xfe\x00\x81\x8d\x8f\x90\x9d"
        p = tmp_path / "b.bin"
        with simulated_locale_default("cp1252"):
            p.write_bytes(blob)
            assert p.read_bytes() == blob     # byte-exact, encoding-agnostic
        # A deliberately non-utf8 *text* fixture stays readable via its own codec.
        f = tmp_path / "l.txt"
        f.write_bytes("café".encode("cp1252"))
        assert f.read_text(encoding="cp1252") == "café"


class TestReaderWriterCompatible:
    def test_writer_and_reader_share_encoding(self, tmp_path):
        p = tmp_path / "c.txt"
        representative_write(p, FULL)         # utf-8 out
        assert representative_read(p) == FULL  # utf-8 in -> exact match
        # A mismatched reader corrupts (or refuses) — the pairing must agree.
        try:
            assert p.read_text(encoding="cp1252") != FULL
        except UnicodeDecodeError:
            pass


class TestMutationControls:
    """In-test mutation proof that the ``encoding="utf-8"`` kwarg is load-bearing."""

    def test_removing_encoding_from_read_turns_red(self, tmp_path):
        p = tmp_path / "r.txt"
        p.write_text(FULL, encoding="utf-8")
        with simulated_locale_default("cp1252"):
            assert representative_read(p) == FULL          # GREEN with the kwarg
            try:
                mutated_ok = p.read_text() == FULL          # kwarg removed (mutant)
            except UnicodeDecodeError:
                mutated_ok = False
        assert mutated_ok is False                          # RED without the kwarg

    def test_removing_encoding_from_write_turns_red(self, tmp_path):
        p = tmp_path / "w.txt"
        with simulated_locale_default("cp1252"):
            representative_write(p, FULL)                   # GREEN with the kwarg
            assert p.read_bytes() == FULL.encode("utf-8")
            try:
                (tmp_path / "w2.txt").write_text(FULL)       # kwarg removed (mutant)
                wrote_ok = True
            except UnicodeEncodeError:
                wrote_ok = False
        assert wrote_ok is False                            # RED without the kwarg

    def test_introducing_mojibake_is_detected(self, tmp_path):
        p = tmp_path / "m.txt"
        representative_write(p, FULL)
        mojibake = p.read_bytes().decode("cp1252", "replace")
        assert mojibake != FULL                             # corruption is detectable
        assert representative_read(p) == FULL               # the correct path is clean
