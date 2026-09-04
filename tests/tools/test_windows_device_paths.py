"""Windows reserved-device-name hardening (WAVE-25 criteria 1-3).

The device predicate is pure and platform-independent, so its adversarial unit
tests run on every OS. Enforcement in the production guards is gated on
``os.name == "nt"`` and routes through code that resolves the home directory at
import time (``tools.file_operations._HOME = Path.home()``), so the integration
tests run NATIVELY on Windows rather than monkeypatching ``os.name`` — patching
it to ``"nt"`` on a POSIX runner would make ``Path.home()`` use Windows logic
and raise ``RuntimeError`` there. The Windows enforcement is therefore exercised
on the ``windows-tools`` / ``windows-runtime-cli`` CI legs; a paired POSIX-
behaviour test proves the rule does NOT change non-Windows semantics (the
existing POSIX sensitive-path suites are preserved untouched — this file only
ADDS coverage).
"""

import os

import pytest

from tools.path_security import is_windows_reserved_device_path


# Integration tests exercise the real guards, which resolve the home directory
# on import; run them where os.name is genuinely "nt" instead of forcing it.
windows_only = pytest.mark.skipif(
    os.name != "nt",
    reason="exercises the os.name=='nt' device enforcement natively; runs on the Windows CI legs",
)
# POSIX-behaviour tests run natively on the POSIX CI legs (ubuntu + macOS, where
# tests/tools already runs); on Windows "POSIX semantics" is not a thing to
# assert, and the paired Windows rejection is proven by the windows_only tests.
posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="asserts POSIX file-name semantics; runs on the POSIX CI legs",
)


# --------------------------------------------------------------------------- #
# 1. Pure predicate — adversarial unit tests (run on every platform)
# --------------------------------------------------------------------------- #
BLOCK_CASES = [
    # bare reserved names, case-insensitive
    "CON", "con", "Con", "PRN", "prn", "AUX", "aux", "NUL", "nul",
    "COM1", "com9", "LPT1", "lpt9",
    # with any extension (Windows resolves "con.txt" to the CON device)
    "con.txt", "CON.TXT", "com1.log", "nul.dat", "con.d",
    # trailing dot / space variants Win32 strips before device resolution
    "con.", "con ", "con. .", "nul...", "COM1 ", "com1.",
    # as any path component, both separators, with a drive letter
    "dir/con", r"dir\con", "sub/dir/nul.txt", r"C:\Users\x\CON", "C:con",
    # device / verbatim namespace prefixes and native device forms
    r"\\.\PhysicalDrive0", r"\\?\C:\x", "//./PhysicalDrive0", "//?/C:/x",
    # superscript digits Win32 accepts for COM/LPT
    "COM\u00b9", "LPT\u00b2",
    # legacy trailing-colon device aliases + alternate-data-stream form +
    # console pseudo-devices (Win32 resolves all of these to a device)
    "CON:", "con:", "COM1:", "NUL:", "LPT1:",
    "CON::$DATA", "con::$DATA", "nul::$DATA",
    "CONIN$", "CONOUT$", "conin$", "conout$", "CONIN$.txt",
    r"dir\CON:", "C:con:",
]

ALLOW_CASES = [
    # names that merely start with / contain a reserved token
    "console.txt", "condor", "communications", "report_con.txt", "nulled.log",
    "aux_backup", "config.yaml",
    # numbers outside the reserved 1-9 range
    "com0", "com10", "lpt0", "lpt10",
    # ordinary files and paths
    "normal.txt", "dir/normal", r"C:\Users\x\notes.md", "",
    ".hidden",
    # a colon/ADS on a NON-reserved stem stays an ordinary file, and names that
    # merely start with a console-device prefix are not the device
    "report:draft", "data.txt:stream", "coninside$", "conincorrect",
]


@pytest.mark.parametrize("path", BLOCK_CASES)
def test_reserved_device_is_detected(path):
    assert is_windows_reserved_device_path(path) is True


@pytest.mark.parametrize("path", ALLOW_CASES)
def test_ordinary_path_is_allowed(path):
    assert is_windows_reserved_device_path(path) is False


