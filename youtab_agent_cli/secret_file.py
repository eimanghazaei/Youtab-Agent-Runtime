"""File-backed secrets for the Agent Runtime service boundary and providers.

12-factor / Docker-secret / Kubernetes-secret / systemd-credential pattern: a
secret's VALUE is never placed in the environment — only a PATH via
``<NAME>_FILE`` — and the application reads the file itself. This keeps the
credential out of ``os.environ``, ``/proc/<pid>/environ``, argv, ``docker
inspect`` and logs.

Two tiers, one loader (``read_secret_file`` / ``env_or_file``):

* **Boundary tier** (``require_secure_perms=False``, the default) — the
  service-boundary secrets ``YOUTAB_AGENT_RUNTIME_SERVICE_SECRET`` and
  ``YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN``. These are frequently delivered as
  Docker/Kubernetes secrets, whose default mount is world-readable ``0444`` and
  root-owned; a strict owner-only/``0400`` requirement would reject those normal
  deliveries. The boundary tier therefore keeps the historical fail-closed
  checks (regular file, not a symlink, non-empty, size cap, UTF-8, and on POSIX
  a group/other-writable tamper check) without an owner/mode/DACL requirement.

* **Strict tier** (``require_secure_perms=True``) — provider API keys
  (``<PROVIDER_API_KEY_ENV>_FILE``, WAVE-30B) and the live-benchmark credential
  files. These are Owner-installed per a documented owner-only ``0400`` (POSIX)
  / protected owner-only DACL (Windows) procedure, so the loader verifies the
  full OS-level posture *before returning any bytes*: opened with ``O_NOFOLLOW``
  and validated from the same file descriptor (no TOCTOU re-open), owner ==
  current euid, mode no broader than ``0400`` (no group/world bits, no
  owner-write/execute), Windows protected owner-only DACL, no UTF-8 BOM,
  absolute non-traversing path, and (opt-in) not inside the repo working tree or
  a cloud-synced folder. If verification cannot be performed (e.g. pywin32 is
  unavailable on Windows) the read is refused — fail closed, never degrade.

The value is returned for in-memory use only and is never echoed; exception
messages disclose the variable name and safe path metadata but never the file
CONTENTS, an authorization header, or any fingerprint derived from the secret.
"""

from __future__ import annotations

import os
import stat

# Real secrets are short; a large file signals a misconfiguration (a wrong path
# mounted) and we refuse rather than read it all.
_MAX_SECRET_FILE_BYTES = 64 * 1024

_UTF8_BOM = b"\xef\xbb\xbf"

# Directory-name fragments that indicate a consumer cloud-sync root. Used only by
# the opt-in ``forbid_repo_and_cloud`` guard for LOCAL secret files (never for
# production ``/run/secrets`` delivery). Matched case-insensitively against path
# components.
_CLOUD_SYNC_MARKERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "google_drive",
    "icloud drive",
    "icloud",
    "box sync",
    "nextcloud",
    "pcloud",
)


class SecretFileError(RuntimeError):
    """A ``<NAME>_FILE`` secret source is present but unusable. Fail closed —
    never silently fall back to an inline value or a default."""


def _path_components_lower(path: str) -> list[str]:
    norm = os.path.normpath(path).replace("\\", "/")
    return [part.lower() for part in norm.split("/") if part]


def _reject_repo_or_cloud_location(path: str, *, var: str) -> None:
    """Refuse a secret stored inside a git working tree or a cloud-sync folder.

    Only invoked for LOCAL secret files (``forbid_repo_and_cloud=True``); it must
    never gate production ``/run/secrets`` delivery.
    """
    components = _path_components_lower(path)
    for marker in _CLOUD_SYNC_MARKERS:
        if marker in components:
            raise SecretFileError(
                f"{var}_FILE={path!r} is inside a cloud-synced folder "
                f"({marker!r}) — refused; store the secret outside sync roots"
            )
    # Walk up from the file's directory looking for a .git marker (repo working
    # tree). A secret checked into / co-located with the repo risks accidental
    # commit or sync.
    probe = os.path.dirname(os.path.abspath(path))
    seen = set()
    while probe and probe not in seen:
        seen.add(probe)
        if os.path.exists(os.path.join(probe, ".git")):
            raise SecretFileError(
                f"{var}_FILE={path!r} is inside a git working tree ({probe!r}) "
                f"— refused; store the secret outside the repository"
            )
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent


