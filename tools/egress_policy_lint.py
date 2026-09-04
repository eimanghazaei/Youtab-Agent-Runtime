"""WAVE-27 egress-policy static gate.

Fails CI when production code constructs a raw outbound-network client outside the
designated audited adapters, UNLESS that exact (file, symbol) site is listed in
``security/egress_allowlist.json`` with a documented reason. This is what makes
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
import json
import sys
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
_REQUESTS_CALLS = {
    "get", "post", "put", "delete", "patch", "head", "options", "request",
    "Session",
}
_AIOHTTP_CTORS = {"ClientSession"}
_WEBSOCKETS_CALLS = {"connect"}


@dataclass(frozen=True)
class Finding:
    file: str          # repo-relative, forward-slash
    symbol: str        # e.g. "httpx.AsyncClient", "requests.post", "urlopen"
    line: int

    def key(self) -> str:
        return f"{self.file}::{self.symbol}"


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
        self.findings: List[Tuple[str, int]] = []  # (symbol, line)

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
            if mod == "httpx" and alias.name in _HTTPX_CTORS:
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
            self.findings.append((sym, node.lineno))
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
                if canon == "httpx" and attr in _HTTPX_CTORS:
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
        for sym, line in sc.findings:
            findings.append(Finding(file=rel, symbol=sym, line=line))
    return findings


def _load_allowlist() -> Dict[str, dict]:
    if not ALLOWLIST_PATH.exists():
        return {}
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return {e["file"] + "::" + e["symbol"]: e for e in data.get("allow", [])}


def check() -> int:
    findings = scan()
    allow = _load_allowlist()
    new = [f for f in findings if f.key() not in allow]
    # Report allowlist entries that no longer match any finding (stale — should be
    # pruned so the exception ledger cannot rot).
    live_keys = {f.key() for f in findings}
    stale = [k for k in allow if k not in live_keys]
    if not new and not stale:
        print(f"egress-policy-gate OK: {len(findings)} known site(s) allowlisted, "
              f"0 new, 0 stale.")
        return 0
    if new:
        print("egress-policy-gate FAIL: raw outbound-client construction outside "
              "the audited adapters (use youtab_runtime.egress_guard_http / "
              "egress_adapters / tools.url_safety.create_ssrf_safe_*, or add a "
              "justified entry to security/egress_allowlist.json):")
        for f in sorted(new, key=lambda x: (x.file, x.line)):
            print(f"  {f.file}:{f.line}  {f.symbol}")
    if stale:
        print("egress-policy-gate FAIL: stale allowlist entries (site removed or "
              "migrated — prune them so the ledger stays honest):")
        for k in sorted(stale):
            print(f"  {k}")
    return 1


def update_baseline() -> int:
    findings = scan()
    existing = _load_allowlist()
    entries = []
    for f in sorted(findings, key=lambda x: (x.file, x.symbol)):
        prev = existing.get(f.key(), {})
        entries.append(
            {
                "file": f.file,
                "symbol": f.symbol,
                "reason": prev.get("reason", "PENDING: pre-existing raw client; "
                          "not yet migrated to the audited boundary."),
                "category": prev.get("category", "pre_existing_unmigrated"),
                "owner_action": prev.get("owner_action", "PENDING_OWNER_ACTION: "
                                "verify via live integration then migrate or keep."),
            }
        )
    ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALLOWLIST_PATH.write_text(
        json.dumps({"allow": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(entries)} allowlist entries to "
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
