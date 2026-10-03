"""Invariant tests for the bundled office/document skills.

Covers skills/productivity/{docx,xlsx,pdf,powerpoint} — the office
document creation/editing suite. Tests assert contracts (frontmatter
shape, referenced scripts exist, cross-links resolve), not snapshots
of skill content.
"""

from __future__ import annotations

import json
import re
import sys
import zlib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SKILLS = REPO / "skills"
OPTIONAL_SKILLS = REPO / "optional-skills"

OFFICE_SKILLS = ["docx", "xlsx", "pdf", "powerpoint"]


def _skill_dir(name: str) -> Path:
    return SKILLS / "productivity" / name


def _frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{skill_md} has no YAML frontmatter"
    return yaml.safe_load(match.group(1))


@pytest.mark.parametrize("name", OFFICE_SKILLS)
def test_skill_exists_with_frontmatter(name):
    skill_md = _skill_dir(name) / "SKILL.md"
    assert skill_md.exists(), f"missing {skill_md}"
    fm = _frontmatter(skill_md)
    assert fm["name"] == name
    assert fm["description"].strip()
    assert len(fm["description"]) <= 60, (
        f"{name}: description is {len(fm['description'])} chars (max 60)"
    )
    assert fm["description"].rstrip('"').endswith(".")
    platforms = fm.get("platforms")
    assert platforms, f"{name}: missing platforms gating"
    assert set(platforms) <= {"linux", "macos", "windows"}


@pytest.mark.parametrize("name", OFFICE_SKILLS)
def test_referenced_scripts_exist(name):
    """Every scripts/... path mentioned in SKILL.md must exist on disk."""
    skill_dir = _skill_dir(name)
    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    refs = set(re.findall(r"scripts/[\w./-]+\.py", body))
    assert refs, f"{name}: SKILL.md references no helper scripts"
    for ref in refs:
        assert (skill_dir / ref).exists(), f"{name}: SKILL.md references missing {ref}"


@pytest.mark.parametrize("name", OFFICE_SKILLS)
def test_license_file_present(name):
    """Adapted Anthropic skills must carry their LICENSE.txt."""
    fm = _frontmatter(_skill_dir(name) / "SKILL.md")
    if "LICENSE.txt" in str(fm.get("license", "")):
        assert (_skill_dir(name) / "LICENSE.txt").exists(), (
            f"{name}: license points to LICENSE.txt but the file is missing"
        )


def test_docx_validator_schema_paths_exist():
    """base.py maps XML parts to XSD files — every mapped schema must ship."""
    for skill in ("docx", "powerpoint"):
        base = _skill_dir(skill) / "scripts" / "office" / "validators" / "base.py"
        schemas = _skill_dir(skill) / "scripts" / "office" / "schemas"
        text = base.read_text(encoding="utf-8")
        refs = set(re.findall(r'"((?:ecma|ISO|mce|microsoft)[\w./-]+\.xsd)"', text))
        assert refs, f"{skill}: no schema references found in validators/base.py"
        for ref in refs:
            assert (schemas / ref).exists(), (
                f"{skill}: validator references missing schema {ref}"
            )


def test_docs_pages_generated():
    """Each bundled office skill has a generated docs-site page."""
    docs_dir = (
        REPO / "website" / "docs" / "user-guide" / "skills" / "bundled" / "productivity"
    )
    for name in OFFICE_SKILLS:
        assert (docs_dir / f"productivity-{name}.md").exists(), (
            f"missing generated docs page for {name}; run website/scripts/generate-skill-docs.py"
        )


# Document/payload readers must not depend on the host locale. OOXML part
# files are UTF-8 (declared in the XML prolog) and the form-fields JSON the
# agent authors is UTF-8, but these sites used locale-default text mode: on
# Windows (cp1251/GBK/cp932) the validators parsed silently mojibake'd
# document text and the pdf fill scripts wrote mojibake'd values into the
# user's form — or crashed outright where the bytes don't decode.
_ENCODING_SENSITIVE_READS = [
    (
        "docx/scripts/office/validators/base.py",
        'with open(xml_file, "rb") as f:',
    ),
    (
        "powerpoint/scripts/office/validators/base.py",
        'with open(xml_file, "rb") as f:',
    ),
    (
        "pdf/scripts/check_bounding_boxes.py",
        'with open(sys.argv[1], encoding="utf-8") as f:',
    ),
    (
        "pdf/scripts/create_validation_image.py",
        "with open(fields_json_path, 'r', encoding='utf-8') as f:",
    ),
    (
        "pdf/scripts/fill_fillable_fields.py",
        'with open(fields_json_path, encoding="utf-8") as f:',
    ),
    (
        "pdf/scripts/fill_pdf_form_with_annotations.py",
        'with open(fields_json_path, "r", encoding="utf-8") as f:',
    ),
]


