---
name: pdf
description: "Create, merge, split, fill, and secure PDF files."
version: 1.0.0
author: Anthropic (adapted by Youtab B.V.)
license: Proprietary. LICENSE.txt has complete terms
platforms: [linux, macos, windows]
metadata:
  youtab:
    tags: [PDF, Documents, Forms, Office, Productivity]
    category: productivity
    related_skills: [ocr-and-documents, nano-pdf, docx, xlsx]
---

# PDF Skill

Create, combine, split, transform, and secure PDF files — merging, page manipulation, form filling, watermarks, encryption, and text/table extraction. For heavy text extraction from scanned documents prefer the `ocr-and-documents` skill; for natural-language edits to existing PDF text prefer `nano-pdf`.

## When to Use

Use this skill whenever the user wants to do anything with PDF files: reading or extracting text/tables, combining or merging multiple PDFs, splitting PDFs apart, rotating pages, adding watermarks, creating new PDFs, filling PDF forms, encrypting/decrypting, extracting images, or OCR on scanned PDFs. If the user mentions a .pdf file or asks to produce one, use this skill.

## Prerequisites

Already installed in the Youtab runtime image — do not try to install them.
`pypdf`, `pdfplumber` and `reportlab` ship in the `documents` extra; `pdftotext`,
`pdftoppm`, `pdfimages`, `qpdf`, `soffice` and `pandoc` are baked into the image,
along with fonts covering Arabic script. The image has no `sudo` and its venv is
sealed, so an `apt install` or `pip install` here cannot work; if a binary really
is missing, say so and stop rather than building a private virtualenv in the
workspace.

Elsewhere (desktop, macOS, a bare checkout):

```bash
pip install pypdf pdfplumber reportlab
which pdftotext || sudo apt install -y poppler-utils   # pdftotext, pdftoppm, pdfimages
which qpdf || sudo apt install -y qpdf                 # CLI merge/split/decrypt
which soffice || sudo apt install -y libreoffice       # complex-script rendering
```

macOS: `brew install poppler qpdf libreoffice`. OCR extras: `pip install pytesseract pdf2image` + `sudo apt install -y tesseract-ocr`.

> Script paths below are relative to this skill's directory. Form filling has its own workflow — read [forms.md](forms.md) and follow it. Advanced library usage (pypdfium2, pdf-lib) and troubleshooting: [reference.md](reference.md).

## Quick Reference

| Task | Best Tool | Command/Code |
|------|-----------|--------------|
| Merge PDFs | pypdf | `writer.add_page(page)` per page |
| Split PDFs | pypdf | One page per file |
| Extract text | pdfplumber | `page.extract_text()` |
| Extract tables | pdfplumber | `page.extract_tables()` |
| Create PDFs (Latin / LTR) | reportlab | Canvas or Platypus |
| **Create PDFs containing Persian, Arabic, Hebrew, Urdu, Indic or Thai** | **soffice** | HTML/DOCX → `python scripts/office/soffice.py --convert-to pdf` — see below. **Never reportlab.** |
| Command-line merge/split | qpdf | `qpdf --empty --pages ...` |
| OCR scanned PDFs | pytesseract | Convert to images first (or use `ocr-and-documents`) |
| Fill PDF forms | see [forms.md](forms.md) | `scripts/fill_fillable_fields.py` etc. |
| Edit existing text | `nano-pdf` skill | `nano-pdf edit file.pdf <page> "<instruction>"` |

## Common operations

### Merge / split / rotate (pypdf)

```python
from pypdf import PdfReader, PdfWriter

# Merge
writer = PdfWriter()
for pdf_file in ["doc1.pdf", "doc2.pdf"]:
    for page in PdfReader(pdf_file).pages:
        writer.add_page(page)
with open("merged.pdf", "wb") as f:
    writer.write(f)

# Split: one file per page
reader = PdfReader("input.pdf")
for i, page in enumerate(reader.pages):
    w = PdfWriter(); w.add_page(page)
    with open(f"page_{i+1}.pdf", "wb") as f:
        w.write(f)

# Rotate
page = reader.pages[0]
page.rotate(90)  # clockwise
```

