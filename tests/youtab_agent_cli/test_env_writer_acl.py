"""WAVE-28 §6.1 — the ``.env`` writers are secure from the first byte.

``config.save_env_value`` / ``remove_env_value`` / ``sanitize_env_file`` used to
write the secret-bearing ``.env`` body into a ``tempfile.mkstemp`` temp (created
under the parent's broad inherited DACL on Windows / umask on POSIX) and only
tighten *after* the atomic replace. On a multi-user Windows host another user
could read that temp during the pre-tighten window.

All three now route through ``config._write_env_lines_secure`` →
``windows_acl.secure_write_secret_file``, which creates the temp EMPTY, applies
and *verifies* an owner+SYSTEM-only protected DACL (Windows) / ``O_EXCL`` at
``0o600`` (POSIX) BEFORE any secret byte, and is fail-closed.

Coverage, paired so neither side hides a regression:
  * native Windows + pywin32 (capability-guarded, runs on this host): the real
    DACL is applied and ``verify_owner_only_dacl`` agrees after each writer;
  * portable (any OS): the writer routes through the born-owner-only helper and
    NOT ``tempfile.mkstemp``; a secure-write failure is fail-closed (raises, no
    ``.env`` written, no orphan temp, a pre-existing ``.env`` left byte-identical);
  * POSIX: the final ``.env`` is ``0o600`` from a fresh write, and a pre-existing
    Docker-volume mode (``0o640``) is preserved on the deployed file.

These are WRITE-side contracts. Each native/POSIX assertion FAILS if the
born-owner-only tightening is reverted (verified by construction: a mkstemp-only
path leaves the file under the parent's inherited DACL / umask, which
``verify_owner_only_dacl`` rejects and the 0o600 assertion catches).
"""

from __future__ import annotations

import importlib.util
import os
import stat

import pytest

from youtab_agent_cli import config as config_mod
from youtab_agent_cli import windows_acl


_PYWIN32_MODULES = ("win32security", "win32api", "win32con", "ntsecuritycon")
_HAVE_WINDOWS_ACL = os.name == "nt" and all(
    importlib.util.find_spec(m) is not None for m in _PYWIN32_MODULES
)

# Capability guard (native Windows + pywin32), never a blanket skipif(win32).
# Paired below with portable fail-closed tests and a POSIX 0o600 test.
windows_acl_native = pytest.mark.skipif(
    not _HAVE_WINDOWS_ACL,
    reason="native Windows + pywin32 required to apply/verify owner-only DACLs; "
    "portable fail-closed + POSIX 0o600 contracts are covered by the paired tests",
)
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX mode-bit semantics; Windows DACL covered above"
)


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    """Point the youtab home (and thus .env) at an isolated tmp dir."""
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("YOUTAB_AGENT_ENV", raising=False)
    config_mod.invalidate_env_cache()
    yield tmp_path
    # Don't leak the test key into the real process environment.
    for k in ("WAVE28_ENV_SECRET", "WAVE28_ENV_OTHER"):
        os.environ.pop(k, None)
    config_mod.invalidate_env_cache()


# ==========================================================================
# native Windows: each writer lands an owner-only, protected DACL
# ==========================================================================


@windows_acl_native
def test_native_save_env_value_owner_only_dacl(env_home):
    config_mod.save_env_value("WAVE28_ENV_SECRET", "sk-super-secret-value")
    env = config_mod.get_env_path()
    assert env.exists()
    assert windows_acl.verify_owner_only_dacl(env) is True
    assert "WAVE28_ENV_SECRET=sk-super-secret-value" in env.read_text(encoding="utf-8")


@windows_acl_native
def test_native_remove_env_value_keeps_owner_only_dacl(env_home):
    config_mod.save_env_value("WAVE28_ENV_SECRET", "sk-a")
    config_mod.save_env_value("WAVE28_ENV_OTHER", "sk-b")
    assert config_mod.remove_env_value("WAVE28_ENV_SECRET") is True
    env = config_mod.get_env_path()
    assert windows_acl.verify_owner_only_dacl(env) is True
    body = env.read_text(encoding="utf-8")
    assert "WAVE28_ENV_SECRET" not in body
    assert "WAVE28_ENV_OTHER=sk-b" in body


