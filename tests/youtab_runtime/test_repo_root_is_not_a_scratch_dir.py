"""The repository root does not accumulate scratch files.

Seven empty files had collected in the root: `$tmp`, and six named
`C:UserseimanAppDataLocalTemppytest-...`. They came from tests interpolating a
Windows `tmp_path` straight into a bash command -- the backslashes read as
escapes, the drive colon mangled, and `touch` created the sentinel in the
current directory under a name with every separator eaten.

A `.gitignore` entry was added for them once. That hides the litter without
stopping it, and an ignored file is worse than a visible one: it is still on
disk, it still gets copied into a build context, and nobody sees it grow. This
test asserts the tree instead, so the generator has to be fixed rather than
muted.

Deliberately not scoped to a filename list. The failure mode is "a shell
command wrote where it should not have", and the next instance will have a
different name.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Shapes that only appear when a path was mishandled by a shell.
_LITTER = (
    # A variable the shell never expanded: `$tmp`, `${d}`.
    re.compile(r"^\$"),
    # A Windows absolute path flattened into one component. U+F03A is the
    # private-use colon MSYS substitutes on an illegal NTFS name.
    re.compile(r"^[A-Za-z][:]?Users", re.IGNORECASE),
    re.compile(r"AppDataLocalTemp"),
    # A leaked pytest temp root.
    re.compile(r"^pytest-of-"),
)


def _looks_like_litter(name: str) -> bool:
    return any(p.search(name) for p in _LITTER)


def test_no_shell_mangled_paths_in_the_repository_root():
    offenders = sorted(
        entry.name for entry in REPO.iterdir() if _looks_like_litter(entry.name)
    )
    assert offenders == [], (
        "the repository root has collected shell-mangled scratch files: "
        f"{offenders}. A test interpolated a path into a shell command without "
        "quoting it -- see tests/tools/test_approved_command_clean_slate.py "
        "for the fix (as_posix + shlex.quote), and do not add a .gitignore "
        "entry instead."
    )


def test_the_root_holds_no_empty_untracked_scratch_files():
    """An empty file in the root is a sentinel someone forgot to clean up.

    Scoped to files with no extension, so real empty markers with a purpose
    (`py.typed`, `__init__.py`) are not swept up by it.
    """
    offenders = sorted(
        entry.name
        for entry in REPO.iterdir()
        if entry.is_file()
        and entry.stat().st_size == 0
        and not entry.suffix
        and not entry.name.startswith(".")
    )
    assert offenders == [], f"empty scratch files in the repository root: {offenders}"
