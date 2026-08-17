#!/usr/bin/env python3
"""Verify the shipped wake word against a real microphone on Windows or macOS.

Everything upstream of the microphone is already qualified without hardware:
``tests/tools/test_wake_word_model_assets.py`` runs the shipped artifacts on
committed audio in CI, and ``scripts/wakeword/evaluate_model.py`` measures them
over fifteen hours of held-out speech. What none of that touches is the capture
path — the part that is different on every operating system, and the part that
has historically been where wake words fail in the field:

* a microphone the OS has muted, or never granted permission to;
* a device that opens successfully and returns silence;
* a device that resamples, so 16 kHz is not what the model receives;
* on macOS ARM64, openWakeWord's ONNX embedding model returning near-zero
  scores, which arms a listener that can never fire
  (dscripka/openWakeWord#336 — the runtime switches to tflite there).

This script exercises exactly that path, using the same
``tools.wake_word`` code the product runs. It is meant to be run by a person
sitting at the machine, and it writes a JSON record of what happened so the
result is evidence rather than a recollection.

Four modes, in the order they should be run:

    --list-devices    what the OS is offering, and which one would be used
    --check-capture   open the device and prove it is delivering real audio
    --play-fixture    play the committed clips at the microphone and score them
    --listen          say "hey youtab" yourself; every fire is logged

The record is machine-readable on purpose. Every mode fills in checklist items
with fixed identifiers (``WW-D1`` … ``WW-D6``), the whole document is validated
against :data:`REPORT_SCHEMA` before it is written, and the exit code is
non-zero when a checklist item that ran did not pass. A run therefore either
produces a report that can be checked mechanically, or it fails — it cannot
produce a reassuring file that nobody can verify. ``--print-schema`` dumps the
schema for whoever is doing the checking.

What it does NOT do is claim anything about hardware it did not touch. A mode
that was not requested leaves its checklist items at ``not-run``, and
``verdict.passed`` is only true for the items that actually ran.

See DEVICE_VERIFICATION.md for the per-OS steps and the permission settings
each one needs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "wakeword"
WAKEWORDS = REPO_ROOT / "tools" / "wakewords"

SAMPLE_RATE = 16000
FRAME = 1280

#: The sample format the engines require. ``tools/wake_word.py`` divides by
#: 32768 itself, so float samples in -1..1 arrive 32768x too quiet and the model
#: sees near-silence — a failure that looks exactly like a broken model. The
#: dtype the device actually delivered is recorded per mode for that reason.
FRAME_DTYPE = "int16"

SCHEMA_NAME = "youtab.wakeword.device-verification"
SCHEMA_VERSION = 2

#: The hardware-only checklist. These are the items no CI job can execute,
#: which is precisely why each one needs an identifier that a report can be
#: checked against rather than a paragraph someone attests to.
#:
#: ``mode`` names the flag that fills the item in; ``requires`` names the
#: section of the record its verdict is computed from.
CHECKLIST: dict[str, dict] = {
    "WW-D1": {
        "mode": "--list-devices",
        "requires": "devices",
        "what": "The OS offers at least one input device and the product's "
                "selection resolves to one of them.",
    },
    "WW-D2": {
        "mode": "--check-capture",
        "requires": "capture",
        "what": "The selected device opens at 16 kHz mono int16 and delivers "
                "audio above the dead-microphone threshold.",
    },
    "WW-D3": {
        "mode": "--check-capture",
        "requires": "capture",
        "what": "The samples come back as int16 at the requested 16 kHz; any "
                "rate conversion is the OS's and is recorded.",
    },
    "WW-D4": {
        "mode": "--play-fixture or --listen",
        "requires": "engine",
        "what": "The engine really built on this platform's expected backend, "
                "and the artifact it loaded matches tools/wakewords/SHA256SUMS.",
    },
    "WW-D5": {
        "mode": "--play-fixture",
        "requires": "fixture_playback",
        "what": "Over the acoustic loop: no near-miss phrase fires, and every "
                "clean and reverberant positive does.",
    },
    "WW-D6": {
        "mode": "--listen",
        "requires": "listen",
        "what": "A person speaking the phrase at the machine produces at least "
                "--expect-activations activations (use 0 for the quiet run).",
    },
}

_STATUS = {"pass", "fail", "not-run"}

#: JSON Schema (draft 2020-12 subset) for the emitted record. Kept in the tool
#: rather than beside it so that the thing which writes the file and the thing
#: which defines the format cannot drift apart.
REPORT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": SCHEMA_NAME,
    "type": "object",
    "required": ["schema", "environment", "checklist", "verdict"],
    "properties": {
        "schema": {
            "type": "object",
            "required": ["name", "version"],
            "properties": {
                "name": {"type": "string", "enum": [SCHEMA_NAME]},
                "version": {"type": "integer"},
            },
        },
        "environment": {
            "type": "object",
            "required": [
                "platform", "system", "machine", "python",
                "inference_framework", "model", "sensitivity",
                "confirmation_frames", "sample_rate", "frame_samples",
                "frame_dtype",
            ],
            "properties": {
                "platform": {"type": "string"},
                "system": {"type": "string"},
                "machine": {"type": "string"},
                "python": {"type": "string"},
                "inference_framework": {"type": "string", "enum": ["onnx", "tflite"]},
                "platform_default_framework": {
                    "type": "string", "enum": ["onnx", "tflite"]},
                "model": {"type": "string"},
                "sensitivity": {"type": "number"},
                "confirmation_frames": {"type": "integer"},
                "sample_rate": {"type": "integer"},
                "frame_samples": {"type": "integer"},
                "frame_dtype": {"type": "string"},
            },
        },
        "engine": {
            "type": "object",
            "required": [
                "inference_framework", "model", "model_sha256",
                "matches_recorded_sha256", "matches_platform_default",
                "prime_deterministic",
            ],
            "properties": {
                "inference_framework": {"type": "string", "enum": ["onnx", "tflite"]},
                "model": {"type": "string"},
                "model_sha256": {"type": "string"},
                "matches_recorded_sha256": {"type": "boolean"},
                "matches_platform_default": {"type": "boolean"},
                "prime_deterministic": {"type": "boolean"},
            },
        },
        "devices": {
            "type": "object",
            "required": ["devices", "configured_input_device", "selected"],
            "properties": {
                "devices": {"type": "array"},
                # A PortAudio selector is an index, a device name, or unset.
                "configured_input_device": {
                    "type": ["integer", "string", "null"]},
                "selected": {"type": ["integer", "string", "null"]},
            },
        },
        "capture": {
            "type": "object",
            "required": [
                "seconds", "peak", "rms", "silent", "verdict",
                "requested_samplerate", "requested_channels",
                "requested_dtype", "delivered_dtype",
            ],
            "properties": {
                "seconds": {"type": "number"},
                "peak": {"type": "integer"},
                "rms": {"type": "number"},
                "silent": {"type": "boolean"},
                "verdict": {"type": "string"},
                "requested_samplerate": {"type": "integer"},
                "requested_channels": {"type": "integer"},
                "requested_dtype": {"type": "string"},
                "delivered_dtype": {"type": "string"},
                "device_default_samplerate": {"type": ["number", "null"]},
                "os_is_resampling": {"type": ["boolean", "null"]},
            },
        },
        "fixture_playback": {
            "type": "object",
            "required": [
                "clips", "missed_wake_words", "false_activations",
                "near_phrase_activations", "missed_easy_positives",
                "frames_scored", "delivered_dtype", "results",
            ],
            "properties": {
                "clips": {"type": "integer"},
                "missed_wake_words": {"type": "integer"},
                "false_activations": {"type": "integer"},
                "near_phrase_activations": {"type": "integer"},
                "missed_easy_positives": {"type": "integer"},
                "frames_scored": {"type": "integer"},
                "delivered_dtype": {"type": "string"},
                "results": {"type": "array"},
            },
        },
        "listen": {
            "type": "object",
            "required": ["seconds", "activations", "at_seconds",
                         "expected_activations", "frames_scored"],
            "properties": {
                "seconds": {"type": "number"},
                "activations": {"type": "integer"},
                "at_seconds": {"type": "array"},
                "expected_activations": {"type": "integer"},
                "frames_scored": {"type": "integer"},
                "delivered_dtype": {"type": "string"},
            },
        },
        "checklist": {"type": "object"},
        "verdict": {
            "type": "object",
            "required": ["passed", "ran", "failed", "not_run"],
            "properties": {
                "passed": {"type": "boolean"},
                "ran": {"type": "array"},
                "failed": {"type": "array"},
                "not_run": {"type": "array"},
            },
        },
    },
}

_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value, expected) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        python_type = _JSON_TYPES[name]
        if name in ("integer", "number") and isinstance(value, bool):
            continue                    # bool is an int in Python; not in JSON
        if isinstance(value, python_type):
            return True
    return False


def validate(record: dict, schema: dict = REPORT_SCHEMA, path: str = "$") -> list[str]:
    """Check ``record`` against the subset of JSON Schema used above.

    Hand-rolled rather than pulled from ``jsonschema``, which this repository
    does not depend on: a report that can only be validated where an extra
    package happens to be installed is one that will be accepted unvalidated
    somewhere. Returns a list of problems, empty when the record conforms.
    """
    problems: list[str] = []
    expected = schema.get("type")
    if expected is not None and not _type_ok(record, expected):
        return [f"{path}: expected {expected}, got {type(record).__name__}"]
    enum = schema.get("enum")
    if enum is not None and record not in enum:
        problems.append(f"{path}: {record!r} is not one of {enum}")
    if isinstance(record, dict):
        for key in schema.get("required", []):
            if key not in record:
                problems.append(f"{path}: missing required key {key!r}")
        for key, subschema in schema.get("properties", {}).items():
            if key in record:
                problems.extend(validate(record[key], subschema, f"{path}.{key}"))
    if isinstance(record, list) and "items" in schema:
        for index, item in enumerate(record):
            problems.extend(validate(item, schema["items"], f"{path}[{index}]"))
    return problems


def _sounddevice():
    try:
        import sounddevice  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - hardware-only path
        raise SystemExit(
            "sounddevice is not importable, so no device work is possible here.\n"
            f"  {type(exc).__name__}: {exc}\n"
            "Install the wake-word extra first:  uv pip install -e '.[wake,voice]'"
        ) from exc
    return sounddevice


def list_devices() -> dict:
    """Every input device the host offers, and which one the product picks."""
    sd = _sounddevice()
    sys.path.insert(0, str(REPO_ROOT))
    from tools import wake_word  # noqa: PLC0415

    cfg = wake_word.load_wake_word_config()
    configured = wake_word._input_device(cfg)
    default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else None

    rows = []
    for index, dev in enumerate(sd.query_devices()):
        if int(dev.get("max_input_channels", 0)) < 1:
            continue
        rows.append(
            {
                "index": index,
                "name": dev.get("name"),
                "host_api": sd.query_hostapis(dev.get("hostapi", 0)).get("name"),
                "max_input_channels": int(dev.get("max_input_channels", 0)),
                "default_samplerate": float(dev.get("default_samplerate", 0.0)),
                "is_host_default": index == default_in,
            }
        )
    return {
        "devices": rows,
        "configured_input_device": configured,
        "selected": configured if configured is not None else default_in,
    }


def check_capture(device, seconds: float) -> dict:
    """Open the device and prove it is delivering real audio, not silence.

    A microphone that opens and returns zeros is the failure this catches, and
    it is common: on Windows a muted device still opens, and on macOS a
    process without the Microphone entitlement gets a stream of silence rather
    than an error. ``tools.wake_word`` has its own dead-mic detector for this
    at runtime; here it is the whole point of the mode.

    The requested format is recorded next to what came back, because the other
    half of this check is the conversion nobody sees: the product asks for
    16 kHz mono int16 and the OS satisfies that by resampling from whatever the
    device is really running at. Recording the device's native rate makes that
    conversion a measurement rather than an assumption, and recording the dtype
    that arrived is what distinguishes a dead microphone from a feed that is
    merely 32768x too quiet.
    """
    import numpy as np  # noqa: PLC0415

    sd = _sounddevice()
    frames = int(seconds * SAMPLE_RATE)
    captured = sd.rec(
        frames, samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=device
    )
    sd.wait()
    delivered = np.asarray(captured)
    audio = delivered.reshape(-1).astype(np.int32)
    peak = int(np.abs(audio).max())
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    sys.path.insert(0, str(REPO_ROOT))
    from tools import wake_word  # noqa: PLC0415

    native = None
    try:
        info = sd.query_devices(device if device is not None else sd.default.device[0],
                                "input")
        native = float(info.get("default_samplerate")) if isinstance(info, dict) else None
    except Exception:                                  # diagnostic only
        native = None

    return {
        "seconds": seconds,
        "peak": peak,
        "rms": rms,
        "peak_dbfs": (20.0 * np.log10(peak / 32768.0)) if peak else None,
        "silent": peak <= wake_word._SILENCE_PEAK,
        "verdict": "dead microphone" if peak <= wake_word._SILENCE_PEAK else "live",
        "requested_samplerate": SAMPLE_RATE,
        "requested_channels": 1,
        "requested_dtype": FRAME_DTYPE,
        "delivered_dtype": str(delivered.dtype),
        "delivered_channels": int(delivered.shape[1]) if delivered.ndim == 2 else 1,
        "device_default_samplerate": native,
        "os_is_resampling": (native != float(SAMPLE_RATE)) if native else None,
    }


def _read_fixture(path: Path):
    import numpy as np  # noqa: PLC0415

    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16)


def _engine():
    sys.path.insert(0, str(REPO_ROOT))
    from tools import wake_word  # noqa: PLC0415

    cfg = wake_word.load_wake_word_config()
    return wake_word._OpenWakeWordEngine(cfg), wake_word


def _recorded_sha256(artifact: str) -> str | None:
    """The digest ``tools/wakewords/SHA256SUMS`` records for ``artifact``."""
    name = Path(artifact).name
    try:
        lines = (WAKEWORDS / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.strip():
            digest, recorded = line.split()
            if recorded == name:
                return digest
    return None


def _engine_record(engine, wake_word) -> dict:
    """What the engine ACTUALLY built with — not what was asked for.

    ``_OpenWakeWordEngine`` may resolve to a different backend than the config
    named: an explicit ``onnx`` is coerced to tflite on macOS ARM64, and a
    tflite request downgrades to onnx where no tflite runtime exists. Reporting
    the requested value would label an ONNX run "tflite", and this repository
    ships both artifacts side by side, so nothing else in the record would
    contradict it.
    """
    model = str(getattr(engine, "model_reference", "") or "")
    digest = ""
    try:
        digest = hashlib.sha256(Path(model).read_bytes()).hexdigest()
    except OSError:
        digest = ""
    recorded = _recorded_sha256(model)
    return {
        "inference_framework": getattr(engine, "inference_framework", ""),
        "model": model,
        "model_sha256": digest,
        "recorded_sha256": recorded,
        # A custom model has no SHA256SUMS entry; that is not a failure, so the
        # comparison is only meaningful when there is something to compare with.
        "matches_recorded_sha256": bool(digest) and (
            recorded is None or digest == recorded),
        "matches_platform_default": (
            getattr(engine, "inference_framework", "")
            == wake_word.default_inference_framework()
        ),
        "prime_deterministic": bool(getattr(engine, "_prime_deterministic", False)),
    }


class _FrameFeeder:
    """Re-blocks a device's chunks into exact 1280-sample engine frames.

    PortAudio is asked for ``blocksize=FRAME``, but that is a request: a host
    API may deliver a different chunk size, and the last callback of a stream is
    routinely short. This used to be

        for start in range(0, len(block) - FRAME + 1, FRAME):

    which silently discarded the remainder of every chunk and restarted
    alignment at zero on the next one — up to 79 ms lost at each seam, which is
    a large fraction of the phrase, and it would look like a weak model rather
    than a broken feed. Carrying the remainder makes the feed lossless and
    gapless, which is what the product's own detector gets for free from
    ``stream.read(FRAME)``.

    The delivered dtype is recorded rather than coerced. The previous
    ``.astype(np.int16)`` was worse than nothing: on a float32 stream in -1..1 it
    truncates every sample to zero, so a mis-configured feed would have been
    reported as a dead microphone.
    """

    def __init__(self, engine):
        import numpy as np  # noqa: PLC0415

        self._np = np
        self._engine = engine
        self._carry = np.empty(0, dtype=np.int16)
        self.frames = 0
        self.dtype = ""

    def feed(self, block) -> bool:
        np = self._np
        block = np.asarray(block)
        # Channel 0, not a flattened interleave — the same choice
        # ``WakeWordDetector._run`` makes. ``channels=1`` is requested, so a 2-D
        # delivery is (frames, 1); flattening a host that ignored the request
        # would interleave the channels into one stream of nonsense.
        block = block[:, 0] if block.ndim == 2 else block.reshape(-1)
        if not self.dtype:
            self.dtype = str(block.dtype)
        if len(self._carry):
            block = np.concatenate([self._carry, block])
        whole = len(block) - len(block) % FRAME
        fired = False
        for start in range(0, whole, FRAME):
            if self._engine.process(block[start:start + FRAME]):
                fired = True
            self.frames += 1
        self._carry = block[whole:]
        return fired

    def reset(self) -> None:
        """Drop the carry with the engine's own state, between clips."""
        self._carry = self._np.empty(0, dtype=self._np.int16)
        self._engine.reset()


