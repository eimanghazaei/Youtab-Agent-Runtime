"""The level facts of a compressed take, on the format submissions arrive in.

Why this file exists separately
-------------------------------
``import_speaker.py`` reads format facts — container, codec, sample rate,
channels, declared duration — out of the container itself, and that is the one
half of a take's description that needs no decoder. The other half does. Peak,
RMS, DC offset, clipping runs and the noise floor are properties of samples, and
an AAC payload has no samples in it until something decodes it. So on the format
human submissions actually arrive in, ``import_speaker`` is only as good as the
decoder on the machine running it.

Human submissions arrive as ``.m4a``, AAC-LC, 48 kHz, written by phone
voice-memo apps. E001 delivered 6 files at 64 kbps mono; E002 delivered 17 at
128 kbps stereo, twelve of them separately recorded near phrases in their own
subdirectory. Both shapes are built here, in that codec, at those bit rates, and
ingested — because "the tool parses an ISO-BMFF header" and "the tool can
measure clipping on the recording somebody actually handed over" are different
claims, and only the second one is worth anything on the day a drive arrives.

PyAV is that decoder, and it is a pinned dependency of this repository's
``wakeword-ingest`` extra rather than something an operator is expected to have
installed. The first test asserts exactly that, in all three places it has to
agree — the extra, the lock file, and the interpreter running the tests — so a
missing pin is a red test rather than a submission that stalls on the day it
cannot be re-recorded. These tests therefore **fail** where PyAV is absent
instead of skipping: a skip is how an ingestion machine ends up not having it.

The fixture, and why it cannot become evidence
----------------------------------------------
Every audio file here is synthesised in ``tmp_path`` by the test itself — a
shaped tone plus dither, encoded to AAC by the same pinned decoder — and not one
byte of anybody's recording is read. That is enough to exercise decode
mechanics, and it is deliberately barred from being anything else:

* it never exists inside the repository or inside any data root that outlives
  the test, which is asserted rather than assumed (and ``import_speaker``
  refuses a submission inside the repository in the first place);
* every fixture whose name this file is free to choose carries the word
  ``synthetic``, which is a marker in
  ``build_human_dataset.SYNTHETIC_SOURCE_MARKERS``: a manifest that ever cited
  one of these files as a recording is refused by
  ``build_human_dataset.refuse_synthetic_source`` before its bytes are read.
  That refusal is asserted here too, so the bar is a mechanism and not a
  promise;
* the two submission-shaped trees, whose file names the package grammar fixes
  and which therefore cannot carry the marker, carry a written declaration
  instead: their ``RECORDING_METADATA.json`` says in the device fields that the
  tree is a generated fixture and not a recording, so a copy of the folder
  carries its own provenance with it.

Nothing here trains, scores, promotes or writes into ``tools/wakewords/``, and
nothing here reads the two frozen human derivations.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import tomllib
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import build_human_dataset as human  # noqa: E402
import freeze_manifest  # noqa: E402
import import_speaker as imp  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402

#: The extra that carries the decoder, and the command that installs it. Written
#: out because the point of the Owner decision behind this file is that the
#: decoder is installed by a documented command rather than by somebody
#: remembering; a test that only checked "some extra somewhere" would pass while
#: that property was gone.
INGEST_EXTRA = "wakeword-ingest"
INSTALL_COMMANDS = (
    "uv sync --extra wakeword-ingest",
    "pip install -e '.[wakeword-ingest]'",
)

#: The pinned decoder version, as a literal.
#:
#: Deliberately not read out of ``pyproject.toml``: a test that read the pin
#: would agree with whatever it found, including nothing at all, and the failure
#: this guards against is the pin going missing. Written here it also records
#: which PyAV the level facts in an ingestion report were measured under.
#:
#: 18.1.0 rather than the 18.0.0 that produced the two frozen human
#: derivations, because the two were measured to be the same instrument: same
#: bundled FFmpeg (libavcodec 62.28.102, libswresample 6.3.102), and identical
#: peak, RMS, DC offset and noise floor on the same AAC bytes in both the mono
#: 64 kbps and the stereo 128 kbps case. The only difference between them is the
#: version string PyAV writes into ``decoded_by``, and 18.1.0 is the version the
#: governed test environment runs, so the pin is what the measurements are
#: actually taken under.
DECODER_PIN = "18.1.0"

#: What a phone writes: AAC-LC in an MP4/M4A container at the capture rate.
CODEC = "aac"
CONTAINER = "iso-bmff"
SAMPLE_RATE_HZ = 48000

#: E001's delivery, and E002's. Kept apart because a mono file and a stereo one
#: go down different branches of ``_decode_av``'s packed/planar handling, and the
#: branch that folds two channels into one signal reports a peak that is not in
#: the recording.
E001_BIT_RATE = 64_000
E001_CHANNELS = 1
E002_BIT_RATE = 128_000
E002_CHANNELS = 2

#: Labels, from the registries rather than typed. E001 and E002 are the only two
#: labels the prior session layout is accepted for, which is the layout their
#: recordings were made in.
E001_LABEL = imp.PRIOR_LAYOUT_LABELS[0]
E002_LABEL = imp.PRIOR_LAYOUT_LABELS[1]

TAKE_SECONDS = 1.2
CONTINUOUS_SECONDS = 2.0

#: How many of the near-phrase battery E002 recorded as separate files.
E002_NEAR_PHRASES = 12

#: The prior layout's session sections, as its two speakers recorded them.
PRIOR_FLAT_SECTIONS = (
    "01_close_normal",
    "02_speed_volume",
    "03_far_field",
    "04_real_noise",
    "05_near_phrases",
    "06_free_speech",
)

#: AAC is lossy, so nothing here asserts a sample-exact level. These are the
#: bands a 64/128 kbps encode of a known tone lands in, wide enough that the
#: encoder's own choices are not under test and narrow enough that a folded
#: channel, a resample or a defaulted zero fails them.
LEVEL_TOLERANCE_DB = 2.0
DURATION_TOLERANCE_S = 0.05


# ── the decoder, required rather than skipped ────────────────────────────────


def decoder():
    """The pinned decoder, or a failure that names the command that installs it.

    ``pytest.importorskip`` is what this used to be and is precisely the wrong
    reflex here: a skip on the ingestion machine is indistinguishable from a
    pass, and the machine it would be silent on is the one with a speaker's
    only recording on a drive plugged into it.
    """
    try:
        import av
    except ImportError as exc:  # pragma: no cover - the state this test forbids
        pytest.fail(
            "PyAV is not importable, so nothing here can measure peak, RMS, "
            "clipping or the noise floor of a compressed take -- which is every "
            "take a phone produces. It is a pinned dependency of this "
            f"repository's [{INGEST_EXTRA}] extra, not a host detail: install it "
            f"with `{INSTALL_COMMANDS[0]}` (or `{INSTALL_COMMANDS[1]}`). "
            f"ImportError: {exc}"
        )
    return av


# ── generated audio, in the format submissions arrive in ─────────────────────


def _tone(seed: str, seconds: float, peak: float, rate: int = SAMPLE_RATE_HZ) -> np.ndarray:
    """A shaped tone plus dither, unique per file.

    Unique matters: two byte-identical takes are a duplicate, which
    ``import_speaker`` refuses, and a fixture that produced them everywhere
    would fail for the right reason at the wrong time. The quiet head and tail
    are what give the noise-floor estimate something to measure.
    """
    generator = np.random.default_rng(
        int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")
    )
    count = int(rate * seconds)
    time_axis = np.arange(count) / rate
    tone = np.sin(2 * np.pi * generator.uniform(180.0, 320.0) * time_axis)
    envelope = np.ones(count)
    edge = max(int(rate * 0.08), 1)
    envelope[:edge] = 0.0
    envelope[-edge:] = 0.0
    dither = generator.normal(0.0, 1e-4, count)
    return np.clip(peak * tone * envelope + dither, -1.0, 1.0)


def write_m4a(
    path: Path,
    *,
    channels: int,
    bit_rate: int,
    seconds: float = TAKE_SECONDS,
    peaks: tuple[float, ...] | None = None,
    rate: int = SAMPLE_RATE_HZ,
) -> Path:
    """One AAC-LC take in an M4A container, as a voice-memo app would deliver it.

    ``peaks`` is per channel, so a stereo fixture can carry two different
    signals: a decoder that folded them would report the average of the two and
    twice the frames, and both are checked below.
    """
    av = decoder()
    if peaks is None:
        peaks = (0.35,) * channels
    assert len(peaks) == channels, "one peak per channel"
    path.parent.mkdir(parents=True, exist_ok=True)

    planes = [
        _tone(f"{path.as_posix()}::{index}", seconds, peak, rate)
        for index, peak in enumerate(peaks)
    ]
    frames = np.stack([np.round(plane * 32767).astype("<i2") for plane in planes])
    # ``s16`` is a packed format: PyAV wants one row of interleaved samples.
    interleaved = frames.T.reshape(1, -1)
    layout = "mono" if channels == 1 else "stereo"

    with av.open(str(path), "w") as writer:
        stream = writer.add_stream(CODEC, rate=rate, layout=layout)
        stream.bit_rate = bit_rate
        frame = av.AudioFrame.from_ndarray(interleaved, format="s16", layout=layout)
        frame.sample_rate = rate
        frame.pts = None
        for packet in stream.encode(frame):
            writer.mux(packet)
        for packet in stream.encode(None):
            writer.mux(packet)
    return path


def zero_the_audio_payload(path: Path) -> Path:
    """Blank the ``mdat`` body, leaving every other box exactly as written.

    A file whose header still describes a second of AAC and whose frames are
    gone is what a damaged transfer produces, and it is the one case the
    predeclared ``undecodable_payload`` rule is for. Truncating the file instead
    would take the ``moov`` box with it — PyAV writes it last — and that is a
    different failure: a container that no longer parses at all.
    """
    body = bytearray(path.read_bytes())
    position = 0
    while position + 8 <= len(body):
        size = int.from_bytes(body[position : position + 4], "big")
        kind = bytes(body[position + 4 : position + 8])
        header = 8
        if size == 1:  # 64-bit size follows the type
            size = int.from_bytes(body[position + 8 : position + 16], "big")
            header = 16
        elif size == 0:
            size = len(body) - position
        if size < header:
            break
        if kind == b"mdat":
            for index in range(position + header, position + size):
                body[index] = 0
        position += size
    path.write_bytes(bytes(body))
    return path


def row_for_file(
    relative: str, *, section: str, category: str, label: int
) -> imp.Original:
    """A classified row for one file, so ``describe`` can be called on it alone.

    The classifier and the section rules have their own tests; what is under
    test here is what ``describe`` makes of a compressed payload.
    """
    return imp.Original(
        path=relative,
        section=section,
        category=category,
        label=label,
        phrase="",
        contested=False,
        contested_reason="",
        bytes=0,
        sha256="",
        format=None,
        levels=None,
        findings=(),
        problems=(),
        excluded=False,
        exclusion_rule="",
        exclusion_reason="",
    )


def describe_one(
    path: Path,
    *,
    section: str = "01_close_normal",
    category: str = "positive_close",
) -> imp.Original:
    """``describe`` one file, from its own directory, as an import would."""
    row = row_for_file(
        path.name,
        section=section,
        category=category,
        label=imp.LABEL_VALUES[spec.POSITIVE],
    )
    return imp.describe(path.parent, row)


# ── submissions, in the two shapes the two speakers delivered ────────────────


def fixture_metadata(label: str) -> dict:
    """The device form, saying in it that this tree is generated.

    ``import_speaker`` copies no free-text value from this form into a report,
    only its digest, so these strings are not a claim about the report. They are
    a claim that travels with the *folder*: a copy of this fixture carries a
    written statement that it is not a recording of a person, next to the audio.
    Dates far from today on purpose, as the sibling suite does.
    """
    return {
        "speaker_id": label,
        "recording_date": "2024-02-29",
        "device_make_model": "GENERATED TEST FIXTURE - NOT A RECORDING",
        "recording_app": "synthetic tone written by tests/tools/test_wakeword_compressed_ingestion.py",
        "room_name": "no room: this audio was never recorded anywhere",
        "room_size_approx": "not applicable to a generated fixture",
        "floor_surface": "not applicable to a generated fixture",
        "wall_surface": "not applicable to a generated fixture",
        "background_sources_present": "none: dither, not a room",
        "noise_sources_used": ["tv", "kitchen"],
        "farfield_distance": "not applicable to a generated fixture",
        "consent_signed_date": "2024-02-20",
    }


def _submission_records(root: Path, label: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / spec.CONSENT_FILE).write_bytes(b"%PDF-1.4 placeholder consent scan")
    (root / spec.METADATA_FILE).write_text(
        json.dumps(fixture_metadata(label), indent=1), encoding="utf-8"
    )


def build_e001_shape(root: Path) -> Path:
    """E001's delivery: one continuous mono take per numbered section, 64 kbps."""
    _submission_records(root, E001_LABEL)
    for section in PRIOR_FLAT_SECTIONS:
        write_m4a(
            root / f"{section}.m4a",
            channels=E001_CHANNELS,
            bit_rate=E001_BIT_RATE,
            seconds=CONTINUOUS_SECONDS,
        )
    return root


