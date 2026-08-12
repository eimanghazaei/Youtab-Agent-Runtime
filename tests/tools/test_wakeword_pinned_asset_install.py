"""Pinned wake-word assets must be *installed*, not merely downloaded.

Background
----------
``scripts/wakeword/assets.py`` pinned openWakeWord's melspectrogram and
embedding models by SHA-256 and said, in its own docstring, that this keeps
"the features a model was fit on ... the features it will be scored on". That
was the intent, but nothing ever put those bytes where openWakeWord loads them
from, so the pin was inert. openWakeWord ships no models inside its wheel, so a
clean environment had an empty ``resources/models`` and the first
``AudioFeatures(...)`` died with ``NO_SUCHFILE`` part-way into a multi-hour
pipeline run. The alternative failure is worse and silent: any caller reaching
for ``openwakeword.utils.download_models()`` trains against whatever upstream
published that day rather than against the pinned bytes.

A second input had the same shape of bug. ``generate_speech._load_generator``
opens ``<checkpoint>.json`` beside the VITS checkpoint, and
piper-sample-generator keeps that config inside its git repository instead of
publishing it as a release asset — the mirror of the ``.pt`` release URL
returns 404 — so it could never be fetched and was never installed either.

These tests are hermetic. They build synthetic assets over temporary files and
hash them for real, so the install logic is exercised without the ~2.6 GB of
genuine inputs, which CI does not and should not download.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import assets  # noqa: E402


def _asset(path: Path, name: str) -> assets.Asset:
    """A pinned Asset describing bytes that actually exist on disk."""
    return assets.Asset(
        name=name,
        url=f"https://example.invalid/{name}",
        sha256=assets.sha256_file(path),
        size=path.stat().st_size,
        license="Apache-2.0",
        attribution="synthetic fixture",
    )


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "downloads"
    src.mkdir()
    (src / "alpha.onnx").write_bytes(b"alpha model bytes" * 64)
    (src / "beta.tflite").write_bytes(b"beta model bytes" * 64)
    return src


@pytest.fixture()
def pinned(source_dir: Path) -> tuple[assets.Asset, ...]:
    return (
        _asset(source_dir / "alpha.onnx", "alpha.onnx"),
        _asset(source_dir / "beta.tflite", "beta.tflite"),
    )


def test_install_pinned_places_every_asset_with_matching_bytes(
    source_dir: Path, pinned: tuple[assets.Asset, ...], tmp_path: Path
) -> None:
    dest = tmp_path / "resources" / "models"
    installed = assets.install_pinned(pinned, source_dir, dest)

    assert {p.name for p in installed} == {a.name for a in pinned}
    for asset in pinned:
        target = dest / asset.name
        assert target.exists(), f"{asset.name} was not installed"
        assert assets.sha256_file(target) == asset.sha256


def test_install_pinned_is_idempotent(
    source_dir: Path, pinned: tuple[assets.Asset, ...], tmp_path: Path
) -> None:
    """Safe to run at the top of every pipeline invocation."""
    dest = tmp_path / "models"
    assets.install_pinned(pinned, source_dir, dest)
    stamps = {p.name: (dest / p.name).stat().st_mtime_ns for p in dest.iterdir()}

    assets.install_pinned(pinned, source_dir, dest)

    for name, before in stamps.items():
        assert (dest / name).stat().st_mtime_ns == before, (
            f"{name} was rewritten on a second install; it already matched"
        )


def test_install_pinned_refuses_a_corrupted_source(
    source_dir: Path, pinned: tuple[assets.Asset, ...], tmp_path: Path
) -> None:
    """A byte-flip at the source must stop the run, not propagate.

    This is the control that makes the pin mean anything: without it a
    tampered or truncated cache installs cleanly and the model trains on
    features nobody pinned.
    """
    (source_dir / "beta.tflite").write_bytes(b"tampered")
    dest = tmp_path / "models"

    with pytest.raises(RuntimeError, match=r"SHA-256 mismatch at the source"):
        assets.install_pinned(pinned, source_dir, dest)

    assert not (dest / "beta.tflite").exists(), (
        "a corrupted asset was installed anyway"
    )


def test_install_pinned_names_the_fetch_step_when_the_source_is_missing(
    source_dir: Path, pinned: tuple[assets.Asset, ...], tmp_path: Path
) -> None:
    (source_dir / "alpha.onnx").unlink()
    with pytest.raises(FileNotFoundError, match=r"fetch step"):
        assets.install_pinned(pinned, source_dir, tmp_path / "models")


def test_generator_config_install_verifies_its_pin(tmp_path: Path) -> None:
    """The VITS config is copied from the clone and hash-checked."""
    generator_root = tmp_path / "piper-sample-generator"
    models = generator_root / "models"
    models.mkdir(parents=True)
    config = models / assets.GENERATOR_CONFIG_NAME
    config.write_bytes(b"not the real config")

    into = tmp_path / "downloads"
    with pytest.raises(RuntimeError, match=r"SHA-256 mismatch"):
        assets.install_generator_config(generator_root, into)
    assert not (into / assets.GENERATOR_CONFIG_NAME).exists()


def test_generator_config_install_reports_a_missing_clone(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"piper-sample-generator"):
        assets.install_generator_config(tmp_path / "absent", tmp_path / "downloads")


def test_feature_extractor_pins_cover_everything_openwakeword_fetches() -> None:
    """Every model openWakeWord would download itself must be pinned.

    The front end is a melspectrogram model and an embedding model, in both
    backends — ONNX on most platforms, tflite on macOS ARM64 — so pinning three
    of those four leaves one backend fetching an unpinned model.

    ``silero_vad.onnx`` belongs here too even though nothing in
    ``tools/wake_word.py`` references VAD: openWakeWord fetches it while
    loading a model regardless. Unpinned, it turned into a mid-evaluation
    network call that several ProcessPoolExecutor workers raced to write, and
    the loser killed the run with a BrokenProcessPool carrying no cause.
    """
    names = {a.name for a in assets.FEATURE_EXTRACTORS}
    assert names == {
        "melspectrogram.onnx",
        "melspectrogram.tflite",
        "embedding_model.onnx",
        "embedding_model.tflite",
        "silero_vad.onnx",
    }


def test_pipeline_actually_installs_the_pinned_assets() -> None:
    """The generator can be pinned perfectly and still never be installed.

    That was the defect. This asserts the pipeline calls the installers, so
    removing the call fails here instead of failing hours into a clean run.
    """
    pipeline = (WAKEWORD / "run_pipeline.sh").read_text(encoding="utf-8")

    # Non-vacuity: prove we are reading the real step-0 body before asserting
    # anything about its contents.
    assert "assets.fetch(asset, into)" in pipeline, (
        "step 0 no longer fetches assets the way this test assumes; re-anchor it"
    )
    for call in ("install_feature_extractors", "install_generator_config"):
        assert re.search(rf"assets\.{call}\(", pipeline), (
            f"run_pipeline.sh never calls assets.{call}(). Pinning an asset "
            "without installing it is exactly the bug this guards: the pin "
            "reads as protection while the runtime loads something else."
        )
