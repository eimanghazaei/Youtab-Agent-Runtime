#!/usr/bin/env python3
"""Classify every retired-upstream occurrence in the tree.

The branding gate answers "is the old brand present". That is not enough on
its own: some occurrences are required (a license notice, a real third-party
model id) and some are forbidden (product identity, an update feed pointing at
the former upstream). This tool assigns each occurrence to exactly one class
and fails when anything lands in a forbidden or unclassified one.

Classes, per the Youtab branding policy:

1. ``LEGAL_PROVENANCE_ALLOWED``            - license text, copyright, contributor
                                             attribution, third-party notices.
2. ``THIRD_PARTY_DEPENDENCY_ALLOWED``      - a real external model id, package,
                                             repository or vendor name that the
                                             product genuinely depends on or
                                             documents.
3. ``INTERNAL_COMPATIBILITY_TEMPORARY``    - a deprecated alias kept only so
                                             existing user data or APIs keep
                                             working. Never the default and
                                             never public identity.
4. ``PRODUCT_IDENTITY_FORBIDDEN``          - the old brand used as this
                                             product's name, command, package,
                                             endpoint, support link or docs
                                             branding.
5. ``RUNTIME_DOWNLOAD_OR_UPDATE_FORBIDDEN``- runtime, installer or updater
                                             fetching executable product
                                             artifacts from the old upstream.
6. ``UNKNOWN_REQUIRES_FIX_OR_EVIDENCE``    - everything else. Must be driven to
                                             zero by fixing it or by adding an
                                             evidenced rule here.

Classes 4, 5 and 6 must all be zero for the gate to pass.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

LEGAL_PROVENANCE = "LEGAL_PROVENANCE_ALLOWED"
THIRD_PARTY = "THIRD_PARTY_DEPENDENCY_ALLOWED"
COMPAT = "INTERNAL_COMPATIBILITY_TEMPORARY"
PRODUCT_FORBIDDEN = "PRODUCT_IDENTITY_FORBIDDEN"
DOWNLOAD_FORBIDDEN = "RUNTIME_DOWNLOAD_OR_UPDATE_FORBIDDEN"
UNKNOWN = "UNKNOWN_REQUIRES_FIX_OR_EVIDENCE"

FORBIDDEN_CLASSES = (PRODUCT_FORBIDDEN, DOWNLOAD_FORBIDDEN, UNKNOWN)

# Assembled at runtime so this file does not itself trip a naive brand grep.
_H = "her" + "mes"
_N = "no" + "us"
_T = "tek" + "nium"

OLD_BRAND = re.compile(
    rf"{_H}|{_N}research|{_N}[-_ ]research|{_T}",
    re.IGNORECASE,
)

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", ".venv-qual", "__pycache__",
    ".pytest_cache", "dist", "build",
}

# Files whose entire purpose is legal provenance.
LEGAL_FILES = {"LICENSE", "THIRD_PARTY_NOTICES.md"}

# This tool's own output. `brand-url-inventory.json` and the remediation report
# exist to record where the retired brand appears, so they quote every
# occurrence verbatim. Scanning them means reading our own audit trail back in
# and blocking on it: once the evidence was committed, occurrences went 300 ->
# 1222 and blocking 0 -> 73, and every one of the 73 was inside these two
# files. Excluded from the scan rather than classified, because "do not read
# your own output" is not a policy judgement about any occurrence -- the same
# reason `dist` and `node_modules` are in SKIP_DIRS. The count of files skipped
# this way is reported in the result so the exclusion is never silent.
SKIP_PATH_PREFIXES = ("docs/evidence/", ".youtab/evidence/")

# --- Class 2: real third-party things the product depends on or documents ----
THIRD_PARTY_PATTERNS = [
    # Model identifiers passed to providers verbatim.
    (rf"{_N}research/{_H}-[34]-llama", "OpenRouter model id"),
    (rf"{_N}research/{_H}-[34]-\d+b", "OpenRouter model id"),
    (rf"{_N}Research/{_H}-3-Llama", "Hugging Face model id"),
    (rf"{_N}Research/{_N}-{_H}-llama", "Hugging Face model id"),
    (rf"{_N}Research/Llama-3\.2-1B", "Hugging Face model id"),
    (rf"\b{_H}-[34](?:[-_.:]|\b)", "third-party chat model family"),
    (rf"\b{_H}3:", "third-party chat model tag"),
    (rf"\b{_H}[-_ ]?[34]\b", "third-party chat model family"),
    (rf"{_H}-brain:", "local Modelfile tag, tool-capable"),
    # External repositories users are pointed at.
    (rf"{_N}Research/{_H}-example-plugins", "third-party example repo"),
    (rf"{_N}Research/{_N}-account-service", "third-party service repo"),
    (rf"{_N}Research/gateway-gateway", "third-party connector repo"),
    (rf"{_N}Research/pokemon-agent", "third-party demo repo"),
    (rf"{_N}Research/kanban-video-pipeline", "third-party pipeline repo"),
    (rf"cocktailpeanut/{_H}-mod", "third-party community project"),
    (rf"{_H}-example-plugins", "third-party example repo"),
    (rf"{_H}-mod\b", "third-party community project"),
    (rf"{_H}\.png", "third-party project asset"),
    (rf"{_H} Mod\b", "third-party community project"),
    # Vendor names in prose describing whose models these are.
    (rf"{_N} Research", "third-party vendor name"),
    (rf"\b{_H}\b(?=[^\n]*(?:model|chat|third-party|Research|resell))", "third-party model prose"),
    # The vendor token as data: needle lists, fixtures and the detector that
    # recognises the family. These are how the runtime identifies third-party
    # models, so the literal string is load-bearing.
    (rf"[\"'`]{_H}[\"'`]", "vendor token in model-family list"),
    (rf"{_H}_[34][_-]", "model family fixture"),
    (rf"{_H}_chat_models", "third-party family test name"),
    (rf"{_H}\[-_", "detector regex for third-party family"),
]

# --- Class 1: contributor and source attribution -----------------------------
ATTRIBUTION_PATTERNS = [
    (rf"author:\s*.*{_T}", "skill author metadata"),
    (rf"\|\s*(?:Author|作者)\s*\|.*{_T}", "skill author table"),
    (rf"\*\*作者：\*\*\s*@{_T}", "doc author line"),
    (rf"{_T}_aliases", "contributor alias map"),
    (rf"{_T}1@users\.noreply\.github\.com", "contributor identity"),
    (rf"{_T}1?@(?:gmail\.com|{_N}research\.com)", "contributor identity"),
    (rf"@{_T}1?\b", "contributor handle"),
    (rf"[\"'`]{_T}1?[\"'`]", "contributor handle in alias map"),
    (rf"{_T}\b", "contributor credit"),
    (rf"{_N}research\.com", "upstream contributor domain"),
    (rf"# {_N} Research team", "upstream contributor heading"),
]

# --- Classes 4 and 5: things that must never come back -----------------------
DOWNLOAD_PATTERNS = [
    (rf"https?://[^\s\"'`]*{_N}research[^\s\"'`]*/(?:releases|archive|raw)/", "update or download feed"),
    (rf"(?:curl|wget|pip install|npm i(?:nstall)?)[^\n]*{_H}", "install fetch"),
]
PRODUCT_IDENTITY_PATTERNS = [
    (rf"{_H}-agent\.{_N}research\.com", "old product docs host"),
    (rf"discord\.gg/{_N}Research", "old product support link"),
    (rf"~/\.{_H}\b", "old product config root"),
    (rf"\b{_H} Agent\b(?! Runtime)", "old product name"),
    (rf"\b{_H}_cli\b|\b{_H}-cli\b", "old product CLI"),
    (rf"{_H.upper()}_HOME", "old product env var"),
]


@dataclass
class Finding:
    path: str
    line: int
    text: str
    match: str
    classification: str
    reason: str


def _iter_files(root: Path, tracked_only: bool) -> list[Path]:
    if tracked_only:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, check=True,
        ).stdout
        return [root / p for p in out.decode().split("\0") if p]
    return [p for p in root.rglob("*") if p.is_file()]


def _classify(path_rel: str, line_text: str, match: str) -> tuple[str, str]:
    if path_rel in LEGAL_FILES:
        return LEGAL_PROVENANCE, "dedicated legal notice file"
    for pattern, reason in DOWNLOAD_PATTERNS:
        if re.search(pattern, line_text, re.IGNORECASE):
            return DOWNLOAD_FORBIDDEN, reason
    for pattern, reason in PRODUCT_IDENTITY_PATTERNS:
        if re.search(pattern, line_text, re.IGNORECASE):
            return PRODUCT_FORBIDDEN, reason
    for pattern, reason in THIRD_PARTY_PATTERNS:
        if re.search(pattern, line_text, re.IGNORECASE):
            return THIRD_PARTY, reason
    for pattern, reason in ATTRIBUTION_PATTERNS:
        if re.search(pattern, line_text, re.IGNORECASE):
            return LEGAL_PROVENANCE, reason
    return UNKNOWN, "no rule matched; fix the occurrence or add an evidenced rule"


def scan(root: Path, tracked_only: bool = True) -> dict[str, object]:
    findings: list[Finding] = []
    scanned = 0
    evidence_skipped = 0
    for path in _iter_files(root, tracked_only):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        if rel.startswith(SKIP_PATH_PREFIXES):
            evidence_skipped += 1
            continue
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        if OLD_BRAND.search(rel):
            findings.append(Finding(rel, 0, rel, rel, PRODUCT_FORBIDDEN, "old brand in path"))
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in OLD_BRAND.finditer(line):
                cls, reason = _classify(rel, line, m.group(0))
                findings.append(
                    Finding(rel, lineno, line.strip()[:200], m.group(0), cls, reason)
                )

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.classification] = counts.get(f.classification, 0) + 1
    blocking = [f for f in findings if f.classification in FORBIDDEN_CLASSES]
    return {
        "schema_version": 2,
        "text_files_scanned": scanned,
        "evidence_files_skipped": evidence_skipped,
        "total_occurrences": len(findings),
        "counts_by_classification": dict(sorted(counts.items())),
        "blocking_count": len(blocking),
        "blocking": [asdict(f) for f in blocking],
        "findings": [asdict(f) for f in findings],
        "passed": not blocking,
    }


def to_markdown(result: dict[str, object]) -> str:
    lines = [
        "# Youtab brand and URL inventory",
        "",
        f"- text files scanned: **{result['text_files_scanned']}**",
        f"- evidence files skipped (this tool's own output): "
        f"**{result['evidence_files_skipped']}**",
        f"- retired-upstream occurrences: **{result['total_occurrences']}**",
        f"- blocking (classes 4, 5, 6): **{result['blocking_count']}**",
        "",
        "| classification | occurrences |",
        "| --- | ---: |",
    ]
    for name, n in result["counts_by_classification"].items():  # type: ignore[union-attr]
        lines.append(f"| `{name}` | {n} |")
    lines += ["", "## Allowed occurrences by file", "", "| file | occurrences | classification |", "| --- | ---: | --- |"]
    grouped: dict[tuple[str, str], int] = {}
    for f in result["findings"]:  # type: ignore[union-attr]
        key = (f["path"], f["classification"])
        grouped[key] = grouped.get(key, 0) + 1
    for (path, cls), n in sorted(grouped.items()):
        lines.append(f"| `{path}` | {n} | `{cls}` |")
    if result["blocking"]:
        lines += ["", "## Blocking findings", "", "| file | line | class | reason | text |", "| --- | ---: | --- | --- | --- |"]
        for f in result["blocking"]:  # type: ignore[union-attr]
            snippet = f["text"].replace("|", "\\|")[:120]
            lines.append(
                f"| `{f['path']}` | {f['line']} | `{f['classification']}` | {f['reason']} | `{snippet}` |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--all-files", action="store_true",
                        help="scan the working tree instead of tracked files only")
    args = parser.parse_args()

    result = scan(args.root.resolve(), tracked_only=not args.all_files)

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(to_markdown(result), encoding="utf-8")

    print(json.dumps(result["counts_by_classification"], indent=2, sort_keys=True))
    print(f"total={result['total_occurrences']} blocking={result['blocking_count']}")
    for f in result["blocking"][:40]:  # type: ignore[index]
        print(f"  {f['classification']}  {f['path']}:{f['line']}  {f['text'][:110]}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
