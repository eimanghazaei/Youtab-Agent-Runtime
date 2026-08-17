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


class BackendFallbackRefused(RuntimeError):
    """A requested backend silently became the other one.

    ``tools.wake_word`` coerces an explicit ``onnx`` to ``tflite`` on macOS
    ARM64 (openWakeWord #336) and downgrades ``tflite`` to ``onnx`` when the
    tflite runtime is missing. Either way ``self.inference_framework`` records
    what ACTUALLY ran. A measurement that labels a tflite run ``onnx`` is a
    false statement about the thing that ships — and it defeats parity outright:
    ``measure --backends onnx tflite`` would then be two tflite runs labelled
    onnx/tflite, agreeing by construction. Named to match
    ``round8_controller.BackendFallbackRefused``, whose ``engine_inference_-
    framework`` check this measurement layer is the real producer for.
    """


def engine_inference_framework(engine, requested: str) -> str:
    """The backend the engine actually built with, refusing a silent fallback.

    ``_OpenWakeWordEngine`` exposes ``inference_framework`` as the truth after
    both the macOS ARM64 coercion and the missing-runtime downgrade. Reading it
    back — rather than trusting the value that was requested — is the only thing
    that knows which library ran, so a coerced run is refused here instead of
    being scored and labelled with a backend that never executed.
    """
    actual = str(getattr(engine, "inference_framework", "") or "")
    if actual != requested:
        raise BackendFallbackRefused(
            f"{requested!r} was requested but the engine ran "
            f"{actual or 'nothing'!r}. A backend that quietly becomes the other "
            "one makes every figure labelled with it a statement about a build "
            "that was never measured, and makes backend parity vacuous."
        )
    return actual


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
    # Refuse before scoring a single frame if the engine did not build with the
    # backend that was asked for — a coerced run mislabels every number it
    # produces and turns parity into a comparison of a backend with itself.
    engine_inference_framework(engine, framework)
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


def _cost_probe(job: tuple) -> dict:
    """Time and measure one backend in a process of its own.

    Runs in a child so ``ru_maxrss`` is that backend's own high-water mark
    rather than the parent's accumulated total — the parent has already
    memory-mapped the corpus and, on the second backend, already loaded the
    first one's runtime, so a same-process reading would attribute both to
    whichever backend happened to run second.

    Deliberately single-process and unsharded: this measures what one frame
    costs the product on one core, which is the number a device budget is
    written against. Throughput under N workers is a different question and is
    not what a latency figure should answer.
    """
    import resource  # noqa: PLC0415 — POSIX; this probe is skipped on Windows
    import time  # noqa: PLC0415

    audio_path, clips, repo_root, model_path, framework, threshold, confirmation = job
    sys.path.insert(0, str(repo_root))
    audio = np.ascontiguousarray(np.load(audio_path, mmap_mode="r")[:clips])

    engine = _engine(Path(model_path), framework, threshold, confirmation)
    frames_per_clip = audio.shape[1] // FRAME

    # One untimed clip first: the first call through either runtime pays for
    # lazy allocation and kernel selection, and folding that into p99 would
    # describe a cost the user pays once as one they pay always.
    engine.reset()
    for f in range(frames_per_clip):
        engine.process(audio[0][f * FRAME : (f + 1) * FRAME])

    per_frame_ms: list[float] = []
    wall0 = time.perf_counter()
    for i in range(audio.shape[0]):
        engine.reset()
        clip = audio[i]
        for f in range(frames_per_clip):
            t0 = time.perf_counter_ns()
            engine.process(clip[f * FRAME : (f + 1) * FRAME])
            per_frame_ms.append((time.perf_counter_ns() - t0) / 1e6)
    wall = time.perf_counter() - wall0

    ms = np.asarray(per_frame_ms, dtype=np.float64)
    audio_seconds = audio.shape[0] * frames_per_clip * FRAME / SAMPLE_RATE
    return {
        "clips": int(audio.shape[0]),
        "frames": int(ms.size),
        "frame_seconds": FRAME / SAMPLE_RATE,
        "latency_ms": {
            "mean": float(ms.mean()),
            "p50": float(np.percentile(ms, 50)),
            "p90": float(np.percentile(ms, 90)),
            "p95": float(np.percentile(ms, 95)),
            "p99": float(np.percentile(ms, 99)),
            "max": float(ms.max()),
        },
        # <1.0 means the backend keeps up with a live microphone on one core.
        "real_time_factor": float(wall / audio_seconds),
        "audio_seconds_processed": float(audio_seconds),
        "wall_seconds": float(wall),
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        "model_bytes": int(Path(model_path).stat().st_size),
    }


def measure_cost(
    audio_path: Path,
    clips: int,
    repo_root: Path,
    model_path: Path,
    framework: str,
    threshold: float,
    confirmation: int,
) -> dict:
    """Latency and peak memory for one backend, measured in a clean child."""
    from concurrent.futures import ProcessPoolExecutor  # noqa: PLC0415

    job = (str(audio_path), clips, str(repo_root), str(model_path),
           framework, threshold, confirmation)
    with ProcessPoolExecutor(max_workers=1) as pool:
        return pool.submit(_cost_probe, job).result()