### Extract text and tables (pdfplumber)

```python
import pdfplumber, pandas as pd

with pdfplumber.open("document.pdf") as pdf:
    text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    tables = [pd.DataFrame(t[1:], columns=t[0])
              for page in pdf.pages
              for t in page.extract_tables() if t]
```

### Create PDFs (reportlab)

```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet

doc = SimpleDocTemplate("report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story = [Paragraph("Report Title", styles["Title"]), Spacer(1, 12),
         Paragraph("Body text...", styles["Normal"]), PageBreak(),
         Paragraph("Page 2", styles["Heading1"])]
doc.build(story)
```

### Create PDFs containing Persian, Arabic, Hebrew, Urdu, Indic or Thai

**Do not use reportlab for these, and do not reorder or reshape the text
yourself.** reportlab draws glyphs in the order it is handed them. It does not
run a shaping engine, so it will not pick Arabic joining forms, will not place a
harakat on its letter (no GPOS), and does not implement the UAX#9 bidi
algorithm. Using it for Persian forces you to pre-shape with `arabic_reshaper`,
pre-reorder with `python-bidi`, break lines by hand and then hand-patch neutral
characters with LRM/RLM marks. That path has been tried here and it produced,
in one document: reversed word order wherever reportlab re-wrapped a line that
was already in visual order, silently deleted vowel marks (`delete_harakat`
defaults to True), and an unbalanced `(` flung to the start of a line. Each fix
uncovered the next case — nested parentheses, `[ ] { } < >`, line-final
neutrals — because the underlying algorithm was being re-implemented by hand.

Use a shaping engine instead. `soffice` is in the image and lays out text
through HarfBuzz and ICU, so joining, mark placement and bidi are done by code
that implements those specifications.

Always invoke it through `scripts/office/soffice.py`, never as a bare `soffice`.
That wrapper is the sanctioned entry point every office skill already uses: it
supplies an isolated `-env:UserInstallation` profile, sets `SAL_USE_VCLPLUGIN=svp`,
and installs an `LD_PRELOAD` socket shim when `AF_UNIX` is blocked. A bare
`soffice` in a sandboxed task environment aborts with "User installation could
not be completed" and converts nothing — so the bare form does not merely run
unprotected, it produces no file at all.

```bash
cat > doc.html <<'HTML'
<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<style>
  /* dir="rtl" alone gets the CHARACTERS right and the PAGE wrong: the shaper
     reorders correctly while every block still aligns left. These three rules
     are what make it read as a Persian document rather than a Persian-looking
     English one. Each was added after seeing it fail in the raster. */
  body { font-family: "Noto Naskh Arabic", "Noto Sans Arabic", serif; font-size: 13pt;
         direction: rtl; text-align: right; }
  h1, h2, h3, p, li, td, th { direction: rtl; text-align: right; }
  ul, ol { direction: rtl; margin-right: 1.4em; margin-left: 0; padding-right: 0; }
</style>
</head><body>
  <h1>تحلیل معماری و هارنس سیمرغ</h1>
  <p>خروجی نهایی: BrainOutcome (ok | refused | failed | degraded)</p>
  <p>خط لولهٔ چهارمرحله‌ای: begin → prepare → decide → complete</p>
  <!-- align="right" and NOT margin-left:auto — LibreOffice's HTML import
       ignores the margin trick for tables and leaves the table at the left. -->
  <table border="1" align="right"><tr><th>لایه</th><th>مسئولیت</th></tr></table>
</body></html>
HTML
python scripts/office/soffice.py --headless --norestore --convert-to pdf:writer_pdf_Export doc.html
```

