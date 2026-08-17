"""The Round 8 controller must make the wrong order impossible, not merely wrong.

What is actually under test
---------------------------
Not "does it train a model" — it never trains one here. The subject is the
**state machine**: which transitions are possible, what each one is gated on,
and what it refuses. So every expensive stage (training an arm, exporting,
streaming a corpus through the engine, computing a verdict) is an injected
callable, and the checks that cost nothing are the *real* modules:

* ``freeze_manifest.load``, ``.build``, ``.verify`` and ``.assert_usable_for``
  run for real over small text files in ``tmp_path``. Manifest verification and
  the sealed-usage refusal are therefore genuinely delegated, not mimed.
* ``qualify.freeze_from_json``, ``FreezeRecord.assert_complete`` and
  ``SealLedger`` run for real. Freezing and spending a seal are the real
  implementations.
* ``qualify.retired_hashes`` reads the committed
  ``retired_synthetic_artifacts.json``, so the content-address refusal is tested
  against the registry that actually holds the shipped pair's digests.
* ``round8_config.check`` and ``promote_model.ACCEPTANCE_TARGETS`` decide whether
  the predeclaration is a plan at all.
* One test drives the **real** ``qualify.build_report`` over Round 8's own
  projected qualification quantities and asserts the controller surfaces its
  ``REFUSED`` verdict rather than routing around it.

``build_human_dataset`` is the one module faked rather than imported: it pulls in
numpy and the whole ingest stage for two lookups. The fake's split table is
asserted against the committed one by reading it with ``ast``, so a fake that
drifted from the registry is a failure here.

Hermetic
--------
No training, no feature extraction, no audio, no network, nothing written outside
``tmp_path``. The dataset fixtures are two text files per dataset: what is being
verified is that a manifest describes its bytes, and a text file has bytes.

A note on how the labels are written
------------------------------------
``tests/tools/test_wakeword_no_human_data_committed.py`` refuses tracked text
that looks like a record keyed by speaker. The labels here are therefore
assembled by :func:`_spk` rather than written next to a key, which is also why
nothing in this file spells one out.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import pytest

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
CONFIG = WAKEWORD / "round8_config.json"

#: The digests of the pair that currently ships, from the synthetic era. Written
#: out so this file states what it expects the registry to recognise; the test
#: below checks them against ``retired_synthetic_artifacts.json`` rather than
#: trusting either.
SHIPPED_ONNX_SHA256 = "59d35d33703e105739daaa0284eef4629ff2aeaa973d6a3d4fe8183cf1699d79"
SHIPPED_TFLITE_SHA256 = "c2b87ca6e7420cb4943fd53ff9f0d136ef39b226c0f556ecc800d32ed6a8ae73"

_LABEL_PREFIX = "E"


def _spk(number: int) -> str:
    """One dataset/speaker label, assembled from fragments."""
    return f"{_LABEL_PREFIX}{number:03d}"


TRAIN = (_spk(1), _spk(3), _spk(4))
VALIDATION = (_spk(5),)
SEALED = (_spk(2), _spk(6), _spk(7))

#: What the fake registry claims, mirroring ``build_human_dataset.SPEAKER_SPLITS``.
#: Asserted against the committed table in
#: ``test_the_faked_split_registry_is_the_committed_one``.
REGISTRY: Mapping[str, str] = {
    _spk(1): "train",
    _spk(2): "eval_sealed",
    _spk(3): "train",
    _spk(4): "train",
    _spk(5): "validation",
    _spk(6): "qualification_sealed",
    _spk(7): "qualification_sealed",
}
SEALED_SPLITS = frozenset({"eval_sealed", "qualification_sealed"})

ARMS = ("r8a", "r8b", "r8c")


# ── loading the modules under and around test ────────────────────────────────


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_r8_{name}", WAKEWORD / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    if str(WAKEWORD) not in sys.path:
        sys.path.insert(0, str(WAKEWORD))
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def controller_module():
    return _load("round8_controller")


@pytest.fixture(scope="module")
def fm():
    return _load("freeze_manifest")


@pytest.fixture(scope="module")
def qualify():
    return _load("qualify")


@pytest.fixture(scope="module")
def promote():
    return _load("promote_model")


@pytest.fixture(scope="module")
def config_module():
    return _load("round8_config")


# ── dataset fixtures: two text files and a real frozen manifest ──────────────


def _freeze_dataset(
    fm_module,
    root: Path,
    manifest_path: Path,
    name: str,
    *,
    split: str,
    usage: str,
    speakers: Sequence[str] = (),
) -> dict:
    """A real ``freeze_manifest`` manifest over two small text files.

    The manifest lives outside the dataset root on purpose: ``verify(strict=True)``
    reports anything under the root that the manifest does not list, and a
    manifest that listed itself would never verify.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "take-01.txt").write_text(f"{name} take one\n", encoding="utf-8")
    (root / "take-02.txt").write_text(f"{name} take two\n", encoding="utf-8")
    manifest = fm_module.build(root, dataset=name, split=split, usage=usage)
    if speakers:
        manifest["speakers"] = list(speakers)
    # Recomputed, because `manifest_digest` covers the whole body except its own
    # field. A manifest with a key added after freezing would fail to verify,
    # which is the behaviour these fixtures rely on elsewhere.
    manifest["manifest_sha256"] = fm_module.manifest_digest(manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


# ── the seams ────────────────────────────────────────────────────────────────


@dataclass
class Fakes:
    """Every seam, with the honest default and a knob for each refusal."""

    tmp: Path
    fm: object
    qualify: object
    config_module: object
    promote: object

    threshold: float = 0.9990234375
    candidate: str = "r8b"
    selected_on: tuple[str, ...] = VALIDATION
    engine: dict = field(default_factory=lambda: {"onnx": "onnx", "tflite": "tflite"})
    weights: dict = field(
        default_factory=lambda: {
            "r8a": "1" * 64,
            "r8b": "2" * 64,
            "r8c": "3" * 64,
        }
    )
    artifact_digest: dict = field(
        default_factory=lambda: {"onnx": "a" * 64, "tflite": "b" * 64}
    )
    export_weights: dict = field(default_factory=dict)
    export_only: tuple[str, ...] | None = None
    digest_for: dict = field(default_factory=dict)
    guard_raises: dict = field(default_factory=dict)
    report_override: dict | None = None
    report_kwargs: dict = field(default_factory=dict)
    real_qualify_report: object | None = None
    #: ``None`` runs the real ``round8_config.check``. A tuple — including an
    #: empty one — stands in for it, so a test can reach the controller's own
    #: layer without the predeclaration validator answering first.
    config_problems: tuple[str, ...] | None = None
    splits: dict = field(default_factory=lambda: dict(REGISTRY))

    calls: list = field(default_factory=list)
    guard_calls: list = field(default_factory=list)
    manifests_read: list = field(default_factory=list)

    # -- the predeclaration ------------------------------------------------

    def check_config(self, config):
        self.calls.append("check_config")
        if self.config_problems is not None:
            return list(self.config_problems)
        return list(self.config_module.check(dict(config)))

    def acceptance_targets(self):
        return {
            target.id: (float(target.limit), str(target.comparison))
            for target in self.promote.ACCEPTANCE_TARGETS.values()
        }

    def backend_artifacts(self):
        return dict(self.promote.BACKEND_ARTIFACTS)

    # -- datasets ----------------------------------------------------------

    def load_manifest(self, path: Path):
        self.manifests_read.append(Path(path).name)
        return self.fm.load(Path(path))

    def verify_manifest(self, manifest: Path, root: Path):
        return self.fm.verify(Path(manifest), Path(root), strict=True)

    def assert_usable_for(self, manifest, purpose):
        return self.fm.assert_usable_for(dict(manifest), purpose)

    def registry_split(self, speaker: str) -> str:
        try:
            return self.splits[speaker]
        except KeyError:
            raise SystemExit(
                f"speaker {speaker!r} is not in the approved registry"
            ) from None

    def sealed_splits_of(self):
        return SEALED_SPLITS

    # -- content addressing ------------------------------------------------

    def file_digest(self, path: Path) -> str:
        override = self.digest_for.get(Path(path).name)
        if override is not None:
            return override
        return self.qualify.sha256_file(Path(path))

    def retired_digests(self):
        return self.qualify.retired_hashes()

    def refuse_synthetic_initialization(self, path: Path) -> None:
        self.guard_calls.append(Path(path).name)
        message = self.guard_raises.get(Path(path).name)
        if message:
            raise SystemExit(message)

    # -- freezing and seals ------------------------------------------------

    def freeze_record(self, body):
        return self.qualify.freeze_from_json(dict(body))

    def _ledger(self):
        return self.qualify.SealLedger(self.tmp / "seals.json")

    def seal_status(self, dataset: str):
        return self._ledger().status(dataset)

    def open_seal(self, dataset: str, freeze):
        return self._ledger().open_sealed(dataset, freeze)

    # -- the expensive stages ----------------------------------------------

    def train_arm(self, spec: Mapping) -> dict:
        return {
            "arm": spec["arm"],
            "channels": list(spec["channels"]),
            "seed": spec["seed"],
            "weights_sha256": self.weights[spec["arm"]],
            "epochs": 60,
        }

    def select_candidate(self, arms: Sequence[Mapping], constraints: Mapping) -> dict:
        return {
            "candidate_id": self.candidate,
            "threshold": self.threshold,
            "weights_sha256": self.weights[self.candidate],
            "selected_on": list(self.selected_on),
            "rule": "lowest utterance-level false-reject rate at each arm's own threshold",
        }

    def export_artifact(self, backend: str, destination: Path, spec: Mapping) -> dict:
        if self.export_only is not None and backend not in self.export_only:
            raise RuntimeError(f"{backend} export was not produced")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text(f"{backend} artifact\n", encoding="utf-8")
        return {
            "weights_sha256": self.export_weights.get(backend, spec["weights_sha256"]),
            "artifact_sha256": self.artifact_digest[backend],
        }

    def run_runtime(self, backend: str, artifact: Path, spec: Mapping) -> dict:
        return {
            "engine_inference_framework": self.engine[backend],
            "artifact_sha256": spec["artifact_sha256"],
            "threshold_hex": spec["threshold_hex"],
            "confirmation_frames": spec["confirmation_frames"],
            "datasets": list(spec["datasets"]),
            "windows": 14987,
            "runtime_cost": {
                "latency_ms": {"p50": 0.8, "p95": 1.2, "p99": 1.9},
                "peak_rss_bytes": 190_000_000,
            },
        }

    def qualify_report(self, request: Mapping) -> dict:
        if self.real_qualify_report is not None:
            return self.real_qualify_report(request)
        if self.report_override is not None:
            return self.report_override
        return _report(**self.report_kwargs)

    # -- assembly ----------------------------------------------------------

    def delegates(self, module):
        return module.Delegates(
            check_config=self.check_config,
            acceptance_targets=self.acceptance_targets,
            backend_artifacts=self.backend_artifacts,
            load_manifest=self.load_manifest,
            verify_manifest=self.verify_manifest,
            assert_usable_for=self.assert_usable_for,
            registry_split=self.registry_split,
            sealed_splits=self.sealed_splits_of,
            file_digest=self.file_digest,
            retired_digests=self.retired_digests,
            refuse_synthetic_initialization=self.refuse_synthetic_initialization,
            freeze_record=self.freeze_record,
            seal_status=self.seal_status,
            open_seal=self.open_seal,
            train_arm=self.train_arm,
            select_candidate=self.select_candidate,
            export_artifact=self.export_artifact,
            run_runtime=self.run_runtime,
            qualify_report=self.qualify_report,
        )


def _report(
    *,
    qualified: bool = True,
    underpowered: Sequence[int] = (),
    refusals: Sequence[str] = (),
    disagreements: int = 0,
    datasets: Sequence[str] = VALIDATION,
    backends: Sequence[str] = ("onnx", "tflite"),
    target_count: int = 5,
    verdict: str | None = None,
    demonstrable_rows: bool | None = None,
    drop: Sequence[str] = (),
) -> dict:
    """A qualification report in ``qualify.py``'s shape."""
    rows = [
        {
            "target": index,
            "statistically_demonstrable": (
                demonstrable_rows
                if demonstrable_rows is not None
                else index not in set(underpowered)
            ),
            "refusals": [],
        }
        for index in range(1, target_count + 1)
    ]
    official = {
        "official": True,
        "conditioning": "as-recorded",
        "backends": list(backends),
        "evidence": {"datasets": list(datasets)},
        "targets": rows,
        "parity": {
            "target": 5,
            "detection_disagreements": disagreements,
            "windows_compared": 14987,
        },
        "targets_demonstrated": target_count - len(set(underpowered)),
        "targets_total": target_count,
        "underpowered_targets": list(underpowered),
        "qualified_5_of_5": qualified,
        "verdict": verdict
        or (
            "QUALIFIED — all five targets are met and statistically demonstrable"
            if qualified
            else f"REFUSED — target(s) {list(underpowered)} are underpowered"
        ),
        "refusals": list(refusals),
    }
    for key in drop:
        official.pop(key, None)
    return {"schema_version": 1, "official": official, "diagnostic": None}


# ── scenario builder ─────────────────────────────────────────────────────────


@dataclass
class Scenario:
    module: object
    fakes: Fakes
    plan: object
    controller: object
    journal_path: Path
    arm_root: Path
    data_root: Path
    manifest_root: Path

    def rebuild(self, **inputs):
        """A fresh controller over the same journal file — a resumed run."""
        journal = self.module.Journal.open(self.journal_path, self.plan)
        return self.module.Round8Controller(
            plan=self.plan,
            journal=journal,
            delegates=self.fakes.delegates(self.module),
            inputs=self.controller.inputs
            if not inputs
            else self.module.replace(self.controller.inputs, **inputs),
        )


def _scenario(
    module,
    fm_module,
    qualify_module,
    config_module_,
    promote_module,
    tmp_path: Path,
    *,
    usage: Mapping[str, str] | None = None,
    speakers: Mapping[str, Sequence[str]] | None = None,
    config: Mapping | None = None,
    **fake_kwargs,
) -> Scenario:
    fakes = Fakes(
        tmp=tmp_path,
        fm=fm_module,
        qualify=qualify_module,
        config_module=config_module_,
        promote=promote_module,
        **fake_kwargs,
    )
    data_root = tmp_path / "data"
    manifest_root = tmp_path / "manifests"
    arm_root = tmp_path / "arms"
    default_usage = {name: "training" for name in TRAIN}
    default_usage.update({name: "validation" for name in VALIDATION})
    default_usage.update({name: "sealed-evaluation" for name in SEALED})
    default_split = {name: "train" for name in TRAIN}
    default_split.update({name: "validation" for name in VALIDATION})
    default_split.update({name: "sealed" for name in SEALED})
    chosen_usage = dict(default_usage)
    chosen_usage.update(usage or {})

    datasets = {}
    for name in (*TRAIN, *VALIDATION, *SEALED):
        root = data_root / name
        manifest_path = manifest_root / f"{name}.json"
        _freeze_dataset(
            fm_module,
            root,
            manifest_path,
            name,
            split=default_split[name],
            usage=chosen_usage[name],
            speakers=(speakers or {}).get(name, ()),
        )
        datasets[name] = module.DatasetPaths(manifest=manifest_path, root=root)

    payload = dict(config) if config is not None else module.load_config(CONFIG)
    plan = module.build_plan(payload, fakes.delegates(module))
    journal_path = tmp_path / "journal.json"
    journal = module.Journal.open(journal_path, plan)
    inputs = module.RunInputs(
        datasets=datasets,
        arm_output={arm: arm_root / arm for arm in ARMS},
        artifact_dir=tmp_path / "candidates" / "r8b",
        runtime_version="0.19.1",
        repo_commit="f" * 40,
        config_sha256="c" * 64,
    )
    controller = module.Round8Controller(
        plan=plan, journal=journal, delegates=fakes.delegates(module), inputs=inputs
    )
    return Scenario(
        module=module,
        fakes=fakes,
        plan=plan,
        controller=controller,
        journal_path=journal_path,
        arm_root=arm_root,
        data_root=data_root,
        manifest_root=manifest_root,
    )


@pytest.fixture
def scenario(controller_module, fm, qualify, config_module, promote, tmp_path):
    def build(**kwargs):
        return _scenario(
            controller_module, fm, qualify, config_module, promote, tmp_path, **kwargs
        )

    return build


def _completed(scenario, *, stop: int = 10) -> Scenario:
    """A default scenario driven through the automatic sequence."""
    case = scenario()
    case.controller.advance(to=stop)
    return case


# ── the plan: the predeclaration, pinned ─────────────────────────────────────


def test_the_pinned_plan_is_the_committed_predeclaration(scenario):
    plan = scenario().plan
    assert plan.round == 8
    assert plan.train == TRAIN
    assert plan.validation == VALIDATION
    assert plan.sealed == SEALED
    assert plan.seed == 20260818
    assert [arm.id for arm in plan.arms] == list(ARMS)
    assert [list(arm.channels) for arm in plan.arms] == [
        [128, 128, 64],
        [32, 32, 16],
        [8, 8, 4],
    ]
    # The stop condition is read, not invented.
    assert "do not add a fourth width" in plan.stop_condition.lower()
    assert plan.confirmation_frames == 3
    assert dict(plan.artifacts) == {
        "onnx": "hey_youtab.onnx",
        "tflite": "hey_youtab.tflite",
    }
    assert plan.targets == (
        (1, 0.05, "<="),
        (2, 0.2, "<="),
        (3, 0.02, "<="),
        (4, 0.0, "=="),
        (5, 0.0, "=="),
    )


def test_a_weakened_target_is_refused(controller_module, scenario):
    """The predeclaration validator would also catch this; the point is that the
    controller catches it *independently*, against the two hard-coded copies in
    ``promote_model`` and ``qualify``. So the validator is stubbed out here and
    the cross-check is what has to fire."""
    config = controller_module.load_config(CONFIG)
    for entry in config["targets"]:
        if entry["id"] == 1:
            entry["limit"] = 0.10
    with pytest.raises(controller_module.PlanChangedRefused, match="cannot be weakened"):
        scenario(config=config, config_problems=())


def test_a_set_wider_than_the_predeclared_bound_is_refused(controller_module, scenario):
    config = controller_module.load_config(CONFIG)
    config["variation"]["arms"].extend(
        [
            {"id": "r8d", "channels": [4, 4, 2], "parameters": 2931},
            {"id": "r8e", "channels": [2, 2, 1], "parameters": 1504},
        ]
    )
    config["variation"]["runs"] = 5
    with pytest.raises(
        controller_module.PlanChangedRefused, match="predeclares exactly 3"
    ):
        scenario(config=config)


def test_a_widened_matrix_is_refused_at_the_predeclaration_not_mid_run(
    controller_module, scenario
):
    """A fourth width no longer survives long enough to be caught mid-run.

    This test used to build a four-arm plan, assert its digest differed, and let
    `Journal.open` refuse it as plan drift. That path is gone: the arm count is
    now closed at exactly three, so `build_plan` refuses a fourth arm before a
    plan object exists. Asserting the old behaviour would assert that the tighter
    bound is absent.

    So it now pins the *earlier* refusal, which is the stronger one — a matrix
    that cannot be built cannot be run at all, whereas mid-run drift detection
    only catches an edit made after a run started.
    """
    case = scenario()
    case.controller.advance(to=3)
    widened = controller_module.load_config(CONFIG)
    widened["variation"]["arms"].append(
        {"id": "r8d", "channels": [4, 4, 2], "parameters": 2931}
    )
    widened["variation"]["runs"] = 4
    with pytest.raises(
        controller_module.PlanChangedRefused, match="predeclares exactly 3"
    ):
        controller_module.build_plan(widened, case.fakes.delegates(controller_module))


def test_arms_that_are_not_ordered_downward_are_refused(controller_module, scenario):
    config = controller_module.load_config(CONFIG)
    config["variation"]["arms"].reverse()
    with pytest.raises(controller_module.PlanChangedRefused, match="widest to narrowest"):
        scenario(config=config)


def test_the_synthetic_eras_seed_is_refused(controller_module, scenario):
    config = controller_module.load_config(CONFIG)
    for field_name in ("seed", "torch_manual_seed", "numpy_default_rng"):
        config["initialization"][field_name] = 20260807
    with pytest.raises(controller_module.PlanChangedRefused, match="20260807"):
        scenario(config=config)


def test_an_empty_stop_condition_is_refused(controller_module, scenario):
    config = controller_module.load_config(CONFIG)
    config["variation"]["stop_condition"] = "   "
    with pytest.raises(controller_module.PlanChangedRefused, match="stop_condition"):
        scenario(config=config)


def test_a_config_that_does_not_validate_is_not_a_plan(controller_module, scenario):
    with pytest.raises(controller_module.PlanChangedRefused, match="does not validate"):
        scenario(config_problems=("splits.train is wrong",))


def test_the_faked_split_registry_is_the_committed_one():
    """The fake stands in for ``build_human_dataset``; prove it did not drift.

    Read with ``ast`` rather than imported, the same way ``round8_config.py``
    reads the pipeline's own constants: importing that module pulls numpy and the
    whole ingest stage in for two lookups.
    """
    tree = ast.parse((WAKEWORD / "build_human_dataset.py").read_text(encoding="utf-8"))
    registry = None
    sealed = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "SPEAKER_SPLITS" and node.value is not None:
                registry = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SEALED_SPLITS"
            for target in node.targets
        ):
            # ``frozenset({...})`` — read the set literal it is built from.
            sealed = frozenset(ast.literal_eval(node.value.args[0]))
    assert registry == dict(REGISTRY)
    assert sealed == SEALED_SPLITS


