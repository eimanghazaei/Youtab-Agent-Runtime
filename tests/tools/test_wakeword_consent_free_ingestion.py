"""Consent is out of the wake-word speaker pipeline; the registry is the authority.

Per the Owner's governance mandate, the per-speaker *consent document* is removed
entirely from the technical pipeline. Technical acceptance depends ONLY on audio
properties, labels, role separation, source integrity (checksums) and model
quality -- never on paperwork. Authorization to use a speaker's recordings is a
single, non-identifying, project-level fact: membership of the trusted speaker
registry ``speaker_recording_spec.SPEAKER_ASSIGNMENTS``. No document, signature,
legal name, consent date or consent-file hash may be required, copied, hashed
into evidence, or included in any dataset.

This file proves the rule two ways:

* Regression tests exercise the real validators and importer and show that a
  consent-free submission is GREEN and imports clean; that a missing, partial or
  field-short ``RECORDING_METADATA.json`` is diagnostic and never blocks; that a
  stray ``CONSENT.pdf`` is tolerated and appears nowhere in the outputs; and that
  role separation (E001 training-only, E002 validation-only) is registry-enforced
  and cannot be moved by a caller flag, folder name, filename or manifest.

* Mutation tests re-introduce each removed gate in the source, run a named
  regression target in a fresh subprocess, and assert it turns RED -- then
  restore the byte-exact original and assert it is GREEN again. A mutation that
  survives is a test that was not actually enforcing the rule.

Everything is hermetic: submissions are built under ``tmp_path`` from generated
tones plus dither, no speech and not one byte of anyone's recording.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import import_speaker as imp  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402
from recording_assistant import core  # noqa: E402

# The full compact-folder builders (audio + metadata + checksums), reused so the
# consent-free assertions run against exactly the tree the rest of the suite
# builds. The builders are consent-free by design.
from tests.tools.test_wakeword_compact_ingestion import (  # noqa: E402
    COMPACT_TRAINING,
    COMPACT_VALIDATION,
    build_compact_submission,
)

THIS_FILE = "tests/tools/test_wakeword_consent_free_ingestion.py"
PRIVATE_NAME = next(iter(spec.PRIVATE_DOC_NAMES))  # "CONSENT.pdf"


@pytest.fixture()
def into(tmp_path: Path) -> Path:
    """A fresh external data root to import into."""
    root = tmp_path / "human"
    root.mkdir()
    return root


# ── 1. a consent-free submission is GREEN and imports clean ──────────────────


def test_compact_submission_without_consent_validates_green_and_imports_clean(
    tmp_path: Path, into: Path
) -> None:
    label = COMPACT_TRAINING
    submission = build_compact_submission(tmp_path / label, label)
    # No consent document anywhere in the built submission.
    assert not (submission / PRIVATE_NAME).exists()

    result = core.validate_compact_submission(submission, core.build_plan(label))
    assert result.ok, f"consent-free submission must be GREEN: {result.errors}"
    assert result.errors == [] and result.warnings == []

    # Imports clean (plan writes nothing -- the Owner's dry-run pass).
    state = imp.plan(label, submission, into)
    assert state.ok, {check.name: check.problems for check in state.failures}
    # There is no "consent" check at all: the gate is gone, not merely satisfied.
    assert "consent" not in {check.name for check in state.checks}


# ── 2. metadata is diagnostic: absent / partial / field-short never blocks ───


def test_absent_partial_or_field_missing_metadata_does_not_block(
    tmp_path: Path, into: Path
) -> None:
    label = COMPACT_TRAINING

    # (a) absent RECORDING_METADATA.json
    submission = build_compact_submission(tmp_path / "absent" / label, label)
    (submission / spec.METADATA_FILE).unlink()
    core.write_sha256sums(submission)  # re-list without the diagnostic form
    plan = core.build_plan(label)
    assert core.validate_compact_submission(submission, plan).ok
    assert imp.plan(label, submission, into).ok

    # (b) partial RECORDING_METADATA.json (only two fields)
    submission = build_compact_submission(tmp_path / "partial" / label, label)
    (submission / spec.METADATA_FILE).write_text(
        json.dumps({"speaker_id": label, "recording_date": "2024-02-29"}, indent=1),
        encoding="utf-8",
    )
    core.write_sha256sums(submission)
    into_b = into / "b"
    into_b.mkdir()
    assert core.validate_compact_submission(submission, core.build_plan(label)).ok
    assert imp.plan(label, submission, into_b).ok

    # (c) present but every field blank
    submission = build_compact_submission(tmp_path / "blank" / label, label)
    blank = {name: "" for name in spec.DIAGNOSTIC_METADATA_FIELDS}
    blank["noise_sources_used"] = []
    (submission / spec.METADATA_FILE).write_text(json.dumps(blank, indent=1), encoding="utf-8")
    core.write_sha256sums(submission)
    into_c = into / "c"
    into_c.mkdir()
    assert core.validate_compact_submission(submission, core.build_plan(label)).ok
    assert imp.plan(label, submission, into_c).ok


# ── 3. a stray CONSENT.pdf is tolerated and appears nowhere in the outputs ───


def test_stray_consent_pdf_is_tolerated_and_absent_from_all_outputs(
    tmp_path: Path, into: Path
) -> None:
    label = COMPACT_TRAINING
    submission = build_compact_submission(tmp_path / label, label)

    private_bytes = b"%PDF-1.4 the Owner's private document, outside the pipeline"
    private_hash = hashlib.sha256(private_bytes).hexdigest()
    (submission / PRIVATE_NAME).write_bytes(private_bytes)

    # It does not block the compact validator, and it is not listed/hashed in
    # the manifest the validator verifies.
    result = core.validate_compact_submission(submission, core.build_plan(label))
    assert result.ok, f"a stray private document must not block: {result.errors}"
    sums = (submission / spec.CHECKSUM_FILE).read_text(encoding="utf-8")
    assert PRIVATE_NAME not in sums
    assert private_hash not in sums

    # It does not block the import, is never copied to the data root, and its
    # bytes/hash/name appear nowhere in the evidence the import writes.
    state = imp.plan(label, submission, into)
    assert state.ok, {check.name: check.problems for check in state.failures}
    body = imp.execute(state, into)

    derived = into / (imp.SPEAKER_DIR_PREFIX + label)
    copied = {p.name for p in derived.rglob("*") if p.is_file()}
    assert PRIVATE_NAME not in copied

    text = json.dumps(body)
    assert PRIVATE_NAME not in text
    assert private_hash not in text
    assert "consent" not in text.lower()
    for evidence in derived.rglob("*"):
        if evidence.is_file():
            blob = evidence.read_bytes()
            assert private_bytes not in blob
            assert private_hash.encode() not in blob


# ── 4. role separation is registry-enforced and not overridable ──────────────


def test_role_separation_is_registry_enforced_e001_train_e002_validation() -> None:
    # The registry alone decides the role, and the two compact speakers are
    # split training-only / validation-only.
    assert imp.assignment(COMPACT_TRAINING).role == spec.ROLE_TRAINING
    assert imp.assignment(COMPACT_VALIDATION).role == spec.ROLE_VALIDATION
    assert COMPACT_TRAINING != COMPACT_VALIDATION


def test_a_caller_flag_folder_or_manifest_cannot_change_the_role(
    tmp_path: Path, into: Path
) -> None:
    label = COMPACT_TRAINING
    submission = build_compact_submission(tmp_path / label, label)

    # A metadata manifest naming the other speaker does not move the role.
    (submission / spec.METADATA_FILE).write_text(
        json.dumps({"speaker_id": COMPACT_VALIDATION}, indent=1), encoding="utf-8"
    )
    core.write_sha256sums(submission)

    # The --layout flag can only tighten the profile; it never changes the role.
    for layout in ("auto", "package"):
        target = into / layout  # a per-iteration data root
        target.mkdir(parents=True, exist_ok=True)
        state = imp.plan(label, submission, target, layout=layout)
        assert state.assignment.role == spec.ROLE_TRAINING, (
            f"layout={layout!r} moved the registry role"
        )
    assert imp.assignment(label).role == spec.ROLE_TRAINING


# ── mutation targets: lightweight, no audio, sensitive to a re-introduced gate ─


def _minimal_compact_folder(tmp_path: Path, label: str) -> Path:
    """A folder named for a compact speaker, with an empty ``originals/`` and no

    audio. Enough for the compact validator to run; it reports the missing takes
    but -- crucially for these targets -- says nothing about consent or metadata
    unless a gate has been re-introduced.
    """
    root = tmp_path / label
    (root / spec.ORIGINALS_DIR).mkdir(parents=True)
    return root


def test_mut_target_consent_not_required(tmp_path: Path) -> None:
    label = COMPACT_TRAINING
    root = _minimal_compact_folder(tmp_path, label)
    result = core.validate_compact_submission(root, core.build_plan(label))
    # The folder is incomplete, so it is not GREEN -- but no error is about a
    # consent document. A re-introduced consent gate breaks exactly this.
    assert not any("CONSENT" in e for e in result.errors), result.errors


def test_mut_target_metadata_not_gating(tmp_path: Path) -> None:
    label = COMPACT_TRAINING
    root = _minimal_compact_folder(tmp_path, label)
    blank = {name: "" for name in spec.DIAGNOSTIC_METADATA_FIELDS}
    blank["noise_sources_used"] = []
    (root / spec.METADATA_FILE).write_text(json.dumps(blank, indent=1), encoding="utf-8")
    result = core.validate_compact_submission(root, core.build_plan(label))
    # Every metadata field is blank; that must not produce a metadata error.
    assert not any(spec.METADATA_FILE in e for e in result.errors), result.errors


def test_mut_target_role_registry_enforced(tmp_path: Path) -> None:
    label = COMPACT_TRAINING
    assert imp.assignment(label).role == spec.ROLE_TRAINING
    assert imp.assignment(COMPACT_VALIDATION).role == spec.ROLE_VALIDATION
    # The layout flag must not change the resolved role.
    root = _minimal_compact_folder(tmp_path, label)
    into = tmp_path / "into"
    into.mkdir()
    state = imp.plan(label, root, into, layout="package")
    assert state.assignment.role == spec.ROLE_TRAINING


# ── mutation harness: re-introduce a removed gate, prove a named test goes RED ─

CORE_PY = WAKEWORD / "recording_assistant" / "core.py"
IMPORT_PY = WAKEWORD / "import_speaker.py"


def _run_named(node: str) -> int:
    """Run one named test in this file in a fresh subprocess; return its exit code.

    A subprocess re-imports the (possibly mutated) source from disk, so the
    verdict reflects the bytes on disk right now rather than the module already
    imported into this process.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(WAKEWORD), str(REPO)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{THIS_FILE}::{node}",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode


