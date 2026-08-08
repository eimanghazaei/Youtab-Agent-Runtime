#!/usr/bin/env python3
"""Measure the shipped model the way the product will actually use it.

Not a validation accuracy. This drives ``tools.wake_word._OpenWakeWordEngine``
— the same class the CLI, TUI and desktop app construct — one 1280-sample
frame at a time, through the same streaming feature buffer, the same raw
threshold and the same N-consecutive-frames confirmation rule. A number
produced any other way describes something the user will never run.

Reported per category, because one averaged false-accept rate hides the only
failure that matters. Firing on room tone and firing on "hey youtube" are not
the same defect, and a detector can be excellent at one while being useless at
the other.

Two views of false accepts:

* **per window** — of N two-second negative windows, how many fired.
* **per hour** — the same fires expressed against the audio duration, which is
  the number that predicts how often an always-on listener interrupts someone
  who was not talking to it.

Both backends are measured separately. openWakeWord's ONNX and tflite feature
extractors are not bit-identical, so the same classifier can land on a
different score; the macOS ARM64 user runs the tflite pair and deserves a
measured number rather than an assumption.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

FRAME = 1280
SAMPLE_RATE = 16000

#: Thresholds to sweep. The default `wake_word.sensitivity` is 0.6.
SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)


def _engine(model_path: Path, framework: str, threshold: float, confirmation: int):
    """Construct the product's engine against a specific artifact."""
    from tools import wake_word  # noqa: PLC0415

    cfg = {
        "provider": "openwakeword",
        "sensitivity": threshold,
        "confirmation_frames": confirmation,
        "openwakeword": {"model": str(model_path), "inference_framework": framework},
    }
    return wake_word._OpenWakeWordEngine(cfg)


def _score_shard(job: tuple) -> tuple[int, np.ndarray, np.ndarray]:
    """Score one contiguous slice of the corpus in a worker process.

    The clips are independent — the engine is reset between each one — so
    sharding changes nothing about the result, only how long it takes. Each
    worker memory-maps the audio and builds its own engine; nothing is pickled
    across the boundary except the slice bounds and the scores.
    """
    audio_path, start, stop, repo_root, model_path, framework, threshold, confirmation = job
    sys.path.insert(0, str(repo_root))
    audio = np.load(audio_path, mmap_mode="r")[start:stop]
    scores, fired = score_clips(
        np.ascontiguousarray(audio), Path(model_path), framework, threshold, confirmation,
        quiet=True,
    )
    return start, scores, fired