def build_e002_shape(root: Path) -> Path:
    """E002's delivery: 128 kbps stereo, near phrases as one file per phrase."""
    _submission_records(root, E002_LABEL)
    originals = root / spec.ORIGINALS_DIR
    for section in PRIOR_FLAT_SECTIONS:
        if section == "05_near_phrases":
            continue
        write_m4a(
            originals / f"{section}.m4a",
            channels=E002_CHANNELS,
            bit_rate=E002_BIT_RATE,
            seconds=CONTINUOUS_SECONDS,
        )
    for item in spec.NEAR_PHRASE_ITEMS[:E002_NEAR_PHRASES]:
        name = item.text.rstrip(".").capitalize()
        write_m4a(
            originals / "05_near_phrases" / f"{name}.m4a",
            channels=E002_CHANNELS,
            bit_rate=E002_BIT_RATE,
        )
    return root


@pytest.fixture()
def generated(tmp_path: Path) -> Path:
    """Where every fixture in this file is written.

    The directory name carries ``synthetic`` so the marker is in the path of
    anything built here, not only in the file names chosen below.
    """
    root = tmp_path / "synthetic-fixtures"
    root.mkdir()
    return root


@pytest.fixture()
def into(tmp_path: Path) -> Path:
    root = tmp_path / "human"
    root.mkdir()
    return root