def _mutate_and_assert_red_then_restore(
    target: Path, anchor: str, replacement: str, node: str
) -> None:
    """Byte-exact backup, anchored replace, assert RED under the mutation, and

    assert GREEN once the original bytes are restored.
    """
    backup = target.read_bytes()
    # Match the file's own newline convention so the anchor is found whether the
    # source is stored LF or CRLF; the restore below is byte-exact regardless.
    newline = b"\r\n" if b"\r\n" in backup else b"\n"
    anchor_b = anchor.encode().replace(b"\n", newline)
    replacement_b = replacement.encode().replace(b"\n", newline)
    assert anchor_b in backup, f"mutation anchor not found in {target.name}"
    mutated = backup.replace(anchor_b, replacement_b, 1)
    assert mutated != backup, "anchored replace produced no change"
    try:
        target.write_bytes(mutated)
        assert _run_named(node) != 0, (
            f"{node} still passed after re-introducing the gate -- the test does "
            "not actually enforce the rule"
        )
    finally:
        target.write_bytes(backup)
    assert target.read_bytes() == backup, "failed to restore the original bytes"
    assert _run_named(node) == 0, f"{node} did not return GREEN after restore"


def test_mutation_reintroducing_a_consent_requirement_is_caught() -> None:
    anchor = (
        "    _check_compact_originals(root, plan, result)\n"
        "    _check_compact_metadata(root, plan, result)\n"
    )
    replacement = (
        "    _check_compact_originals(root, plan, result)\n"
        '    if not (root / "CONSENT.pdf").is_file():\n'
        '        result.errors.append("missing CONSENT.pdf: consent record required")\n'
        "    _check_compact_metadata(root, plan, result)\n"
    )
    _mutate_and_assert_red_then_restore(
        CORE_PY, anchor, replacement, "test_mut_target_consent_not_required"
    )


