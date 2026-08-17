"""Training must checkpoint every epoch and resume from where it stopped.

Why this is structural rather than behavioural
----------------------------------------------
``train_model.py`` imports ``torch`` at module scope, and torch is not part of
the dependency set required CI installs. Importing it here would make this file
fail collection in CI, and guarding the import with ``importorskip`` would be a
skip — which the standing rules forbid, and which would quietly stop testing
this the moment the environment changed.

So this asserts the *shape* of the training loop from its AST: that the loop
starts at a resumed epoch, that a checkpoint is written inside the loop body
rather than after it, that the write is atomic, and that both RNG streams are
persisted. The behavioural half — save, kill, resume, identical continuation —
is exercised in the training environment, which has torch, and its evidence is
recorded alongside the run.

Every assertion first proves it located the construct it is judging. An AST
query that silently matches nothing would otherwise pass forever.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAIN = REPO_ROOT / "scripts" / "wakeword" / "train_model.py"

TREE = ast.parse(TRAIN.read_text(encoding="utf-8"))


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    ]


def _epoch_loop() -> ast.For:
    """The `for epoch in range(start_epoch, args.epochs)` loop."""
    for node in ast.walk(TREE):
        if not isinstance(node, ast.For):
            continue
        if not (isinstance(node.target, ast.Name) and node.target.id == "epoch"):
            continue
        call = node.iter
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            if call.func.id == "range":
                return node
    raise AssertionError(
        "no `for epoch in range(...)` loop found in train_model.py — this test "
        "can no longer see the training loop and must be re-anchored, not deleted"
    )


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"train_model.py defines no {name}()")


def test_epoch_loop_starts_from_a_resumed_epoch() -> None:
    """`range(start_epoch, ...)`, not `range(args.epochs)`.

    A loop hardcoded to start at zero cannot resume, however faithfully the
    checkpoint was written.
    """
    loop = _epoch_loop()
    args = loop.iter.args  # type: ignore[attr-defined]
    assert len(args) == 2, (
        "the epoch loop takes a single-argument range(), so it always restarts "
        "at zero and no checkpoint can resume it"
    )
    assert isinstance(args[0], ast.Name) and args[0].id == "start_epoch", (
        f"epoch loop starts at {ast.dump(args[0])}, not a resumed start_epoch"
    )


def test_a_checkpoint_is_written_inside_every_epoch() -> None:
    """Inside the loop body — after the loop would checkpoint once, at the end."""
    loop = _epoch_loop()
    inside = [c for stmt in loop.body for c in _calls_named(stmt, "save_checkpoint")]
    assert inside, (
        "save_checkpoint() is never called inside the epoch loop. A run "
        "interrupted at epoch 55 of 60 would lose every epoch."
    )


def test_checkpoint_write_is_atomic() -> None:
    """Write to a temp path, then replace.

    A checkpoint half-written when the host slept is worse than none: it loads,
    looks plausible, and resumes from corrupt optimizer state.
    """
    fn = _function("save_checkpoint")

    # The staged write: torch.save(...) must target a temporary, not the final
    # path. Read it off the AST so a comment mentioning "tmp" cannot satisfy it.
    saves = [
        call
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "save"
    ]
    assert saves, "save_checkpoint never calls torch.save() — test is vacuous"
    # torch.save(obj, path) — the destination is the SECOND argument, so scan
    # every argument rather than assuming a position.
    staged = [
        call
        for call in saves
        if any(isinstance(a, ast.Name) and a.id == "tmp" for a in call.args)
    ]
    assert staged, (
        "torch.save() writes straight to the destination. An interrupted save "
        "then leaves a truncated checkpoint that still loads."
    )

    # The rename: tmp.replace(path) is what makes it atomic.
    renames = [
        call
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "replace"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "tmp"
    ]
    assert renames, (
        "save_checkpoint stages to a temp file but never calls tmp.replace(...); "
        "the checkpoint is never atomically moved into place"
    )


def test_both_rng_streams_are_persisted() -> None:
    """numpy drives the batch shuffle, torch drives dropout.

    Restoring only the weights resumes a different run: different batch order,
    different masks. The epoch numbering would then describe something that
    never happened.
    """
    fn = _function("save_checkpoint")
    keys = {
        node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for required in ("numpy_rng", "torch_rng", "optimizer", "model", "epoch"):
        assert required in keys, (
            f"save_checkpoint does not persist {required!r}; a resume would not "
            "continue the same run"
        )


def test_resume_can_be_disabled_explicitly() -> None:
    """Resuming across a data or hyperparameter change is wrong.

    It would produce a model that no single configuration describes, so there
    must be a way to refuse the checkpoint.
    """
    source = TRAIN.read_text(encoding="utf-8")
    assert '"--no-resume"' in source, (
        "no --no-resume flag: a stale checkpoint from different data or "
        "hyperparameters would be silently resumed into"
    )


def test_real_human_near_misses_carry_the_hard_negative_weight() -> None:
    """A near miss is a hard negative whether it was synthesised or spoken.

    Round 6 puts speaker E001's deliberate near misses into training under
    their own category, ``near_phrase_human``, so that a false accept can be
    attributed to a real voice instead of averaged in with synthesis. That
    separation has a trap in it: ``class_weights`` used to select hard
    negatives with ``c == "near_phrase"``, an exact match, which would have
    silently handed the real near misses the ordinary ``--negative-weight``
    (3.0) instead of ``--hard-negative-weight`` (6.0) — downweighting the one
    class the whole round exists to fix, while every count and every log line
    still looked right.

    Structural for the reason in the module docstring: torch is not installed
    in required CI, so the weighting is asserted from the source rather than by
    calling it.
    """
    names = {
        node.targets[0].id
        for node in ast.walk(TREE)
        if isinstance(node, ast.Assign)
        and node.targets
        and isinstance(node.targets[0], ast.Name)
    }
    assert "HARD_NEGATIVE_CATEGORIES" in names, (
        "HARD_NEGATIVE_CATEGORIES is gone; hard-negative selection is back to "
        "an inline test that a new near-miss category would not join"
    )

    fn = _function("class_weights")
    used = {
        node.id for node in ast.walk(fn) if isinstance(node, ast.Name)
    }
    assert "HARD_NEGATIVE_CATEGORIES" in used, (
        "class_weights no longer consults HARD_NEGATIVE_CATEGORIES, so the set "
        "can list a category that never receives the hard-negative weight"
    )

    source = TRAIN.read_text(encoding="utf-8")
    for category in ("near_phrase", "near_phrase_human"):
        assert f'"{category}"' in source, (
            f"{category!r} is not in the hard-negative set; windows in that "
            "category would train at the ordinary negative weight"
        )


def test_the_resume_path_refuses_a_retired_synthetic_checkpoint() -> None:
    """A guard with no caller reads as protection while enforcing nothing.

    ``build_human_dataset.refuse_synthetic_initialization`` was written, tested
    and never called from production. `--checkpoint <a rejected round's
    checkpoint.pt>` therefore warm-started Round 8 from synthetic weights and
    produced an artifact that looked entirely human-only at the end — the one
    outcome the whole retirement exists to prevent. An adversarial audit found
    it by grepping for callers rather than by reading the guard.

    Structural for the reason in the module docstring: torch is not installed in
    required CI, so this asserts from the AST that the refusal is invoked on the
    resume path, before the checkpoint is loaded. Order matters: refusing after
    the load would still have read the file.
    """
    fn = _function("train")
    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "refuse_synthetic_initialization"
    ]
    assert calls, (
        "train() never calls refuse_synthetic_initialization, so a retired "
        "synthetic checkpoint can warm-start a human-only round"
    )

    source = TRAIN.read_text(encoding="utf-8")
    guard_at = source.find("refuse_synthetic_initialization(")
    load_at = source.find("load_checkpoint(")
    # Non-vacuity: both anchors must exist, or the ordering claim is vacuous.
    assert guard_at != -1 and load_at != -1, "re-anchor this test"
    resume_load = source.find("load_checkpoint(", guard_at)
    assert resume_load != -1 and guard_at < resume_load, (
        "the refusal does not precede the resume load; a retired checkpoint "
        "would be read before it was judged"
    )
