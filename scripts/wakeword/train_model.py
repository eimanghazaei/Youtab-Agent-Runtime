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
import sys
from pathlib import Path

import numpy as np
import torch
from torch import nn

FEATURE_FRAMES = 16
EMBEDDING_DIM = 96

#: Window categories that carry `--hard-negative-weight` rather than the
#: ordinary `--negative-weight`. Deliberate near misses are the entire error
#: surface this model has, whether they were synthesised or spoken by a person,
#: so both categories belong here. Kept as a set rather than an equality test
#: because a new near-miss source must be a one-line addition, not a silent
#: demotion to the ordinary weight.
HARD_NEGATIVE_CATEGORIES = frozenset({"near_phrase", "near_phrase_human"})

#: The runtime's default `wake_word.sensitivity`. Scores are compared to this
#: raw threshold, so it is the operating point the model is selected at rather
#: than an afterthought applied to a finished model.
OPERATING_THRESHOLD = 0.6


class WakeWordNet(nn.Module):
    """Temporal convolutions over the frame sequence, then a small head.

    The first version flattened all sixteen frames into one 1,536-wide vector
    and learned a dense map from it. That throws away the one thing the input
    has structure in -- time -- and spends 951k parameters rediscovering it
    from 144k windows, which it did by memorising: training loss reached 0.037
    while held-out near-miss confusion sat at 8.7%.

    Convolving along the frame axis gives the same evidence a fifth of the
    parameters and the right inductive bias. Each kernel sees a five-frame
    (400 ms) span of all 96 embedding dimensions and slides, so "a /t/ closure
    followed by an /ae/" is one feature wherever it lands rather than sixteen
    separately-learned ones. That is exactly the distinction the measured
    failures turned on -- `hey you tap` against `hey youtab` is a voicing
    feature in one 400 ms span.
    """

    def __init__(self, channels: tuple[int, ...] = (128, 128, 64), dropout: float = 0.15):
        super().__init__()
        # Per-embedding-dimension, shared across frames: a convolution slides
        # over time, so a per-(frame, dimension) scale would fight it.
        self.register_buffer("mean", torch.zeros(EMBEDDING_DIM))
        self.register_buffer("std", torch.ones(EMBEDDING_DIM))
        kernels = (5, 5, 3)
        sizes = (EMBEDDING_DIM, *channels)
        self.conv = nn.ModuleList(
            nn.Conv1d(sizes[i], sizes[i + 1], kernels[i]) for i in range(len(channels))
        )
        self.dropout = nn.Dropout(dropout)
        remaining = FEATURE_FRAMES
        for k in kernels[: len(channels)]:
            remaining -= k - 1
        self.head = nn.Linear(channels[-1] * remaining, 64)
        self.out = nn.Linear(64, 1)

    def set_normalization(self, mean: np.ndarray, std: np.ndarray) -> None:
        self.mean.copy_(torch.from_numpy(mean.astype(np.float32)))
        self.std.copy_(torch.from_numpy(std.astype(np.float32)))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # (N, frames, 96) -> (N, 96, frames): Conv1d convolves the last axis.
        h = ((x - self.mean) / self.std).transpose(1, 2)
        for layer in self.conv:
            h = self.dropout(torch.relu(layer(h)))
        h = torch.relu(self.head(h.reshape(h.shape[0], -1)))
        return torch.sigmoid(self.out(h))


