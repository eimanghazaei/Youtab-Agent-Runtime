"""Windows stage tests for the pinned, customer-facing Runtime artifact path."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"
SHA = "a" * 40
BASE = "https://api.youtab.io/pilot-runtime-" + "b" * 32 + "/releases"
ROOT = f"youtab-runtime-{SHA}/"
FILES = {
    "scripts/install.ps1": b"# pinned install script\n",
    "pyproject.toml": b"[project]\nname='youtab'\n",
    "apps/desktop/package.json": b'{"name":"youtab"}',
}


def _powershell() -> str:
    shell = shutil.which("powershell.exe")
    if not shell:
        pytest.skip("Windows PowerShell 5.1 is required for installer stage tests")
    return shell


def _make_archive(path: Path, *, entry: str | None = None, symlink: bool = False) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in FILES.items():
            archive.writestr(ROOT + name, data)
        if entry:
            member = zipfile.ZipInfo(ROOT + entry)
            if symlink:
                member.create_system = 3
                member.external_attr = 0o120777 << 16
            archive.writestr(member, b"outside")


def _run_stage(
    tmp_path: Path,
    *,
    manifest_change: dict[str, object] | None = None,
    entry: str | None = None,
    symlink: bool = False,
    missing_file: str | None = None,
    malformed_archive: bool = False,
    missing_manifest: bool = False,
    base: str = BASE,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object], Path]:
    archive_path = tmp_path / "runtime.zip"
    _make_archive(archive_path, entry=entry, symlink=symlink)
    if missing_file:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in FILES.items():
                if name != missing_file:
                    archive.writestr(ROOT + name, data)
    if malformed_archive:
        archive_path.write_bytes(b"not-a-zip")

    manifest: dict[str, object] = {
        "version": "0.19.1",
        "release_sequence": 1,
        "source_sha": SHA,
        "artifact_url": f"{BASE}/{SHA}/youtab-runtime-{SHA}.zip",
        "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "install_script_url": f"{BASE}/{SHA}/install-{SHA}.ps1",
        "install_script_sha256": hashlib.sha256(FILES["scripts/install.ps1"]).hexdigest(),
        "created_at": "2026-10-01T00:00:00Z",
        "platform": "windows",
        "architecture": "x64",
        "format": "zip",
    }
    manifest.update(manifest_change or {})
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "existing.txt").write_text("unchanged", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        YT_TEST_MANIFEST=str(manifest_path),
        YT_TEST_ZIP=str(archive_path),
        YT_TEST_INSTALL_DIR=str(install_dir),
        YT_TEST_SHA=SHA,
        YT_TEST_BASE=base,
        YT_TEST_SCRIPT=str(SCRIPT),
        YT_TEST_MISSING_MANIFEST="1" if missing_manifest else "0",
    )
    command = r"""
