"""Every external input the wake-word pipeline consumes, pinned by SHA-256.

A training run is only reproducible if its inputs are. Each entry below names
the exact bytes the shipped model was built from, so a later run that fetches
different bytes fails loudly here instead of quietly training a different
model. Nothing in this pipeline reads a URL that is not in this table.

Licensing is recorded per asset because the artifacts are redistributed:
``tools/wakewords/hey_youtab.onnx`` and ``.tflite`` are derived works of the
openWakeWord feature extractors and of the speech these datasets contain.
"""

from __future__ import annotations

import hashlib
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: How many bytes to read per chunk when hashing or downloading.
_CHUNK = 1 << 20

#: Network timeout, seconds. Generous: some of these are gigabyte downloads.
_TIMEOUT = 600


@dataclass(frozen=True)
class Asset:
    """One pinned external input."""

    #: Local filename under the download directory.
    name: str
    #: Where to fetch it from.
    url: str
    #: SHA-256 of the exact bytes this pipeline was built against.
    sha256: str
    #: Size in bytes, for a cheap pre-hash mismatch signal.
    size: int
    #: SPDX-style identifier, or a short phrase where no SPDX id applies.
    license: str
    #: Who holds the copyright, and where the licence text lives.
    attribution: str


_OWW = "David Scripka, openWakeWord (github.com/dscripka/openWakeWord)"

#: openWakeWord's shared front end. Every openWakeWord model — the ones
#: upstream ships and the one trained here — is a classifier over the 96-dim
#: embeddings these two produce, so they are inputs to training *and* to
#: inference. The runtime fetches them itself on first use
#: (``openwakeword.utils.download_models``); training pins them so the
#: features a model was fit on are the features it will be scored on.
FEATURE_EXTRACTORS: tuple[Asset, ...] = (
    Asset(
        name="melspectrogram.onnx",
        url="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx",
        sha256="ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
        size=1087958,
        license="Apache-2.0",
        attribution=_OWW,
    ),
    Asset(
        name="melspectrogram.tflite",
        url="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.tflite",
        sha256="96fa0adccb6e8cf95cb14465409a1a2898ee4a96a85bb9ed3c7eb0e68bf163e8",
        size=1092516,
        license="Apache-2.0",
        attribution=_OWW,
    ),
    Asset(
        name="embedding_model.onnx",
        url="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx",
        sha256="70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
        size=1326578,
        license="Apache-2.0",
        attribution=_OWW,
    ),
    Asset(
        name="embedding_model.tflite",
        url="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.tflite",
        sha256="c0aea21eb84a4ce90a08c870da41b7a7173b45269e6a3207c71d67c40f3a59d8",
        size=1330312,
        license="Apache-2.0",
        attribution=_OWW,
    ),
)

#: The multi-speaker VITS generator that synthesizes the spoken phrases. 904
#: speaker embeddings, and the driver mixes pairs of them (SLERP) so the voice
#: pool is far wider than the speaker count. Exported from a LibriTTS-R
#: checkpoint by the piper-sample-generator project.
TTS_GENERATOR = Asset(
    name="en_US-libritts_r-medium.pt",
    url=(
        "https://github.com/rhasspy/piper-sample-generator/releases/download/"
        "v2.0.0/en_US-libritts_r-medium.pt"
    ),
    sha256="e95ee53770bf598c354a6e6dbfc95ccb259aeeb501d35a86be8a767429ab0ff6",
    size=204089915,
    license="MIT (generator code) over LibriTTS-R (CC BY 4.0)",
    attribution=(
        "Michael Hansen, piper-sample-generator "
        "(github.com/rhasspy/piper-sample-generator); LibriTTS-R corpus, "
        "Koizumi et al. 2023, CC BY 4.0"
    ),
)

#: Recorded human speech, used only as negatives. 105,829 one-second
#: utterances of 35 words from 2,618 speakers, plus six background-noise
#: recordings. None of the 35 words is the wake phrase or any part of it, so
#: every clip is a true negative by construction — no labelling needed.
NEGATIVE_SPEECH = Asset(
    name="speech_commands_v0.02.tar.gz",
    url=(
        "https://storage.googleapis.com/download.tensorflow.org/data/"
        "speech_commands_v0.02.tar.gz"
    ),
    sha256="af14739ee7dc311471de98f5f9d2c9191b18aedfe957f4a6ff791c709868ff58",
    size=2428923189,
    license="CC BY 4.0",
    attribution=(
        "Warden, P. Speech Commands: A Dataset for Limited-Vocabulary Speech "
        "Recognition (2018), arXiv:1804.03209. Google LLC, CC BY 4.0"
    ),
)

ALL_ASSETS: tuple[Asset, ...] = (*FEATURE_EXTRACTORS, TTS_GENERATOR, NEGATIVE_SPEECH)


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file, read in chunks so size does not matter."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(asset: Asset, into: Path) -> Path:
    """Download ``asset`` into ``into`` unless it is already there and correct.

    Verification is unconditional: an already-present file is hashed too, so a
    truncated or tampered cache is caught rather than reused.
    """
    into.mkdir(parents=True, exist_ok=True)
    target = into / asset.name
    if target.exists() and sha256_file(target) == asset.sha256:
        return target

    if not asset.url.startswith("https://"):
        raise ValueError(f"refusing non-HTTPS asset URL: {asset.url}")
    partial = target.with_suffix(target.suffix + ".partial")
    with urllib.request.urlopen(asset.url, timeout=_TIMEOUT) as response:  # noqa: S310
        with partial.open("wb") as handle:
            shutil.copyfileobj(response, handle, _CHUNK)

    actual = sha256_file(partial)
    if actual != asset.sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"{asset.name}: SHA-256 mismatch\n"
            f"  expected {asset.sha256}\n"
            f"  got      {actual}\n"
            f"  from     {asset.url}"
        )
    partial.replace(target)
    return target
