import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys

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


@pytest.mark.skipif(os.name != "posix", reason="POSIX private access contract")
def test_offline_release_bytes_and_discovery_remain_private(candidate):
    root, source, sha, base, qualification, verify, _legacy = candidate
    publish(root, source, sha, base, "pilot", None, None, None, verify)
    publish(root, source, sha, base, "stable", None, digest(root / "latest.json"), qualification, verify)
    for path in list((root / sha).iterdir()) + [root / "latest.json", root / "channels/pilot/latest.json", root / "channels/stable/latest.json"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for path in [root / sha, root / "channels", root / "channels/pilot", root / "channels/stable"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    # The publication lock remains private even when downloads are shared.
    assert stat.S_IMODE((root / ".publication.lock").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setfacl"),
                    reason="Requires isolated Linux root and setfacl to test named web-worker ACLs")
def test_unprivileged_web_worker_can_download_but_cannot_mutate(candidate):
    root, source, sha, base, *_rest = candidate
    web_gid, web_uid, unrelated_uid = 40002, 40003, 40004
    # Prove publication replaces unrelated/inherited ACLs instead of accumulating them.
    subprocess.run(["setfacl", "-m", f"u:{unrelated_uid}:rwx,d:u:{unrelated_uid}:rwx", str(root)], check=True)
    publish(root, source, sha, base, "pilot", None, None, None, candidate[5], web_uid=web_uid)
    publish(root, source, sha, base, "stable", None, digest(root / "latest.json"), candidate[4], candidate[5], web_uid=web_uid)

    def become_web_worker():
        os.setgroups([])
        os.setgid(web_gid)
        os.setuid(web_uid)

    # cwd is entered before dropping identity, avoiding unrelated pytest parent permissions.
    result = subprocess.run([sys.executable, "-c", """
from pathlib import Path
import sys
artifact = Path(sys.argv[1])
assert artifact.read_bytes()
assert Path('channels/pilot/latest.json').read_bytes()
assert Path('channels/stable/latest.json').read_bytes()
assert Path('latest.json').read_bytes()
for path, mode in [(artifact, 'wb'), (Path('channels/pilot/latest.json'), 'wb'), (Path('.publication.lock'), 'rb')]:
    try:
        with path.open(mode):
            pass
    except PermissionError:
        continue
    raise AssertionError('web worker gained mutation or private-lock access')
for action in [lambda: artifact.chmod(0o600), lambda: artifact.rename(artifact.with_suffix('.moved')),
               lambda: Path('channels/pilot/latest.json').rename(Path('channels/pilot/moved.json'))]:
    try:
        action()
    except PermissionError:
        continue
    raise AssertionError('web worker gained chmod or rename authority')
""", f"{sha}/Youtab-Setup-{sha}.exe"], cwd=root, preexec_fn=become_web_worker,
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr

    def become_unrelated_worker():
        os.setgroups([])
        os.setgid(web_gid)
        os.setuid(unrelated_uid)

    result = subprocess.run([sys.executable, "-c", """
from pathlib import Path
import sys
for path in [Path(sys.argv[1]), Path('channels/pilot/latest.json')]:
    try:
        path.read_bytes()
    except PermissionError:
        continue
    raise AssertionError('unrelated UID gained release read access')
""", f"{sha}/Youtab-Setup-{sha}.exe"], cwd=root, preexec_fn=become_unrelated_worker,
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    for path in [root, root / sha, root / "channels", root / "channels/pilot", root / "channels/stable"]:
        acl = subprocess.run(["getfacl", "--numeric", "--omit-header", str(path)], check=True, capture_output=True, text=True).stdout
        assert f"user:{web_uid}:r-x" in acl
        assert "group::---" in acl and "other::---" in acl
        assert "default:" not in acl and f"user:{unrelated_uid}:" not in acl
        assert path.stat().st_uid == path.stat().st_gid == 0
    for path in list((root / sha).iterdir()) + [root / "channels/pilot/latest.json", root / "channels/stable/latest.json", root / "latest.json"]:
        acl = subprocess.run(["getfacl", "--numeric", "--omit-header", str(path)], check=True, capture_output=True, text=True).stdout
        assert f"user:{web_uid}:r--" in acl
        assert "group::---" in acl and "other::---" in acl
        assert f"user:{unrelated_uid}:" not in acl
        assert path.stat().st_uid == path.stat().st_gid == 0


def test_named_acl_refuses_root_or_invalid_uid_without_changing_discovery(candidate):
    root, source, sha, base, _, verify, legacy = candidate
    for uid in (0, -1, True, "40003"):
        with pytest.raises(ValueError):
            publish(root, source, sha, base, "pilot", None, None, None, verify, web_uid=uid)
        assert (root / "latest.json").read_bytes() == legacy
        assert not (root / sha).exists()


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="POSIX root capability preflight")
def test_named_acl_missing_tool_stops_before_publication(candidate, monkeypatch):
    root, source, sha, base, _, verify, legacy = candidate
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="setfacl support"):
        publish(root, source, sha, base, "pilot", None, None, None, verify, web_uid=40003)
    assert (root / "latest.json").read_bytes() == legacy
    assert not (root / "channels").exists()


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setfacl"),
                    reason="POSIX root ACL capability preflight")
def test_named_acl_filesystem_failure_stops_before_publication(candidate, monkeypatch):
    import scripts.release_channels as channels

    root, source, sha, base, _, verify, legacy = candidate
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "setfacl")
    monkeypatch.setattr(channels.subprocess, "run", unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        publish(root, source, sha, base, "pilot", None, None, None, verify, web_uid=40003)
    assert (root / "latest.json").read_bytes() == legacy
    assert not (root / "channels").exists()
    assert not list(root.glob(".acl-preflight-*"))


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