def score_corpus(
    audio_path: Path,
    total: int,
    model_path: Path,
    framework: str,
    threshold: float,
    confirmation: int,
    repo_root: Path,
    jobs: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Score every clip, across ``jobs`` processes."""
    if jobs <= 1:
        return score_clips(
            np.load(audio_path, mmap_mode="r")[:],
            model_path,
            framework,
            threshold,
            confirmation,
        )

    from concurrent.futures import ProcessPoolExecutor  # noqa: PLC0415

    edges = np.linspace(0, total, jobs + 1).astype(int)
    work = [
        (
            str(audio_path),
            int(edges[i]),
            int(edges[i + 1]),
            str(repo_root),
            str(model_path),
            framework,
            threshold,
            confirmation,
        )
        for i in range(jobs)
        if edges[i + 1] > edges[i]
    ]
    scores: np.ndarray | None = None
    fired = np.zeros(total, dtype=bool)
    done = 0
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for start, shard_scores, shard_fired in pool.map(_score_shard, work):
            if scores is None:
                scores = np.zeros((total, shard_scores.shape[1]), dtype=np.float32)
            scores[start : start + len(shard_scores)] = shard_scores
            fired[start : start + len(shard_fired)] = shard_fired
            done += len(shard_scores)
            print(f"    {done}/{total}", end="\r", flush=True)
    print(f"    {total}/{total}")
    assert scores is not None
    return scores, fired


def score_clips(
    audio: np.ndarray,
    model_path: Path,
    framework: str,
    threshold: float,
    confirmation: int,
    quiet: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Stream every clip through the product engine, one 80 ms frame at a time.

    Returns ``(per-frame scores, did the engine fire)``. The fire decision is
    the engine's own — ``_OpenWakeWordEngine.process`` applying its threshold
    and its consecutive-frame rule — not a reconstruction. The per-frame
    scores are read back out of openWakeWord's prediction buffer in the same
    pass, which is what makes the threshold sweep possible without scoring the
    corpus eight more times.

    ``reset()`` between clips is what the runtime does when it resumes after a
    voice turn, so two adjacent windows cannot bleed into one another.
    """
    engine = _engine(model_path, framework, threshold=threshold, confirmation=confirmation)
    label = engine._labels[0]
    frames_per_clip = audio.shape[1] // FRAME
    scores = np.zeros((audio.shape[0], frames_per_clip), dtype=np.float32)
    fired = np.zeros(audio.shape[0], dtype=bool)
    for i in range(audio.shape[0]):
        engine.reset()
        clip = audio[i]
        for f in range(frames_per_clip):
            if engine.process(clip[f * FRAME : (f + 1) * FRAME]):
                fired[i] = True
            # Read fresh each frame: Model.reset() rebinds prediction_buffer to
            # a new defaultdict, so a reference captured before the loop goes
            # stale on the first clip boundary.
            scores[i, f] = float(engine._model.prediction_buffer[label][-1])
        if not quiet and (i + 1) % 500 == 0:
            print(f"    {i + 1}/{audio.shape[0]}", end="\r", flush=True)
    if not quiet:
        print(f"    {audio.shape[0]}/{audio.shape[0]}")
    return scores, fired


def fires_from_scores(frame_scores: np.ndarray, threshold: float, confirmation: int) -> np.ndarray:
    """Apply the engine's N-consecutive-frames rule to per-frame scores."""
    over = frame_scores >= threshold
    streak = np.zeros(over.shape[0], dtype=np.int32)
    fired = np.zeros(over.shape[0], dtype=bool)
    for f in range(over.shape[1]):
        streak = np.where(over[:, f], streak + 1, 0)
        fired |= streak >= confirmation
        streak = np.where(fired, 0, streak)
    return fired


def longest_run(over: np.ndarray) -> np.ndarray:
    """Longest run of consecutive True per row."""
    best = np.zeros(over.shape[0], dtype=np.int32)
    current = np.zeros(over.shape[0], dtype=np.int32)
    for f in range(over.shape[1]):
        current = np.where(over[:, f], current + 1, 0)
        best = np.maximum(best, current)
    return best


def plateau_stats(frame_scores: np.ndarray, labels: np.ndarray, threshold: float) -> dict:
    """How long the score actually stays up on a real utterance.

    The engine needs three consecutive frames over threshold. That is a claim
    about the *shape* of the score over time, not its peak, and it is the one
    thing a peak-only metric cannot see: a model that spikes to 0.99 for a
    single frame and drops looks perfect and never fires. This measures the
    margin the confirmation rule is operating on.
    """
    runs = longest_run(frame_scores[labels == 1] >= threshold)
    return {
        "median_frames_over_threshold": float(np.median(runs)),
        "p10_frames_over_threshold": float(np.percentile(runs, 10)),
        "min_frames_over_threshold": int(runs.min()),
        "utterances_never_over_threshold": int((runs == 0).sum()),
    }


def summarize(
    frame_scores: np.ndarray,
    labels: np.ndarray,
    categories: list[str],
    confirmation: int,
    window_seconds: float,
) -> dict:
    """False-accept and false-reject rates at every swept threshold."""
    cats = np.array(categories)
    result: dict[str, dict] = {}
    for threshold in SWEEP:
        fired = fires_from_scores(frame_scores, threshold, confirmation)
        positives = labels == 1
        negatives = labels == 0
        negative_hours = float(negatives.sum()) * window_seconds / 3600.0
        per_category = {}
        for name in sorted(set(categories)):
            mask = (cats == name) & negatives
            if not mask.any():
                continue
            hours = float(mask.sum()) * window_seconds / 3600.0
            per_category[name] = {
                "windows": int(mask.sum()),
                "false_accepts": int(fired[mask].sum()),
                "false_accept_rate": float(fired[mask].mean()),
                "false_accepts_per_hour": float(fired[mask].sum()) / hours if hours else 0.0,
            }
        result[f"{threshold:.2f}"] = {
            "false_reject_rate": float((~fired[positives]).mean()) if positives.any() else None,
            "false_rejects": int((~fired[positives]).sum()),
            "positive_windows": int(positives.sum()),
            "false_accept_rate": float(fired[negatives].mean()) if negatives.any() else None,
            "false_accepts": int(fired[negatives].sum()),
            "negative_windows": int(negatives.sum()),
            "false_accepts_per_hour": (
                float(fired[negatives].sum()) / negative_hours if negative_hours else 0.0
            ),
            "by_category": per_category,
            "positive_score_plateau": plateau_stats(frame_scores, labels, threshold),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True, help="dir with audio_eval.npy")
    parser.add_argument("--models", type=Path, required=True, help="dir with hey_youtab.*")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--confirmation-frames", type=int, default=3)
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.6,
        help="the runtime's default wake_word.sensitivity",
    )
    parser.add_argument("--limit", type=int, default=0, help="0 = every window")
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="worker processes; clips are independent so this only changes speed",
    )
    parser.add_argument(
        "--frameworks", nargs="+", default=["onnx", "tflite"], choices=["onnx", "tflite"]
    )
    args = parser.parse_args()

    sys.path.insert(0, str(args.repo_root))
    args.out.parent.mkdir(parents=True, exist_ok=True)

    audio_path = args.features / "audio_eval.npy"
    audio = np.load(audio_path, mmap_mode="r")
    labels = np.load(args.features / "y_eval.npy")
    categories = json.loads((args.features / "categories_eval.json").read_text(encoding="utf-8"))
    total = int(audio.shape[0]) if not args.limit else min(args.limit, int(audio.shape[0]))
    labels, categories = labels[:total], categories[:total]

    window_seconds = audio.shape[1] / SAMPLE_RATE
    report = {
        "windows": total,
        "positive_windows": int((labels == 1).sum()),
        "negative_windows": int((labels == 0).sum()),
        "window_seconds": window_seconds,
        "confirmation_frames": args.confirmation_frames,
        "negative_audio_hours": float((labels == 0).sum()) * window_seconds / 3600.0,
        "backends": {},
    }
    for framework in args.frameworks:
        model_path = args.models / f"hey_youtab.{framework}"
        print(f"  scoring {total} windows through the {framework} engine "
              f"({args.jobs} workers)")
        frame_scores, engine_fired = score_corpus(
            audio_path,
            total,
            model_path,
            framework,
            args.threshold,
            args.confirmation_frames,
            args.repo_root,
            args.jobs,
        )
        table = summarize(
            frame_scores, labels, categories, args.confirmation_frames, window_seconds
        )
        # The sweep is derived from per-frame scores; the headline row is the
        # engine's own verdict. If the two disagree on a single window the
        # derivation is wrong, and every other row in the sweep is suspect --
        # so this is an error, not a warning.
        derived = fires_from_scores(frame_scores, args.threshold, args.confirmation_frames)
        disagreements = int((derived != engine_fired).sum())
        if disagreements:
            raise SystemExit(
                f"{framework}: threshold sweep disagrees with the engine on "
                f"{disagreements} of {len(engine_fired)} windows"
            )
        table[f"{args.threshold:.2f}"]["source"] = "tools.wake_word._OpenWakeWordEngine.process"
        report["backends"][framework] = table
        np.save(args.out.parent / f"frame_scores_{framework}.npy", frame_scores)

    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for framework, table in report["backends"].items():
        at = table[f"{args.threshold:.2f}"]
        print(
            f"{framework:7s} @0.60  false-reject {at['false_reject_rate'] * 100:.2f}%  "
            f"false-accept {at['false_accept_rate'] * 100:.3f}%  "
            f"({at['false_accepts_per_hour']:.2f}/hour)"
        )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
