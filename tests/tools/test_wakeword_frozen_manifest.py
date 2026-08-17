"""A frozen dataset must stay frozen, and a sealed one must stay sealed.

Background
----------
Downloaded inputs are pinned by SHA-256 in ``assets.py`` and a rerun that
fetches different bytes fails immediately. Recorded datasets get none of that:
a folder of takes is as authoritative as whatever is in it today, so a
re-export, a sync client dropping a file, or a take deleted for sounding bad
all change what "measured on E002" means without leaving a trace.

The second failure is the expensive one. A single-use evaluation speaker is
worth exactly its unseenness, and one ``--data-dir`` pointed at the wrong
folder spends it permanently — re-recording the same person does not restore
it, because they are no longer unseen. So usage is a field that raises, not a
line in a README.

These tests are hermetic: every dataset here is a handful of bytes written into
``tmp_path``. No real recording is read, and nothing outside the temporary
directory is touched.

Where the environment provides ``sha256sum`` the sidecar is checked by running
it, because a verifier that is the only thing able to verify its own output
proves less than it looks like it does. Where it does not, the format itself is
still asserted byte-for-byte against what coreutils reads — the check narrows,
it does not disappear.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_module():
    """Load the script by path, the way the other wake-word tests do.

    ``sys.modules`` registration is not optional here: ``@dataclass`` resolves
    string annotations through ``sys.modules[cls.__module__]``, and a module
    loaded from a spec without being registered has no entry, so class creation
    dies with an ``AttributeError`` on ``None``.
    """
    spec = importlib.util.spec_from_file_location(
        "_freeze_manifest", REPO / "scripts" / "wakeword" / "freeze_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fm = _load_module()


@pytest.fixture()
def dataset(tmp_path: Path) -> Path:
    """A small dataset that looks like a recording session."""
    root = tmp_path / "E003"
    (root / "positives").mkdir(parents=True)
    (root / "near").mkdir()
    for index in range(4):
        (root / "positives" / f"take_{index:02d}.wav").write_bytes(b"RIFF" + bytes([index]) * 64)
    (root / "near" / "okay_youtab.wav").write_bytes(b"RIFF" + b"\x07" * 64)
    (root / ".DS_Store").write_bytes(b"junk")  # must be ignored
    return root


@pytest.fixture()
def manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "E003.manifest.json"


def _freeze(root: Path, out: Path, **overrides):
    kwargs = {
        "dataset": "E003",
        "split": "evaluate",
        "usage": "sealed-evaluation",
    }
    kwargs.update(overrides)
    return fm.freeze(root, out, **kwargs)


# ── freezing ─────────────────────────────────────────────────────────────────


def test_freeze_hashes_every_file_and_the_list_itself(dataset: Path, manifest_path: Path):
    manifest = _freeze(dataset, manifest_path)

    # Non-vacuity: five files, and the litter file excluded. A manifest over an
    # empty list would satisfy every "nothing changed" assertion below.
    assert manifest["file_count"] == 5, manifest["files"]
    assert {entry["path"] for entry in manifest["files"]} == {
        "positives/take_00.wav",
        "positives/take_01.wav",
        "positives/take_02.wav",
        "positives/take_03.wav",
        "near/okay_youtab.wav",
    }
    for entry in manifest["files"]:
        assert len(entry["sha256"]) == 64
        assert entry["bytes"] > 0
    # Every digest distinct, so the hashing is per-file and not one value
    # copied across the list.
    assert len({entry["sha256"] for entry in manifest["files"]}) == 5

    assert manifest["manifest_sha256"] == fm.manifest_digest(manifest)
    assert manifest["total_bytes"] == sum(e["bytes"] for e in manifest["files"])


def test_the_manifest_identity_is_the_content_and_not_the_formatting(
    dataset: Path, manifest_path: Path
):
    """Content-addressed, so a reformat is not a change and an edit is."""
    manifest = _freeze(dataset, manifest_path)
    reformatted = json.loads(json.dumps(manifest, indent=8))
    assert fm.manifest_digest(reformatted) == manifest["manifest_sha256"]

    tampered = json.loads(json.dumps(manifest))
    tampered["files"][0]["sha256"] = "0" * 64
    assert fm.manifest_digest(tampered) != manifest["manifest_sha256"]


def test_freeze_is_a_no_op_when_nothing_changed(dataset: Path, manifest_path: Path):
    first = _freeze(dataset, manifest_path)
    stamp = manifest_path.stat().st_mtime_ns

    again = _freeze(dataset, manifest_path)

    assert again["manifest_sha256"] == first["manifest_sha256"]
    assert manifest_path.stat().st_mtime_ns == stamp, "the manifest was rewritten"


def test_freeze_refuses_to_overwrite_a_manifest_that_no_longer_matches(
    dataset: Path, manifest_path: Path
):
    """The control that makes freezing mean anything.

    Overwriting silently would make the manifest a description of the present,
    which is the thing a directory listing already is.
    """
    original = _freeze(dataset, manifest_path)
    (dataset / "positives" / "take_00.wav").write_bytes(b"RIFF re-exported")

    with pytest.raises(fm.ManifestConflict, match=r"1 changed"):
        _freeze(dataset, manifest_path)

    assert fm.load(manifest_path)["manifest_sha256"] == original["manifest_sha256"], (
        "the refused freeze still modified the manifest on disk"
    )


def test_freeze_reports_what_moved_rather_than_only_that_something_did(
    dataset: Path, manifest_path: Path
):
    _freeze(dataset, manifest_path)
    (dataset / "positives" / "take_03.wav").unlink()
    (dataset / "positives" / "take_09.wav").write_bytes(b"RIFF new take")

    with pytest.raises(fm.ManifestConflict) as caught:
        _freeze(dataset, manifest_path)
    message = str(caught.value)
    assert "1 added" in message and "1 removed" in message
    assert "take_09.wav" in message and "take_03.wav" in message


def test_freeze_refuses_an_empty_dataset(tmp_path: Path):
    """An empty manifest records that a dataset was verified when nothing was."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(ValueError, match=r"no files found"):
        fm.freeze(empty, tmp_path / "m.json", dataset="X", split="evaluate", usage="training")


