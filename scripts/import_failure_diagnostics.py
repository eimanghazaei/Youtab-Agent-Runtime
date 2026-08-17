#!/usr/bin/env python3
"""Diagnostic capture for import-shaped test failures. INSTRUMENTATION ONLY.

Why this exists
---------------
CI run 32003878520 died while *collecting* a test file that does
``import numpy`` at module scope::

    E   ModuleNotFoundError: No module named 'numpy._utils'

Three sibling files that also import numpy at module scope PASSED in the same
job, milliseconds apart, out of the same ``site-packages``. The failure has
never reproduced: 240 concurrent cold numpy imports on a 2-CPU cpuset produced
zero failures, and 146,093 directory listings taken during concurrent
compilation produced zero incomplete listings. The concurrent
``__pycache__``-write hypothesis is REFUTED for CI's installer --
``pip install -e '.[dev,web,slack]'`` byte-compiles at install time, so 406 of
406 numpy ``.pyc`` already existed and no worker had anything to write.

The mechanism that fits the traceback is in CPython's own import machinery:

* ``importlib._bootstrap_external.FileFinder._fill_cache`` maps
  ``FileNotFoundError`` / ``PermissionError`` / ``NotADirectoryError`` raised by
  ``os.listdir(dir)`` to an EMPTY directory listing, and
* ``FileFinder.find_spec`` maps *any* ``OSError`` raised by ``_path_stat(dir)``
  to ``mtime = -1``.

Either path silently turns a module that is present on disk into
``ModuleNotFoundError``, and neither preserves the errno that caused it. By the
time a human reads the CI log the evidence is gone -- which is precisely why
the flake is still UNRESOLVED.

What this module is, and is not
-------------------------------
It is a read-only observer. Given the captured output of a per-file pytest
subprocess, it decides whether the failure was import-shaped and, if so,
renders a report of the filesystem / interpreter state at that moment.

It is NOT a fix, NOT a retry, and NOT a workaround:

* it never re-runs the failing file and never spawns a process;
* it never imports the module that failed -- module resolution here is a
  filesystem walk that mirrors ``FileFinder``, so its answer stays valid even
  if a real import would now succeed (and "it resolves fine now" is itself the
  load-bearing observation);
* it returns text. The caller concatenates that text to output it was already
  going to print. Nothing here can change an exit status.

Secrets
-------
The report reads exactly one environment allowlist (``_ENV_ALLOWLIST``:
runner-identity variables), and every name in it is additionally passed through
:func:`looks_like_credential` before its value is read -- so a credential-shaped
name cannot be emitted even if someone later adds one to the allowlist. The
predicate is deliberately BROADER than the ``_TOKEN``-suffix notion in
``tests/conftest.py``; ``tests/youtab_runtime/test_import_failure_diagnostics.py``
asserts that it is a superset of conftest's live policy, so the two cannot
drift into disagreement.
"""

from __future__ import annotations

import errno as _errno
import os
import platform
import re
import shutil
import stat as _stat
import sys
from dataclasses import dataclass

# ── Report identity ─────────────────────────────────────────────────────────
#: One-line pointer appended to the failing file's captured output. Kept short
#: so it does not crowd the inline failure tail the runner prints.
MARKER = "[import-diagnostics]"

#: Section banner. ASCII-only on purpose: this has to survive cp1252 stdio on
#: native Windows and any log scraper that greps for it.
DIAGNOSTICS_HEADER = "=== import-failure diagnostics (instrumentation only, NOT a fix) ==="

# Bounds. A diagnostic that scrolls a CI log off the top is a diagnostic nobody
# reads, and site-packages can hold hundreds of entries.
_MAX_LISTING_SAMPLE = 60
_MAX_SYS_PATH = 40
_MAX_VALUE_CHARS = 300