def test_mutation_making_metadata_gating_again_is_caught() -> None:
    anchor = (
        "    if not isinstance(data, dict):\n"
        "        return\n"
        "\n"
        "    result.metadata_fields_present = [\n"
    )
    replacement = (
        "    if not isinstance(data, dict):\n"
        "        return\n"
        "\n"
        "    for _name in spec.DIAGNOSTIC_METADATA_FIELDS:\n"
        '        if _name != "noise_sources_used" and not str(data.get(_name, "")).strip():\n'
        '            result.errors.append(f"{spec.METADATA_FILE} field {_name!r} is blank")\n'
        "    result.metadata_fields_present = [\n"
    )
    _mutate_and_assert_red_then_restore(
        CORE_PY, anchor, replacement, "test_mut_target_metadata_not_gating"
    )


def test_mutation_weakening_role_separation_is_caught() -> None:
    anchor = (
        "    where = assignment(label)\n"
        '    _refuse_repo_path(submission, "a submission")\n'
    )
    replacement = (
        "    where = assignment(label)\n"
        '    if layout != "auto":\n'
        "        where = Assignment(where.label, layout, where.split, where.why)\n"
        '    _refuse_repo_path(submission, "a submission")\n'
    )
    _mutate_and_assert_red_then_restore(
        IMPORT_PY, anchor, replacement, "test_mut_target_role_registry_enforced"
    )
