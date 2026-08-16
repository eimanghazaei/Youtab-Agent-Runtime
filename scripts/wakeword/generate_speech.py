#!/usr/bin/env python3
"""Synthesize the spoken phrases the detector is trained and judged against.

Drives the piper-sample-generator LibriTTS-R model (904 speaker embeddings)
directly rather than through its CLI, for two reasons that matter to whether
the resulting numbers mean anything:

1. **Speaker pools are disjoint.** The CLI walks ``product(range(n), range(n))``
   in order, so a run of N clips only ever reaches the first ``N/n`` values of
   the first speaker index — and there is no way to ask it for *these* speakers
   and not those. This driver samples speaker pairs uniformly from an explicit
   pool, so the evaluation set can be synthesized from voices that training
   never saw. A false-reject rate measured on the training voices is not a
   false-reject rate.

2. **Every clip is accounted for.** Each one is written to a JSONL manifest
   with the speaker pair, mixing weight, speed and noise settings, and the
   text. That is the provenance record, and it is what makes a rerun
   reproducible rather than merely similar.

Voices are mixed pairwise by spherical interpolation between two speaker
embeddings, so the effective voice count is far larger than 904.

Output is 16 kHz mono 16-bit WAV — the rate the runtime captures at, so no
resampling happens between here and inference.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import wave
from pathlib import Path

import numpy as np
import torch
from scipy.signal import resample_poly

# The generator model emits 22.05 kHz; the wake-word runtime captures 16 kHz.
# 16000/22050 reduces to 320/441.
_TTS_RATE = 22050
_TARGET_RATE = 16000
_RESAMPLE_UP = 320
_RESAMPLE_DOWN = 441

#: Three disjoint speaker pools, fixed here rather than passed in so they
#: cannot drift between the run that trains a model and the run that measures
#: it. Evaluation keeps the range it has always had, so measurements stay
#: comparable across rounds; the training range was narrowed to carve out a
#: validation pool, because selecting an epoch *and* an operating point on a
#: random split of the training windows is selecting on voices the model has
#: already heard.
SPEAKER_POOLS = {
    "train": (0, 600),
    "validation": (600, 700),
    "eval": (700, 904),
}
SPEAKER_COUNT = 904

#: Speaking-rate multipliers. Lower is faster. Spanning 0.7-1.3 covers the
#: clipped "heyyoutab" and the drawn-out "heeey youuutab" alike.
_LENGTH_SCALES = (0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3)
#: VITS stochastic-duration and overall-variability knobs.
_NOISE_SCALES = (0.5, 0.667, 0.8)
_NOISE_SCALE_WS = (0.6, 0.8, 1.0)
#: How far to interpolate between the two speaker embeddings.
_SLERP_WEIGHTS = (0.0, 0.15, 0.3, 0.5, 0.7, 0.85, 1.0)


def _load_generator(model_path: Path):
    """Load the TTS checkpoint and its config."""
    model = torch.load(model_path, weights_only=False)
    model.eval()
    with (model_path.parent / f"{model_path.name}.json").open(encoding="utf-8") as fh:
        config = json.load(fh)
    return model, config


def _to_target_rate(audio: np.ndarray) -> np.ndarray:
    """22.05 kHz float -> 16 kHz int16."""
    resampled = resample_poly(audio.astype(np.float64), _RESAMPLE_UP, _RESAMPLE_DOWN)
    peak = float(np.abs(resampled).max())
    if peak > 1.0:
        resampled = resampled / peak
    return (resampled * 32767.0).astype(np.int16)


def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_TARGET_RATE)
        handle.writeframes(samples.tobytes())


def generate(
    texts: list[str],
    out_dir: Path,
    model_path: Path,
    generator_root: Path,
    count: int,
    speaker_lo: int,
    speaker_hi: int,
    seed: int,
    accents: list[str] | None = None,
    batch_size: int = 16,
    length_scales: list[float] | None = None,
) -> Path:
    """Synthesize ``count`` clips of ``texts`` from speakers [lo, hi).

    Returns the path of the JSONL manifest describing every clip written.

    ``length_scales`` defaults to the full spread, so an existing call
    reproduces exactly what it produced before.
    """
    length_scales = tuple(length_scales) if length_scales else _LENGTH_SCALES
    sys.path.insert(0, str(generator_root))
    from piper_sample_generator.__main__ import (  # noqa: PLC0415
        audio_float_to_int16,
        generate_audio,
        get_phonemes,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    model, config = _load_generator(model_path)
    rng = random.Random(seed)
    accent_pool = list(accents) if accents else [config["espeak"]["voice"]]

    # Phonemize every (accent, text) pair once, up front, and never call espeak
    # again.
    #
    # `get_phonemes` drives a module-level espeak singleton and switches its
    # voice on every call. Doing that per clip segfaults: four of eight
    # generation groups died with SIGSEGV, and a seeded repro crashed on one
    # seed while surviving 20,000 calls on two others. It is not a bad input --
    # every accent-by-text pair was swept individually without a crash -- so it
    # is state accumulating across voice switches inside the C library, which
    # this pipeline cannot fix from the outside.
    #
    # It can avoid it. The inventory is a dozen accents by a few dozen phrases,
    # so the whole cross-product is a few hundred calls, in the regime that
    # measured safe; the hot loop then does dictionary lookups. Faster, too.
    phoneme_cache = {
        (accent, text): get_phonemes(accent, config, text, False, False)
        for accent in dict.fromkeys(accent_pool)
        for text in dict.fromkeys(texts)
    }
    print(f"  phonemized {len(phoneme_cache)} accent/text pairs up front")

    manifest_path = out_dir / "manifest.jsonl"
    written = 0
    with manifest_path.open("w", encoding="utf-8") as manifest:
        while written < count:
            batch = min(batch_size, count - written)
            rows = [
                {
                    "text": rng.choice(texts),
                    "accent": rng.choice(accent_pool),
                    "speaker_1": rng.randrange(speaker_lo, speaker_hi),
                    "speaker_2": rng.randrange(speaker_lo, speaker_hi),
                    "slerp_weight": rng.choice(_SLERP_WEIGHTS),
                    "length_scale": rng.choice(length_scales),
                    "noise_scale": rng.choice(_NOISE_SCALES),
                    "noise_scale_w": rng.choice(_NOISE_SCALE_WS),
                }
                for _ in range(batch)
            ]
            # generate_audio applies one setting triple to the whole batch, so
            # group by taking the first row's settings and recording them for
            # every row in the batch. Settings are resampled each batch, so
            # across the run they are still uniformly covered.
            head = rows[0]
            for row in rows:
                for key in ("slerp_weight", "length_scale", "noise_scale", "noise_scale_w"):
                    row[key] = head[key]

            # Accent enters here: the acoustic model is the same throughout,
            # and what differs is the phoneme string it is asked to speak.
            phoneme_ids = [list(phoneme_cache[(r["accent"], r["text"])]) for r in rows]
            longest = max(len(p) for p in phoneme_ids)
            phoneme_ids = [p + [1] * (longest - len(p)) for p in phoneme_ids]

            with torch.no_grad():
                audio, phoneme_samples = generate_audio(
                    model,
                    torch.LongTensor([r["speaker_1"] for r in rows]),
                    torch.LongTensor([r["speaker_2"] for r in rows]),
                    phoneme_ids,
                    head["slerp_weight"],
                    head["noise_scale"],
                    head["noise_scale_w"],
                    head["length_scale"],
                    None,
                )
                for i in range(audio.shape[0]):
                    last = int(phoneme_samples[i].flatten().sum().item())
                    audio[i, 0, last + 1 :] = 0
                clips = audio_float_to_int16(audio.cpu().numpy())

            for i, row in enumerate(rows):
                trimmed = np.trim_zeros(clips[i].flatten())
                if trimmed.size < _TTS_RATE // 10:  # < 100 ms of speech: a dud
                    continue
                name = f"{written:06d}.wav"
                _write_wav(out_dir / name, _to_target_rate(trimmed / 32768.0))
                row["file"] = name
                row["samples_16k"] = int(round(trimmed.size * _TARGET_RATE / _TTS_RATE))
                manifest.write(json.dumps(row, sort_keys=True) + "\n")
                written += 1
                if written >= count:
                    break
            print(f"  {written}/{count}", end="\r", flush=True)
    print()
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--model", type=Path, required=True, help="generator .pt")
    parser.add_argument("--generator-root", type=Path, required=True)
    parser.add_argument(
        "--group",
        required=True,
        choices=("positive", "hard-negative", "confusable", "soft-negative", "common"),
    )
    parser.add_argument(
        "--split",
        required=True,
        choices=tuple(SPEAKER_POOLS),
        help="which disjoint speaker pool to draw voices from",
    )
    parser.add_argument(
        "--accents",
        action="store_true",
        help="phonemize across the weighted accent set instead of en-us only",
    )
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument(
        "--length-scales",
        type=float,
        nargs="+",
        default=None,
        metavar="SCALE",
        help=(
            "Speaking-rate multipliers to draw from (higher is slower). "
            f"Default: {' '.join(str(s) for s in _LENGTH_SCALES)}. "
            "Exists because the wake word is confirmed only after N "
            "consecutive frames over threshold, so a positive that peaks "
            "sharply and decays can score well and still never fire. "
            "Measured on the round-2 model: the median positive held 6 frames "
            "but the 10th percentile held 2, one short of the 3 required, and "
            "that tail was two thirds of all false rejects. Drawing a block of "
            "positives from the slower end widens the plateau. Not a default "
            "change: the full range still covers clipped and drawn-out alike."
        ),
    )
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import phrases  # noqa: PLC0415

    texts = {
        "positive": phrases.positive_texts(),
        "hard-negative": list(phrases.HARD_NEGATIVES),
        "confusable": list(phrases.CONFUSABLE_NEGATIVES),
        "soft-negative": list(phrases.SOFT_NEGATIVES),
        "common": list(phrases.COMMON_PHRASES),
    }[args.group]

    lo, hi = SPEAKER_POOLS[args.split]
    accent_pool = phrases.accents() if args.accents else None
    print(
        f"{args.group}/{args.split}: {args.count} clips from speakers [{lo}, {hi})"
        + (f" across {len(set(accent_pool))} accents" if accent_pool else "")
    )
    manifest = generate(
        texts=texts,
        out_dir=args.out,
        model_path=args.model,
        generator_root=args.generator_root,
        count=args.count,
        speaker_lo=lo,
        speaker_hi=hi,
        seed=args.seed,
        accents=accent_pool,
        batch_size=args.batch_size,
        length_scales=args.length_scales,
    )
    print(f"manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
