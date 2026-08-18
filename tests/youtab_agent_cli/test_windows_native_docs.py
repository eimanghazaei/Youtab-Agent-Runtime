from pathlib import Path


def test_windows_native_install_path_docs_match_installer() -> None:
    doc = Path("website/docs/user-guide/windows-native.md").read_text(encoding="utf-8")
    install = Path("scripts/install.ps1").read_text(encoding="utf-8")

    assert "%LOCALAPPDATA%\\youtab\\youtab-agent-runtime\\venv\\Scripts" in doc
    assert "Get-Command youtab        # should print C:\\Users\\<you>\\AppData\\Local\\youtab\\youtab-agent-runtime\\venv\\Scripts\\youtab.exe" in doc
    assert '$youtabBin = "$InstallDir\\venv\\Scripts"' in install