Verified by rendering exactly that input and looking at the page: joining forms
correct; the damma in «میان‌بُری» sitting on its letter; multi-line paragraphs
with their lines in the right order; `begin → prepare → decide → complete` with
the arrows pointing the way the source does; `COMPLETED، DEGRADED، FAILED،
REFUSED` keeping its source order; `(ok | refused | failed | degraded)` balanced
with no LRM anchoring at all; bullets and tables on the right;
`NotoNaskhArabic` embedded as a subset.

Every one of those is a defect that a hand-rolled reportlab pipeline produced on
the same content, so none of them is hypothetical. One cosmetic artifact
remains: a Latin parenthetical sometimes keeps a space before its closing
bracket — check it in the raster if the document is formal.

Set `dir="rtl"` on `<html>`, not only on `<body>`: the paragraph direction is
what decides where neutral characters at a line edge land. For a Word
deliverable, build the `.docx` (see the `docx` skill) and convert that same way —
it goes through the same engine.

Only two reasons to reach for reportlab on a complex-script job: stamping a
watermark or filling a form field on an **existing** PDF. Both are page surgery
on already-shaped glyphs, not text layout.

**Subscripts/superscripts:** never use Unicode sub/superscript characters (₀₁₂, ⁰¹²) — the built-in fonts lack the glyphs and render solid black boxes. Use `<sub>`/`<super>` markup inside `Paragraph` objects: `Paragraph("H<sub>2</sub>O", styles['Normal'])`. For canvas-drawn text, adjust font size and position manually.

### Command-line tools

```bash
pdftotext -layout input.pdf output.txt                     # text, layout preserved
pdftotext -f 1 -l 5 input.pdf output.txt                   # pages 1-5
qpdf --empty --pages file1.pdf file2.pdf -- merged.pdf     # merge
qpdf input.pdf --pages . 1-5 -- pages1-5.pdf               # split range
qpdf input.pdf output.pdf --rotate=+90:1                   # rotate page 1
qpdf --password=pw --decrypt encrypted.pdf decrypted.pdf   # remove password
pdfimages -j input.pdf img                                 # extract images
```

### Watermark

```python
from pypdf import PdfReader, PdfWriter

watermark = PdfReader("watermark.pdf").pages[0]
reader, writer = PdfReader("document.pdf"), PdfWriter()
for page in reader.pages:
    page.merge_page(watermark)
    writer.add_page(page)
with open("watermarked.pdf", "wb") as f:
    writer.write(f)
```

### Password protection

```python
writer.encrypt("userpassword", "ownerpassword")
```

### OCR scanned PDFs

```python
import pytesseract
from pdf2image import convert_from_path

pages = convert_from_path("scanned.pdf")
text = "\n\n".join(pytesseract.image_to_string(img) for img in pages)
```

For batch/structured extraction from scans, the `ocr-and-documents` skill (pymupdf, marker-pdf) is the better path.

## Form filling

Read [forms.md](forms.md) first — it distinguishes fillable (AcroForm) PDFs from flat scanned forms and walks through the helper scripts:

- `scripts/check_fillable_fields.py` — does the PDF have AcroForm fields?
- `scripts/extract_form_field_info.py` / `scripts/extract_form_structure.py` — enumerate fields
- `scripts/fill_fillable_fields.py` — fill AcroForm fields
- `scripts/fill_pdf_form_with_annotations.py` — overlay text on flat forms
- `scripts/check_bounding_boxes.py`, `scripts/create_validation_image.py` — verify placement visually

## Pitfalls

- `page.extract_text()` returns `None` on image-only pages — guard with `or ""` and fall back to OCR.
- pypdf preserves encryption flags: reading an encrypted PDF requires `PdfReader(path, password=...)` before pages are accessible.
- reportlab coordinates are bottom-left origin, points (1/72″) — not top-left.
- reportlab does no text shaping. For any complex script this means three
  separate failures, all of which have been observed: a line it re-wraps is
  reversed if the caller already put it in visual order (and `Frame` silently
  takes 6pt of padding per side, so the usable width is 12pt narrower than the
  frame width you passed — that alone is enough to trigger a re-wrap); vowel
  marks are dropped by `arabic_reshaper` unless `delete_harakat=False`; and even
  when preserved they are not positioned on their letter. Do not patch these —
  use `soffice`, above.
