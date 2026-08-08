#!/usr/bin/env python3
"""Fit the "hey youtab" classifier and export it for both runtime backends.

The model is a small fully-connected network over the sixteen 96-dimensional
frames openWakeWord's front end produces — the same shape upstream's own
wake-word models use, because it is dictated by the front end rather than
chosen. Everything interesting happened in the previous stage; this one keeps
the architecture deliberately plain so the artifact is auditable and converts
cleanly to both backends.

Two exports, one set of weights. ``tools/wake_word.py`` loads ONNX everywhere
except macOS ARM64, where openWakeWord's ONNX embedding model is broken
upstream and the runtime switches to tflite. A user on a Mac and a user on
Linux must get the same detector, so the two files are built from the same
tensors and then checked against each other on real data — a parity failure
fails the run rather than shipping two models that disagree.

Input normalization is folded into the first layer at export time. The trained
network standardizes its input as a separate step; the exported graphs do not,
because ``W/s`` and ``b - (W/s)m`` is exactly equivalent and leaves both files
as plain matrix multiplies with nothing backend-specific to misconvert.

Model selection uses a grouped split of the training set — every augmented copy
of one utterance stays on one side — and the reported numbers come from
``evaluate_model.py`` on genuinely held-out voices, rooms and noise. The
validation split here only picks an epoch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from torch import nn

FEATURE_FRAMES = 16
EMBEDDING_DIM = 96
FLAT = FEATURE_FRAMES * EMBEDDING_DIM

#: The runtime's default `wake_word.sensitivity`. Scores are compared to this
#: raw threshold, so it is the operating point the model is selected at rather
#: than an afterthought applied to a finished model.
OPERATING_THRESHOLD = 0.6


class WakeWordNet(nn.Module):
    """Standardize, flatten, four dense layers, one probability out."""

    def __init__(self, hidden: tuple[int, ...] = (256, 128, 64), dropout: float = 0.2):
        super().__init__()
        self.register_buffer("mean", torch.zeros(FLAT))
        self.register_buffer("std", torch.ones(FLAT))
        sizes = (FLAT, *hidden)
        self.hidden = nn.ModuleList(
            nn.Linear(sizes[i], sizes[i + 1]) for i in range(len(hidden))
        )
        self.dropout = nn.Dropout(dropout)
        self.out = nn.Linear(hidden[-1], 1)

    def set_normalization(self, mean: np.ndarray, std: np.ndarray) -> None:
        self.mean.copy_(torch.from_numpy(mean.astype(np.float32)))
        self.std.copy_(torch.from_numpy(std.astype(np.float32)))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = (x.reshape(x.shape[0], FLAT) - self.mean) / self.std
        for i, layer in enumerate(self.hidden):
            h = torch.relu(layer(h))
            if i < len(self.hidden) - 1:
                h = self.dropout(h)
        return torch.sigmoid(self.out(h))


class ExportNet(nn.Module):
    """The same function with normalization folded into the first layer."""

    def __init__(self, source: WakeWordNet):
        super().__init__()
        hidden = [nn.Linear(layer.in_features, layer.out_features) for layer in source.hidden]
        self.hidden = nn.ModuleList(hidden)
        self.out = nn.Linear(source.out.in_features, source.out.out_features)
        with torch.no_grad():
            mean = source.mean.detach().clone()
            std = source.std.detach().clone()
            first = source.hidden[0]
            scaled = first.weight / std.unsqueeze(0)
            self.hidden[0].weight.copy_(scaled)
            self.hidden[0].bias.copy_(first.bias - scaled @ mean)
            for dst, src in zip(self.hidden[1:], source.hidden[1:]):
                dst.weight.copy_(src.weight)
                dst.bias.copy_(src.bias)
            self.out.weight.copy_(source.out.weight)
            self.out.bias.copy_(source.out.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = x.reshape(x.shape[0], FLAT)
        for layer in self.hidden:
            h = torch.relu(layer(h))
        return torch.sigmoid(self.out(h))


def grouped_split(groups: np.ndarray, fraction: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Split window indices so no source utterance straddles the boundary."""
    unique = np.unique(groups)
    rng = np.random.default_rng(seed)
    rng.shuffle(unique)
    held = set(unique[: max(1, int(len(unique) * fraction))].tolist())
    mask = np.fromiter((g in held for g in groups), dtype=bool, count=groups.size)
    return np.flatnonzero(~mask), np.flatnonzero(mask)