def test_check_bounding_boxes_reads_utf8_fields_json(tmp_path):
    """Run the real script on a UTF-8 fields.json with non-ASCII field
    descriptions under a forced non-UTF-8 locale. Without the explicit
    encoding the json.load crashes (C locale on POSIX; cp1251 chokes on
    the 0x98 byte of U+2018 on Windows)."""
    import json
    import os
    import subprocess
    import sys

    script = _skill_dir("pdf") / "scripts" / "check_bounding_boxes.py"
    fields = {
        "form_fields": [
            {
                "description": "Фамилия (‘label’)",
                "page_number": 1,
                "label_bounding_box": [0, 0, 10, 10],
                "entry_bounding_box": [20, 20, 30, 40],
            }
        ]
    }
    fields_json = tmp_path / "fields.json"
    fields_json.write_bytes(
        json.dumps(fields, ensure_ascii=False, indent=2).encode("utf-8")
    )

    env = dict(os.environ)
    env.update({
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    result = subprocess.run(
        [sys.executable, str(script), str(fields_json)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"script failed under non-UTF-8 locale:\n{result.stderr}"
    )
    assert "SUCCESS" in result.stdout


# --------------------------------------------------------------------------
# A skill may not depend on something the runtime image does not provide.
#
# Every one of these skills opened with a Prerequisites block telling the agent
# to `pip install ...` and `sudo apt install -y pandoc libreoffice
# poppler-utils qpdf`. In the published image none of those binaries existed,
# `sudo` was not installed, and the venv is sealed
# (YOUTAB_AGENT_DISABLE_LAZY_INSTALLS=1 with /opt/youtab read-only), so the
# instructions failed at their first line. An agent asked for a Word, Excel or
# PDF deliverable then had to improvise its own pipeline in the task workspace,
# and what the user received depended on what that single run happened to
# invent. The two tests below make that state unrepresentable: a prerequisite a
# skill names must be resolved for it, or the suite fails here rather than a
# user receiving a damaged file.
# --------------------------------------------------------------------------

PYPROJECT = REPO / "pyproject.toml"
DOCKERFILE = REPO / "Dockerfile"

# `pip install pytesseract pdf2image` is deliberately out of scope: the pdf
# skill lists it under "OCR extras", scanned-document reading is the
# ocr-and-documents skill's job, and it needs the tesseract-ocr binary which
# this image does not carry. Named here so the omission is a recorded decision
# rather than an oversight these tests quietly tolerate.
OCR_EXTRA_PACKAGES = {"pytesseract", "pdf2image"}


def _resolved_distributions() -> set[str]:
    """Every distribution the project resolves for the published image.

    Core `dependencies` and the `documents` extra together: Pillow and
    defusedxml are already core because other code needs them, and a skill that
    names one is just as satisfied as by the extra. The question this answers is
    only "will this import work in the image", not "which table is it in".
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    names = set()
    patterns = (
        r"^dependencies = \[(.*?)^\]",
        r"^documents = \[(.*?)^\]",
        # installed by the Linux image but deliberately outside `[all]`
        r"^documents-extract = \[(.*?)^\]",
    )
    for pattern in patterns:
        block = re.search(pattern, text, re.MULTILINE | re.DOTALL)
        assert block, f"pyproject.toml no longer has a block matching {pattern!r}"
        for raw in re.findall(r'"([^"]+)"', block.group(1)):
            names.add(re.split(r"[\[=<>!~; ]", raw, maxsplit=1)[0].lower())
    return names


def _declared_pip_packages(skill_md: Path) -> set[str]:
    """Packages a skill's own Prerequisites tell the agent to pip install."""
    text = skill_md.read_text(encoding="utf-8")
    names = set()
    for line in re.findall(r"^\s*pip install (.+)$", text, re.MULTILINE):
        line = line.split("#", 1)[0]  # strip the trailing explanatory comment
        for token in line.split():
            if token.startswith("-") or token in {"||", "&&"}:
                continue
            token = token.strip("\"'")
            names.add(re.split(r"[\[=<>!~]", token, maxsplit=1)[0].lower())
    return names - OCR_EXTRA_PACKAGES


@pytest.mark.parametrize("name", OFFICE_SKILLS)
def test_declared_python_packages_are_resolved_for_the_image(name):
    """What a skill tells the agent to install must already be resolved for it.

    The image cannot pip install at runtime, so a package named here and absent
    from the extra is a dead instruction — exactly the gap that pushed document
    generation into a per-task virtualenv.
    """
    declared = _declared_pip_packages(_skill_dir(name) / "SKILL.md")
    missing = sorted(declared - _resolved_distributions())
    assert not missing, (
        f"{name} skill tells the agent to `pip install {' '.join(missing)}`, "
        f"but pyproject.toml resolves none of {missing} for the image. Either "
        f"add them to the `documents` extra or stop naming them in the skill."
    )


@pytest.mark.parametrize("name", OFFICE_SKILLS)
def test_declared_system_binaries_are_baked_into_the_image(name):
    """Same contract for system binaries, which no pip install can supply.

    The Dockerfile asserts these at build time; this test asserts the skills and
    that build-time list cannot drift apart.
    """
    text = (_skill_dir(name) / "SKILL.md").read_text(encoding="utf-8")
    needed = set(re.findall(r"^\s*which (\w[\w.-]*)", text, re.MULTILINE))
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    asserted = set()
    for loop in re.findall(r"for b in ([\w .-]+); do", dockerfile):
        asserted.update(loop.split())
    missing = sorted(needed - asserted)
    assert not missing, (
        f"{name} skill checks for {missing}, but the Dockerfile does not verify "
        f"that the image provides them. Add the package to the document "
        f"toolchain layer and to its build-time assertion."
    )


def test_pdf_skill_routes_complex_scripts_to_a_shaping_engine():
    """reportlab must not be the prescribed route for Persian/Arabic/Hebrew.

    It applies no shaping: joining forms, mark placement and the UAX#9 bidi
    algorithm all have to be re-implemented by the caller, and doing so by hand
    is what produced reversed lines, dropped vowel marks and an unbalanced
    bracket in a real document. The skill has to send that work to an engine
    that implements them.
    """
    text = (_skill_dir("pdf") / "SKILL.md").read_text(encoding="utf-8")
    section = re.search(
        r"^### Create PDFs containing[^\n]*\n(.*?)(?=^### )",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert section, "the pdf skill has no complex-script creation section"
    body = section.group(1)
    assert "soffice" in body, "the complex-script route must name a shaping engine"
    assert re.search(
        r"not use reportlab|Never reportlab", body + text, re.IGNORECASE
    ), "the skill must say plainly that reportlab is not the route for these scripts"


def test_pdf_verification_is_binding_in_both_directions():
    """The acceptance checks must be authoritative AND closed to the agent.

    A verifier the agent may rewrite proves nothing: in a real run the checks
    were softened six times and then reported as "All checks pass". A verifier
    the agent ignores on success is just as bad — that run kept building extra
    harnesses for hours after its own checks had already gone green.
    """
    text = (_skill_dir("pdf") / "SKILL.md").read_text(encoding="utf-8")
    section = text.split("## Verification", 1)
    assert len(section) == 2, "the pdf skill lost its Verification section"
    body = section[1]
    assert "Deliver it and stop." in body, "a pass must end the work"
    assert "Do not edit these checks" in body, "the checks must be closed to the agent"
    assert "pdffonts" in body, "embedded-font check missing"
    assert "pdftoppm" in body and "vision_analyze" in body, (
        "the raster check is the only one that can see shaping defects"
    )


def test_pdf_skill_forbids_a_verifier_derived_from_the_builder():
    """A verifier that imports the builder's shaping function checks the builder
    against itself.

    Observed on a real 5-page Persian translation: ten checks passed, one of them
    named "visual word order is RTL-reversed", while the page showed every
    multi-line paragraph with its lines in reverse order, arrows mirrored so the
    pipeline read backwards, and a Latin enum reversed. Each check compared
    `fa(source)` with the stream the same `fa()` produced, so the gate agreed
    with the defect. The skill has to forbid that construction and name the
    raster assertions that catch what a text-layer check cannot.
    """
    text = (_skill_dir("pdf") / "SKILL.md").read_text(encoding="utf-8")
    # Whitespace-folded: these are prose assertions and Markdown wraps lines,
    # so a sentence may be split anywhere.
    body = " ".join(text.split("## Verification", 1)[1].split())
    assert "may not be derived from the builder" in body, (
        "the prohibition must be a heading an agent cannot skim past"
    )
    assert "compares the builder against itself" in body, "the reason must be stated"
    for anchor in ("SOURCE text", "RASTER"):
        assert anchor in body, (
            f"the verifier's two independent references must be named ({anchor})"
        )
    # The four raster assertions, each tied to a defect that shipped.
    for claim in (
        "beginning of its sentence",
        "arrows",
        "source order",
        "bullet markers",
    ):
        assert claim in body, f"raster assertion missing: {claim}"


# ---------------------------------------------------------------------------
# Executable proof.
#
# Everything above reads text: it can only prove that the declarations have not
# drifted apart. It cannot prove the toolchain works. The tests below run the
# recipe the skill actually documents and judge the rendered pixels, so a
# toolchain that installs but cannot render fails here instead of passing
# quietly.
#
# They skip when the binaries are absent (a bare dev checkout, and the public CI
# runners, have no LibreOffice). The place they are *not* allowed to skip is the
# runtime image: the Dockerfile runs this same pipeline at build time, so an
# image whose document toolchain cannot render never finishes building.
# ---------------------------------------------------------------------------

# The checks themselves live in scripts/check_document_toolchain.py, which the
# Dockerfile also runs during the build. One implementation, two callers: a
# toolchain that installs but cannot render fails the image build, and fails here
# too on any machine that has the binaries.
sys.path.insert(0, str(REPO))
from scripts.check_document_toolchain import (  # noqa: E402
    ToolchainError,
    _check_embedded_arabic_font,
    _font_descriptors,
    _resolve,
    documented_recipe,
    missing_binaries,
    verify,
)

_MISSING_BINARIES = missing_binaries()
needs_toolchain = pytest.mark.skipif(
    bool(_MISSING_BINARIES),
    reason=f"document toolchain not installed here: {', '.join(_MISSING_BINARIES)}",
)


@needs_toolchain
def test_the_documented_rtl_recipe_renders_a_real_persian_pdf():
    """Run the skill's own recipe, then judge the raster rather than the source."""
    try:
        notes = verify(REPO)
    except ToolchainError as exc:  # pragma: no cover - only on a broken toolchain
        pytest.fail(str(exc))
    assert len(notes) >= 5, f"the toolchain check ran fewer checks than expected: {notes}"


def test_the_documented_recipe_is_still_extractable_without_the_toolchain():
    """The build-time check must be able to find the recipe even where it skips.

    If SKILL.md's recipe block is renamed or reflowed, the renderer silently
    stops being exercised; this fails instead, everywhere, including in CI where
    LibreOffice is absent.
    """
    html, command = documented_recipe(REPO)
    assert 'dir="rtl"' in html, "the documented HTML no longer sets dir=rtl"
    assert command[:2] == ["python", "scripts/office/soffice.py"], command
    assert "--convert-to" in command, command


def test_the_soffice_wrapper_has_not_forked_between_skills():
    """Each office skill carries a copy of the shim; they must stay identical.

    Per-skill script duplication is this repo's existing convention, but four
    copies that drift are four behaviours. The pdf skill's copy was added for
    complex-script rendering and must not become a private fork of the shim.
    """
    copies = sorted((REPO / "skills" / "productivity").glob("*/scripts/office/soffice.py"))
    assert len(copies) >= 4, f"expected one copy per office skill, found {copies}"
    assert len({c.read_bytes() for c in copies}) == 1, (
        "the office soffice wrapper has drifted between skills: "
        + ", ".join(str(c.relative_to(REPO)) for c in copies)
    )


BARE_SOFFICE_CALL = re.compile(r"(^|[^/\w])soffice\s+--")


def test_the_pdf_skill_never_invokes_the_bare_soffice_binary():
    """A bare `soffice` call aborts in a sandbox, so the wrapper is not optional.

    `run_soffice` supplies an isolated `-env:UserInstallation` profile, sets
    `SAL_USE_VCLPLUGIN=svp` and preloads a socket shim when AF_UNIX is blocked.
    Without it a non-root sandbox cannot bootstrap LibreOffice's default profile
    and the conversion produces no file at all.
    """
    text = (_skill_dir("pdf") / "SKILL.md").read_text(encoding="utf-8")
    body = text.split("## Prerequisites", 1)[1]
    # drop the "Elsewhere (desktop, macOS, a bare checkout)" install block, which
    # legitimately probes for the bare binary with `which soffice`
    body = re.sub(r"^which soffice.*$", "", body, flags=re.MULTILINE)
    bare = [
        line
        for line in body.splitlines()
        if BARE_SOFFICE_CALL.search(line) and "scripts/office/soffice.py" not in line
    ]
    assert not bare, (
        "the pdf skill invokes soffice directly instead of through "
        "scripts/office/soffice.py; in a sandboxed task environment that aborts "
        f'with "User installation could not be completed": {bare}'
    )


PACKAGE_JSON = REPO / "package.json"

# Named here so each omission is a recorded decision rather than something these
# tests quietly tolerate. `sharp` needs a native build the sealed image cannot
# run, and the other three only matter for the powerpoint icon path, which the
# skill now tells the agent to do without.
UNBAKED_NPM_PACKAGES = {"react-icons", "react", "react-dom", "sharp", "ntn"}


def _manifest_npm_packages() -> set[str]:
    """Every npm package the image installs from the root manifest.

    The Dockerfile copies package.json + package-lock.json and runs `npm install`
    before sealing /opt/youtab, so a package here is present at runtime and a
    package absent from here can never be installed later.
    """
    manifest = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        names.update(manifest.get(section) or {})
    return {n.lower() for n in names}


def _declared_npm_packages(skill_md: Path) -> set[str]:
    """Packages a skill's own Prerequisites tell the agent to npm install."""
    text = skill_md.read_text(encoding="utf-8")
    names: set[str] = set()
    for line in re.findall(r"^.*npm install (.+)$", text, re.MULTILINE):
        line = line.split("#", 1)[0]
        for token in line.split():
            if token.startswith("-") or token in {"||", "&&"}:
                continue
            names.add(token.strip("\"'").lower())
    return names - UNBAKED_NPM_PACKAGES


@pytest.mark.parametrize("name", OFFICE_SKILLS)
def test_declared_npm_packages_are_baked_into_the_image(name):
    """The same contract as the pip one, for the Node generators.

    `docx` and `pptxgenjs` are how the docx and powerpoint skills CREATE files.
    The image seals /opt/youtab read-only for the youtab user, so an `npm install`
    at task time cannot write node_modules: a package named by a skill and absent
    from the root manifest is a dead instruction, and Word and PowerPoint creation
    simply does not work.
    """
    declared = _declared_npm_packages(_skill_dir(name) / "SKILL.md")
    missing = sorted(declared - _manifest_npm_packages())
    assert not missing, (
        f"{name} skill tells the agent to `npm install {' '.join(missing)}`, but "
        f"package.json does not declare {missing}, so the sealed image has no such "
        f"module. Either add them to the root manifest and lock them, or stop "
        f"naming them in the skill."
    )


def test_the_node_document_generators_are_locked_not_merely_declared():
    """Declared and locked, because the image installs with the lockfile present.

    A manifest entry with no lock entry resolves at build time against the live
    registry, which is exactly the unreproducible install this whole change exists
    to remove.
    """
    lock = (REPO / "package-lock.json").read_text(encoding="utf-8")
    for package in ("docx", "pptxgenjs"):
        assert f'"node_modules/{package}"' in lock, (
            f"{package} is not in package-lock.json, so the image build would "
            f"resolve it against the live npm registry instead of the lock"
        )


# ---------------------------------------------------------------------------
# Embedding detection, exercised without a toolchain.
#
# The render test above skips wherever LibreOffice is absent, which includes CI.
# The embedding check does not need to skip: a PDF shaped like LibreOffice's
# output can be built by hand, and that shape is precisely where the first two
# versions of the check were wrong — once by not dereferencing the indirect
# /Font, once by not following /DescendantFonts. Both would have stopped the
# image building, so both are pinned here, in CI, with no binaries required.
# ---------------------------------------------------------------------------

_ARABIC_SAMPLE = "معماری"


def _type0_pdf(path: Path, *, descendant_embedded: bool) -> Path:
    """A minimal PDF whose page font is Type0 with a CID descendant.

    LibreOffice emits this shape for embedded Arabic: the page font carries no
    /FontDescriptor of its own — the descriptor and its /FontFile2 live on the
    CID font under /DescendantFonts. Identity-H plus a /ToUnicode CMap so the
    drawn glyph codes extract back as the Arabic characters.
    """
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "/CMapName /Custom def /CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\n"
        "endcodespacerange\n%d beginbfchar\n%s\nendbfchar\nendcmap\n"
        "CMapName currentdict /CMap defineresource pop end end"
    ) % (
        len(_ARABIC_SAMPLE),
        "\n".join("<%04X> <%04X>" % (i + 1, ord(c)) for i, c in enumerate(_ARABIC_SAMPLE)),
    )
    glyphs = "".join("%04X" % (i + 1) for i in range(len(_ARABIC_SAMPLE)))
    content = ("BT /F1 18 Tf 400 700 Td <%s> Tj ET" % glyphs).encode("latin-1")
    fontfile = zlib.compress(b"\x00\x01\x00\x00" + b"\x00" * 64)  # stand-in TTF bytes

    descriptor = (
        b"<< /Type /FontDescriptor /FontName /AAAAAA+NotoNaskhArabic /Flags 4 "
        b"/ItalicAngle 0 /Ascent 1069 /Descent -293 /CapHeight 714 /StemV 80 "
        b"/FontBBox [-1000 -300 2000 1100]"
        + (b" /FontFile2 9 0 R" if descendant_embedded else b"")
        + b" >>"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /AAAAAA+NotoNaskhArabic "
        b"/Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 7 0 R >>",
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /AAAAAA+NotoNaskhArabic "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        b"/FontDescriptor 8 0 R /DW 1000 >>",
        b"<< /Length %d >>\nstream\n" % len(cmap.encode("latin-1"))
        + cmap.encode("latin-1")
        + b"\nendstream",
        descriptor,
        b"<< /Length %d /Length1 68 /Filter /FlateDecode >>\nstream\n" % len(fontfile)
        + fontfile
        + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.5\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path.write_bytes(bytes(out))
    return path


def test_the_fixture_really_hides_the_descriptor_on_the_descendant(tmp_path):
    """Guard the guard: if the fixture degrades, the two tests below prove nothing."""
    pdf = _type0_pdf(tmp_path / "type0.pdf", descendant_embedded=True)
    from pypdf import PdfReader

    page = PdfReader(str(pdf)).pages[0]
    font = _resolve(_resolve(page["/Resources"])["/Font"])["/F1"]
    font = _resolve(font)
    assert str(font.get("/Subtype")) == "/Type0", font.get("/Subtype")
    assert font.get("/FontDescriptor") is None, "the fixture must NOT carry a top-level descriptor"
    assert len(_font_descriptors(font)) == 1, "the descriptor must be reachable only via /DescendantFonts"


def test_embedding_is_detected_on_a_type0_descendant_font(tmp_path):
    """LibreOffice's shape must count as embedded.

    Reading only the top-level /FontDescriptor recorded a correctly embedded Noto
    face as unembedded, and because the Dockerfile runs this check during the
    build, that stopped the image building rather than shipping a bad image.
    """
    pdf = _type0_pdf(tmp_path / "type0.pdf", descendant_embedded=True)
    faces = _check_embedded_arabic_font(pdf)
    assert "NotoNaskhArabic" in faces and "embedded" in faces, faces


def test_a_type0_font_without_a_font_file_is_still_rejected(tmp_path):
    """The relaxation must not become "any Type0 font passes"."""
    pdf = _type0_pdf(tmp_path / "type0-unembedded.pdf", descendant_embedded=False)
    with pytest.raises(ToolchainError, match="no font is embedded"):
        _check_embedded_arabic_font(pdf)
