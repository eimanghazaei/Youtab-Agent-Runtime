"""WAVE-27 egress-policy static gate.

Fails CI when production code constructs a raw outbound-network client outside the
designated audited adapters, UNLESS that exact call site is listed in
``security/egress_allowlist.json`` with a documented reason. WAVE-28 §6.5 keys the
allowlist on ``(file, symbol, fingerprint)`` — a per-call-site AST hash (see
:func:`_call_fingerprint`) — so an already-approved ``(file, symbol)`` cannot be
silently mutated into an unsafe site (new destination, added ``follow_redirects``,
extra verb) while the gate still passes: any change to the site changes its
fingerprint, which fails as both a stale entry and a new site. This is what makes
"every directly-controlled outbound client routes through the audited boundary"
an *enforced, regression-proof* invariant rather than a one-time sweep:

  * New code must use the audited factories
    (``youtab_runtime.egress_guard_http`` / ``youtab_runtime.egress_adapters`` /
    ``tools.url_safety.create_ssrf_safe_*``) — a bare ``httpx.Client``,
    ``requests.post``, ``aiohttp.ClientSession``, ``urllib.request.urlopen`` or
    ``websockets.connect`` in production trips the gate.
  * Pre-existing sites that cannot be mechanically migrated this wave (untestable
    platform adapters, SDK-internal transports) are enumerated in the allowlist
    with a per-entry {reason, category, owner_action} — never a broad
    package-level exception and never a silent skip. The allowlist IS the
    machine-checkable companion to ``docs/security/EGRESS_EXCEPTIONS.md``.

Usage:
  python -m tools.egress_policy_lint --check            # CI: exit 1 on new violations
  python -m tools.egress_policy_lint --list             # print current findings
  python -m tools.egress_policy_lint --update-baseline  # regenerate the allowlist
                                                        # (dev only; review the diff)
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "security" / "egress_allowlist.json"

#: Production source roots that are scanned (tests / scratch / vendored excluded).
PRODUCTION_DIRS = (
    "agent", "gateway", "tools", "plugins", "youtab_agent_cli", "youtab_runtime",
)
#: Top-level production modules also scanned.
TOP_LEVEL_FILES = ("run_agent.py", "youtab_constants.py", "utils.py")

#: The audited boundary itself — always allowed to construct raw clients (it is
#: what everything else routes through). Paths are repo-relative, forward-slash.
ADAPTER_MODULES = frozenset(
    {
        "tools/url_safety.py",
        "youtab_runtime/egress_guard_http.py",
        "youtab_runtime/egress_adapters.py",
    }
)

#: Directories skipped anywhere in the path.
_SKIP_PARTS = frozenset(
    {".venv", ".venv-qual", "node_modules", "scratchpad", ".claude", "tests",
     "__pycache__", "optional-skills", "website", "build", "dist"}
)

#: Banned constructors keyed by (module-root, attribute-tail). A match means a
#: raw outbound client is being built. ``requests.Session`` and the verb helpers
#: all count.
_HTTPX_CTORS = {"Client", "AsyncClient"}
#: httpx module-level convenience functions each build an ephemeral client and
#: egress, so they are banned exactly like a bare Client construction (symmetry
#: with _REQUESTS_CALLS — a raw ``httpx.get(url)`` is as unaudited as ``httpx.Client()``).
_HTTPX_CALLS = {
    "get", "post", "put", "delete", "patch", "head", "options", "request",
    "stream",
}
_REQUESTS_CALLS = {
    "get", "post", "put", "delete", "patch", "head", "options", "request",
    "Session",
}
_AIOHTTP_CTORS = {"ClientSession"}
_WEBSOCKETS_CALLS = {"connect"}


def _call_fingerprint(node: ast.Call) -> str:
    """A stable structural hash of one banned call site.

    WAVE-28 §6.5: the allowlist keys on (file, symbol, FINGERPRINT), not just
    (file, symbol), so an APPROVED site cannot be silently mutated into an unsafe
    one while the gate still passes. The fingerprint is the sha256 of the call
    node's normalized AST dump with line/col attributes stripped — so it is
    stable across benign line moves, but any change to the call's ARGUMENT
    EXPRESSIONS changes it:
      * a changed destination *expression* — a new literal URL, or swapping the
        argument to a different name/attribute/call;
      * an added/removed/changed keyword (``follow_redirects=True``, ``verify=``,
        a widened ``timeout``);
      * a different call shape (extra args, a wrapped call).
    A changed site's old fingerprint goes stale AND its new fingerprint is
    un-allowlisted — either way the gate fails until the change is re-reviewed and
    re-baselined. Adding a brand-new call (even of an already-allowlisted symbol
    in an already-allowlisted file) is a new fingerprint and also fails.

    Known limit (data-flow, not value): the fingerprint is over the call node's
    AST, so a destination passed as a *variable* (``httpx.get(URL)``) hashes the
    name ``URL``, not its value — re-pointing ``URL``'s definition elsewhere does
    not change this fingerprint. This is a defense-in-depth STATIC gate; the
    runtime connect-time SSRF pin (``tools.url_safety.create_ssrf_safe_*``) is the
    actual destination control. The alias / local-import / from-import evasions
    ARE caught (the scanner resolves those import forms).
    """
    dump = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Finding:
    file: str          # repo-relative, forward-slash
    symbol: str        # e.g. "httpx.AsyncClient", "requests.post", "urlopen"
    line: int
    fingerprint: str   # structural hash of the call site (see _call_fingerprint)

    def key(self) -> str:
        return f"{self.file}::{self.symbol}::{self.fingerprint}"

    def site(self) -> Tuple[str, str, str]:
        return (self.file, self.symbol, self.fingerprint)


class _Scanner(ast.NodeVisitor):
    """Per-file: track outbound-lib imports, then flag banned constructions."""

    def __init__(self) -> None:
        # alias name -> canonical module ("httpx","requests","aiohttp","websockets")
        self.module_alias: Dict[str, str] = {}
        # bound name -> canonical symbol for `from x import y` forms.
        self.symbol_alias: Dict[str, str] = {}
        # `from urllib.request import urlopen` / `import urllib.request`
        self.urlopen_names: Set[str] = set()
        self.urllib_request_alias: Set[str] = set()
        self.findings: List[Tuple[str, int, str]] = []  # (symbol, line, fingerprint)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.name
            asname = alias.asname or name.split(".")[0]
            if name in ("httpx", "requests", "aiohttp", "websockets"):
                self.module_alias[asname] = name
            if name == "urllib.request":
                self.urllib_request_alias.add(alias.asname or "urllib")
            if name == "urllib":
                self.urllib_request_alias.add(alias.asname or "urllib")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if mod == "httpx" and alias.name in (_HTTPX_CTORS | _HTTPX_CALLS):
                self.symbol_alias[bound] = f"httpx.{alias.name}"
            elif mod == "aiohttp" and alias.name in _AIOHTTP_CTORS:
                self.symbol_alias[bound] = f"aiohttp.{alias.name}"
            elif mod == "urllib.request" and alias.name == "urlopen":
                self.urlopen_names.add(bound)
            elif mod == "websockets" and alias.name == "connect":
                self.symbol_alias[bound] = "websockets.connect"
            elif mod == "requests" and alias.name in _REQUESTS_CALLS:
                self.symbol_alias[bound] = f"requests.{alias.name}"
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        sym = self._banned_symbol(node.func)
        if sym is not None:
            self.findings.append((sym, node.lineno, _call_fingerprint(node)))
        self.generic_visit(node)

    def _banned_symbol(self, func: ast.AST) -> Optional[str]:
        # Attribute form: mod.Attr(...) or urllib.request.urlopen(...)
        if isinstance(func, ast.Attribute):
            attr = func.attr
            root = func.value
            # urllib.request.urlopen
            if attr == "urlopen":
                if isinstance(root, ast.Attribute) and root.attr == "request" \
                        and isinstance(root.value, ast.Name) \
                        and root.value.id in self.urllib_request_alias:
                    return "urllib.request.urlopen"
            if isinstance(root, ast.Name):
                canon = self.module_alias.get(root.id)
                if canon == "httpx" and attr in (_HTTPX_CTORS | _HTTPX_CALLS):
                    return f"httpx.{attr}"
                if canon == "requests" and attr in _REQUESTS_CALLS:
                    return f"requests.{attr}"
                if canon == "aiohttp" and attr in _AIOHTTP_CTORS:
                    return f"aiohttp.{attr}"
                if canon == "websockets" and attr in _WEBSOCKETS_CALLS:
                    return "websockets.connect"
        # Bare name form from `from x import y`.
        if isinstance(func, ast.Name):
            if func.id in self.symbol_alias:
                return self.symbol_alias[func.id]
            if func.id in self.urlopen_names:
                return "urllib.request.urlopen"
        return None


def _iter_py_files() -> List[Path]:
    files: List[Path] = []
    for d in PRODUCTION_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if _SKIP_PARTS.intersection(p.parts):
                continue
            files.append(p)
    for f in TOP_LEVEL_FILES:
        p = REPO_ROOT / f
        if p.is_file():
            files.append(p)
    return files


def scan() -> List[Finding]:
    findings: List[Finding] = []
    for path in _iter_py_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ADAPTER_MODULES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        sc = _Scanner()
        sc.visit(tree)
        for sym, line, fp in sc.findings:
            findings.append(Finding(file=rel, symbol=sym, line=line, fingerprint=fp))
    return findings


def _load_allowlist() -> List[dict]:
    if not ALLOWLIST_PATH.exists():
        return []
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return list(data.get("allow", []))


def _allow_counts(allow: List[dict]) -> "Counter[Tuple[str, str, str]]":
    """Multiset of allowed sites (file, symbol, fingerprint) -> occurrences.

    An entry MUST carry a fingerprint. An entry missing one (a pre-fingerprint
    allowlist) matches nothing, so it surfaces as stale and forces a re-baseline
    — a fail-closed migration, never a silent pass.
    """
    ct: "Counter[Tuple[str, str, str]]" = Counter()
    for e in allow:
        fp = e.get("fingerprint")
        if not fp:
            continue
        ct[(e["file"], e["symbol"], fp)] += int(e.get("occurrences", 1))
    return ct


def check() -> int:
    findings = scan()
    found = Counter(f.site() for f in findings)
    allow = _load_allowlist()
    allowed = _allow_counts(allow)
    # Counter subtraction keeps only positive residues:
    #   new   = sites found MORE times than allowed (unknown site, or a mutated
    #           site whose fingerprint no longer matches its old entry, or an
    #           extra occurrence of an approved site);
    #   stale = sites allowed MORE times than found (removed/migrated site, a
    #           now-mismatched fingerprint, or an entry with no fingerprint).
    new = found - allowed
    stale = allowed - found
    # entries that are malformed (no fingerprint) are always reported so the
    # ledger cannot rot into an unenforceable state.
    unfingerprinted = [e for e in allow if not e.get("fingerprint")]
    if not new and not stale and not unfingerprinted:
        print(f"egress-policy-gate OK: {sum(found.values())} known site(s) "
              f"across {len(allowed)} fingerprinted allowlist key(s), 0 new, "
              f"0 stale.")
        return 0
    if new:
        lines_by_site: Dict[Tuple[str, str, str], List[int]] = {}
        for f in findings:
            lines_by_site.setdefault(f.site(), []).append(f.line)
        print("egress-policy-gate FAIL: raw outbound-client construction outside "
              "the audited adapters, OR an approved site was modified (its "
              "fingerprint changed). Use youtab_runtime.egress_guard_http / "
              "egress_adapters / tools.url_safety.create_ssrf_safe_*, or "
              "re-review and re-baseline security/egress_allowlist.json:")
        for site, n in sorted(new.items()):
            f_, sym, fp = site
            lines = sorted(lines_by_site.get(site, []))
            loc = ", ".join(f"{f_}:{ln}" for ln in lines[:n]) or f_
            print(f"  {loc}  {sym}  (fingerprint {fp})")
    if stale:
        print("egress-policy-gate FAIL: stale allowlist entries (site removed, "
              "migrated, or its fingerprint changed — prune/re-review so the "
              "ledger stays honest):")
        for site, n in sorted(stale.items()):
            f_, sym, fp = site
            print(f"  {f_}::{sym}  (fingerprint {fp} x{n})")
    if unfingerprinted:
        print("egress-policy-gate FAIL: allowlist entries without a fingerprint "
              "(unenforceable — re-run --update-baseline):")
        for e in unfingerprinted:
            print(f"  {e.get('file')}::{e.get('symbol')}")
    return 1


def update_baseline() -> int:
    findings = scan()
    # Preserve curated reason/category/owner_action. Prefer an exact
    # (file, symbol, fingerprint) match; fall back to a same-(file, symbol) entry
    # so a re-fingerprinted or newly-split site keeps its human rationale.
    existing = _load_allowlist()
    by_full: Dict[Tuple[str, str, str], dict] = {}
    by_symbol: Dict[Tuple[str, str], dict] = {}
    for e in existing:
        fp = e.get("fingerprint")
        if fp:
            by_full[(e["file"], e["symbol"], fp)] = e
        by_symbol.setdefault((e["file"], e["symbol"]), e)

    counts = Counter(f.site() for f in findings)
    entries = []
    for site in sorted(counts):
        file_, symbol, fp = site
        prev = by_full.get(site) or by_symbol.get((file_, symbol), {})
        entry = {
            "file": file_,
            "symbol": symbol,
            "fingerprint": fp,
            "reason": prev.get("reason", "PENDING: pre-existing raw client; "
                      "not yet migrated to the audited boundary."),
            "category": prev.get("category", "pre_existing_unmigrated"),
            "owner_action": prev.get("owner_action", "PENDING_OWNER_ACTION: "
                            "verify via live integration then migrate or keep."),
        }
        if counts[site] > 1:
            entry["occurrences"] = counts[site]
        entries.append(entry)
    ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALLOWLIST_PATH.write_text(
        json.dumps({"allow": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(entries)} fingerprinted allowlist entries to "
          f"{ALLOWLIST_PATH.relative_to(REPO_ROOT).as_posix()}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="WAVE-27 egress-policy static gate")
    ap.add_argument("--check", action="store_true", help="fail on new/stale sites")
    ap.add_argument("--list", action="store_true", help="print all findings")
    ap.add_argument("--update-baseline", action="store_true",
                    help="regenerate the allowlist (review the diff)")
    args = ap.parse_args(argv)
    if args.update_baseline:
        return update_baseline()
    if args.list:
        for f in sorted(scan(), key=lambda x: (x.file, x.line)):
            print(f"{f.file}:{f.line}  {f.symbol}")
        return 0
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
