"""WAVE-27 #7b: owner-only DACL guarantees for the credential/secret WRITERS.

WAVE-26 shipped :mod:`youtab_agent_cli.windows_acl` (the born-owner-only,
fail-closed secret-file primitive) and proved the primitive itself. WAVE-27
wires the actual secret writers in ``auth.py`` / ``config.py`` / ``profiles.py``
through it so that:

* the plaintext secret (OAuth tokens, refresh tokens, ``.env`` API keys) is
  never present on disk under the parent directory's broad *inherited* DACL —
  the writers create a temp file that is *born* owner-only and atomic-replace it
  into place; and
* the DACL apply/verify is **fail-closed** — a writer that cannot prove the
  finalized file is owner-only removes it and raises, instead of the previous
  best-effort ``except OSError: pass`` that could leave a broadly-readable
  ``.env``/``config.yaml`` behind.

Two proof planes, matching the sibling modules:

* **native Windows + pywin32** (capability-guarded, runs on this host): the real
  DACL is applied and ``verify_owner_only_dacl`` agrees after each writer.
* **portable fail-closed** (runs on every platform): the Windows control flow is
  forced via monkeypatch so the residue-cleanup + raise is exercised on POSIX
  CI too; plus paired POSIX ``0o600`` behaviour checks.

This module is WRITE-side only — it never asserts a read-side rejection of
normal-user files (the WAVE-26 read-side check was reverted for breaking those).
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat

import pytest

from tests import _wincompat

from youtab_agent_cli import windows_acl
from youtab_agent_cli import auth as auth_mod
from youtab_agent_cli import config as config_mod
from youtab_agent_cli import profiles as profiles_mod


_PYWIN32_MODULES = ("win32security", "win32api", "win32con", "ntsecuritycon")
_HAVE_WINDOWS_ACL = os.name == "nt" and all(
    importlib.util.find_spec(m) is not None for m in _PYWIN32_MODULES
)

# Capability guard (native Windows + pywin32), never a blanket skipif(win32).
# Paired below with portable fail-closed tests and POSIX 0o600 tests.
windows_acl_native = pytest.mark.skipif(
    not _HAVE_WINDOWS_ACL,
    reason="native Windows + pywin32 required to apply/verify owner-only DACLs; "
    "portable fail-closed + POSIX 0o600 contracts are covered by the paired tests",
)


def _force_windows_branch(monkeypatch):
    """Make windows_acl.secure_write_secret_file take the Windows path anywhere."""
    monkeypatch.setattr(windows_acl, "is_windows", lambda: True)
    monkeypatch.setattr(windows_acl, "pywin32_available", lambda: True)


# ==========================================================================
# native Windows: each writer lands an owner-only, protected DACL
# ==========================================================================


@windows_acl_native
def test_native_save_auth_store_owner_only_dacl(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    store = {
        "version": auth_mod.AUTH_STORE_VERSION,
        "providers": {"openai-codex": {"tokens": {"access_token": "secret-x"}}},
        "active_provider": "openai-codex",
    }
    path = auth_mod._save_auth_store(store)

    assert windows_acl.verify_owner_only_dacl(path) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["providers"]["openai-codex"]["tokens"]["access_token"] == "secret-x"


@windows_acl_native
def test_native_save_qwen_cli_tokens_owner_only_dacl(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tokens = {"access_token": "qwen-secret", "refresh_token": "qwen-refresh"}
    path = auth_mod._save_qwen_cli_tokens(tokens)

    assert windows_acl.verify_owner_only_dacl(path) is True
    assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == "qwen-secret"


@windows_acl_native
def test_native_shared_youtab_store_owner_only_dacl(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv(
        "YOUTAB_AGENT_SHARED_AUTH_DIR", str(tmp_path / "shared_override")
    )
    state = {
        "access_token": "youtab-access-xxx",
        "refresh_token": "youtab-refresh-xxx",
        "token_type": "Bearer",
    }
    auth_mod._write_shared_youtab_state(state)
    path = auth_mod._youtab_shared_store_path()

    assert path.exists()
    assert windows_acl.verify_owner_only_dacl(path) is True
    assert json.loads(path.read_text(encoding="utf-8"))["refresh_token"] == "youtab-refresh-xxx"


@windows_acl_native
def test_native_secure_file_owner_only_dacl(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-abc\n", encoding="utf-8")
    config_mod._secure_file(env)
    assert windows_acl.verify_owner_only_dacl(env) is True


@windows_acl_native
def test_native_auth_store_uses_born_owner_only_temp(tmp_path, monkeypatch):
    """No-window proof: the writer routes through secure_write_secret_file (which
    creates the temp file born owner-only) rather than a plain os.open — so the
    secret never exists under the parent's broad inherited DACL."""
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))

    seen: list[str] = []
    real = windows_acl.secure_write_secret_file

    def _spy(path, data, **kw):
        seen.append(os.fspath(path))
        return real(path, data, **kw)

    monkeypatch.setattr(windows_acl, "secure_write_secret_file", _spy)
    auth_mod._save_auth_store(
        {"version": auth_mod.AUTH_STORE_VERSION, "providers": {}}
    )

    tmp_writes = [p for p in seen if "auth.json.tmp" in p]
    assert tmp_writes, f"born-owner-only temp write not used; observed={seen!r}"


