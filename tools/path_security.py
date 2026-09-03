"""Shared path validation helpers for tool implementations.

Extracts the ``resolve() + relative_to()`` and ``..`` traversal check
patterns previously duplicated across skill_manager_tool, skills_tool,
skills_hub, cronjob_tools, and credential_files.
"""

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def validate_within_dir(path: Path, root: Path) -> Optional[str]:
    """Ensure *path* resolves to a location within *root*.

    Returns an error message string if validation fails, or ``None`` if the
    path is safe.  Uses ``Path.resolve()`` to follow symlinks and normalize
    ``..`` components.

    Usage::

        error = validate_within_dir(user_path, allowed_root)
        if error:
            return tool_error(error)
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        return f"Path escapes allowed directory: {exc}"
    return None


def has_traversal_component(path_str: str) -> bool:
    """Return True if *path_str* contains ``..`` traversal components.

    Quick check for obvious traversal attempts before doing full resolution.
    """
    parts = Path(path_str).parts
    return ".." in parts


# Windows reserved device names. These resolve to devices (console, serial
# ports, printers, the null sink) in *every* directory, so opening one for
# write hangs, silently discards data, or -- via the device namespace -- can
# address raw hardware. The set is the classic reserved list; COM0/LPT0 and
# names >= 10 are ordinary files and are deliberately absent.
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# Win32 accepts superscript digits in COM/LPT device names (COM¹ == COM1).
_SUPERSCRIPT_DIGITS = str.maketrans({"¹": "1", "²": "2", "³": "3"})

# Splits on both separators so the check is identical on POSIX and Windows --
# ``pathlib`` only splits ``\\`` on Windows, which would make this host-dependent.
_PATH_SEPARATORS = re.compile(r"[\\/]+")


def _component_is_reserved_device(component: str) -> bool:
    """Return True if a single path component names a Windows reserved device."""
    # Strip a leading drive prefix so "C:con" (drive-relative) is examined as
    # "con".
    if len(component) >= 2 and component[1] == ":" and component[0].isalpha():
        component = component[2:]
    # Win32 strips trailing spaces and dots from a name before resolving the
    # device, so "con.", "con ", and "con. ." all reach the CON device.
    trimmed = component.rstrip(" .")
    if not trimmed:
        return False
    # A name maps to a device by the portion before its first extension dot
    # ("con.txt" -> "con"), ignoring trailing spaces on that portion.
    stem = trimmed.split(".", 1)[0].rstrip(" ").translate(_SUPERSCRIPT_DIGITS)
    return stem.upper() in _WINDOWS_RESERVED_DEVICE_NAMES


def is_windows_reserved_device_path(path_str: str) -> bool:
    """Return True if *path_str* names, or contains a component naming, a
    Windows reserved device, or addresses the device/verbatim namespace.

    Pure and platform-independent (it does not consult ``os.name``), so the
    same logic can be unit-tested on any operating system; callers gate
    *enforcement* on Windows.  Detection covers, case-insensitively:

    * ``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM1``-``COM9``, ``LPT1``-``LPT9``
      in any path component;
    * those names carrying any extension (``con.txt``) and the trailing
      dot/space variants Win32 strips before resolving the device
      (``con.``, ``con ``);
    * the ``\\\\.\\`` (device) and ``\\\\?\\`` (verbatim) namespace prefixes in
      either separator orientation, which reach ``\\\\.\\PhysicalDrive0`` and
      other native-device forms.
    """
    if not path_str:
        return False
    # Device / verbatim namespace prefixes, checked with separators normalized
    # so "//./" and "//?/" are caught alongside the backslash forms.
    normalized = path_str.replace("/", "\\")
    if normalized.startswith("\\\\.\\") or normalized.startswith("\\\\?\\"):
        return True
    for component in _PATH_SEPARATORS.split(path_str):
        if component and _component_is_reserved_device(component):
            return True
    return False
