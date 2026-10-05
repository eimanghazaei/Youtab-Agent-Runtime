"""Install identity and update guidance, independent of CLI configuration.

Only stdlib and the import-safe runtime constants module are needed here.
Configuration's compatibility wrappers supply its historical dependency seams;
update checks use the canonical implementation directly.
"""

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from youtab_constants import get_youtab_home

logger = logging.getLogger(__name__)


def get_project_root() -> Path:
    """Return the running code's installation directory."""
    return Path(__file__).parent.parent.resolve()


_MANAGED_TRUE_VALUES = ("true", "1", "yes")
_MANAGED_SYSTEM_NAMES = {
    "nix": "NixOS",
    "nixos": "NixOS",
}
# The Nix store root. Used by detect_install_method to identify installs
# from `nix run` / `nix profile install` (which don't set YOUTAB_AGENT_MANAGED).
# A module-level constant so tests can patch it without creating files
# under the real /nix/store.
_NIX_STORE = Path("/nix/store")
# Values that used to signal a Homebrew-managed install. Homebrew is no
# longer a supported distribution method, so these are explicitly ignored
# rather than treated as a managed system — they fall through to git/unknown
# detection instead of blocking config writes.
_IGNORED_MANAGED_VALUES = frozenset({"brew", "homebrew"})


def get_managed_system(*, home: Optional[Path] = None) -> Optional[str]:
    """Return the package manager owning this install, if any."""
    raw = os.getenv("YOUTAB_AGENT_MANAGED", "").strip()
    if raw:
        normalized = raw.lower()
        if normalized in _IGNORED_MANAGED_VALUES:
            return None
        if normalized in _MANAGED_TRUE_VALUES:
            return "NixOS"
        return _MANAGED_SYSTEM_NAMES.get(normalized, raw)

    managed_marker = (home if home is not None else get_youtab_home()) / ".managed"
    if managed_marker.exists():
        return "NixOS"
    return None


def is_managed() -> bool:
    """Check if Youtab is running in package-manager-managed mode.

    Two signals: the YOUTAB_AGENT_MANAGED env var (set by the systemd service),
    or a .managed marker file in YOUTAB_AGENT_HOME (set by the NixOS activation
    script, so interactive shells also see it).
    """
    return get_managed_system() is not None


_NIX_UPDATE_MSG = (
    "Update Youtab through the Nix source that installed it "
    "(e.g. nix profile upgrade, or update your flake input and rebuild with nixos-rebuild or home-manager switch)"
)


def get_managed_update_command(*, managed_system: Optional[Callable[[], Optional[str]]] = None) -> Optional[str]:
    """Return the preferred upgrade command for a managed install."""
    system = (managed_system or get_managed_system)()
    if system == "NixOS":
        return _NIX_UPDATE_MSG
    return None


def _install_method_project_root(project_root: Optional[Path] = None) -> Path:
    """Resolve the directory that holds the *running code* (the install tree).

    This is the parent of ``youtab_agent_cli/`` — i.e. the git checkout for source
    installs, ``/opt/youtab`` inside the published image. It is a property of
    the running interpreter, NOT of ``$YOUTAB_AGENT_HOME``, which is why a
    code-scoped stamp here is immune to two installs sharing one data
    directory.
    """
    if project_root is not None:
        return project_root
    return Path(__file__).parent.parent.resolve()