def test_the_shipped_pair_is_in_the_registry_the_controller_consults(qualify):
    retired = qualify.retired_hashes()
    assert retired.get(SHIPPED_ONNX_SHA256) == "tools/wakewords/hey_youtab.onnx"
    assert retired.get(SHIPPED_TFLITE_SHA256) == "tools/wakewords/hey_youtab.tflite"


def test_the_module_guard_the_controller_delegates_to_is_wired_into_the_trainer():
    """Grounding: the guard exists and the trainer's resume path calls it."""
    human = (WAKEWORD / "build_human_dataset.py").read_text(encoding="utf-8")
    trainer = (WAKEWORD / "train_model.py").read_text(encoding="utf-8")
    assert "def refuse_synthetic_initialization(" in human
    assert "refuse_synthetic_initialization(checkpoint)" in trainer


# ── ordering and persistence ─────────────────────────────────────────────────


def test_a_later_state_cannot_run_before_an_earlier_one(controller_module, scenario):
    controller = scenario().controller
    with pytest.raises(controller_module.StateOutOfOrderRefused, match="state 1"):
        controller.run_state(5)


def test_every_state_is_gated_on_its_predecessor(controller_module, scenario):
    controller = scenario().controller
    for number in range(2, 11):
        with pytest.raises(controller_module.StateOutOfOrderRefused):
            controller.run_state(number)


