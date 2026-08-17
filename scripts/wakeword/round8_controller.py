#!/usr/bin/env python3
"""Round 8's execution order, as a state machine that cannot be run out of order.

Why this exists
---------------
``ROUND8_DESIGN.md`` fixes an order and calls it non-negotiable: verify the
training manifests, verify the validation manifest, prove the sealed sets are
still sealed, start from nothing, run the three predeclared widths, select on
E005 alone, export both backends from one frozen checkpoint, measure both
through the real product runtime, require 5/5 with zero detection
disagreements, freeze everything, and only then — as a separate, deliberate
act — open a sealed set.

An order written in a document is followed by whoever read it most recently.
Every failure this project has recorded was available in a document at the
time: the threshold retuned on the set it was measured on, the ``0.000/h`` over
11.659 hours printed next to the word "pass", the synthetic warm start behind a
guard that had no caller. So the order is code here, the transitions are
persisted, and a state whose predecessor has not succeeded cannot run at all.

What this file is *not*
-----------------------
It is not a second implementation of anything. Every check it makes either
delegates to the module that owns it or verifies that module's own recorded
output:

============================  =========================================
what                          who owns it
============================  =========================================
the predeclaration            ``round8_config.check``
manifest freshness            ``freeze_manifest.verify``
"this set may not be used     ``freeze_manifest.assert_usable_for``
for that"
which split a speaker is in   ``build_human_dataset.registry_split``
a synthetic initialization    ``build_human_dataset.refuse_synthetic_initialization``
a retired artifact's bytes    ``retired_synthetic_artifacts.json``, via
                              ``qualify.retired_hashes``
whether 5/5 is *demonstrated* ``qualify.build_report``
the freeze's completeness     ``qualify.FreezeRecord.assert_complete``
spending a sealed set         ``qualify.SealLedger.open_sealed``
which files ship, by backend  ``promote_model.BACKEND_ARTIFACTS``
the five acceptance targets   ``promote_model.ACCEPTANCE_TARGETS`` and
                              ``qualify.TARGETS``, cross-checked
============================  =========================================

Because a delegation that is quietly dropped looks exactly like a check that
passed, every state declares the delegates it must call and the controller
refuses the state if one of them was not called. Deleting a line is therefore a
test failure and not a silent weakening.

The expensive stages — training an arm, exporting, streaming a corpus through
the runtime, computing a verdict — are injected callables. That is what makes
this file testable without a GPU, without audio and without a single recorded
human being, and it is also what keeps the orchestrator from growing a second
copy of the training loop.

Two things it deliberately refuses to do
----------------------------------------
**It never computes a verdict.** ``qualify.py`` decides whether the evidence
*demonstrates* a target, and on Round 8's own projected quantities it refuses
targets 2 and 3 for want of evidence (11.66 h against the 15 the 0.2/h target
needs; 112 near-phrase utterances against 150). State 9 reads
``official.qualified_5_of_5`` and the harness's own verdict line and refuses
with them quoted. There is no path here that converts a refusal into a pass,
and no arithmetic here that could produce a competing answer.

**It never promotes.** Shipping is ``promote_model.py``'s gate, with its own
authorization and its own ledger. This controller stops at the freeze.

The barrier between state 10 and state 11
-----------------------------------------
Opening a sealed set publishes the one measurement nobody has tuned against,
and no amount of care puts it back. So state 11 is not in ``SEQUENCE``: the
automatic driver iterates states 1 to 10, ``run_state`` refuses state 11 by
name, ``advance(to=11)`` refuses, and the CLI has no way to spell it. State 11
runs only through :meth:`Round8Controller.open_sealed`, which additionally
requires the exact authorization phrase ``OPEN SEALED <dataset>`` for the one
dataset being spent. Reaching it takes a different command, a different method
and a sentence that names the cost.

Refusals, and what each one is for
---------------------------------
Every refusal below is a distinct failure with a distinct name, so a log line
says what went wrong rather than that something did.

================================  ==================================================
refusal                           the condition it fails closed on
================================  ==================================================
SyntheticInitializationRefused    a synthetic hash, retired checkpoint or the
                                  shipped model offered as initialization
SealedSpeakerLeakRefused          a sealed speaker in training, validation or
                                  selection
ValidationSpeakerLeakRefused      a validation speaker in training
ThresholdMovedAfterSealRefused    the threshold changed after the freeze
BackendMissingRefused             ONNX or TFLite absent
BackendFallbackRefused            a requested backend silently became the other
IncompleteEvidenceRefused         evidence missing what a verdict needs
IncompatibleCheckpointRefused     a resume from a checkpoint that is not this plan's
WeightsDivergedRefused            the two exports did not come from one checkpoint
DetectionDisagreementRefused      a non-zero detection disagreement
QualificationRefused              the harness did not declare 5/5 (this is how an
                                  underpowered target surfaces)
ManifestNotFrozenRefused          a manifest that does not verify against its data
SealAlreadySpentRefused           a sealed set with an opening already on the record
StateOutOfOrderRefused            a state whose predecessor has not succeeded
PlanChangedRefused                the predeclaration moved under a run
DelegationMissingRefused          a state did not call a check it must delegate
SealBarrierRefused                state 11 reached without its own deliberate command
================================  ==================================================

Usage::

    round8_controller.py plan    --journal round8_journal.json
    round8_controller.py status  --journal round8_journal.json
    round8_controller.py advance --journal round8_journal.json --to 3
    round8_controller.py open-sealed --journal round8_journal.json \\
        --dataset E006 --authorize 'OPEN SEALED E006'

``advance`` cannot reach state 11, whatever ``--to`` says.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

SCHEMA_VERSION = 1

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CONFIG_PATH = HERE / "round8_config.json"

TOOL = "scripts/wakeword/round8_controller.py"

#: The seed the synthetic era used. Reusing it would imply a draw-for-draw
#: comparability that a different dataset does not have, and the predeclaration
#: says so; asserted here rather than trusted.
SYNTHETIC_ERA_SEED = 20260807

#: The two backends that ship. Both, always: macOS ARM64 loads the tflite pair,
#: so a result on one backend is a result about half the product.
BACKENDS: tuple[str, ...] = ("onnx", "tflite")

#: Written into each arm's output directory at state 4, so a checkpoint found
#: there later can be checked against the plan it claims to belong to. Same
#: shape of evidence as the ``DATASET_CONTRACT.json`` that
#: ``build_human_dataset.refuse_synthetic_initialization`` looks for: beside the
#: file, and judged only after the bytes are.
INIT_RECORD_NAME = "ROUND8_INIT.json"

#: The trainer's own resume file. ``train_model.train`` picks this up from the
#: output directory automatically, which is exactly why the controller looks for
#: it before invoking the stage rather than after.
CHECKPOINT_NAME = "checkpoint.pt"

#: The phrase that opens a sealed set. It names the dataset because a generic
#: ``--yes`` is a habit and a dataset name is a decision.
AUTHORIZATION_TEMPLATE = "OPEN SEALED {dataset}"


# ── refusals ─────────────────────────────────────────────────────────────────


class Refusal(RuntimeError):
    """Base class for every refusal this controller makes."""


class StateOutOfOrderRefused(Refusal):
    """A state was reached before an earlier one succeeded, or reached twice."""


class PlanChangedRefused(Refusal):
    """The predeclaration is not the one this run started against."""


class DelegationMissingRefused(Refusal):
    """A state did not call a check it is required to delegate."""


class SealBarrierRefused(Refusal):
    """A sealed set was reached for without the separate deliberate command."""


class ManifestNotFrozenRefused(Refusal):
    """A manifest does not describe the bytes on disk."""


class SealedSpeakerLeakRefused(Refusal):
    """A sealed speaker appeared where nothing sealed may appear."""


class ValidationSpeakerLeakRefused(Refusal):
    """A validation speaker appeared in training."""


class SyntheticInitializationRefused(Refusal):
    """Something retired, synthetic or already shipped was offered as a start."""


class IncompatibleCheckpointRefused(Refusal):
    """A resume from a checkpoint that is not this plan's own."""


class WeightsDivergedRefused(Refusal):
    """The two exported artifacts did not come from one set of weights."""


class BackendMissingRefused(Refusal):
    """A backend the product ships was not measured."""


class BackendFallbackRefused(Refusal):
    """A requested backend quietly became the other one."""


class DetectionDisagreementRefused(Refusal):
    """The two backends decided differently about a window."""


class IncompleteEvidenceRefused(Refusal):
    """The evidence does not carry what a verdict needs."""


class QualificationRefused(Refusal):
    """The qualification harness did not declare 5/5, in its own words."""


class ThresholdMovedAfterSealRefused(Refusal):
    """The frozen operating point changed after it was frozen."""


class SealAlreadySpentRefused(Refusal):
    """A sealed set already has an opening on the record."""


# ── the plan, pinned from the predeclaration ─────────────────────────────────


@dataclass(frozen=True)
class Arm:
    """One predeclared capacity arm."""

    id: str
    channels: tuple[int, ...]
    parameters: int

    def body(self) -> dict:
        return {"id": self.id, "channels": list(self.channels), "parameters": self.parameters}


@dataclass(frozen=True)
class Plan:
    """Everything about Round 8 that must not move while it runs.

    Its digest is what a resumed run is checked against. Widening the arm set,
    changing the seed, renaming a split or weakening a target all change the
    digest, and a journal recorded under a different digest is refused rather
    than continued — a half-executed plan continued under a new one is how a
    result ends up describing neither.
    """

    round: int
    train: tuple[str, ...]
    validation: tuple[str, ...]
    sealed: tuple[str, ...]
    arms: tuple[Arm, ...]
    seed: int
    stop_condition: str
    targets: tuple[tuple[int, float, str], ...]
    validation_gate_rule: str
    sealed_precondition: str
    confirmation_frames: int
    artifacts: tuple[tuple[str, str], ...]

    def body(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "round": self.round,
            "splits": {
                "train": list(self.train),
                "validation": list(self.validation),
                "sealed": list(self.sealed),
            },
            "arms": [arm.body() for arm in self.arms],
            "seed": self.seed,
            "stop_condition": self.stop_condition,
            "targets": [
                {"id": tid, "limit": limit, "comparison": comparison}
                for tid, limit, comparison in self.targets
            ],
            "validation_gate_rule": self.validation_gate_rule,
            "sealed_precondition": self.sealed_precondition,
            "confirmation_frames": self.confirmation_frames,
            "artifacts": {backend: name for backend, name in self.artifacts},
        }

    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.body())).hexdigest()

    def arm(self, arm_id: str) -> Arm | None:
        for arm in self.arms:
            if arm.id == arm_id:
                return arm
        return None


