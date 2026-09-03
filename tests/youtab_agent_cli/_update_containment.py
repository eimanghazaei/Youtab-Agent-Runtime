"""Containment primitives for updater tests (WAVE-24).

The updater (``youtab_agent_cli.update_cmd`` via ``youtab_agent_cli.main``) runs
its git operations — including ``git reset --hard origin/<branch>`` and
``git checkout`` — with ``cwd=youtab_agent_cli.main.PROJECT_ROOT``. In an
editable install ``PROJECT_ROOT`` is ``Path(main.__file__).parent.parent`` =
the developer's PRIMARY checkout. In WAVE-23 a Windows run of the updater test
suite let a real ``youtab update`` reach that primary checkout and autostashed
all uncommitted work as whole-tree CRLF.

This module provides defense-in-depth so no updater test can reach the primary
checkout again, regardless of whether an individual test remembers to mock
``subprocess`` or redirect ``PROJECT_ROOT``:

* :func:`make_disposable_repo` — a real, OFFLINE git repo under ``tmp_path``
  with a local bare ``origin`` so ``reset --hard origin/main`` works with zero
  network access.
* :func:`assert_disposable_target` — a fail-closed guard that rejects any target
  (``..``, absolute path outside ``tmp``, symlink escaping ``tmp``, or the real
  primary checkout) BEFORE the updater is invoked.
* :func:`snapshot_refs` / :func:`assert_refs_unchanged` — the before/after
  tripwire that fails loudly if the primary checkout's HEAD, branch, porcelain
  status, or reflog top changed during a test.
These are pure, import-safe helpers. The pytest fixtures that wire them
together (``contained_update_repo``, ``update_target_guard``,
``primary_ref_tripwire`` and the autouse tripwire) live in
``tests/youtab_agent_cli/conftest.py`` so pytest auto-discovers them.

A module-level reference to the real ``subprocess.run`` (:data:`_REAL_RUN`) is
captured at import time so the tripwire keeps working even inside tests that
monkeypatch ``subprocess.run``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Captured before any test can monkeypatch subprocess.run — the tripwire and
# the disposable-repo builder must always reach the real git, never a mock.
_REAL_RUN = subprocess.run

# The PRIMARY developer checkout the updater would target in an editable
# install: parent-of-parent of youtab_agent_cli/main.py. Resolved once.
try:  # pragma: no cover - trivial import guard
    from youtab_agent_cli import main as _cli_main

    PRIMARY_CHECKOUT = Path(_cli_main.__file__).resolve().parent.parent
except Exception:  # pragma: no cover - import failures surface elsewhere
    PRIMARY_CHECKOUT = Path(__file__).resolve().parents[2]


class ContainmentError(AssertionError):
    """Raised fail-closed when a test target escapes the disposable sandbox."""


def _isolating_git_env(home: Path) -> dict[str, str]:
    """Env that pins git config into ``home`` and forbids all network I/O.

    ``GIT_ALLOW_PROTOCOL=file`` means any accidental ``http(s)://`` / ``ssh://``
    remote is refused by git itself, so a test can never reach the internet even
    if a remote URL leaks in. ``GIT_TERMINAL_PROMPT=0`` + ``GIT_ASKPASS``
    guarantee git never blocks on credentials.
    """
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_AUTHOR_NAME": "youtab-test",
        "GIT_AUTHOR_EMAIL": "youtab-test@example.invalid",
        "GIT_COMMITTER_NAME": "youtab-test",
        "GIT_COMMITTER_EMAIL": "youtab-test@example.invalid",
    }


def _git(cwd: Path, *args: str, env: dict[str, str], check: bool = True):
    full = dict(os.environ)
    full.update(env)
    return _REAL_RUN(
        ["git", *args],
        cwd=str(cwd),
        env=full,
        capture_output=True,
        text=True,
        check=check,
    )


def make_disposable_repo(tmp_root: Path) -> "DisposableRepo":
    """Build an OFFLINE checkout + bare origin under ``tmp_root``.

    ``origin/main`` exists locally, so the updater's ``git merge --ff-only
    origin/main`` and ``git reset --hard origin/main`` both work with no network.
    """
    home = tmp_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = _isolating_git_env(home)

    origin = tmp_root / "origin.git"
    _git(tmp_root, "init", "--bare", "-b", "main", str(origin), env=env)

    checkout = tmp_root / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main", env=env)
    _git(checkout, "remote", "add", "origin", str(origin), env=env)
    (checkout / "file.txt").write_text("v1\n", encoding="utf-8")
    _git(checkout, "add", "-A", env=env)
    _git(checkout, "commit", "-qm", "init", env=env)
    _git(checkout, "push", "-q", "origin", "main", env=env)
    _git(checkout, "fetch", "-q", "origin", "main", env=env)
    _git(checkout, "branch", "--set-upstream-to=origin/main", "main", env=env, check=False)
    return DisposableRepo(root=tmp_root, checkout=checkout, origin=origin, env=env)


class DisposableRepo:
    """A throwaway offline git checkout + origin, all under ``tmp_path``."""

    def __init__(self, root: Path, checkout: Path, origin: Path, env: dict[str, str]):
        self.root = root
        self.checkout = checkout
        self.origin = origin
        self.env = env

    def git(self, *args: str, check: bool = True):
        return _git(self.checkout, *args, env=self.env, check=check)

    def diverge_local(self, content: str = "LOCAL EDIT\n") -> None:
        """Create a committed local change so ``reset --hard`` has work to undo."""
        (self.checkout / "file.txt").write_text(content, encoding="utf-8")
        self.git("commit", "-aqm", "local divergence")


def assert_disposable_target(target: Path, tmp_root: Path, primary: Path | None = None) -> Path:
    """Fail-closed containment guard.

    Returns the resolved target iff it is safely contained under ``tmp_root`` and
    is not the primary checkout. ``.resolve()`` collapses ``..`` and follows
    symlinks, so a symlink or ``..`` that escapes ``tmp_root`` lands outside and
    is rejected by the ``parents`` check. Raises :class:`ContainmentError`
    otherwise — never returns an unsafe path.
    """
    primary = PRIMARY_CHECKOUT if primary is None else primary
    tr = Path(tmp_root).resolve()
    tgt = Path(target).resolve()
    pri = Path(primary).resolve()
    if tgt == pri:
        raise ContainmentError(
            f"REFUSING updater target: resolves to the PRIMARY checkout {pri}"
        )
    if tgt != tr and tr not in tgt.parents:
        raise ContainmentError(
            f"REFUSING updater target: {tgt} escapes the disposable sandbox {tr}"
        )
    return tgt


def snapshot_refs(repo: Path) -> dict[str, str]:
    """Read-only snapshot of a repo's identity. Uses the real subprocess.run."""

    def one(*args: str) -> str:
        r = _REAL_RUN(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return r.stdout.strip()

    return {
        "head": one("rev-parse", "HEAD"),
        "branch": one("rev-parse", "--abbrev-ref", "HEAD"),
        "status": one("status", "--porcelain"),
        "reflog": one("reflog", "show", "-1"),
    }


def assert_refs_unchanged(before: dict[str, str], after: dict[str, str], repo: Path) -> None:
    """Loudly fail if a repo's refs/status/reflog changed between snapshots."""
    if before != after:
        diffs = "\n".join(
            f"  {k}: {before.get(k)!r} -> {after.get(k)!r}"
            for k in sorted(set(before) | set(after))
            if before.get(k) != after.get(k)
        )
        raise ContainmentError(
            "PRIMARY CHECKOUT WAS MUTATED BY AN UPDATER TEST — this is the "
            f"WAVE-23 corruption tripwire.\nrepo={repo}\n{diffs}"
        )