def _reject_traversal(path: str, *, var: str) -> None:
    """Require an absolute path with no ``..`` components (strict tier)."""
    if not os.path.isabs(path):
        raise SecretFileError(
            f"{var}_FILE={path!r} is not an absolute path — refused"
        )
    if ".." in path.replace("\\", "/").split("/"):
        raise SecretFileError(
            f"{var}_FILE={path!r} contains a '..' path segment — refused"
        )


def _verify_posix_secure(st: os.stat_result, path: str, *, var: str, allowed_uids) -> None:
    """Owner + mode verification for a strict-tier POSIX secret file.

    ``st`` must come from ``fstat`` on the ``O_NOFOLLOW``-opened descriptor so the
    checks and the read observe the same inode.
    """
    if allowed_uids is None:
        allowed_uids = {os.geteuid()}
    if st.st_uid not in allowed_uids:
        raise SecretFileError(
            f"{var}_FILE={path!r} is not owned by the runtime account "
            f"(uid {st.st_uid}) — refused"
        )
    perm = stat.S_IMODE(st.st_mode)
    # No group/world bits and no owner write/execute: only owner-read (0o400) may
    # remain set. "no broader than 0400".
    if perm & 0o377:
        raise SecretFileError(
            f"{var}_FILE={path!r} mode {perm:#o} is broader than 0o400 — refused"
        )


def _verify_windows_secure(path: str, *, var: str) -> None:
    """Owner-only protected-DACL verification for a strict-tier Windows secret."""
    from youtab_agent_cli import windows_acl

    if not windows_acl.pywin32_available():
        raise SecretFileError(
            f"{var}_FILE={path!r} cannot be permission-verified on Windows "
            f"(pywin32 unavailable) — refused"
        )
    try:
        ok = windows_acl.verify_owner_only_dacl(path)
    except OSError as exc:
        raise SecretFileError(
            f"{var}_FILE={path!r} DACL verification failed — refused"
        ) from exc
    if not ok:
        raise SecretFileError(
            f"{var}_FILE={path!r} is not an owner-only (protected DACL) file — refused"
        )


def _decode(raw: bytes, *, path: str, var: str, secure: bool) -> str:
    if secure and raw.startswith(_UTF8_BOM):
        raise SecretFileError(
            f"{var}_FILE={path!r} begins with a UTF-8 BOM — refused"
        )
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


