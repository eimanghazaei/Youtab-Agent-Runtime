from pathlib import Path
import subprocess

from tests import _wincompat


REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "setup-youtab.sh"


@_wincompat.requires_working_bash
def test_setup_youtab_script_is_valid_shell():
    # Feed the script to `bash -n` on stdin as bytes. A Windows path with
    # backslashes is mangled when handed to bash as an argument, and text-mode
    # piping would translate newlines; reading raw bytes sidesteps both.
    result = subprocess.run(
        ["bash", "-n", "-"], input=SETUP_SCRIPT.read_bytes(), capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode()


def test_setup_youtab_script_has_termux_path():
    content = SETUP_SCRIPT.read_text(encoding="utf-8")

    assert "is_termux()" in content
    assert ".[termux]" in content
    assert "constraints-termux.txt" in content
    assert "$PREFIX/bin" in content