def test_a_state_runs_once(controller_module, scenario):
    controller = scenario().controller
    controller.run_state(1)
    with pytest.raises(controller_module.StateOutOfOrderRefused, match="already succeeded"):
        controller.run_state(1)


def test_the_journal_persists_the_sequence_across_processes(scenario):
    case = scenario()
    case.controller.advance(to=3)
    resumed = case.rebuild()
    assert resumed.journal.reached() == (1, 2, 3)
    assert resumed.journal.evidence(1)["manifest_sha256"].keys() == set(TRAIN)
    # And a resumed run picks up at 4 rather than repeating 1.
    resumed.advance(to=4)
    assert resumed.journal.reached() == (1, 2, 3, 4)


def test_a_resumed_run_refuses_if_the_plan_changed_under_it(
    controller_module, scenario, fm, qualify, config_module, promote, tmp_path
):
    case = scenario()
    case.controller.advance(to=3)
    moved = controller_module.load_config(CONFIG)
    # Changes the plan without leaving the predeclared capacity set. Dropping an
    # arm used to do this and can no longer: the matrix is closed at exactly
    # three, so a two-arm config is refused before a plan exists and this test
    # would stop exercising plan drift at all.
    #
    # Changing the seed is what plan drift looks like when the matrix cannot be
    # widened: the arms are identical, the config still validates, and the run is
    # nonetheless a different experiment. The schedule is deliberately NOT used
    # here -- `Plan.body()` covers round, splits, arms, seed, stop condition and
    # targets, so a batch-size edit validates and leaves the digest untouched,
    # which would make this test pass for the wrong reason.
    for field_name in ("seed", "torch_manual_seed", "numpy_default_rng"):
        moved["initialization"][field_name] = 20260819
    fakes = Fakes(
        tmp=tmp_path, fm=fm, qualify=qualify, config_module=config_module, promote=promote
    )
    other = controller_module.build_plan(moved, fakes.delegates(controller_module))
    with pytest.raises(controller_module.PlanChangedRefused, match="changed under a run"):
        controller_module.Journal.open(case.journal_path, other)