def _canonical(body: Mapping | Sequence) -> bytes:
    """The bytes an identity is computed over: sorted keys, no incidental space."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the states ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class State:
    """One step of the order, and the delegations it may not skip."""

    number: int
    name: str
    purpose: str
    delegates: tuple[str, ...]


#: States 1 to 10 — the automatic sequence. State 11 is deliberately absent:
#: see :data:`SEAL_STATE` and the module docstring.
SEQUENCE: tuple[State, ...] = (
    State(
        1,
        "verify_training_manifests",
        "E001, E003 and E004 verify against their own bytes and are usable for training",
        ("load_manifest", "verify_manifest", "assert_usable_for", "registry_split",
         "sealed_splits"),
    ),
    State(
        2,
        "verify_validation_manifest",
        "E005 verifies, is usable for validation, and shares no speaker with training",
        ("load_manifest", "verify_manifest", "assert_usable_for", "registry_split",
         "sealed_splits"),
    ),
    State(
        3,
        "prove_seals_intact",
        "E002, E006 and E007 are registry-sealed, unopened, and named nowhere upstream",
        ("registry_split", "sealed_splits", "seal_status"),
    ),
    State(
        4,
        "initialize_from_fresh_weights",
        "every arm starts from nothing, at the predeclared seed",
        ("retired_digests",),
    ),
    State(
        5,
        "run_bounded_capacity_design",
        "the three predeclared widths, in order, at one seed, and no fourth",
        ("train_arm", "retired_digests"),
    ),
    State(
        6,
        "select_candidate_and_threshold",
        "one candidate and one threshold, chosen on E005 and nothing else",
        ("select_candidate",),
    ),
    State(
        7,
        "export_both_backends",
        "hey_youtab.onnx and hey_youtab.tflite from one set of weights",
        ("export_artifact", "retired_digests"),
    ),
    State(
        8,
        "measure_through_product_runtime",
        "both artifacts through the real engine, each on the backend it was asked for",
        ("run_runtime",),
    ),
    State(
        9,
        "require_validation_5_of_5",
        "the harness declares 5/5 with zero detection disagreements, or this stops",
        ("qualify_report",),
    ),
    State(
        10,
        "freeze_candidate_threshold_and_evidence",
        "candidate, threshold, artifacts, manifests, runtime and commit stop moving",
        ("load_manifest", "verify_manifest", "assert_usable_for", "freeze_record"),
    ),
)

#: State 11. Not in :data:`SEQUENCE`, not in the driver's table, and not
#: expressible on the ``advance`` command line.
SEAL_STATE = State(
    11,
    "open_sealed_set",
    "spend one sealed set, once, against the frozen candidate",
    ("freeze_record", "seal_status", "open_seal"),
)

ALL_STATES: tuple[State, ...] = (*SEQUENCE, SEAL_STATE)

STATES_BY_NUMBER: Mapping[int, State] = {state.number: state for state in ALL_STATES}


# ── the seams ────────────────────────────────────────────────────────────────


def _load_wakeword_module(name: str):
    """Import a ``scripts/wakeword`` module by path.

    That directory is not a package, and every tool in it that needs a sibling
    does this. ``HERE`` goes on ``sys.path`` because ``build_human_dataset``
    imports ``build_dataset`` and ``assets`` by bare name.
    """
    cached = sys.modules.get(f"_round8_{name}")
    if cached is not None:
        return cached
    path = HERE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_round8_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreadable file
        raise Refusal(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    for entry in (str(HERE), str(REPO_ROOT)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Delegates:
    """Every check and every expensive stage, as an injected callable.

    The verification delegates have real defaults, because verifying a frozen
    manifest and reading a seal ledger cost nothing and are exactly what this
    controller is useful for before any recording exists. The four expensive
    stages default to ``None`` and are refused at the point of use, so a run
    that has not been given a trainer says so instead of pretending.
    """

    # the predeclaration
    check_config: Callable[[Mapping], Sequence[str]]
    acceptance_targets: Callable[[], Mapping[int, tuple[float, str]]]
    backend_artifacts: Callable[[], Mapping[str, str]]
    # datasets and splits
    load_manifest: Callable[[Path], Mapping]
    verify_manifest: Callable[[Path, Path], Mapping]
    assert_usable_for: Callable[[Mapping, str], None]
    registry_split: Callable[[str], str]
    sealed_splits: Callable[[], frozenset[str]]
    # content addressing
    file_digest: Callable[[Path], str]
    retired_digests: Callable[[], Mapping[str, str]]
    refuse_synthetic_initialization: Callable[[Path], None]
    # freezing and seals
    freeze_record: Callable[[Mapping], object]
    seal_status: Callable[[str], Mapping | None]
    open_seal: Callable[[str, object], Mapping] | None = None
    # the expensive stages
    train_arm: Callable[[Mapping], Mapping] | None = None
    select_candidate: Callable[[Sequence[Mapping], Mapping], Mapping] | None = None
    export_artifact: Callable[[str, Path, Mapping], Mapping] | None = None
    run_runtime: Callable[[str, Path, Mapping], Mapping] | None = None
    qualify_report: Callable[[Mapping], Mapping] | None = None

    @classmethod
    def default(cls, *, ledger: Path, **overrides) -> "Delegates":
        """Bind the verification delegates to the modules that own them.

        Every binding is a lazy closure. Importing ``build_human_dataset`` pulls
        in numpy and the whole ingest stage, and a controller that cannot even
        print its plan without it would be useless on a machine that only needs
        to read a journal.
        """

        def _freeze_manifest():
            return _load_wakeword_module("freeze_manifest")

        def _qualify():
            return _load_wakeword_module("qualify")

        def _human():
            return _load_wakeword_module("build_human_dataset")

        def _ledger():
            return _qualify().SealLedger(ledger)

        base = cls(
            check_config=lambda config: _load_wakeword_module("round8_config").check(dict(config)),
            acceptance_targets=lambda: {
                target.id: (float(target.limit), str(target.comparison))
                for target in _load_wakeword_module("promote_model").ACCEPTANCE_TARGETS.values()
            },
            backend_artifacts=lambda: dict(
                _load_wakeword_module("promote_model").BACKEND_ARTIFACTS
            ),
            load_manifest=lambda path: _freeze_manifest().load(path),
            verify_manifest=lambda manifest, root: _freeze_manifest().verify(
                manifest, root, strict=True
            ),
            assert_usable_for=lambda manifest, purpose: _freeze_manifest().assert_usable_for(
                dict(manifest), purpose
            ),
            registry_split=lambda speaker: _human().registry_split(speaker),
            sealed_splits=lambda: frozenset(_human().SEALED_SPLITS),
            file_digest=lambda path: _qualify().sha256_file(path),
            retired_digests=lambda: _qualify().retired_hashes(),
            refuse_synthetic_initialization=(
                lambda path: _human().refuse_synthetic_initialization(path)
            ),
            freeze_record=lambda body: _qualify().freeze_from_json(dict(body)),
            seal_status=lambda dataset: _ledger().status(dataset),
            open_seal=lambda dataset, freeze: _ledger().open_sealed(dataset, freeze),
        )
        return replace(base, **overrides) if overrides else base


@dataclass(frozen=True)
class DatasetPaths:
    """Where one dataset's frozen manifest and its data live."""

    manifest: Path
    root: Path


@dataclass(frozen=True)
class RunInputs:
    """What the operator supplies. Nothing here can change a verdict."""

    datasets: Mapping[str, DatasetPaths] = field(default_factory=dict)
    arm_output: Mapping[str, Path] = field(default_factory=dict)
    artifact_dir: Path = Path(".")
    runtime_version: str = ""
    repo_commit: str = ""
    config_sha256: str = ""
    resume_checkpoint: Path | None = None
    note: str = ""