# ── Detection ───────────────────────────────────────────────────────────────
# The trigger has to fire on an import-shaped failure and must NOT fire on an
# ordinary assertion failure. Anchoring is what buys that: the exception name
# must sit at a position where only a real exception header can put it.
#
# Real shapes, captured from pytest 9.0.2 (see the module docstring of the
# test file for the verbatim samples):
#
#   collection error       "E   ModuleNotFoundError: No module named 'x'"
#   in-test failure        "E       ImportError: cannot import name 'y' from 'x' (/p)"
#   short summary          "FAILED t.py::t - ImportError: cannot import name ..."
#   pre-pytest crash       "ModuleNotFoundError: No module named 'x'"  (column 0,
#                          under a "Traceback (most recent call last):" header)
#
# An assertion failure renders as "E   AssertionError: ...". Its *message* is
# free text that may itself contain the words ModuleNotFoundError or a whole
# fake traceback, so nothing may match on the bare word anywhere in the output.
_EXC = r"(?:ModuleNotFoundError|ImportError)"
#: Optional dotted qualname, e.g. "builtins.ModuleNotFoundError".
_QUAL = r"(?:[A-Za-z_][\w.]*\.)?"

#: pytest traceback exception line. The exception name must follow the "E"
#: gutter immediately -- "E   AssertionError: ModuleNotFoundError: ..." does
#: not match, because after the gutter comes AssertionError.
_RE_TRACEBACK_LINE = re.compile(
    rf"^E\s+{_QUAL}(?P<exc>{_EXC}): (?P<msg>.*)$", re.MULTILINE
)
#: pytest short-summary line -- the only rendering left when someone runs with
#: ``--tb=no`` / ``--tb=line``. Same anchoring, after the " - " separator.
#:
#: The node id is matched as ``[^\s\[]+`` rather than ``\S+`` so a PARAMETRIZED
#: id cannot forge a match: ``FAILED t.py::test_x[a - ImportError: b]`` would
#: otherwise satisfy ``\S+\s+-\s+ImportError:`` out of the parameter text alone.
#: The cost is that a parametrized test failing with a real ImportError under
#: ``--tb=no`` is not caught by THIS rule; under any traceback style it is
#: caught by the rule above, which is what CI actually runs.
_RE_SUMMARY_LINE = re.compile(
    rf"^(?:ERROR|FAILED)\s+[^\s\[]+\s+-\s+{_QUAL}(?P<exc>{_EXC}): (?P<msg>.*)$",
    re.MULTILINE,
)
#: Raw interpreter traceback at column 0 -- what you get when the process dies
#: before pytest can render anything (a conftest that fails to import, an
#: INTERNALERROR). Only consulted AFTER a real traceback header has been seen,
#: and only for matches positioned after it: captured-stdout sections also sit
#: at column 0, so an unanchored column-0 match would fire on a test that
#: merely *prints* an import error.
_RE_BARE_LINE = re.compile(
    rf"^{_QUAL}(?P<exc>{_EXC}): (?P<msg>.*)$", re.MULTILINE
)
_RAW_TRACEBACK_HEADER = "Traceback (most recent call last):"
#: pytest's failure gutter. Its presence means pytest DID render an exception of
#: its own, so the two anchored rules above have already had their authoritative
#: look at it and rule 3 must stand down -- otherwise a test that fails an
#: assertion while printing a whole fake traceback to stdout would trigger a
#: capture out of its own captured output.
_RE_E_GUTTER = re.compile(r"^E\s", re.MULTILINE)

#: Corroborating evidence, not a trigger on its own.
_RE_COLLECTION_HINT = re.compile(
    r"^ImportError while importing test module ", re.MULTILINE
)

#: "No module named 'numpy._utils'" -> the full dotted name CPython looked for.
_RE_NO_MODULE = re.compile(r"No module named ['\"]([\w.]+)['\"]")
#: "cannot import name 'x' from 'numpy' (/path/to/numpy/__init__.py)"
_RE_CANNOT_IMPORT = re.compile(
    r"cannot import name ['\"](?P<attr>[\w]+)['\"] from "
    r"(?:partially initialized module )?['\"](?P<module>[\w.]+)['\"]"
    r"(?:\s*\((?P<path>[^)]*)\))?"
)


