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

The OCR half separates two failures that used to be indistinguishable. "No OCR
engine is installed here" is an environment fault: it says nothing about any
particular image, so recording it as one ``UNKNOWN_REQUIRES_FIX_OR_EVIDENCE``
per image buried the real signal under an entry for every shipped asset --
including 16x16 favicons that carry no text at all -- and made an unchecked
tree look exactly like a tree full of retired branding. It is now reported
once, as a gate error. The gate still refuses to pass, so the "never silently
pass" property is unchanged; only the message is honest about which failure
happened.

Exit codes:

- ``0`` -- the gate ran and found nothing blocking.
- ``1`` -- the gate ran and found blocking occurrences.
- ``2`` -- the gate could not run: OCR was requested but no engine exists.
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
    SKIP_PATH_PREFIXES,
    scan as classify_tree,
)

OCR_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# Not a classification of any occurrence -- a statement that the gate itself
# could not perform a check it was asked to perform. Kept out of
# ``FORBIDDEN_CLASSES`` so it can never be mistaken for a branding finding.
GATE_COULD_NOT_RUN = "GATE_COULD_NOT_RUN"


def _ocr_backend() -> tuple[str, object] | tuple[None, None]:
    """Pick an OCR engine: RapidOCR if importable, else the tesseract binary.

    RapidOCR is preferred on measurement, not taste. Over the 61 images tracked
    in this repository, RapidOCR reads the retired brand out of 16 of them and
    tesseract out of 1 -- these are dashboard screenshots with small
    anti-aliased text, which tesseract handles badly. The order used to be the
    other way round, which meant a machine with tesseract installed reported 1
    and looked fifteen findings cleaner than it was.

    Returning ``(None, None)`` means images cannot be read here at all. That is
    an environment fault rather than a finding about any particular image, and
    :func:`main` reports it as one.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        pass
    else:
        return "rapidocr", RapidOCR()
    if shutil.which("tesseract"):
        return "tesseract", None
    return None, None


def _read_image_text(path: Path, engine: str, reader: object) -> str | None:
    """Extract text from *path*, or ``None`` if extraction failed."""
    if engine == "tesseract":
        with tempfile.TemporaryDirectory(prefix="youtab-brand-ocr-") as temp:
            out = Path(temp) / "result"
            done = subprocess.run(
                ["tesseract", str(path), str(out)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=60,
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


def _ocr_findings(
    root: Path, *, ocr: bool
) -> tuple[list[dict[str, str]], int, int, str]:
    findings: list[dict[str, str]] = []
    binary_files = 0
    images_seen = 0
    engine, reader = _ocr_backend() if ocr else (None, None)
    for path in _tracked(root):
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in Path(rel).parts) or not path.is_file():
            continue
        if rel.startswith(SKIP_PATH_PREFIXES):
            # This gate's own evidence output -- see brand_url_inventory.
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
        images_seen += 1
        if not ocr or engine is None:
            # There is nothing to report about this image. With no engine the
            # gate fails as a whole (see `main`) rather than accusing every
            # shipped asset of carrying the retired brand.
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
    return findings, binary_files, images_seen, (engine or "none")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()

    inventory = classify_tree(root)
    ocr_findings, binary_files, images_seen, ocr_engine = _ocr_findings(
        root, ocr=args.ocr
    )

    if not args.ocr:
        ocr_status = "disabled"
    elif ocr_engine == "none":
        ocr_status = "engine-unavailable"
    else:
        ocr_status = "ran"

    gate_errors: list[dict[str, str]] = []
    if ocr_status == "engine-unavailable":
        gate_errors.append({
            "kind": "ocr-engine-unavailable",
            "classification": GATE_COULD_NOT_RUN,
            "detail": (
                f"--ocr was requested but neither the `tesseract` binary nor the "
                f"`rapidocr_onnxruntime` package is available, so none of the "
                f"{images_seen} tracked images were read. This is an environment "
                f"fault and says nothing about whether those images carry the "
                f"retired brand. Install the `tesseract-ocr` package, or "
                f"`pip install rapidocr-onnxruntime`, then run the gate again."
            ),
        })

    blocking = list(inventory["blocking"]) + [
        f for f in ocr_findings if f["classification"] in FORBIDDEN_CLASSES
    ]
    result = {
        "schema_version": 3,
        "text_files_scanned": inventory["text_files_scanned"],
        "evidence_files_skipped": inventory["evidence_files_skipped"],
        "binary_files_seen": binary_files,
        "ocr_enabled": args.ocr,
        "ocr_engine": ocr_engine,
        "ocr_status": ocr_status,
        "images_seen": images_seen,
        "images_ocr_unreadable": sum(
            1 for f in ocr_findings if f["kind"] == "ocr-error"
        ),
        "images_with_old_brand": sum(1 for f in ocr_findings if f["kind"] == "ocr"),
        "occurrences_total": inventory["total_occurrences"],
        "counts_by_classification": inventory["counts_by_classification"],
        "ocr_findings": ocr_findings,
        "gate_errors": gate_errors,
        "blocking": blocking,
        "passed": not blocking and not gate_errors,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")

    # The payload above runs to hundreds of lines. State the verdict in one
    # line at the end so a CI log reader does not have to reconstruct it.
    if gate_errors:
        print(
            f"BLOCKED: branding gate could not run - {gate_errors[0]['detail']}",
            file=sys.stderr,
        )
        return 2
    if blocking:
        print(
            f"FAIL: branding gate found {len(blocking)} blocking occurrence(s), "
            f"of which {result['images_with_old_brand']} image(s) carry the "
            f"retired brand and {result['images_ocr_unreadable']} image(s) could "
            f"not be read",
            file=sys.stderr,
        )
        return 1
    print(
        f"PASS: branding gate (ocr {ocr_status}, engine {ocr_engine}, "
        f"{images_seen} image(s) seen)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