def rates(scores: np.ndarray, labels: np.ndarray, threshold: float) -> tuple[float, float]:
    """(false-accept rate, false-reject rate) at ``threshold``."""
    fired = scores >= threshold
    negatives = labels == 0
    positives = labels == 1
    far = float(fired[negatives].mean()) if negatives.any() else 0.0
    frr = float((~fired[positives]).mean()) if positives.any() else 0.0
    return far, frr


def threshold_for_false_reject(scores: np.ndarray, labels: np.ndarray, target: float) -> float:
    """The score threshold at which the false-reject rate is ``target``."""
    positives = np.sort(scores[labels == 1])
    if positives.size == 0:
        return OPERATING_THRESHOLD
    index = min(int(round(target * positives.size)), positives.size - 1)
    return float(positives[index])


def calibration_shift(threshold: float) -> float:
    """Logit offset that would make ``threshold`` behave as the runtime default.

    Used for epoch *comparison*, and available for export behind
    ``--calibrate-operating-point``, which is off by default.

    Comparing epochs at a fixed 0.6 compares each one at whatever point on its
    own curve the loss happened to put it, which is not a comparison at all.
    Comparing them at a common false-reject rate is. But shifting the shipped
    bias to hit a false-reject target is a different decision, and a measured
    bad one: it moved the operating point to a part of the curve where false
    accepts on ordinary recorded speech tripled, from 0.105% to 0.272%, to buy
    two points of false-reject rate. The trained model's own 0.6 was measured
    to sit at a better place than any false-reject target put it, so that is
    what ships.
    """
    threshold = min(max(threshold, 1e-6), 1 - 1e-6)
    return float(np.log(OPERATING_THRESHOLD / (1 - OPERATING_THRESHOLD)) - np.log(threshold / (1 - threshold)))


def class_weights(categories: list[str], labels: np.ndarray, args) -> np.ndarray:
    """Per-window loss weight.

    Not one weight for "negative". The first trained model was measured at
    0.11% false accepts on recorded human speech and 0.00% on room tone, but
    13% on deliberate near misses -- "hey youtube", "hey your tab", "youtab"
    on its own. Averaged into a single negative class those 24,000 windows are
    a fifth of the negatives and get a fifth of the pressure, while being the
    entire problem. Weighting them separately puts the loss where the errors
    are.
    """
    weights = np.full(len(labels), args.negative_weight, dtype=np.float32)
    weights[labels > 0.5] = 1.0
    near = np.array([c == "near_phrase" for c in categories])
    weights[near & (labels < 0.5)] = args.hard_negative_weight
    return weights