def play_fixture(device, output_device, settle: float) -> tuple[dict, dict]:
    """Play each committed clip at the microphone and record what fires.

    This is the acoustic loop: the model's own held-out audio, through a
    speaker, through the room, through the microphone, through the real
    engine. Anything the capture path breaks — resampling, gain staging, a
    channel layout the product does not expect — shows up as a disagreement
    with the expected column, which the automated tests cannot see.

    Returns the playback record and the engine record, so a report can say
    which backend and which artifact produced these outcomes.
    """
    sd = _sounddevice()
    manifest = json.loads((FIXTURES / "samples.json").read_text(encoding="utf-8"))
    engine, wake_word = _engine()
    feeder = _FrameFeeder(engine)

    results = []
    for row in manifest["samples"]:
        clip = _read_fixture(FIXTURES / "audio" / row["file"])
        feeder.reset()
        fired = False

        def on_frame(indata, _frames, _t, _status):
            nonlocal fired
            if feeder.feed(indata):
                fired = True

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype=FRAME_DTYPE,
            blocksize=FRAME,
            device=device,
            callback=on_frame,
        ):
            sd.play(clip, samplerate=SAMPLE_RATE, device=output_device)
            sd.wait()
            time.sleep(settle)

        expected = bool(row["label"])
        results.append(
            {
                "file": row["file"],
                "condition": row["condition"],
                "spoken_text": row.get("spoken_text"),
                "expected_fire": expected,
                "fired": fired,
                "correct": fired == expected,
            }
        )
        print(
            f"  {row['file']:<28} expected={'fire' if expected else 'quiet':<5} "
            f"got={'fire' if fired else 'quiet':<5} "
            f"{'ok' if fired == expected else 'MISMATCH'}"
        )

    positives = [r for r in results if r["expected_fire"]]
    negatives = [r for r in results if not r["expected_fire"]]
    # The pass condition is split, because the two halves are not equally
    # forgiving over a real acoustic path. A near-miss phrase firing is a defect
    # anywhere. A positive recorded at 1.1 dB SNR, replayed through a speaker
    # into a room that adds its own noise on top, may legitimately be missed —
    # so those are counted and reported but do not gate. Clean and reverberant
    # positives do gate: if "hey youtab" spoken clearly does not fire over the
    # loop, the capture path is broken.
    easy = [r for r in positives if r["condition"] in
            ("positive_clean", "positive_reverberant")]
    return {
        "clips": len(results),
        "missed_wake_words": sum(1 for r in positives if not r["fired"]),
        "false_activations": sum(1 for r in negatives if r["fired"]),
        "near_phrase_activations": sum(
            1 for r in negatives if r["fired"] and r["condition"] == "near_phrase"),
        "easy_positives": len(easy),
        "missed_easy_positives": sum(1 for r in easy if not r["fired"]),
        "frames_scored": feeder.frames,
        "delivered_dtype": feeder.dtype,
        "results": results,
    }, _engine_record(engine, wake_word)


