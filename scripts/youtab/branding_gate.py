#!/usr/bin/env python3
"""Fail on retired upstream identity outside explicit legal notices."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


FORBIDDEN = re.compile(
    ("her" + "mes") + "|" + ("no" + "us") + r"\s*research|" + ("no" + "usresearch"),
    re.IGNORECASE,
)
LEGAL = {"LICENSE", "THIRD_PARTY_NOTICES.md"}
SKIP_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}
OCR_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def candidates(root: Path):
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_PARTS for part in path.relative_to(root).parts):
            continue
        yield path


def scan(root: Path, *, ocr: bool) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    text_files = 0
    binary_files = 0
    for path in candidates(root):
        rel = path.relative_to(root).as_posix()
        if FORBIDDEN.search(rel):
            findings.append({"kind": "path", "path": rel})
        if not path.is_file() or rel in LEGAL:
            continue
        raw = path.read_bytes()
        if b"\0" not in raw:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                text_files += 1
                match = FORBIDDEN.search(text)
                if match:
                    line = text.count("\n", 0, match.start()) + 1
                    findings.append(
                        {"kind": "text", "path": rel, "line": str(line)}
                    )
                continue
        binary_files += 1
        if not ocr or path.suffix.lower() not in OCR_SUFFIXES:
            continue
        if not shutil.which("tesseract"):
            findings.append({"kind": "ocr-blocked", "path": rel})
            continue
        with tempfile.TemporaryDirectory(prefix="youtab-brand-ocr-") as temp:
            output = Path(temp) / "result"
            completed = subprocess.run(
                ["tesseract", str(path), str(output)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if completed.returncode != 0:
                findings.append({"kind": "ocr-error", "path": rel})
                continue
            text = output.with_suffix(".txt").read_text(
                encoding="utf-8", errors="replace"
            )
            if FORBIDDEN.search(text):
                findings.append({"kind": "ocr", "path": rel})
    return {
        "schema_version": 1,
        "text_files_scanned": text_files,
        "binary_files_seen": binary_files,
        "ocr_enabled": ocr,
        "legal_allowlist": sorted(LEGAL),
        "findings": findings,
        "passed": not findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = scan(args.root.resolve(), ocr=args.ocr)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