- A text-layer check cannot see any of that. Vowel marks share their letter's x
  coordinate, so an extractor may report them either side of it, and a mirrored
  bracket is still the right codepoint. Only the raster shows it.
- When filling flat forms by annotation overlay, always render a validation image and check the placement before delivering.

## Verification

These checks are the acceptance test for the document. Run all of them.

1. Open the output with `PdfReader` and assert the expected page count.
2. Re-extract text from the output (`pdftotext` or pdfplumber) and confirm the content you added is present.
3. For anything visual (watermarks, filled forms, created reports) — and
   **always** for a complex-script document: `pdftoppm -png -r 110 output.pdf page`
   and inspect the images with `vision_analyze`. This is the only step that can
   see a mark landing beside its letter, a mirrored bracket, or a line laid out
   in the wrong order, and it is not optional because the earlier steps looked fine.
4. Confirm the fonts are embedded subsets: `pdffonts output.pdf` — every face
   used must show `emb yes`.

### The verifier may not be derived from the builder

**Never import the builder's shaping, layout or line-breaking code into the
verifier.** A verifier that calls the builder's own `fa()`/`shape()` compares the
builder against itself, so a bug in that function is invisible by construction —
both sides of the comparison make the same mistake.

This is not hypothetical. A 5-page Persian translation reported
`RESULT: ALL CHECKS PASSED` on ten checks, including one explicitly named
"visual word order is RTL-reversed", while the rendered page actually showed:

* **every multi-line paragraph printed with its lines in reverse order** — the
  end of the sentence above its beginning;
* `begin → prepare → decide → complete` drawn as `begin ← prepare ← decide`,
  the arrows mirrored by UAX#9 L4, so the pipeline reads backwards;
* a comma-separated Latin enum reversed — `COMPLETED, DEGRADED, FAILED,
  REFUSED` came out `REFUSED، FAILED، DEGRADED، COMPLETED`;
* bullet markers on the left of right-aligned text;
* a long identifier split mid-word across a line break.

Every check passed because each one compared `fa(source)` against the stream
that the same `fa()` had produced. The document was wrong and the gate agreed
with it.

So the verifier compares against the two things the builder cannot influence:

* **the SOURCE text, in logical order** — never a pre-shaped string; and
* **the RASTER**, read with `vision_analyze`.

From the raster, assert these four explicitly. They are the ones a text-layer
check structurally cannot see, and each corresponds to a defect observed above:

1. the first line of each multi-line paragraph is the beginning of its sentence;
2. arrows and brackets point the way the source does;
3. comma-separated Latin runs keep their source order;
4. bullet markers sit on the leading edge — right, for RTL.

**When these pass, the document is finished. Deliver it and stop.**

That is a rule, not a suggestion, and it binds in both directions:

* **Do not keep working after a pass.** Building an extra cross-check harness,
  a local web server or a browser comparison *after* the acceptance checks
  already passed is not diligence — it is unbounded scope, it is how a job that
  was done in minutes ran for hours, and it ends with the agent trusting its own
  improvised harness over the agreed one.
* **Do not edit these checks to make them pass.** Loosening a threshold or
  narrowing a condition after seeing a failure proves nothing; the result is a
  pass the checks were changed to produce. If a check is genuinely wrong, say
  so, show the output that demonstrates it, and leave it to the operator.
* **Report a pass with its evidence.** Paste the actual command output — page
  count, the extracted text, the `pdffonts` table, what the raster showed. The
  sentence "all checks pass" on its own is a claim, not a result.
* **If it still fails after two attempts and one polish, stop and report.**
  Say which check failed and what the output was. A third attempt without new
  information is guessing.

## Related skills

`ocr-and-documents` (scanned-document text extraction), `nano-pdf` (NL text edits in place), `docx` (Word), `xlsx` (spreadsheets), `powerpoint` (decks).