# ==========================================================================
# native Windows: profiles writers land an owner-only DACL on .env
# ==========================================================================


@pytest.fixture()
def profile_home(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".youtab-agent-runtime"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(default_home))
    return tmp_path


@windows_acl_native
def test_native_profile_seed_env_owner_only_dacl(profile_home):
    profile_dir = profiles_mod.create_profile("coder", no_alias=True, no_skills=True)
    env = profile_dir / ".env"
    assert env.exists()
    assert windows_acl.verify_owner_only_dacl(env) is True


@windows_acl_native
def test_native_profile_clone_env_owner_only_dacl(profile_home):
    (profile_home / ".youtab-agent-runtime" / ".env").write_text(
        "OPENROUTER_API_KEY=root-key\n", encoding="utf-8"
    )
    profile_dir = profiles_mod.create_profile(
        "coder", clone_config=True, no_alias=True
    )
    env = profile_dir / ".env"
    assert env.read_text(encoding="utf-8") == "OPENROUTER_API_KEY=root-key\n"
    assert windows_acl.verify_owner_only_dacl(env) is True


@windows_acl_native
def test_native_backfill_env_owner_only_dacl(profile_home):
    (profile_home / ".youtab-agent-runtime" / ".env").write_text(
        "OPENROUTER_API_KEY=root-key\n", encoding="utf-8"
    )
    p1 = profiles_mod.create_profile("old1", no_alias=True, no_skills=True)
    (p1 / ".env").unlink()

    backfilled = profiles_mod.backfill_profile_envs(quiet=True)
    assert "old1" in backfilled
    env = p1 / ".env"
    assert env.read_text(encoding="utf-8") == "OPENROUTER_API_KEY=root-key\n"
    assert windows_acl.verify_owner_only_dacl(env) is True


# ==========================================================================
# native Windows: fail-closed removes the real file on a real verify failure
# ==========================================================================


@windows_acl_native
def test_native_secure_file_failclosed_removes_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-abc\n", encoding="utf-8")
    # Real apply runs; force the post-apply verification to report non-compliant.
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: False)
    with pytest.raises(OSError):
        config_mod._secure_file(env)
    assert not env.exists(), "a config secret must not persist when its DACL can't be proven"


@windows_acl_native
def test_native_save_auth_store_failclosed_removes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: False)
    with pytest.raises(OSError):
        auth_mod._save_auth_store(
            {"version": auth_mod.AUTH_STORE_VERSION, "providers": {}}
        )
    assert not auth_mod._auth_file_path().exists()
    # No stray plaintext temp left behind either.
    parent = auth_mod._auth_file_path().parent
    if parent.exists():
        assert not any(p.name.startswith("auth.json.tmp") for p in parent.iterdir())


# ==========================================================================
# portable: fail-closed control flow (forced Windows branch) leaves no residue
# ==========================================================================


