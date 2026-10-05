import hashlib
import json

import pytest

from scripts.release_channels import digest, promote


@pytest.fixture
def release(tmp_path):
    sha = "a" * 40
    base = "https://api.youtab.io/pilot-runtime-" + "b" * 32 + "/releases"
    directory = tmp_path / sha
    directory.mkdir()
    manifest = dict(source_sha=sha, release_sequence=4, version="0.19.1", platform="windows", architecture="x64", format="zip", setup_source_sha=sha, updater_protocol=1)
    for name, url, checksum, payload in (
        (f"youtab-runtime-{sha}.zip", "artifact_url", "sha256", b"synthetic runtime"),
        (f"install-{sha}.ps1", "install_script_url", "install_script_sha256", b"synthetic script"),
        (f"Youtab-Setup-{sha}.exe", "setup_url", "setup_sha256", b"synthetic setup"),
    ):
        (directory / name).write_bytes(payload)
        manifest[url] = f"{base}/{sha}/{name}"
        manifest[checksum] = hashlib.sha256(payload).hexdigest()
    manifest["setup_size"] = len(b"synthetic setup")
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path, sha, base, manifest


def qualification(manifest):
    return {**{k: manifest[k] for k in ("source_sha", "sha256", "setup_sha256", "release_sequence")}, **dict.fromkeys(("fresh_install", "update", "login_inference", "restart", "preservation", "files_skills_plugins", "owner_approved"), True)}


def test_promote_same_bytes_to_stable_without_rebuild(release):
    root, sha, base, manifest = release
    original = {p.name: p.read_bytes() for p in (root / sha).iterdir()}
    pilot = promote(root, "pilot", sha, base, None)
    stable = promote(root, "stable", sha, base, None, qualification(manifest))
    assert pilot == stable
    assert original == {p.name: p.read_bytes() for p in (root / sha).iterdir()}
    assert not (root / "latest.json").exists()


@pytest.mark.parametrize("case", ["tampered_setup", "missing_setup", "wrong_qualification", "no_qualification", "unqualified_update", "reused_sequence", "changed_pointer", "bad_channel"])
def test_fail_closed_preserves_existing_pointer(release, case):
    root, sha, base, manifest = release
    promote(root, "pilot", sha, base, None)
    q = qualification(manifest)
    if case == "tampered_setup":
        (root / sha / f"Youtab-Setup-{sha}.exe").write_bytes(b"tampered")
    elif case == "missing_setup":
        (root / sha / f"Youtab-Setup-{sha}.exe").unlink()
    elif case == "wrong_qualification":
        q["setup_sha256"] = "c" * 64
    elif case == "no_qualification":
        q = None
    elif case == "unqualified_update":
        q["update"] = False
    channel = "stable"
    expected = None
    if case in ("reused_sequence", "changed_pointer"):
        promote(root, "stable", sha, base, None, q)
        expected = digest(root / "channels/stable/latest.json")
        if case == "changed_pointer":
            expected = "d" * 64
    if case == "bad_channel":
        channel = "../../other"
    pointer = root / "channels/stable/latest.json"
    before = pointer.read_bytes() if pointer.exists() else None
    with pytest.raises((ValueError, FileNotFoundError)):
        promote(root, channel, sha, base, expected, q)
    assert (pointer.read_bytes() if pointer.exists() else None) == before