def failed(state: imp.Plan) -> set[str]:
    return {check.name for check in state.failures}


def problems_of(state: imp.Plan, name: str) -> tuple[str, ...]:
    for check in state.checks:
        if check.name == name:
            return check.problems
    raise AssertionError(f"no check named {name}")


def assert_measured(levels: imp.LevelFacts, *, below_full_scale: bool = True) -> None:
    """Every level fact is a measurement: finite, and not the "no signal" value.

    The failure this is aimed at is not a wrong number, it is a *defaulted* one.
    ``level_facts`` returns ``SILENCE_DBFS`` for a payload with nothing in it and
    zeros for the clipping counters, so a decode that quietly produced no
    samples would populate an ingestion report with a full set of plausible
    fields describing a recording nobody made.

    ``below_full_scale`` is a parameter rather than an assertion because an AAC
    decode of a clipped take legitimately overshoots 0 dBFS: the encoder is
    lossy and the reconstruction rings past the flat top it was given. That is
    itself a fact about the format, and asserting the opposite would only make
    the clipped case impossible to test.
    """
    assert levels.samples > 0
    for name in ("peak_dbfs", "rms_dbfs", "noise_floor_dbfs", "dc_offset", "duration_s"):
        value = getattr(levels, name)
        assert math.isfinite(value), f"{name} is not finite: {value}"
    assert levels.peak_dbfs > imp.SILENCE_DBFS, "peak is the no-signal sentinel"
    assert levels.rms_dbfs > imp.SILENCE_DBFS, "RMS is the no-signal sentinel"
    assert levels.noise_floor_dbfs > imp.SILENCE_DBFS, "noise floor is the sentinel"
    assert levels.rms_dbfs < levels.peak_dbfs
    assert levels.duration_s > 0.0
    if below_full_scale:
        assert levels.peak_dbfs < 0.0