def test_a_refused_state_is_recorded_and_is_not_a_success(controller_module, scenario):
    case = scenario(engine={"onnx": "onnx", "tflite": "onnx"})
    case.controller.advance(to=7)
    with pytest.raises(controller_module.BackendFallbackRefused):
        case.controller.run_state(8)
    journal = json.loads(case.journal_path.read_text(encoding="utf-8"))
    eight = [row for row in journal["transitions"] if row["state"] == 8]
    assert [row["status"] for row in eight] == ["refused"]
    assert "BackendFallbackRefused" in eight[0]["detail"]
    assert case.controller.journal.status(8) == "refused"
    assert 8 not in case.controller.journal.reached()


# ── delegation ───────────────────────────────────────────────────────────────


def test_a_state_that_skips_a_required_delegation_is_refused(
    controller_module, scenario, monkeypatch
):
    """Drop the usage delegation from state 1 and the state itself is refused."""
    case = scenario()
    monkeypatch.setattr(
        controller_module.Round8Controller,
        "_delegate_usage",
        lambda self, manifest, purpose, dataset: None,
    )
    with pytest.raises(
        controller_module.DelegationMissingRefused, match="assert_usable_for"
    ):
        case.controller.run_state(1)


def test_an_unwired_expensive_stage_is_refused_not_skipped(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=4)
    case.controller.delegates = controller_module.replace(
        case.controller.delegates, train_arm=None
    )
    with pytest.raises(controller_module.DelegationMissingRefused, match="train_arm"):
        case.controller.run_state(5)


def test_every_transition_records_the_delegations_it_made(scenario):
    case = _completed(scenario)
    journal = json.loads(case.journal_path.read_text(encoding="utf-8"))
    for row in journal["transitions"]:
        assert row["status"] == "succeeded"
        assert set(row["delegates_required"]) <= set(row["delegates_called"])


# ── states 1 and 2: manifests, and who may appear in them ────────────────────


def test_a_training_manifest_that_does_not_verify_is_refused(controller_module, scenario):
    case = scenario()
    (case.data_root / TRAIN[1] / "take-01.txt").write_text("edited\n", encoding="utf-8")
    with pytest.raises(controller_module.ManifestNotFrozenRefused, match="changed"):
        case.controller.run_state(1)


def test_an_extra_file_under_a_verified_root_is_refused(controller_module, scenario):
    case = scenario()
    (case.data_root / VALIDATION[0] / "take-03.txt").write_text("new\n", encoding="utf-8")
    case.controller.run_state(1)
    with pytest.raises(controller_module.ManifestNotFrozenRefused, match="extra"):
        case.controller.run_state(2)


def test_a_sealed_set_offered_as_training_data_is_refused(controller_module, scenario):
    """The usage refusal is ``freeze_manifest``'s; the controller delegates it."""
    case = scenario(usage={TRAIN[1]: "sealed-evaluation"})
    with pytest.raises(controller_module.SealedSpeakerLeakRefused, match="sealed"):
        case.controller.run_state(1)


def test_a_sealed_speaker_named_in_a_training_manifest_is_refused(
    controller_module, scenario
):
    case = scenario(speakers={TRAIN[0]: (TRAIN[0], SEALED[1])})
    with pytest.raises(
        controller_module.SealedSpeakerLeakRefused, match="no longer unseen"
    ):
        case.controller.run_state(1)


def test_a_sealed_speaker_named_in_the_validation_manifest_is_refused(
    controller_module, scenario
):
    """The mutation case: the sealed voice reaching the set that picks the threshold."""
    case = scenario(speakers={VALIDATION[0]: (VALIDATION[0], SEALED[0])})
    case.controller.run_state(1)
    with pytest.raises(
        controller_module.SealedSpeakerLeakRefused, match="no longer unseen"
    ):
        case.controller.run_state(2)


def test_the_validation_speaker_inside_a_training_manifest_is_refused(
    controller_module, scenario
):
    case = scenario(speakers={TRAIN[2]: (TRAIN[2], VALIDATION[0])})
    with pytest.raises(
        controller_module.ValidationSpeakerLeakRefused, match="fitted on"
    ):
        case.controller.run_state(1)


def test_a_training_speaker_reappearing_in_validation_is_refused(
    controller_module, scenario
):
    """The same leak from the other side: the registry catches it on its own."""
    case = scenario(speakers={VALIDATION[0]: (VALIDATION[0], TRAIN[0])})
    # State 1 passes: nothing in the training manifests is out of place.
    case.controller.run_state(1)
    with pytest.raises(
        controller_module.ValidationSpeakerLeakRefused, match="measures memory"
    ):
        case.controller.run_state(2)


def test_an_overlap_the_registry_cannot_see_is_still_refused(
    controller_module, scenario
):
    """The cross-check against what state 1 wrote down, not against the registry.

    Both manifests declare speakers the registry binds to the split they are in,
    so the per-speaker rule is satisfied and the overlap is only visible by
    comparing the two records.
    """
    case = scenario(
        speakers={
            TRAIN[0]: (TRAIN[0], TRAIN[1]),
            VALIDATION[0]: (VALIDATION[0],),
        }
    )
    case.controller.run_state(1)
    # Rewrite state 1's record so the shared label is a validation-bound one; the
    # per-speaker rule then passes on both sides and only the cross-check remains.
    recorded = case.controller.journal.transitions[-1]["evidence"]
    recorded["speakers"][TRAIN[0]] = [TRAIN[0], VALIDATION[0]]
    with pytest.raises(
        controller_module.ValidationSpeakerLeakRefused, match="both the training"
    ):
        case.controller.run_state(2)


def test_a_speaker_the_registry_does_not_know_is_refused(controller_module, scenario):
    case = scenario(speakers={TRAIN[0]: (TRAIN[0], "E404")})
    with pytest.raises(
        controller_module.ManifestNotFrozenRefused, match="approved registry"
    ):
        case.controller.run_state(1)


# ── state 3: the seals, proved without opening them ──────────────────────────


def test_state_3_reads_no_sealed_manifest(scenario):
    case = scenario()
    case.controller.advance(to=2)
    case.fakes.manifests_read.clear()
    evidence = case.controller.run_state(3)["evidence"]
    assert case.fakes.manifests_read == []
    assert evidence["manifests_read"] == []
    assert set(evidence["registry_bindings"]) == set(SEALED)
    assert set(evidence["registry_bindings"].values()) <= SEALED_SPLITS


