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
    #: ``Source.key`` of the project or corpus these bytes come from. Required,
    #: with no default: a file whose origin nobody named is exactly the thing
    #: ``tests/tools/test_wakeword_asset_licences.py`` exists to prevent, and a
    #: default would let one be added by forgetting rather than by deciding.
    source: str


@dataclass(frozen=True)
class Source:
    """One external project or corpus, and what is known about its licence.

    ``Asset`` records bytes; this records the *provenance* of bytes. They are
    separate because the two do not correspond one-to-one: openWakeWord's
    release carries five files from three upstreams, and two of the projects
    this pipeline depends on (pyroomacoustics, espeak-ng) contribute no
    redistributed bytes at all — they generate or transform data at build time,
    which is a different licence question and one that is easy to leave
    unasked.

    Sources that were considered and *not* used are recorded here too. A corpus
    that is absent because nobody wanted it and a corpus that is absent because
    its licence was refused look identical in a dependency list, and only one
    of those is a decision.
    """

    #: Stable identifier, referenced by ``Asset.source``.
    key: str
    #: Human name, as the upstream writes it.
    name: str
    #: ``model`` | ``corpus`` | ``tool``.
    kind: str
    #: Project or corpus home.
    origin: str
    #: Release tag, version, or commit. Where there is genuinely no pin, this
    #: must start with ``unpinned:`` and ``notes`` must say what that exposes —
    #: a vague version is worse than a stated absence, because it reads as one.
    version: str
    #: SPDX-style identifier where one applies, else the licence as published.
    license: str
    #: Where the licence text can be read.
    license_url: str
    #: Copyright holder, and the citation where the corpus asks for one.
    attribution: str
    #: ``Asset.name`` values this source provides, if any.
    provides: tuple[str, ...]
    #: ``in-use`` | ``planned`` | ``declined``.
    status: str
    #: Why it is unpinned, planned or declined. Required for all three.
    notes: str = ""


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
        source="openwakeword",
    ),
    Asset(
        name="melspectrogram.tflite",
        url="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.tflite",
        sha256="96fa0adccb6e8cf95cb14465409a1a2898ee4a96a85bb9ed3c7eb0e68bf163e8",
        size=1092516,
        license="Apache-2.0",
        attribution=_OWW,
        source="openwakeword",
    ),
    Asset(
        name="embedding_model.onnx",
        url="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx",
        sha256="70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
        size=1326578,
        license="Apache-2.0",
        attribution=_OWW,
        source="openwakeword",
    ),
    #: openWakeWord fetches this while loading a model, whether or not VAD is
    #: enabled — `tools/wake_word.py` never references VAD and it is still
    #: downloaded. Left unpinned it is a network call in the middle of
    #: evaluation, and under a ProcessPoolExecutor several workers race to write
    #: the same path: one loses, raises FeatureUnavailable, and the run dies as
    #: an unexplained BrokenProcessPool. Pinning it removes the download, so the
    #: race cannot happen and the measurement does not depend on the network.
    Asset(
        name="silero_vad.onnx",
        url="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/silero_vad.onnx",
        sha256="a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28",
        size=1807522,
        license="MIT (Silero VAD, snakers4/silero-vad), redistributed by openWakeWord",
        attribution="Silero Team, silero-vad (github.com/snakers4/silero-vad); " + _OWW,
        source="silero-vad",
    ),
    Asset(
        name="embedding_model.tflite",
        url="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.tflite",
        sha256="c0aea21eb84a4ce90a08c870da41b7a7173b45269e6a3207c71d67c40f3a59d8",
        size=1330312,
        license="Apache-2.0",
        attribution=_OWW,
        source="openwakeword",
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
    source="piper-sample-generator",
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
    source="speech-commands",
)

ALL_ASSETS: tuple[Asset, ...] = (*FEATURE_EXTRACTORS, TTS_GENERATOR, NEGATIVE_SPEECH)