# ── the pin itself ───────────────────────────────────────────────────────────


def test_the_decoder_is_a_pinned_dependency_and_not_a_host_detail() -> None:
    """One version, declared in the project, locked, and installed here.

    Before this was true, ``import_speaker`` needed PyAV for the level facts of
    every take a phone produces and PyAV was in no dependency group at all — so
    the documented way to get an ingestion machine was for somebody to remember.
    Three places have to agree, and this checks all three: the extra declares
    the pin, ``uv.lock`` resolves that same version for hash-verified installs,
    and the interpreter running these measurements is using it.
    """
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    assert INGEST_EXTRA in extras, (
        f"no [{INGEST_EXTRA}] extra in pyproject.toml. The decoder ingestion "
        "needs must be installable by a written command, not by recollection: "
        f"{INSTALL_COMMANDS[0]}"
    )
    specs = extras[INGEST_EXTRA]
    assert f"av=={DECODER_PIN}" in specs, (
        f"[{INGEST_EXTRA}] must pin av=={DECODER_PIN} exactly (found {specs}). An "
        "unpinned decoder is a level report whose numbers depend on which "
        "FFmpeg the operator's machine happened to fetch."
    )
    # numpy computes the level facts, so this extra alone has to be a working
    # ingestion environment; and it must not fork the version the rest of the
    # project pins (tests/test_packaging_metadata.py fails on a fork).
    assert any(item.startswith("numpy==") for item in specs), specs
    numpy_pins = {
        item
        for name in ("wake", "voice", INGEST_EXTRA)
        for item in extras[name]
        if item.startswith("numpy==")
    }
    assert len(numpy_pins) == 1, f"the numpy pin has forked: {numpy_pins}"

    # Not in [all]: ingestion runs on the Owner's machine, once per speaker, and
    # PyAV carries a whole FFmpeg. Shipping that to every desktop install for a
    # script none of them runs is blast radius bought for nothing.
    assert not [item for item in extras["all"] if INGEST_EXTRA in item], (
        f"[{INGEST_EXTRA}] is in [all]; it is an operator-side tool, not part of "
        "the runtime every session installs"
    )

    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["version"] for package in lock["package"] if package["name"] == "av"}
    assert locked == {DECODER_PIN}, (
        f"uv.lock resolves av to {sorted(locked)} and the pin is {DECODER_PIN}. "
        "Bump the pin and regenerate the lock in the same commit, or a "
        "hash-verified install measures takes with a different decoder than the "
        "one this suite proves."
    )

    av = decoder()
    assert av.__version__ == DECODER_PIN, (
        f"these level facts were measured with PyAV {av.__version__} and the "
        f"project pins {DECODER_PIN}. Install the pinned environment "
        f"(`{INSTALL_COMMANDS[0]}`) rather than relaxing this assertion: the "
        "version is recorded in every ingestion report's `decoded_by`, and a "
        "report that names a decoder nobody can reproduce is not evidence."
    )