@dataclass(frozen=True)
class ImportFailureSignal:
    """What the detector found. Purely descriptive."""

    exc_type: str
    message: str
    #: The dotted module name the import machinery was looking for, when the
    #: message shape allows extracting it. ``None`` is not fatal -- the report
    #: still carries interpreter and runner state.
    module: str | None
    #: For ``cannot import name X from Y``: the attribute, and the path pytest
    #: reported for Y.
    attribute: str | None
    reported_path: str | None
    #: Which anchored rule matched, and the line it matched. Both are printed
    #: so a false positive is self-evident in the log rather than mysterious.
    rule: str
    evidence: str
    #: True when pytest's own "ImportError while importing test module" banner
    #: is present, i.e. the failure happened during collection.
    at_collection: bool


def detect_import_failure(output: str) -> ImportFailureSignal | None:
    """Return a signal if *output* contains an import-shaped failure, else None.

    ``output`` is the combined stdout/stderr of one ``python -m pytest <file>``
    subprocess.

    The three rules are tried most-specific first. Every one of them requires
    ``ModuleNotFoundError``/``ImportError`` at an *anchored* position, so an
    ``AssertionError`` whose message quotes an import error does not match --
    that discrimination is the whole point and is asserted directly by
    ``test_diagnostics_absent_on_a_plain_assertion_failure``.

    Known residual: output that contains a real ``Traceback (most recent call
    last):`` header, a column-0 import-error line, and NO pytest failure gutter
    at all would match rule 3. In practice that is an interpreter-level import
    crash, which is what rule 3 is for. The worst case if it is ever wrong is
    extra log output -- a false positive cannot change an exit status.
    """
    if not output:
        return None

    at_collection = bool(_RE_COLLECTION_HINT.search(output))

    match = _RE_TRACEBACK_LINE.search(output)
    rule = "pytest-traceback-exception-line"
    if match is None:
        match = _RE_SUMMARY_LINE.search(output)
        rule = "pytest-short-summary-line"
    if match is None:
        if _RE_E_GUTTER.search(output):
            # pytest rendered an exception and it was not an import error.
            return None
        header = output.find(_RAW_TRACEBACK_HEADER)
        if header == -1:
            return None
        match = _RE_BARE_LINE.search(output, header + len(_RAW_TRACEBACK_HEADER))
        rule = "raw-interpreter-traceback"
    if match is None:
        return None

    message = match.group("msg").strip()
    module: str | None = None
    attribute: str | None = None
    reported_path: str | None = None

    no_module = _RE_NO_MODULE.search(message)
    if no_module is not None:
        module = no_module.group(1)
    else:
        cannot = _RE_CANNOT_IMPORT.search(message)
        if cannot is not None:
            module = cannot.group("module")
            attribute = cannot.group("attr")
            reported_path = cannot.group("path")

    return ImportFailureSignal(
        exc_type=match.group("exc"),
        message=message,
        module=module,
        attribute=attribute,
        reported_path=reported_path,
        rule=rule,
        evidence=match.group(0).strip(),
        at_collection=at_collection,
    )


# ── Credential policy ───────────────────────────────────────────────────────
# Same idea as ``_looks_like_credential`` in tests/conftest.py (a name-shape
# test, keyed on families like the ``_TOKEN`` suffix), deliberately widened to
# substring matching so it is a strict superset. Substrings, not suffixes,
# because this side has no business emitting *any* variable whose name mentions
# a secret-shaped word in any position.
_CREDENTIAL_SUBSTRINGS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "CREDENTIAL",
    "KEY",
    "AUTH",
    "SIGNATURE",
    "COOKIE",
    "BEARER",
    "SESSION",
    "PRIVATE",
    "CERT",
    "SALT",
    # conftest classes provider base URLs as credential-shaped; nothing here
    # needs a URL, so the whole family goes.
    "URL",
)