#: Every external project or corpus this pipeline touches, whether or not it
#: contributes redistributed bytes — and the ones that were considered and
#: refused.
#:
#: The assets above answer "what did we download, and is the hash right". This
#: answers "whose work is it, which version, under what terms, and can we say
#: so". Those are different questions, and the second one has no home in a
#: table of files: two of the entries below ship nothing at all, and one is not
#: used *at* all.
#:
#: ``tests/tools/test_wakeword_asset_licences.py`` is what keeps this honest.
#: It fails if a pinned asset names a source that is not here, if a source in
#: use lacks a licence, version or attribution, and if any pipeline script
#: mentions a known corpus that this table does not account for.
SOURCES: tuple[Source, ...] = (
    Source(
        key="openwakeword",
        name="openWakeWord",
        kind="model",
        origin="https://github.com/dscripka/openWakeWord",
        version="v0.5.1 (release assets); the runtime package is pinned to 0.6.0 in pyproject",
        license="Apache-2.0",
        license_url="https://github.com/dscripka/openWakeWord/blob/main/LICENSE",
        attribution=_OWW,
        provides=(
            "melspectrogram.onnx",
            "melspectrogram.tflite",
            "embedding_model.onnx",
            "embedding_model.tflite",
        ),
        status="in-use",
        notes=(
            "The shared front end, and therefore an input to training and to "
            "inference alike. The trained classifier is a derived work of it."
        ),
    ),
    Source(
        key="silero-vad",
        name="Silero VAD",
        kind="model",
        origin="https://github.com/snakers4/silero-vad",
        version="the copy redistributed in openWakeWord's v0.5.1 release",
        license="MIT",
        license_url="https://github.com/snakers4/silero-vad/blob/master/LICENSE",
        attribution="Silero Team, silero-vad (github.com/snakers4/silero-vad)",
        provides=("silero_vad.onnx",),
        status="in-use",
        notes=(
            "Fetched because openWakeWord loads it unconditionally, not because "
            "anything here wants VAD. Listed separately from openWakeWord "
            "because the licence and the copyright holder are different, which "
            "a table keyed by release asset would have hidden."
        ),
    ),
    Source(
        key="piper-sample-generator",
        name="piper-sample-generator",
        kind="model",
        origin="https://github.com/rhasspy/piper-sample-generator",
        version=(
            "v2.0.0 for the checkpoint; unpinned: the working clone is "
            "`git clone --depth 1` of the default branch"
        ),
        license="MIT",
        license_url="https://github.com/rhasspy/piper-sample-generator/blob/master/LICENSE.md",
        attribution="Michael Hansen, piper-sample-generator (github.com/rhasspy/piper-sample-generator)",
        provides=("en_US-libritts_r-medium.pt",),
        status="in-use",
        notes=(
            "Two different pins, deliberately. The checkpoint is a release "
            "asset fetched by URL and hash-checked. The clone is not pinned to "
            "a commit — run_pipeline.sh says why: generate_speech.py puts it on "
            "sys.path, so it is executed code and a moving branch is a real "
            "exposure. What limits that exposure is that the only file copied "
            "out of the clone, the VITS config, is itself hash-checked "
            "(GENERATOR_CONFIG_SHA256); the executed code is not. Pinning the "
            "clone to a commit is the outstanding hardening."
        ),
    ),
    Source(
        key="libritts-r",
        name="LibriTTS-R",
        kind="corpus",
        origin="https://www.openslr.org/141/",
        version="the 2023 release, as distilled into the piper checkpoint above",
        license="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution=(
            "Koizumi et al., LibriTTS-R: A Restored Multi-Speaker Text-to-Speech "
            "Corpus (2023), arXiv:2305.18802"
        ),
        provides=(),
        status="in-use",
        notes=(
            "No bytes of the corpus are fetched: it reaches this pipeline only "
            "through the 904 speaker embeddings inside the checkpoint. It is "
            "recorded anyway because every synthesized positive is derived from "
            "those voices, and CC BY 4.0 asks for the attribution."
        ),
    ),
    Source(
        key="speech-commands",
        name="Speech Commands",
        kind="corpus",
        origin="https://www.tensorflow.org/datasets/catalog/speech_commands",
        version="v0.02",
        license="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution=(
            "Warden, P. Speech Commands: A Dataset for Limited-Vocabulary Speech "
            "Recognition (2018), arXiv:1804.03209. Google LLC"
        ),
        provides=("speech_commands_v0.02.tar.gz",),
        status="in-use",
        notes=(
            "Both the recorded-speech negatives and the six background "
            "recordings come from this one tarball. That is why no separate "
            "noise corpus appears in this table."
        ),
    ),
    Source(
        key="pyroomacoustics",
        name="pyroomacoustics",
        kind="tool",
        origin="https://github.com/LCAV/pyroomacoustics",
        version="unpinned: installed by the README's Environment block with no version",
        license="MIT",
        license_url="https://github.com/LCAV/pyroomacoustics/blob/master/LICENSE",
        attribution="Scheibler, Bezzam and Dokmanić, pyroomacoustics (2018), arXiv:1710.04196",
        provides=(),
        status="in-use",
        notes=(
            "Generates the room impulse responses rather than shipping a "
            "recorded set, which is what keeps reverberation licence-free and "
            "seeded. The absent version pin is a reproducibility exposure and "
            "not a licence one: a different release could compute a different "
            "impulse response from the same seed, which DETERMINISM.md's "
            "cross-machine caveat already covers in general terms."
        ),
    ),
    Source(
        key="espeak-ng",
        name="eSpeak NG",
        kind="tool",
        origin="https://github.com/espeak-ng/espeak-ng",
        version="unpinned: whatever the piper-tts 1.3.0 wheel bundles",
        license="GPL-3.0-or-later",
        license_url="https://github.com/espeak-ng/espeak-ng/blob/master/COPYING",
        attribution="eSpeak NG contributors; Jonathan Duddington's eSpeak",
        provides=(),
        status="in-use",
        notes=(
            "Reached through piper-sample-generator's get_phonemes, and the "
            "only GPL component anywhere near this pipeline. It is a build-time "
            "tool: what leaves it is phoneme strings, and running a GPL program "
            "over an input does not place the output under the GPL. No espeak-ng "
            "code or data is redistributed in tools/wakewords/. Recorded "
            "explicitly because 'a GPL dependency' found later, unexplained, is "
            "a licence review nobody scheduled."
        ),
    ),
    Source(
        key="common-voice",
        name="Mozilla Common Voice",
        kind="corpus",
        origin="https://commonvoice.mozilla.org/datasets",
        version="Corpus 17.0, English — the version scripts/wakeword/COMMON_VOICE.md is written against",
        license="CC0-1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        attribution="Ardila et al., Common Voice: A Massively-Multilingual Speech Corpus (2020), arXiv:1912.06670",
        provides=(),
        status="planned",
        notes=(
            "Not acquired. Access needs the dataset terms accepted under the "
            "Owner's account and a read-scope token, neither of which an agent "
            "can hold. acquire_common_voice.py refuses to run without one and "
            "downloads nothing until then. See COMMON_VOICE.md for the bound "
            "and the exact Owner action."
        ),
    ),
    Source(
        key="musan",
        name="MUSAN",
        kind="corpus",
        origin="https://www.openslr.org/17/",
        version="n/a — never fetched",
        license="CC BY 4.0 as published on openslr.org/17; NOT independently verified here",
        license_url="https://www.openslr.org/17/",
        attribution="Snyder, Chen and Povey, MUSAN: A Music, Speech, and Noise Corpus (2015), arXiv:1510.08484",
        provides=(),
        status="declined",
        notes=(
            "Recorded because MUSAN is the obvious corpus for this job and its "
            "absence reads like an oversight. It is not used: no script "
            "references it, no byte of it has been fetched, and the noise the "
            "pipeline mixes in comes from Speech Commands' six background "
            "recordings plus synthetic impulse responses. The licence above is "
            "quoted from the publisher and is deliberately not asserted as "
            "verified — nothing here has ever downloaded the corpus to check. "
            "If MUSAN is ever adopted, this entry moves to in-use and the "
            "licence has to be confirmed against the distribution itself."
        ),
    ),
)