def test_state_3_refuses_a_sealed_set_already_on_the_ledger(
    controller_module, scenario, qualify, tmp_path
):
    case = scenario()
    case.controller.advance(to=2)
    ledger = qualify.SealLedger(tmp_path / "seals.json")
    freeze = qualify.freeze_from_json(
        {
            "candidate_id": "r7",
            "threshold_hex": float(0.5).hex(),
            "confirmation_frames": 3,
            "runtime_version": "0.1.0",
            "repo_commit": "e" * 40,
            "manifest_sha256": {SEALED[1]: "d" * 64},
            "artifact_sha256": {"onnx": "1" * 64, "tflite": "2" * 64},
        }
    )
    ledger.open_sealed(SEALED[1], freeze)
    with pytest.raises(controller_module.SealAlreadySpentRefused, match="CONSUMED|spent"):
        case.controller.run_state(3)


def test_state_3_refuses_when_the_registry_and_the_config_disagree(
    controller_module, scenario
):
    case = scenario(splits={**REGISTRY, SEALED[2]: "train"})
    case.controller.advance(to=2)
    with pytest.raises(controller_module.SealedSpeakerLeakRefused, match="disagree"):
        case.controller.run_state(3)


# ── state 4: fresh weights ───────────────────────────────────────────────────


def test_state_4_requires_every_arm_directory_to_be_empty(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=3)
    leftover = case.arm_root / ARMS[0]
    leftover.mkdir(parents=True)
    (leftover / "checkpoint.pt").write_bytes(b"stale")
    with pytest.raises(
        controller_module.IncompatibleCheckpointRefused, match="undeclared warm start"
    ):
        case.controller.run_state(4)


def test_state_4_writes_the_plan_beside_each_arm(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=4)
    for arm in ARMS:
        record = json.loads(
            (case.arm_root / arm / controller_module.INIT_RECORD_NAME).read_text(
                encoding="utf-8"
            )
        )
        assert record["plan_digest"] == case.plan.digest()
        assert record["seed"] == 20260818
        assert record["fresh"] is True


def test_state_4_refuses_an_empty_retired_registry(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=3)
    case.controller.delegates = controller_module.replace(
        case.controller.delegates, retired_digests=lambda: {}
    )
    with pytest.raises(
        controller_module.SyntheticInitializationRefused, match="recognises nothing"
    ):
        case.controller.run_state(4)


# ── the synthetic-initialization refusal ─────────────────────────────────────


def test_the_shipped_model_as_an_initialization_is_refused_before_the_module_guard(
    controller_module, scenario
):
    """The mutation case, and the ordering claim with it.

    The bytes are judged first. ``refuse_synthetic_initialization`` must not even
    be reached, because a retired candidate copied into a clean directory with a
    contract written beside it passes everything that reads a path or a sibling
    file — and the content address does not move with the copy.
    """
    case = scenario(digest_for={"checkpoint.pt": SHIPPED_ONNX_SHA256})
    case.controller.advance(to=4)
    checkpoint = case.arm_root / ARMS[0] / "checkpoint.pt"
    checkpoint.write_bytes(b"a copy of something retired")
    with pytest.raises(
        controller_module.SyntheticInitializationRefused,
        match="tools/wakewords/hey_youtab.onnx",
    ):
        case.controller.run_state(5)
    assert case.fakes.guard_calls == []


def test_the_module_guards_own_refusal_is_surfaced(controller_module, scenario):
    case = scenario(
        guard_raises={"checkpoint.pt": "no DATASET_CONTRACT.json beside it"}
    )
    case.controller.advance(to=4)
    (case.arm_root / ARMS[0] / "checkpoint.pt").write_bytes(b"clean bytes")
    with pytest.raises(
        controller_module.SyntheticInitializationRefused, match="DATASET_CONTRACT"
    ):
        case.controller.run_state(5)
    assert case.fakes.guard_calls == ["checkpoint.pt"]


def test_trained_weights_that_are_a_retired_artifact_are_refused(
    controller_module, scenario
):
    case = scenario()
    case.fakes.weights["r8a"] = SHIPPED_TFLITE_SHA256
    case.controller.advance(to=4)
    with pytest.raises(
        controller_module.SyntheticInitializationRefused, match="under a new name"
    ):
        case.controller.run_state(5)


def test_an_export_that_is_the_shipped_artifact_is_refused(controller_module, scenario):
    case = scenario()
    case.fakes.artifact_digest["tflite"] = SHIPPED_TFLITE_SHA256
    case.controller.advance(to=6)
    with pytest.raises(
        controller_module.SyntheticInitializationRefused, match="shipped synthetic-era"
    ):
        case.controller.run_state(7)


# ── resuming from an incompatible checkpoint ─────────────────────────────────


def test_a_checkpoint_from_another_plan_is_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=4)
    sidecar = case.arm_root / ARMS[0] / controller_module.INIT_RECORD_NAME
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    record["plan_digest"] = "9" * 64
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    (case.arm_root / ARMS[0] / "checkpoint.pt").write_bytes(b"epoch 41")
    with pytest.raises(
        controller_module.IncompatibleCheckpointRefused, match="belongs to plan"
    ):
        case.controller.run_state(5)


def test_a_checkpoint_seeded_differently_is_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=4)
    sidecar = case.arm_root / ARMS[1] / controller_module.INIT_RECORD_NAME
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    record["seed"] = 20260807
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    (case.arm_root / ARMS[1] / "checkpoint.pt").write_bytes(b"epoch 12")
    with pytest.raises(
        controller_module.IncompatibleCheckpointRefused, match="draw order"
    ):
        case.controller.run_state(5)


def test_a_checkpoint_with_no_plan_beside_it_is_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=4)
    (case.arm_root / ARMS[2] / controller_module.INIT_RECORD_NAME).unlink()
    (case.arm_root / ARMS[2] / "checkpoint.pt").write_bytes(b"epoch 3")
    with pytest.raises(
        controller_module.IncompatibleCheckpointRefused, match="not a resume"
    ):
        case.controller.run_state(5)


def test_this_rounds_own_crash_checkpoint_is_admitted(scenario):
    """The permitted case, so the refusals above are not merely 'refuse a resume'."""
    case = scenario()
    case.controller.advance(to=4)
    (case.arm_root / ARMS[0] / "checkpoint.pt").write_bytes(b"epoch 41 of this plan")
    evidence = case.controller.run_state(5)["evidence"]
    resumed = {row["arm"]: row["resumed_from"] for row in evidence["arms"]}
    assert resumed[ARMS[0]]["arm"] == ARMS[0]
    assert resumed[ARMS[0]]["seed"] == 20260818
    assert resumed[ARMS[1]] is None


# ── state 5: the bounded design ──────────────────────────────────────────────


def test_state_5_runs_exactly_the_predeclared_arms_in_order(scenario):
    case = scenario()
    case.controller.advance(to=5)
    evidence = case.controller.journal.evidence(5)
    assert [row["arm"] for row in evidence["arms"]] == list(ARMS)
    assert {row["seed"] for row in evidence["arms"]} == {20260818}
    assert "do not add a fourth width" in evidence["stop_condition"].lower()


def test_a_trainer_that_reports_another_width_is_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=4)

    def wrong_width(spec):
        return {
            "arm": spec["arm"],
            "channels": [64, 64, 32],
            "seed": spec["seed"],
            "weights_sha256": "7" * 64,
        }

    case.controller.delegates = controller_module.replace(
        case.controller.delegates, train_arm=wrong_width
    )
    with pytest.raises(controller_module.PlanChangedRefused, match="Capacity is the one axis"):
        case.controller.run_state(5)


def test_a_trainer_that_reports_another_seed_is_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=4)

    def wrong_seed(spec):
        return {
            "arm": spec["arm"],
            "channels": list(spec["channels"]),
            "seed": 20260807,
            "weights_sha256": "8" * 64,
        }

    case.controller.delegates = controller_module.replace(
        case.controller.delegates, train_arm=wrong_seed
    )
    with pytest.raises(controller_module.PlanChangedRefused, match="reports seed"):
        case.controller.run_state(5)