def detect_install_method(
    project_root: Optional[Path] = None, *, home: Optional[Path] = None,
    managed_system: Optional[Callable[[], Optional[str]]] = None,
    container_check: Optional[Callable[[], bool]] = None,
    nix_store: Optional[Path] = None,
) -> str:
    """Detect how Youtab was installed: 'docker', 'nix', 'nixos', 'git', or 'unknown'.

    Resolution order:
    1. Code-scoped stamp ``<install tree>/.install_method`` (next to the
       running code) — the authoritative marker.
    2. Legacy home-scoped stamp ``$YOUTAB_AGENT_HOME/.install_method`` — read for
       backward compatibility, but a ``docker`` value is IGNORED when we are
       not actually running inside a container (see below).
    3. YOUTAB_AGENT_MANAGED env / .managed marker (NixOS managed mode)
    4. /nix/store/ path detection -> 'nix' (nix run / nix profile install)
    5. .git directory presence -> 'git'
    6. Fallback -> 'unknown'

    Why the stamp is code-scoped, not home-scoped (issue: shared ``~/.youtab-agent-runtime``)
    --------------------------------------------------------------------------
    The install method describes *the binary that is running*, but
    ``$YOUTAB_AGENT_HOME`` is a shared DATA directory — the Docker docs deliberately
    bind-mount it (``~/.youtab-agent-runtime:/opt/data``) so config/sessions/memory persist
    and can be shared with a host-side Desktop/CLI install. When a
    containerised gateway and a host install share one ``$YOUTAB_AGENT_HOME``, a
    home-scoped stamp is a single slot describing two different installs:
    the container stamps ``docker`` on every boot, the host install then reads
    ``docker`` and ``youtab update`` refuses to run ("doesn't apply inside the
    Docker container") even though the host binary is a perfectly updatable
    git/pip install. Scoping the stamp to the install tree gives each install
    its own truthful marker.

    Self-healing for already-poisoned homes: a legacy ``docker`` value in the
    home-scoped stamp is only honoured when we are genuinely in a container.
    On a host install that read a contaminating ``docker`` stamp, we fall
    through to managed/.git detection instead — so existing shared-home
    setups recover without the user touching anything.

    Note: running inside a container is NOT treated as "docker" on its own.
    The supported installs self-identify via the code-scoped stamp:
      - the curl installer (scripts/install.sh, the README/website install
        command) git-clones the repo and stamps ``git`` next to the code;
      - the published ``youtab/youtab-agent-runtime`` image bakes a ``docker``
        stamp into ``/opt/youtab`` at build time.
    An unsupported manual install dropped into a container (no stamp) falls
    through to the ``.git`` checks and behaves like any off-path install.
    See issue #34397.
    """
    root = _install_method_project_root(project_root)
    supported_methods = {"docker", "nix", "nixos", "git", "unknown"}

    # 1. Code-scoped stamp — authoritative, immune to shared $YOUTAB_AGENT_HOME.
    try:
        method = (root / ".install_method").read_text(encoding="utf-8").strip().lower()
        if method in supported_methods:
            return method
    except OSError:
        logger.debug("Install-tree method stamp unavailable; trying other identity signals")

    # 2. Legacy home-scoped stamp — back-compat. Ignore a ``docker`` value
    #    when we are not actually containerised: that is the signature of a
    #    host install whose shared $YOUTAB_AGENT_HOME was stamped by a co-located
    #    container, and honouring it wrongly blocks ``youtab update``.
    try:
        method = (
            ((home if home is not None else get_youtab_home()) / ".install_method")
            .read_text(encoding="utf-8")
            .strip()
            .lower()
        )
        if method in supported_methods and not (method == "docker" and not (container_check or _running_in_container)()):
            return method
    except OSError:
        logger.debug("Legacy install-method stamp unavailable; trying other identity signals")

    managed = (managed_system or get_managed_system)()
    if managed:
        return managed.lower().replace(" ", "-")

    # detect Nix installs that don't set YOUTAB_AGENT_MANAGED (e.g. ``nix run``,
    # ``nix profile install``). The code lives under /nix/store/ which is the
    # hallmark of a nix-built install — no other supported install path puts
    # code there.
    try:
        resolved = root.resolve()
        store = nix_store if nix_store is not None else _NIX_STORE
        if resolved != store and store in resolved.parents:
            return "nix"
    except OSError:
        logger.debug("Nix install-path identity unavailable; trying Git identity")

    # detect git repo installs (normal installer, development env)
    git_path = root / ".git"
    if git_path.is_dir():
        return "git"

    # detect git repo installs from worktrees
    if git_path.is_file():
        try:
            content = git_path.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                return "git"
        except OSError:
            logger.debug("Git worktree identity unavailable; install method remains unknown")
    return "unknown"


def _running_in_container() -> bool:
    """Thin wrapper around ``youtab_constants.is_container`` (import-safe)."""
    try:
        from youtab_constants import is_container

        return is_container()
    except Exception:
        return False


def stamp_install_method(method: str, project_root: Optional[Path] = None) -> None:
    """Write the install method next to the running code (code-scoped stamp).

    The stamp lives in the install tree (``<install tree>/.install_method``),
    not in ``$YOUTAB_AGENT_HOME``, so that two installs sharing one data directory
    do not overwrite each other's marker. See ``detect_install_method`` for
    the full rationale.

    Best-effort: if the install tree is read-only (e.g. the immutable
    ``/opt/youtab`` in the published image, which instead bakes the stamp at
    build time) the write silently no-ops and detection falls back to its
    other signals.
    """
    root = _install_method_project_root(project_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".install_method").write_text(method + "\n", encoding="utf-8")
    except OSError:
        logger.debug("Best-effort install-method stamp write failed")