@windows_acl_native
def test_native_sanitize_env_file_keeps_owner_only_dacl(env_home):
    env = config_mod.get_env_path()
    # A file needing sanitization (trailing whitespace / blank lines) that also
    # holds a secret. Write it broadly first, then sanitize.
    env.write_text("WAVE28_ENV_SECRET=sk-secret   \n\n\n", encoding="utf-8")
    config_mod.sanitize_env_file()
    assert windows_acl.verify_owner_only_dacl(env) is True
    assert "WAVE28_ENV_SECRET=sk-secret" in env.read_text(encoding="utf-8")


# ==========================================================================
# portable: born-owner-only path is used, mkstemp is not, and failure is
# fail-closed (runs on every OS)
# ==========================================================================


def test_save_routes_through_secure_helper_not_mkstemp(env_home, monkeypatch):
    """The secret is written via secure_write_secret_file, never mkstemp."""
    calls = {"secure": [], "mkstemp": 0}
    real_secure = windows_acl.secure_write_secret_file

    def _spy_secure(path, data, **kw):
        calls["secure"].append(str(path))
        return real_secure(path, data, **kw)

    def _boom_mkstemp(*a, **k):  # any mkstemp use for the secret is a regression
        calls["mkstemp"] += 1
        raise AssertionError("save_env_value must not use tempfile.mkstemp")

    monkeypatch.setattr(windows_acl, "secure_write_secret_file", _spy_secure)
    monkeypatch.setattr(config_mod.tempfile, "mkstemp", _boom_mkstemp)

    config_mod.save_env_value("WAVE28_ENV_SECRET", "sk-value")

    env = config_mod.get_env_path()
    assert calls["mkstemp"] == 0
    assert calls["secure"], "secure_write_secret_file was not used"
    # The born-owner-only write targeted a temp in the .env's own directory.
    assert all(os.path.dirname(p) == str(env.parent) for p in calls["secure"])
    assert "WAVE28_ENV_SECRET=sk-value" in env.read_text(encoding="utf-8")


def test_save_is_fail_closed_and_leaves_no_orphan_or_partial(env_home, monkeypatch):
    """A secure-write failure raises, writes no .env, leaves no orphan temp,
    and leaves any pre-existing .env byte-identical — never a permissive
    fallback."""
    env = config_mod.get_env_path()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("WAVE28_ENV_OTHER=keep-me\n", encoding="utf-8")
    before = env.read_bytes()

    def _boom(path, data, **kw):
        raise OSError("secure write refused (simulated)")

    monkeypatch.setattr(windows_acl, "secure_write_secret_file", _boom)

    with pytest.raises(OSError):
        config_mod.save_env_value("WAVE28_ENV_SECRET", "sk-should-not-land")

    # Pre-existing file untouched.
    assert env.read_bytes() == before
    # No orphan temp left behind.
    leftovers = [p for p in env.parent.iterdir() if p.name.startswith(".env_")]
    assert leftovers == [], f"orphan temp(s) left: {leftovers}"


def test_remove_is_fail_closed(env_home, monkeypatch):
    env = config_mod.get_env_path()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("WAVE28_ENV_SECRET=drop\nWAVE28_ENV_OTHER=keep\n", encoding="utf-8")
    before = env.read_bytes()

    monkeypatch.setattr(
        windows_acl, "secure_write_secret_file",
        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")),
    )
    with pytest.raises(OSError):
        config_mod.remove_env_value("WAVE28_ENV_SECRET")
    assert env.read_bytes() == before
    assert [p for p in env.parent.iterdir() if p.name.startswith(".env_")] == []


# ==========================================================================
# POSIX: final mode contracts
# ==========================================================================


@posix_only
def test_posix_new_env_is_0600(env_home):
    config_mod.save_env_value("WAVE28_ENV_SECRET", "sk-posix")
    env = config_mod.get_env_path()
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


@posix_only
def test_posix_preserves_existing_docker_mode(env_home):
    env = config_mod.get_env_path()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("WAVE28_ENV_OTHER=x\n", encoding="utf-8")
    os.chmod(env, 0o640)  # simulate a Docker volume-mount mode
    config_mod.save_env_value("WAVE28_ENV_SECRET", "sk-posix")
    mode = stat.S_IMODE(config_mod.get_env_path().stat().st_mode)
    assert mode == 0o640, f"Docker mode not preserved, got {oct(mode)}"
