"""Local static Runtime release package contract tests (no network)."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import release


BASE = "https://api.youtab.io/pilot-runtime-" + "a" * 32 + "/releases"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def committed_source(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in {
        "scripts/install.ps1": "Write-Host Youtab\n",
        "pyproject.toml": "[project]\nname = 'youtab-agent-runtime'\n",
        "apps/desktop/package.json": '{"name":"youtab"}\n',
        "agent/runtime.py": "print('runtime')\n",
        "tests/ignored.py": "test-only\n",
        ".github/workflows/ignored.yml": "dev-only\n",
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "source")
    monkeypatch.setattr(release, "REPO_ROOT", repo)
    return repo


def test_static_release_is_sha_bound_and_immutable(committed_source, tmp_path):
    out = tmp_path / "static-releases"
    manifest = release.package_runtime_artifact(out, "0.19.1", 1, BASE)
    sha = manifest["source_sha"]
    immutable = out / sha
    archive = immutable / f"youtab-runtime-{sha}.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["sha256"]
    assert (immutable / f"install-{sha}.ps1").read_bytes() == b"Write-Host Youtab\n"
    assert manifest["install_script_sha256"] == hashlib.sha256(b"Write-Host Youtab\n").hexdigest()
    assert json.loads((immutable / "manifest.json").read_text()) == manifest
    latest = json.loads((out / "latest.json").read_text())
    assert latest["manifest_url"] == f"{BASE}/{sha}/manifest.json"
    assert latest["release_sequence"] == 1
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
    assert f"youtab-runtime-{sha}/agent/runtime.py" in names
    assert not any("tests/" in name or ".github/" in name for name in names)

    before = (out / "latest.json").read_bytes()
    with pytest.raises(FileExistsError):
        release.package_runtime_artifact(out, "0.19.2", 2, BASE)
    assert (out / "latest.json").read_bytes() == before


def test_dirty_checkout_and_secret_file_fail_closed(committed_source, tmp_path):
    out = tmp_path / "releases"
    (committed_source / "agent/runtime.py").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="clean checkout"):
        release.package_runtime_artifact(out, "0.19.1", 1, BASE)
    assert not (out / "latest.json").exists()

    _git(committed_source, "restore", "agent/runtime.py")
    (committed_source / "agent/private.pem").write_text("secret", encoding="utf-8")
    _git(committed_source, "add", "agent/private.pem")
    _git(committed_source, "commit", "-m", "bad source")
    with pytest.raises(RuntimeError, match="credential-like file"):
        release.package_runtime_artifact(out, "0.19.1", 1, BASE)
    assert not (out / "latest.json").exists()


def test_private_key_material_after_first_megabyte_is_rejected(committed_source, tmp_path):
    payload = committed_source / "agent" / "large.txt"
    payload.write_bytes(b"x" * (1024 * 1024 + 3) + b"-----BEGIN PRIVATE KEY-----")
    _git(committed_source, "add", "agent/large.txt")
    _git(committed_source, "commit", "-m", "late secret")
    out = tmp_path / "releases"
    with pytest.raises(RuntimeError, match="private-key material"):
        release.package_runtime_artifact(out, "0.19.1", 1, BASE)
    assert not (out / "latest.json").exists()

def test_release_url_and_sequence_are_strict(committed_source, tmp_path):
    out = tmp_path / "releases"
    for invalid in ("http://api.youtab.io/pilot-runtime-" + "a" * 32 + "/releases",
                    "https://evil.example/pilot-runtime-" + "a" * 32 + "/releases",
                    "https://api.youtab.io/release"):
        with pytest.raises(ValueError):
            release.package_runtime_artifact(out, "0.19.1", 1, invalid)
    with pytest.raises(ValueError, match="positive"):
        release.package_runtime_artifact(out, "0.19.1", 0, BASE)


def test_existing_latest_cannot_be_downgraded(committed_source, tmp_path):
    out = tmp_path / "releases"
    out.mkdir()
    latest = out / "latest.json"
    latest.write_text('{"release_sequence": 5}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="must increase"):
        release.package_runtime_artifact(out, "0.19.1", 4, BASE)
    assert latest.read_text(encoding="utf-8") == '{"release_sequence": 5}\n'


def test_real_checkout_filter_preserves_workspace_build_inputs():
    names = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=release.REPO_ROOT,
        text=True, capture_output=True, check=True,
    ).stdout.splitlines()
    included = {name for name in names if release._runtime_artifact_path_allowed(name)}
    required = {
        "package.json", "package-lock.json", "pyproject.toml", "uv.lock",
        "scripts/install.ps1", "apps/bootstrap-installer/package.json",
        "apps/desktop/package.json", "apps/desktop/vite.config.ts",
        "apps/desktop/scripts/write-build-stamp.mjs", "apps/desktop/electron/main.ts",
        "apps/shared/package.json", "ui-tui/package.json", "web/package.json",
        "tests-js/package.json", "packages/youtab-ui-desktop/package.json",
    }
    assert required <= included
    assert any(name.startswith("apps/desktop/src/") for name in included)
    assert any(name.startswith("packages/youtab-ui-desktop/") for name in included)
    assert "apps/bootstrap-installer/src-tauri/src/bootstrap.rs" not in included
    assert "tests-js/assistant-ui-tap-compat.test.ts" not in included
    assert "scripts/release.py" not in included
    assert not any(name.endswith(".pem") or "/e2e/" in name for name in included)
