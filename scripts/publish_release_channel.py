"""Common release publication. Invocation on the VPS needs exact Owner approval.

Pilot never moves legacy latest.json. Stable promotes the same approved bytes
and advances legacy discovery so old clients can reach the common installer.
Keep the publication lock through installation, verification and both pointers.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.request

from scripts.release_channels import _read, digest, promote, set_release_access, verify_release, preflight_release_access


def _replace(pointer: Path, content: bytes, *, web_uid: int | None = None) -> None:
    handle, name = tempfile.mkstemp(prefix=".latest-", suffix=".tmp", dir=pointer.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as writer:
            writer.write(content)
            writer.flush()
            os.fsync(writer.fileno())
        set_release_access(temporary, pointer.parent, web_uid=web_uid)
        os.replace(temporary, pointer)
        if os.name != "nt":
            descriptor = os.open(pointer.parent, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def publication_lock(root: Path):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("invalid publication root")
    lock = root / ".publication.lock"
    if lock.is_symlink():
        raise ValueError("unsafe publication lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as file:
        set_release_access(lock, root)
        if os.name == "nt":
            import msvcrt
            file.write(b"0")
            file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def install_immutable(source_root: Path, root: Path, sha: str, base: str,
                      *, web_uid: int | None = None) -> dict:
    """Install only the four verified members; never overwrite an immutable SHA."""
    if source_root.is_symlink() or root.is_symlink() or not root.is_dir():
        raise ValueError("unsafe release root")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid SHA")
    source = source_root / sha
    target = root / sha
    manifest = _read(source / "manifest.json")
    verify_release(source_root, manifest, base)
    members = {"manifest.json", f"youtab-runtime-{sha}.zip", f"install-{sha}.ps1", f"Youtab-Setup-{sha}.exe"}
    if source.is_symlink() or {file.name for file in source.iterdir()} != members:
        raise ValueError("invalid immutable layout")
    if target.exists() or target.is_symlink():
        verify_release(root, manifest, base)
        if target.is_symlink() or {file.name for file in target.iterdir()} != members:
            raise ValueError("immutable conflict")
        for member in members:
            set_release_access(target / member, root, web_uid=web_uid)
        set_release_access(target, root, directory=True, web_uid=web_uid)
        return manifest
    stage = Path(tempfile.mkdtemp(prefix=".publish-", dir=root))
    try:
        for member in members:
            with (source / member).open("rb") as reader, (stage / member).open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            set_release_access(stage / member, root, web_uid=web_uid)
            if digest(stage / member) != digest(source / member):
                raise ValueError("staged integrity failure")
        set_release_access(stage, root, directory=True, web_uid=web_uid)
        if os.name != "nt":
            descriptor = os.open(stage, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.rename(stage, target)
        if os.name != "nt":
            descriptor = os.open(root, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        verify_release(root, manifest, base)
    finally:
        if stage.exists():
            # Only the fresh, internally generated staging directory is removed.
            if stage.parent != root or not stage.name.startswith(".publish-") or stage.is_symlink():
                raise ValueError("unsafe cleanup")
            shutil.rmtree(stage)
    return manifest


def publish(root: Path, source_root: Path, sha: str, base: str, channel: str,
            expected: str | None, expected_legacy: str | None,
            qualification: dict | None, public_verify, *, web_uid: int | None = None) -> str:
    """Rollback discovery on a failed public readback; retain immutable artifacts."""
    access = {"web_uid": web_uid} if web_uid is not None else {}
    if web_uid is not None:
        preflight_release_access(root, web_uid)
    with publication_lock(root):
        if web_uid is not None:
            set_release_access(root, root, directory=True, web_uid=web_uid)
        pointer = root / "channels" / channel / "latest.json"
        if channel not in {"pilot", "stable"}:
            raise ValueError("invalid channel")
        before = pointer.read_bytes() if pointer.exists() and not pointer.is_symlink() else None
        if pointer.is_symlink() or (digest(pointer) if before is not None else None) != expected:
            raise ValueError("channel changed")
        legacy = root / "latest.json"
        legacy_before = None
        if channel == "stable":
            if legacy.is_symlink():
                raise ValueError("unsafe legacy index")
            legacy_before = legacy.read_bytes() if legacy.exists() else None
            if (digest(legacy) if legacy_before is not None else None) != expected_legacy:
                raise ValueError("legacy index changed")
        manifest = install_immutable(source_root, root, sha, base, **access)
        candidate = (json.dumps({**manifest, "manifest_url": f"{base}/{sha}/manifest.json"}, sort_keys=True, indent=2) + "\n").encode()
        if legacy_before is not None:
            previous = json.loads(legacy_before)
            if type(previous.get("release_sequence")) is not int or previous["release_sequence"] >= manifest["release_sequence"]:
                raise ValueError("legacy downgrade/reused sequence")
        for name in ("manifest.json", f"youtab-runtime-{sha}.zip", f"install-{sha}.ps1", f"Youtab-Setup-{sha}.exe"):
            public_verify(f"{base}/{sha}/{name}", digest(root / sha / name), False)
        try:
            result = promote(root, channel, sha, base, expected, qualification, **access)
            public_verify(f"{base}/channels/{channel}/latest.json", result, True)
            if channel == "stable":
                _replace(legacy, candidate, **access)
                public_verify(f"{base}/latest.json", hashlib.sha256(candidate).hexdigest(), True)
            return result
        except Exception:
            # Rename can succeed before fsync raises. Inspect actual pointer
            # bytes rather than assuming a helper that raised made no change.
            try:
                if channel == "stable" and legacy.exists() and not legacy.is_symlink() and digest(legacy) == hashlib.sha256(candidate).hexdigest():
                    if legacy_before is None:
                        legacy.unlink()
                    else:
                        _replace(legacy, legacy_before, **access)
            finally:
                # A failed restore/flush for one index must not suppress the
                # independent restoration attempt for the other index.
                if pointer.exists() and not pointer.is_symlink() and digest(pointer) == hashlib.sha256(candidate).hexdigest():
                    if before is None:
                        pointer.unlink()
                    else:
                        _replace(pointer, before, **access)
            raise


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_public(url: str, expected: str, mutable: bool) -> None:
    if not re.fullmatch(r"https://api\.youtab\.io/pilot-runtime-[0-9a-f]{32,}/releases/[A-Za-z0-9_./-]+", url):
        raise ValueError("untrusted download")
    with urllib.request.build_opener(NoRedirect).open(url, timeout=30) as response:
        value = hashlib.sha256()
        total = 0
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            total += len(chunk)
            if total > 256 * 1024 * 1024:
                raise ValueError("public response too large")
            value.update(chunk)
        if response.status != 200 or value.hexdigest() != expected:
            raise ValueError("public integrity failure")
        if mutable and ("no-store" not in response.headers.get("Cache-Control", "").lower()
                        or response.headers.get("CF-Cache-Status", "").upper() not in {"BYPASS", "DYNAMIC"}
                        or response.headers.get("Age") is not None):
            raise ValueError("mutable cache policy unqualified")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--manifest-sha256", required=True, help="Exact Owner-reviewed manifest hash")
    parser.add_argument("--base-file", required=True, type=Path)
    parser.add_argument("--channel", required=True, choices=("pilot", "stable"))
    parser.add_argument("--expected-index", required=True, help="SHA-256 or absent")
    parser.add_argument("--expected-legacy", default="absent")
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--web-user", required=True, help="Provisioned non-root web-server account for read-only named ACLs")
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        raise ValueError("approved VPS root execution required")
    root = Path("/srv/youtab-runtime-releases")
    if root.is_symlink() or root.stat().st_uid != 0 or root.stat().st_mode & 0o022:
        raise ValueError("untrusted publication root")
    import pwd
    web_uid = pwd.getpwnam(args.web_user).pw_uid
    if web_uid == 0:
        raise ValueError("web-server account must be non-root")
    if args.base_file.is_symlink() or not args.base_file.is_file():
        raise ValueError("invalid private input")
    manifest_file = args.source_root / args.source_sha / "manifest.json"
    if not re.fullmatch(r"[0-9a-f]{64}", args.manifest_sha256) or digest(manifest_file) != args.manifest_sha256:
        raise ValueError("Owner-reviewed input changed")
    for item in (args.source_root, args.source_root / args.source_sha, manifest_file, args.base_file):
        if item.is_symlink() or item.stat().st_uid != 0 or item.stat().st_mode & 0o022:
            raise ValueError("untrusted publication input")
    if args.qualification and (args.qualification.is_symlink() or args.qualification.stat().st_uid != 0 or args.qualification.stat().st_mode & 0o022):
        raise ValueError("untrusted qualification input")
    expected = lambda value: None if value == "absent" else value
    qualification = _read(args.qualification) if args.qualification else None
    publish(root, args.source_root, args.source_sha, args.base_file.read_text().strip(), args.channel,
            expected(args.expected_index), expected(args.expected_legacy), qualification, verify_public,
            web_uid=web_uid)
    print("PUBLICATION=PASS")
    print("CHANNEL=" + args.channel)
    print("SOURCE_SHA=" + args.source_sha)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("PUBLICATION=STOPPED_DETAILS_MASKED")
        raise SystemExit(1) from None