def test_freeze_refuses_to_write_a_manifest_into_the_repository(dataset: Path):
    """Filenames of private recordings are part of what stays on local disk."""
    with pytest.raises(ValueError, match=r"refusing to write a manifest inside"):
        _freeze(dataset, REPO / "docs" / "E003.manifest.json")
    assert not (REPO / "docs" / "E003.manifest.json").exists()


def test_a_manifest_never_records_where_the_dataset_lives(dataset: Path, manifest_path: Path):
    manifest = _freeze(dataset, manifest_path)
    serialized = json.dumps(manifest)
    assert manifest["root_name"] == "E003"
    assert str(dataset.parent) not in serialized, "the manifest embeds the capture root"
    assert str(dataset) not in serialized


# ── verifying ────────────────────────────────────────────────────────────────


def test_verify_passes_on_an_untouched_dataset(dataset: Path, manifest_path: Path):
    _freeze(dataset, manifest_path)
    report = fm.verify(manifest_path, dataset)
    assert report["passed"], report
    assert report["files_checked"] == 5  # non-vacuity: it checked all of them
    assert report["manifest_digest_matches"]
    assert not report["missing"] and not report["changed"] and not report["extra"]


def test_verify_catches_an_edited_file(dataset: Path, manifest_path: Path):
    _freeze(dataset, manifest_path)
    (dataset / "near" / "okay_youtab.wav").write_bytes(b"RIFF" + b"\x08" * 64)

    report = fm.verify(manifest_path, dataset)
    assert report["changed"] == ["near/okay_youtab.wav"]
    assert not report["passed"]


def test_verify_catches_a_missing_file(dataset: Path, manifest_path: Path):
    _freeze(dataset, manifest_path)
    (dataset / "positives" / "take_02.wav").unlink()

    report = fm.verify(manifest_path, dataset)
    assert report["missing"] == ["positives/take_02.wav"]
    assert not report["passed"]


def test_verify_reports_unlisted_files_and_only_fails_on_them_when_strict(
    dataset: Path, manifest_path: Path
):
    """An extra file is ambiguous, so the caller decides.

    A stray export sitting beside the frozen takes does not invalidate the
    frozen ones — but for a sealed evaluation set, something arriving in the
    directory afterwards is exactly what you want to hear about.
    """
    _freeze(dataset, manifest_path)
    (dataset / "positives" / "take_99.wav").write_bytes(b"RIFF late arrival")

    lenient = fm.verify(manifest_path, dataset)
    assert lenient["extra"] == ["positives/take_99.wav"]
    assert lenient["passed"]

    strict = fm.verify(manifest_path, dataset, strict=True)
    assert not strict["passed"]


def test_verify_catches_an_edited_manifest(dataset: Path, manifest_path: Path):
    """The hash-fixing case: somebody makes the manifest agree with the data.

    Per-file hashes cannot catch this — after the edit they match. The
    manifest's own digest is what does.
    """
    _freeze(dataset, manifest_path)
    manifest = fm.load(manifest_path)
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    report = fm.verify(manifest_path, dataset)
    assert not report["manifest_digest_matches"]
    assert not report["passed"]