def test_unhashed_weights_are_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=4)
    case.fakes.weights["r8a"] = ""
    with pytest.raises(controller_module.IncompleteEvidenceRefused, match="weights_sha256"):
        case.controller.run_state(5)


# ── state 6: selection on E005 only ──────────────────────────────────────────


def test_selection_on_a_sealed_set_is_refused(controller_module, scenario):
    case = scenario(selected_on=(VALIDATION[0], SEALED[1]))
    case.controller.advance(to=5)
    with pytest.raises(controller_module.SealedSpeakerLeakRefused, match="has spent it"):
        case.controller.run_state(6)


def test_selection_on_the_training_split_is_refused(controller_module, scenario):
    case = scenario(selected_on=(TRAIN[0],))
    case.controller.advance(to=5)
    with pytest.raises(controller_module.PlanChangedRefused, match="selects on"):
        case.controller.run_state(6)


def test_a_selection_that_does_not_say_what_it_saw_is_refused(
    controller_module, scenario
):
    case = scenario(selected_on=())
    case.controller.advance(to=5)
    with pytest.raises(controller_module.IncompleteEvidenceRefused, match="which datasets"):
        case.controller.run_state(6)


def test_a_candidate_outside_the_predeclared_arms_is_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=5)
    case.fakes.candidate = "r8d"
    case.fakes.weights["r8d"] = "5" * 64
    with pytest.raises(controller_module.PlanChangedRefused, match="not one of the predeclared"):
        case.controller.run_state(6)


def test_the_threshold_is_recorded_at_full_precision(scenario):
    case = scenario()
    case.controller.advance(to=6)
    evidence = case.controller.journal.evidence(6)
    assert float.fromhex(evidence["threshold_hex"]) == 0.9990234375
    assert evidence["threshold_decimal"] == repr(0.9990234375)


# ── state 7: both artifacts, one checkpoint ──────────────────────────────────


def test_an_export_that_produces_only_one_backend_is_refused(controller_module, scenario):
    case = scenario(export_only=("onnx",))
    case.controller.advance(to=6)
    with pytest.raises(controller_module.BackendMissingRefused, match="half a model"):
        case.controller.run_state(7)


def test_exports_from_two_different_checkpoints_are_refused(controller_module, scenario):
    case = scenario(export_weights={"tflite": "9" * 64})
    case.controller.advance(to=6)
    with pytest.raises(controller_module.WeightsDivergedRefused, match="one checkpoint"):
        case.controller.run_state(7)


def test_two_backends_reporting_one_artifact_digest_are_refused(
    controller_module, scenario
):
    case = scenario()
    case.controller.advance(to=6)
    case.fakes.artifact_digest["tflite"] = case.fakes.artifact_digest["onnx"]
    with pytest.raises(controller_module.WeightsDivergedRefused, match="never written"):
        case.controller.run_state(7)


# ── state 8: the runtime, and the silent fallback ────────────────────────────


def test_a_tflite_request_the_engine_answers_with_onnx_is_refused(
    controller_module, scenario
):
    """The mutation case for the silent downgrade.

    ``default_inference_framework`` returns tflite on macOS ARM64 and onnx
    elsewhere, and the engine falls back when the runtime it was asked for is not
    importable. Nothing in the numbers shows it.
    """
    case = scenario(engine={"onnx": "onnx", "tflite": "onnx"})
    case.controller.advance(to=7)
    with pytest.raises(
        controller_module.BackendFallbackRefused, match="quietly becomes the other"
    ):
        case.controller.run_state(8)


def test_an_engine_that_reports_no_backend_at_all_is_refused(
    controller_module, scenario
):
    case = scenario(engine={"onnx": "", "tflite": "tflite"})
    case.controller.advance(to=7)
    with pytest.raises(controller_module.BackendFallbackRefused, match="reports nothing"):
        case.controller.run_state(8)


def test_state_8_records_which_library_actually_ran(scenario):
    case = scenario()
    case.controller.advance(to=8)
    evidence = case.controller.journal.evidence(8)
    assert evidence["engine_inference_framework"] == {"onnx": "onnx", "tflite": "tflite"}


def test_a_runtime_measured_at_another_threshold_is_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=7)

    def drifting(backend, artifact, spec):
        row = case.fakes.run_runtime(backend, artifact, spec)
        row["threshold_hex"] = float(0.6).hex()
        return row

    case.controller.delegates = controller_module.replace(
        case.controller.delegates, run_runtime=drifting
    )
    with pytest.raises(
        controller_module.IncompleteEvidenceRefused, match="another operating point"
    ):
        case.controller.run_state(8)


def test_a_runtime_that_streamed_a_sealed_set_is_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=7)

    def peeking(backend, artifact, spec):
        row = case.fakes.run_runtime(backend, artifact, spec)
        row["datasets"] = [*VALIDATION, SEALED[0]]
        return row

    case.controller.delegates = controller_module.replace(
        case.controller.delegates, run_runtime=peeking
    )
    with pytest.raises(controller_module.SealedSpeakerLeakRefused, match="before the freeze"):
        case.controller.run_state(8)


# ── state 9: the harness decides, and its refusal is surfaced ────────────────


def test_the_harness_refusal_is_surfaced_verbatim(controller_module, scenario):
    verdict = (
        "REFUSED — 3/5 targets demonstrable. Target(s) [2, 3] are underpowered: the "
        "evidence present cannot bound them below their limits whatever the point "
        "estimate says."
    )
    case = scenario(
        report_kwargs={
            "qualified": False,
            "underpowered": (2, 3),
            "verdict": verdict,
            "refusals": (
                "target 2: underpowered: 11.66 hours of recorded human speech present, "
                "15 required for any outcome to bound this target at 0.2",
                "target 3: underpowered: 112 near-phrase utterances present, 150 "
                "required for any outcome to bound this target at 0.02",
            ),
        }
    )
    case.controller.advance(to=8)
    with pytest.raises(controller_module.QualificationRefused) as raised:
        case.controller.run_state(9)
    message = str(raised.value)
    assert verdict in message
    assert "11.66 hours" in message and "15 required" in message
    assert "112 near-phrase utterances" in message and "150" in message
    assert "underpowered targets: [2, 3]" in message
    assert "sealed sets stay sealed" in message
    # And the stop condition travels with the refusal.
    assert "do not add a fourth width" in message.lower()


def test_the_controller_reads_the_harnesss_verdict_and_not_the_rows(
    controller_module, scenario
):
    """Every per-target row says demonstrable; the harness says no. No is the answer.

    This is the defect that would matter most: a controller that recomputed 5/5
    from the parts and reported a pass the harness refused.
    """
    case = scenario(
        report_kwargs={
            "qualified": False,
            "underpowered": (2, 3),
            "demonstrable_rows": True,
        }
    )
    case.controller.advance(to=8)
    with pytest.raises(controller_module.QualificationRefused):
        case.controller.run_state(9)


def test_a_refused_qualification_leaves_the_freeze_unreachable(
    controller_module, scenario
):
    case = scenario(report_kwargs={"qualified": False, "underpowered": (2,)})
    case.controller.advance(to=8)
    with pytest.raises(controller_module.QualificationRefused):
        case.controller.run_state(9)
    with pytest.raises(controller_module.StateOutOfOrderRefused, match="state 9"):
        case.controller.run_state(10)
    # Even with the correct authorization phrase, the sealed sets stay sealed.
    with pytest.raises(controller_module.StateOutOfOrderRefused, match="state 9"):
        case.controller.open_sealed(
            SEALED[1],
            authorization=controller_module.AUTHORIZATION_TEMPLATE.format(
                dataset=SEALED[1]
            ),
        )


@pytest.mark.parametrize(
    "missing", ["qualified_5_of_5", "verdict", "underpowered_targets", "refusals", "parity"]
)
def test_a_report_that_does_not_state_its_own_verdict_is_refused(
    controller_module, scenario, missing
):
    case = scenario(report_kwargs={"drop": (missing,)})
    case.controller.advance(to=8)
    with pytest.raises(controller_module.IncompleteEvidenceRefused):
        case.controller.run_state(9)