# ── building the plan ────────────────────────────────────────────────────────


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_plan(config: Mapping, delegates: Delegates) -> Plan:
    """Pin the plan, after the module that owns the predeclaration validates it.

    ``round8_config.check`` recomputes every derived figure in the config from
    its own inputs, so it is called rather than restated. What is added here is
    the one thing that file cannot check about itself: that the five acceptance
    targets still agree with the two *independent* hard-coded copies in
    ``promote_model`` and ``qualify``. A config that weakened a limit would
    otherwise validate itself.
    """
    problems = list(delegates.check_config(config))
    if problems:
        raise PlanChangedRefused(
            "the predeclaration does not validate, so there is no plan to run:\n  - "
            + "\n  - ".join(str(problem) for problem in problems)
        )

    splits = config.get("splits", {})
    train = tuple(str(name) for name in splits.get("train", ()))
    validation = tuple(str(name) for name in splits.get("validation", ()))
    sealed = tuple(str(name) for name in splits.get("sealed", ()))
    if not train or not validation or not sealed:
        raise PlanChangedRefused(
            f"splits must name all three roles; got train={list(train)}, "
            f"validation={list(validation)}, sealed={list(sealed)}"
        )
    if splits.get("immutable") is not True:
        raise PlanChangedRefused("splits.immutable is not true, so the splits are not a plan")
    overlap = (set(train) & set(validation)) | (set(train) & set(sealed)) | (
        set(validation) & set(sealed)
    )
    if overlap:
        raise PlanChangedRefused(f"{sorted(overlap)} appear in more than one split")

    variation = config.get("variation", {})
    arms = tuple(
        Arm(str(entry["id"]), tuple(int(c) for c in entry["channels"]), int(entry["parameters"]))
        for entry in variation.get("arms", ())
    )
    if len(arms) < 2 or len(arms) > 4:
        raise PlanChangedRefused(
            f"{len(arms)} arms; the predeclared design is a bounded set of 2 to 4 and "
            "widening it needs an Owner decision, not a config edit"
        )
    if variation.get("runs") != len(arms):
        raise PlanChangedRefused(
            f"variation.runs is {variation.get('runs')!r} for {len(arms)} arms"
        )
    widths = [arm.channels for arm in arms]
    if sorted(widths, reverse=True) != widths:
        raise PlanChangedRefused(
            "the arms are not ordered widest to narrowest; the design varies capacity "
            "downward and the order is the experiment"
        )
    if len(set(widths)) != len(widths):
        raise PlanChangedRefused("two arms declare the same channel widths")
    stop_condition = str(variation.get("stop_condition", "")).strip()
    if not stop_condition:
        raise PlanChangedRefused(
            "variation.stop_condition is empty. A capacity sweep without a stated stop "
            "is a sweep, and this design forbids one"
        )

    init = config.get("initialization", {})
    if init.get("fresh") is not True:
        raise PlanChangedRefused("initialization.fresh is not true")
    if init.get("from_synthetic_checkpoint") is not False:
        raise PlanChangedRefused("initialization.from_synthetic_checkpoint is not false")
    seed = init.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise PlanChangedRefused(f"initialization.seed is {seed!r}, not an integer")
    if seed == SYNTHETIC_ERA_SEED:
        raise PlanChangedRefused(
            f"initialization.seed reuses the synthetic era's {SYNTHETIC_ERA_SEED}"
        )
    for field_name in ("torch_manual_seed", "numpy_default_rng"):
        if init.get(field_name) != seed:
            raise PlanChangedRefused(f"initialization.{field_name} disagrees with the seed")

    targets = tuple(
        (int(entry["id"]), float(entry["limit"]), str(entry["comparison"]))
        for entry in sorted(config.get("targets", ()), key=lambda e: int(e["id"]))
    )
    acceptance = delegates.acceptance_targets()
    if {tid for tid, _, _ in targets} != set(acceptance):
        raise PlanChangedRefused(
            f"the config declares targets {sorted(tid for tid, _, _ in targets)}; the "
            f"acceptance gate knows {sorted(acceptance)}"
        )
    for tid, limit, comparison in targets:
        gate_limit, gate_comparison = acceptance[tid]
        if limit != gate_limit or comparison != gate_comparison:
            raise PlanChangedRefused(
                f"target {tid} is {comparison} {limit!r} in the config and "
                f"{gate_comparison} {gate_limit!r} in the acceptance gate. The five "
                "targets are unchanged from ROUND6_DESIGN.md and cannot be weakened here"
            )

    gate_rule = str(config.get("validation_gate", {}).get("rule", ""))
    lowered = gate_rule.lower()
    if "5/5" not in lowered or "before any sealed" not in lowered:
        raise PlanChangedRefused(
            "validation_gate.rule no longer states the 5/5-before-any-sealed requirement"
        )
    precondition = str(config.get("sealed_opening_rule", {}).get("precondition", ""))
    if "5/5" not in precondition:
        raise PlanChangedRefused("sealed_opening_rule.precondition no longer requires 5/5")

    runtime = config.get("runtime_contract", {})
    confirmation = runtime.get("confirmation_frames")
    if not isinstance(confirmation, int) or isinstance(confirmation, bool) or confirmation < 1:
        raise PlanChangedRefused(
            f"runtime_contract.confirmation_frames is {confirmation!r}; the engine needs "
            "a positive count of consecutive frames"
        )

    artifacts = delegates.backend_artifacts()
    if set(artifacts) != set(BACKENDS):
        raise PlanChangedRefused(
            f"the promotion gate ships {sorted(artifacts)}; this controller measures "
            f"{sorted(BACKENDS)}"
        )

    return Plan(
        round=int(config.get("round", 0)),
        train=train,
        validation=validation,
        sealed=sealed,
        arms=arms,
        seed=seed,
        stop_condition=stop_condition,
        targets=targets,
        validation_gate_rule=gate_rule,
        sealed_precondition=precondition,
        confirmation_frames=confirmation,
        artifacts=tuple(sorted((str(k), str(v)) for k, v in artifacts.items())),
    )


# ── the journal ──────────────────────────────────────────────────────────────