def detection_parity(
    fired: dict[str, np.ndarray], scores: dict[str, np.ndarray]
) -> dict:
    """Do the two backends make the same decisions, not merely similar scores?

    Numerical parity (``max_absolute_difference`` in train_model.py) compares
    raw outputs. It can look excellent while the backends still disagree on
    windows whose score sits within rounding distance of the threshold — and a
    disagreement there is a user-visible difference: one build wakes, the other
    does not. This measures the decision, which is the thing that ships.
    """
    if sorted(fired) != ["onnx", "tflite"]:
        return {"measured": False, "reason": "needs both backends"}

    a, b = fired["onnx"], fired["tflite"]
    disagreements = int((a != b).sum())
    sa, sb = scores["onnx"], scores["tflite"]
    delta = np.abs(sa.astype(np.float64) - sb.astype(np.float64))
    return {
        "measured": True,
        "windows": int(a.size),
        "detection_disagreements": disagreements,
        "detection_agreement_rate": float((a == b).mean()),
        "onnx_fired_tflite_did_not": int((a & ~b).sum()),
        "tflite_fired_onnx_did_not": int((b & ~a).sum()),
        "max_absolute_frame_score_difference": float(delta.max()),
        "mean_absolute_frame_score_difference": float(delta.mean()),
    }


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


def operating_key(threshold: float) -> str:
    """Key for the operating-point row.

    Four decimals, where the grid uses two, for a reason. ``--calibrate-
    operating-point`` selects an arbitrary threshold, and at two decimals a
    calibrated 0.601 would render as ``"0.60"`` and overwrite the grid row —
    leaving a row labelled 0.60 that was actually measured at 0.601. At four
    decimals the operating key can never collide with a two-decimal grid key,
    so the two live side by side and each says what it means. The exact float
    is stored in the row's ``threshold`` field regardless.
    """
    return f"{threshold:.4f}"


def summarize(
    frame_scores: np.ndarray,
    labels: np.ndarray,
    categories: list[str],
    confirmation: int,
    window_seconds: float,
    operating_threshold: float | None = None,
) -> dict:
    """False-accept and false-reject rates at every swept threshold.

    ``operating_threshold`` adds one more row at the threshold the model was
    actually calibrated to on validation. Without it the grid is the only thing
    measured, and a calibrated threshold that is not on the grid — which is the
    normal case, the shipped model sits at 0.9991 — simply has no row at all.
    """
    cats = np.array(categories)
    result: dict[str, dict] = {}
    swept = list(SWEEP)
    if operating_threshold is not None:
        swept.append(float(operating_threshold))
    for threshold in swept:
        is_operating = (
            operating_threshold is not None and threshold == float(operating_threshold)
        )
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
        result[operating_key(threshold) if is_operating else f"{threshold:.2f}"] = {
            "threshold": float(threshold),
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
    parser.add_argument(
        "--latency-clips",
        type=int,
        default=200,
        help=(
            "clips used for the single-process latency/memory probe. Separate "
            "from --limit: accuracy wants the whole corpus, cost wants a "
            "quiet single-core run. 0 skips the probe."
        ),
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
        "runtime_cost": {},
        "parity": {},
    }
    fired_by_backend: dict[str, np.ndarray] = {}
    scores_by_backend: dict[str, np.ndarray] = {}
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
            frame_scores, labels, categories, args.confirmation_frames,
            window_seconds, operating_threshold=args.threshold,
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
        table[operating_key(args.threshold)]["source"] = (
            "tools.wake_word._OpenWakeWordEngine.process"
        )
        # The backend that actually ran, carried into the record. score_clips
        # has already refused a mismatch, so this is a checked truth rather than
        # a restated request — and it is the field round8_controller reads to
        # refuse a fallback (BackendFallbackRefused).
        table[operating_key(args.threshold)]["engine_inference_framework"] = framework
        report["backends"][framework] = table
        np.save(args.out.parent / f"frame_scores_{framework}.npy", frame_scores)
        fired_by_backend[framework] = engine_fired
        scores_by_backend[framework] = frame_scores

        if args.latency_clips:
            print(f"  measuring {framework} latency and peak memory "
                  f"({args.latency_clips} clips, single process)")
            report["runtime_cost"][framework] = measure_cost(
                audio_path,
                min(args.latency_clips, total),
                args.repo_root,
                model_path,
                framework,
                args.threshold,
                args.confirmation_frames,
            )

    # Numerical parity is checked at export time in train_model.py. This is the
    # decision-level counterpart: two backends can agree numerically to 1e-7 and
    # still split on a window sitting on the threshold, and that split is what a
    # user would experience as "it wakes on my Mac but not on my PC".
    report["parity"] = detection_parity(fired_by_backend, scores_by_backend)

    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for framework, table in report["backends"].items():
        at = table[operating_key(args.threshold)]
        print(
            f"{framework:7s} @{args.threshold:.2f}  "
            f"false-reject {at['false_reject_rate'] * 100:.2f}%  "
            f"false-accept {at['false_accept_rate'] * 100:.3f}%  "
            f"({at['false_accepts_per_hour']:.2f}/hour)"
        )
        cost = report["runtime_cost"].get(framework)
        if cost:
            print(
                f"{'':7s}        frame p50 {cost['latency_ms']['p50']:.3f} ms  "
                f"p99 {cost['latency_ms']['p99']:.3f} ms  "
                f"RTF {cost['real_time_factor']:.4f}  "
                f"peak RSS {cost['peak_rss_mb']:.0f} MB"
            )

    parity = report["parity"]
    if parity.get("measured"):
        print(
            f"parity  detection {parity['detection_disagreements']} disagreement(s) "
            f"over {parity['windows']} windows "
            f"({parity['detection_agreement_rate'] * 100:.4f}% agreement), "
            f"max frame-score delta {parity['max_absolute_frame_score_difference']:.3e}"
        )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