# ── the format a phone actually writes ───────────────────────────────────────


def test_a_mono_phone_take_is_measured_and_not_described_from_its_header(
    generated: Path,
) -> None:
    """E001's delivery: AAC-LC, 48 kHz, mono, 64 kbps.

    The container is parsed with no decoder and the levels come from decoding,
    and the two have to describe the same recording. Both halves are asserted
    because the failure mode is a report that looks complete: a rate read off a
    decoder default would "prove" a transcode that never happened, and a level
    block full of sentinels would describe silence that was never recorded.
    """
    take = write_m4a(
        generated / "synthetic_tone_mono_64kbps.m4a",
        channels=E001_CHANNELS,
        bit_rate=E001_BIT_RATE,
    )

    facts = imp.format_facts(take)
    assert facts.container == CONTAINER
    assert facts.codec == "mp4a"  # the ISO-BMFF sample entry's own name for AAC
    assert facts.sample_rate_hz == SAMPLE_RATE_HZ
    assert facts.channels == E001_CHANNELS
    assert facts.read_by.startswith("container-parser:iso-bmff")

    samples, rate, decoded_by = imp.decode(take, facts)
    levels = imp.level_facts(samples, rate, decoded_by)
    assert decoded_by == f"pyav {DECODER_PIN} ({CODEC})", decoded_by
    assert_measured(levels)
    assert levels.channels == E001_CHANNELS
    assert levels.sample_rate_hz == SAMPLE_RATE_HZ, "nothing here resamples"
    assert facts.duration_s is not None
    assert levels.duration_s == pytest.approx(facts.duration_s, abs=0.05)
    # The tone was written at 0.35 full scale; AAC moves a peak, but not far.
    assert levels.peak_dbfs == pytest.approx(20 * math.log10(0.35), abs=LEVEL_TOLERANCE_DB)
    assert abs(levels.dc_offset) < imp.DC_OFFSET_LIMIT
    # The quiet head and tail are what the noise-floor estimate measures, and it
    # found them: a floor sitting at the RMS would mean the percentile never saw
    # anything but the tone, which is what a truncated decode looks like.
    assert levels.noise_floor_dbfs < levels.rms_dbfs - 20

    # ...and the same numbers through the path an import actually takes.
    row = describe_one(take)
    assert row.levels is not None
    assert row.levels.as_json() == levels.as_json()
    assert row.problems == ()
    assert not row.excluded