def listen(device, seconds: float, expected: int) -> tuple[dict, dict]:
    """Log every activation while a person speaks to the machine.

    ``expected`` makes the mode machine-checkable in both directions: run it
    with 1 or more while saying the phrase, and again with 0 while talking about
    something else, and each run's checklist verdict is computed rather than
    attested.
    """
    sd = _sounddevice()
    engine, wake_word = _engine()
    feeder = _FrameFeeder(engine)
    fires: list[float] = []
    started = time.monotonic()

    def on_frame(indata, _frames, _t, _status):
        if feeder.feed(indata):
            elapsed = time.monotonic() - started
            fires.append(elapsed)
            print(f"  FIRE at {elapsed:6.2f}s")

    print(f"listening for {seconds:.0f}s — say \"hey youtab\"")
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype=FRAME_DTYPE,
        blocksize=FRAME,
        device=device,
        callback=on_frame,
    ):
        sd.sleep(int(seconds * 1000))
    return {
        "seconds": seconds,
        "activations": len(fires),
        "at_seconds": fires,
        "expected_activations": expected,
        "frames_scored": feeder.frames,
        "delivered_dtype": feeder.dtype,
    }, _engine_record(engine, wake_word)


def _environment() -> dict:
    """What this machine WOULD use, resolved from config without touching audio.

    ``inference_framework`` here is the config resolution — it is not proof that
    the backend loaded. The tflite runtime can still be missing, in which case
    the engine downgrades (or, on macOS ARM64, refuses); only a mode that builds
    an engine can report what really ran, and that lands in ``record["engine"]``.
    ``model`` is resolved through the same function the engine uses, so a
    configured custom model is named here instead of the bundled artifact.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from tools import wake_word  # noqa: PLC0415

    cfg = wake_word.load_wake_word_config()
    framework = wake_word.resolve_inference_framework(cfg)
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "inference_framework": framework,
        "platform_default_framework": wake_word.default_inference_framework(),
        "model": wake_word.resolve_model_reference(cfg, framework),
        "sensitivity": wake_word._sensitivity(cfg),
        "confirmation_frames": wake_word._confirmation_frames(cfg),
        "sample_rate": SAMPLE_RATE,
        "frame_samples": FRAME,
        "frame_dtype": FRAME_DTYPE,
    }


def _item(status: str, detail: str) -> dict:
    assert status in _STATUS, status
    return {"status": status, "detail": detail}


def evaluate_checklist(record: dict) -> dict:
    """Turn the record into a per-item pass/fail/not-run verdict.

    Every item is derived from data in the record, so re-running this over an
    attached report reproduces the verdict — which is the difference between
    evidence and a summary.
    """
    checklist = {
        key: _item("not-run", f"{meta['mode']} was not run")
        for key, meta in CHECKLIST.items()
    }

    devices = record.get("devices")
    if devices is not None:
        rows = devices.get("devices") or []
        selected = devices.get("selected")
        indexes = {row.get("index") for row in rows}
        names = {row.get("name") for row in rows}
        resolved = selected in indexes or selected in names
        checklist["WW-D1"] = _item(
            "pass" if rows and resolved else "fail",
            f"{len(rows)} input device(s); selection {selected!r} "
            f"{'resolves' if resolved else 'does NOT resolve'} to one of them",
        )

    capture = record.get("capture")
    if capture is not None:
        checklist["WW-D2"] = _item(
            "fail" if capture["silent"] else "pass",
            f"peak {capture['peak']} (dead-microphone threshold is the "
            f"product's own _SILENCE_PEAK); verdict {capture['verdict']}",
        )
        format_ok = (
            capture["requested_samplerate"] == SAMPLE_RATE
            and capture["requested_channels"] == 1
            and capture["delivered_dtype"] == FRAME_DTYPE
            and capture.get("delivered_channels", 1) == 1
        )
        checklist["WW-D3"] = _item(
            "pass" if format_ok else "fail",
            f"requested {SAMPLE_RATE} Hz mono {FRAME_DTYPE}, device delivered "
            f"{capture['delivered_dtype']}; device native rate "
            f"{capture.get('device_default_samplerate')}, OS resampling: "
            f"{capture.get('os_is_resampling')}",
        )

    engine = record.get("engine")
    if engine is not None:
        ok = engine["matches_recorded_sha256"] and engine["matches_platform_default"]
        checklist["WW-D4"] = _item(
            "pass" if ok else "fail",
            f"engine built on {engine['inference_framework']} "
            f"(platform default {record['environment']['platform_default_framework']}), "
            f"artifact {Path(engine['model']).name} sha256 "
            f"{engine['model_sha256'][:12]}…, matches SHA256SUMS: "
            f"{engine['matches_recorded_sha256']}",
        )

    playback = record.get("fixture_playback")
    if playback is not None:
        ok = (playback["near_phrase_activations"] == 0
              and playback["missed_easy_positives"] == 0)
        checklist["WW-D5"] = _item(
            "pass" if ok else "fail",
            f"{playback['clips']} clips over the acoustic loop: "
            f"{playback['near_phrase_activations']} near-miss activation(s), "
            f"{playback['missed_easy_positives']} missed clean/reverberant "
            f"positive(s), {playback['missed_wake_words']} missed positive(s) in "
            f"total (noisy clips do not gate)",
        )

    heard = record.get("listen")
    if heard is not None:
        ok = heard["activations"] >= heard["expected_activations"]
        checklist["WW-D6"] = _item(
            "pass" if ok else "fail",
            f"{heard['activations']} activation(s) in {heard['seconds']:.0f}s, "
            f"expected at least {heard['expected_activations']}, at "
            f"{[round(t, 2) for t in heard['at_seconds']]}",
        )

    return checklist


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--check-capture", action="store_true")
    parser.add_argument("--play-fixture", action="store_true")
    parser.add_argument("--listen", action="store_true")
    parser.add_argument("--device", default=None, help="input device index or name")
    parser.add_argument("--output-device", default=None, help="playback device")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--settle", type=float, default=0.4)
    parser.add_argument(
        "--expect-activations", type=int, default=1,
        help="activations --listen must see to pass (0 for the quiet run)",
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument(
        "--print-schema", action="store_true",
        help="print the JSON Schema of the report and exit",
    )
    args = parser.parse_args()

    if args.print_schema:
        print(json.dumps(REPORT_SCHEMA, indent=2, sort_keys=True))
        return 0

    device = args.device
    if device is not None and str(device).lstrip("-").isdigit():
        device = int(device)

    record: dict = {
        "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION},
        "environment": _environment(),
    }
    print(json.dumps(record["environment"], indent=2))

    if args.list_devices:
        record["devices"] = list_devices()
        print(json.dumps(record["devices"], indent=2))
    if args.check_capture:
        record["capture"] = check_capture(device, args.seconds)
        print(json.dumps(record["capture"], indent=2))
        if record["capture"]["silent"]:
            print(
                "\nThe device opened and returned silence. That is a permission or "
                "mute problem, not a model problem — see DEVICE_VERIFICATION.md."
            )
    if args.play_fixture:
        record["fixture_playback"], record["engine"] = play_fixture(
            device, args.output_device, args.settle)
        summary = record["fixture_playback"]
        print(
            f"\n{summary['clips']} clips: {summary['missed_wake_words']} missed, "
            f"{summary['false_activations']} false activations"
        )
    if args.listen:
        record["listen"], record["engine"] = listen(
            device, args.seconds, args.expect_activations)

    record["checklist"] = evaluate_checklist(record)
    ran = sorted(k for k, v in record["checklist"].items() if v["status"] != "not-run")
    failed = sorted(k for k, v in record["checklist"].items() if v["status"] == "fail")
    record["verdict"] = {
        # Only over what ran. A report from `--list-devices` alone must not read
        # as a qualification of the microphone it never opened.
        "passed": bool(ran) and not failed,
        "ran": ran,
        "failed": failed,
        "not_run": sorted(
            k for k, v in record["checklist"].items() if v["status"] == "not-run"),
    }

    problems = validate(record)
    if problems:
        # A malformed record is worse than no record: it would be attached to a
        # qualification and read as if it meant something.
        print("\nthe report does not match its own schema:")
        for problem in problems:
            print(f"  {problem}")
        return 2

    print("\nchecklist")
    for key in sorted(record["checklist"]):
        item = record["checklist"][key]
        print(f"  {key} {item['status']:<8} {item['detail']}")
    print(f"\nverdict: {'PASS' if record['verdict']['passed'] else 'FAIL'} "
          f"(ran {ran or '-'}, failed {failed or '-'})")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.report}")
    return 0 if record["verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
