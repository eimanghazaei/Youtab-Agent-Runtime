#!/usr/bin/env python3
"""Fail on retired upstream identity used as Youtab product identity.

This gate used to fail on *any* occurrence of the retired upstream brand
outside ``LICENSE`` and ``THIRD_PARTY_NOTICES.md``. That is the wrong test in
two directions at once:

- it passed a tree that still carried the upstream author's handle and home
  directory, because the pattern never covered contributor identity; and
- it would fail a tree that correctly names a real third-party model id or
  repository, which pressures the next author into rewriting third-party
  facts. That is exactly how the previous pass ended up shipping unroutable
  model ids and a `.mailmap` full of invented addresses.

So the text half of this gate now delegates to
:mod:`scripts.youtab.brand_url_inventory`, which assigns every occurrence to
one of six classes and blocks only the forbidden ones: product identity,
runtime download/update, and anything unclassified.

This module keeps the two checks the classifier cannot do: paths, and binary
assets via OCR.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from brand_url_inventory import (  # noqa: E402
    FORBIDDEN_CLASSES,
    OLD_BRAND,
    PRODUCT_FORBIDDEN,
    SKIP_DIRS,
    scan as classify_tree,
)

OCR_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _ocr_backend() -> tuple[str, object] | tuple[None, None]:
    """Pick an OCR engine. Tesseract if present, else the pip-only engine.

    Returning ``(None, None)`` means images cannot be read here, and the gate
    reports them as blocked rather than silently passing them.
    """
    if shutil.which("tesseract"):
        return "tesseract", None
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None, None
    return "rapidocr", RapidOCR()


def _read_image_text(path: Path, engine: str, reader: object) -> str | None:
    """Extract text from *path*, or ``None`` if extraction failed."""
    if engine == "tesseract":
        with tempfile.TemporaryDirectory(prefix="youtab-brand-ocr-") as temp:
            out = Path(temp) / "result"
            done = subprocess.run(
                ["tesseract", str(path), str(out)],
                capture_output=True, text=True, check=False, timeout=60,
            )
            if done.returncode != 0:
                return None
            return out.with_suffix(".txt").read_text(encoding="utf-8", errors="replace")
    try:
        result, _ = reader(str(path))  # type: ignore[operator]
    except Exception:
        return None
    if not result:
        return ""
    return "\n".join(str(line[1]) for line in result)


def _tracked(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [root / p for p in out.decode().split("\0") if p]


def _ocr_findings(root: Path, *, ocr: bool) -> tuple[list[dict[str, str]], int, str]:
    findings: list[dict[str, str]] = []
    binary_files = 0
    engine, reader = _ocr_backend() if ocr else (None, None)
    for path in _tracked(root):
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in Path(rel).parts) or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" not in raw:
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                continue
        binary_files += 1
        if path.suffix.lower() not in OCR_SUFFIXES:
            continue
        if not ocr:
            continue
        if engine is None:
            findings.append({"kind": "ocr-blocked", "path": rel,
                             "classification": "UNKNOWN_REQUIRES_FIX_OR_EVIDENCE"})
            continue
        text = _read_image_text(path, engine, reader)
        if text is None:
            findings.append({"kind": "ocr-error", "path": rel,
                             "classification": "UNKNOWN_REQUIRES_FIX_OR_EVIDENCE"})
            continue
        match = OLD_BRAND.search(text)
        if match:
            findings.append({"kind": "ocr", "path": rel, "match": match.group(0),
                             "classification": PRODUCT_FORBIDDEN})
    return findings, binary_files, (engine or "none")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()

    inventory = classify_tree(root)
    ocr_findings, binary_files, ocr_engine = _ocr_findings(root, ocr=args.ocr)

    blocking = list(inventory["blocking"]) + [
        f for f in ocr_findings if f["classification"] in FORBIDDEN_CLASSES
    ]
    result = {
        "schema_version": 2,
        "text_files_scanned": inventory["text_files_scanned"],
        "binary_files_seen": binary_files,
        "ocr_enabled": args.ocr,
        "ocr_engine": ocr_engine,
        "images_ocr_unreadable": sum(
            1 for f in ocr_findings if f["kind"] in {"ocr-blocked", "ocr-error"}
        ),
        "occurrences_total": inventory["total_occurrences"],
        "counts_by_classification": inventory["counts_by_classification"],
        "ocr_findings": ocr_findings,
        "blocking": blocking,
        "passed": not blocking,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
