"""File-backed secrets for the Agent Runtime service boundary.

12-factor / Docker-secret / Kubernetes-secret / systemd-credential pattern: a
secret's VALUE is never placed in the environment — only a PATH via
``<NAME>_FILE`` — and the application reads the file itself. This keeps the
credential out of ``os.environ``, ``/proc/<pid>/environ``, argv, ``docker
inspect`` and logs.

Scope: the service-boundary secrets read directly from the environment
(``YOUTAB_AGENT_RUNTIME_SERVICE_SECRET`` and
``YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN``). Provider/model credentials continue to
resolve through the profile-scoped credential pool / ``config.yaml`` path and are
NOT handled here.
"""

from __future__ import annotations

import os
import stat

# Real service secrets are short; a large file signals a misconfiguration
# (a wrong path mounted) and we refuse rather than read it all.
_MAX_SECRET_FILE_BYTES = 64 * 1024


class SecretFileError(RuntimeError):
    """A ``<NAME>_FILE`` secret source is present but unusable. Fail closed —
    never silently fall back to an inline value or a default."""


def read_secret_file(path: str, *, var: str) -> str:
    """Read a secret from ``path`` with fail-closed safety checks.

    Refuses missing / empty / oversized / symlinked / non-regular / unsafe-mode
    files. Uses a bounded read and trims ONLY a single documented terminal
    newline (``\\n`` or ``\\r\\n``) — never arbitrary secret characters. Never
    includes the file CONTENTS in an exception message.
    """
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise SecretFileError(f"{var}_FILE={path!r} is not accessible: {exc}") from exc
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        raise SecretFileError(f"{var}_FILE={path!r} is a symlink — refused")
    if not stat.S_ISREG(mode):
        raise SecretFileError(f"{var}_FILE={path!r} is not a regular file — refused")
    if info.st_size == 0:
        raise SecretFileError(f"{var}_FILE={path!r} is empty — refused")
    if info.st_size > _MAX_SECRET_FILE_BYTES:
        raise SecretFileError(
            f"{var}_FILE={path!r} is too large ({info.st_size} bytes) — refused"
        )
    # On POSIX, a group/other-writable secret file is unsafe (another principal
    # could swap the value under us). Windows lstat mode bits are not meaningful
    # here, so this check is POSIX-only.
    if os.name == "posix" and (mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise SecretFileError(f"{var}_FILE={path!r} is group/other-writable — refused")
    # WAVE-26 #7b note: the residual addressed by WAVE-26 is that the credential
    # files the runtime *writes* (auth.json, .env, provider tokens) used chmod
    # 0o600, a no-op for access control on Windows — those write paths now apply
    # an owner-only protected DACL (see youtab_agent_cli.windows_acl). The read
    # side here handles operator-supplied 12-factor *_FILE secrets whose DACL the
    # operator owns; a strict owner-only requirement would reject normally-created
    # files (which inherit broader-but-not-writable DACLs) and is intentionally
    # NOT imposed. The POSIX group/other-writable tamper check stays POSIX-only,
    # unchanged from before WAVE-26 (no Windows regression).
    with open(path, "rb") as handle:
        raw = handle.read(_MAX_SECRET_FILE_BYTES + 1)
    if len(raw) > _MAX_SECRET_FILE_BYTES:
        raise SecretFileError(f"{var}_FILE={path!r} exceeds the size ceiling — refused")
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretFileError(
            f"{var}_FILE={path!r} is not valid UTF-8 — refused"
        ) from exc


def env_or_file(name: str, default: str = "") -> str:
    """Return a secret from ``<name>_FILE`` (preferred) or ``<name>`` (fallback).

    If ``<name>_FILE`` is set, the file is read with fail-closed safety checks and
    its inline counterpart must NOT also be set — a dual source has no precedence
    contract and a silent winner would hide a deployment mistake. If neither the
    file nor the inline var is set, ``default`` is returned. Reads happen at call
    time, never at import.
    """
    file_path = os.getenv(f"{name}_FILE", "").strip()
    if file_path:
        inline = os.getenv(name)
        if inline not in (None, ""):
            raise SecretFileError(
                f"both {name} and {name}_FILE are set — refusing an ambiguous secret source"
            )
        return read_secret_file(file_path, var=name)
    return os.getenv(name, default)