def train(args: argparse.Namespace) -> tuple[WakeWordNet, dict]:
    torch.manual_seed(args.seed)
    x = np.load(args.features / "x_train.npy")
    y = np.load(args.features / "y_train.npy").astype(np.float32)
    groups = np.load(args.features / "groups_train.npy")
    categories = json.loads(
        (args.features / "categories_train.json").read_text(encoding="utf-8")
    )
    fit_idx, val_idx = grouped_split(groups, args.val_fraction, args.seed)

    flat = x.reshape(len(x), FLAT)
    mean = flat[fit_idx].mean(axis=0)
    std = flat[fit_idx].std(axis=0)
    std[std < 1e-6] = 1.0

    model = WakeWordNet(hidden=tuple(args.hidden), dropout=args.dropout)
    model.set_normalization(mean, std)

    weights_all = class_weights(categories, y, args)
    x_fit = torch.from_numpy(flat[fit_idx])
    y_fit = torch.from_numpy(y[fit_idx]).unsqueeze(1)
    w_fit = torch.from_numpy(weights_all[fit_idx]).unsqueeze(1)
    x_val = torch.from_numpy(flat[val_idx])
    y_val = y[val_idx]
    val_categories = [categories[i] for i in val_idx]
    val_near = np.array([c == "near_phrase" for c in val_categories])

    # Wake words are asymmetric: a missed wake word is an annoyance, a false
    # fire wakes the agent while its owner is talking to someone else.
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    loss_fn = nn.BCELoss(reduction="none")

    best = {"score": float("inf"), "state": None, "epoch": -1}
    history: list[dict] = []
    order = np.arange(len(fit_idx))
    rng = np.random.default_rng(args.seed)

    for epoch in range(args.epochs):
        model.train()
        rng.shuffle(order)
        total = 0.0
        for start in range(0, len(order), args.batch_size):
            batch = order[start : start + args.batch_size]
            xb = x_fit[batch]
            yb = y_fit[batch]
            optimizer.zero_grad()
            pred = model(xb)
            per = loss_fn(pred, yb)
            loss = (per * w_fit[batch]).mean()
            loss.backward()
            optimizer.step()
            total += float(loss) * len(batch)

        model.eval()
        with torch.no_grad():
            val_scores = model(x_val).squeeze(1).numpy()
        # Every epoch is compared at the SAME false-reject rate, by moving the
        # threshold rather than by hoping the raw sigmoid landed well. Ranking
        # epochs at a fixed 0.6 compares points at different places on each
        # epoch's curve, which is how a first attempt at this picked an epoch
        # with 9.5% false rejects: the constraint it was supposed to enforce
        # was met by no epoch at all, so it silently degenerated into the
        # weighted sum it was meant to replace.
        calibrated = threshold_for_false_reject(val_scores, y_val, args.max_false_reject)
        far, frr = rates(val_scores, y_val, calibrated)
        near_far = (
            float((val_scores[val_near] >= calibrated).mean()) if val_near.any() else 0.0
        )
        score = near_far * 2.0 + far
        history.append(
            {
                "epoch": epoch,
                "calibrated_threshold": calibrated,
                "train_loss": total / len(order),
                "val_false_accept": far,
                "val_false_accept_near_phrase": near_far,
                "val_false_reject": frr,
                "selection_score": score,
            }
        )
        marker = ""
        if score < best["score"]:
            best = {
                "score": score,
                "state": {k: v.detach().clone() for k, v in model.state_dict().items()},
                "epoch": epoch,
                "threshold": calibrated,
            }
            marker = "  <- best"
        print(
            f"  epoch {epoch:3d}  loss {total / len(order):.5f}  t={calibrated:.4f}  "
            f"val FA {far * 100:6.3f}%  near {near_far * 100:6.3f}%  "
            f"val FR {frr * 100:6.3f}%{marker}"
        )

    if best["state"] is not None:
        model.load_state_dict(best["state"])
    model.eval()
    summary = {
        "selected_epoch": best["epoch"],
        "calibrated_threshold": best.get("threshold", OPERATING_THRESHOLD),
        "calibration_shift": calibration_shift(best.get("threshold", OPERATING_THRESHOLD)),
        "validation_windows": int(len(val_idx)),
        "fit_windows": int(len(fit_idx)),
        "history": history,
    }
    return model, summary


def export_onnx(export: ExportNet, path: Path) -> None:
    """Static 1x16x96 in, 1x1 out — the shapes openWakeWord reads back."""
    dummy = torch.zeros(1, FEATURE_FRAMES, EMBEDDING_DIM)
    torch.onnx.export(
        export,
        (dummy,),
        str(path),
        input_names=["input"],
        output_names=["output"],
        opset_version=15,
        dynamo=False,
    )


def export_tflite(export: ExportNet, path: Path) -> None:
    """The same weights through Keras, converted with no quantization.

    Quantization is deliberately off: the two backends must agree numerically,
    and an int8 tflite file would not match the float ONNX one.
    """
    import tensorflow as tf  # noqa: PLC0415

    layers: list[tf.keras.layers.Layer] = [
        tf.keras.layers.Input(shape=(FEATURE_FRAMES, EMBEDDING_DIM), batch_size=1),
        tf.keras.layers.Reshape((FLAT,)),
    ]
    for linear in export.hidden:
        layers.append(tf.keras.layers.Dense(linear.out_features, activation="relu"))
    layers.append(tf.keras.layers.Dense(1, activation="sigmoid"))
    model = tf.keras.Sequential(layers)

    dense = [layer for layer in model.layers if isinstance(layer, tf.keras.layers.Dense)]
    for keras_layer, linear in zip(dense, [*export.hidden, export.out]):
        keras_layer.set_weights(
            [
                linear.weight.detach().numpy().T.astype(np.float32),
                linear.bias.detach().numpy().astype(np.float32),
            ]
        )

    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = []
    path.write_bytes(converter.convert())


