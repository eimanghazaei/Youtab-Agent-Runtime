#!/usr/bin/env python3
"""A06 / LLM supply-chain CVE gate: fail on any known-vulnerable installed dependency.

Integrity is not the same as safety. ``uv.lock`` and ``package-lock.json`` prove a
dependency is the exact artifact we pinned (``dependency_gate.py``); they say
nothing about whether that pinned artifact carries a *known vulnerability*. A
pinned-but-vulnerable dependency passes every other Youtab gate. This gate closes
that gap: it runs ``pip-audit`` against the environment that actually executes and
fails on any advisory that is not explicitly, and non-expiredly, triaged.

The allowlist is the honesty mechanism. An accepted or currently-unfixable CVE
must be named, justified, and given an ``expires`` date. A matching allowlist
entry suppresses a finding *only while it has not expired*, so a triaged CVE can
never silently rot into an untracked one — the day the entry expires, the finding
blocks again. An allowlist entry that matches nothing (the CVE was fixed, or the
dependency dropped) is reported as ``stale`` so the list can be pruned, but never
counts as a pass on its own.

Design note (testability): the network/subprocess step (:func:`run_pip_audit`)
and the policy step (:func:`evaluate`) are separate. ``evaluate`` is a pure
function over parsed findings + allowlist + a date, so the gate's decision logic
is covered by state-based tests without touching the network.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Finding:
    """One (package, vulnerability) pair reported by pip-audit."""

    package: str
    version: str
    vuln_id: str
    aliases: tuple[str, ...] = ()
    fix_versions: tuple[str, ...] = ()
    description: str = ""

    @property
    def identifiers(self) -> frozenset[str]:
        """Every id this finding can be triaged under (primary id + aliases)."""
        return frozenset({self.vuln_id, *self.aliases})


@dataclass(frozen=True)
class AllowEntry:
    """A single triaged exception.

    Match is by ``id`` (the vuln id or any of its aliases, e.g. ``CVE-...`` or
    ``GHSA-...`` or ``PYSEC-...``) and/or by ``package`` (covers every advisory
    for that distribution — the coarse knob for a package with many open CVEs).
    At least one of ``id``/``package`` must be set. ``reason`` and ``expires``
    are mandatory: an exception with no justification or no end date is not an
    exception, it is a hole.
    """

    reason: str
    expires: _dt.date
    id: Optional[str] = None
    package: Optional[str] = None

    def matches(self, finding: Finding) -> bool:
        if self.id is not None and self.id not in finding.identifiers:
            return False
        if self.package is not None and self.package.lower() != finding.package.lower():
            return False
        # An entry with neither id nor package is rejected at load time, so here
        # at least one selector is present and every present selector matched.
        return True

    def is_expired(self, today: _dt.date) -> bool:
        return today > self.expires


@dataclass
class Report:
    """The outcome of evaluating findings against the allowlist."""

    blocking: list[Finding] = field(default_factory=list)
    suppressed: list[tuple[Finding, AllowEntry]] = field(default_factory=list)
    expired_hits: list[tuple[Finding, AllowEntry]] = field(default_factory=list)
    stale_entries: list[AllowEntry] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.blocking


class CveAuditError(RuntimeError):
    """A configuration or tooling error. Never a silent pass."""


# --------------------------------------------------------------------------- #
# Parsing pip-audit
# --------------------------------------------------------------------------- #
def parse_pip_audit(data: Any) -> list[Finding]:
    """Turn a parsed pip-audit ``--format json`` document into Findings.

    pip-audit emits ``{"dependencies": [{"name", "version", "vulns": [...]}, ...]}``
    (older versions emit a bare list). Both shapes are accepted.
    """
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    if not isinstance(deps, list):
        raise CveAuditError("pip-audit output has no 'dependencies' list")
    findings: list[Finding] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name", "")).strip()
        version = str(dep.get("version", "")).strip()
        for vuln in dep.get("vulns", []) or []:
            if not isinstance(vuln, dict):
                continue
            findings.append(
                Finding(
                    package=name,
                    version=version,
                    vuln_id=str(vuln.get("id", "")).strip(),
                    aliases=tuple(str(a) for a in (vuln.get("aliases") or [])),
                    fix_versions=tuple(str(f) for f in (vuln.get("fix_versions") or [])),
                    description=str(vuln.get("description", "")),
                )
            )
    return findings


def run_pip_audit(
    python_exe: str,
    *,
    requirement: Optional[Path] = None,
    extra_args: tuple[str, ...] = (),
) -> list[Finding]:
    """Invoke pip-audit and return parsed findings.

    Audits the interpreter's installed environment by default. Pass
    ``requirement`` to audit a locked requirements/`uv.lock`-exported file
    instead. A pip-audit that cannot run (missing, crashed, unparseable) raises
    :class:`CveAuditError` — not being able to check is never a pass.
    """
    cmd = [python_exe, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"]
    if requirement is not None:
        cmd += ["--requirement", str(requirement)]
    cmd += list(extra_args)
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", check=False
        )
    except FileNotFoundError as exc:  # interpreter missing
        raise CveAuditError(f"could not launch pip-audit: {exc}") from exc
    stdout = completed.stdout or ""
    # pip-audit exits 1 when it finds vulnerabilities *and* 0 when clean; both
    # print the JSON report to stdout. A non-JSON stdout means the tool itself
    # failed (e.g. not installed), which must fail closed.
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CveAuditError(
            "pip-audit did not emit JSON (is it installed? "
            f"exit={completed.returncode}). stderr: {(completed.stderr or '').strip()[:500]}"
        ) from exc
    return parse_pip_audit(document)


# --------------------------------------------------------------------------- #
# Allowlist
# --------------------------------------------------------------------------- #
def load_allowlist(path: Path) -> list[AllowEntry]:
    """Read the TOML allowlist. A missing file is an empty allowlist.

    Schema (per ``[[ignore]]`` table): ``reason`` (str, required), ``expires``
    (``YYYY-MM-DD`` or a TOML date, required), and at least one of ``id`` (str)
    or ``package`` (str).
    """
    if not path.exists():
        return []
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CveAuditError(f"allowlist {path} could not be read: {exc}") from exc
    entries: list[AllowEntry] = []
    for i, item in enumerate(raw.get("ignore", [])):
        if not isinstance(item, dict):
            raise CveAuditError(f"allowlist entry #{i} is not a table")
        reason = str(item.get("reason", "")).strip()
        if not reason:
            raise CveAuditError(f"allowlist entry #{i} has no 'reason'")
        expires = _coerce_date(item.get("expires"), i)
        ident = item.get("id")
        package = item.get("package")
        if not ident and not package:
            raise CveAuditError(
                f"allowlist entry #{i} must set at least one of 'id' or 'package'"
            )
        entries.append(
            AllowEntry(
                reason=reason,
                expires=expires,
                id=str(ident) if ident else None,
                package=str(package) if package else None,
            )
        )
    return entries


def _coerce_date(value: Any, index: int) -> _dt.date:
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return _dt.date.fromisoformat(value.strip())
        except ValueError as exc:
            raise CveAuditError(
                f"allowlist entry #{index} 'expires' is not YYYY-MM-DD: {value!r}"
            ) from exc
    raise CveAuditError(f"allowlist entry #{index} has no valid 'expires' date")


# --------------------------------------------------------------------------- #
# Policy (pure)
# --------------------------------------------------------------------------- #
def evaluate(
    findings: list[Finding],
    allowlist: list[AllowEntry],
    today: _dt.date,
) -> Report:
    """Decide which findings block. Pure — no I/O, deterministic on ``today``.

    A finding is suppressed only by a *non-expired* matching entry. A matching
    entry that has expired does not suppress; the finding blocks and the pair is
    also surfaced under ``expired_hits`` so the operator sees why. An entry that
    matches no finding is ``stale``.
    """
    report = Report()
    used: set[int] = set()
    for finding in findings:
        valid_match: Optional[AllowEntry] = None
        expired_match: Optional[AllowEntry] = None
        for idx, entry in enumerate(allowlist):
            if not entry.matches(finding):
                continue
            used.add(idx)
            if entry.is_expired(today):
                expired_match = expired_match or entry
            else:
                valid_match = entry
                break
        if valid_match is not None:
            report.suppressed.append((finding, valid_match))
        else:
            report.blocking.append(finding)
            if expired_match is not None:
                report.expired_hits.append((finding, expired_match))
    report.stale_entries = [
        entry for idx, entry in enumerate(allowlist) if idx not in used
    ]
    return report


# --------------------------------------------------------------------------- #
# Rendering + CLI
# --------------------------------------------------------------------------- #
def render_json(report: Report) -> dict[str, Any]:
    def f(finding: Finding) -> dict[str, Any]:
        return {
            "package": finding.package,
            "version": finding.version,
            "id": finding.vuln_id,
            "aliases": list(finding.aliases),
            "fix_versions": list(finding.fix_versions),
        }

    return {
        "schema_version": 1,
        "passed": report.passed,
        "blocking_count": len(report.blocking),
        "suppressed_count": len(report.suppressed),
        "blocking": [f(x) for x in report.blocking],
        "suppressed": [
            {**f(x), "reason": e.reason, "expires": e.expires.isoformat()}
            for x, e in report.suppressed
        ],
        "expired_hits": [
            {**f(x), "reason": e.reason, "expires": e.expires.isoformat()}
            for x, e in report.expired_hits
        ],
        "stale_allowlist_entries": [
            {"id": e.id, "package": e.package, "reason": e.reason,
             "expires": e.expires.isoformat()}
            for e in report.stale_entries
        ],
    }


def _print_human(report: Report) -> None:
    if report.blocking:
        print(f"CVE gate: {len(report.blocking)} un-triaged vulnerability(ies):",
              file=sys.stderr)
        for x in report.blocking:
            fix = ", ".join(x.fix_versions) or "no fixed version listed"
            print(f"  - {x.package} {x.version}: {x.vuln_id} "
                  f"({'/'.join(x.aliases) or 'no aliases'}) -> fix: {fix}",
                  file=sys.stderr)
    for x, e in report.expired_hits:
        print(f"  ! allowlist entry for {x.vuln_id} EXPIRED {e.expires} "
              f"({e.reason}) — no longer suppresses", file=sys.stderr)
    for e in report.stale_entries:
        print(f"  ~ stale allowlist entry (matches nothing): "
              f"id={e.id} package={e.package}", file=sys.stderr)
    if report.passed:
        print(f"CVE gate: PASS ({len(report.suppressed)} triaged/suppressed, "
              f"0 blocking)")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter whose environment to audit")
    parser.add_argument("--allowlist", type=Path,
                        default=Path(__file__).with_name("cve_allowlist.toml"))
    parser.add_argument("--requirement", type=Path, default=None,
                        help="audit a locked requirements file instead of the env")
    parser.add_argument("--no-deps", dest="no_deps", action="store_true",
                        help="audit exactly the pins in --requirement without "
                             "re-resolving (the uv export already carries the "
                             "full closure; avoids downloading/building wheels)")
    parser.add_argument("--disable-pip", dest="disable_pip", action="store_true",
                        help="do not invoke pip at all — audit the fully-pinned "
                             "--requirement set directly against the advisory DB. "
                             "Requires --no-deps. Without this, pip-audit still "
                             "spins up a venv and runs pip to enumerate the "
                             "requirements, which fails on a locked pin whose "
                             "Requires-Python excludes the runner's interpreter "
                             "(a resolver artifact, not a real vulnerability). "
                             "Since the uv lock is already the full pinned "
                             "closure, no pip step is needed.")
    parser.add_argument("--output", type=Path, help="write the JSON report here")
    parser.add_argument("--today", type=str, default=None,
                        help="override 'today' (YYYY-MM-DD) for allowlist expiry")
    args = parser.parse_args(argv)

    today = _dt.date.fromisoformat(args.today) if args.today else _dt.date.today()
    try:
        allowlist = load_allowlist(args.allowlist)
        if args.disable_pip and not args.no_deps:
            raise CveAuditError("--disable-pip requires --no-deps")
        extra = []
        if args.no_deps:
            extra.append("--no-deps")
        if args.disable_pip:
            extra.append("--disable-pip")
        extra_args = tuple(extra)
        findings = run_pip_audit(
            args.python, requirement=args.requirement, extra_args=extra_args
        )
    except CveAuditError as exc:
        print(f"CVE gate: ERROR (fail-closed): {exc}", file=sys.stderr)
        return 2
    report = evaluate(findings, allowlist, today)
    payload = render_json(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
    _print_human(report)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