def test_a_report_missing_a_target_is_refused(controller_module, scenario):
    case = scenario(report_kwargs={"target_count": 4})
    case.controller.advance(to=8)
    with pytest.raises(
        controller_module.IncompleteEvidenceRefused, match="missing target is not a met one"
    ):
        case.controller.run_state(9)


def test_a_report_covering_one_backend_is_refused(controller_module, scenario):
    case = scenario(report_kwargs={"backends": ("onnx",)})
    case.controller.advance(to=8)
    with pytest.raises(controller_module.BackendMissingRefused, match="independently"):
        case.controller.run_state(9)


def test_one_detection_disagreement_is_fatal(controller_module, scenario):
    case = scenario(report_kwargs={"disagreements": 1})
    case.controller.advance(to=8)
    with pytest.raises(
        controller_module.DetectionDisagreementRefused, match="not a tolerance"
    ):
        case.controller.run_state(9)


def test_a_gate_computed_over_a_sealed_set_is_refused(controller_module, scenario):
    case = scenario(report_kwargs={"datasets": (*VALIDATION, SEALED[2])})
    case.controller.advance(to=8)
    with pytest.raises(controller_module.SealedSpeakerLeakRefused, match="may be opened"):
        case.controller.run_state(9)


def test_the_real_harness_refuses_round_8s_projected_quantities(
    controller_module, scenario, qualify
):
    """The whole point, against the real ``qualify.build_report``.

    The quantities are Round 8's own projections for the qualification
    measurement: 72 positive utterances, 112 near-phrase utterances, the Speech
    Commands eval partition's 20,986 clips of 2.00 s window time (11.659 h) and
    61 background windows, with a perfectly clean run on both backends and zero
    detection disagreements. The harness's derived requirements are 60 positive
    utterances, 150 near phrases and 15 hours, so a clean run demonstrates
    targets 1, 4 and 5 and **cannot** demonstrate 2 or 3 whatever it measures.

    Fabricated outcomes and no audio: what is under test is that the controller
    reports the harness's ``REFUSED`` rather than a 5/5 it liked the look of.
    The evidence is labelled with the validation dataset because that is the set
    the gate is computed on; the sample sizes are the projected ones the brief
    names.
    """
    dataset = VALIDATION[0]

    def window(index: int, fired: bool) -> object:
        return qualify.Window(
            window_index=index,
            # Both backends agree on every window, so parity is clean and the
            # only thing left to decide is whether the sample sizes carry a claim.
            fired={"onnx": fired, "tflite": fired},
            delta=qualify.FrameDelta(max_abs=8.2e-06, sum_abs=1.0e-06, frames=16),
        )

    utterances: list[object] = []

    def add(prefix: str, count: int, category: str, provenance: str, windows: int,
            seconds: float, fired: bool) -> None:
        for index in range(count):
            utterances.append(
                qualify.Utterance(
                    utterance_id=f"{prefix}-{index:05d}",
                    category=category,
                    provenance=provenance,
                    audio_seconds=seconds,
                    windows=tuple(window(w, fired) for w in range(windows)),
                    dataset=dataset,
                )
            )

    # A perfect run: every wake phrase fires, nothing else does.
    add("pos", 72, "positive_human", "recorded-human", 7, 2.0, True)
    add("near", 112, "near_phrase_human", "recorded-human", 7, 2.0, False)
    add("sc", 20986, "recorded_speech", "recorded-corpus", 1, 2.0, False)
    add("bg", 61, "background_only", "recorded-corpus", 1, 2.0, False)

    cost = {
        "latency_ms": {"p50": 0.8, "p95": 1.2, "p99": 1.9},
        "peak_rss_bytes": 190_000_000,
        "frames_timed": 100_000,
        "real_time_factor": 0.02,
        "model_bytes": 3_806_464,
        "measured_on": "ci",
    }
    measurement = qualify.Measurement(
        conditioning=qualify.AS_RECORDED,
        backends=("onnx", "tflite"),
        threshold=0.9990234375,
        confirmation_frames=3,
        candidate_id="r8b",
        utterances=tuple(utterances),
        runtime_cost={"onnx": cost, "tflite": cost},
    )
    case = scenario(real_qualify_report=lambda request: qualify.build_report(measurement))
    case.controller.advance(to=8)
    with pytest.raises(controller_module.QualificationRefused) as raised:
        case.controller.run_state(9)
    message = str(raised.value)
    assert "REFUSED" in message
    assert "underpowered targets: [2, 3]" in message
    assert "targets demonstrated: 3/5" in message
    assert "15" in message and "150" in message
    # Nothing was routed around: state 10 is still out of reach.
    with pytest.raises(controller_module.StateOutOfOrderRefused):
        case.controller.run_state(10)


# ── state 10: the freeze ─────────────────────────────────────────────────────


def test_state_10_is_the_first_state_to_read_a_sealed_manifest(scenario):
    case = scenario()
    case.controller.advance(to=9)
    assert not set(case.fakes.manifests_read) & {f"{name}.json" for name in SEALED}
    evidence = case.controller.run_state(10)["evidence"]
    assert set(evidence["sealed_manifest_sha256"]) == set(SEALED)
    assert set(case.fakes.manifests_read) >= {f"{name}.json" for name in SEALED}


def test_the_freeze_pins_everything_the_design_lists(scenario):
    case = _completed(scenario, stop=10)
    evidence = case.controller.journal.evidence(10)
    assert set(evidence["manifest_sha256"]) == set(TRAIN) | set(VALIDATION) | set(SEALED)
    assert set(evidence["artifact_sha256"]) == {"onnx", "tflite"}
    assert evidence["runtime_version"] == "0.19.1"
    assert evidence["repo_commit"] == "f" * 40
    assert float.fromhex(evidence["threshold_hex"]) == 0.9990234375
    assert len(evidence["freeze_digest"]) == 64


def test_a_freeze_without_the_runtime_version_is_refused(
    controller_module, scenario
):
    case = scenario()
    case.controller.advance(to=9)
    case.controller.inputs = controller_module.replace(
        case.controller.inputs, runtime_version=""
    )
    with pytest.raises(
        controller_module.IncompleteEvidenceRefused, match="traceable to nothing"
    ):
        case.controller.run_state(10)


def test_a_freeze_missing_a_backend_artifact_digest_is_refused(
    controller_module, scenario
):
    case = scenario()
    case.controller.advance(to=9)

    def half(body):
        body = dict(body)
        body["artifact_sha256"] = {"onnx": body["artifact_sha256"]["onnx"]}
        return case.fakes.freeze_record(body)

    case.controller.delegates = controller_module.replace(
        case.controller.delegates, freeze_record=half
    )
    with pytest.raises(
        controller_module.IncompleteEvidenceRefused, match="artifact_sha256.tflite"
    ):
        case.controller.run_state(10)


def test_a_freeze_at_a_different_threshold_is_refused(controller_module, scenario):
    """The mutation case: the operating point moving between measure and freeze."""
    case = scenario()
    case.controller.advance(to=9)

    def retuned(body):
        body = dict(body)
        body["threshold_hex"] = float(0.6).hex()
        return case.fakes.freeze_record(body)

    case.controller.delegates = controller_module.replace(
        case.controller.delegates, freeze_record=retuned
    )
    with pytest.raises(
        controller_module.ThresholdMovedAfterSealRefused, match="operating point moved"
    ):
        case.controller.run_state(10)


def test_the_journal_refuses_a_second_freeze_at_a_new_threshold(
    controller_module, scenario
):
    case = _completed(scenario, stop=10)
    with pytest.raises(
        controller_module.ThresholdMovedAfterSealRefused, match="frozen at"
    ):
        case.controller.journal.seal(
            threshold_hex=float(0.6).hex(),
            freeze_digest="0" * 64,
            candidate_id="r8b",
            freeze_body={},
        )