def source_for(asset: Asset) -> Source:
    """The Source an asset's bytes come from.

    Raises rather than returning None: an asset whose provenance cannot be
    resolved is the failure this table exists to make impossible, and a caller
    that got ``None`` back would most likely print it.
    """
    for source in SOURCES:
        if source.key == asset.source:
            return source
    raise KeyError(
        f"{asset.name} names source {asset.source!r}, which is not in SOURCES. "
        "Add the project or corpus there — with its licence, version and "
        "attribution — before pinning bytes from it."
    )


#: The VITS checkpoint's config. piper-sample-generator keeps this inside its
#: git repository (``models/``) rather than publishing it as a release asset —
#: the release URL that would mirror the ``.pt`` returns 404 — so it cannot be
#: fetched like everything else here and is instead copied from the pinned
#: clone and checked against this hash.
#:
#: It is not optional. ``generate_speech._load_generator`` opens
#: ``f"{model_path.name}.json"`` beside the checkpoint, so without it stage 1
#: dies on FileNotFoundError before synthesizing a single clip.
GENERATOR_CONFIG_NAME = "en_US-libritts_r-medium.pt.json"
GENERATOR_CONFIG_SHA256 = (
    "119118e510d0b8a7a0c8649a0668640d6db9b00239e874169d964853a8d15848"
)
GENERATOR_CONFIG_SOURCE = "piper-sample-generator, models/ (MIT)"


