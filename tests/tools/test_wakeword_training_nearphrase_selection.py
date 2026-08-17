"""Target 3 (near-phrase false accepts) must be live on a Round 8 human dataset.

The trainer selects an epoch and an operating point on validation by finding
the lowest threshold that meets every false-accept target. Target 3 is judged
on the deliberate near misses. The Round 8 human builder labels those
``near_phrase_human`` (build_human_dataset), while the synthetic era used the
bare ``near_phrase``. A selection mask keyed on the bare spelling is *empty* on a
human-only dataset, and ``scores[empty].mean()`` is ``nan`` — so ``nan > limit``
is False, the near-phrase constraint never fires, and the model is selected with
target 3 silently disabled and ``val_false_accept_near_phrase`` reported as nan.

These tests drive the production selection functions directly:
``train_model.build_validation_masks`` (what ``train`` builds its masks with)
and ``train_model.threshold_meeting_targets`` (what it selects with).
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np

WAKEWORD = Path(__file__).resolve().parents[2] / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))


def _import_train_model():
    """Import ``train_model`` for its numpy-only selection helpers.

    ``train_model`` imports torch at module scope, and torch is not in the
    qualification environment (the same reason
    ``test_wakeword_training_checkpoint.py`` reads it by AST). The two functions
    under test here — ``build_validation_masks`` and
    ``threshold_meeting_targets`` — use only numpy, so a minimal torch stub is
    enough to import the module and exercise the real functions, not a copy of
    them.
    """
    try:  # pragma: no cover - the training env has real torch
        import torch  # noqa: F401

        return importlib.import_module("train_model")
    except ModuleNotFoundError:
        torch_stub = types.ModuleType("torch")
        nn_stub = types.ModuleType("torch.nn")
        nn_stub.Module = object  # only used as a base class at import time
        torch_stub.nn = nn_stub
        sys.modules["torch"] = torch_stub
        sys.modules["torch.nn"] = nn_stub
        return importlib.import_module("train_model")


tm = _import_train_model()


def _args(**over) -> SimpleNamespace:
    base = dict(validation_margin=0.5, max_near_phrase=0.02, max_recorded_per_hour=0.2)
    base.update(over)
    return SimpleNamespace(**base)


def test_the_selection_mask_counts_the_round8_near_phrase_spelling() -> None:
    """``near_phrase_human`` windows are the near-phrase negatives, not an empty set."""
    cats = np.array(
        ["near_phrase_human", "near_phrase_human", "positive_human", "recorded_speech"]
    )
    y = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
    masks = tm.build_validation_masks(cats, y)
    assert int(masks["near_phrase"].sum()) == 2
    # And the synthetic-era spelling is still recognised, so a mixed dataset works.
    mixed = np.array(["near_phrase", "near_phrase_human", "recorded_speech"])
    ymix = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    assert int(tm.build_validation_masks(mixed, ymix)["near_phrase"].sum()) == 2


def test_a_threshold_that_fires_on_round8_near_phrase_is_rejected() -> None:
    """(a) the constraint bites, and (b) the reported near-phrase FAR is real, not nan."""
    scores = np.array(
        [0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.95, 0.95, 0.95, 0.95], dtype=np.float32
    )
    y = np.array([0, 0, 0, 0, 0, 0, 1, 1, 1, 1], dtype=np.float32)
    cats = np.array(["near_phrase_human"] * 6 + ["positive_human"] * 4)
    masks = tm.build_validation_masks(cats, y)
    args = _args()

    threshold, meets = tm.threshold_meeting_targets(
        scores, y, masks, recorded_hours=0.0, args=args
    )

    near_far = float((scores[masks["near_phrase"]] >= threshold).mean())
    assert not np.isnan(near_far), "near-phrase false-accept came back nan"
    # The selected threshold does not admit the near-phrase windows.
    assert near_far <= args.max_near_phrase * args.validation_margin
    assert threshold > 0.9, "a threshold at the near-phrase score band was accepted"


def test_a_dataset_with_no_near_phrase_windows_cannot_pass_target_3() -> None:
    """Untested is not met: an empty near-phrase set refuses certification.

    With no near-phrase windows of either spelling the per-threshold test is
    ``nan > limit`` (False), which would let a threshold pass target 3 without
    ever exercising it. The selector must instead report ``meets=False``.
    """
    scores = np.array([0.1, 0.1, 0.95, 0.95], dtype=np.float32)
    y = np.array([0, 0, 1, 1], dtype=np.float32)
    cats = np.array(
        ["recorded_speech", "recorded_speech", "positive_human", "positive_human"]
    )
    masks = tm.build_validation_masks(cats, y)

    _, meets = tm.threshold_meeting_targets(
        scores, y, masks, recorded_hours=1.0, args=_args()
    )
    assert meets is False