class ExportNet(nn.Module):
    """The same function with normalization folded into the first convolution.

    ``y = sum_ck w[o,c,k] * (x[c] - m[c]) / s[c] + b[o]`` is identically
    ``sum_ck (w[o,c,k]/s[c]) * x[c] + (b[o] - sum_ck (w[o,c,k]/s[c]) * m[c])``,
    so the standardization disappears into the weights and both exported
    graphs are plain convolutions with nothing backend-specific to misconvert.
    Exact, not approximate -- and the parity check scores it anyway.
    """

    def __init__(self, source: WakeWordNet):
        super().__init__()
        self.conv = nn.ModuleList(
            nn.Conv1d(layer.in_channels, layer.out_channels, layer.kernel_size[0])
            for layer in source.conv
        )
        self.head = nn.Linear(source.head.in_features, source.head.out_features)
        self.out = nn.Linear(source.out.in_features, source.out.out_features)
        with torch.no_grad():
            mean = source.mean.detach().clone()
            std = source.std.detach().clone()
            first = source.conv[0]
            scaled = first.weight / std.view(1, -1, 1)
            self.conv[0].weight.copy_(scaled)
            self.conv[0].bias.copy_(first.bias - (scaled * mean.view(1, -1, 1)).sum((1, 2)))
            for dst, src in zip(self.conv[1:], source.conv[1:]):
                dst.weight.copy_(src.weight)
                dst.bias.copy_(src.bias)
            for dst, src in ((self.head, source.head), (self.out, source.out)):
                dst.weight.copy_(src.weight)
                dst.bias.copy_(src.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = x.transpose(1, 2)
        for layer in self.conv:
            h = torch.relu(layer(h))
        h = torch.relu(self.head(h.reshape(h.shape[0], -1)))
        return torch.sigmoid(self.out(h))


#: Window length, mirrored from build_dataset, for per-hour rates.
WINDOW_SECONDS = 2.0


def build_validation_masks(val_categories: np.ndarray, y_val: np.ndarray) -> dict:
    """The per-target validation selection masks, over the negatives only.

    The near-phrase mask is built from ``HARD_NEGATIVE_CATEGORIES`` — the same
    frozenset the loss weighting reads in ``class_weights`` — rather than the
    literal ``"near_phrase"``. The Round 8 human builder emits its deliberate
    near misses under ``near_phrase_human`` (``build_human_dataset``'s category
    table and its ``near_phrase -> near_phrase_human`` alias), so a mask that
    recognised only the synthetic-era spelling would be empty on every
    human-only dataset and silently disable target 3 during epoch and threshold
    selection: ``scores[empty].mean()`` is ``nan``, ``nan > limit`` is False,
    and the near-phrase constraint never fires. One definition of "hard
    negative", reused, so the selection mask and the loss weight can never
    disagree about what one is.
    """
    masks = {
        name: (val_categories == name) & (y_val == 0)
        for name in ("recorded_speech", "background_only", "common_speech")
    }
    masks["near_phrase"] = np.isin(
        val_categories, sorted(HARD_NEGATIVE_CATEGORIES)
    ) & (y_val == 0)
    return masks


def threshold_meeting_targets(
    scores: np.ndarray,
    labels: np.ndarray,
    masks: dict,
    recorded_hours: float,
    args,
) -> tuple[float, bool]:
    """Lowest threshold meeting every false-accept target, and whether it does.

    Lowest, because raising the threshold only ever costs missed wake words:
    among the thresholds that satisfy the targets, the smallest is the one that
    rejects fewest real utterances. Candidates are the observed negative scores
    themselves, so the search is exact rather than a grid.
    """
    # Targets are tightened by `--validation-margin` before the search.
    #
    # Not pessimism for its own sake. At 0.2 activations per hour over eight
    # hours of validation speech the expected count is under two, so the point
    # estimate carries about its own size in uncertainty, and a threshold
    # picked to sit exactly on the target is as likely to land above it as
    # below on any other sample of speakers. Requiring half the target on
    # validation costs false rejects and buys the margin that makes the
    # measured number mean something.
    margin = args.validation_margin
    negatives = scores[labels == 0]
    candidates = np.unique(np.concatenate([negatives, [0.0, 1.0 + 1e-6]]))
    # Target 3 (near-phrase false accepts) can only be *met* if there are
    # near-phrase windows to meet it on. An empty mask makes the per-threshold
    # test below `nan > x`, which is False, so every candidate would silently
    # "pass" a constraint that was never exercised. Untested is not met: refuse
    # to certify any threshold rather than report a vacuous pass. This is also
    # the backstop for a dataset whose near misses were labelled under a spelling
    # `build_validation_masks` does not recognise — that must fail loudly here,
    # not disappear.
    if not masks["near_phrase"].any():
        return float(candidates[-1]), False
    for threshold in candidates:
        if masks["background_only"].any() and (scores[masks["background_only"]] >= threshold).any():
            continue
        if (scores[masks["near_phrase"]] >= threshold).mean() > args.max_near_phrase * margin:
            continue
        if recorded_hours > 0:
            per_hour = (
                float((scores[masks["recorded_speech"]] >= threshold).sum()) / recorded_hours
            )
            if per_hour > args.max_recorded_per_hour * margin:
                continue
        return float(threshold), True
    return float(candidates[-1]), False


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
    # Real-human near misses are hard negatives too. They arrive under their own
    # category so a false accept can be attributed to a real voice rather than
    # averaged in with synthesis -- but an exact match on "near_phrase" would
    # then quietly demote them to the ordinary negative weight, which is the
    # opposite of why they were recorded.
    near = np.array([c in HARD_NEGATIVE_CATEGORIES for c in categories])
    weights[near & (labels < 0.5)] = args.hard_negative_weight
    return weights


def train(args: argparse.Namespace) -> tuple[WakeWordNet, dict]:
    torch.manual_seed(args.seed)
    x = np.load(args.features / "x_train.npy")
    y = np.load(args.features / "y_train.npy").astype(np.float32)
    categories = json.loads(
        (args.features / "categories_train.json").read_text(encoding="utf-8")
    )
    # Validation is its own split, built from its own speakers, its own
    # recorded-speech partition and its own room tone -- not a slice of the
    # training windows. Selecting an epoch and an operating point on voices
    # the model has already heard is how a model looks better than it is.
    x_val_raw = np.load(args.features / "x_validation.npy")
    y_val = np.load(args.features / "y_validation.npy").astype(np.float32)
    val_categories = np.array(
        json.loads((args.features / "categories_validation.json").read_text(encoding="utf-8"))
    )

    mean = x.reshape(-1, EMBEDDING_DIM).mean(axis=0)
    std = x.reshape(-1, EMBEDDING_DIM).std(axis=0)
    std[std < 1e-6] = 1.0

    model = WakeWordNet(channels=tuple(args.channels), dropout=args.dropout)
    model.set_normalization(mean, std)

    weights_all = class_weights(categories, y, args)
    x_fit = torch.from_numpy(x)
    y_fit = torch.from_numpy(y).unsqueeze(1)
    w_fit = torch.from_numpy(weights_all).unsqueeze(1)
    x_val = torch.from_numpy(x_val_raw)
    val_masks = build_validation_masks(val_categories, y_val)
    val_positive = y_val == 1
    recorded_hours = float(val_masks["recorded_speech"].sum()) * WINDOW_SECONDS / 3600.0

    # Wake words are asymmetric: a missed wake word is an annoyance, a false
    # fire wakes the agent while its owner is talking to someone else.
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = (
        torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
        if args.lr_schedule == "cosine"
        else None
    )
    loss_fn = nn.BCELoss(reduction="none")

    best = {"score": float("inf"), "state": None, "epoch": -1}
    history: list[dict] = []
    order = np.arange(len(y))
    rng = np.random.default_rng(args.seed)

    # Resume, if a checkpoint from this run is on disk. Training here is tens of
    # minutes to hours on CPU, and the host is not guaranteed to stay awake for
    # it; losing epoch 55 of 60 to a sleeping laptop and restarting from zero is
    # not an acceptable failure mode.
    start_epoch = 0
    checkpoint = args.checkpoint or (args.out / "checkpoint.pt")
    if checkpoint.exists() and not args.no_resume:
        # Refuse a retired synthetic checkpoint before loading it.
        #
        # `build_human_dataset.refuse_synthetic_initialization` existed, was
        # tested, and had no production caller — so `--checkpoint <a rejected
        # round's checkpoint.pt>` was a synthetic warm start that produced an
        # artifact looking entirely human-only at the end. A guard nothing calls
        # reads as protection while enforcing nothing, which is worse than no
        # guard, because it stops anyone looking again.
        #
        # Imported here rather than at module scope: this module is the synthetic
        # era's trainer and is still used to reproduce historical rounds, so it
        # must not fail to import when the human-only stage's dependencies are
        # absent. A missing guard is a hard error at the point of use instead.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import build_human_dataset as human_only

        human_only.refuse_synthetic_initialization(checkpoint)
        start_epoch, best, history = load_checkpoint(
            checkpoint, model=model, optimizer=optimizer, scheduler=scheduler, rng=rng
        )
        if start_epoch >= args.epochs:
            print(f"  checkpoint already at epoch {start_epoch - 1}; nothing to train")
        else:
            print(
                f"  resuming from {checkpoint} at epoch {start_epoch} "
                f"(best so far: epoch {best['epoch']}, score {best['score']:.5f})"
            )

    for epoch in range(start_epoch, args.epochs):
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
            if args.focal_gamma > 0:
                # Focal weighting. What decides this model is the tail: the few
                # windows of ordinary recorded speech that score highest are
                # the ones that set the false-accept rate, and they are a
                # vanishing share of the loss once the easy millions are
                # learned. Scaling each example by (1 - p_correct)^gamma keeps
                # the gradient on the ones still being got wrong instead of
                # spending it re-confirming the easy ones.
                p_t = torch.where(yb > 0.5, pred, 1.0 - pred)
                per = per * (1.0 - p_t).clamp(min=0.0) ** args.focal_gamma
            loss = (per * w_fit[batch]).mean()
            loss.backward()
            optimizer.step()
            total += float(loss) * len(batch)
        if scheduler is not None:
            scheduler.step()

        model.eval()
        with torch.no_grad():
            val_scores = model(x_val).squeeze(1).numpy()
        # Both the epoch and the threshold are chosen here, on validation,
        # against the shipping targets rather than a weighted sum of them. For
        # each epoch: find the LOWEST threshold at which every false-accept
        # target is met, which is the one that costs the fewest missed wake
        # words; the epoch's score is the false-reject rate it buys. An epoch
        # where no threshold satisfies the targets cannot be selected at all.
        calibrated, meets = threshold_meeting_targets(
            val_scores, y_val, val_masks, recorded_hours, args
        )
        frr = float((val_scores[val_positive] < calibrated).mean())
        near_far = float((val_scores[val_masks["near_phrase"]] >= calibrated).mean())
        recorded_ph = (
            float((val_scores[val_masks["recorded_speech"]] >= calibrated).sum())
            / recorded_hours
        )
        far = float((val_scores[y_val == 0] >= calibrated).mean())
        score = frr if meets else 1000.0 + frr
        history.append(
            {
                "epoch": epoch,
                "calibrated_threshold": calibrated,
                "meets_targets": bool(meets),
                "train_loss": total / len(order),
                "val_false_accept": far,
                "val_false_accept_near_phrase": near_far,
                "val_recorded_speech_per_hour": recorded_ph,
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
            f"{'ok ' if meets else 'MISS'}  near {near_far * 100:5.2f}%  "
            f"rec {recorded_ph:5.2f}/h  FR {frr * 100:6.3f}%{marker}",
            flush=True,
        )
        save_checkpoint(
            checkpoint,
            epoch=epoch,
            model=model,
            optimizer=optimizer,
            scheduler=scheduler,
            rng=rng,
            best=best,
            history=history,
        )

    if best["state"] is not None:
        model.load_state_dict(best["state"])
    model.eval()
    summary = {
        "selected_epoch": best["epoch"],
        "targets": {
            "max_false_reject": args.max_false_reject,
            "max_near_phrase_false_accept": args.max_near_phrase,
            "max_recorded_per_hour": args.max_recorded_per_hour,
            "validation_margin": args.validation_margin,
        },
        "calibrated_threshold": best.get("threshold", OPERATING_THRESHOLD),
        "calibration_shift": calibration_shift(best.get("threshold", OPERATING_THRESHOLD)),
        "validation_windows": int(len(y_val)),
        "fit_windows": int(len(y)),
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
    ]
    # Keras Conv1D is (batch, steps, channels) and torch Conv1d is
    # (batch, channels, steps), so the Keras graph needs no transpose and the
    # kernels are laid out (k, in, out) against torch's (out, in, k).
    for conv in export.conv:
        layers.append(
            tf.keras.layers.Conv1D(conv.out_channels, conv.kernel_size[0], activation="relu")
        )
    layers.append(tf.keras.layers.Flatten())
    layers.append(tf.keras.layers.Dense(export.head.out_features, activation="relu"))
    layers.append(tf.keras.layers.Dense(1, activation="sigmoid"))
    model = tf.keras.Sequential(layers)

    keras_convs = [ly for ly in model.layers if isinstance(ly, tf.keras.layers.Conv1D)]
    for keras_layer, conv in zip(keras_convs, export.conv):
        keras_layer.set_weights(
            [
                conv.weight.detach().numpy().transpose(2, 1, 0).astype(np.float32),
                conv.bias.detach().numpy().astype(np.float32),
            ]
        )
    # The head sees a flattened convolution stack, and the two frameworks
    # flatten it in different orders: torch holds (channels, steps) and
    # flattens channel-major, Keras holds (steps, channels) and flattens
    # step-major. Feeding the same matrix to both wires every unit to the
    # wrong feature -- which is not subtle (the parity check measured 1.0) but
    # is completely invisible without one. Reindex instead of transposing the
    # graph, so neither exported file carries an extra op.
    channels = export.conv[-1].out_channels
    steps = export.head.in_features // channels
    head_weight = export.head.weight.detach().numpy()
    head_weight = (
        head_weight.reshape(-1, channels, steps).transpose(0, 2, 1).reshape(-1, steps * channels)
    )

    dense = [ly for ly in model.layers if isinstance(ly, tf.keras.layers.Dense)]
    dense[0].set_weights(
        [head_weight.T.astype(np.float32), export.head.bias.detach().numpy().astype(np.float32)]
    )
    dense[1].set_weights(
        [
            export.out.weight.detach().numpy().T.astype(np.float32),
            export.out.bias.detach().numpy().astype(np.float32),
        ]
    )

    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = []
    path.write_bytes(converter.convert())


def save_checkpoint(
    path: Path,
    *,
    epoch: int,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    scheduler,
    rng: np.random.Generator,
    best: dict,
    history: list[dict],
) -> None:
    """Persist everything needed to continue this exact run after an epoch.

    Written atomically. A checkpoint half-flushed when the machine slept is
    worse than none at all: it loads, looks plausible, and silently continues
    from corrupt optimizer state.

    Both RNG streams are saved. Restoring only the weights would resume a
    *different* run -- numpy drives the batch shuffle and torch drives dropout,
    so an unrestored resume changes which examples pair with which mask and the
    epoch numbering stops describing what actually happened.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "epoch": epoch,
        "model": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "scheduler": scheduler.state_dict() if scheduler is not None else None,
        "numpy_rng": rng.bit_generator.state,
        "torch_rng": torch.get_rng_state(),
        "best": best,
        "history": history,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    torch.save(payload, tmp)
    tmp.replace(path)


def load_checkpoint(
    path: Path,
    *,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    scheduler,
    rng: np.random.Generator,
) -> tuple[int, dict, list[dict]]:
    """Restore a run. Returns (next epoch to run, best, history)."""
    payload = torch.load(path, weights_only=False)
    model.load_state_dict(payload["model"])
    optimizer.load_state_dict(payload["optimizer"])
    if scheduler is not None and payload.get("scheduler") is not None:
        scheduler.load_state_dict(payload["scheduler"])
    rng.bit_generator.state = payload["numpy_rng"]
    torch.set_rng_state(payload["torch_rng"])
    return payload["epoch"] + 1, payload["best"], payload["history"]


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
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help=(
            "per-epoch checkpoint file (default: <out>/checkpoint.pt). Written "
            "atomically after every epoch and resumed from automatically."
        ),
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help=(
            "ignore an existing checkpoint and train from scratch. Use when the "
            "data or hyperparameters changed — resuming across a change would "
            "produce a model no single configuration describes."
        ),
    )
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--lr", type=float, default=1e-3)
    # Both of these default to the behaviour that produced the shipped model.
    # A new knob that changes what an existing command does would quietly make
    # run_pipeline.sh reproduce something other than the artifact it documents.
    parser.add_argument(
        "--focal-gamma",
        type=float,
        default=0.0,
        help="focal-loss exponent; 0 (the default) gives plain weighted BCE",
    )
    parser.add_argument(
        "--lr-schedule",
        choices=("constant", "cosine"),
        default="constant",
        help="cosine decays the learning rate to zero across --epochs",
    )
    parser.add_argument("--dropout", type=float, default=0.2)
    parser.add_argument("--channels", type=int, nargs="+", default=[128, 128, 64])
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
        help="shipping target: missed wake words, recorded for the card",
    )
    parser.add_argument(
        "--max-near-phrase",
        type=float,
        default=0.02,
        help="shipping target: activation on deliberate near misses",
    )
    parser.add_argument(
        "--max-recorded-per-hour",
        type=float,
        default=0.2,
        help="shipping target: activations per hour of recorded human speech",
    )
    parser.add_argument(
        "--validation-margin",
        type=float,
        default=0.5,
        help="fraction of each false-accept target the threshold must meet on "
        "validation, so a noisy small-count estimate is not shipped as exact",
    )
    parser.add_argument(
        "--calibrate-operating-point",
        action="store_true",
        help="shift the exported bias so the runtime's 0.6 sits at the "
        "comparison point; off because the trained placement measured better",
    )
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
        "focal_gamma": args.focal_gamma,
        "lr_schedule": args.lr_schedule,
        "dropout": args.dropout,
        "channels": list(args.channels),
        "negative_weight": args.negative_weight,
        "hard_negative_weight": args.hard_negative_weight,
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