def test_a_stereo_phone_take_is_not_folded_into_one_signal(generated: Path) -> None:
    """E002's delivery: 128 kbps stereo, and two channels that stay two.

    ``to_ndarray`` hands back a packed frame as one interleaved row, and reading
    that as if it were planar folds the channels together: the frame count
    doubles, the duration doubles with it, and the peak becomes something that
    is in no channel of the recording. The fixture's channels carry deliberately
    different levels so a fold cannot pass by averaging to the right answer.
    """
    loud, quiet = 0.40, 0.05
    take = write_m4a(
        generated / "synthetic_tone_stereo_128kbps.m4a",
        channels=E002_CHANNELS,
        bit_rate=E002_BIT_RATE,
        peaks=(loud, quiet),
    )

    facts = imp.format_facts(take)
    assert facts.channels == E002_CHANNELS
    assert facts.sample_rate_hz == SAMPLE_RATE_HZ

    samples, rate, decoded_by = imp.decode(take, facts)
    levels = imp.level_facts(samples, rate, decoded_by)
    assert_measured(levels)
    assert samples.ndim == 2 and samples.shape[1] == E002_CHANNELS
    assert levels.channels == E002_CHANNELS
    assert facts.duration_s is not None
    assert levels.duration_s == pytest.approx(facts.duration_s, abs=DURATION_TOLERANCE_S), (
        "the decoded duration doubled, which is what folding two channels into "
        "one signal looks like"
    )
    left = float(np.abs(samples[:, 0]).max())
    right = float(np.abs(samples[:, 1]).max())
    assert left > right * 4, (left, right)
    assert levels.peak_dbfs == pytest.approx(20 * math.log10(loud), abs=LEVEL_TOLERANCE_DB)
    # Not the average of the two channels, which a fold would report.
    assert levels.peak_dbfs > 20 * math.log10((loud + quiet) / 2) + 1.0

    row = describe_one(take)
    assert row.levels is not None and row.levels.channels == E002_CHANNELS
    assert "multichannel" in row.findings
    assert not row.excluded


def test_clipping_on_a_compressed_take_is_counted_and_never_excludes_it(
    generated: Path,
) -> None:
    """The measurement the Owner decision named: clipping, on the real format.

    A clipped take is a real recording of a real delivery and E001's policy
    keeps it, so what matters is that the number is *measured* rather than
    reported as zero because nothing could decode the payload. Zero clipping
    runs is indistinguishable from a defaulted zero, which is why this drives
    the fixture past full scale and asserts a non-zero count.
    """
    take = write_m4a(
        generated / "synthetic_tone_clipped_mono_64kbps.m4a",
        channels=E001_CHANNELS,
        bit_rate=E001_BIT_RATE,
        peaks=(4.0,),  # clipped to +-1.0 before the encoder ever sees it
    )
    row = describe_one(take, section="02_speed_volume", category="positive_speed_volume")

    assert row.levels is not None
    assert_measured(row.levels, below_full_scale=False)
    assert row.levels.peak_dbfs >= -imp.FULL_SCALE_TOLERANCE, "the flat top is gone"
    assert row.levels.full_scale_samples > 0
    assert row.levels.clipping_runs > 0
    assert row.levels.longest_clipping_run >= imp.CLIPPING_RUN_SAMPLES
    assert "clipping" in row.findings
    assert not row.excluded, "a loud take is a recording, not a defect"
    assert row.exclusion_rule == ""


# ── the two deliveries, end to end, as compressed audio ──────────────────────


