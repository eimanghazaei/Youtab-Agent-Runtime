"""Strict-tier (``require_secure_perms=True``) tests for the hardened secret
loader used by provider API keys and live-benchmark credentials (WAVE-30B §6).

The strict tier adds, before any bytes are returned: an ``O_NOFOLLOW`` read with
owner==euid and mode-no-broader-than-0400 verification (POSIX); a protected
owner-only DACL check (Windows, fail-closed if pywin32 is unavailable); UTF-8 BOM
rejection; absolute/no-``..`` traversal rejection; and an opt-in repo/cloud-sync
location refusal for local secret files.

Platform capabilities are expressed as narrow capability guards (per WAVE-23
_wincompat convention), never blanket ``skipif(win32)``; each POSIX perm check has
a paired Windows DACL check so both production paths are proven.
"""

from __future__ import annotations

import os

import pytest

from youtab_agent_cli.secret_file import (
    SecretFileError,
    env_or_file,
    read_secret_file,
)

from tests._wincompat import requires_posix_permissions


def _requires_pywin32():
    from youtab_agent_cli import windows_acl

    return pytest.mark.skipif(
        not (os.name == "nt" and windows_acl.pywin32_available()),
        reason="Windows owner-only DACL requires pywin32 on a Windows host",
    )


requires_windows_dacl = _requires_pywin32()

_VAR = "OPENAI_API_KEY"
_SECRET = "sk-strict-tier-sentinel-6d8033ffa1b2c3d4e5f6"


def _write_mode(tmp_path, value=_SECRET, *, mode=0o400, trailing="\n"):
    p = tmp_path / "provider.key"
    p.write_bytes((value + trailing).encode("utf-8"))
    if os.name == "posix":
        os.chmod(p, mode)
    return str(p)


# --- traversal / path shape (platform-independent) --------------------------


def test_strict_rejects_relative_path():
    with pytest.raises(SecretFileError, match="not an absolute path"):
        read_secret_file("relative/key", var=_VAR, require_secure_perms=True)


def test_strict_rejects_dotdot_segment(tmp_path):
    sneaky = os.path.join(str(tmp_path), "..", "key")
    with pytest.raises(SecretFileError, match=r"'\.\.' path segment"):
        read_secret_file(sneaky, var=_VAR, require_secure_perms=True)


def test_strict_rejects_bom(tmp_path):
    p = tmp_path / "bom.key"
    p.write_bytes(b"\xef\xbb\xbf" + _SECRET.encode("utf-8") + b"\n")
    if os.name == "posix":
        os.chmod(p, 0o400)
    # On Windows without a protected DACL this would fail the DACL check first;
    # apply an owner-only DACL there so the BOM branch is what trips.
    if os.name == "nt":
        from youtab_agent_cli import windows_acl

        if not windows_acl.pywin32_available():
            pytest.skip("needs pywin32 to isolate the BOM branch on Windows")
        windows_acl.apply_owner_only_dacl(str(p))
    with pytest.raises(SecretFileError, match="BOM"):
        read_secret_file(str(p), var=_VAR, require_secure_perms=True)


def test_boundary_tier_tolerates_bom(tmp_path):
    """The boundary tier is unchanged: it does not reject a BOM."""
    p = tmp_path / "bom2.key"
    p.write_bytes(b"\xef\xbb\xbf" + _SECRET.encode("utf-8") + b"\n")
    if os.name == "posix":
        os.chmod(p, 0o400)
    out = read_secret_file(str(p), var=_VAR)  # boundary default
    assert out.endswith(_SECRET)


# --- repo / cloud-sync location refusal (opt-in) ----------------------------


