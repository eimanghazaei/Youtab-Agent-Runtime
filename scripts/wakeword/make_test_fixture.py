#!/usr/bin/env python3
"""Cut the committed regression fixture out of the evaluation set.

``tests/tools/test_wake_word_model_assets.py`` needs real audio, but CI has no
openWakeWord front end — it is downloaded at first use rather than committed —
so the fixture ships both the waveform (auditable: play it and hear whether it
says the wake phrase) and the front-end features that waveform produces (which
is what the test can actually run without the network).

Selection is stratified by *condition* and seeded, never by score. Picking the
negatives the model already handles well, or the positives it happens to be
confident about, would produce a fixture that passes by construction and
detects nothing. What goes in is decided before any of it is scored:

* positives across the SNR range, including the hard end, with and without
  reverberation
* near misses spread across distinct phrases, so one text cannot stand in for
  all of them
* recorded human speech and background-only windows, drawn at random

Whatever the model then does on those windows is the measurement.
"""

from __future__ import annotations

import argparse
import json
import random
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000

#: How many of each condition to commit. Small enough that the fixture is a
#: megabyte or two, wide enough that the coverage assertion in the test means
#: something.
QUOTA = {
    "positive_clean": 4,
    "positive_noisy": 5,
    "positive_reverberant": 3,
    "near_phrase": 9,
    "recorded_speech": 6,
    "synthesized_speech": 3,
    "background_only": 3,
}

#: SNR at or below which a window counts as noisy. 8 dB is speech in a room
#: with the television on.
NOISY_SNR_DB = 8.0


def _tts_texts(tts_root: Path) -> dict[str, str]:
    """filename -> the text that was synthesized, for every TTS clip."""
    texts: dict[str, str] = {}
    for manifest in sorted(tts_root.glob("*/manifest.jsonl")):
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                # Keyed by `directory/file`: the bare name is not unique across
                # the six TTS groups, which all number their clips from zero.
                texts[f"{manifest.parent.name}/{row['file']}"] = row["text"]
    return texts


def _condition(setting: dict) -> str | None:
    """Which quota bucket a window belongs to, or None if it fills no quota."""
    category = setting["category"]
    if category != "positive":
        return category if category in QUOTA else None
    snr = setting.get("snr_db")
    if setting.get("reverb"):
        return "positive_reverberant"
    if snr is not None and snr <= NOISY_SNR_DB:
        return "positive_noisy"
    if snr is not None and snr >= 15.0:
        return "positive_clean"
    return None


def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(samples.astype(np.int16).tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--tts-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=41)
    args = parser.parse_args()

    audio = np.load(args.features / "audio_eval.npy")
    x = np.load(args.features / "x_eval.npy")
    y = np.load(args.features / "y_eval.npy")
    settings = json.loads((args.features / "settings_eval.json").read_text(encoding="utf-8"))
    texts = _tts_texts(args.tts_root)

    buckets: dict[str, list[int]] = defaultdict(list)
    for index, setting in enumerate(settings):
        bucket = _condition(setting)
        if bucket:
            buckets[bucket].append(index)

    rng = random.Random(args.seed)
    chosen: list[int] = []
    for bucket, wanted in QUOTA.items():
        available = buckets.get(bucket, [])
        if len(available) < wanted:
            raise SystemExit(f"only {len(available)} windows available for {bucket}, need {wanted}")
        if bucket == "near_phrase":
            # One per distinct phrase before repeating any, so the fixture
            # cannot end up being nine takes of "hey youtube".
            by_text: dict[str, list[int]] = defaultdict(list)
            for index in available:
                by_text[texts.get(settings[index]["source"], "?")].append(index)
            order = sorted(by_text)
            rng.shuffle(order)
            picks = [rng.choice(by_text[text]) for text in order[:wanted]]
        else:
            picks = rng.sample(available, wanted)
        chosen.extend(picks)

    chosen.sort()
    audio_dir = args.out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    for stale in audio_dir.glob("*.wav"):
        stale.unlink()

    names: list[str] = []
    rows: list[dict] = []
    for position, index in enumerate(chosen):
        setting = settings[index]
        bucket = _condition(setting)
        name = f"{position:02d}_{bucket}.wav"
        _write_wav(audio_dir / name, audio[index])
        names.append(name)
        rows.append(
            {
                "file": name,
                "category": setting["category"],
                "condition": bucket,
                "label": int(y[index]),
                "source": setting["source"],
                "spoken_text": texts.get(setting["source"]),
                "snr_db": (
                    round(setting["snr_db"], 2) if setting.get("snr_db") is not None else None
                ),
                "reverberant": bool(setting["reverb"]),
                "peak_dbfs": round(setting["peak_dbfs"], 2),
            }
        )

    np.savez_compressed(
        args.out / "samples.npz",
        features=x[chosen].astype(np.float32),
        labels=y[chosen].astype(np.uint8),
        categories=np.array([settings[i]["category"] for i in chosen]),
        names=np.array(names),
    )
    (args.out / "samples.json").write_text(
        json.dumps(
            {
                "speaker_pool": "evaluation",
                "selection": "stratified by condition, seeded, never by score",
                "seed": args.seed,
                "window_seconds": audio.shape[1] / SAMPLE_RATE,
                "noisy_snr_threshold_db": NOISY_SNR_DB,
                "quota": QUOTA,
                "samples": rows,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(chosen)} samples to {args.out}")
    for bucket in QUOTA:
        print(f"  {bucket:24s} {sum(1 for r in rows if r['condition'] == bucket)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
