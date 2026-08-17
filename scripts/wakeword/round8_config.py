#!/usr/bin/env python3
"""Load ``round8_config.json`` and prove it is complete and self-consistent.

Why this exists
---------------
``ROUND8_DESIGN.md`` is the argument; ``round8_config.json`` is what a pipeline
would read. A predeclaration is only worth something if the two cannot drift
apart and if the numbers in it are checkable arithmetic rather than assertions,
so every derived figure in the config is recomputed here from its own stated
inputs:

* parameter counts, from the architecture and the channel widths;
* the loss weights, from the derivation rule and the r6c1 anchor;
* the window projection, from the utterance counts and the offset grid;
* the clean-run bounds, from the sample sizes;
* the offset grid itself, from ``build_dataset.PHRASE_END_JITTER`` and the
  runtime's frame length;
* the background pool, from ``build_dataset``'s own noise constants, so
  "generated noise is excluded" is a fact about the builder rather than a
  sentence in the config.

The last two are the reason this reads the pipeline's own source with ``ast``
rather than importing it: a check that restates a constant proves nothing, and
importing ``build_dataset`` would pull in numpy for a job that is arithmetic on
integers.

Two bounds here are deliberately *exact* rather than generous. The capacity set
is three arms — not two, not four — because ``ROUND8_DESIGN.md``'s stop condition
closes the matrix at both ends, and a range would let a pre-run config edit widen
the experiment without an Owner decision. And no split may be handed generated
background, because a rule with no guard is how ``VALIDATION_NOISE`` went on
naming ``pink_noise.wav`` through seven rounds.

The other half of the job is the *state* of the hashes. A predeclared config
must have every hash field empty — inventing one in advance is exactly the
failure this document exists to prevent — and an executable one must have them
all filled. A half-filled config is the dangerous state, because it looks frozen
and is not, so it is a failure here rather than a warning.

Run it with::

    python scripts/wakeword/round8_config.py --check

Exit status is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CONFIG_PATH = HERE / "round8_config.json"
DESIGN_PATH = HERE / "ROUND8_DESIGN.md"
BUILD_DATASET = HERE / "build_dataset.py"
#: The *active* Round 8 builder. ``check`` derives the window geometry from
#: ``BUILD_DATASET`` (the retired synthetic builder); this one is what actually
#: emits the Round 8 tensors, and ``builder_windowing_divergence`` reads it to
#: catch the predeclaration describing a dataset the builder cannot produce.
BUILD_HUMAN_DATASET = HERE / "build_human_dataset.py"
RUNTIME = REPO_ROOT / "tools" / "wake_word.py"

#: The five acceptance targets, by id, exactly as ``ROUND6_DESIGN.md`` fixed
#: them. Hard-coded rather than read from the config, because "the targets are
#: unchanged" is the claim under test and a config that changed them would
#: otherwise validate itself.
TARGETS: dict[int, tuple[str, float, str]] = {
    1: ("false rejects", 0.05, "<="),
    2: ("recorded-human-speech false activations", 0.2, "<="),
    3: ("deliberate near-miss false accepts", 0.02, "<="),
    4: ("background-only false accepts", 0, "=="),
    5: ("ONNX/TFLite numerical and detection parity", 0, "=="),
}

#: Prohibitions the Owner decision names. A config that quietly dropped one
#: would still parse, so the list is asserted rather than trusted.
REQUIRED_PROHIBITIONS: tuple[str, ...] = (
    "tts",
    "libritts",
    "vctk",
    "voice conversion",
    "synthetic positives",
    "generated background noise",
    "generated impulse responses",
    "pitch shift",
    "time stretch",
    "speed augmentation",
    "gain augmentation",
    "synthetic candidate as an initialization",
)

#: Sections a reader of the brief is entitled to find. Absence is a failure, not
#: a default.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "policy",
    "splits",
    "manifests",
    "recorded_negative_corpora",
    "background_recordings",
    "window_construction",
    "dataset_projection",
    "architecture",
    "variation",
    "initialization",
    "loss",
    "optimizer",
    "schedule",
    "selection",
    "targets",
    "runtime_contract",
    "export_and_parity",
    "validation_gate",
    "statistical_power",
    "sealed_opening_rule",
    "if_validation_fails",
    "prerequisite_code_changes",
    "grounding_evidence",
)

#: The predeclared capacity set: three widths, downward. Hard-coded for the same
#: reason as TARGETS — "Round 8 runs exactly these three arms" is the claim under
#: test, and a config that added a fourth would otherwise validate itself.
#:
#: The set is *exact*, not an upper limit. ``ROUND8_DESIGN.md``'s stop condition
#: forbids adding a width; dropping one is the same edit to the same predeclared
#: matrix, and it would silently turn the capacity–accuracy relation into two
#: points and a line through them. Both are refused here.
ARMS: tuple[tuple[int, ...], ...] = ((128, 128, 64), (32, 32, 16), (8, 8, 4))

#: The clause a refusal quotes, so the failure names the decision it enforces
#: rather than a count nobody can trace to a document.
STOP_CONDITION = "do not add a fourth width, do not widen the set"

#: Immutable split membership. Also hard-coded, for the same reason as TARGETS.
SPLITS: dict[str, tuple[str, ...]] = {
    "train": ("E001", "E003", "E004"),
    "validation": ("E005",),
    "sealed": ("E002", "E006", "E007"),
}

#: Every field that must be empty in a predeclared config and filled in an
#: executable one, as ``(section path, field)``.
HASH_FIELDS: tuple[tuple[str, ...], ...] = (
    ("initialization", "epoch0_checkpoint_sha256"),
)


# ── arithmetic the config asserts, recomputed ────────────────────────────────


def parameter_count(
    channels: list[int] | tuple[int, ...],
    *,
    embedding_dim: int = 96,
    frames: int = 16,
    kernels: tuple[int, ...] = (5, 5, 3),
    head_units: int = 64,
) -> int:
    """Parameters in ``WakeWordNet`` for one channel triple.

    ``conv1 embedding->c1``, ``conv2 c1->c2``, ``conv3 c2->c3``, then
    ``head (c3 x remaining)->head_units`` and ``out head_units->1``. Reproduces
    r6c1's recorded 192,961 for (128, 128, 64), which is the check that the
    formula is the same one the trainer implements.
    """
    sizes = (embedding_dim, *channels)
    total = 0
    remaining = frames
    for index, out_channels in enumerate(channels):
        total += sizes[index] * out_channels * kernels[index] + out_channels
        remaining -= kernels[index] - 1
    total += channels[-1] * remaining * head_units + head_units
    total += head_units * 1 + 1
    return total


def positive_loss_mass_share(
    positives: int,
    hard_negatives: int,
    other_negatives: int,
    *,
    negative_weight: float,
    hard_negative_weight: float,
) -> float:
    """Share of total weighted loss mass carried by the positive class."""
    mass = (
        positives
        + hard_negatives * hard_negative_weight
        + other_negatives * negative_weight
    )
    return positives / mass


def derived_negative_weight(
    positives: int,
    hard_negatives: int,
    other_negatives: int,
    *,
    anchor_share: float,
    hard_multiple: float = 2.0,
) -> float:
    """The ``w`` that makes the positive class carry ``anchor_share`` of the loss.

    Solves ``P / (P + hard_multiple*w*H + w*N) = anchor_share`` for ``w``. The
    rule, not the number, is what the design predeclares; this is how the number
    is obtained from it.
    """
    target_mass = positives / anchor_share
    denominator = hard_multiple * hard_negatives + other_negatives
    if denominator <= 0:
        raise ValueError("no negatives to weight")
    return (target_mass - positives) / denominator


def clean_run_upper_bound(n: int, alpha: float = 0.05) -> float:
    """Exact one-sided 95% upper bound on a rate after zero events in ``n``.

    Clopper-Pearson with ``k = 0`` collapses to ``1 - alpha**(1/n)``, so this
    needs no special functions. The rule of three, ``3/n``, is its first-order
    approximation and is the form the recording package reasons in.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    return 1.0 - alpha ** (1.0 / n)