def looks_like_credential(name: str) -> bool:
    """True if an env var name is credential-shaped and must never be emitted.

    Superset of ``tests/conftest.py::_looks_like_credential`` -- proven against
    conftest's live constants by
    ``test_credential_policy_is_a_superset_of_the_conftest_policy``.
    """
    upper = name.upper()
    return any(token in upper for token in _CREDENTIAL_SUBSTRINGS)


#: The ONLY environment variables this module reads. Runner identity and
#: interpreter location -- the two things the report cannot derive from the
#: process itself. Every one is still filtered by looks_like_credential().
_ENV_ALLOWLIST = frozenset(
    {
        "CI",
        "GITHUB_ACTIONS",
        "GITHUB_JOB",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_ID",
        "GITHUB_WORKFLOW",
        "ImageOS",
        "ImageVersion",
        "PYTHONPATH",
        "RUNNER_ARCH",
        "RUNNER_OS",
        "VIRTUAL_ENV",
    }
)


def safe_env_snapshot(
    allowlist: frozenset[str] | set[str] = _ENV_ALLOWLIST,
) -> list[tuple[str, str]]:
    """Allowlisted, credential-filtered ``(name, value)`` pairs from the environment.

    Two independent gates: the name must be in *allowlist* AND must not look
    like a credential. The second gate is what makes a future careless addition
    to the allowlist harmless.
    """
    out: list[tuple[str, str]] = []
    for name in sorted(allowlist):
        if looks_like_credential(name):
            continue
        value = os.environ.get(name)
        if value is None:
            continue
        out.append((name, _truncate(value)))
    return out


def _truncate(value: str, limit: int = _MAX_VALUE_CHARS) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... (+{len(value) - limit} chars)"


# ── Filesystem-level probes (no imports, no subprocesses) ───────────────────


def _oserror_detail(exc: OSError) -> str:
    """errno name + numeric value + strerror, plus winerror where present."""
    name = _errno.errorcode.get(exc.errno or 0, "?")
    detail = f"errno={exc.errno} ({name}) strerror={exc.strerror!r}"
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        detail += f" winerror={winerror}"
    if exc.filename:
        detail += f" filename={exc.filename!r}"
    return detail