def test_strict_rejects_git_working_tree(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    key = repo / "provider.key"
    key.write_bytes((_SECRET + "\n").encode("utf-8"))
    if os.name == "posix":
        os.chmod(key, 0o400)
    elif os.name == "nt":
        from youtab_agent_cli import windows_acl

        if windows_acl.pywin32_available():
            windows_acl.apply_owner_only_dacl(str(key))
    with pytest.raises(SecretFileError, match="git working tree"):
        read_secret_file(
            str(key), var=_VAR, require_secure_perms=True, forbid_repo_and_cloud=True
        )


def test_strict_rejects_cloud_sync_folder(tmp_path):
    cloud = tmp_path / "OneDrive" / "secrets"
    cloud.mkdir(parents=True)
    key = cloud / "provider.key"
    key.write_bytes((_SECRET + "\n").encode("utf-8"))
    if os.name == "posix":
        os.chmod(key, 0o400)
    elif os.name == "nt":
        from youtab_agent_cli import windows_acl

        if windows_acl.pywin32_available():
            windows_acl.apply_owner_only_dacl(str(key))
    with pytest.raises(SecretFileError, match="cloud-synced folder"):
        read_secret_file(
            str(key), var=_VAR, require_secure_perms=True, forbid_repo_and_cloud=True
        )


# --- POSIX owner + mode verification ----------------------------------------


@requires_posix_permissions
def test_strict_posix_accepts_owner_only_0400(tmp_path):
    path = _write_mode(tmp_path, mode=0o400)
    assert read_secret_file(path, var=_VAR, require_secure_perms=True) == _SECRET


@requires_posix_permissions
@pytest.mark.parametrize("mode", [0o600, 0o440, 0o444, 0o404, 0o410, 0o500])
def test_strict_posix_rejects_broader_than_0400(tmp_path, mode):
    path = _write_mode(tmp_path, mode=mode)
    with pytest.raises(SecretFileError, match="broader than 0o400"):
        read_secret_file(path, var=_VAR, require_secure_perms=True)


@requires_posix_permissions
def test_strict_posix_rejects_wrong_owner(tmp_path):
    """Owner mismatch is exercised deterministically without root by passing an
    ``allowed_uids`` set that excludes the file's real owner."""
    path = _write_mode(tmp_path, mode=0o400)
    with pytest.raises(SecretFileError, match="not owned by the runtime account"):
        read_secret_file(
            path,
            var=_VAR,
            require_secure_perms=True,
            allowed_uids={os.geteuid() + 4242},
        )


@requires_posix_permissions
def test_strict_posix_group_writable_rejected(tmp_path):
    path = _write_mode(tmp_path, mode=0o420)
    with pytest.raises(SecretFileError, match="writable|broader"):
        read_secret_file(path, var=_VAR, require_secure_perms=True)


# --- Windows owner-only DACL verification -----------------------------------


@requires_windows_dacl
def test_strict_windows_accepts_owner_only_dacl(tmp_path):
    from youtab_agent_cli import windows_acl

    path = str(tmp_path / "provider.key")
    windows_acl.secure_write_secret_file(path, (_SECRET + "\n").encode("utf-8"))
    assert read_secret_file(path, var=_VAR, require_secure_perms=True) == _SECRET


@requires_windows_dacl
def test_strict_windows_rejects_broad_dacl(tmp_path):
    """A normally-created file (inherited/broad DACL) is refused by the strict
    tier on Windows."""
    path = tmp_path / "loose.key"
    path.write_bytes((_SECRET + "\n").encode("utf-8"))
    with pytest.raises(SecretFileError, match="owner-only|DACL"):
        read_secret_file(str(path), var=_VAR, require_secure_perms=True)


def test_strict_windows_fail_closed_without_pywin32(tmp_path, monkeypatch):
    """On Windows, if pywin32 cannot verify the DACL the read is refused."""
    if os.name != "nt":
        pytest.skip("Windows-only fail-closed path")
    from youtab_agent_cli import windows_acl

    path = tmp_path / "k.key"
    path.write_bytes((_SECRET + "\n").encode("utf-8"))
    monkeypatch.setattr(windows_acl, "pywin32_available", lambda: False)
    with pytest.raises(SecretFileError, match="pywin32 unavailable"):
        read_secret_file(str(path), var=_VAR, require_secure_perms=True)


# --- env_or_file strict propagation + no leakage ----------------------------


@requires_posix_permissions
def test_env_or_file_strict_reads_provider_key_from_file(tmp_path, monkeypatch):
    path = _write_mode(tmp_path, mode=0o400)
    monkeypatch.delenv(_VAR, raising=False)
    monkeypatch.setenv(f"{_VAR}_FILE", path)
    got = env_or_file(_VAR, require_secure_perms=True)
    assert got == _SECRET
    # value never lands in the environment; only the path is present
    assert _VAR not in os.environ
    assert all(_SECRET not in v for v in os.environ.values())


def test_env_or_file_strict_dual_source_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(_VAR, "inline-key")
    monkeypatch.setenv(f"{_VAR}_FILE", str(tmp_path / "x"))
    with pytest.raises(SecretFileError, match="ambiguous secret source"):
        env_or_file(_VAR, require_secure_perms=True)
