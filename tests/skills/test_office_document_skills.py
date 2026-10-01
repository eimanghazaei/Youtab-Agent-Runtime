"""Invariant tests for the bundled office/document skills.

Covers skills/productivity/{docx,xlsx,pdf,powerpoint} — the office
document creation/editing suite. Tests assert contracts (frontmatter
shape, referenced scripts exist, cross-links resolve), not snapshots
of skill content.
"""

from __future__ import annotations

import re
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
    for pattern in (r"^dependencies = \[(.*?)^\]", r"^documents = \[(.*?)^\]"):
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