# --------------------------------------------------------------------------- #
# 2. Guard B — agent write/delete/move gate (agent/file_safety.py)
# --------------------------------------------------------------------------- #
@windows_only
class TestWriteDenialClassifierWindows:
    def test_device_name_is_denied(self):
        from agent import file_safety

        assert file_safety.is_write_denied("CON") is True
        assert file_safety._classify_write_denial("con.txt") == "device"
        msg = file_safety.get_write_denied_error("NUL")
        assert msg is not None and "reserved device" in msg

    def test_namespace_prefix_is_denied(self):
        from agent import file_safety

        assert file_safety.is_write_denied(r"\\.\PhysicalDrive0") is True

    def test_ordinary_file_still_allowed(self, tmp_path):
        from agent import file_safety

        assert file_safety.get_write_denied_error(str(tmp_path / "notes.md")) is None


class TestWriteDenialClassifierPosixUnchanged:
    """The device rule must NOT fire on POSIX: 'con'/'nul' are ordinary files
    there, so classification stays exactly as before (no write is performed)."""

    @posix_only
    def test_device_name_not_denied_on_posix(self, tmp_path):
        from agent import file_safety

        # A reserved *name* under a tmp dir is a normal POSIX file → allowed.
        assert file_safety.get_write_denied_error(str(tmp_path / "con")) is None
        assert file_safety._classify_write_denial(str(tmp_path / "nul.txt")) is None


# --------------------------------------------------------------------------- #
# 3. utils.py shared atomic sinks (cover skill/memory/cron/oauth writers)
# --------------------------------------------------------------------------- #
@windows_only
class TestAtomicSinksRejectDeviceWindows:
    def test_atomic_write_text_rejects_device(self):
        import utils

        with pytest.raises(ValueError, match="reserved device"):
            utils.atomic_write_text("CON", "data")

    def test_atomic_json_write_rejects_device(self):
        import utils

        with pytest.raises(ValueError, match="reserved device"):
            utils.atomic_json_write("nul.json", {"a": 1})

    def test_atomic_replace_rejects_device(self, tmp_path):
        import utils

        src = tmp_path / "src.tmp"
        src.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="reserved device"):
            utils.atomic_replace(str(src), r"\\.\PhysicalDrive0")

    def test_normal_write_still_works(self, tmp_path):
        import utils

        target = tmp_path / "ok.txt"
        utils.atomic_write_text(str(target), "hello")
        assert target.read_text(encoding="utf-8") == "hello"


# --------------------------------------------------------------------------- #
# 4. kanban attachment sanitiser (agent / dashboard / CLI attach path)
# --------------------------------------------------------------------------- #
class TestAttachmentNameRejectsDevice:
    @windows_only
    def test_reserved_names_rejected(self):
        from youtab_agent_cli import kanban_db

        for bad in ("CON", "con.txt", "NUL", "com1.log", "aux"):
            with pytest.raises(ValueError):
                kanban_db._safe_attachment_name(bad)

    @windows_only
    def test_ordinary_names_allowed(self):
        from youtab_agent_cli import kanban_db

        assert kanban_db._safe_attachment_name("report.pdf") == "report.pdf"

    @posix_only
    def test_posix_allows_reserved_name(self):
        from youtab_agent_cli import kanban_db

        # POSIX behaviour unchanged: 'CON' is a legal attachment basename.
        assert kanban_db._safe_attachment_name("CON") == "CON"


# --------------------------------------------------------------------------- #
# 5. Guard D read-side (tools/file_tools.py) — reading a device hangs / leaks
# --------------------------------------------------------------------------- #
@windows_only
class TestReadGuardRejectsDeviceWindows:
    def test_device_read_blocked(self):
        from tools import file_tools

        assert file_tools._is_blocked_device_path("CON") is True
        assert file_tools._is_blocked_device_path("nul.txt") is True
        assert file_tools._is_blocked_device_path(r"\\.\PhysicalDrive0") is True

    def test_ordinary_read_not_blocked(self):
        from tools import file_tools

        assert file_tools._is_blocked_device_path("notes.txt") is False