def recommended_update_command_for_method(method: str) -> str:
    """Return the update command or guidance for a given install method."""
    if method in {"nix", "nixos"}:
        return _NIX_UPDATE_MSG
    if method == "docker":
        return "docker pull youtab/youtab-agent-runtime:latest"
    return "youtab update"


def recommended_update_command() -> str:
    """Return the best update command for the current installation."""
    managed_cmd = get_managed_update_command()
    if managed_cmd:
        return managed_cmd
    method = detect_install_method(get_project_root())
    return recommended_update_command_for_method(method)


# Long-form text for ``youtab update`` / ``--check`` when running inside the
# Docker image.  Surfaced by ``cmd_update`` and ``_cmd_update_check`` in
# youtab_agent_cli/main.py; lives here so the wording stays consistent and we
# don't grow two slightly-different copies.
#
# Why this matters:
#   - The published image excludes ``.git`` (see .dockerignore), so the
#     git-based update path can never succeed inside the container.
#   - The pre-existing fallback message ("✗ Not a git repository. Please
#     reinstall: curl ... install.sh") is actively misleading inside Docker
#     — that script installs a *new* host-side Youtab, it doesn't update
#     the running container.
#   - The right action is ``docker pull`` + restart the container; this
#     helper spells that out, with notes on tag pinning and config
#     persistence so users don't get blindsided.
_DOCKER_UPDATE_MESSAGE = """\
✗ ``youtab update`` doesn't apply inside the Docker container.

Youtab Agent Runtime runs as a published image (youtab/youtab-agent-runtime), not a
git checkout — the container has no working tree to pull into.  Update by
pulling a fresh image and restarting your container instead:

  docker pull youtab/youtab-agent-runtime:latest
  # then restart whatever started the container, e.g.:
  docker compose up -d --force-recreate youtab-agent-runtime
  # or, for ad-hoc runs, exit the current container and `docker run` again

Verify the new version after restart:
  docker run --rm youtab/youtab-agent-runtime:latest --version

Notes:
  • If you pinned a specific tag (e.g. ``:v0.14.0``) the ``:latest`` tag
    won't move your container — pull the newer tag you actually want, or
    switch to ``:latest`` / ``:main`` for rolling updates.  See available
    tags at https://hub.docker.com/r/youtab/youtab-agent-runtime/tags
  • Your config and session history live under ``$YOUTAB_AGENT_HOME`` (``/opt/data``
    in the container, typically bind-mounted from the host) and persist
    across image upgrades — re-pulling doesn't lose any state.
  • Running a fork?  Build your own image with this repo's ``Dockerfile``
    and replace the ``docker pull`` step with your build/push pipeline."""


def format_docker_update_message() -> str:
    """Return the user-facing message for ``youtab update`` inside Docker.

    Centralised so ``cmd_update`` (the apply path) and ``_cmd_update_check``
    (the dry-run path) share the same wording.  See ``_DOCKER_UPDATE_MESSAGE``
    above for the full rationale.
    """
    return _DOCKER_UPDATE_MESSAGE


def format_managed_message(
    action: str = "modify this Youtab installation", *,
    managed_system: Optional[Callable[[], Optional[str]]] = None,
) -> str:
    """Build a user-facing error for managed installs."""
    system = (managed_system or get_managed_system)() or "a package manager"
    raw = os.getenv("YOUTAB_AGENT_MANAGED", "").strip().lower()

    if system == "NixOS":
        env_hint = "true" if raw in _MANAGED_TRUE_VALUES else raw or "true"
        return (
            f"Cannot {action}: this Youtab installation is managed by NixOS "
            f"(YOUTAB_AGENT_MANAGED={env_hint}).\n"
            "Edit services.youtab-agent-runtime-agent.settings in your configuration.nix and run:\n"
            "  sudo nixos-rebuild switch"
        )

    return (
        f"Cannot {action}: this Youtab installation is managed by {system}.\n"
        "Use your package manager to upgrade or reinstall Youtab."
    )
