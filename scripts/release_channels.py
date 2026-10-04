"""Offline channel promotion. Call only with an Owner-approved release root.

This module does not publish, contact GitHub or modify customer installations.
The caller must hold its existing publication lock for the entire operation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

CHANNELS = frozenset({"pilot", "stable"})


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("release member must be a regular file")
    value = hashlib.sha256()
    with path.open("rb") as reader:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("invalid release metadata file")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("invalid release metadata object")
    return result


def verify_release(root: Path, manifest: dict, base: str) -> None:
    """Verify common Runtime and Setup files, exact SHA and public URL binding."""
    if not re.fullmatch(r"https://api\.youtab\.io/pilot-runtime-[0-9a-f]{32,}/releases", base):
        raise ValueError("invalid release base")
    sha = manifest.get("source_sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid source SHA")
    sequence = manifest.get("release_sequence")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("invalid sequence")
    if (manifest.get("platform"), manifest.get("architecture"), manifest.get("format")) != ("windows", "x64", "zip"):
        raise ValueError("unsupported platform")
    if manifest.get("setup_source_sha") != sha or type(manifest.get("updater_protocol")) is not int or manifest.get("updater_protocol") != 1:
        raise ValueError("Setup source/protocol mismatch")
    directory = root / sha
    if root.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise ValueError("invalid immutable directory")
    for name, url_key, hash_key in (
        (f"youtab-runtime-{sha}.zip", "artifact_url", "sha256"),
        (f"install-{sha}.ps1", "install_script_url", "install_script_sha256"),
        (f"Youtab-Setup-{sha}.exe", "setup_url", "setup_sha256"),
    ):
        expected = manifest.get(hash_key)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid artifact hash")
        if manifest.get(url_key) != f"{base}/{sha}/{name}" or digest(directory / name) != expected:
            raise ValueError("artifact binding/integrity failure")
    size = manifest.get("setup_size")
    if type(size) is not int or not 0 < size <= 128 * 1024 * 1024:
        raise ValueError("invalid Setup size")
    if (directory / f"Youtab-Setup-{sha}.exe").stat().st_size != size:
        raise ValueError("Setup size mismatch")
    if _read(directory / "manifest.json") != manifest:
        raise ValueError("immutable manifest mismatch")


def promote(root: Path, channel: str, source_sha: str, base: str,
            expected_pointer_sha256: str | None, qualification: dict | None = None) -> str:
    """Atomically promote verified bytes; None means the pointer must be absent.

    Owner-reviewed qualification is an explicit input, not inferred from green CI.
    No automated qualification signing infrastructure is claimed by this helper.
    """
    if channel not in CHANNELS or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("invalid channel/source")
    manifest = _read(root / source_sha / "manifest.json")
    verify_release(root, manifest, base)
    if channel == "stable":
        pilot = _read(root / "channels" / "pilot" / "latest.json")
        if pilot != {**manifest, "manifest_url": f"{base}/{source_sha}/manifest.json"}:
            raise ValueError("Stable candidate is not the current Pilot package")
        required = {key: manifest[key] for key in ("source_sha", "sha256", "setup_sha256", "release_sequence")}
        if not isinstance(qualification, dict) or any(qualification.get(k) != v for k, v in required.items()):
            raise ValueError("qualification does not bind candidate bytes")
        for key in ("fresh_install", "update", "login_inference", "restart", "preservation", "files_skills_plugins", "owner_approved"):
            if qualification.get(key) is not True:
                raise ValueError("qualification incomplete")
    channels = root / "channels"
    directory = channels / channel
    if channels.is_symlink() or directory.is_symlink():
        raise ValueError("unsafe channel directory")
    directory.mkdir(parents=True, exist_ok=True)
    pointer = directory / "latest.json"
    if pointer.is_symlink():
        raise ValueError("unsafe channel pointer")
    exists = pointer.exists()
    if exists != (expected_pointer_sha256 is not None):
        raise ValueError("channel changed; review required")
    if exists:
        if digest(pointer) != expected_pointer_sha256:
            raise ValueError("channel changed; review required")
        previous = _read(pointer)
        if type(previous.get("release_sequence")) is not int or previous["release_sequence"] >= manifest["release_sequence"]:
            raise ValueError("downgrade/reused sequence forbidden")
    payload = (json.dumps({**manifest, "manifest_url": f"{base}/{source_sha}/manifest.json"}, sort_keys=True, indent=2) + "\n").encode()
    handle, name = tempfile.mkstemp(prefix=".latest-", suffix=".tmp", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(temporary, 0o644)
        # Detect a caller that failed to hold the required publication lock.
        if pointer.exists() != exists or (exists and digest(pointer) != expected_pointer_sha256):
            raise ValueError("concurrent channel change")
        os.replace(temporary, pointer)
        if os.name != "nt":
            descriptor = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()
