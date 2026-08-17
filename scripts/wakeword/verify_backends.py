#!/usr/bin/env python3
"""Run both shipped artifacts on this operating system and prove they agree.

Why this exists separately from the regression suite
----------------------------------------------------
``tests/tools/test_wake_word_model_assets.py`` already scores
``hey_youtab.onnx`` and ``hey_youtab.tflite`` on the committed fixtures — but
only ever on ``ubuntu-latest``, because that is the only runner the gate uses.
Two of the three desktop platforms have therefore never executed the file their
users load:

* **Windows** loads the ONNX artifact for every user, and no Windows machine
  has run it in any recorded gate.
* **macOS ARM64** loads the *tflite* artifact, because openWakeWord's ONNX
  embedding model returns near-zero scores there (dscripka/openWakeWord#336).
  That is the one platform where the second file is not a spare, and it is the
  least covered.

"Both backends work" is also not the same claim as "both backends agree". They
are two encodings of one set of weights, so a per-sample disagreement is a
conversion or a runtime bug, and the deterministic-reset work
(``DETERMINISM.md``) showed how easily such a disagreement is misattributed:
what looked like a 1.1e-01 gap between the front ends was a random buffer
prime, and the true divergence is ~1e-5.

Acceptance additionally requires latency and memory to be *measured* rather
than assumed, per backend and per platform. This produces that record.

What it measures, and what it does not
--------------------------------------
Only the phrase classifier — the part this repository ships. openWakeWord's
shared melspectrogram and embedding front end is downloaded at runtime and is
not committed, so the fixtures are already-extracted 16x96 feature frames.
End-to-end capture latency belongs to ``verify_on_device.py``, which needs a
microphone; this needs nothing but the two files.

Run it with::

    python scripts/wakeword/verify_backends.py --report backend-report.json

Exit status is 0 only if every criterion in the report passed.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import platform
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "wakeword"
WAKEWORDS = REPO_ROOT / "tools" / "wakewords"

#: Same tolerance the regression suite pins. The shipped pair's recorded
#: maximum divergence is 2.68e-06 over 512 held-out windows, so this is two
#: orders of magnitude of headroom over the known conversion error and still
#: far below anything that could move a decision.
PARITY_TOLERANCE = 1e-4

#: openWakeWord rescores every 80 ms. A classifier slower than that cannot keep
#: up with its own input, whatever its accuracy, so this is a real ceiling
#: rather than a round number.
FRAME_INTERVAL_MS = 80.0

#: How many times each fixture is scored when timing. Enough for a stable p95
#: without making the run something people skip.
DEFAULT_REPEATS = 20


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _peak_rss_bytes() -> int:
    """High-water-mark resident set size of this process, in bytes.

    Stdlib only, because this script has to run on a bare machine that is not a
    training environment. ``psutil`` *is* a dependency (``pyproject.toml`` pins
    ``psutil==7.2.2``), so the original justification for avoiding it was wrong —
    but the conclusion still holds for a different and better reason: this script
    is the evidence that the shipped artifacts load on a fresh machine, so it
    must not require the project's dependency set to be installed first.

    The high-water mark rather than the current RSS: it is the number available
    on every platform without a native call per sample, it is monotonic, so a
    difference across a load is a lower bound on what that load cost, and it is
    the number that matters for a device budget.
    """
    if sys.platform == "win32":
        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_uint32),
                ("PageFaultCount", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        # Both restypes are mandatory. GetCurrentProcess returns the pseudo
        # handle (HANDLE)-1, and ctypes' default `c_int` return truncates it to
        # a 32-bit -1 that the 64-bit callee then reads as a wild pointer: the
        # call fails with a last-error of 0, which is the least informative
        # failure Windows has.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_Counters),
            ctypes.c_uint32,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
        return int(counters.PeakWorkingSetSize)

    import resource  # noqa: PLC0415 — POSIX only

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS and the BSDs report bytes. Same field,
    # different unit; getting this wrong is a 1024x error in a report nobody
    # would question.
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def load_fixtures() -> tuple["object", list[str], "object"]:
    """The committed evaluation-set features, labels and names."""
    import numpy as np  # noqa: PLC0415

    payload = np.load(FIXTURES / "samples.npz")
    return (
        payload["features"].astype(np.float32),
        [str(n) for n in payload["names"]],
        payload["labels"],
    )


def onnx_runner() -> tuple["object", float]:
    """A one-frame scorer over the shipped ONNX artifact, and its load cost.

    The session is built once and returned closed over, because that is how the
    runtime uses it: ``_OpenWakeWordEngine`` constructs a model at startup and
    then scores every 80 ms for as long as the agent runs. Timing a scorer that
    rebuilds the session per call would report construction as if it were
    inference — on this repository's own artifact that is a ~40x overstatement.
    """
    import onnxruntime as ort  # noqa: PLC0415

    start = time.perf_counter()
    session = ort.InferenceSession(
        str(WAKEWORDS / "hey_youtab.onnx"), providers=["CPUExecutionProvider"]
    )
    load_ms = (time.perf_counter() - start) * 1000.0
    name = session.get_inputs()[0].name

    def run_one(frame) -> float:
        return float(session.run(None, {name: frame[None, ...]})[0][0][0])

    return run_one, load_ms


def tflite_runner() -> tuple["object", float]:
    """The same, over the shipped tflite artifact — the file macOS ARM64 loads."""
    from ai_edge_litert.interpreter import Interpreter  # noqa: PLC0415

    start = time.perf_counter()
    interpreter = Interpreter(model_path=str(WAKEWORDS / "hey_youtab.tflite"))
    interpreter.allocate_tensors()
    load_ms = (time.perf_counter() - start) * 1000.0
    in_index = interpreter.get_input_details()[0]["index"]
    out_index = interpreter.get_output_details()[0]["index"]

    def run_one(frame) -> float:
        interpreter.set_tensor(in_index, frame[None, ...])
        interpreter.invoke()
        return float(interpreter.get_tensor(out_index)[0][0])

    return run_one, load_ms


def score_all(run_one, features) -> list[float]:
    return [run_one(frame) for frame in features]


def _time_backend(run_one, features, repeats: int) -> dict:
    """Per-inference latency in milliseconds.

    One warm-up pass is discarded: it carries first-touch page faults and the
    runtime's first kernel selection, neither of which happens again in a
    process that listens for hours. Every subsequent call is timed individually
    rather than averaged over a pass, so the p95 is a real tail and not a mean
    wearing a percentile's name.
    """
    score_all(run_one, features)  # warm-up, deliberately unmeasured
    per_call: list[float] = []
    for _ in range(repeats):
        for frame in features:
            start = time.perf_counter()
            run_one(frame)
            per_call.append((time.perf_counter() - start) * 1000.0)
    per_call.sort()

    def _pct(fraction: float) -> float:
        return per_call[min(len(per_call) - 1, int(fraction * len(per_call)))]

    # p90 and p99 are here because the predeclared design asks for
    # "latency p50/p90/p95/p99" and this function reported median/p95/max only —
    # so a backend-report.json could not satisfy acceptance target 5 on its own
    # terms. p99 needs the sample to be large enough to have a 99th percentile
    # at all; DEFAULT_REPEATS over every fixture frame is thousands of calls, and
    # `inferences` is reported so a reader can judge the tail rather than trust it.
    return {
        "inferences": len(per_call),
        "median_ms": statistics.median(per_call),
        "p50_ms": statistics.median(per_call),
        "p90_ms": _pct(0.90),
        "p95_ms": _pct(0.95),
        "p99_ms": _pct(0.99),
        "max_ms": per_call[-1],
    }


def verify(repeats: int = DEFAULT_REPEATS) -> dict:
    """Load both artifacts, score the fixtures with each, and compare."""
    import numpy as np  # noqa: PLC0415

    sys.path.insert(0, str(REPO_ROOT))
    from tools import wake_word  # noqa: PLC0415

    threshold = float(wake_word._DEFAULTS["sensitivity"])
    features, names, labels = load_fixtures()

    # Each backend is loaded, scored and timed in turn, with the peak RSS read
    # between them: the delta across a backend's own block is what that backend
    # cost, which is the number a device budget needs.
    backends = {}
    scored: dict[str, list[float]] = {}
    rss_before = _peak_rss_bytes()
    for backend, build in (("onnx", onnx_runner), ("tflite", tflite_runner)):
        run_one, load_ms = build()
        scores = score_all(run_one, features)
        latency = _time_backend(run_one, features, repeats)
        rss_after = _peak_rss_bytes()
        fired = [bool(s >= threshold) for s in scores]
        artifact = WAKEWORDS / f"hey_youtab.{backend}"
        scored[backend] = scores
        backends[backend] = {
            "artifact": artifact.name,
            "sha256": _sha256(artifact),
            "artifact_bytes": artifact.stat().st_size,
            "scores": dict(zip(names, scores)),
            "missed_wake_words": [
                n for n, f, label in zip(names, fired, labels) if label == 1 and not f
            ],
            "false_accepts": [
                n for n, f, label in zip(names, fired, labels) if label == 0 and f
            ],
            "load_ms": load_ms,
            "latency": latency,
            "peak_rss_bytes": rss_after,
            "peak_rss_delta_bytes": max(0, rss_after - rss_before),
        }
        rss_before = rss_after

    onnx_scores, tflite_scores = scored["onnx"], scored["tflite"]
    delta = np.abs(np.array(onnx_scores) - np.array(tflite_scores))
    worst = int(delta.argmax())

    checks = {
        "parity_within_tolerance": bool(delta.max() <= PARITY_TOLERANCE),
        "backends_agree_on_every_decision": all(
            (a >= threshold) == (b >= threshold)
            for a, b in zip(onnx_scores, tflite_scores)
        ),
        "onnx_detects_every_positive": not backends["onnx"]["missed_wake_words"],
        "tflite_detects_every_positive": not backends["tflite"]["missed_wake_words"],
        "onnx_rejects_every_negative": not backends["onnx"]["false_accepts"],
        "tflite_rejects_every_negative": not backends["tflite"]["false_accepts"],
        "onnx_keeps_up_with_the_frame_rate": (
            backends["onnx"]["latency"]["p95_ms"] < FRAME_INTERVAL_MS
        ),
        "tflite_keeps_up_with_the_frame_rate": (
            backends["tflite"]["latency"]["p95_ms"] < FRAME_INTERVAL_MS
        ),
        "memory_was_measurable": all(backends[b]["peak_rss_bytes"] > 0 for b in backends),
    }

    return {
        "schema_version": 1,
        "environment": {
            "platform": platform.platform(),
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "onnxruntime": _version("onnxruntime"),
            "ai_edge_litert": _version("ai_edge_litert"),
            # Which artifact THIS host would actually load at runtime. On macOS
            # ARM64 it is tflite; everywhere else onnx. Recorded so a report can
            # be read as evidence about a platform rather than about a laptop.
            "runtime_framework": wake_word.default_inference_framework(),
        },
        "fixtures": {
            "path": str(FIXTURES.relative_to(REPO_ROOT).as_posix()),
            "count": len(names),
            "positives": int((labels == 1).sum()),
            "negatives": int((labels == 0).sum()),
        },
        "threshold": threshold,
        "parity": {
            "tolerance": PARITY_TOLERANCE,
            "max_abs_delta": float(delta.max()),
            "mean_abs_delta": float(delta.mean()),
            "worst_sample": names[worst],
        },
        "backends": backends,
        "checks": checks,
        "passed": all(checks.values()),
    }


def _version(module_name: str) -> str:
    try:
        module = __import__(module_name)
    except ImportError as exc:
        # Not skipped and not swallowed: a backend that will not import is the
        # finding, and it travels into the report rather than out of it.
        return f"NOT INSTALLED ({exc})"
    return str(getattr(module, "__version__", "unknown"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=None, help="write JSON here")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    args = parser.parse_args()

    report = verify(args.repeats)

    env = report["environment"]
    print(f"{env['system']} {env['machine']}  python {env['python']}")
    print(f"  onnxruntime    {env['onnxruntime']}")
    print(f"  ai-edge-litert {env['ai_edge_litert']}")
    print(f"  this host loads: {env['runtime_framework']}")
    print(
        f"\nparity: max |delta| {report['parity']['max_abs_delta']:.3e} "
        f"(tolerance {PARITY_TOLERANCE:.0e}) on {report['parity']['worst_sample']}"
    )
    for name, backend in report["backends"].items():
        latency = backend["latency"]
        print(
            f"  {name:<7} {latency['median_ms'] * 1000:7.1f} us median, "
            f"{latency['p95_ms'] * 1000:7.1f} us p95, "
            f"load {backend['load_ms']:6.1f} ms   "
            f"peak RSS {backend['peak_rss_bytes'] / 1e6:.1f} MB "
            f"(+{backend['peak_rss_delta_bytes'] / 1e6:.1f} MB)"
        )
    print()
    for check, ok in sorted(report["checks"].items()):
        print(f"  {'PASS' if ok else 'FAIL'}  {check}")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {args.report}")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
