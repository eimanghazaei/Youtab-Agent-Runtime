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

from tests._wincompat import requires_posix_permissions, requires_symlink


def _requires_pywin32():
    from youtab_agent_cli import windows_acl

    return pytest.mark.skipif(
        not (os.name == "nt" and windows_acl.pywin32_available()),
        reason="Windows owner-only DACL requires pywin32 on a Windows host",
    )


requires_windows_dacl = _requires_pywin32()

_VAR = "OPENAI_API_KEY"
# Synthetic value only; deliberately NOT an ``sk-`` shape so the secret-scan gate
# does not classify this test fixture as a real OpenAI key.
_SECRET = "SENTINEL-strict-tier-6d8033ffa1b2c3d4e5f6"


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


# --- Windows SAME-HANDLE read (WAVE-30C Fix A: no path-revalidation TOCTOU) --


@requires_windows_dacl
def test_strict_windows_reads_exact_bytes_via_handle(tmp_path):
    """The strict Windows read returns the exact secret sourced from the SAME
    verified handle (not a second ``open(path)``)."""
    from youtab_agent_cli import windows_acl

    path = str(tmp_path / "provider.key")
    windows_acl.secure_write_secret_file(path, (_SECRET + "\n").encode("utf-8"))
    assert read_secret_file(path, var=_VAR, require_secure_perms=True) == _SECRET
    # The low-level handle reader returns the raw bytes (incl. the newline the
    # decoder later trims) — proving the bytes come from the verified handle.
    raw = windows_acl.read_secret_bytes_owner_only(path, max_bytes=64 * 1024)
    assert raw == (_SECRET + "\n").encode("utf-8")


@requires_windows_dacl
@requires_symlink
def test_strict_windows_handle_reader_rejects_reparse_point(tmp_path):
    """A symlink/junction (reparse point) is rejected on the OPEN handle itself,
    so the ACL that is proven can never belong to a different object than the
    bytes that would be read."""
    from youtab_agent_cli import windows_acl

    real = str(tmp_path / "real.key")
    windows_acl.secure_write_secret_file(real, (_SECRET + "\n").encode("utf-8"))
    link = str(tmp_path / "link.key")
    os.symlink(real, link)
    with pytest.raises(OSError, match="reparse point|different path"):
        windows_acl.read_secret_bytes_owner_only(link, max_bytes=64 * 1024)


@requires_windows_dacl
def test_strict_windows_handle_reader_reparse_bit_fails_closed(tmp_path, monkeypatch):
    """Paired fail-closed test (no symlink privilege needed): if the open handle
    reports FILE_ATTRIBUTE_REPARSE_POINT, the read is refused before the DACL
    check or any byte read — proving the reparse guard, not just symlink creation."""
    from youtab_agent_cli import windows_acl

    path = str(tmp_path / "provider.key")
    windows_acl.secure_write_secret_file(path, (_SECRET + "\n").encode("utf-8"))

    import win32file

    real_info = win32file.GetFileInformationByHandle

    def _reparse(handle):
        info = list(real_info(handle))
        info[0] = info[0] | 0x400  # force FILE_ATTRIBUTE_REPARSE_POINT
        return tuple(info)

    monkeypatch.setattr(win32file, "GetFileInformationByHandle", _reparse)
    with pytest.raises(OSError, match="reparse point"):
        windows_acl.read_secret_bytes_owner_only(path, max_bytes=64 * 1024)


@requires_windows_dacl
def test_strict_windows_read_does_not_consult_path_based_check(tmp_path, monkeypatch):
    """TOCTOU proof: even if the PATH-based ``verify_owner_only_dacl`` is forced
    to return True, a broad-DACL file is STILL refused — because the strict read
    now validates the DACL on the same handle it reads from, never by path."""
    from youtab_agent_cli import windows_acl

    # Force the old path-based check to "pass" for everything.
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: True)

    loose = tmp_path / "loose.key"
    loose.write_bytes((_SECRET + "\n").encode("utf-8"))  # inherited/broad DACL
    with pytest.raises(SecretFileError, match="owner-only|DACL|securely read"):
        read_secret_file(str(loose), var=_VAR, require_secure_perms=True)


@requires_windows_dacl
def test_strict_windows_handle_reader_fails_closed_on_security_info_error(tmp_path, monkeypatch):
    """If handle-based ``GetSecurityInfo`` raises, the read fails closed (OSError)
    and returns no bytes — never degrades to a path-based read."""
    from youtab_agent_cli import windows_acl

    path = str(tmp_path / "provider.key")
    windows_acl.secure_write_secret_file(path, (_SECRET + "\n").encode("utf-8"))

    import win32security

    def _boom(*_a, **_k):
        raise OSError("simulated GetSecurityInfo failure")

    monkeypatch.setattr(win32security, "GetSecurityInfo", _boom)
    with pytest.raises(OSError, match="cannot read security info"):
        windows_acl.read_secret_bytes_owner_only(path, max_bytes=64 * 1024)


@requires_windows_dacl
def test_strict_windows_errors_never_contain_secret(tmp_path):
    """A refusal on a broad-DACL file must not disclose the secret value."""
    loose = tmp_path / "loose.key"
    loose.write_bytes((_SECRET + "\n").encode("utf-8"))
    with pytest.raises(SecretFileError) as excinfo:
        read_secret_file(str(loose), var=_VAR, require_secure_perms=True)
    assert _SECRET not in str(excinfo.value)


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