def read_secret_file(
    path: str,
    *,
    var: str,
    require_secure_perms: bool = False,
    allowed_uids=None,
    forbid_repo_and_cloud: bool = False,
) -> str:
    """Read a secret from ``path`` with fail-closed safety checks.

    Refuses missing / empty / oversized / symlinked / non-regular / unsafe files.
    Uses a bounded read and trims ONLY a single documented terminal newline
    (``\\n`` or ``\\r\\n``) — never arbitrary secret characters. Never includes
    the file CONTENTS in an exception message.

    ``require_secure_perms`` selects the strict tier: an ``O_NOFOLLOW`` (POSIX)
    read with owner/mode verification, or a protected owner-only DACL check
    (Windows), plus BOM and traversal rejection, performed BEFORE any bytes are
    returned. ``forbid_repo_and_cloud`` additionally refuses a secret stored in a
    git working tree or a cloud-sync folder (for LOCAL secret files only).
    """
    if require_secure_perms:
        _reject_traversal(path, var=var)
        if forbid_repo_and_cloud:
            _reject_repo_or_cloud_location(path, var=var)

    # lstat first: catches missing/symlink/non-regular without following a link.
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
    # here, so this check is POSIX-only. Applies to BOTH tiers.
    if os.name == "posix" and (mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise SecretFileError(f"{var}_FILE={path!r} is group/other-writable — refused")

    if require_secure_perms and os.name == "nt":
        _verify_windows_secure(path, var=var)

    if require_secure_perms and os.name == "posix":
        # Re-open with O_NOFOLLOW and validate from the same descriptor so the
        # permission checks and the read observe the same inode (no TOCTOU).
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise SecretFileError(
                f"{var}_FILE={path!r} could not be securely opened: {exc}"
            ) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise SecretFileError(
                    f"{var}_FILE={path!r} is not a regular file — refused"
                )
            _verify_posix_secure(st, path, var=var, allowed_uids=allowed_uids)
            if st.st_size > _MAX_SECRET_FILE_BYTES:
                raise SecretFileError(
                    f"{var}_FILE={path!r} exceeds the size ceiling — refused"
                )
            raw = os.read(fd, _MAX_SECRET_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > _MAX_SECRET_FILE_BYTES:
            raise SecretFileError(
                f"{var}_FILE={path!r} exceeds the size ceiling — refused"
            )
        return _decode(raw, path=path, var=var, secure=True)

    with open(path, "rb") as handle:
        raw = handle.read(_MAX_SECRET_FILE_BYTES + 1)
    if len(raw) > _MAX_SECRET_FILE_BYTES:
        raise SecretFileError(f"{var}_FILE={path!r} exceeds the size ceiling — refused")
    return _decode(raw, path=path, var=var, secure=require_secure_perms)


def env_or_file(
    name: str,
    default: str = "",
    *,
    require_secure_perms: bool = False,
    allowed_uids=None,
    forbid_repo_and_cloud: bool = False,
) -> str:
    """Return a secret from ``<name>_FILE`` (preferred) or ``<name>`` (fallback).

    If ``<name>_FILE`` is set, the file is read with fail-closed safety checks and
    its inline counterpart must NOT also be set — a dual source has no precedence
    contract and a silent winner would hide a deployment mistake. If neither the
    file nor the inline var is set, ``default`` is returned. Reads happen at call
    time, never at import.

    ``require_secure_perms`` / ``forbid_repo_and_cloud`` are forwarded to
    :func:`read_secret_file` for the strict tier (provider keys, benchmark creds).
    """
    file_path = os.getenv(f"{name}_FILE", "").strip()
    if file_path:
        inline = os.getenv(name)
        if inline not in (None, ""):
            raise SecretFileError(
                f"both {name} and {name}_FILE are set — refusing an ambiguous secret source"
            )
        return read_secret_file(
            file_path,
            var=name,
            require_secure_perms=require_secure_perms,
            allowed_uids=allowed_uids,
            forbid_repo_and_cloud=forbid_repo_and_cloud,
        )
    inline = os.getenv(name, default)
    # When a caller opts into the strict tier (provider keys / benchmark creds) but
    # only a plaintext-environment value is present, warn once — and refuse under
    # live-benchmark mode (file-based delivery is mandatory) — WAVE-30B A#1.
    if require_secure_perms and inline and inline != default:
        note_plaintext_credential(name)
    return inline


# ---------------------------------------------------------------------------
# Provider-neutral credential file resolution (WAVE-30B §4).
#
# Every credential-bearing provider names its API key by an environment variable
# (OPENAI_API_KEY, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, GLM_API_KEY, ...). The
# generic file contract is ``<ENV>_FILE``: adding a provider requires no change
# here or in benchmark core — the loader keys off the variable NAME, never the
# provider identity. Provider keys always use the strict tier.
# ---------------------------------------------------------------------------

_warned_plaintext_vars: set[str] = set()


def live_benchmark_file_secrets_required() -> bool:
    """True when the process is running an authorized live-provider benchmark, in
    which case plaintext-environment provider credentials are refused and
    file-based delivery (``<ENV>_FILE``) is mandatory."""
    return os.getenv("YOUTAB_AGENT_LIVE_BENCHMARK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_credential_file(
    env_var: str,
    *,
    inline_present: bool,
    forbid_repo_and_cloud: bool = False,
) -> str | None:
    """Return the strict-tier value of ``<env_var>_FILE`` if configured, else None.

    Fail closed on a dual source (both an inline value and a ``_FILE`` path
    configured). Provider keys are always read with ``require_secure_perms=True``.
    """
    if not env_var:
        return None
    file_path = os.getenv(f"{env_var}_FILE", "").strip()
    if not file_path:
        return None
    if inline_present:
        raise SecretFileError(
            f"both {env_var} and {env_var}_FILE are set — refusing an ambiguous secret source"
        )
    return read_secret_file(
        file_path,
        var=env_var,
        require_secure_perms=True,
        forbid_repo_and_cloud=forbid_repo_and_cloud,
    )


def read_named_key_file_env(
    path_env_var: str, *, forbid_repo_and_cloud: bool = False
) -> str | None:
    """Load a provider key given an env var that holds the PATH to the key file.

    Supports a custom profile's ``api_key_file_env`` declaration. Returns None
    when the env var is unset/empty; loads strictly otherwise.
    """
    if not path_env_var:
        return None
    path = os.getenv(path_env_var, "").strip()
    if not path:
        return None
    return read_secret_file(
        path,
        var=path_env_var,
        require_secure_perms=True,
        forbid_repo_and_cloud=forbid_repo_and_cloud,
    )


def looks_like_credential_env(env_var: str) -> bool:
    """True only for env-var NAMES that denote a secret credential.

    ``note_plaintext_credential`` is reached from generic readers (e.g.
    ``runtime_provider._getenv``, which also reads base URLs, timeouts and the
    provider name), so the plaintext refusal/warning must NOT fire for
    non-credential vars — otherwise a live benchmark that sets, say,
    ``OPENROUTER_BASE_URL`` would be wrongly refused. Match clear credential
    suffixes/substrings and exclude obvious non-secrets.
    """
    u = (env_var or "").upper()
    if not u:
        return False
    if u.endswith(("_URL", "_BASE_URL", "_ENDPOINT", "_HOST", "_REGION",
                   "_PROVIDER", "_MODEL", "_TIMEOUT", "_SECONDS")) or "TIMEOUT" in u:
        return False
    return (
        "API_KEY" in u
        or "APIKEY" in u
        or u.endswith(("_KEY", "_TOKEN", "_SECRET"))
        or "_TOKEN_" in u
    )


def note_plaintext_credential(env_var: str, *, log=None) -> None:
    """Enforce/observe legacy plaintext-environment credential use.

    In live-benchmark mode a plaintext provider credential is refused (file-based
    delivery is mandatory). Otherwise it is allowed for backwards compatibility
    but emits a one-time non-secret warning naming ONLY the variable. No-ops for
    non-credential env names (see :func:`looks_like_credential_env`).
    """
    if not env_var or not looks_like_credential_env(env_var):
        return
    if live_benchmark_file_secrets_required():
        raise SecretFileError(
            f"{env_var} is a plaintext-environment credential but live-benchmark "
            f"mode requires file-based delivery via {env_var}_FILE"
        )
    if env_var in _warned_plaintext_vars:
        return
    _warned_plaintext_vars.add(env_var)
    msg = (
        f"credential {env_var} is provided as a plaintext environment value; "
        f"prefer file-based delivery via {env_var}_FILE "
        f"(see docs/ops/SECRET_FILE_SUPPORT.md)"
    )
    if log is not None:
        log(msg)
    else:
        import logging

        logging.getLogger("youtab_agent_cli.secret_file").warning(msg)
