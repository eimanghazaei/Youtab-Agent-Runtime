#!/usr/bin/env python3
"""Prove the document toolchain can actually render a complex-script PDF.

This runs the recipe the `pdf` skill documents — read out of SKILL.md, not
restated here — and then judges the rendered page. It exists because declaring
the dependencies is not the same as being able to use them: the image that
shipped before this check had every skill installed and could not render a
single Persian document, and the agent's own acceptance script reported
"ALL CHECKS PASSED" while the delivered PDF had reversed lines and mirrored
arrows.

Two callers, one implementation:

  * the Dockerfile runs it during the build, so an image whose document
    toolchain cannot render never finishes building;
  * `tests/skills/test_office_document_skills.py` runs it wherever the binaries
    happen to be installed.

Checks, in order, each one a thing that has actually been observed to fail:

  1. the documented command exits 0 and produces a PDF at all
     (a bare `soffice` call in a sandbox aborts and converts nothing);
  2. the PDF is large enough to hold a shaped page;
  3. an Arabic-script face is embedded, so the glyphs are really in the file;
  4. the rasterised page is right-aligned — more ink in the right third than in
     the left third. Text extraction passes on a Persian-looking page laid out
     as an English one; this is the check that does not.

Exit code 0 and a line per check on success, non-zero with the reason on
failure. It judges the output of the build, never the source that asked for it.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REQUIRED_BINARIES = ("soffice", "pdftoppm")

# The recipe's shape, in one place. A change to SKILL.md's layout is a change
# here, not a silent change in behaviour.
RTL_HTML_BLOCK = r"cat > doc\.html <<'HTML'\n(.*?)\nHTML\n"
RTL_COMMAND_LINE = r"^(python scripts/office/soffice\.py .*doc\.html)$"

MIN_PDF_BYTES = 2048
ARABIC_FONT_HINT = re.compile(r"Arabic|Naskh|Noto")
INK_THRESHOLD = 128


class ToolchainError(RuntimeError):
    """A check failed. The message is the operator-facing explanation."""


def _pdf_skill(repo: Path) -> Path:
    skill = repo / "skills" / "productivity" / "pdf"
    if not (skill / "SKILL.md").is_file():
        raise ToolchainError(f"no pdf skill at {skill}")
    return skill


def documented_recipe(repo: Path) -> tuple[str, list[str]]:
    """The HTML and the command verbatim from the pdf skill's own recipe."""
    text = (_pdf_skill(repo) / "SKILL.md").read_text(encoding="utf-8")
    html = re.search(RTL_HTML_BLOCK, text, re.DOTALL)
    if not html:
        raise ToolchainError("the pdf skill no longer documents an RTL HTML recipe")
    command = re.search(RTL_COMMAND_LINE, text, re.MULTILINE)
    if not command:
        raise ToolchainError(
            "the pdf skill no longer documents a runnable "
            "`python scripts/office/soffice.py ... doc.html` command"
        )
    return html.group(1), command.group(1).split()


def missing_binaries() -> list[str]:
    return [name for name in REQUIRED_BINARIES if shutil.which(name) is None]


def _render(repo: Path, workdir: Path) -> Path:
    html, command = documented_recipe(repo)
    (workdir / "doc.html").write_text(html, encoding="utf-8")

    # the sanctioned wrapper, copied in so the documented relative path resolves
    office = workdir / "scripts" / "office"
    office.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_pdf_skill(repo) / "scripts" / "office" / "soffice.py", office / "soffice.py")

    done = subprocess.run(command, cwd=workdir, capture_output=True, text=True, timeout=300)
    pdf = workdir / "doc.pdf"
    if done.returncode != 0 or not pdf.is_file():
        raise ToolchainError(
            "the documented recipe did not produce a PDF.\n"
            f"  command: {' '.join(command)}\n"
            f"  exit:    {done.returncode}\n"
            f"  stdout:  {done.stdout.strip()}\n"
            f"  stderr:  {done.stderr.strip()}"
        )
    return pdf


def _check_embedded_font(pdf: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    if not reader.pages:
        raise ToolchainError("the rendered PDF has no pages")
    fonts = repr(reader.pages[0].get("/Resources", {}).get("/Font", {}))
    if not ARABIC_FONT_HINT.search(fonts):
        raise ToolchainError(
            "no Arabic-script font is embedded on page 1, so the shaped glyphs "
            f"are not in the file. /Font was: {fonts}"
        )
    return fonts


def _check_right_aligned(pdf: Path, workdir: Path) -> tuple[int, int]:
    raster = subprocess.run(
        [
            "pdftoppm", "-r", "72", "-gray", "-png",
            "-f", "1", "-l", "1", str(pdf), str(workdir / "page"),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if raster.returncode != 0:
        raise ToolchainError(f"pdftoppm could not rasterise the page: {raster.stderr.strip()}")
    pages = sorted(workdir.glob("page*.png"))
    if not pages:
        raise ToolchainError("pdftoppm reported success but wrote no raster")

    from PIL import Image

    with Image.open(pages[0]) as image:
        grey = image.convert("L")
        width, height = grey.size
        third = width // 3
        left = sum(1 for px in grey.crop((0, 0, third, height)).getdata() if px < INK_THRESHOLD)
        right = sum(
            1
            for px in grey.crop((width - third, 0, width, height)).getdata()
            if px < INK_THRESHOLD
        )
    if right <= left:
        raise ToolchainError(
            f"the rendered page is not right-aligned: {left} dark pixels in the "
            f"left third against {right} in the right third. The text can be "
            f"shaped correctly and the page still be laid out as an LTR document."
        )
    return left, right


def verify(repo: Path) -> list[str]:
    """Run every check. Returns one human-readable line per check that passed."""
    absent = missing_binaries()
    if absent:
        raise ToolchainError(f"document toolchain incomplete: {', '.join(absent)} missing")

    notes = [f"binaries present: {', '.join(REQUIRED_BINARIES)}"]
    with tempfile.TemporaryDirectory(prefix="doctoolchain_") as tmp:
        workdir = Path(tmp)
        pdf = _render(repo, workdir)
        notes.append(f"documented recipe rendered {pdf.name} ({pdf.stat().st_size} bytes)")

        if pdf.stat().st_size <= MIN_PDF_BYTES:
            raise ToolchainError(
                f"the PDF is {pdf.stat().st_size} bytes, too small to hold a shaped page"
            )
        notes.append(f"PDF larger than {MIN_PDF_BYTES} bytes")

        _check_embedded_font(pdf)
        notes.append("an Arabic-script font is embedded on page 1")

        left, right = _check_right_aligned(pdf, workdir)
        notes.append(f"page reads right-to-left ({right} right-third vs {left} left-third ink)")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root holding skills/productivity/pdf (default: this checkout)",
    )
    args = parser.parse_args(argv)
    try:
        for note in verify(args.repo):
            print(f"document toolchain OK: {note}")
    except ToolchainError as exc:
        print(f"document toolchain FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