def test_a_manifest_from_another_schema_is_refused_rather_than_guessed_at(
    dataset: Path, manifest_path: Path
):
    _freeze(dataset, manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=r"schema_version"):
        fm.load(manifest_path)


# ── independent verification ─────────────────────────────────────────────────


def test_the_sidecars_are_in_the_format_coreutils_reads(dataset: Path, manifest_path: Path):
    manifest = _freeze(dataset, manifest_path)
    sums = manifest_path.with_name(manifest_path.stem + ".SHA256SUMS")
    self_sum = manifest_path.with_name(manifest_path.name + ".sha256")

    lines = sums.read_text(encoding="utf-8").splitlines()
    assert len(lines) == manifest["file_count"]
    for line in lines:
        digest, sep, name = line.partition("  ")
        assert sep == "  ", f"not `<digest>  <name>`: {line!r}"
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        assert (dataset / name).is_file()

    assert self_sum.read_text(encoding="utf-8").strip().endswith(manifest_path.name)


def test_an_independent_tool_confirms_the_same_files(dataset: Path, manifest_path: Path):
    """`sha256sum -c` reaches the same verdict, without running any of this code.

    Not skipped when the binary is absent: the format assertions above still
    run, and this reports the narrowing rather than hiding it.
    """
    sha256sum = shutil.which("sha256sum") or shutil.which("shasum")
    if not sha256sum:
        pytest.fail(
            "no sha256sum or shasum on PATH, so the independent check could not "
            "run here. The format is still asserted by the test above; install "
            "coreutils to close the gap."
        )

    _freeze(dataset, manifest_path)
    sums = manifest_path.with_name(manifest_path.stem + ".SHA256SUMS")
    argv = [sha256sum, "-c", str(sums)]
    if sha256sum.endswith(("shasum", "shasum.exe")):
        argv = [sha256sum, "-a", "256", "-c", str(sums)]

    ok = subprocess.run(argv, cwd=dataset, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ok.stdout.count(": OK") == 5, ok.stdout

    (dataset / "positives" / "take_01.wav").write_bytes(b"RIFF tampered")
    bad = subprocess.run(argv, cwd=dataset, capture_output=True, text=True)
    assert bad.returncode != 0, "the independent tool accepted a tampered file"


# ── the sealed set ───────────────────────────────────────────────────────────


def test_a_sealed_evaluation_set_cannot_be_consumed_for_training(
    dataset: Path, manifest_path: Path
):
    manifest = _freeze(dataset, manifest_path, usage="sealed-evaluation")

    with pytest.raises(fm.SealedDatasetError, match=r"sealed evaluation set"):
        fm.assert_usable_for(manifest, "training")
    with pytest.raises(fm.SealedDatasetError):
        fm.assert_usable_for(manifest, "validation")

    # And the positive control: it is still usable for the one thing it is for.
    fm.assert_usable_for(manifest, "evaluation")


def test_a_training_set_cannot_be_used_as_a_measurement(dataset: Path, manifest_path: Path):
    """The mirror image. E001 is training-only; measuring on it is a fiction."""
    manifest = _freeze(dataset, manifest_path, dataset="E001", split="train", usage="training")

    fm.assert_usable_for(manifest, "training")
    with pytest.raises(ValueError, match=r"does not permit evaluation"):
        fm.assert_usable_for(manifest, "evaluation")


def test_usage_must_be_one_of_the_declared_kinds(dataset: Path, manifest_path: Path):
    with pytest.raises(ValueError, match=r"usage must be one of"):
        _freeze(dataset, manifest_path, usage="whatever")


# ── the command line ─────────────────────────────────────────────────────────


def test_the_cli_freezes_verifies_and_refuses(dataset: Path, tmp_path: Path, capsys):
    """The path a pipeline stage would actually call, exit codes included."""
    out = tmp_path / "cli.manifest.json"
    argv = [
        "freeze",
        "--root", str(dataset),
        "--out", str(out),
        "--dataset", "E003",
        "--split", "evaluate",
        "--usage", "sealed-evaluation",
    ]
    assert fm.main(argv) == 0
    assert out.is_file()

    assert fm.main(["verify", "--root", str(dataset), "--manifest", str(out)]) == 0

    # A sealed set requested for training exits non-zero, so a shell `||` or a
    # `set -e` pipeline stops instead of proceeding.
    assert fm.main(["usable", "--manifest", str(out), "--for", "training"]) == 1
    assert fm.main(["usable", "--manifest", str(out), "--for", "evaluation"]) == 0

    (dataset / "positives" / "take_00.wav").unlink()
    assert fm.main(["verify", "--root", str(dataset), "--manifest", str(out)]) == 1

    printed = capsys.readouterr()
    assert str(dataset) not in printed.out, "the CLI printed the capture root"
    assert "missing: 1" in printed.out