def _stat_line(path: str) -> str:
    """``os.stat`` of *path*, or the exact errno that refused it.

    ``st_mtime_ns`` is the field ``FileFinder`` compares against its cached
    listing, and ``FileFinder.find_spec`` substitutes ``-1`` for it when this
    call raises anything at all. That substitution is the suspected mechanism,
    so the real value is worth a line of log.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        return f"os.stat FAILED: {_oserror_detail(exc)}"
    return (
        f"mode={_stat.filemode(st.st_mode)} ino={st.st_ino} dev={st.st_dev} "
        f"nlink={st.st_nlink} uid={st.st_uid} gid={st.st_gid} "
        f"size={st.st_size} mtime_ns={st.st_mtime_ns} ctime_ns={st.st_ctime_ns}"
    )


def _listdir(path: str) -> tuple[list[str] | None, str]:
    """``os.listdir`` with the errno preserved.

    ``FileFinder._fill_cache`` swallows FileNotFoundError, PermissionError and
    NotADirectoryError here and proceeds with an EMPTY listing -- a module that
    exists then simply is not found. If this call is going to fail, the errno is
    the single most valuable field in the whole report.
    """
    try:
        return sorted(os.listdir(path)), "ok"
    except OSError as exc:
        return None, f"FAILED: {_oserror_detail(exc)}"


def _mount_for(path: str) -> str:
    """Longest-prefix match of *path* in /proc/mounts (Linux only).

    overlayfs versus ext4 versus a 9p/drvfs share is a first-order suspect for a
    transient ``listdir`` error, and it is not otherwise recoverable from a log.
    """
    if not os.path.exists("/proc/mounts"):
        return "unavailable (no /proc/mounts)"
    best = ""
    best_line = ""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 4:
                    continue
                mountpoint = fields[1].replace("\\040", " ")
                if path.startswith(mountpoint) and len(mountpoint) > len(best):
                    best = mountpoint
                    best_line = (
                        f"device={fields[0]} mountpoint={mountpoint} "
                        f"fstype={fields[2]} options={fields[3]}"
                    )
    except OSError as exc:
        return f"unavailable ({_oserror_detail(exc)})"
    return best_line or "no matching mountpoint"


def _open_fd_count() -> str:
    try:
        return str(len(os.listdir("/proc/self/fd")))
    except OSError:
        return "unavailable"


def _rlimit_nofile() -> str:
    """Soft/hard NOFILE limit.

    EMFILE/ENFILE is an OSError like any other, so exhausting the descriptor
    table is one of the ways ``_path_stat`` gets mapped to ``mtime = -1``.
    """
    try:
        import resource  # noqa: PLC0415 -- POSIX-only, imported on demand
    except ImportError:
        return "unavailable (no resource module)"
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return f"soft={soft} hard={hard}"


# ── Module resolution: a FileFinder-shaped walk that imports nothing ────────


@dataclass(frozen=True)
class _ResolvedStep:
    dotted: str
    kind: str  # package | namespace-package | module | extension | MISSING
    location: str | None
    #: Directory whose listing the NEXT component's lookup would come from.
    next_search: str | None
    searched: int


def _find_component(name: str, search_paths: list[str]) -> _ResolvedStep:
    """Locate one dotted component by looking at the filesystem, not sys.modules.

    Mirrors what ``FileFinder`` does for a single directory entry: package dir
    with ``__init__``, then source module, then extension module, then a
    namespace-package directory. Deliberately does not import anything, so the
    answer is "what is on disk right now" rather than "what the import system
    is willing to do about it".
    """
    from importlib.machinery import (  # noqa: PLC0415 -- stdlib, failure path only
        EXTENSION_SUFFIXES,
        SOURCE_SUFFIXES,
    )

    searched = 0
    for entry in search_paths:
        searched += 1
        base = os.path.join(entry or os.getcwd(), name)
        for suffix in SOURCE_SUFFIXES:
            init = os.path.join(base, "__init__" + suffix)
            if os.path.isfile(init):
                return _ResolvedStep(name, "package", init, base, searched)
        for suffix in (*SOURCE_SUFFIXES, *EXTENSION_SUFFIXES):
            candidate = base + suffix
            if os.path.isfile(candidate):
                kind = "extension" if suffix in EXTENSION_SUFFIXES else "module"
                return _ResolvedStep(name, kind, candidate, None, searched)
        if os.path.isdir(base):
            return _ResolvedStep(name, "namespace-package", base, base, searched)
    return _ResolvedStep(name, "MISSING", None, None, searched)


def _resolve_chain(dotted: str, search_paths: list[str]) -> list[_ResolvedStep]:
    """Walk ``a.b.c`` component by component, stopping at the first miss."""
    steps: list[_ResolvedStep] = []
    current = list(search_paths)
    prefix: list[str] = []
    for part in dotted.split("."):
        prefix.append(part)
        step = _find_component(part, current)
        steps.append(
            _ResolvedStep(
                ".".join(prefix), step.kind, step.location, step.next_search, step.searched
            )
        )
        if step.next_search is None:
            break
        current = [step.next_search]
    return steps


def _distribution_of(top_level: str) -> str:
    """Installed distribution version for a top-level import name.

    Reads ``*.dist-info`` metadata off disk -- it does not import the package,
    which matters when the package is exactly the thing that would not import.
    """
    from importlib import metadata  # noqa: PLC0415 -- failure path only

    names: list[str] = []
    try:
        return _describe_dist(metadata.distribution(top_level))
    except Exception:  # noqa: BLE001 -- any metadata problem must not mask the report
        pass
    try:
        names = list(metadata.packages_distributions().get(top_level, []))
    except Exception:  # noqa: BLE001
        names = []
    for name in names:
        try:
            return _describe_dist(metadata.distribution(name))
        except Exception:  # noqa: BLE001
            continue
    return f"no installed distribution found for import name {top_level!r}"


def _describe_dist(dist: object) -> str:
    name = getattr(dist, "name", "?")
    version = getattr(dist, "version", "?")
    where = getattr(dist, "_path", None)
    if where is None:
        try:
            where = dist.locate_file("")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            where = "?"
    return f"{name} {version}  (metadata: {where})"


# ── Rendering ───────────────────────────────────────────────────────────────


class _Lines:
    """Tiny accumulator; keeps the render functions readable."""

    def __init__(self) -> None:
        self._out: list[str] = []

    def section(self, title: str) -> None:
        self._out.append(f"  {title}")

    def field(self, key: str, value: object) -> None:
        self._out.append(f"    {key:<22} {value}")

    def raw(self, text: str) -> None:
        self._out.append(f"    {text}")

    def blank(self) -> None:
        self._out.append("")

    def text(self) -> str:
        return "\n".join(self._out)


def _render_directory(lines: _Lines, label: str, path: str, target: str | None) -> None:
    """The cached-listing report for one directory.

    This is the directory whose ``FileFinder`` cache the failed lookup would
    have consulted: for ``numpy._utils`` that is ``.../site-packages/numpy``.
    """
    lines.section(f"{label}: {path}")
    lines.field("os.stat", _stat_line(path))
    entries, status = _listdir(path)
    if entries is None:
        lines.field("os.listdir", status)
    else:
        lines.field("os.listdir", f"ok, {len(entries)} entries")
        if target:
            related = [
                e for e in entries if e == target or e.startswith(target + ".")
            ]
            lines.field(f"entries matching {target!r}", related or "NONE")
            for candidate in _expected_filenames(path, target):
                lines.field(f"expected {os.path.basename(candidate)}", _stat_line(candidate))
        sample = entries[:_MAX_LISTING_SAMPLE]
        suffix = "" if len(entries) <= _MAX_LISTING_SAMPLE else f" (of {len(entries)})"
        lines.field(f"listing sample{suffix}", ", ".join(sample))
        cache = os.path.join(path, "__pycache__")
        cache_entries, cache_status = _listdir(cache)
        if cache_entries is None:
            lines.field("__pycache__", cache_status)
        else:
            lines.field("__pycache__", f"{len(cache_entries)} entries")
    try:
        usage = shutil.disk_usage(path)
        free_pct = (usage.free / usage.total * 100) if usage.total else 0.0
        lines.field(
            "disk",
            f"total={usage.total} used={usage.used} free={usage.free} "
            f"({free_pct:.1f}% free)",
        )
    except OSError as exc:
        lines.field("disk", f"shutil.disk_usage FAILED: {_oserror_detail(exc)}")
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        lines.field("statvfs", "unavailable on this platform (Windows)")
    else:
        try:
            vfs = statvfs(path)
        except OSError as exc:
            lines.field("statvfs", f"FAILED: {_oserror_detail(exc)}")
        else:
            lines.field(
                "statvfs blocks",
                f"bsize={vfs.f_bsize} frsize={vfs.f_frsize} blocks={vfs.f_blocks} "
                f"bfree={vfs.f_bfree} bavail={vfs.f_bavail}",
            )
            inode_used = vfs.f_files - vfs.f_ffree if vfs.f_files else 0
            inode_pct = (inode_used / vfs.f_files * 100) if vfs.f_files else 0.0
            lines.field(
                "statvfs inodes",
                f"files={vfs.f_files} ffree={vfs.f_ffree} favail={vfs.f_favail} "
                f"used={inode_used} ({inode_pct:.1f}%)",
            )
            lines.field("statvfs flags", f"flag={vfs.f_flag} namemax={vfs.f_namemax}")
    lines.field("mount", _mount_for(path))
    lines.field(
        "access",
        f"R_OK={os.access(path, os.R_OK)} X_OK={os.access(path, os.X_OK)}",
    )


def _expected_filenames(directory: str, target: str) -> list[str]:
    """The exact paths ``FileFinder`` would have accepted for *target*."""
    from importlib.machinery import (  # noqa: PLC0415 -- stdlib, failure path only
        EXTENSION_SUFFIXES,
        SOURCE_SUFFIXES,
    )

    out = [os.path.join(directory, target, "__init__.py")]
    for suffix in (*SOURCE_SUFFIXES, *EXTENSION_SUFFIXES):
        out.append(os.path.join(directory, target + suffix))
    return out


def render_diagnostics(
    signal: ImportFailureSignal,
    *,
    source: str,
    search_paths: list[str] | None = None,
) -> str:
    """Render the full report for *signal*. Pure text; no side effects."""
    paths = list(search_paths if search_paths is not None else sys.path)
    lines = _Lines()

    lines.section(f"trigger ({source})")
    lines.field("matched rule", signal.rule)
    lines.field("matched line", signal.evidence)
    lines.field("exception", signal.exc_type)
    lines.field("message", _truncate(signal.message))
    lines.field("module looked up", signal.module or "(not parseable from message)")
    if signal.attribute:
        lines.field("missing attribute", signal.attribute)
    if signal.reported_path:
        lines.field("path pytest reported", signal.reported_path)
    lines.field("failed during", "collection" if signal.at_collection else "test call")
    lines.blank()

    lines.section("interpreter")
    lines.field("sys.executable", sys.executable)
    lines.field("version", sys.version.replace("\n", " "))
    lines.field("version_info", tuple(sys.version_info))
    lines.field(
        "implementation",
        f"{platform.python_implementation()} {platform.python_version()} "
        f"(build={platform.python_build()}, compiler={platform.python_compiler()})",
    )
    lines.field("prefix", sys.prefix)
    lines.field("base_prefix", sys.base_prefix)
    lines.field("dont_write_bytecode", sys.dont_write_bytecode)
    lines.field("pycache_prefix", sys.pycache_prefix)
    lines.field("search path entries", len(paths))
    for entry in paths[:_MAX_SYS_PATH]:
        lines.raw(f"  path[] {entry}")
    if len(paths) > _MAX_SYS_PATH:
        lines.raw(f"  path[] ... (+{len(paths) - _MAX_SYS_PATH} more)")
    lines.blank()

    if signal.module:
        top_level = signal.module.split(".")[0]
        lines.section("distribution")
        lines.field(top_level, _distribution_of(top_level))
        lines.blank()

        lines.section("module resolution (filesystem walk; nothing was imported)")
        steps = _resolve_chain(signal.module, paths)
        for step in steps:
            lines.field(
                step.dotted,
                f"{step.kind} at {step.location or '-'} "
                f"(after {step.searched} search path entr"
                f"{'y' if step.searched == 1 else 'ies'})",
            )
        lines.blank()

        # The directory whose cached listing the failed lookup used: the parent
        # package's directory for a dotted name, else every search root.
        parent_steps = steps[:-1]
        target = signal.module.rsplit(".", 1)[-1]
        parent_dir = parent_steps[-1].next_search if parent_steps else None
        if parent_dir:
            _render_directory(
                lines, "cached-listing directory (parent package)", parent_dir, target
            )
        else:
            hits = [p for p in paths if p and os.path.isdir(os.path.join(p, target))]
            hits += [
                p
                for p in paths
                if p and os.path.isfile(os.path.join(p, target + ".py"))
            ]
            for candidate in list(dict.fromkeys(hits))[:2]:
                _render_directory(
                    lines, "cached-listing directory (search root)", candidate, target
                )
            if not hits:
                lines.section(
                    f"cached-listing directory: no search root currently holds {target!r}"
                )
                for candidate in _site_dirs()[:2]:
                    _render_directory(
                        lines, "search root (site-packages)", candidate, target
                    )
        lines.blank()

        verdict = _verdict(steps)
        lines.section("verdict (post-failure re-check, NOT a fix)")
        for line in verdict:
            lines.raw(line)
        lines.blank()

    lines.section("runner")
    lines.field("platform", platform.platform())
    lines.field("machine / arch", f"{platform.machine()} / {platform.architecture()[0]}")
    lines.field("processor", platform.processor() or "(unreported)")
    lines.field("system / release", f"{platform.system()} {platform.release()}")
    lines.field("libc", " ".join(platform.libc_ver()) or "(unreported)")
    lines.field("os.name / sys.platform", f"{os.name} / {sys.platform}")
    lines.field("cpu_count", os.cpu_count())
    affinity = getattr(os, "sched_getaffinity", None)
    lines.field("sched_getaffinity", len(affinity(0)) if affinity else "unavailable")
    try:
        lines.field("loadavg", os.getloadavg())
    except (OSError, AttributeError):
        lines.field("loadavg", "unavailable")
    lines.field("pid / ppid", f"{os.getpid()} / {os.getppid()}")
    lines.field("open fds", _open_fd_count())
    lines.field("RLIMIT_NOFILE", _rlimit_nofile())
    lines.field("cwd", os.getcwd())
    for name, value in safe_env_snapshot():
        lines.field(f"env {name}", value)
    lines.field(
        "env policy",
        f"{len(_ENV_ALLOWLIST)}-name allowlist, credential-shaped names dropped",
    )

    body = lines.text()
    return f"{DIAGNOSTICS_HEADER}\n{body}\n{MARKER} end of capture for {source}"


def _site_dirs() -> list[str]:
    import sysconfig  # noqa: PLC0415 -- failure path only

    paths = sysconfig.get_paths()
    out: list[str] = []
    for key in ("purelib", "platlib"):
        value = paths.get(key)
        if value and value not in out:
            out.append(value)
    return out


def _verdict(steps: list[_ResolvedStep]) -> list[str]:
    """State plainly whether the module is on disk *now*.

    This is triage, not a diagnosis: a module that resolves on a post-failure
    re-check is consistent with the transient-lookup mechanism described in the
    module docstring, and rules out "it was never installed". It does not
    identify the cause, and nothing here makes the failure go away.
    """
    missing = [s for s in steps if s.kind == "MISSING"]
    if not missing:
        return [
            "every component of the failed import resolves on disk NOW.",
            "consistent with a transient lookup failure (see FileFinder notes in",
            "scripts/import_failure_diagnostics.py); NOT evidence of a fix, and NOT",
            "a reason to retry -- the flake is still unresolved.",
        ]
    return [
        f"{missing[0].dotted!r} is genuinely absent from every search path.",
        "this looks like a real missing dependency / wrong interpreter, not the",
        "transient numpy._utils flake. Check the install step before the runner.",
    ]


def capture(
    output: str,
    *,
    source: str,
    search_paths: list[str] | None = None,
) -> tuple[ImportFailureSignal, str] | None:
    """Detect and render in one call.

    Returns ``(signal, report_text)`` for an import-shaped failure, else
    ``None``. Every exception is contained: a diagnostic that crashes the runner
    would be strictly worse than no diagnostic at all, and the caller's exit
    status must not depend on anything in here.
    """
    try:
        signal = detect_import_failure(output)
        if signal is None:
            return None
        return signal, render_diagnostics(
            signal, source=source, search_paths=search_paths
        )
    except Exception as exc:  # noqa: BLE001 -- see docstring
        # Say so once, on stderr, and get out of the way. The caller keeps its
        # own exit status either way.
        sys.stderr.write(
            f"{MARKER} diagnostics capture failed ({type(exc).__name__}: {exc}); "
            "the underlying test failure is unaffected\n"
        )
        return None
