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

See DEVICE_VERIFICATION.md for the per-OS steps and the permission settings
each one needs.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "wakeword"

SAMPLE_RATE = 16000
FRAME = 1280


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
    """
    import numpy as np  # noqa: PLC0415

    sd = _sounddevice()
    frames = int(seconds * SAMPLE_RATE)
    captured = sd.rec(
        frames, samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=device
    )
    sd.wait()
    audio = np.asarray(captured).reshape(-1).astype(np.int32)
    peak = int(np.abs(audio).max())
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    sys.path.insert(0, str(REPO_ROOT))
    from tools import wake_word  # noqa: PLC0415

    return {
        "seconds": seconds,
        "peak": peak,
        "rms": rms,
        "peak_dbfs": (20.0 * np.log10(peak / 32768.0)) if peak else None,
        "silent": peak <= wake_word._SILENCE_PEAK,
        "verdict": "dead microphone" if peak <= wake_word._SILENCE_PEAK else "live",
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


def play_fixture(device, output_device, settle: float) -> dict:
    """Play each committed clip at the microphone and record what fires.

    This is the acoustic loop: the model's own held-out audio, through a
    speaker, through the room, through the microphone, through the real
    engine. Anything the capture path breaks — resampling, gain staging, a
    channel layout the product does not expect — shows up as a disagreement
    with the expected column, which the automated tests cannot see.
    """
    import numpy as np  # noqa: PLC0415

    sd = _sounddevice()
    manifest = json.loads((FIXTURES / "samples.json").read_text(encoding="utf-8"))
    engine, _ = _engine()

    results = []
    for row in manifest["samples"]:
        clip = _read_fixture(FIXTURES / "audio" / row["file"])
        engine.reset()
        fired = False

        def on_frame(indata, _frames, _t, _status):
            nonlocal fired
            block = np.asarray(indata).reshape(-1).astype(np.int16)
            for start in range(0, len(block) - FRAME + 1, FRAME):
                if engine.process(block[start : start + FRAME]):
                    fired = True

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
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
    return {
        "clips": len(results),
        "missed_wake_words": sum(1 for r in positives if not r["fired"]),
        "false_activations": sum(1 for r in negatives if r["fired"]),
        "results": results,
    }


def listen(device, seconds: float) -> dict:
    """Log every activation while a person speaks to the machine."""
    import numpy as np  # noqa: PLC0415

    sd = _sounddevice()
    engine, _ = _engine()
    fires: list[float] = []
    started = time.monotonic()

    def on_frame(indata, _frames, _t, _status):
        block = np.asarray(indata).reshape(-1).astype(np.int16)
        for start in range(0, len(block) - FRAME + 1, FRAME):
            if engine.process(block[start : start + FRAME]):
                elapsed = time.monotonic() - started
                fires.append(elapsed)
                print(f"  FIRE at {elapsed:6.2f}s")

    print(f"listening for {seconds:.0f}s — say \"hey youtab\"")
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME,
        device=device,
        callback=on_frame,
    ):
        sd.sleep(int(seconds * 1000))
    return {"seconds": seconds, "activations": len(fires), "at_seconds": fires}


def _environment() -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    from tools import wake_word  # noqa: PLC0415

    cfg = wake_word.load_wake_word_config()
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "inference_framework": wake_word.resolve_inference_framework(cfg),
        "model": wake_word._bundled_wakeword_path(
            wake_word.resolve_inference_framework(cfg)
        ),
        "sensitivity": wake_word._sensitivity(cfg),
        "confirmation_frames": wake_word._confirmation_frames(cfg),
    }


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
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    device = args.device
    if device is not None and str(device).lstrip("-").isdigit():
        device = int(device)

    record: dict = {"environment": _environment()}
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
        record["fixture_playback"] = play_fixture(device, args.output_device, args.settle)
        summary = record["fixture_playback"]
        print(
            f"\n{summary['clips']} clips: {summary['missed_wake_words']} missed, "
            f"{summary['false_activations']} false activations"
        )
    if args.listen:
        record["listen"] = listen(device, args.seconds)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