class Journal:
    """The persisted record of which states have succeeded, and on what.

    Append-only. Nothing removes a transition, including a refused one: a run
    that failed state 9 twice and passed on the third attempt is a different
    thing from one that passed first time, and the difference belongs in the
    record rather than in somebody's memory.

    The plan digest is stored and checked on every load. A journal recorded
    against a different plan is refused, not continued.
    """

    def __init__(self, path: Path, plan: Plan, payload: Mapping | None = None):
        self.path = path
        self.plan = plan
        self.transitions: list[dict] = []
        self.sealed: dict | None = None
        if payload is not None:
            self.transitions = [dict(entry) for entry in payload.get("transitions", [])]
            recorded_seal = payload.get("sealed")
            self.sealed = dict(recorded_seal) if recorded_seal else None

    @classmethod
    def open(cls, path: Path, plan: Plan) -> "Journal":
        """Load a journal for this plan, or start one."""
        if not path.exists():
            return cls(path, plan)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise PlanChangedRefused(
                f"{path.name} is schema_version {payload.get('schema_version')!r}; this "
                f"tool reads {SCHEMA_VERSION}. A journal read under the wrong schema "
                "would report the wrong state as reached."
            )
        recorded = str(payload.get("plan_digest", ""))
        if recorded != plan.digest():
            raise PlanChangedRefused(
                f"{path.name} was recorded against plan {recorded[:12]}... and the "
                f"predeclaration now digests to {plan.digest()[:12]}.... The plan "
                "changed under a run in progress. A half-executed plan continued under "
                "a new one describes neither; start a new journal or restore the plan."
            )
        return cls(path, plan, payload)

    # -- reading -----------------------------------------------------------

    def status(self, number: int) -> str | None:
        """The status of the most recent attempt at ``number``, or None."""
        for entry in reversed(self.transitions):
            if entry.get("state") == number:
                return str(entry.get("status"))
        return None

    def evidence(self, number: int) -> dict:
        """The evidence the most recent *successful* attempt at ``number`` recorded."""
        for entry in reversed(self.transitions):
            if entry.get("state") == number and entry.get("status") == "succeeded":
                return dict(entry.get("evidence", {}))
        raise StateOutOfOrderRefused(
            f"state {number} ({STATES_BY_NUMBER[number].name}) has no successful "
            "transition, so there is no evidence from it to read"
        )

    def reached(self) -> tuple[int, ...]:
        return tuple(
            state.number for state in ALL_STATES if self.status(state.number) == "succeeded"
        )

    # -- writing -----------------------------------------------------------

    def record(
        self,
        state: State,
        *,
        status: str,
        evidence: Mapping,
        delegates_called: Sequence[str],
        detail: str = "",
    ) -> dict:
        entry = {
            "state": state.number,
            "name": state.name,
            "status": status,
            "recorded_utc": _utc_now(),
            "delegates_called": sorted(set(delegates_called)),
            "delegates_required": list(state.delegates),
            "detail": detail,
            "evidence": json.loads(json.dumps(evidence, sort_keys=True)),
        }
        self.transitions.append(entry)
        self._persist()
        return entry

    def seal(self, *, threshold_hex: str, freeze_digest: str, candidate_id: str,
             freeze_body: Mapping) -> dict:
        """Record the frozen operating point. Once."""
        if self.sealed is not None:
            if self.sealed.get("threshold_hex") != threshold_hex:
                raise ThresholdMovedAfterSealRefused(
                    f"the threshold was frozen at {self.sealed.get('threshold_hex')} and "
                    f"is now {threshold_hex}. A threshold that moves after the freeze "
                    "means the sealed measurement is not of the candidate that was "
                    "qualified, and re-recording the speaker does not fix it."
                )
            if self.sealed.get("freeze_digest") != freeze_digest:
                raise ThresholdMovedAfterSealRefused(
                    f"the freeze was recorded as {str(self.sealed.get('freeze_digest'))[:12]}"
                    f"... and now digests to {freeze_digest[:12]}.... Something inside it "
                    "changed after it stopped moving."
                )
            return dict(self.sealed)
        self.sealed = {
            "threshold_hex": threshold_hex,
            "threshold_decimal": repr(float.fromhex(threshold_hex)),
            "freeze_digest": freeze_digest,
            "candidate_id": candidate_id,
            "sealed_utc": _utc_now(),
            "freeze_body": json.loads(json.dumps(dict(freeze_body), sort_keys=True)),
        }
        self._persist()
        return dict(self.sealed)

    def body(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": TOOL,
            "plan_digest": self.plan.digest(),
            "plan": self.plan.body(),
            "note": (
                "Append-only. A transition here is a claim that a state ran and what it "
                "recorded; a refused transition stays for the same reason a passed one "
                "does. State 11 is reachable only through the open-sealed command."
            ),
            "sealed": self.sealed,
            "transitions": self.transitions,
        }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(self.body(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        # Write-then-replace. A journal truncated mid-write would read as "state
        # never reached", which for state 10 means the freeze is gone and for
        # state 11 means a spent sealed set looks unspent.
        os.replace(temporary, self.path)


# ── the controller ───────────────────────────────────────────────────────────


class Round8Controller:
    """Runs the order, records it, and refuses everything else."""

    def __init__(
        self,
        *,
        plan: Plan,
        journal: Journal,
        delegates: Delegates,
        inputs: RunInputs | None = None,
    ):
        self.plan = plan
        self.journal = journal
        self.delegates = delegates
        self.inputs = inputs if inputs is not None else RunInputs()
        self._called: list[str] = []

    # -- delegation --------------------------------------------------------

    def _call(self, name: str, *args, **kwargs):
        """Invoke a delegate and record that it was invoked.

        The recording is the point. ``REQUIRED`` names, per state, the checks
        that state may not skip, and a state that returns without having called
        one of them is refused. Deleting a delegation from a state body is
        therefore a failure rather than a quietly weaker run.
        """
        delegate = getattr(self.delegates, name, None)
        if delegate is None:
            raise DelegationMissingRefused(
                f"no {name!r} delegate is wired, so the check or stage it stands for "
                "cannot run. This controller orchestrates; it does not reimplement. "
                "Supply the stage or do not claim the state."
            )
        self._called.append(name)
        return delegate(*args, **kwargs)

    def _assert_delegated(self, state: State) -> None:
        skipped = [name for name in state.delegates if name not in self._called]
        if skipped:
            raise DelegationMissingRefused(
                f"state {state.number} ({state.name}) finished without calling "
                f"{skipped}. Every check this controller makes is either a delegation "
                "or a reading of one; a state that made neither has checked nothing."
            )

    # -- ordering ----------------------------------------------------------

    def _assert_reached(self, number: int) -> None:
        for state in SEQUENCE:
            if state.number >= number:
                break
            if self.journal.status(state.number) != "succeeded":
                raise StateOutOfOrderRefused(
                    f"state {number} ({STATES_BY_NUMBER[number].name}) cannot run: "
                    f"state {state.number} ({state.name}) has not succeeded "
                    f"(status {self.journal.status(state.number)!r}). The order is the "
                    "design, not a suggestion."
                )

    def run_state(self, number: int) -> dict:
        """Run one state of the automatic sequence."""
        if number == SEAL_STATE.number:
            raise SealBarrierRefused(
                f"state {SEAL_STATE.number} ({SEAL_STATE.name}) is not part of the "
                "automatic sequence and cannot be run from here. Opening a sealed set "
                "publishes the one measurement nobody has tuned against, and nothing "
                "puts it back, so it takes its own command: "
                f"open_sealed(dataset, authorization={AUTHORIZATION_TEMPLATE!r})."
            )
        state = STATES_BY_NUMBER.get(number)
        if state is None:
            raise StateOutOfOrderRefused(
                f"there is no state {number}; the sequence is "
                f"{[s.number for s in SEQUENCE]} plus the sealed opening"
            )
        if self.journal.status(number) == "succeeded":
            raise StateOutOfOrderRefused(
                f"state {number} ({state.name}) already succeeded. A state runs once: "
                "re-running selection or the freeze is how an operating point moves "
                "after it was measured."
            )
        self._assert_reached(number)
        self._called = []
        body = getattr(self, f"_state_{number}")
        try:
            evidence = body()
        except Refusal as refusal:
            self.journal.record(
                state,
                status="refused",
                evidence={},
                delegates_called=self._called,
                detail=f"{type(refusal).__name__}: {refusal}",
            )
            raise
        self._assert_delegated(state)
        return self.journal.record(
            state, status="succeeded", evidence=evidence, delegates_called=self._called
        )

    def advance(self, *, to: int | None = None) -> list[dict]:
        """Run every not-yet-succeeded state up to ``to``, default state 10.

        Cannot reach state 11. ``to`` is validated before anything runs, so an
        operator who typed 11 gets a refusal instead of nine states and then a
        refusal.
        """
        limit = SEQUENCE[-1].number if to is None else int(to)
        if limit >= SEAL_STATE.number:
            raise SealBarrierRefused(
                f"advance stops at state {SEQUENCE[-1].number} "
                f"({SEQUENCE[-1].name}). State {SEAL_STATE.number} spends a sealed set "
                "and is not something a sequence walks into; it needs the open-sealed "
                "command and the authorization phrase that names the dataset."
            )
        if limit < SEQUENCE[0].number:
            raise StateOutOfOrderRefused(f"--to {limit} is before state {SEQUENCE[0].number}")
        done: list[dict] = []
        for state in SEQUENCE:
            if state.number > limit:
                break
            if self.journal.status(state.number) == "succeeded":
                continue
            done.append(self.run_state(state.number))
        return done

    # -- state 1 -----------------------------------------------------------

    def _state_1(self) -> dict:
        """The training manifests verify, and hold nobody they must not."""
        return self._verify_role(self.plan.train, role="train", purpose="training")

    # -- state 2 -----------------------------------------------------------

    def _state_2(self) -> dict:
        """E005 verifies, and shares no speaker with what state 1 recorded."""
        evidence = self._verify_role(self.plan.validation, role="validation",
                                     purpose="validation")
        trained = set()
        for names in self.journal.evidence(1).get("speakers", {}).values():
            trained |= {str(name) for name in names}
        for dataset, names in evidence["speakers"].items():
            shared = sorted(trained & {str(name) for name in names})
            if shared:
                raise ValidationSpeakerLeakRefused(
                    f"{shared} appear in both the training manifests and the validation "
                    f"dataset {dataset}. Selecting a threshold and an epoch on a voice "
                    "the model was fitted on is how a model looks better than it is, "
                    "and the E005 result is the only unseen number Round 8 has before "
                    "the seals are opened."
                )
        evidence["shares_no_speaker_with_training"] = True
        return evidence

    def _verify_role(self, datasets: Sequence[str], *, role: str, purpose: str) -> dict:
        digests: dict[str, str] = {}
        speakers: dict[str, list[str]] = {}
        reports: dict[str, dict] = {}
        for dataset in datasets:
            paths = self._dataset_paths(dataset)
            manifest = dict(self._call("load_manifest", paths.manifest))
            if str(manifest.get("dataset")) != dataset:
                raise ManifestNotFrozenRefused(
                    f"{paths.manifest} describes dataset "
                    f"{manifest.get('dataset')!r}, not {dataset!r}. A manifest read for "
                    "the wrong dataset verifies the wrong bytes."
                )
            # freeze_manifest owns "this usage does not permit that purpose",
            # including the sealed-evaluation case. Delegated, not restated.
            self._delegate_usage(manifest, purpose, dataset)
            named = self._speakers_of(dataset, manifest)
            self._assert_speakers_admissible(named, role=role, dataset=dataset)
            report = dict(self._call("verify_manifest", paths.manifest, paths.root))
            self._assert_manifest_verified(dataset, paths, report)
            digest = str(manifest.get("manifest_sha256", ""))
            if len(digest) != 64:
                raise ManifestNotFrozenRefused(
                    f"{dataset}: manifest_sha256 is {digest!r}, not a sha256. The freeze "
                    "records this digest, and a result traced to a manifest nobody "
                    "hashed is traced to nothing."
                )
            digests[dataset] = digest
            speakers[dataset] = sorted(named)
            reports[dataset] = {
                "files_checked": report.get("files_checked"),
                "manifest_digest_matches": report.get("manifest_digest_matches"),
                "usage": manifest.get("usage"),
            }
        return {
            "role": role,
            "purpose": purpose,
            "datasets": list(datasets),
            "manifest_sha256": digests,
            "speakers": speakers,
            "verification": reports,
        }

    def _delegate_usage(self, manifest: Mapping, purpose: str, dataset: str) -> None:
        """Ask ``freeze_manifest`` whether this dataset may be used this way."""
        try:
            self._call("assert_usable_for", manifest, purpose)
        except Refusal:
            raise
        except SystemExit as exc:  # build_human_dataset.Refused is a SystemExit
            raise SealedSpeakerLeakRefused(f"{dataset}: {exc}") from exc
        except Exception as exc:
            usage = str(manifest.get("usage"))
            if usage == "sealed-evaluation":
                raise SealedSpeakerLeakRefused(
                    f"{dataset} is marked usage='sealed-evaluation' and was offered for "
                    f"{purpose}. {exc}"
                ) from exc
            raise ManifestNotFrozenRefused(
                f"{dataset} cannot be used for {purpose}: {exc}"
            ) from exc

    def _speakers_of(self, dataset: str, manifest: Mapping) -> set[str]:
        """Every speaker the manifest implicates, including the dataset itself.

        A per-speaker dataset is named for its speaker, so the dataset name is
        the first speaker. Anything the manifest additionally lists is added
        rather than trusted to be the same one.
        """
        named = {dataset}
        declared = manifest.get("speakers")
        if isinstance(declared, (list, tuple)):
            named |= {str(name) for name in declared}
        return named

    def _assert_speakers_admissible(self, named: set[str], *, role: str, dataset: str) -> None:
        sealed_splits = frozenset(self._call("sealed_splits"))
        for speaker in sorted(named):
            try:
                split = str(self._call("registry_split", speaker))
            except Refusal:
                raise
            except SystemExit as exc:
                raise ManifestNotFrozenRefused(
                    f"{dataset}: {exc}"
                ) from exc
            if split in sealed_splits or speaker in self.plan.sealed:
                raise SealedSpeakerLeakRefused(
                    f"{speaker} is bound to the sealed split {split!r} and appears in "
                    f"the {role} dataset {dataset}. A speaker who has been fitted on or "
                    "selected against is no longer unseen, and re-recording them does "
                    "not restore what the seal was worth."
                )
            if role == "train" and (split == "validation" or speaker in self.plan.validation):
                raise ValidationSpeakerLeakRefused(
                    f"{speaker} is the validation speaker (registry split {split!r}) and "
                    f"appears in the training dataset {dataset}. The threshold and the "
                    "epoch would then be chosen on a voice the model was fitted on."
                )
            if role == "validation" and (split == "train" or speaker in self.plan.train):
                # The same leak from the other side, and it needs the same name:
                # what makes the validation number worth having is that the model
                # has not heard the voice, and it does not matter which manifest
                # the overlap was written into.
                raise ValidationSpeakerLeakRefused(
                    f"{speaker} is a training speaker (registry split {split!r}) and "
                    f"appears in the validation dataset {dataset}. Train and validation "
                    "are disjoint by speaker or the validation result measures memory."
                )
            expected = {"train": "train", "validation": "validation"}.get(role)
            if expected is not None and split != expected:
                raise ManifestNotFrozenRefused(
                    f"{speaker} is bound to the {split!r} split and was offered as "
                    f"{role} data in {dataset}. A speaker's split is a decision recorded "
                    "in the registry before any of their audio is read."
                )

    def _assert_manifest_verified(self, dataset: str, paths: DatasetPaths,
                                  report: Mapping) -> None:
        if report.get("manifest_digest_matches") is not True:
            raise ManifestNotFrozenRefused(
                f"{dataset}: the manifest's own digest does not match its body. Somebody "
                "edited the record rather than the data."
            )
        for key in ("missing", "changed", "extra"):
            offending = list(report.get(key) or ())
            if offending:
                raise ManifestNotFrozenRefused(
                    f"{dataset}: {len(offending)} {key} file(s) under {paths.root} "
                    f"({offending[:5]}). The frozen manifest no longer describes the "
                    "bytes a result would be traced to."
                )
        if report.get("passed") is not True:
            raise ManifestNotFrozenRefused(
                f"{dataset}: {paths.manifest} did not verify against {paths.root}"
            )

    def _dataset_paths(self, dataset: str) -> DatasetPaths:
        paths = self.inputs.datasets.get(dataset)
        if paths is None:
            raise IncompleteEvidenceRefused(
                f"no manifest and root were supplied for {dataset}. A state that cannot "
                "read the dataset it is verifying cannot report that it verified."
            )
        return paths

    # -- state 3 -----------------------------------------------------------

    def _state_3(self) -> dict:
        """The sealed sets are still sealed — proved from the record, not the data.

        Nothing here reads a sealed manifest, hashes a sealed file or lists a
        sealed directory. ``ROUND8_DESIGN.md`` puts all three after the 5/5 gate,
        so the proof at this point has to come from evidence that already exists:
        the registry's binding, the seal ledger, and what states 1 and 2 wrote
        down.
        """
        sealed_splits = frozenset(self._call("sealed_splits"))
        bindings: dict[str, str] = {}
        for dataset in self.plan.sealed:
            try:
                split = str(self._call("registry_split", dataset))
            except Refusal:
                raise
            except SystemExit as exc:
                raise SealedSpeakerLeakRefused(
                    f"{dataset} is declared sealed by the predeclaration and the speaker "
                    f"registry does not know it: {exc}"
                ) from exc
            if split not in sealed_splits:
                raise SealedSpeakerLeakRefused(
                    f"{dataset} is declared sealed in round8_config.json and the registry "
                    f"binds it to {split!r}, which is not one of {sorted(sealed_splits)}. "
                    "Two records disagree about whether a voice has been spent; neither "
                    "may be assumed to be the right one."
                )
            status = self._call("seal_status", dataset)
            if status is not None:
                raise SealAlreadySpentRefused(
                    f"{dataset} already has an opening on the record "
                    f"(opened {status.get('opened_utc')} against candidate "
                    f"{status.get('candidate_id')!r}). It is spent, and a candidate that "
                    "was tuned after it was opened cannot be qualified on it."
                )
            bindings[dataset] = split

        # Nothing upstream may have named a sealed set. States 1 and 2 wrote down
        # exactly which datasets and speakers they touched, so this is checkable
        # rather than assertable.
        upstream: set[str] = set()
        for number in (1, 2):
            recorded = self.journal.evidence(number)
            upstream |= {str(name) for name in recorded.get("datasets", ())}
            for names in recorded.get("speakers", {}).values():
                upstream |= {str(name) for name in names}
        leaked = sorted(upstream & set(self.plan.sealed))
        if leaked:
            raise SealedSpeakerLeakRefused(
                f"{leaked} are sealed and were named by the training or validation "
                "states. A sealed set that has been read as training or validation data "
                "is not sealed, whatever the config still says."
            )
        return {
            "sealed": list(self.plan.sealed),
            "registry_bindings": bindings,
            "openings_on_record": {dataset: None for dataset in self.plan.sealed},
            "manifests_read": [],
            "why_no_manifest_was_read": (
                "ROUND8_DESIGN.md: no sealed manifest is read, hashed against or listed "
                "until one arm has passed 5/5 on E005. That happens at state 10."
            ),
        }

    # -- state 4 -----------------------------------------------------------

    def _state_4(self) -> dict:
        """Fresh weights: nothing retired, nothing left over, one seed.

        The retired-artifact registry is loaded here even when no checkpoint is
        offered. A state that claims "this started from nothing" without being
        able to recognise a retired artifact has claimed something it could not
        have checked.
        """
        retired = dict(self._call("retired_digests"))
        if not retired:
            raise SyntheticInitializationRefused(
                "the retired-artifact registry is empty. An empty registry recognises "
                "nothing and would pass the shipped synthetic pair as a fresh start."
            )
        launched: dict[str, dict] = {}
        for arm in self.plan.arms:
            out = self._arm_output(arm)
            leftovers = sorted(p.name for p in out.iterdir()) if out.is_dir() else []
            if leftovers:
                raise IncompatibleCheckpointRefused(
                    f"arm {arm.id}: {out} is not empty ({leftovers[:5]}). "
                    f"train_model resumes from <out>/{CHECKPOINT_NAME} automatically, so "
                    "a non-empty output directory at launch is an undeclared warm start "
                    "— the design requires each arm's directory to be empty and the "
                    "epoch-0 checkpoint to be the record that it started from nothing."
                )
            record = {
                "schema_version": SCHEMA_VERSION,
                "tool": TOOL,
                "plan_digest": self.plan.digest(),
                "arm": arm.id,
                "channels": list(arm.channels),
                "seed": self.plan.seed,
                "fresh": True,
                "written_utc": _utc_now(),
            }
            out.mkdir(parents=True, exist_ok=True)
            (out / INIT_RECORD_NAME).write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            launched[arm.id] = {"out": str(out), "init_record": INIT_RECORD_NAME}

        offered = self.inputs.resume_checkpoint
        resume: dict | None = None
        if offered is not None:
            arm = self._arm_of_output(offered)
            resume = self._admit_checkpoint(offered, arm, retired=retired)
        return {
            "fresh": True,
            "seed": self.plan.seed,
            "arms": launched,
            "retired_artifacts_known": len(retired),
            "explicit_resume": resume,
        }

    def _arm_output(self, arm: Arm) -> Path:
        out = self.inputs.arm_output.get(arm.id)
        if out is None:
            raise IncompleteEvidenceRefused(
                f"no output directory was supplied for arm {arm.id}. Every predeclared "
                "arm runs; one without a place to write is one that will not."
            )
        return out

    def _arm_of_output(self, checkpoint: Path) -> Arm | None:
        for arm in self.plan.arms:
            out = self.inputs.arm_output.get(arm.id)
            if out is not None and checkpoint.parent == out:
                return arm
        return None

    def _admit_checkpoint(self, checkpoint: Path, arm: Arm | None, *,
                          retired: Mapping[str, str]) -> dict:
        """Decide whether a checkpoint may initialise anything.

        Order matters and is the same order ``refuse_synthetic_initialization``
        argues for: the bytes are judged first. A retired candidate copied into a
        clean directory under an innocent name, with a contract written beside
        it, passes every check that reads a path or a file next to the file. Its
        SHA-256 does not move with it.

        Then the module guard runs anyway — it knows about synthetic trees and
        dataset contracts and this controller does not reimplement either — and
        only then is the plan sidecar read.
        """
        digest = str(self._call("file_digest", checkpoint)).lower()
        if digest in retired:
            raise SyntheticInitializationRefused(
                f"{checkpoint} is byte-identical to the retired artifact "
                f"{retired[digest]!r} (sha256 {digest}). A retired candidate used as an "
                "initialization is a synthetic model with real data fine-tuned onto it, "
                "and the artifact looks entirely human-only afterwards. Refused wherever "
                "it sits and whatever it is called."
            )
        try:
            self._call("refuse_synthetic_initialization", checkpoint)
        except Refusal:
            raise
        except SystemExit as exc:  # build_human_dataset.Refused
            raise SyntheticInitializationRefused(
                f"{checkpoint} was refused as an initialization: {exc}"
            ) from exc
        except Exception as exc:
            raise SyntheticInitializationRefused(
                f"{checkpoint} could not be shown to be a human-only start: {exc}"
            ) from exc

        sidecar = checkpoint.parent / INIT_RECORD_NAME
        if not sidecar.is_file():
            raise IncompatibleCheckpointRefused(
                f"{checkpoint} has no {INIT_RECORD_NAME} beside it, so nothing says which "
                "plan, arm or seed it belongs to. A resume that cannot name its own run "
                "is not a resume."
            )
        try:
            record = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IncompatibleCheckpointRefused(f"{sidecar} is not readable as JSON: {exc}") from exc
        if str(record.get("plan_digest")) != self.plan.digest():
            raise IncompatibleCheckpointRefused(
                f"{checkpoint} belongs to plan {str(record.get('plan_digest'))[:12]}... and "
                f"this run is plan {self.plan.digest()[:12]}.... Continuing would mix two "
                "predeclarations in one artifact."
            )
        if int(record.get("seed", -1)) != self.plan.seed:
            raise IncompatibleCheckpointRefused(
                f"{checkpoint} was seeded {record.get('seed')!r} and the plan seeds "
                f"{self.plan.seed}. The draw order differs, so the epoch numbering would "
                "stop describing what happened."
            )
        recorded_arm = str(record.get("arm", ""))
        planned = self.plan.arm(recorded_arm)
        if planned is None:
            raise IncompatibleCheckpointRefused(
                f"{checkpoint} claims arm {recorded_arm!r}, which is not one of "
                f"{[a.id for a in self.plan.arms]}"
            )
        if tuple(int(c) for c in record.get("channels", ())) != planned.channels:
            raise IncompatibleCheckpointRefused(
                f"{checkpoint} records channels {record.get('channels')!r} for arm "
                f"{recorded_arm}, which the plan declares as {list(planned.channels)}. "
                "Loading it would either fail on shapes or silently train a width nobody "
                "predeclared."
            )
        if arm is not None and recorded_arm != arm.id:
            raise IncompatibleCheckpointRefused(
                f"{checkpoint} sits in arm {arm.id}'s output directory and records arm "
                f"{recorded_arm!r}"
            )
        return {
            "checkpoint": str(checkpoint),
            "sha256": digest,
            "arm": recorded_arm,
            "seed": int(record["seed"]),
            "channels": list(planned.channels),
        }

    # -- state 5 -----------------------------------------------------------

    def _state_5(self) -> dict:
        """The three predeclared widths, in order, at one seed, and no fourth.

        The stop condition is copied into the record verbatim. It is not enforced
        here — it is a rule about what happens *after* state 9 refuses — and a
        rule quoted next to the run it governs is harder to forget than one in a
        document.
        """
        retired = dict(self._call("retired_digests"))
        results: list[dict] = []
        for arm in self.plan.arms:
            out = self._arm_output(arm)
            checkpoint = out / CHECKPOINT_NAME
            resumed = None
            if checkpoint.exists():
                # train_model would pick this up by itself. Admit it first, or a
                # crashed run becomes a warm start from something unexamined.
                resumed = self._admit_checkpoint(checkpoint, arm, retired=retired)
            reported = dict(
                self._call(
                    "train_arm",
                    {
                        "arm": arm.id,
                        "channels": list(arm.channels),
                        "parameters": arm.parameters,
                        "seed": self.plan.seed,
                        "out": str(out),
                        "plan_digest": self.plan.digest(),
                        "resumed_from": resumed,
                    },
                )
            )
            self._assert_arm_matches_plan(arm, reported)
            weights = str(reported.get("weights_sha256", "")).lower()
            if len(weights) != 64:
                raise IncompleteEvidenceRefused(
                    f"arm {arm.id} reported weights_sha256 "
                    f"{reported.get('weights_sha256')!r}. The selected candidate's bytes "
                    "are what both exports and the freeze are traced to; unhashed "
                    "weights make every later digest a claim about nothing."
                )
            if weights in retired:
                raise SyntheticInitializationRefused(
                    f"arm {arm.id} produced weights byte-identical to the retired "
                    f"artifact {retired[weights]!r}. That is not a trained model, it is "
                    "the retired one arriving under a new name."
                )
            results.append(
                {
                    "arm": arm.id,
                    "channels": list(arm.channels),
                    "parameters": arm.parameters,
                    "seed": self.plan.seed,
                    "weights_sha256": weights,
                    "resumed_from": resumed,
                    "reported": {
                        key: value
                        for key, value in reported.items()
                        if key not in {"weights_sha256"}
                    },
                }
            )
        if len(results) != len(self.plan.arms):
            raise PlanChangedRefused(
                f"{len(results)} arms ran for {len(self.plan.arms)} predeclared. Three "
                "arms is the whole matrix."
            )
        return {
            "arms": results,
            "seed": self.plan.seed,
            "stop_condition": self.plan.stop_condition,
        }

    def _assert_arm_matches_plan(self, arm: Arm, reported: Mapping) -> None:
        if str(reported.get("arm")) != arm.id:
            raise PlanChangedRefused(
                f"the trainer reports arm {reported.get('arm')!r} for predeclared arm "
                f"{arm.id}. An arm that is not the one asked for is a fourth arm."
            )
        if tuple(int(c) for c in reported.get("channels", ())) != arm.channels:
            raise PlanChangedRefused(
                f"arm {arm.id}: the trainer reports channels {reported.get('channels')!r}, "
                f"the plan declares {list(arm.channels)}. Capacity is the one axis this "
                "round varies, so it is the one number that cannot drift."
            )
        if int(reported.get("seed", -1)) != self.plan.seed:
            raise PlanChangedRefused(
                f"arm {arm.id}: the trainer reports seed {reported.get('seed')!r}, the "
                f"plan declares {self.plan.seed}"
            )

    # -- state 6 -----------------------------------------------------------

    def _state_6(self) -> dict:
        """One candidate, one threshold, chosen on E005 and nothing else."""
        arms = self.journal.evidence(5)["arms"]
        selection = dict(
            self._call(
                "select_candidate",
                arms,
                {
                    "selected_on": list(self.plan.validation),
                    "never_selected_on": list(self.plan.sealed),
                    "confirmation_frames": self.plan.confirmation_frames,
                },
            )
        )
        used = tuple(str(name) for name in selection.get("selected_on", ()))
        if not used:
            raise IncompleteEvidenceRefused(
                "the selection did not record which datasets it was made on. 'Selected on "
                "validation only' is the claim, and a selection that does not say what it "
                "saw cannot support it."
            )
        spent = sorted(set(used) & set(self.plan.sealed))
        if spent:
            raise SealedSpeakerLeakRefused(
                f"the candidate and threshold were selected using {spent}, which are "
                "sealed. A threshold chosen on a sealed set has spent it, and the sealed "
                "result afterwards measures the tuning rather than the model."
            )
        if set(used) != set(self.plan.validation):
            raise PlanChangedRefused(
                f"the selection used {sorted(used)}; the predeclaration selects on "
                f"{list(self.plan.validation)} only — threshold, epoch and candidate all "
                "three."
            )
        candidate = str(selection.get("candidate_id", ""))
        chosen = self.plan.arm(candidate)
        if chosen is None:
            raise PlanChangedRefused(
                f"the selected candidate {candidate!r} is not one of the predeclared arms "
                f"{[a.id for a in self.plan.arms]}"
            )
        threshold = selection.get("threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            raise IncompleteEvidenceRefused(
                f"the selected threshold is {threshold!r}, not a number"
            )
        threshold = float(threshold)
        if not 0.0 < threshold < 1.0:
            raise IncompleteEvidenceRefused(
                f"the selected threshold {threshold!r} is not a probability the engine "
                "could ever cross"
            )
        weights = {row["arm"]: row["weights_sha256"] for row in arms}
        reported_weights = str(selection.get("weights_sha256", "")).lower()
        if reported_weights != weights.get(candidate):
            raise IncompleteEvidenceRefused(
                f"the selection names candidate {candidate} with weights "
                f"{reported_weights[:12]}... and state 5 recorded "
                f"{str(weights.get(candidate))[:12]}... for that arm. Both exports and "
                "the freeze are traced to these bytes."
            )
        return {
            "candidate_id": candidate,
            "channels": list(chosen.channels),
            "parameters": chosen.parameters,
            "weights_sha256": reported_weights,
            # Hex, not a shortened decimal. The threshold is an observed negative
            # score such as 0.9990234375, and round-tripping it through four
            # decimals silently moves the operating point everything downstream is
            # frozen at.
            "threshold_hex": threshold.hex(),
            "threshold_decimal": repr(threshold),
            "selected_on": sorted(used),
            "selection_report": {
                key: value
                for key, value in selection.items()
                if key not in {"threshold", "weights_sha256"}
            },
        }

    # -- state 7 -----------------------------------------------------------

    def _state_7(self) -> dict:
        """Both artifacts, from one set of weights, neither of them retired."""
        retired = dict(self._call("retired_digests"))
        selected = self.journal.evidence(6)
        weights = str(selected["weights_sha256"]).lower()
        exported: dict[str, dict] = {}
        for backend, filename in self.plan.artifacts:
            destination = self.inputs.artifact_dir / filename
            try:
                reported = dict(
                    self._call(
                        "export_artifact",
                        backend,
                        destination,
                        {
                            "candidate_id": selected["candidate_id"],
                            "weights_sha256": weights,
                            "channels": list(selected["channels"]),
                        },
                    )
                )
            except Refusal:
                raise
            except Exception as exc:
                # An export that failed is a backend that is not there, and the
                # product ships both. Named, so it cannot be read as a transient.
                raise BackendMissingRefused(
                    f"the {backend} export did not produce an artifact: {exc}. Both files "
                    "ship and macOS ARM64 loads the tflite one, so half a model is not a "
                    "candidate."
                ) from exc
            produced = str(reported.get("weights_sha256", "")).lower()
            if produced != weights:
                raise WeightsDivergedRefused(
                    f"the {backend} export reports weights {produced[:12]}... and the "
                    f"frozen candidate is {weights[:12]}.... Both artifacts come from one "
                    "checkpoint or the parity target is comparing two models."
                )
            digest = str(reported.get("artifact_sha256", "")).lower()
            if len(digest) != 64:
                raise IncompleteEvidenceRefused(
                    f"the {backend} export reports artifact_sha256 "
                    f"{reported.get('artifact_sha256')!r}; the freeze pins both artifact "
                    "digests and cannot pin a non-digest"
                )
            if digest in retired:
                raise SyntheticInitializationRefused(
                    f"the {backend} export is byte-identical to the retired artifact "
                    f"{retired[digest]!r}. That is the shipped synthetic-era model, not "
                    "an export of this candidate."
                )
            exported[backend] = {
                "artifact": str(destination),
                "filename": filename,
                "artifact_sha256": digest,
            }
        missing = sorted(set(BACKENDS) - set(exported))
        if missing:
            raise BackendMissingRefused(
                f"{missing} was not exported. The product ships both files and macOS "
                "ARM64 loads the tflite one, so exporting one is exporting half a model."
            )
        if len({row["artifact_sha256"] for row in exported.values()}) != len(exported):
            raise WeightsDivergedRefused(
                "two backends reported the same artifact digest, so one of the two files "
                "was never written"
            )
        return {
            "candidate_id": selected["candidate_id"],
            "weights_sha256": weights,
            "artifacts": exported,
            "artifact_sha256": {
                backend: row["artifact_sha256"] for backend, row in exported.items()
            },
        }

    # -- state 8 -----------------------------------------------------------

    def _state_8(self) -> dict:
        """Both artifacts through the real engine, each on the backend it was asked for.

        ``tools.wake_word.default_inference_framework`` returns tflite on macOS
        ARM64 and onnx everywhere else, and the engine will fall back when the
        runtime it was asked for is not importable. The fallback is *silent*, and
        a "tflite" latency figure or a "tflite" false-reject rate produced by the
        ONNX backend is a false statement about the thing that ships. So the
        engine's own ``_model.inference_framework`` is asserted against the
        backend that was requested, exactly as
        ``tests/tools/test_wake_word_reset_determinism.py`` does before it draws
        any conclusion.
        """
        selected = self.journal.evidence(6)
        artifacts = self.journal.evidence(7)["artifacts"]
        threshold_hex = str(selected["threshold_hex"])
        threshold = float.fromhex(threshold_hex)
        runs: dict[str, dict] = {}
        for backend in BACKENDS:
            row = artifacts.get(backend)
            if row is None:
                raise BackendMissingRefused(
                    f"state 7 recorded no {backend} artifact, so it cannot be measured"
                )
            try:
                reported = dict(
                    self._call(
                        "run_runtime",
                        backend,
                        Path(row["artifact"]),
                        {
                            "threshold": threshold,
                            "threshold_hex": threshold_hex,
                            "confirmation_frames": self.plan.confirmation_frames,
                            "artifact_sha256": row["artifact_sha256"],
                            "datasets": list(self.plan.validation),
                        },
                    )
                )
            except Refusal:
                raise
            except Exception as exc:
                raise BackendMissingRefused(
                    f"the {backend} backend could not be measured through the product "
                    f"runtime: {exc}. A backend that will not load is a backend the "
                    "candidate has no result for, and both must pass independently."
                ) from exc
            engine = str(reported.get("engine_inference_framework", ""))
            if engine != backend:
                raise BackendFallbackRefused(
                    f"{backend} was requested and the engine reports "
                    f"{engine or 'nothing'}. A backend that quietly becomes the other one "
                    f"makes every {backend!r} figure a statement about a build that was "
                    "never measured; the engine's own inference_framework is the only "
                    "thing that knows which library actually ran."
                )
            if str(reported.get("artifact_sha256", "")).lower() != row["artifact_sha256"]:
                raise IncompleteEvidenceRefused(
                    f"the {backend} run measured artifact "
                    f"{str(reported.get('artifact_sha256'))[:12]}... and state 7 exported "
                    f"{row['artifact_sha256'][:12]}..."
                )
            if str(reported.get("threshold_hex", "")) != threshold_hex:
                raise IncompleteEvidenceRefused(
                    f"the {backend} run used threshold "
                    f"{reported.get('threshold_hex')!r} and the selection chose "
                    f"{threshold_hex}. A measurement at another operating point is a "
                    "measurement of another candidate."
                )
            if int(reported.get("confirmation_frames", -1)) != self.plan.confirmation_frames:
                raise PlanChangedRefused(
                    f"the {backend} run used {reported.get('confirmation_frames')!r} "
                    f"confirmation frames; the runtime contract fixes "
                    f"{self.plan.confirmation_frames}"
                )
            measured = set(str(name) for name in reported.get("datasets", ()))
            spent = sorted(measured & set(self.plan.sealed))
            if spent:
                raise SealedSpeakerLeakRefused(
                    f"the {backend} run measured {spent}, which are sealed. Nothing "
                    "sealed is streamed before the freeze."
                )
            runs[backend] = dict(reported)
        missing = sorted(set(BACKENDS) - set(runs))
        if missing:
            raise BackendMissingRefused(f"{missing} was not measured through the runtime")
        return {
            "candidate_id": selected["candidate_id"],
            "threshold_hex": threshold_hex,
            "confirmation_frames": self.plan.confirmation_frames,
            "backends": sorted(runs),
            "engine_inference_framework": {
                backend: run.get("engine_inference_framework") for backend, run in runs.items()
            },
            "runs": runs,
        }

    # -- state 9 -----------------------------------------------------------

    def _state_9(self) -> dict:
        """The harness declares 5/5, or this stops with the harness's own words.

        This is the state the whole controller exists to get right, and the only
        honest implementation is a very short one. ``qualify.build_report``
        decides demonstrability; on Round 8's projected quantities it refuses
        targets 2 and 3 for want of evidence — 11.66 h of recorded speech against
        the 15 the 0.2/h target needs, 112 near-phrase utterances against 150 —
        and prints ``REFUSED`` rather than ``QUALIFIED``.

        So there is no arithmetic here, no threshold on the number of targets, no
        "not contradicted" that could be read as a pass, and no flag that turns a
        refusal into one. The verdict line and every refusal are quoted into the
        exception, because a controller that reported "validation 5/5" while the
        harness said NOT DEMONSTRATED would be the worst defect this design could
        have.
        """
        selected = self.journal.evidence(6)
        measured = self.journal.evidence(8)
        report = dict(
            self._call(
                "qualify_report",
                {
                    "candidate_id": selected["candidate_id"],
                    "threshold_hex": selected["threshold_hex"],
                    "confirmation_frames": self.plan.confirmation_frames,
                    "backends": list(BACKENDS),
                    "runs": measured["runs"],
                    "datasets": list(self.plan.validation),
                },
            )
        )
        official = report.get("official")
        if not isinstance(official, Mapping):
            raise IncompleteEvidenceRefused(
                "the qualification report has no official block. The as-recorded block is "
                "the only one that may carry a verdict, and a report without it has not "
                "been asked the question."
            )
        for key in (
            "qualified_5_of_5",
            "verdict",
            "targets_demonstrated",
            "targets_total",
            "underpowered_targets",
            "refusals",
            "targets",
            "parity",
            "backends",
        ):
            if key not in official:
                raise IncompleteEvidenceRefused(
                    f"the official block carries no {key!r}. A report that does not state "
                    "its own verdict, its own refusals and its own sample coverage cannot "
                    "be read as a pass by anything downstream."
                )
        if len(list(official["targets"])) != len(self.plan.targets):
            raise IncompleteEvidenceRefused(
                f"the report covers {len(list(official['targets']))} targets and the "
                f"predeclaration has {len(self.plan.targets)}. A missing target is not a "
                "met one."
            )
        if set(str(b) for b in official["backends"]) != set(BACKENDS):
            raise BackendMissingRefused(
                f"the report covers backends {sorted(official['backends'])}; both of "
                f"{sorted(BACKENDS)} must pass independently, and an average over the two "
                "would let a good ONNX result carry a bad tflite one onto a Mac."
            )
        datasets = sorted(str(name) for name in official.get("evidence", {}).get("datasets", ()))
        if not datasets:
            raise IncompleteEvidenceRefused(
                "the report names no datasets, so nothing says what the verdict is about"
            )
        spent = sorted(set(datasets) & set(self.plan.sealed))
        if spent:
            raise SealedSpeakerLeakRefused(
                f"the validation gate was computed over {spent}, which are sealed. The "
                "gate exists to decide whether those sets may be opened at all."
            )
        if set(datasets) != set(self.plan.validation):
            raise PlanChangedRefused(
                f"the gate was computed over {datasets}; the predeclared gate is 5/5 on "
                f"{list(self.plan.validation)}"
            )

        parity = official["parity"]
        if "detection_disagreements" not in parity:
            raise IncompleteEvidenceRefused(
                "the parity result does not report a detection-disagreement count, which "
                "is the one figure the design makes fatal"
            )
        disagreements = int(parity["detection_disagreements"])
        if disagreements != 0:
            raise DetectionDisagreementRefused(
                f"{disagreements} detection disagreement(s) between ONNX and TFLite over "
                f"{parity.get('windows_compared')} window(s). One build wakes and the "
                "other does not; the design makes that fatal and it is not a tolerance."
            )

        if official["qualified_5_of_5"] is not True:
            raise QualificationRefused(
                "the qualification harness did not declare 5/5, so the sealed sets stay "
                "sealed and Round 8 reports a failure. Its verdict, verbatim:\n"
                f"  {official['verdict']}\n"
                f"  targets demonstrated: {official['targets_demonstrated']}"
                f"/{official['targets_total']}\n"
                f"  underpowered targets: {list(official['underpowered_targets'])}\n"
                + "".join(f"  refusal: {why}\n" for why in official["refusals"])
                + f"  stop condition: {self.plan.stop_condition}"
            )
        return {
            "qualified_5_of_5": True,
            "verdict": str(official["verdict"]),
            "targets_demonstrated": int(official["targets_demonstrated"]),
            "targets_total": int(official["targets_total"]),
            "underpowered_targets": list(official["underpowered_targets"]),
            "detection_disagreements": disagreements,
            "windows_compared": parity.get("windows_compared"),
            "datasets": datasets,
            "backends": sorted(str(b) for b in official["backends"]),
            "decided_by": "scripts/wakeword/qualify.py",
        }

    # -- state 10 ----------------------------------------------------------

    def _state_10(self) -> dict:
        """Everything stops moving, and only now are the sealed manifests hashed.

        This is the first state that reads a sealed manifest, and it is the first
        one allowed to: ``ROUND8_DESIGN.md`` puts "each sealed manifest" in the
        freeze list and forbids reading one before 5/5. Hashing the manifest is
        not scoring the audio, and the freeze has to pin the bytes a sealed result
        will be traced to before that result exists.
        """
        selected = self.journal.evidence(6)
        exported = self.journal.evidence(7)
        measured = self.journal.evidence(8)
        qualified = self.journal.evidence(9)
        if qualified.get("qualified_5_of_5") is not True:
            raise SealBarrierRefused(
                "state 9 did not record a 5/5, so there is nothing to freeze"
            )

        manifests: dict[str, str] = {}
        for number in (1, 2):
            manifests.update(
                {str(k): str(v) for k, v in self.journal.evidence(number)["manifest_sha256"].items()}
            )
        sealed_digests: dict[str, str] = {}
        for dataset in self.plan.sealed:
            paths = self._dataset_paths(dataset)
            manifest = dict(self._call("load_manifest", paths.manifest))
            if str(manifest.get("dataset")) != dataset:
                raise ManifestNotFrozenRefused(
                    f"{paths.manifest} describes {manifest.get('dataset')!r}, not {dataset!r}"
                )
            # The sealed sets are read here for one purpose only. Ask the module
            # that owns usage whether that purpose is permitted.
            self._delegate_usage(manifest, "evaluation", dataset)
            report = dict(self._call("verify_manifest", paths.manifest, paths.root))
            self._assert_manifest_verified(dataset, paths, report)
            digest = str(manifest.get("manifest_sha256", ""))
            if len(digest) != 64:
                raise ManifestNotFrozenRefused(
                    f"{dataset}: manifest_sha256 is {digest!r}, not a sha256"
                )
            sealed_digests[dataset] = digest
        manifests.update(sealed_digests)

        if not self.inputs.runtime_version or not self.inputs.repo_commit:
            raise IncompleteEvidenceRefused(
                "the freeze needs the runtime version and the repository commit. A sealed "
                "measurement whose code nobody can name is traceable to nothing."
            )
        body = {
            "candidate_id": selected["candidate_id"],
            "threshold_hex": selected["threshold_hex"],
            "confirmation_frames": self.plan.confirmation_frames,
            "runtime_version": self.inputs.runtime_version,
            "repo_commit": self.inputs.repo_commit,
            "manifest_sha256": manifests,
            "artifact_sha256": dict(exported["artifact_sha256"]),
            "note": (
                f"Round 8 plan {self.plan.digest()}; config sha256 "
                f"{self.inputs.config_sha256 or 'unrecorded'}; qualified by "
                f"{qualified['verdict']}"
            ),
        }
        freeze = self._call("freeze_record", body)
        try:
            freeze.assert_complete()
        except Refusal:
            raise
        except Exception as exc:
            raise IncompleteEvidenceRefused(
                f"the candidate is not frozen, so no sealed set may be opened against it: "
                f"{exc}"
            ) from exc
        frozen_hex = float(freeze.threshold).hex()
        if frozen_hex != str(selected["threshold_hex"]):
            raise ThresholdMovedAfterSealRefused(
                f"the freeze pins threshold {frozen_hex} and the validation selection "
                f"chose {selected['threshold_hex']}. The operating point moved between "
                "being measured and being frozen."
            )
        if str(freeze.candidate_id) != str(selected["candidate_id"]):
            raise IncompleteEvidenceRefused(
                f"the freeze names candidate {freeze.candidate_id!r} and the selection "
                f"chose {selected['candidate_id']!r}"
            )
        digest = str(freeze.digest())
        sealed = self.journal.seal(
            threshold_hex=frozen_hex,
            freeze_digest=digest,
            candidate_id=str(freeze.candidate_id),
            freeze_body=body,
        )
        return {
            "freeze_digest": digest,
            "candidate_id": str(freeze.candidate_id),
            "threshold_hex": frozen_hex,
            "threshold_decimal": sealed["threshold_decimal"],
            "artifact_sha256": dict(exported["artifact_sha256"]),
            "manifest_sha256": manifests,
            "sealed_manifest_sha256": sealed_digests,
            "runtime_version": self.inputs.runtime_version,
            "repo_commit": self.inputs.repo_commit,
            "engine_inference_framework": dict(measured["engine_inference_framework"]),
            "next": (
                "Nothing else runs automatically. Opening a sealed set is a separate "
                f"command: open_sealed(dataset, authorization={AUTHORIZATION_TEMPLATE!r})."
            ),
        }

    # -- state 11, behind its own door ------------------------------------

    def open_sealed(self, dataset: str, *, authorization: str) -> dict:
        """Spend one sealed set, once, against the frozen candidate.

        Not reachable from :meth:`advance` or :meth:`run_state`. Three
        independent things have to be true and none of them is a default: the
        first ten states have succeeded and the freeze is on the record, the
        dataset is one the predeclaration sealed, and the caller has typed the
        phrase that names *this* dataset. A generic confirmation flag becomes a
        habit; a sentence with E006 in it does not.
        """
        state = SEAL_STATE
        if dataset not in self.plan.sealed:
            raise SealBarrierRefused(
                f"{dataset!r} is not one of the sealed sets {list(self.plan.sealed)}"
            )
        expected = AUTHORIZATION_TEMPLATE.format(dataset=dataset)
        if str(authorization).strip() != expected:
            raise SealBarrierRefused(
                f"opening {dataset} requires the exact authorization {expected!r}; got "
                f"{str(authorization).strip()!r}. Opening a sealed set publishes the one "
                "measurement nobody has tuned against and nothing puts it back, so it "
                "cannot be reached by continuing a sequence or by a flag that means yes "
                "to anything."
            )
        self._assert_reached(state.number)
        # Per dataset, not per state. The design opens E002, E006 and E007 once
        # each against one freeze, so a second dataset is the same operation
        # continuing and a second opening of the *same* dataset is not.
        for entry in self.journal.transitions:
            if (
                entry.get("state") == state.number
                and entry.get("status") == "succeeded"
                and str(entry.get("evidence", {}).get("dataset")) == dataset
            ):
                raise SealAlreadySpentRefused(
                    f"{dataset} was opened on {entry.get('recorded_utc')} in this journal. "
                    "A measured sealed set is spent and may not be measured again for a "
                    "different candidate."
                )
        sealed = self.journal.sealed
        if sealed is None:
            raise SealBarrierRefused(
                "no freeze is on the record. State 10 pins the candidate, the threshold "
                "at full precision, both artifact digests and every manifest digest, and "
                "a sealed set may not be opened against something that can still change."
            )
        qualified = self.journal.evidence(9)
        if qualified.get("qualified_5_of_5") is not True:
            raise SealBarrierRefused(
                "state 9 did not record a 5/5. If no arm passes, the sealed sets stay "
                "sealed and Round 8 reports a failure."
            )
        self._called = []
        try:
            evidence = self._state_11(dataset, sealed)
        except Refusal as refusal:
            self.journal.record(
                state,
                status="refused",
                evidence={},
                delegates_called=self._called,
                detail=f"{type(refusal).__name__}: {refusal}",
            )
            raise
        self._assert_delegated(state)
        return self.journal.record(
            state, status="succeeded", evidence=evidence, delegates_called=self._called
        )

    def _state_11(self, dataset: str, sealed: Mapping) -> dict:
        freeze = self._call("freeze_record", sealed["freeze_body"])
        frozen_hex = float(freeze.threshold).hex()
        if frozen_hex != str(sealed["threshold_hex"]):
            raise ThresholdMovedAfterSealRefused(
                f"the frozen threshold was {sealed['threshold_hex']} and the record now "
                f"reads {frozen_hex}. A threshold re-tuned after the freeze makes the "
                "sealed measurement a measurement of the tuning."
            )
        if str(freeze.digest()) != str(sealed["freeze_digest"]):
            raise ThresholdMovedAfterSealRefused(
                f"the freeze was recorded as {str(sealed['freeze_digest'])[:12]}... and "
                f"now digests to {str(freeze.digest())[:12]}.... Something inside it "
                "changed after it stopped moving."
            )
        if dataset not in dict(freeze.manifest_sha256):
            raise IncompleteEvidenceRefused(
                f"the freeze pins no manifest digest for {dataset}. A sealed result over "
                "bytes nobody hashed is traceable to nothing."
            )
        existing = self._call("seal_status", dataset)
        if existing is not None and str(existing.get("freeze_digest")) != str(freeze.digest()):
            raise SealAlreadySpentRefused(
                f"{dataset} was opened against freeze "
                f"{str(existing.get('freeze_digest'))[:12]}... and this freeze is "
                f"{str(freeze.digest())[:12]}.... A candidate that failed cannot be tuned "
                "and then qualified on the same sealed set."
            )
        # The ledger is the authority on consumption and refuses a second opening
        # under a different freeze itself. Delegated, not restated.
        try:
            entry = dict(self._call("open_seal", dataset, freeze))
        except Refusal:
            raise
        except Exception as exc:
            raise SealAlreadySpentRefused(
                f"{dataset} was not opened: {exc}"
            ) from exc
        return {
            "dataset": dataset,
            "authorization": AUTHORIZATION_TEMPLATE.format(dataset=dataset),
            "freeze_digest": str(freeze.digest()),
            "candidate_id": str(freeze.candidate_id),
            "threshold_hex": frozen_hex,
            "ledger_entry": entry,
            "spent": True,
            "after": (
                "The threshold is not re-tuned on a sealed set. Difficult samples are not "
                "excluded. Nothing is retried. A measured sealed set is spent."
            ),
        }

    # -- reporting ---------------------------------------------------------

    def status_report(self) -> dict:
        rows = []
        for state in ALL_STATES:
            rows.append(
                {
                    "state": state.number,
                    "name": state.name,
                    "purpose": state.purpose,
                    "status": self.journal.status(state.number) or "not-run",
                    "automatic": state in SEQUENCE,
                }
            )
        return {
            "plan_digest": self.plan.digest(),
            "journal": str(self.journal.path),
            "reached": list(self.journal.reached()),
            "sealed": self.journal.sealed,
            "states": rows,
        }


# ── command line ─────────────────────────────────────────────────────────────


def _controller(args: argparse.Namespace) -> Round8Controller:
    config = load_config(args.config)
    delegates = Delegates.default(ledger=args.ledger)
    plan = build_plan(config, delegates)
    journal = Journal.open(args.journal, plan)
    datasets: dict[str, DatasetPaths] = {}
    if args.dataset_root is not None:
        # One directory per dataset, each holding its own frozen manifest. A
        # convention rather than seven flags, and it is deliberately not a glob:
        # only the datasets the predeclaration names are looked for at all, so a
        # stray eighth directory cannot be picked up as data.
        for name in (*plan.train, *plan.validation, *plan.sealed):
            root = args.dataset_root / name
            datasets[name] = DatasetPaths(manifest=root / args.manifest_name, root=root)
    arm_output: dict[str, Path] = {}
    if args.arm_root is not None:
        arm_output = {arm.id: args.arm_root / arm.id for arm in plan.arms}
    inputs = RunInputs(
        datasets=datasets,
        arm_output=arm_output,
        artifact_dir=args.artifacts,
        runtime_version=args.runtime_version,
        repo_commit=args.repo_commit,
        config_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest(),
    )
    return Round8Controller(plan=plan, journal=journal, delegates=delegates, inputs=inputs)


def _print_plan(plan: Plan) -> None:
    print(f"round {plan.round}: plan {plan.digest()}")
    print(f"  train      {list(plan.train)}")
    print(f"  validation {list(plan.validation)}")
    print(f"  sealed     {list(plan.sealed)}")
    print(f"  seed       {plan.seed}")
    for arm in plan.arms:
        print(f"  arm {arm.id}: channels {list(arm.channels)}, {arm.parameters} parameters")
    print(f"  stop condition: {plan.stop_condition}")
    for target_id, limit, comparison in plan.targets:
        print(f"  target {target_id}: {comparison} {limit}")


def _print_status(report: Mapping) -> None:
    print(f"plan {report['plan_digest']}")
    print(f"journal {report['journal']}")
    for row in report["states"]:
        door = "sequence" if row["automatic"] else "separate command"
        print(f"  {row['state']:>2}  {row['status']:<10} {row['name']}  [{door}]")
    if report["sealed"]:
        sealed = report["sealed"]
        print(
            f"  frozen: candidate {sealed['candidate_id']} at "
            f"{sealed['threshold_decimal']} ({sealed['threshold_hex']}), freeze "
            f"{str(sealed['freeze_digest'])[:12]}..."
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--journal", type=Path, default=Path("round8_journal.json"),
        help="where the state machine's transitions are recorded",
    )
    parser.add_argument(
        "--ledger", type=Path, default=Path("round8_seals.json"),
        help="the qualify.py seal ledger; a sealed set listed there is spent",
    )
    parser.add_argument("--artifacts", type=Path, default=Path("candidates"))
    parser.add_argument(
        "--dataset-root", type=Path, default=None,
        help="directory holding one subdirectory per dataset, each with its frozen manifest",
    )
    parser.add_argument("--manifest-name", default="MANIFEST.json")
    parser.add_argument(
        "--arm-root", type=Path, default=None,
        help="directory holding one output subdirectory per predeclared arm",
    )
    parser.add_argument("--runtime-version", default="")
    parser.add_argument("--repo-commit", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("plan", help="print the pinned plan and its digest")
    sub.add_parser("status", help="print which states have succeeded")
    advance = sub.add_parser(
        "advance",
        help=f"run states {SEQUENCE[0].number} to {SEQUENCE[-1].number}; never state "
             f"{SEAL_STATE.number}",
    )
    advance.add_argument(
        "--to", type=int, default=None,
        help=f"stop after this state; must be at most {SEQUENCE[-1].number}",
    )
    opener = sub.add_parser(
        "open-sealed",
        help="spend one sealed set; requires the authorization phrase that names it",
    )
    opener.add_argument("--dataset", required=True)
    opener.add_argument(
        "--authorize", required=True,
        help=f"exactly {AUTHORIZATION_TEMPLATE.format(dataset='<dataset>')!r}",
    )

    args = parser.parse_args(argv)
    try:
        controller = _controller(args)
        if args.command == "plan":
            _print_plan(controller.plan)
            return 0
        if args.command == "status":
            _print_status(controller.status_report())
            return 0
        if args.command == "advance":
            done = controller.advance(to=args.to)
            for entry in done:
                print(f"state {entry['state']} {entry['name']}: {entry['status']}")
            if not done:
                print("nothing to do; every requested state has already succeeded")
            return 0
        if args.command == "open-sealed":
            entry = controller.open_sealed(args.dataset, authorization=args.authorize)
            print(f"state {entry['state']} {entry['name']}: {entry['status']}")
            print(f"  {args.dataset} is now CONSUMED")
            return 0
    except Refusal as refusal:
        print(f"{type(refusal).__name__}: {refusal}", file=sys.stderr)
        return 1
    return 2  # pragma: no cover - argparse requires a subcommand


if __name__ == "__main__":
    raise SystemExit(main())