def test_portable_save_auth_store_failclosed_no_residue(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    _force_windows_branch(monkeypatch)
    monkeypatch.setattr(windows_acl, "apply_owner_only_dacl", lambda _p: None)
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: False)

    with pytest.raises(OSError):
        auth_mod._save_auth_store(
            {
                "version": auth_mod.AUTH_STORE_VERSION,
                "providers": {"p": {"tokens": {"access_token": "leak-me"}}},
            }
        )

    # No auth.json and no secret bytes anywhere under the home.
    for root, _dirs, files in os.walk(tmp_path):
        for name in files:
            with open(os.path.join(root, name), "rb") as fh:
                assert b"leak-me" not in fh.read(), f"secret leaked into {name}"


def test_portable_save_qwen_failclosed_no_residue(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _force_windows_branch(monkeypatch)
    monkeypatch.setattr(windows_acl, "apply_owner_only_dacl", lambda _p: None)
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: False)

    with pytest.raises(OSError):
        auth_mod._save_qwen_cli_tokens({"access_token": "qwen-leak", "refresh_token": "r"})

    assert not auth_mod._qwen_cli_auth_path().exists()
    for root, _dirs, files in os.walk(tmp_path):
        for name in files:
            with open(os.path.join(root, name), "rb") as fh:
                assert b"qwen-leak" not in fh.read()


def test_portable_profile_seed_env_failclosed_raises(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".youtab-agent-runtime"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(default_home))
    _force_windows_branch(monkeypatch)
    monkeypatch.setattr(windows_acl, "apply_owner_only_dacl", lambda _p: None)
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: False)

    # The seed .env writer no longer swallows: create_profile must surface the
    # fail-closed error rather than leave an unprotectable .env behind.
    with pytest.raises(OSError):
        profiles_mod.create_profile("coder", no_alias=True, no_skills=True)

    profile_dir = profiles_mod.get_profile_dir("coder")
    assert not (profile_dir / ".env").exists()


def test_portable_backfill_env_failclosed_skips_profile(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".youtab-agent-runtime"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(default_home))
    (default_home / ".env").write_text("OPENROUTER_API_KEY=root-key\n", encoding="utf-8")

    # Create the profile BEFORE forcing the failure so create_profile succeeds,
    # then remove its .env to simulate a pre-seeding profile.
    p1 = profiles_mod.create_profile("old1", no_alias=True, no_skills=True)
    (p1 / ".env").unlink()

    _force_windows_branch(monkeypatch)
    monkeypatch.setattr(windows_acl, "apply_owner_only_dacl", lambda _p: None)
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: False)

    backfilled = profiles_mod.backfill_profile_envs(quiet=True)
    # Fail-closed + resilient: the unprotectable profile is skipped, not listed,
    # and no readable .env is left behind.
    assert "old1" not in backfilled
    assert not (p1 / ".env").exists()


# ==========================================================================
# paired POSIX: the 0o600 write contract is preserved by the new code paths
# ==========================================================================


@_wincompat.requires_posix_permissions
def test_posix_save_auth_store_is_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    old = os.umask(0o022)
    try:
        path = auth_mod._save_auth_store(
            {"version": auth_mod.AUTH_STORE_VERSION, "providers": {}}
        )
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@_wincompat.requires_posix_permissions
def test_posix_secure_file_tightens_to_0600(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk\n", encoding="utf-8")
    os.chmod(env, 0o644)
    config_mod._secure_file(env)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


@_wincompat.requires_posix_permissions
def test_posix_profile_clone_env_is_0600(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".youtab-agent-runtime"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(default_home))
    src_env = default_home / ".env"
    src_env.write_text("OPENROUTER_API_KEY=root-key\n", encoding="utf-8")
    os.chmod(src_env, 0o644)  # loose source mode

    profile_dir = profiles_mod.create_profile(
        "coder", clone_config=True, no_alias=True
    )
    env = profile_dir / ".env"
    assert env.read_text(encoding="utf-8") == "OPENROUTER_API_KEY=root-key\n"
    # Clone normalises the loose source mode to owner-only.
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