def test_the_mono_64kbps_delivery_imports_and_reports_measured_levels(
    generated: Path, into: Path
) -> None:
    """E001's shape and format, through ``plan`` and ``execute``.

    The whole point of the decision behind this file: a submission that arrives
    as AAC-LC m4a is ingestible on a machine installed from the project's own
    metadata, and the report it produces carries measured level facts for every
    take rather than a missing-decoder problem.
    """
    root = build_e001_shape(generated / E001_LABEL)
    state = imp.plan(E001_LABEL, root, into)
    assert state.layout.name == imp.LAYOUT_PRIOR
    assert state.ok, {check.name: check.problems for check in state.failures}
    assert len(state.originals) == len(PRIOR_FLAT_SECTIONS)

    for row in state.originals:
        assert row.format is not None and row.format.container == CONTAINER
        assert row.format.sample_rate_hz == SAMPLE_RATE_HZ
        assert row.levels is not None, f"{row.path} has no level facts"
        assert_measured(row.levels)
        assert row.levels.decoded_by == f"pyav {DECODER_PIN} ({CODEC})"
        assert row.levels.channels == E001_CHANNELS
        assert not row.excluded
        assert row.problems == ()

    body = imp.execute(state, into)
    assert len(body["originals"]) == len(PRIOR_FLAT_SECTIONS)
    for reported in body["originals"]:
        for key in (
            "samples",
            "channels",
            "sample_rate_hz",
            "duration_s",
            "peak_dbfs",
            "rms_dbfs",
            "dc_offset",
            "full_scale_samples",
            "clipping_runs",
            "longest_clipping_run_samples",
            "noise_floor_dbfs",
            "decoded_by",
        ):
            assert key in reported["levels"], (reported["path"], key)
        assert reported["levels"]["noise_floor_dbfs"] > imp.SILENCE_DBFS
        assert reported["format"]["codec"] == "mp4a"
    assert body["exclusions"]["excluded"] == []

    manifest = freeze_manifest.load(
        into / (imp.SPEAKER_DIR_PREFIX + E001_LABEL) / imp.MANIFEST_FILENAME
    )
    assert {entry["path"] for entry in manifest["files"]} == {
        f"{section}.m4a" for section in PRIOR_FLAT_SECTIONS
    }


def test_the_stereo_128kbps_delivery_imports_with_its_near_phrases_apart(
    generated: Path, into: Path
) -> None:
    """E002's shape and format: 17 stereo takes, twelve of them near phrases.

    Same end-to-end claim as the mono case plus the one property that shape
    exists for -- twelve separately recorded phrases stay twelve originals -- now
    proved on compressed payloads, where every level fact in the report had to
    come through the decoder.
    """
    root = build_e002_shape(generated / E002_LABEL)
    state = imp.plan(E002_LABEL, root, into)
    assert state.assignment.role == spec.ROLE_SEALED
    assert state.ok, {check.name: check.problems for check in state.failures}
    assert len(state.originals) == len(PRIOR_FLAT_SECTIONS) - 1 + E002_NEAR_PHRASES

    near = [row for row in state.originals if row.section == "05_near_phrases"]
    assert len(near) == E002_NEAR_PHRASES
    assert len({row.sha256 for row in near}) == E002_NEAR_PHRASES
    for row in state.originals:
        assert row.levels is not None, f"{row.path} has no level facts"
        assert_measured(row.levels)
        assert row.levels.channels == E002_CHANNELS
        assert row.levels.sample_rate_hz == SAMPLE_RATE_HZ
        assert row.levels.decoded_by == f"pyav {DECODER_PIN} ({CODEC})"
        assert not row.excluded

    body = imp.execute(state, into)
    frozen = {
        entry["path"]
        for entry in freeze_manifest.load(
            into / (imp.SPEAKER_DIR_PREFIX + E002_LABEL) / imp.MANIFEST_FILENAME
        )["files"]
        if entry["path"].startswith("05_near_phrases/")
    }
    assert len(frozen) == E002_NEAR_PHRASES
    assert body["role"] == spec.ROLE_SEALED


# ── and what happens when the decoder is not there ───────────────────────────


def test_a_missing_decoder_is_an_environment_problem_and_never_an_exclusion(
    generated: Path, into: Path, monkeypatch
) -> None:
    """The degradation, simulated at the import statement rather than by uninstalling.

    ``None`` in ``sys.modules`` is what a genuinely absent module looks like to
    ``import av``: the statement raises ``ImportError`` and
    ``import_speaker._import_av`` turns it into ``DecoderUnavailable``. That
    distinction is the one this asserts. A decoder that is not installed is a
    finding about the *machine*; excluding a take for it would hold a perfectly
    good recording out of the dataset under a predeclared rule that was never
    true of it, and reporting its levels as zeros would put invented
    measurements in a frozen record.
    """
    take = write_m4a(
        generated / "synthetic_tone_mono_64kbps.m4a",
        channels=E001_CHANNELS,
        bit_rate=E001_BIT_RATE,
    )
    root = build_e001_shape(generated / E001_LABEL)

    monkeypatch.setitem(sys.modules, "av", None)

    with pytest.raises(imp.DecoderUnavailable) as raised:
        imp._import_av()
    assert isinstance(raised.value, imp.DecodeError)
    assert "pyav" in str(raised.value).lower()

    # One take: format facts survive, level facts are absent rather than faked.
    row = describe_one(take)
    assert row.format is not None, "the container parser needs no decoder"
    assert row.format.sample_rate_hz == SAMPLE_RATE_HZ
    assert row.levels is None, "no decoder must mean no level facts, not zeroed ones"
    assert not row.excluded
    assert row.exclusion_rule == ""
    assert row.problems and any("pyav" in problem.lower() for problem in row.problems)
    assert "levels" not in row.as_json()

    # The whole submission: refused loudly, and nothing published. The `levels`
    # check reports the takes it could not measure; the sentence naming pyav and
    # the environment travels on the rows themselves, which `_format_check`
    # collects — so an operator reading the failure is told what to install
    # rather than only that something is missing.
    state = imp.plan(E001_LABEL, root, into)
    assert not state.ok
    assert "levels" in failed(state)
    assert len(problems_of(state, "levels")) == len(PRIOR_FLAT_SECTIONS)
    assert all(
        "no level facts could be measured" in problem
        for problem in problems_of(state, "levels")
    )
    assert any("pyav" in problem.lower() for problem in problems_of(state, "format"))
    assert any("install" in problem.lower() for problem in problems_of(state, "format"))
    assert [row.path for row in state.originals if row.excluded] == []
    assert all(row.levels is None for row in state.originals)
    with pytest.raises(imp.Refused):
        imp.execute(state, into)
    assert list(into.iterdir()) == [], "a refused import must write nothing"