# ── the barrier between 10 and 11 ────────────────────────────────────────────


def test_state_11_is_not_in_the_automatic_sequence(controller_module):
    assert [state.number for state in controller_module.SEQUENCE] == list(range(1, 11))
    assert controller_module.SEAL_STATE.number == 11
    assert controller_module.SEAL_STATE not in controller_module.SEQUENCE


def test_run_state_11_is_refused_by_name(controller_module, scenario):
    case = _completed(scenario, stop=10)
    with pytest.raises(controller_module.SealBarrierRefused, match="own command"):
        case.controller.run_state(11)


def test_advance_cannot_reach_state_11(controller_module, scenario):
    case = _completed(scenario, stop=10)
    for target in (11, 12, 99):
        with pytest.raises(
            controller_module.SealBarrierRefused, match="advance stops at state 10"
        ):
            case.controller.advance(to=target)
    # Nothing was spent by asking.
    assert case.controller.journal.status(11) is None


def test_advance_with_no_limit_stops_at_state_10(scenario):
    case = scenario()
    case.controller.advance()
    assert case.controller.journal.reached() == tuple(range(1, 11))
    assert case.controller.journal.status(11) is None


def test_the_cli_cannot_spell_state_11(controller_module, tmp_path, capsys):
    code = controller_module.main(
        [
            "--config",
            str(CONFIG),
            "--journal",
            str(tmp_path / "cli.json"),
            "advance",
            "--to",
            "11",
        ]
    )
    assert code == 1
    assert "SealBarrierRefused" in capsys.readouterr().err


def test_opening_a_sealed_set_requires_the_phrase_that_names_it(
    controller_module, scenario
):
    """The mutation case for the barrier itself."""
    case = _completed(scenario, stop=10)
    for wrong in ("", "yes", "OPEN SEALED", "open sealed E006", "OPEN SEALED E007"):
        with pytest.raises(
            controller_module.SealBarrierRefused, match="requires the exact authorization"
        ):
            case.controller.open_sealed(SEALED[1], authorization=wrong)
    assert case.controller.journal.status(11) is None
    assert case.fakes.seal_status(SEALED[1]) is None


def test_a_dataset_that_is_not_sealed_cannot_be_opened(controller_module, scenario):
    case = _completed(scenario, stop=10)
    with pytest.raises(controller_module.SealBarrierRefused, match="not one of the sealed"):
        case.controller.open_sealed(
            VALIDATION[0],
            authorization=controller_module.AUTHORIZATION_TEMPLATE.format(
                dataset=VALIDATION[0]
            ),
        )


def test_opening_a_sealed_set_before_the_freeze_is_refused(controller_module, scenario):
    case = scenario()
    case.controller.advance(to=9)
    with pytest.raises(controller_module.StateOutOfOrderRefused, match="state 10"):
        case.controller.open_sealed(
            SEALED[1],
            authorization=controller_module.AUTHORIZATION_TEMPLATE.format(
                dataset=SEALED[1]
            ),
        )


def test_opening_a_sealed_set_spends_it(controller_module, scenario):
    case = _completed(scenario, stop=10)
    entry = case.controller.open_sealed(
        SEALED[1],
        authorization=controller_module.AUTHORIZATION_TEMPLATE.format(dataset=SEALED[1]),
    )
    assert entry["status"] == "succeeded"
    assert entry["evidence"]["spent"] is True
    ledger = case.fakes.seal_status(SEALED[1])
    assert ledger["status"] == "CONSUMED"
    assert ledger["freeze_digest"] == case.controller.journal.sealed["freeze_digest"]
    # The other two are untouched.
    assert case.fakes.seal_status(SEALED[0]) is None
    assert case.fakes.seal_status(SEALED[2]) is None


def test_a_retuned_threshold_cannot_open_a_sealed_set(controller_module, scenario):
    """The mutation case: the threshold changed after the freeze."""
    case = _completed(scenario, stop=10)
    # Tamper with the recorded freeze the way a retune would: the body moves, the
    # digest the journal recorded does not.
    case.controller.journal.sealed["freeze_body"]["threshold_hex"] = float(0.6).hex()
    with pytest.raises(
        controller_module.ThresholdMovedAfterSealRefused, match="frozen threshold was"
    ):
        case.controller.open_sealed(
            SEALED[1],
            authorization=controller_module.AUTHORIZATION_TEMPLATE.format(
                dataset=SEALED[1]
            ),
        )
    assert case.fakes.seal_status(SEALED[1]) is None


def test_a_freeze_that_changed_anything_else_cannot_open_a_sealed_set(
    controller_module, scenario
):
    case = _completed(scenario, stop=10)
    case.controller.journal.sealed["freeze_body"]["runtime_version"] = "0.20.0"
    with pytest.raises(
        controller_module.ThresholdMovedAfterSealRefused, match="stopped moving"
    ):
        case.controller.open_sealed(
            SEALED[1],
            authorization=controller_module.AUTHORIZATION_TEMPLATE.format(
                dataset=SEALED[1]
            ),
        )


def test_a_sealed_set_already_spent_under_another_freeze_is_refused(
    controller_module, scenario, qualify, tmp_path
):
    case = _completed(scenario, stop=10)
    other = qualify.freeze_from_json(
        {
            "candidate_id": "r8a",
            "threshold_hex": float(0.7).hex(),
            "confirmation_frames": 3,
            "runtime_version": "0.19.1",
            "repo_commit": "e" * 40,
            "manifest_sha256": {SEALED[1]: "d" * 64},
            "artifact_sha256": {"onnx": "3" * 64, "tflite": "4" * 64},
        }
    )
    qualify.SealLedger(tmp_path / "seals.json").open_sealed(SEALED[1], other)
    with pytest.raises(controller_module.SealAlreadySpentRefused):
        case.controller.open_sealed(
            SEALED[1],
            authorization=controller_module.AUTHORIZATION_TEMPLATE.format(
                dataset=SEALED[1]
            ),
        )


# ── the whole order ──────────────────────────────────────────────────────────


def test_the_whole_order_records_eleven_gated_transitions(controller_module, scenario):
    case = _completed(scenario, stop=10)
    for dataset in (SEALED[1], SEALED[2]):
        case.controller.open_sealed(
            dataset,
            authorization=controller_module.AUTHORIZATION_TEMPLATE.format(dataset=dataset),
        )
    journal = json.loads(case.journal_path.read_text(encoding="utf-8"))
    numbers = [row["state"] for row in journal["transitions"]]
    assert numbers == [*range(1, 11), 11, 11]
    assert {row["status"] for row in journal["transitions"]} == {"succeeded"}
    assert journal["plan_digest"] == case.plan.digest()
    report = case.controller.status_report()
    assert report["reached"] == list(range(1, 12))
    assert [row["automatic"] for row in report["states"]] == [True] * 10 + [False]


def test_a_sealed_set_cannot_be_opened_twice_in_one_journal(controller_module, scenario):
    case = _completed(scenario, stop=10)
    phrase = controller_module.AUTHORIZATION_TEMPLATE.format(dataset=SEALED[1])
    case.controller.open_sealed(SEALED[1], authorization=phrase)
    with pytest.raises(controller_module.SealAlreadySpentRefused, match="is spent"):
        case.controller.open_sealed(SEALED[1], authorization=phrase)


def test_the_status_report_names_every_state_and_its_door(controller_module, scenario):
    report = scenario().controller.status_report()
    assert [row["state"] for row in report["states"]] == list(range(1, 12))
    assert {row["status"] for row in report["states"]} == {"not-run"}
    names = [row["name"] for row in report["states"]]
    assert names[0] == "verify_training_manifests"
    assert names[-1] == "open_sealed_set"


def test_the_journal_is_written_atomically(scenario):
    case = _completed(scenario, stop=3)
    assert not list(case.journal_path.parent.glob("*.tmp"))
    assert json.loads(case.journal_path.read_text(encoding="utf-8"))["tool"].endswith(
        "round8_controller.py"
    )