def check_parity(onnx_path: Path, tflite_path: Path, samples: np.ndarray) -> dict:
    """Score the same windows through both files and compare."""
    import onnxruntime as ort  # noqa: PLC0415
    import tensorflow as tf  # noqa: PLC0415

    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name
    interpreter = tf.lite.Interpreter(model_path=str(tflite_path))
    interpreter.allocate_tensors()
    in_index = interpreter.get_input_details()[0]["index"]
    out_index = interpreter.get_output_details()[0]["index"]

    onnx_scores = []
    tflite_scores = []
    for window in samples:
        batch = window[None, ...].astype(np.float32)
        onnx_scores.append(float(session.run(None, {name: batch})[0][0][0]))
        interpreter.set_tensor(in_index, batch)
        interpreter.invoke()
        tflite_scores.append(float(interpreter.get_tensor(out_index)[0][0]))

    delta = np.abs(np.array(onnx_scores) - np.array(tflite_scores))
    return {
        "windows_compared": int(len(samples)),
        "max_absolute_difference": float(delta.max()),
        "mean_absolute_difference": float(delta.mean()),
        "onnx_input_shape": [int(d) for d in session.get_inputs()[0].shape],
        "onnx_output_shape": [int(d) for d in session.get_outputs()[0].shape],
        "tflite_input_shape": [int(d) for d in interpreter.get_input_details()[0]["shape"]],
        "tflite_output_shape": [int(d) for d in interpreter.get_output_details()[0]["shape"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="artifact directory")
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--dropout", type=float, default=0.2)
    parser.add_argument("--hidden", type=int, nargs="+", default=[256, 128, 64])
    parser.add_argument("--negative-weight", type=float, default=3.0)
    parser.add_argument(
        "--hard-negative-weight",
        type=float,
        default=8.0,
        help="loss weight for deliberate near misses, the dominant error",
    )
    parser.add_argument(
        "--max-false-reject",
        type=float,
        default=0.05,
        help="false-reject rate at which epochs are compared to each other",
    )
    parser.add_argument(
        "--calibrate-operating-point",
        action="store_true",
        help="shift the exported bias so the runtime's 0.6 sits at the "
        "comparison point; off because the trained placement measured better",
    )
    parser.add_argument("--val-fraction", type=float, default=0.08)
    parser.add_argument("--parity-windows", type=int, default=512)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    model, summary = train(args)
    export = ExportNet(model)
    # Bake the calibration into the exported bias, so the artifact the runtime
    # loads has its operating point at the runtime's own default threshold.
    if args.calibrate_operating_point:
        with torch.no_grad():
            export.out.bias += summary["calibration_shift"]
        print(
            f"calibrated at validation threshold {summary['calibrated_threshold']:.4f} "
            f"-> output bias shifted by {summary['calibration_shift']:+.4f}"
        )
    else:
        summary["calibration_shift"] = 0.0
    export.eval()

    onnx_path = args.out / "hey_youtab.onnx"
    tflite_path = args.out / "hey_youtab.tflite"
    export_onnx(export, onnx_path)
    export_tflite(export, tflite_path)

    x_eval = np.load(args.features / "x_eval.npy")
    rng = np.random.default_rng(args.seed)
    picks = rng.choice(len(x_eval), size=min(args.parity_windows, len(x_eval)), replace=False)
    parity = check_parity(onnx_path, tflite_path, x_eval[picks])
    print(
        f"backend parity over {parity['windows_compared']} windows: "
        f"max |onnx - tflite| = {parity['max_absolute_difference']:.3e}"
    )
    if parity["max_absolute_difference"] > 1e-4:
        raise SystemExit(
            "ONNX and tflite exports disagree beyond 1e-4 — refusing to ship "
            "two backends that would behave differently"
        )

    summary["parity"] = parity
    summary["hyperparameters"] = {
        "seed": args.seed,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "learning_rate": args.lr,
        "dropout": args.dropout,
        "hidden": list(args.hidden),
        "negative_weight": args.negative_weight,
        "hard_negative_weight": args.hard_negative_weight,
        "val_fraction": args.val_fraction,
        "max_false_reject": args.max_false_reject,
        "operating_threshold": OPERATING_THRESHOLD,
    }
    summary["parameters"] = int(sum(p.numel() for p in export.parameters()))
    (args.out / "training.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {onnx_path} ({onnx_path.stat().st_size:,} bytes)")
    print(f"wrote {tflite_path} ({tflite_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