def test_an_undecodable_payload_is_still_an_exclusion(generated: Path) -> None:
    """The control for the test above: the two failures keep opposite verdicts.

    Without this, "a missing decoder does not exclude" could be satisfied by a
    tool that never excludes anything. Truncating the payload of a real m4a
    leaves a container that parses and bytes no decoder can turn into samples,
    which is the predeclared ``undecodable_payload`` rule.
    """
    take = write_m4a(
        generated / "synthetic_tone_mono_64kbps.m4a",
        channels=E001_CHANNELS,
        bit_rate=E001_BIT_RATE,
    )
    zero_the_audio_payload(take)

    row = describe_one(take)
    assert row.format is not None
    assert row.excluded
    assert row.exclusion_rule in {"undecodable_payload", "no_audio_payload", "digital_silence"}
    assert row.exclusion_rule in {rule.code for rule in imp.PREDECLARED_EXCLUSIONS}
    assert row.exclusion_reason


# ── the fixture's own provenance bar ─────────────────────────────────────────


def test_the_generated_fixture_can_never_become_dataset_evidence(
    generated: Path, into: Path
) -> None:
    """Synthetic audio is retired from every dataset and measurement path here.

    A generated tone is fine for proving that a decoder decodes. It is not fine
    anywhere near a dataset, and "the test cleans up after itself" is not the
    kind of bar this repository accepts. So the fixture is barred twice: it is
    outside the repository and outside any surviving data root, and where this
    file is free to choose a name it carries the marker
    ``build_human_dataset`` already refuses a recording reference on.
    """
    standalone = write_m4a(
        generated / "synthetic_tone_mono_64kbps.m4a",
        channels=E001_CHANNELS,
        bit_rate=E001_BIT_RATE,
    )
    submission = build_e001_shape(generated / E001_LABEL)

    for path in (generated, standalone, submission, into):
        assert REPO not in path.parents, f"{path} is inside the repository"

    # The marker is a mechanism, not a naming convention: a manifest citing this
    # file as the recording a clip came from is refused before its bytes are read.
    assert human.synthetic_source_marker(standalone.name) == "synthetic"
    with pytest.raises(human.Refused, match="synthetic"):
        human.refuse_synthetic_source(standalone.name, "a clip's source_file")
    assert human.synthetic_source_marker(generated.name) == "synthetic"

    # The submission tree's file names are the package's, so they cannot carry
    # the marker; the folder carries a written declaration instead.
    metadata = json.loads((submission / spec.METADATA_FILE).read_text(encoding="utf-8"))
    assert "NOT A RECORDING" in metadata["device_make_model"]
    assert "synthetic" in metadata["recording_app"]

    # And the one thing that would make any of this dataset evidence -- a frozen
    # manifest under the real human data root -- is somewhere pytest deletes.
    state = imp.plan(E001_LABEL, submission, into)
    assert state.ok, {check.name: check.problems for check in state.failures}
    imp.execute(state, into)
    manifest_path = into / (imp.SPEAKER_DIR_PREFIX + E001_LABEL) / imp.MANIFEST_FILENAME
    assert manifest_path.is_file()
    assert REPO not in manifest_path.parents
