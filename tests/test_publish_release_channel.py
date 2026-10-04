import hashlib
import json

import pytest

from scripts.publish_release_channel import install_immutable, publication_lock, publish
from scripts.release_channels import digest


@pytest.fixture
def candidate(tmp_path):
    source = tmp_path / "input"
    root = tmp_path / "public"
    source.mkdir()
    root.mkdir()
    sha = "a" * 40
    base = "https://api.youtab.io/pilot-runtime-" + "b" * 32 + "/releases"
    directory = source / sha
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
    legacy = json.dumps({"source_sha": "c" * 40, "release_sequence": 3}).encode()
    (root / "latest.json").write_bytes(legacy)
    qualification = {**{k: manifest[k] for k in ("source_sha", "sha256", "setup_sha256", "release_sequence")}, **dict.fromkeys(("fresh_install", "update", "login_inference", "restart", "preservation", "files_skills_plugins", "owner_approved"), True)}

    def verify(url, expected, mutable):
        file = root / url.removeprefix(base + "/")
        assert digest(file) == expected
        assert mutable == file.name.startswith("latest")

    return root, source, sha, base, qualification, verify, legacy


def test_pilot_then_stable_publish_identical_bytes_and_legacy_bridge(candidate):
    root, source, sha, base, qualification, verify, legacy = candidate
    publish(root, source, sha, base, "pilot", None, None, None, verify)
    assert (root / "latest.json").read_bytes() == legacy
    original = {p.name: p.read_bytes() for p in (root / sha).iterdir()}
    publish(root, source, sha, base, "stable", None, digest(root / "latest.json"), qualification, verify)
    assert (root / "latest.json").read_bytes() == (root / "channels/stable/latest.json").read_bytes()
    assert (root / "channels/pilot/latest.json").read_bytes() == (root / "latest.json").read_bytes()
    assert original == {p.name: p.read_bytes() for p in (root / sha).iterdir()}


@pytest.mark.parametrize("failure", ["channel_readback", "legacy_readback", "unqualified", "changed_legacy"])
def test_failed_stable_readback_or_qualification_preserves_discovery(candidate, failure):
    root, source, sha, base, qualification, verify, legacy = candidate
    publish(root, source, sha, base, "pilot", None, None, None, verify)
    expected = digest(root / "latest.json")
    if failure == "unqualified":
        qualification["update"] = False
    if failure == "changed_legacy":
        expected = "d" * 64

    def failed_verify(url, expected_hash, mutable):
        verify(url, expected_hash, mutable)
        if mutable and (failure == "channel_readback" or failure == "legacy_readback" and url == base + "/latest.json"):
            raise ValueError("unqualified cache policy")

    with pytest.raises(ValueError):
        publish(root, source, sha, base, "stable", None, expected, qualification, failed_verify)
    assert (root / "latest.json").read_bytes() == legacy
    assert not (root / "channels/stable/latest.json").exists()
    assert (root / sha / "manifest.json").is_file()


def test_immutable_conflicts_are_never_overwritten(candidate):
    root, source, sha, base, *_ = candidate
    install_immutable(source, root, sha, base)
    setup = root / sha / f"Youtab-Setup-{sha}.exe"
    setup.write_bytes(b"existing unexpected bytes")
    with pytest.raises(ValueError):
        install_immutable(source, root, sha, base)
    assert setup.read_bytes() == b"existing unexpected bytes"


def test_publication_lock_excludes_concurrent_promotions(candidate):
    root, *_ = candidate
    with publication_lock(root):
        with pytest.raises(OSError):
            with publication_lock(root):
                pytest.fail("second publisher acquired lock")


def test_post_rename_flush_failure_attempts_both_pointer_restores(candidate, monkeypatch):
    import scripts.publish_release_channel as publisher
    import scripts.release_channels as channels

    root, source, sha, base, qualification, verify, legacy = candidate
    publish(root, source, sha, base, "pilot", None, None, None, verify)
    expected = digest(root / "latest.json")
    normal_replace = publisher._replace
    writes = []

    def replace_then_fail(pointer, content):
        normal_replace(pointer, content)
        writes.append(pointer)
        if pointer == root / "latest.json":
            raise OSError("synthetic post-rename flush failure")

    monkeypatch.setattr(publisher, "_replace", replace_then_fail)
    with pytest.raises(OSError):
        publish(root, source, sha, base, "stable", None, expected, qualification, verify)
    assert (root / "latest.json").read_bytes() == legacy
    assert not (root / "channels/stable/latest.json").exists()
    assert writes.count(root / "latest.json") == 2

    normal_promote = channels.promote
    monkeypatch.setattr(publisher, "_replace", normal_replace)

    def promote_then_fail(*args, **kwargs):
        normal_promote(*args, **kwargs)
        raise OSError("synthetic post-rename channel flush failure")

    monkeypatch.setattr(publisher, "promote", promote_then_fail)
    with pytest.raises(OSError):
        publish(root, source, sha, base, "stable", None, expected, qualification, verify)
    assert not (root / "channels/stable/latest.json").exists()