function git { throw 'Git must not be called in customer release mode' }
function Invoke-RestMethod {
    param($Uri, $MaximumRedirection)
    if ($env:YT_TEST_MISSING_MANIFEST -eq '1') { throw 'HTTP 404' }
    if ($Uri -cne "$env:YT_TEST_BASE/$env:YT_TEST_SHA/manifest.json" -or $MaximumRedirection -ne 0) { throw 'unexpected manifest URL' }
    Get-Content -LiteralPath $env:YT_TEST_MANIFEST -Raw | ConvertFrom-Json
}
function Invoke-WebRequest {
    param($Uri, $OutFile, [switch]$UseBasicParsing, $MaximumRedirection)
    if ($Uri -cne "$env:YT_TEST_BASE/$env:YT_TEST_SHA/youtab-runtime-$env:YT_TEST_SHA.zip" -or $MaximumRedirection -ne 0) { throw 'unexpected artifact URL' }
    Copy-Item -LiteralPath $env:YT_TEST_ZIP -Destination $OutFile
}
& $env:YT_TEST_SCRIPT -Stage repository -NonInteractive -Json -InstallDir $env:YT_TEST_INSTALL_DIR -Commit $env:YT_TEST_SHA -ReleaseBaseUrl $env:YT_TEST_BASE
"""
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert len(frames) == 1, result.stdout + result.stderr
    return result, frames[0], install_dir


def test_pinned_artifact_stages_without_mutating_current(tmp_path: Path) -> None:
    result, frame, current = _run_stage(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert frame["ok"] is True
    data = frame["data"]
    assert data["source_sha"] == SHA
    assert data["release_base_url"] == BASE
    assert data["artifact_sha256"] == hashlib.sha256((tmp_path / "runtime.zip").read_bytes()).hexdigest()
    stage = Path(data["staged_install_dir"])
    assert stage.parent == current.parent
    assert stage.name.startswith(current.name + ".new-")
    assert (stage / "scripts" / "install.ps1").read_bytes() == FILES["scripts/install.ps1"]
    assert (current / "existing.txt").read_text(encoding="utf-8") == "unchanged"
    assert not (stage / ".git").exists()


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"manifest_change": {"source_sha": "c" * 40}}, "manifest"),
        ({"manifest_change": {"sha256": "0" * 64}}, "SHA256"),
        ({"manifest_change": {"artifact_url": "https://github.com/evil.zip"}}, "manifest"),
        ({"manifest_change": {"install_script_sha256": "0" * 64}}, "install script"),
        ({"manifest_change": {"architecture": "arm64"}}, "manifest"),
        ({"missing_manifest": True}, "404"),
        ({"entry": "../outside.txt"}, "unsafe path"),
        ({"entry": "link", "symlink": True}, "symbolic link"),
        ({"missing_file": "pyproject.toml"}, "missing required"),
        ({"missing_file": "scripts/install.ps1"}, "missing required"),
        ({"malformed_archive": True}, "archive"),
        ({"base": "https://github.com/example/releases"}, "Untrusted release"),
    ],
)
def test_bad_artifact_fails_closed_without_git_or_current_mutation(
    tmp_path: Path, kwargs: dict[str, object], reason: str
) -> None:
    result, frame, current = _run_stage(tmp_path, **kwargs)
    assert result.returncode != 0
    assert frame["ok"] is False
    assert reason.lower() in str(frame["reason"]).lower()
    assert (current / "existing.txt").read_text(encoding="utf-8") == "unchanged"
    assert not list(tmp_path.glob("install.new-*"))


@pytest.mark.parametrize("customer_mode", [True, False])
def test_bootstrap_marker_waits_for_setup_health_only_in_customer_mode(
    tmp_path: Path, customer_mode: bool
) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = os.environ.copy()
    env.update(YT_TEST_SCRIPT=str(SCRIPT), YT_TEST_INSTALL_DIR=str(install_dir), YT_TEST_SHA=SHA, YT_TEST_BASE=BASE)
    release_args = "-ReleaseBaseUrl $env:YT_TEST_BASE" if customer_mode else ""
    command = (
        "& $env:YT_TEST_SCRIPT -Stage bootstrap-marker -NonInteractive -Json "
        "-InstallDir $env:YT_TEST_INSTALL_DIR -Commit $env:YT_TEST_SHA " + release_args
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert len(frames) == 1
    assert frames[0]["ok"] is True
    assert frames[0]["skipped"] is customer_mode
    marker = install_dir / ".youtab-agent-runtime-bootstrap-complete"
    assert marker.exists() is not customer_mode
    if not customer_mode:
        assert json.loads(marker.read_text(encoding="utf-8"))["pinnedCommit"] == SHA


def test_customer_artifact_git_stage_is_skipped(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(YT_TEST_SCRIPT=str(SCRIPT), YT_TEST_INSTALL_DIR=str(tmp_path / "install"),
               YT_TEST_SHA=SHA, YT_TEST_BASE=BASE)
    command = (
        "& $env:YT_TEST_SCRIPT -Stage git -NonInteractive -Json "
        "-InstallDir $env:YT_TEST_INSTALL_DIR -Commit $env:YT_TEST_SHA "
        "-ReleaseBaseUrl $env:YT_TEST_BASE"
    )
    result = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert len(frames) == 1
    assert frames[0]["ok"] is True
    assert frames[0]["skipped"] is True
    assert "not required" in str(frames[0]["reason"])