def hours_for_poisson_bound(rate_per_hour: float, alpha: float = 0.05) -> float:
    """Hours needed for zero events to bound ``rate_per_hour`` at 95%.

    ``P(0 | lambda) = exp(-rate*hours) <= alpha``.
    """
    import math

    return -math.log(alpha) / rate_per_hour


# ── reading the pipeline's own constants ─────────────────────────────────────


def _literal(node: ast.AST) -> object:
    """``ast.literal_eval``, extended to the one call these constants use.

    ``frozenset({...})`` is a call, so ``literal_eval`` refuses it — and reading
    the pipeline's constants *without importing the pipeline* is the whole point
    of doing this with ``ast``. Only a single positional literal argument is
    unwrapped; anything else is still refused, because a constant this cannot
    read must fail loudly rather than be silently approximated.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set"}
        and len(node.args) == 1
        and not node.keywords
    ):
        return frozenset(ast.literal_eval(node.args[0]))
    return ast.literal_eval(node)


def _module_constants(path: Path, names: set[str]) -> dict[str, object]:
    """Module-level literal assignments, without importing the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in names:
                try:
                    found[target.id] = _literal(node.value)
                except ValueError:
                    pass
    return found


def _class_attribute(path: Path, class_name: str, attribute: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                targets = []
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    targets = [item.target.id]
                    value = item.value
                elif isinstance(item, ast.Assign):
                    targets = [t.id for t in item.targets if isinstance(t, ast.Name)]
                    value = item.value
                else:
                    continue
                if attribute in targets and value is not None:
                    try:
                        return ast.literal_eval(value)
                    except ValueError:
                        return None
    return None


def offset_grid_from_pipeline() -> list[int]:
    """The frame offsets the documented trailing-context band implies.

    ``PHRASE_END_JITTER`` is a band in seconds and the runtime scores one frame
    at a time, so the offsets are the integer frame counts inside that band. The
    grid is therefore derived from two things already in the code, not chosen.
    """
    consts = _module_constants(BUILD_DATASET, {"PHRASE_END_JITTER", "SAMPLE_RATE"})
    low, high = consts["PHRASE_END_JITTER"]  # type: ignore[misc]
    frame_samples = _class_attribute(RUNTIME, "_OpenWakeWordEngine", "frame_length")
    if frame_samples is None:
        frame_samples = _class_attribute(RUNTIME, "_Engine", "frame_length")
    frame_seconds = float(frame_samples) / float(consts["SAMPLE_RATE"])  # type: ignore[arg-type]
    first = round(low / frame_seconds)
    last = round(high / frame_seconds)
    return list(range(first, last + 1))


def _module_constants_annotated(path: Path, names: set[str]) -> dict[str, object]:
    """Like ``_module_constants`` but also reads annotated (``x: T = ...``) ones.

    The Round 8 builder declares ``TRAILING_OFFSETS_S`` and ``HUMAN_CATEGORIES``
    with type annotations, which ``_module_constants`` skips because it matches
    only a bare ``Assign``. Read here without importing the builder, for the same
    reason ``check`` reads the pipeline with ``ast`` — importing it would pull in
    numpy to inspect a tuple and a dict.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, object] = {}
    for node in tree.body:
        targets: list[str] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        for name in targets:
            if name in names and value is not None:
                try:
                    found[name] = _literal(value)
                except ValueError:
                    pass
    return found


def builder_windowing_divergence(config: dict | None = None) -> list[str]:
    """Where the predeclared windowing and the *real* Round 8 builder disagree.

    ``check`` derives the phrase-anchored offset grid from
    ``build_dataset.PHRASE_END_JITTER`` — the retired synthetic builder — and the
    committed config predeclares seven offsets, projecting ``near_phrase_human``
    as ``utterances x 7`` phrase-anchored windows, with ``loss.negative_weight``
    derived from those counts. This function checks that the builder that will
    actually run, ``build_human_dataset``, produces exactly that: its
    ``TRAILING_OFFSETS_S`` ladder must land on the predeclared offset frames, and
    ``near_phrase_human`` — a label-0 hard negative that carries the wake phrase —
    must be phrase-anchored with the same ladder rather than tiled, so that its
    projected ``utterances x offsets`` count is the count the builder emits.

    V6 raised the builder to this predeclaration: the seven-offset ladder and
    ``PHRASE_ANCHORED_NEGATIVES = {near_phrase_human}`` are what make the two
    agree. This is no longer a standing Owner-deferred divergence — ``check``
    calls this function, so any future drift (a shortened ladder, near-phrase
    reverted to tiling) fails validation instead of being discovered at build
    time. It returns the list of disagreements; an empty list means the
    predeclaration and the builder agree.
    """
    if config is None:
        config = load()
    problems: list[str] = []
    window = config.get("window_construction", {})

    consts = _module_constants_annotated(
        BUILD_HUMAN_DATASET,
        {"TRAILING_OFFSETS_S", "HUMAN_CATEGORIES", "PHRASE_ANCHORED_NEGATIVES"},
    )
    ladder = consts.get("TRAILING_OFFSETS_S")
    if not isinstance(ladder, (tuple, list)) or not ladder:
        problems.append(
            "build_human_dataset defines no readable TRAILING_OFFSETS_S, so the "
            "builder's real trailing-offset ladder cannot be checked against the "
            "predeclaration"
        )
        return problems

    sample_rate = _module_constants(BUILD_DATASET, {"SAMPLE_RATE"}).get("SAMPLE_RATE")
    frame_samples = _class_attribute(RUNTIME, "_OpenWakeWordEngine", "frame_length")
    if frame_samples is None:
        frame_samples = _class_attribute(RUNTIME, "_Engine", "frame_length")
    frame_seconds = float(frame_samples) / float(sample_rate)  # type: ignore[arg-type]
    builder_frames = [round(float(offset) / frame_seconds) for offset in ladder]

    declared_frames = window.get("phrase_anchored_offsets_frames")
    if declared_frames != builder_frames:
        problems.append(
            "window_construction.phrase_anchored_offsets_frames is "
            f"{declared_frames!r}, but build_human_dataset.TRAILING_OFFSETS_S "
            f"{tuple(ladder)!r} lands on {builder_frames!r} at the runtime frame "
            "rate. The predeclaration is derived from build_dataset (the retired "
            "synthetic builder), not the build_human_dataset that will run."
        )
    declared_n = window.get("offsets_per_phrase_anchored_utterance")
    if declared_n != len(ladder):
        problems.append(
            "window_construction.offsets_per_phrase_anchored_utterance is "
            f"{declared_n!r}, but build_human_dataset emits {len(ladder)} offsets "
            "per positive utterance"
        )

    # The label-0 windowing method. ``near_phrase_human`` is a hard NEGATIVE
    # (build_human_dataset.HUMAN_CATEGORIES maps it to 0). The builder phrase-
    # anchors it — with the same trailing-offset ladder as positives — exactly
    # when it is listed in build_human_dataset.PHRASE_ANCHORED_NEGATIVES, which is
    # the one switch its windows_for and emit loop both read. When it is anchored,
    # ``utterances x offsets`` is precisely what the builder emits and the
    # projection must equal it; when it is not, the builder tiles the clip into a
    # variable count set by clip length, so ``utterances x offsets`` is a count
    # the builder never emits for it.
    human_cats = consts.get("HUMAN_CATEGORIES")
    near_label = human_cats.get("near_phrase_human") if isinstance(human_cats, dict) else None
    anchored = consts.get("PHRASE_ANCHORED_NEGATIVES")
    near_is_anchored = (
        isinstance(anchored, (set, frozenset, list, tuple))
        and "near_phrase_human" in anchored
    )
    per = window.get("offsets_per_phrase_anchored_utterance", 0)
    for split_name, block in config.get("dataset_projection", {}).items():
        if not isinstance(block, dict):
            continue
        entry = block.get("near_phrase_human")
        if not isinstance(entry, dict) or "utterances" not in entry:
            continue
        if near_label != 0:
            continue
        expected = entry["utterances"] * per
        if near_is_anchored:
            if entry.get("windows") != expected:
                problems.append(
                    f"dataset_projection.{split_name}.near_phrase_human projects "
                    f"{entry.get('windows')} windows, but build_human_dataset "
                    f"phrase-anchors it (PHRASE_ANCHORED_NEGATIVES) as "
                    f"{entry['utterances']} x {per} offsets = {expected}."
                )
        elif entry.get("windows") == expected:
            problems.append(
                f"dataset_projection.{split_name}.near_phrase_human is projected as "
                f"{entry['utterances']} x {per} phrase-anchored offsets = "
                f"{entry.get('windows')}, but near_phrase_human is a label-0 category "
                "that build_human_dataset TILES into non-overlapping 2 s windows -- a "
                "count set by clip length, not utterances x offsets."
            )
    return problems


# ── the checks ───────────────────────────────────────────────────────────────


def load(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check(config: dict) -> list[str]:
    """Every problem found, as a list of human-readable strings."""
    problems: list[str] = []

    def bad(message: str) -> None:
        problems.append(message)

    # -- shape ---------------------------------------------------------------
    for section in REQUIRED_SECTIONS:
        if section not in config:
            bad(f"missing section: {section!r}")
    if config.get("round") != 8:
        bad(f"round is {config.get('round')!r}, not 8")

    # -- the Owner decision --------------------------------------------------
    policy = config.get("policy", {})
    if policy.get("training_data") != "human-only":
        bad(f"policy.training_data is {policy.get('training_data')!r}, not 'human-only'")
    prohibited = " | ".join(policy.get("prohibited", [])).lower()
    for needle in REQUIRED_PROHIBITIONS:
        if needle not in prohibited:
            bad(f"policy.prohibited does not cover {needle!r}")
    allowed = " | ".join(policy.get("allowed_processing", [])).lower()
    for forbidden_in_allowed in ("pitch", "stretch", "reverb", "noise", "mix"):
        if forbidden_in_allowed in allowed:
            bad(f"policy.allowed_processing mentions {forbidden_in_allowed!r}")

    # -- splits --------------------------------------------------------------
    splits = config.get("splits", {})
    for role, members in SPLITS.items():
        if tuple(splits.get(role, ())) != members:
            bad(f"splits.{role} is {splits.get(role)!r}, not {list(members)!r}")
    seen: set[str] = set()
    for role in SPLITS:
        for member in splits.get(role, ()):
            if member in seen:
                bad(f"{member} appears in more than one split")
            seen.add(member)
    if splits.get("immutable") is not True:
        bad("splits.immutable is not true")

    # -- manifests, and the state of their hashes ----------------------------
    manifests = config.get("manifests", [])
    by_dataset = {entry.get("dataset"): entry for entry in manifests}
    for role, members in SPLITS.items():
        for member in members:
            entry = by_dataset.get(member)
            if entry is None:
                bad(f"no manifest entry for {member}")
                continue
            if entry.get("role") != role:
                bad(f"manifest {member} has role {entry.get('role')!r}, not {role!r}")
            if "manifest_sha256" not in entry:
                bad(f"manifest {member} has no manifest_sha256 field at all")
    expected_usage = {"train": "training", "validation": "validation", "sealed": "sealed-evaluation"}
    for entry in manifests:
        role = entry.get("role")
        if role in expected_usage and entry.get("usage") != expected_usage[role]:
            bad(
                f"manifest {entry.get('dataset')} declares usage "
                f"{entry.get('usage')!r}; role {role!r} requires "
                f"{expected_usage[role]!r}"
            )

    frozen = _hash_state(config)
    if frozen == "mixed":
        bad(
            "some hash fields are filled and some are empty. A half-frozen "
            "config looks frozen and is not; fill all of them or none."
        )
    if frozen == "empty" and config.get("status") != "predeclared-not-executed":
        bad("hashes are empty but status does not say the round is unexecuted")
    if frozen == "filled" and config.get("status") == "predeclared-not-executed":
        bad("hashes are filled but status still says the round is unexecuted")

    # -- architecture and the one varied axis --------------------------------
    architecture = config.get("architecture", {})
    if tuple(architecture.get("kernels", ())) != (5, 5, 3):
        bad(f"architecture.kernels is {architecture.get('kernels')!r}, not [5, 5, 3]")
    if architecture.get("input_shape") != [1, 16, 96]:
        bad(f"architecture.input_shape is {architecture.get('input_shape')!r}")
    if architecture.get("output_shape") != [1, 1]:
        bad(f"architecture.output_shape is {architecture.get('output_shape')!r}")

    variation = config.get("variation", {})
    if variation.get("axis") != "channels":
        bad(f"variation.axis is {variation.get('axis')!r}, not 'channels'")
    arms = variation.get("arms", [])
    widths = [tuple(arm["channels"]) for arm in arms]
    if len(arms) != len(ARMS):
        bad(
            f"variation.arms has {len(arms)} arms; Round 8 predeclares exactly "
            f"{len(ARMS)}. ROUND8_DESIGN.md's stop condition is \"{STOP_CONDITION}\", "
            "so the matrix is closed at both ends: a fourth width and a dropped arm "
            "are the same edit to the same predeclaration, and changing it takes an "
            "Owner decision rather than a config edit."
        )
    if set(widths) != set(ARMS):
        bad(
            f"variation.arms declares the widths {[list(w) for w in widths]!r}; the "
            f"predeclared capacity set is {[list(w) for w in ARMS]!r}. "
            f"\"{STOP_CONDITION}\" bounds which widths run, not only how many."
        )
    if variation.get("runs") != len(arms):
        bad(f"variation.runs is {variation.get('runs')!r} for {len(arms)} arms")
    stop_condition = str(variation.get("stop_condition", ""))
    if not stop_condition.strip():
        bad("variation.stop_condition is empty")
    elif STOP_CONDITION not in stop_condition.lower():
        # The refusals above quote this clause. If the config's own stop
        # condition no longer carries it, they would be quoting a document the
        # config had already walked away from.
        bad(
            f"variation.stop_condition no longer says {STOP_CONDITION!r}, which is "
            "the clause that makes the capacity set exact"
        )
    baseline = None
    for arm in arms:
        recomputed = parameter_count(arm["channels"])
        if recomputed != arm.get("parameters"):
            bad(
                f"arm {arm.get('id')}: config says {arm.get('parameters')} "
                f"parameters, the architecture gives {recomputed}"
            )
        if arm["channels"] == [128, 128, 64]:
            baseline = recomputed
    if baseline != 192961:
        bad(
            "the (128, 128, 64) control is missing or does not reproduce r6c1's "
            f"recorded 192,961 (got {baseline})"
        )
    if len(set(widths)) != len(widths):
        bad("two arms have the same channel widths")
    if sorted(widths, reverse=True) != widths:
        bad("arms are not ordered from widest to narrowest")

    # -- fresh initialization ------------------------------------------------
    init = config.get("initialization", {})
    if init.get("fresh") is not True:
        bad("initialization.fresh is not true")
    if init.get("from_synthetic_checkpoint") is not False:
        bad("initialization.from_synthetic_checkpoint is not false")
    seed = init.get("seed")
    if not isinstance(seed, int):
        bad(f"initialization.seed is {seed!r}, not an integer")
    else:
        for field in ("torch_manual_seed", "numpy_default_rng"):
            if init.get(field) != seed:
                bad(f"initialization.{field} disagrees with initialization.seed")
        if seed == 20260807:
            bad("initialization.seed reuses the synthetic era's seed 20260807")

    # -- window construction, against the pipeline's own constants -----------
    window = config.get("window_construction", {})
    try:
        derived_offsets = offset_grid_from_pipeline()
    except (KeyError, OSError, SyntaxError, TypeError) as error:  # pragma: no cover
        derived_offsets = []
        bad(f"could not derive the offset grid from the pipeline source: {error}")
    if derived_offsets and window.get("phrase_anchored_offsets_frames") != derived_offsets:
        bad(
            "window_construction.phrase_anchored_offsets_frames is "
            f"{window.get('phrase_anchored_offsets_frames')!r}; the documented "
            f"trailing-context band at the runtime frame rate gives {derived_offsets!r}"
        )
    offsets = window.get("phrase_anchored_offsets_frames", [])
    if window.get("offsets_per_phrase_anchored_utterance") != len(offsets):
        bad("window_construction.offsets_per_phrase_anchored_utterance disagrees with the grid")
    if window.get("augmentation") != "none":
        bad(f"window_construction.augmentation is {window.get('augmentation')!r}, not 'none'")
    for field, expected in (
        ("positive_speech_prefix_fraction", 0.0),
        ("reverb_fraction", 0.0),
        ("rir_count", 0),
        ("snr_mixing", False),
        ("gain_augmentation", False),
    ):
        if window.get(field) != expected:
            bad(f"window_construction.{field} is {window.get(field)!r}, not {expected!r}")
    build_consts = _module_constants(
        BUILD_DATASET,
        {"WINDOW_SAMPLES", "SAMPLE_RATE", "FEATURE_FRAMES", "EMBEDDING_DIM"},
    )
    for config_field, const_name in (
        ("window_samples", "WINDOW_SAMPLES"),
        ("sample_rate", "SAMPLE_RATE"),
        ("feature_frames", "FEATURE_FRAMES"),
        ("embedding_dim", "EMBEDDING_DIM"),
    ):
        if window.get(config_field) != build_consts.get(const_name):
            bad(
                f"window_construction.{config_field} is {window.get(config_field)!r}; "
                f"the pipeline's {const_name} is {build_consts.get(const_name)!r}"
            )

    # -- background recordings: generated ones must be out ------------------
    background = config.get("background_recordings", {})
    excluded = set(background.get("generated_and_therefore_excluded", []))
    if not {"pink_noise.wav", "white_noise.wav"} <= excluded:
        bad("background_recordings does not exclude both generated noise files")
    usable = background.get("real_and_usable", {})
    if excluded & set(usable):
        bad("a generated noise file appears in background_recordings.real_and_usable")
    assignment = background.get("assignment_disjoint_by_recording", {})
    assigned: list[str] = []
    for names in assignment.values():
        assigned.extend(names)
    if len(assigned) != len(set(assigned)):
        bad("a background recording is assigned to more than one split")
    if set(assigned) != set(usable):
        bad("the background assignment does not cover exactly the usable recordings")
    total = round(sum(usable.values()), 1)
    if abs(total - background.get("total_real_seconds", -1)) > 0.05:
        bad(
            f"background_recordings.total_real_seconds is "
            f"{background.get('total_real_seconds')!r}, the durations sum to {total}"
        )

    # -- and the builder's own constants, not just the config's prose ---------
    #
    # The config can only say generated noise is excluded. Whether a split
    # actually receives it is decided by three constants in ``build_dataset.py``,
    # so those are read here — a config that documents the rule while the builder
    # still names ``pink_noise.wav`` is exactly the drift this file exists to
    # catch, and it is how the rule stayed broken for seven rounds.
    noise_consts = _module_constants(
        BUILD_DATASET,
        {"GENERATED_BACKGROUND_NAMES", "VALIDATION_NOISE", "EVAL_NOISE"},
    )
    builder_generated = noise_consts.get("GENERATED_BACKGROUND_NAMES")
    if builder_generated is None:
        bad(
            "build_dataset.py defines no GENERATED_BACKGROUND_NAMES, so nothing in "
            "the builder names the generated background files that are excluded"
        )
    elif set(builder_generated) != excluded:
        bad(
            f"build_dataset.GENERATED_BACKGROUND_NAMES is {sorted(builder_generated)!r}; "
            "background_recordings.generated_and_therefore_excluded is "
            f"{sorted(excluded)!r}. One list, or the exclusion means two things."
        )
    for const_name in ("VALIDATION_NOISE", "EVAL_NOISE"):
        names = noise_consts.get(const_name)
        if names is None:
            bad(f"build_dataset.py defines no {const_name}")
            continue
        smuggled = sorted(set(names) & excluded)
        if smuggled:
            bad(
                f"build_dataset.{const_name} names {smuggled!r}, which is generated "
                "rather than recorded. Round 8 trains, validates and qualifies on "
                "real recorded noise only."
            )
        unlisted = sorted(set(names) - set(usable))
        if unlisted:
            bad(
                f"build_dataset.{const_name} names {unlisted!r}, which is not among "
                "the real recordings background_recordings.real_and_usable lists"
            )

    # -- the projected dataset, recomputed -----------------------------------
    projection = config.get("dataset_projection", {})
    per_utterance = window.get("offsets_per_phrase_anchored_utterance", 0)
    for split_name, block in projection.items():
        if not isinstance(block, dict) or split_name == "note":
            continue
        for pool in ("positive_human", "near_phrase_human"):
            entry = block.get(pool)
            if not isinstance(entry, dict) or "utterances" not in entry:
                continue
            expected_windows = entry["utterances"] * per_utterance
            if entry.get("windows") != expected_windows:
                bad(
                    f"dataset_projection.{split_name}.{pool}: "
                    f"{entry['utterances']} utterances x {per_utterance} offsets "
                    f"= {expected_windows}, config says {entry.get('windows')}"
                )
        if "total_windows" in block:
            summed = sum(
                value["windows"]
                for key, value in block.items()
                if isinstance(value, dict) and "windows" in value
            )
            if summed != block["total_windows"]:
                bad(
                    f"dataset_projection.{split_name}.total_windows is "
                    f"{block['total_windows']}, the pools sum to {summed}"
                )
    for retired in ("positive", "near_phrase", "synthesized_speech", "common_speech"):
        if retired not in projection.get("retired_categories", []):
            bad(f"dataset_projection.retired_categories omits {retired!r}")

    # -- the training-split utterance counts must match the manifests --------
    train_block = projection.get("train", {})
    for pool, field in (
        ("positive_human", "positive_utterances"),
        ("near_phrase_human", "near_phrase_utterances"),
    ):
        declared = train_block.get(pool, {}).get("utterances")
        from_manifests = sum(
            by_dataset[member].get(field, 0)
            for member in SPLITS["train"]
            if member in by_dataset
        )
        if declared is not None and declared != from_manifests:
            bad(
                f"dataset_projection.train.{pool}.utterances is {declared}, "
                f"the train manifests declare {from_manifests}"
            )

    # -- the derived loss weights ------------------------------------------
    loss = config.get("loss", {})
    anchor = loss.get("r6c1_anchor", {})
    anchor_share = anchor.get("positive_loss_mass_share")
    if anchor_share is None:
        bad("loss.r6c1_anchor.positive_loss_mass_share is missing")
    else:
        recomputed_anchor = positive_loss_mass_share(
            anchor.get("positive_windows", 0),
            anchor.get("hard_negative_windows", 0),
            anchor.get("other_negative_windows", 0),
            negative_weight=anchor.get("negative_weight", 0.0),
            hard_negative_weight=anchor.get("hard_negative_weight", 0.0),
        )
        if abs(recomputed_anchor - anchor_share) > 5e-5:
            bad(
                f"loss.r6c1_anchor.positive_loss_mass_share is {anchor_share}, "
                f"its own counts and weights give {recomputed_anchor:.5f}"
            )
        anchor_total = (
            anchor.get("positive_windows", 0)
            + anchor.get("hard_negative_windows", 0)
            + anchor.get("other_negative_windows", 0)
        )
        if anchor_total != anchor.get("train_windows"):
            bad(
                f"loss.r6c1_anchor pools sum to {anchor_total}, "
                f"train_windows says {anchor.get('train_windows')}"
            )
        positives = train_block.get("positive_human", {}).get("windows", 0)
        hard = train_block.get("near_phrase_human", {}).get("windows", 0)
        other = (
            train_block.get("total_windows", 0) - positives - hard
            if "total_windows" in train_block
            else 0
        )
        derived = derived_negative_weight(positives, hard, other, anchor_share=anchor_share)
        if abs(derived - loss.get("negative_weight", -1)) > 5e-4:
            bad(
                f"loss.negative_weight is {loss.get('negative_weight')}, the "
                f"derivation rule on the projected counts gives {derived:.4f}"
            )
        if abs(loss.get("hard_negative_weight", -1) - 2 * loss.get("negative_weight", 0)) > 1e-9:
            bad("loss.hard_negative_weight is not exactly twice loss.negative_weight")
        carried_over = positive_loss_mass_share(
            positives,
            hard,
            other,
            negative_weight=anchor.get("negative_weight", 0.0),
            hard_negative_weight=anchor.get("hard_negative_weight", 0.0),
        )
        if abs(carried_over - loss.get("what_carrying_3_and_6_over_would_give", -1)) > 5e-4:
            bad(
                "loss.what_carrying_3_and_6_over_would_give is "
                f"{loss.get('what_carrying_3_and_6_over_would_give')}, "
                f"recomputed {carried_over:.4f}"
            )
    if loss.get("focal_gamma") != 2.0:
        bad(f"loss.focal_gamma is {loss.get('focal_gamma')!r}, not 2.0")
    if sorted(loss.get("hard_negative_categories", [])) != ["near_phrase", "near_phrase_human"]:
        bad("loss.hard_negative_categories changed")

    # -- schedule ------------------------------------------------------------
    schedule = config.get("schedule", {})
    if schedule.get("epochs") != 60:
        bad(f"schedule.epochs is {schedule.get('epochs')!r}, not 60")
    total_windows = train_block.get("total_windows")
    batch = schedule.get("batch_size")
    if total_windows and batch:
        steps = -(-total_windows // batch)
        if schedule.get("projected_steps_per_epoch") != steps:
            bad(
                f"schedule.projected_steps_per_epoch is "
                f"{schedule.get('projected_steps_per_epoch')}, "
                f"ceil({total_windows}/{batch}) = {steps}"
            )
        if schedule.get("projected_total_steps") != steps * schedule["epochs"]:
            bad(
                f"schedule.projected_total_steps is "
                f"{schedule.get('projected_total_steps')}, "
                f"{steps} x {schedule['epochs']} = {steps * schedule['epochs']}"
            )

    # -- the five targets, unchanged ----------------------------------------
    targets = {entry.get("id"): entry for entry in config.get("targets", [])}
    if set(targets) != set(TARGETS):
        bad(f"targets are {sorted(targets)}, not {sorted(TARGETS)}")
    for target_id, (name, limit, comparison) in TARGETS.items():
        entry = targets.get(target_id, {})
        if entry.get("limit") != limit:
            bad(f"target {target_id} limit is {entry.get('limit')!r}, not {limit!r}")
        if entry.get("comparison") != comparison:
            bad(f"target {target_id} comparison is {entry.get('comparison')!r}, not {comparison!r}")
        if name.split()[0].lower() not in str(entry.get("name", "")).lower():
            bad(f"target {target_id} is named {entry.get('name')!r}, expected {name!r}")

    # -- runtime contract ----------------------------------------------------
    runtime = config.get("runtime_contract", {})
    frame_length = _class_attribute(RUNTIME, "_OpenWakeWordEngine", "frame_length")
    if runtime.get("frame_length_samples") != frame_length:
        bad(
            f"runtime_contract.frame_length_samples is "
            f"{runtime.get('frame_length_samples')!r}; the engine uses {frame_length!r}"
        )
    runtime_consts = _module_constants(RUNTIME, {"_DEFAULT_CONFIRMATION_FRAMES"})
    if runtime.get("confirmation_frames") != runtime_consts.get("_DEFAULT_CONFIRMATION_FRAMES"):
        bad(
            f"runtime_contract.confirmation_frames is "
            f"{runtime.get('confirmation_frames')!r}; the runtime default is "
            f"{runtime_consts.get('_DEFAULT_CONFIRMATION_FRAMES')!r}"
        )

    # -- export and parity ---------------------------------------------------
    export = config.get("export_and_parity", {})
    if export.get("both_artifacts_from_one_frozen_candidate") is not True:
        bad("export_and_parity does not require both artifacts from one frozen candidate")
    if export.get("evaluated_independently_through_the_product_runtime") is not True:
        bad("export_and_parity does not require independent evaluation through the product runtime")
    if export.get("required_detection_disagreements") != 0:
        bad(
            "export_and_parity.required_detection_disagreements is "
            f"{export.get('required_detection_disagreements')!r}, not 0"
        )
    if export.get("detection_disagreement_is_fatal") is not True:
        bad("a detection-parity disagreement is not marked fatal")

    # -- the validation gate -------------------------------------------------
    gate = config.get("validation_gate", {})
    rule = str(gate.get("rule", "")).lower()
    if "5/5" not in rule or "before any sealed" not in rule:
        bad("validation_gate.rule does not state the 5/5-before-sealed requirement")
    gate_targets = {entry.get("id") for entry in gate.get("per_target_e005_form", [])}
    if gate_targets != set(TARGETS):
        bad(f"validation_gate.per_target_e005_form covers {sorted(gate_targets)}")
    validation_block = projection.get("validation", {})
    for entry in gate.get("per_target_e005_form", []):
        # The parity denominator is every validation window, so it has to be the
        # projection's own total rather than an independently typed number.
        if entry.get("id") == 5 and "total_windows" in validation_block:
            if entry.get("n") != validation_block["total_windows"]:
                bad(
                    f"validation_gate target 5 compares {entry.get('n')} windows; "
                    f"the validation projection totals "
                    f"{validation_block['total_windows']}"
                )
        n = entry.get("n")
        stated = entry.get("clean_run_bound")
        if isinstance(n, int) and isinstance(stated, (int, float)):
            recomputed = clean_run_upper_bound(n)
            tolerance = max(5e-5, stated * 0.02)
            if abs(recomputed - stated) > tolerance:
                bad(
                    f"validation_gate target {entry.get('id')}: clean-run bound "
                    f"{stated} for n={n}, exact Clopper-Pearson gives {recomputed:.6f}"
                )

    # -- statistical power ---------------------------------------------------
    power = config.get("statistical_power", {})
    if power.get("unit_of_evidence", "").split(",")[0].strip() != "utterance":
        bad("statistical_power.unit_of_evidence is not the utterance")
    for key in ("target_1", "target_2", "target_3", "target_4", "target_5"):
        if key not in power:
            bad(f"statistical_power has no {key}")
    for key in ("target_1", "target_3", "target_4", "target_5"):
        for n_text, stated in power.get(key, {}).get("clean_run_bound_by_n", {}).items():
            recomputed = clean_run_upper_bound(int(n_text))
            tolerance = max(5e-5, stated * 0.02)
            if abs(recomputed - stated) > tolerance:
                bad(
                    f"statistical_power.{key}: bound {stated} for n={n_text}, "
                    f"exact Clopper-Pearson gives {recomputed:.6f}"
                )
    target_2 = power.get("target_2", {})
    needed = hours_for_poisson_bound(TARGETS[2][1])
    if abs(target_2.get("hours_needed_at_0.2_per_hour", -1) - needed) > 0.02:
        bad(
            "statistical_power.target_2.hours_needed_at_0.2_per_hour is "
            f"{target_2.get('hours_needed_at_0.2_per_hour')}, Poisson gives {needed:.2f}"
        )
    for source, block in target_2.get("clean_run_bound_by_source", {}).items():
        hours = block.get("hours")
        stated = block.get("bound_per_hour")
        if hours and stated:
            recomputed = -__import__("math").log(0.05) / hours
            if abs(recomputed - stated) > max(0.002, stated * 0.02):
                bad(
                    f"statistical_power.target_2 source {source!r}: bound {stated}/h "
                    f"for {hours} h, Poisson gives {recomputed:.3f}/h"
                )
    if target_2.get("demonstrable") is not False:
        bad("statistical_power.target_2.demonstrable must be false until the 20 h subset exists")
    target_3 = power.get("target_3", {})
    if target_3.get("minimum_n", 0) - target_3.get("sealed_n_available", 0) != target_3.get(
        "shortfall_utterances"
    ):
        bad("statistical_power.target_3 shortfall does not equal minimum_n - sealed_n_available")

    # -- selection -----------------------------------------------------------
    selection = config.get("selection", {})
    if selection.get("selected_on") != "validation only":
        bad(f"selection.selected_on is {selection.get('selected_on')!r}")
    never = {str(x) for x in selection.get("never_selected_on", [])}
    if not set(SPLITS["sealed"]) <= never:
        bad("selection.never_selected_on does not name every sealed dataset")
    if selection.get("unit_of_evidence") != "utterance":
        bad("selection.unit_of_evidence is not the utterance")
    if selection.get("validation_margin") != 0.5:
        bad(f"selection.validation_margin is {selection.get('validation_margin')!r}, not 0.5")
    tie = selection.get("candidate_tie_break", {})
    if tie.get("prefer") != "fewer parameters":
        bad(f"selection.candidate_tie_break.prefer is {tie.get('prefer')!r}")

    # -- the sealed opening rule --------------------------------------------
    sealed = config.get("sealed_opening_rule", {})
    precondition = str(sealed.get("precondition", "")).lower()
    if "5/5" not in precondition:
        bad("sealed_opening_rule.precondition does not require 5/5 on validation")
    freeze_list = " | ".join(sealed.get("freeze_before_open", [])).lower()
    for needle in ("checkpoint", "onnx", "tflite", "threshold", "manifest", "commit"):
        if needle not in freeze_list:
            bad(f"sealed_opening_rule.freeze_before_open omits {needle!r}")

    # -- the failure procedure ----------------------------------------------
    failure = config.get("if_validation_fails", {})
    if "categ" not in str(failure.get("step_1", "")).lower():
        bad("if_validation_fails.step_1 does not diagnose by recording category")
    step_2 = str(failure.get("step_2", "")).lower()
    if "one" not in step_2 or "structural" not in step_2:
        bad("if_validation_fails.step_2 does not require exactly one structural correction")
    if "synthetic" not in str(failure.get("step_3_forbidden", "")).lower():
        bad("if_validation_fails does not forbid returning to synthetic data")

    # -- prerequisites -------------------------------------------------------
    prerequisites = " | ".join(config.get("prerequisite_code_changes", [])).lower()
    for needle in ("augment", "prefix", "offset", "utterance-level", "refusal"):
        if needle not in prerequisites:
            bad(f"prerequisite_code_changes omits {needle!r}")

    # -- the predeclaration against the real builder (V6) --------------------
    # Folded in so the predeclared windowing and build_human_dataset can never
    # silently drift again: once V6 raised the builder to the seven-offset
    # phrase-anchored design, an empty divergence is an invariant of a valid
    # config, and any regression on either side fails --check rather than
    # surfacing hours into a build. --check-builder-windowing runs the same
    # function on its own for a focused report.
    problems.extend(builder_windowing_divergence(config))

    return problems


def _hash_state(config: dict) -> str:
    """``"empty"``, ``"filled"`` or ``"mixed"`` over every required hash field."""
    values: list[str] = []
    for entry in config.get("manifests", []):
        values.append(str(entry.get("manifest_sha256", "")))
    for section, field in HASH_FIELDS:
        values.append(str(config.get(section, {}).get(field, "")))
    if not values:
        return "empty"
    if all(not value for value in values):
        return "empty"
    if all(value for value in values):
        return "filled"
    return "mixed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate round8_config.json and exit non-zero on any problem",
    )
    parser.add_argument(
        "--check-builder-windowing",
        action="store_true",
        help=(
            "compare the predeclared offset/window arithmetic against the real "
            "build_human_dataset ladder and label-0 windowing, and exit non-zero "
            "on any divergence. Separate from --check because the divergence is a "
            "known Owner decision, not a config edit to make green (V6)"
        ),
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)

    config = load(args.config)

    if args.check_builder_windowing:
        divergences = builder_windowing_divergence(config)
        if divergences:
            print(
                f"builder-windowing reconciliation: {len(divergences)} "
                "divergence(s) between the predeclaration and build_human_dataset "
                "(Owner decision — see builder_windowing_divergence):"
            )
            for divergence in divergences:
                print(f"  - {divergence}")
            return 1
        print("builder-windowing reconciliation: predeclaration matches the builder")
        return 0

    problems = check(config)
    state = _hash_state(config)
    print(f"round {config.get('round')}: status={config.get('status')} hashes={state}")
    print(f"arms: {[arm['id'] for arm in config['variation']['arms']]}")
    print(f"varied axis: {config['variation']['axis']}")
    if not args.check:
        return 0
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