def install_pinned(assets: tuple[Asset, ...], source: Path, dest: Path) -> list[Path]:
    """Copy pinned assets from ``source`` into ``dest``, hashing both ends.

    Both ends on purpose. Hashing the source proves the bytes are the ones this
    pipeline was built against; hashing the destination after the copy proves
    the copy itself did not truncate — which a copy onto a nearly-full disk, or
    across the Windows/WSL filesystem boundary, genuinely can do.

    Idempotent: a destination that already matches is left alone, so this is
    safe to run at the top of every pipeline invocation.
    """
    dest.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []
    for asset in assets:
        src = source / asset.name
        if not src.exists():
            raise FileNotFoundError(
                f"{asset.name} is not in {source}. Run the fetch step first: "
                f"it downloads and hash-verifies every pinned asset."
            )
        actual = sha256_file(src)
        if actual != asset.sha256:
            raise RuntimeError(
                f"{asset.name}: SHA-256 mismatch at the source\n"
                f"  expected {asset.sha256}\n"
                f"  got      {actual}\n"
                f"  in       {source}"
            )
        target = dest / asset.name
        if not (target.exists() and sha256_file(target) == asset.sha256):
            shutil.copyfile(src, target)
            written = sha256_file(target)
            if written != asset.sha256:
                target.unlink(missing_ok=True)
                raise RuntimeError(
                    f"{asset.name}: SHA-256 mismatch after copying into {dest}\n"
                    f"  expected {asset.sha256}\n"
                    f"  got      {written}"
                )
        installed.append(target)
    return installed


def openwakeword_models_dir() -> Path:
    """Where openWakeWord loads its shared front end from.

    ``openwakeword.utils.AudioFeatures`` defaults to this directory, and
    ``openwakeword.utils.download_models()`` populates it over the network with
    whatever is current — unpinned. Installing the pinned bytes here replaces
    that download rather than racing it, which is what keeps the features a
    model was fit on identical to the features it is scored on.
    """
    import openwakeword  # noqa: PLC0415 — optional at import time

    return Path(openwakeword.__file__).parent / "resources" / "models"


def install_feature_extractors(source: Path, dest: Path | None = None) -> list[Path]:
    """Put the pinned melspectrogram/embedding models where openWakeWord looks.

    openWakeWord ships no models in its wheel, so a fresh environment has an
    empty ``resources/models`` and the first ``AudioFeatures(...)`` fails with
    ``NO_SUCHFILE``. Pinning the assets in this module was never enough on its
    own — nothing installed them — so a clean run either crashed or, worse,
    silently fell back to ``download_models()`` and trained against whatever
    upstream published that day.
    """
    return install_pinned(FEATURE_EXTRACTORS, source, dest or openwakeword_models_dir())


def install_generator_config(generator_root: Path, into: Path) -> Path:
    """Copy the VITS config out of the pinned clone, next to the checkpoint."""
    src = generator_root / "models" / GENERATOR_CONFIG_NAME
    if not src.exists():
        raise FileNotFoundError(
            f"{GENERATOR_CONFIG_NAME} not found at {src}. It ships inside the "
            "piper-sample-generator repository; clone it first."
        )
    actual = sha256_file(src)
    if actual != GENERATOR_CONFIG_SHA256:
        raise RuntimeError(
            f"{GENERATOR_CONFIG_NAME}: SHA-256 mismatch\n"
            f"  expected {GENERATOR_CONFIG_SHA256}\n"
            f"  got      {actual}\n"
            f"  from     {src}"
        )
    into.mkdir(parents=True, exist_ok=True)
    target = into / GENERATOR_CONFIG_NAME
    if not (target.exists() and sha256_file(target) == GENERATOR_CONFIG_SHA256):
        shutil.copyfile(src, target)
        written = sha256_file(target)
        if written != GENERATOR_CONFIG_SHA256:
            target.unlink(missing_ok=True)
            raise RuntimeError(
                f"{GENERATOR_CONFIG_NAME}: SHA-256 mismatch after copy into {into}"
            )
    return target


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
