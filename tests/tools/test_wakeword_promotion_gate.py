"""A wake-word model may only ship through the gate, and only with the evidence.

Background
----------
``tools/wakewords/hey_youtab.onnx`` and ``.tflite`` are the detector Youtab
ships. Eleven candidates across seven synthetic rounds all failed acceptance,
and the shipped pair is itself a synthetic-era export listed in
``scripts/wakeword/retired_synthetic_artifacts.json`` as retired. So the failure
mode this file exists to make impossible is not a bug: it is somebody copying a
file into ``tools/wakewords/`` and calling it a release.

What is asserted here, in order:

* the **non-vacuity control** first — a fully valid fixture promotion succeeds.
  Without it every refusal below could be a gate that refuses everything, which
  proves nothing at all;
* each required item, degraded one at a time, is refused by name;
* a 5/5 candidate on 11.659 h of negative audio is refused as underpowered, even
  though it observed zero events, because that sample bounds 0.257/h and the
  target is 0.2/h;
* the pair this repository ships today is refused as a candidate, using the real
  registry and the real artifacts;
* a refused promotion writes nothing;
* a crash between the two artifact replaces resolves in one direction — never a
  mixture — and a rollback restores the previous bytes byte-for-byte, compared
  with ``filecmp`` at ``shallow=False``.

Hermetic. Every promotion here happens inside a throwaway git repository under
``tmp_path``: a checkout with its own workflow, its own retired-artifact
registry, its own candidate and its own ``tools/wakewords``. The real
``tools/wakewords/`` is read (to prove the shipped pair is refused) and never
written; the three tests that touch the real repository at all only read it.

git is a hard requirement rather than a skip. "Tracked, clean, and the same blob
the tested commit carried" is the check that ties promoted bytes to reviewed
bytes, and a suite that silently skipped it would be reporting on a gate with
its centre removed.
"""

from __future__ import annotations

import filecmp
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
SHIPPED = REPO / "tools" / "wakewords"


def _load(name: str, filename: str):
    """Load a pipeline script by path, the way the other wake-word tests do.

    ``sys.modules`` registration is not optional: ``@dataclass`` resolves string
    annotations through ``sys.modules[cls.__module__]``, and a module loaded from
    a spec without being registered has no entry there.
    """
    spec = importlib.util.spec_from_file_location(name, WAKEWORD / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pm = _load("_promote_model", "promote_model.py")
r8 = _load("_round8_config_for_promotion", "round8_config.py")

ONNX = "hey_youtab.onnx"
TFLITE = "hey_youtab.tflite"

#: The bytes that ship today, and their blob ids. Both digests are in the
#: retired registry, which is why the pair cannot be re-promoted as a candidate.
SHIPPED_ONNX_SHA256 = "59d35d33703e105739daaa0284eef4629ff2aeaa973d6a3d4fe8183cf1699d79"
SHIPPED_TFLITE_SHA256 = "c2b87ca6e7420cb4943fd53ff9f0d136ef39b226c0f556ecc800d32ed6a8ae73"

#: Bounds taken from ``round8_config.json``'s own published tables rather than
#: computed by the module under test, so the fixture's evidence is not the gate's
#: own arithmetic handed back to it. ``test_the_bounds_are_the_predeclared_ones``
#: is what proves the two agree.
BOUND_144_POSITIVE_UTTERANCES = 0.0206
BOUND_168_NEAR_PHRASE_UTTERANCES = 0.0177
BOUND_61_BACKGROUND_WINDOWS = 0.047924
BOUND_31986_WINDOWS_COMPARED = 9.4e-05
BOUND_20_HOURS_PER_HOUR = 0.15

#: The sample the predeclaration says cannot demonstrate target 2: Speech
#: Commands' eval partition. Zero fires over it bounds 0.257/h, and the target is
#: 0.2/h.
UNDERPOWERED_HOURS = 11.659
UNDERPOWERED_BOUND_PER_HOUR = 0.257

#: A CI workflow shaped like this repository's: two single-leg jobs and one
#: `os` matrix with an `include:`. ``test_the_real_workflow_produces_the_five...``
#: proves the shape is the real one.
FIXTURE_WORKFLOW = """\
name: Youtab Agent Runtime gates
on:
  pull_request:
  push:
    branches: [main]
jobs:
  python-security:
    runs-on: ubuntu-latest
    steps:
      - run: echo gates
  wake-word-backends:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        include:
          - os: macos-latest
    runs-on: ${{ matrix.os }}
    steps:
      - run: echo backends
  javascript:
    runs-on: ubuntu-latest
    steps:
      - run: echo npm
  windows-runtime-cli:
    runs-on: windows-latest
    steps:
      - run: echo windows-runtime-cli
  windows-tools:
    runs-on: windows-latest
    steps:
      - run: echo windows-tools
"""

CI_CONTEXTS = {
    "python-security": "success",
    "javascript": "success",
    "wake-word-backends (ubuntu-latest)": "success",
    "wake-word-backends (windows-latest)": "success",
    "wake-word-backends (macos-latest)": "success",
    "windows-runtime-cli": "success",
    "windows-tools": "success",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("git")
    if exe is None:  # pragma: no cover - environment
        pytest.fail(
            "git is not on PATH. This gate's authorization and its 'promoted bytes "
            "are reviewed bytes' check are statements about the repository, and a "
            "suite that skipped them would describe a different gate."
        )
    done = subprocess.run(
        [
            exe,
            "-C",
            str(repo),
            "-c",
            "user.email=gate@example.invalid",
            "-c",
            "user.name=promotion gate test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stdout}{done.stderr}"
    return done


def _snapshot(root: Path) -> dict[str, str]:
    """Every file under ``root``, by digest. Used to prove nothing was written."""
    return {
        path.relative_to(root).as_posix(): _sha256(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class Promotion:
    """A throwaway checkout carrying one promotable candidate.

    Everything the gate reads lives here: the workflow, the retired-artifact
    registry, the candidate's artifacts and dataset contracts, the Owner decision
    document, the promotion record, and a ``tools/wakewords`` holding a model
    that already ships. Mutate ``self.record`` and call :meth:`seal` to produce a
    record that is internally consistent — digest, decision document and commit —
    so a test degrades exactly one required item and nothing else.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "checkout"
        self.candidate_dir = self.repo / "candidates" / "r9c1"
        self.dest = self.repo / "tools" / "wakewords"
        self.workflow = self.repo / ".github" / "workflows" / "youtab-ci.yml"
        self.registry = self.repo / "scripts" / "wakeword" / "retired_synthetic_artifacts.json"
        self.record_path = self.candidate_dir / "promotion-record.json"
        self.decision_path = self.repo / "docs" / "decisions" / "wakeword-r9c1.md"

        for directory in (
            self.candidate_dir,
            self.dest,
            self.workflow.parent,
            self.registry.parent,
            self.decision_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.workflow.write_text(FIXTURE_WORKFLOW, encoding="utf-8")
        self.write_registry({_sha256(b"a retired r7 export"): "candidates/r7/hey_youtab.onnx"})

        # The model that already ships, and the sums that describe it.
        self.previous = {ONNX: b"shipped onnx bytes v0", TFLITE: b"shipped tflite bytes v0"}
        for name, data in self.previous.items():
            (self.dest / name).write_bytes(data)
        self.write_sums(self.dest, self.previous)

        # The candidate.
        self.artifacts = {
            ONNX: b"candidate r9c1 onnx bytes",
            TFLITE: b"candidate r9c1 tflite bytes",
        }
        for name, data in self.artifacts.items():
            (self.candidate_dir / name).write_bytes(data)
        self.write_contracts()

        _git(self.repo, "init", "--quiet")
        self.decision_path.write_text("placeholder\n", encoding="utf-8")
        self.record_path.write_text("{}\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "--quiet", "-m", "candidate r9c1, measured and tested")
        self.base_commit = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

        self.record = self.build_record()
        self.seal()

    # ── building the fixture ────────────────────────────────────────────────

    @staticmethod
    def write_sums(directory: Path, payload: dict[str, bytes]) -> None:
        directory.joinpath("SHA256SUMS").write_text(
            "".join(f"{_sha256(payload[name])}  {name}\n" for name in (ONNX, TFLITE)),
            encoding="utf-8",
            newline="\n",
        )

    def write_registry(self, entries: dict[str, str]) -> None:
        self.registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "artifacts": [
                        {"sha256": digest, "bytes": 1, "kind": "rejected_export", "name": name}
                        for digest, name in entries.items()
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def write_contracts(self) -> None:
        """The contracts ``build_human_dataset.py`` writes beside a built split."""
        for split, manifest in (("train", "1" * 64), ("validation", "2" * 64)):
            (self.candidate_dir / f"{split}.DATASET_CONTRACT.json").write_text(
                json.dumps(
                    {
                        "round": 9,
                        "split": split,
                        "human_only": True,
                        "synthetic_samples": 0,
                        "sources": [{"type": "human", "manifest_sha256": manifest}],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    def digest(self, name: str) -> str:
        return _sha256(self.artifacts[name])

    def build_record(self) -> dict:
        return {
            "schema_version": 1,
            "candidate": {
                "id": "r9c1",
                "round": 9,
                "threshold": 0.9311742,
                "commit": self.base_commit,
            },
            "artifacts": {
                name: {"sha256": self.digest(name), "bytes": len(self.artifacts[name])}
                for name in (ONNX, TFLITE)
            },
            "acceptance": {
                backend: {
                    "artifact_sha256": self.digest(artifact),
                    "threshold": 0.9311742,
                    "targets": [
                        {"id": 1, "limit": 0.05, "comparison": "<=",
                         "observed": 0.0, "passed": True},
                        {"id": 2, "limit": 0.2, "comparison": "<=",
                         "observed": 0.0, "passed": True},
                        {"id": 3, "limit": 0.02, "comparison": "<=",
                         "observed": 0.0, "passed": True},
                        {"id": 4, "limit": 0, "comparison": "==", "observed": 0, "passed": True},
                        {"id": 5, "limit": 0, "comparison": "==", "observed": 0, "passed": True},
                    ],
                }
                for backend, artifact in (("onnx", ONNX), ("tflite", TFLITE))
            },
            "parity": {
                "measured": True,
                "windows": 31986,
                "detection_disagreements": 0,
                "max_absolute_frame_score_difference": 2.7e-06,
            },
            "statistics": {
                "alpha": 0.05,
                "targets": [
                    {
                        "id": 1,
                        "unit": "positive utterances",
                        "n": 144,
                        "events": {"onnx": 0, "tflite": 0},
                        "bound": {
                            "onnx": BOUND_144_POSITIVE_UTTERANCES,
                            "tflite": BOUND_144_POSITIVE_UTTERANCES,
                        },
                    },
                    {
                        "id": 2,
                        "unit": "hours of recorded negative audio",
                        "hours": 20.0,
                        "events": {"onnx": 0, "tflite": 0},
                        "bound": {
                            "onnx": BOUND_20_HOURS_PER_HOUR,
                            "tflite": BOUND_20_HOURS_PER_HOUR,
                        },
                    },
                    {
                        "id": 3,
                        "unit": "near-phrase utterances",
                        "n": 168,
                        "events": {"onnx": 0, "tflite": 0},
                        "bound": {
                            "onnx": BOUND_168_NEAR_PHRASE_UTTERANCES,
                            "tflite": BOUND_168_NEAR_PHRASE_UTTERANCES,
                        },
                    },
                    {
                        "id": 4,
                        "unit": "background windows",
                        "n": 61,
                        "events": {"onnx": 0, "tflite": 0},
                        "bound": {
                            "onnx": BOUND_61_BACKGROUND_WINDOWS,
                            "tflite": BOUND_61_BACKGROUND_WINDOWS,
                        },
                    },
                    {
                        "id": 5,
                        "unit": "windows compared on both backends",
                        "n": 31986,
                        "events": {"onnx": 0, "tflite": 0},
                        "bound": {
                            "onnx": BOUND_31986_WINDOWS_COMPARED,
                            "tflite": BOUND_31986_WINDOWS_COMPARED,
                        },
                    },
                ],
            },
            "sources": [
                {
                    "dataset": "E001",
                    "role": "train",
                    "usage": "training",
                    "kind": "human_recording",
                    "synthetic": False,
                    "manifest_sha256": "1" * 64,
                    "licence": "owner-authorized project recording, not redistributed as audio",
                    "provenance": "recorded 2026-08, owner-authorized takes, frozen on the host",
                    "redistributable": True,
                },
                {
                    "dataset": "E005",
                    "role": "validation",
                    "usage": "validation",
                    "kind": "human_recording",
                    "synthetic": False,
                    "manifest_sha256": "2" * 64,
                    "licence": "owner-authorized project recording, not redistributed as audio",
                    "provenance": "recorded 2026-08, owner-authorized takes, frozen on the host",
                    "redistributable": True,
                },
                {
                    "dataset": "common_voice_en_governed_subset",
                    "role": "sealed",
                    "usage": "sealed-evaluation",
                    "kind": "recorded_corpus",
                    "synthetic": False,
                    "manifest_sha256": "3" * 64,
                    "licence": "CC0-1.0",
                    "provenance": "Mozilla Common Voice en, governed bounded subset, pinned lock",
                    "redistributable": True,
                },
            ],
            "lineage": {
                "initialised_from_synthetic_checkpoint": False,
                "checkpoint_sha256": _sha256(b"r9c1 checkpoint"),
                "dataset_contracts": [
                    {
                        "path": "train.DATASET_CONTRACT.json",
                        "sha256": _sha256(
                            (self.candidate_dir / "train.DATASET_CONTRACT.json").read_bytes()
                        ),
                    },
                    {
                        "path": "validation.DATASET_CONTRACT.json",
                        "sha256": _sha256(
                            (self.candidate_dir / "validation.DATASET_CONTRACT.json").read_bytes()
                        ),
                    },
                ],
            },
            "ci": {
                "commit": self.base_commit,
                "conclusion": "success",
                "run_id": "17412990001",
                "contexts": dict(CI_CONTEXTS),
            },
            "authorization": {
                "method": "repository-governed",
                "record_sha256": "",
                "owner_decision": {"path": "docs/decisions/wakeword-r9c1.md", "sha256": ""},
            },
        }

    def decision_text(self) -> str:
        candidate = self.record["candidate"]
        return (
            f"# Owner decision — promote {candidate['id']}\n\n"
            "Authorized for promotion into `tools/wakewords/`, on the evidence in the\n"
            "promotion record whose digest is quoted below. Any change to that evidence\n"
            "changes the digest and voids this decision.\n\n"
            f"- candidate: {candidate['id']}\n"
            f"- promotion record sha256: {pm.record_digest(self.record)}\n"
            f"- {ONNX} sha256: {self.record['artifacts'][ONNX]['sha256']}\n"
            f"- {TFLITE} sha256: {self.record['artifacts'][TFLITE]['sha256']}\n"
        )

    def seal(self, *, decision: str | None = None, commit: bool = True) -> None:
        """Make the record self-consistent, then commit it and the decision.

        The order is the real one: the evidence digest first, then a decision that
        quotes it, then the decision's own hash back into the record.
        """
        self.decision_path.write_text(
            self.decision_text() if decision is None else decision, encoding="utf-8"
        )
        authorization = self.record.get("authorization")
        if isinstance(authorization, dict):
            authorization["record_sha256"] = pm.record_digest(self.record)
            if isinstance(authorization.get("owner_decision"), dict):
                authorization["owner_decision"]["sha256"] = _sha256(
                    self.decision_path.read_bytes()
                )
        self.write_record()
        if commit:
            _git(self.repo, "add", "-A")
            _git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "seal the promotion record")

    def write_record(self) -> None:
        self.record_path.write_text(
            json.dumps(self.record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def rebaseline(self) -> None:
        """Commit the candidate as it now stands and point the record at that commit.

        The real sequence: artifacts are committed, CI runs on that commit, then the
        record citing the run is committed on top. Used by tests that change the
        artifacts and want the *other* checks to stay satisfied.
        """
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "re-measure the candidate")
        self.base_commit = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.record["candidate"]["commit"] = self.base_commit
        self.record["ci"]["commit"] = self.base_commit
        self.seal()

    def set_artifact(self, name: str, data: bytes) -> None:
        """Replace a candidate artifact and keep every digest in the record true."""
        self.artifacts[name] = data
        (self.candidate_dir / name).write_bytes(data)
        self.record["artifacts"][name] = {"sha256": _sha256(data), "bytes": len(data)}
        backend = "onnx" if name == ONNX else "tflite"
        self.record["acceptance"][backend]["artifact_sha256"] = _sha256(data)

    # ── running the gate ────────────────────────────────────────────────────

    def context(self, **overrides):
        arguments = {
            "record_path": self.record_path,
            "candidate_dir": self.candidate_dir,
            "dest": self.dest,
            "repo_root": self.repo,
            "workflow": self.workflow,
            "registry": self.registry,
        }
        arguments.update(overrides)
        return pm.build_context(**arguments)

    def verdict(self, **overrides):
        return pm.evaluate(self.context(**overrides))

    def failed(self, **overrides) -> set[str]:
        return set(self.verdict(**overrides).failed_checks())

    def messages(self, check_id: str, **overrides) -> str:
        verdict = self.verdict(**overrides)
        return "\n".join(
            message
            for name, failures in verdict.results
            if name == check_id
            for message in failures
        )

    def target(self, target_id: int) -> dict:
        return next(
            entry for entry in self.record["statistics"]["targets"] if entry["id"] == target_id
        )

    def acceptance_target(self, backend: str, target_id: int) -> dict:
        return next(
            entry
            for entry in self.record["acceptance"][backend]["targets"]
            if entry["id"] == target_id
        )


@pytest.fixture()
def promotion(tmp_path: Path) -> Promotion:
    return Promotion(tmp_path)


def _refuses(promotion: Promotion, check_id: str) -> str:
    """Assert the named check refused, and that the verdict is not promotable."""
    verdict = promotion.verdict()
    assert check_id in verdict.failed_checks(), (
        f"{check_id} passed; failed checks were {verdict.failed_checks()}"
    )
    assert not verdict.passed
    with pytest.raises(pm.Refused, match=r"refusing to promote"):
        pm.promote(promotion.context())
    return promotion.messages(check_id)


# ── the non-vacuity control ──────────────────────────────────────────────────


def test_a_fully_valid_fixture_promotion_succeeds(promotion: Promotion) -> None:
    """First, because every refusal below is worthless without it.

    A gate that refused everything would satisfy all seventeen negative tests in
    this file and ship nothing, forever, for the wrong reason.
    """
    verdict = promotion.verdict()
    assert verdict.passed, verdict.failures
    # Non-vacuity of the verdict itself: all eighteen checks ran and each one is
    # a real entry, not an empty registry that trivially has no failures.
    assert len(verdict.results) == len(pm.CHECKS) == 18
    assert {name for name, _ in verdict.results} == {name for name, _, _ in pm.CHECKS}

    report = pm.promote(promotion.context())

    assert report["to"] == {name: promotion.digest(name) for name in (ONNX, TFLITE)}
    for name in (ONNX, TFLITE):
        assert filecmp.cmp(
            promotion.dest / name, promotion.candidate_dir / name, shallow=False
        ), f"{name} was not promoted byte-for-byte"
    assert (promotion.dest / "SHA256SUMS").read_text(encoding="utf-8") == "".join(
        f"{promotion.digest(name)}  {name}\n" for name in (ONNX, TFLITE)
    )
    assert not (promotion.dest / pm.JOURNAL_NAME).exists(), "the journal outlived the promotion"

    ledger = pm.read_ledger(promotion.dest)
    assert [entry["action"] for entry in ledger["entries"]] == ["promote"]
    assert ledger["entries"][0]["candidate"] == "r9c1"
    assert pm.verify_shipped(promotion.dest)["passed"]


def test_the_previously_shipped_model_is_preserved_as_a_rollback_artifact(
    promotion: Promotion,
) -> None:
    report = pm.promote(promotion.context())
    bundle = promotion.dest / report["rollback_bundle"]

    for name, data in promotion.previous.items():
        assert (bundle / name).read_bytes() == data, f"{name} was not preserved intact"
    manifest = json.loads((bundle / "ROLLBACK.json").read_text(encoding="utf-8"))
    assert manifest["preserved_from"] == {
        name: _sha256(data) for name, data in promotion.previous.items()
    }
    assert manifest["replaced_by_candidate"] == "r9c1"


# ── each required item, degraded one at a time ───────────────────────────────


def test_a_record_from_another_schema_is_refused_rather_than_guessed_at(
    promotion: Promotion,
) -> None:
    promotion.record["schema_version"] = 99
    promotion.seal()
    assert "schema_version" in _refuses(promotion, "record_schema")


def test_a_record_missing_a_whole_section_is_refused(promotion: Promotion) -> None:
    del promotion.record["statistics"]
    promotion.seal()
    assert "'statistics'" in _refuses(promotion, "record_schema")


def test_an_unnamed_candidate_cannot_be_promoted(promotion: Promotion) -> None:
    promotion.record["candidate"]["id"] = ""
    promotion.seal()
    assert "candidate.id" in _refuses(promotion, "candidate_identity")


def test_a_synthetic_era_round_cannot_be_promoted(promotion: Promotion) -> None:
    """Rounds 1-7 are retired, and a candidate declaring one is refused by number."""
    promotion.record["candidate"]["round"] = 7
    promotion.seal()
    assert "retired synthetic experiments" in _refuses(promotion, "candidate_identity")


def test_a_candidate_with_no_operating_threshold_cannot_be_promoted(
    promotion: Promotion,
) -> None:
    """The threshold is folded into the exported bias, so it is part of identity."""
    del promotion.record["candidate"]["threshold"]
    promotion.seal()
    assert "threshold" in _refuses(promotion, "candidate_identity")


def test_an_artifact_whose_bytes_do_not_match_the_record_is_refused(
    promotion: Promotion,
) -> None:
    promotion.seal()
    (promotion.candidate_dir / ONNX).write_bytes(b"different bytes entirely")
    assert "hashes to" in _refuses(promotion, "artifact_bytes")


def test_a_missing_tflite_artifact_is_refused(promotion: Promotion) -> None:
    """Half a model is not a model: macOS ARM64 loads the tflite pair."""
    (promotion.candidate_dir / TFLITE).unlink()
    promotion.seal()
    assert "not in the candidate directory" in _refuses(promotion, "artifact_bytes")


def test_acceptance_measured_against_other_bytes_is_refused(promotion: Promotion) -> None:
    """What is promoted has to be what was measured."""
    promotion.record["acceptance"]["onnx"]["artifact_sha256"] = _sha256(b"some other export")
    promotion.seal()
    assert "was measured against" in _refuses(promotion, "measured_artifacts")


def test_acceptance_measured_at_another_threshold_is_refused(promotion: Promotion) -> None:
    promotion.record["acceptance"]["tflite"]["threshold"] = 0.6
    promotion.seal()
    assert "another detector" in _refuses(promotion, "measured_artifacts")


def test_an_untracked_candidate_cannot_be_promoted(promotion: Promotion) -> None:
    """An untracked artifact has been reviewed by nobody and tested by CI never."""
    loose = promotion.repo / "loose"
    shutil.copytree(promotion.candidate_dir, loose)
    messages = promotion.messages("committed_artifacts", candidate_dir=loose)
    assert "not tracked in git" in messages
    assert not promotion.verdict(candidate_dir=loose).passed


def test_bytes_that_differ_from_the_tested_commit_are_refused(promotion: Promotion) -> None:
    """Tracked and clean is not enough: CI went green on a blob, not a filename.

    The record is kept truthful here — digests, sizes and the acceptance hashes
    all updated — so the only thing wrong is that the tested commit's tree holds
    different bytes under that name.
    """
    promotion.set_artifact(ONNX, b"a quietly reworked export")
    promotion.seal()
    messages = _refuses(promotion, "committed_artifacts")
    assert "the bytes CI saw are not the bytes on disk" in messages


def test_four_of_five_on_one_backend_is_refused_and_the_other_backend_is_untouched(
    promotion: Promotion,
) -> None:
    """Both backends are measured independently; neither carries the other."""
    entry = promotion.acceptance_target("onnx", 1)
    entry["observed"] = 0.0555
    entry["passed"] = False
    promotion.seal()

    failed = promotion.failed()
    assert "acceptance_onnx" in failed
    assert "acceptance_tflite" not in failed, "a bad ONNX result was attributed to tflite"
    assert "target 1 (false rejects) failed" in promotion.messages("acceptance_onnx")
    with pytest.raises(pm.Refused, match=r"refusing to promote"):
        pm.promote(promotion.context())


def test_four_of_five_on_the_tflite_backend_is_refused(promotion: Promotion) -> None:
    entry = promotion.acceptance_target("tflite", 3)
    entry["observed"] = 0.03
    entry["passed"] = False
    promotion.seal()

    failed = promotion.failed()
    assert "acceptance_tflite" in failed
    assert "acceptance_onnx" not in failed
    assert "near-miss" in promotion.messages("acceptance_tflite")


def test_a_failing_target_declared_as_passed_is_refused(promotion: Promotion) -> None:
    """The boolean is never trusted; the comparison is recomputed."""
    promotion.acceptance_target("onnx", 4)["observed"] = 2
    promotion.seal()
    messages = _refuses(promotion, "acceptance_onnx")
    assert "the claim and the arithmetic disagree" in messages


def test_a_record_cannot_relax_a_target(promotion: Promotion) -> None:
    promotion.acceptance_target("onnx", 1)["limit"] = 0.5
    promotion.seal()
    assert "does not get to relax it" in _refuses(promotion, "acceptance_onnx")


def test_a_target_that_was_not_measured_at_all_is_refused(promotion: Promotion) -> None:
    targets = promotion.record["acceptance"]["tflite"]["targets"]
    promotion.record["acceptance"]["tflite"]["targets"] = [
        entry for entry in targets if entry["id"] != 2
    ]
    promotion.seal()
    assert "5/5 means all five were measured" in _refuses(promotion, "acceptance_tflite")


def test_one_detection_parity_disagreement_is_refused(promotion: Promotion) -> None:
    promotion.record["parity"]["detection_disagreements"] = 1
    for backend in ("onnx", "tflite"):
        promotion.acceptance_target(backend, 5)["observed"] = 1
    promotion.target(5)["events"] = {"onnx": 1, "tflite": 1}
    promotion.seal()
    assert "one disagreement is one user" in _refuses(promotion, "parity")


def test_parity_that_was_never_measured_is_not_zero(promotion: Promotion) -> None:
    promotion.record["parity"]["measured"] = False
    promotion.seal()
    assert "cannot be zero" in _refuses(promotion, "parity")


def test_a_parity_window_count_that_disagrees_with_the_statistics_is_refused(
    promotion: Promotion,
) -> None:
    promotion.record["parity"]["windows"] = 512
    promotion.seal()
    assert "statistics target 5 compares" in _refuses(promotion, "parity")


def test_a_point_estimate_with_no_denominator_is_not_evidence(promotion: Promotion) -> None:
    """"0.000 per hour" over no stated hours is a number that cannot be wrong."""
    del promotion.target(2)["hours"]
    promotion.seal()
    messages = _refuses(promotion, "statistical_completeness")
    assert "a point estimate bounds nothing" in messages


def test_evidence_counted_in_windows_where_the_unit_is_the_utterance_is_refused(
    promotion: Promotion,
) -> None:
    """Seven deterministic framings of one utterance are one trial, not seven."""
    promotion.target(1)["unit"] = "positive windows"
    promotion.seal()
    assert "one utterance are one trial" in _refuses(promotion, "statistical_completeness")


def test_a_record_cannot_relax_the_confidence_level(promotion: Promotion) -> None:
    promotion.record["statistics"]["alpha"] = 0.5
    promotion.seal()
    assert "buy power by relaxing confidence" in _refuses(promotion, "statistical_completeness")


def test_a_five_of_five_candidate_on_eleven_point_seven_hours_is_refused(
    promotion: Promotion,
) -> None:
    """The packet's case, and the one a point estimate would wave through.

    Speech Commands' eval partition is 11.659 h. A perfect clean run over it
    bounds the activation rate at 0.257/h, and target 2 is 0.2/h — so acceptance
    is genuinely 5/5 on both backends and the candidate is still not promotable.
    """
    target = promotion.target(2)
    target["hours"] = UNDERPOWERED_HOURS
    target["bound"] = {
        "onnx": UNDERPOWERED_BOUND_PER_HOUR,
        "tflite": UNDERPOWERED_BOUND_PER_HOUR,
    }
    promotion.seal()

    failed = promotion.failed()
    assert "acceptance_onnx" not in failed and "acceptance_tflite" not in failed, (
        "the candidate is supposed to be 5/5; this test would then prove nothing"
    )
    assert failed == {"statistical_power"}, failed
    messages = promotion.messages("statistical_power")
    assert "underpowered" in messages
    assert "11.659 hours bounds the rate at 0.2569" in messages
    assert "needs at least 14.98 hours" in messages

    before = _snapshot(promotion.dest)
    with pytest.raises(pm.Refused, match=r"refusing to promote"):
        pm.promote(promotion.context())
    assert _snapshot(promotion.dest) == before, "a refused promotion wrote to the destination"


def test_thirty_six_positive_utterances_cannot_demonstrate_five_percent(
    promotion: Promotion,
) -> None:
    """The validation-gate sample size is 'not contradicted', not demonstrated.

    36 clean positives bound the false-reject rate at 0.0798. Promotion needs the
    bound to be under the target, so the sample the round 8 gate uses to *select*
    is refused as evidence to *ship*.
    """
    target = promotion.target(1)
    target["n"] = 36
    target["bound"] = {"onnx": 0.0798, "tflite": 0.0798}
    promotion.seal()

    assert promotion.failed() == {"statistical_power"}
    messages = promotion.messages("statistical_power")
    assert "underpowered" in messages and "59 trials" in messages


def test_an_observed_event_beyond_what_the_sample_supports_is_refused(
    promotion: Promotion,
) -> None:
    """Non-zero events are bounded too, not waved through as "still under 5%"."""
    target = promotion.target(1)
    target["events"] = {"onnx": 4, "tflite": 0}
    target["bound"] = {"onnx": 0.0625, "tflite": BOUND_144_POSITIVE_UTTERANCES}
    promotion.acceptance_target("onnx", 1)["observed"] = 4 / 144
    promotion.seal()
    messages = _refuses(promotion, "statistical_power")
    assert "4 event(s) in 144 trials" in messages


def test_a_stated_bound_the_sample_does_not_support_is_refused(promotion: Promotion) -> None:
    """The optimistic-typo case: a bound copied from a bigger run."""
    promotion.target(3)["bound"] = {"onnx": 0.001, "tflite": 0.001}
    promotion.seal()
    messages = _refuses(promotion, "statistical_consistency")
    assert "0 event(s) in 168 trials gives 0.0176737" in messages


def test_an_acceptance_figure_that_contradicts_its_own_sample_is_refused(
    promotion: Promotion,
) -> None:
    promotion.target(2)["events"] = {"onnx": 3, "tflite": 0}
    promotion.target(2)["bound"] = {"onnx": 0.3901, "tflite": BOUND_20_HOURS_PER_HOUR}
    promotion.seal()
    messages = promotion.messages("statistical_consistency")
    assert "but statistics records 3 event(s)" in messages
    assert not promotion.verdict().passed


def test_a_dataset_with_no_frozen_manifest_digest_is_refused(promotion: Promotion) -> None:
    promotion.record["sources"][0]["manifest_sha256"] = ""
    promotion.seal()
    messages = _refuses(promotion, "source_manifests")
    assert "cannot be re-verified later" in messages


def test_a_sealed_dataset_declared_as_training_usage_is_refused(promotion: Promotion) -> None:
    promotion.record["sources"][2]["usage"] = "training"
    promotion.seal()
    assert "requires usage" in _refuses(promotion, "source_manifests")


def test_a_candidate_with_no_sealed_measurement_is_refused(promotion: Promotion) -> None:
    promotion.record["sources"] = promotion.record["sources"][:2]
    promotion.seal()
    messages = _refuses(promotion, "source_manifests")
    assert "['sealed']" in messages


def test_a_source_with_no_licence_is_refused(promotion: Promotion) -> None:
    promotion.record["sources"][2]["licence"] = ""
    promotion.seal()
    assert "no licence recorded" in _refuses(promotion, "licence_provenance")


def test_a_source_with_no_provenance_is_refused(promotion: Promotion) -> None:
    promotion.record["sources"][1]["provenance"] = "   "
    promotion.seal()
    assert "no provenance recorded" in _refuses(promotion, "licence_provenance")


def test_a_human_recording_needs_no_consent_record(promotion: Promotion) -> None:
    # Authorization to use a human voice is the project-level registry fact, not
    # a per-source document. A human_recording source with no consent_record
    # referenced still clears the licence/provenance gate.
    for source in promotion.record["sources"]:
        source.pop("consent_record", None)
    promotion.seal()
    assert "licence_provenance" not in promotion.failed()


def test_a_synthesized_source_has_no_kind_it_can_be_declared_under(
    promotion: Promotion,
) -> None:
    """The allow-list is the point: 'tts' is not a value this gate accepts."""
    promotion.record["sources"][0]["kind"] = "tts"
    promotion.seal()
    messages = _refuses(promotion, "no_synthetic_lineage")
    assert "has no kind here to be named" in messages


def test_a_source_that_admits_it_is_synthetic_is_refused(promotion: Promotion) -> None:
    promotion.record["sources"][1]["synthetic"] = True
    promotion.seal()
    assert "synthetic=false" in _refuses(promotion, "no_synthetic_lineage")


def test_warm_starting_from_a_synthetic_checkpoint_is_refused(promotion: Promotion) -> None:
    promotion.record["lineage"]["initialised_from_synthetic_checkpoint"] = True
    promotion.seal()
    assert "carries synthetic training into what ships" in _refuses(
        promotion, "no_synthetic_lineage"
    )


def test_a_dataset_contract_that_measured_synthetic_samples_is_refused(
    promotion: Promotion,
) -> None:
    """`synthetic_samples: 0` is a measurement, and a non-zero one is a refusal."""
    contract = promotion.candidate_dir / "train.DATASET_CONTRACT.json"
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["synthetic_samples"] = 4
    contract.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    promotion.record["lineage"]["dataset_contracts"][0]["sha256"] = _sha256(contract.read_bytes())
    promotion.seal()
    assert "does not assert a human-only dataset" in _refuses(promotion, "no_synthetic_lineage")


def test_a_promotion_with_no_dataset_contract_at_all_is_refused(promotion: Promotion) -> None:
    promotion.record["lineage"]["dataset_contracts"] = []
    promotion.seal()
    messages = _refuses(promotion, "no_synthetic_lineage")
    assert "not from this record restating it" in messages


def test_a_training_manifest_no_contract_accounts_for_is_refused(promotion: Promotion) -> None:
    """A source can be listed without ever having been built into a tensor."""
    promotion.record["sources"][0]["manifest_sha256"] = "9" * 64
    promotion.seal()
    messages = _refuses(promotion, "no_synthetic_lineage")
    assert "no dataset contract records having consumed it" in messages


def test_a_contract_whose_bytes_no_longer_match_is_refused(promotion: Promotion) -> None:
    contract = promotion.candidate_dir / "validation.DATASET_CONTRACT.json"
    contract.write_text(contract.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    promotion.seal()
    assert "hashes to" in _refuses(promotion, "no_synthetic_lineage")


def test_retired_bytes_are_refused_wherever_they_sit_and_whatever_they_are_called(
    promotion: Promotion,
) -> None:
    """A relocated, renamed, correctly-manifested retired export is still refused."""
    promotion.write_registry(
        {promotion.digest(ONNX): "candidates/r5c2/hey_youtab.onnx"}
    )
    promotion.seal()
    messages = _refuses(promotion, "retired_artifacts")
    assert "lists as retired synthetic material" in messages
    assert "candidates/r5c2" in messages


def test_a_retired_training_checkpoint_is_refused_as_a_lineage(promotion: Promotion) -> None:
    promotion.write_registry(
        {promotion.record["lineage"]["checkpoint_sha256"]: "candidates/r7/checkpoint.pt"}
    )
    promotion.seal()
    assert "the training checkpoint" in _refuses(promotion, "retired_artifacts")


def test_a_registry_that_cannot_be_read_is_a_refusal_and_not_a_pass(
    promotion: Promotion,
) -> None:
    promotion.registry.unlink()
    assert "could not be read" in _refuses(promotion, "retired_artifacts")


def test_a_registry_from_another_schema_is_refused(promotion: Promotion) -> None:
    promotion.registry.write_text(
        json.dumps({"schema_version": 2, "artifacts": [{"sha256": "0" * 64}]}), encoding="utf-8"
    )
    assert "schema_version" in _refuses(promotion, "retired_artifacts")


def test_an_empty_registry_recognises_nothing_and_is_refused(promotion: Promotion) -> None:
    promotion.registry.write_text(
        json.dumps({"schema_version": 1, "artifacts": []}), encoding="utf-8"
    )
    assert "recognises nothing" in _refuses(promotion, "retired_artifacts")


def test_a_ci_leg_that_never_ran_is_not_a_leg_that_passed(promotion: Promotion) -> None:
    del promotion.record["ci"]["contexts"]["wake-word-backends (macos-latest)"]
    promotion.seal()
    messages = _refuses(promotion, "ci_success")
    assert "wake-word-backends (macos-latest)" in messages


def test_a_red_ci_context_is_refused(promotion: Promotion) -> None:
    promotion.record["ci"]["contexts"]["javascript"] = "failure"
    promotion.seal()
    assert "concluded 'failure'" in _refuses(promotion, "ci_success")


def test_a_ci_run_on_another_commit_is_a_run_on_another_tree(promotion: Promotion) -> None:
    promotion.record["ci"]["commit"] = "0" * 40
    promotion.seal()
    assert "another tree" in _refuses(promotion, "ci_success")


def test_a_commit_this_history_does_not_contain_is_refused(promotion: Promotion) -> None:
    promotion.record["candidate"]["commit"] = "0" * 40
    promotion.record["ci"]["commit"] = "0" * 40
    promotion.seal()
    assert "not a commit in this repository" in _refuses(promotion, "ci_success")


def test_an_unsupported_authorization_method_is_refused_by_name(promotion: Promotion) -> None:
    """An unverifiable signature field would be worse than the governed record."""
    promotion.record["authorization"]["method"] = "ssh-signature"
    promotion.seal()
    messages = _refuses(promotion, "authorization")
    assert "PROMOTION_SIGNERS" in messages


def test_editing_the_evidence_after_authorization_voids_it(promotion: Promotion) -> None:
    """The governed equivalent of a signature: the decision names this digest.

    The record is rewritten *without* re-sealing, which is exactly what tampering
    looks like: every number still parses and the authorization now describes
    content that no longer exists.
    """
    promotion.seal()
    promotion.record["parity"]["windows"] = 12
    promotion.record["statistics"]["targets"][4]["n"] = 12
    promotion.write_record()

    messages = _refuses(promotion, "authorization")
    assert "authorizes a different promotion" in messages


def test_a_decision_that_does_not_quote_what_it_authorizes_is_refused(
    promotion: Promotion,
) -> None:
    promotion.seal(decision="# Owner decision\n\nPromote the new wake word.\n")
    messages = _refuses(promotion, "authorization")
    assert "authorizes anything" in messages


def test_an_untracked_owner_decision_is_refused(promotion: Promotion) -> None:
    """Branch protection is the signing authority, and it only governs commits."""
    promotion.seal()
    _git(promotion.repo, "rm", "--quiet", "--cached", str(promotion.decision_path))
    _git(promotion.repo, "commit", "--quiet", "-m", "untrack the decision")
    messages = _refuses(promotion, "authorization")
    assert "not tracked in git" in messages


def test_a_missing_owner_decision_is_refused(promotion: Promotion) -> None:
    del promotion.record["authorization"]["owner_decision"]
    promotion.seal()
    assert "nobody authorized this" in _refuses(promotion, "authorization")


def test_a_destination_somebody_copied_a_file_into_is_refused(promotion: Promotion) -> None:
    """The failure this module exists for, in the state it leaves behind."""
    (promotion.dest / ONNX).write_bytes(b"a file somebody copied in")
    messages = _refuses(promotion, "destination_state")
    assert "SHA256SUMS" in messages


def test_promoting_bytes_that_already_ship_is_refused(promotion: Promotion) -> None:
    for name, data in promotion.artifacts.items():
        (promotion.dest / name).write_bytes(data)
    promotion.write_sums(promotion.dest, promotion.artifacts)
    assert "nothing to promote" in _refuses(promotion, "destination_state")


def test_an_unresolved_journal_blocks_a_second_promotion(promotion: Promotion) -> None:
    (promotion.dest / pm.JOURNAL_NAME).write_text("{}", encoding="utf-8")
    verdict = promotion.verdict()
    assert "destination_state" in verdict.failed_checks()
    assert "Run `recover` first" in promotion.messages("destination_state")


# ── nothing is written unless everything passed ──────────────────────────────


def test_checking_a_candidate_writes_nothing_anywhere(promotion: Promotion) -> None:
    """``check`` is a diagnostic, on a passing record and on a failing one."""
    before_dest = _snapshot(promotion.dest)
    before_candidate = _snapshot(promotion.candidate_dir)
    assert promotion.verdict().passed
    assert _snapshot(promotion.dest) == before_dest
    assert _snapshot(promotion.candidate_dir) == before_candidate

    promotion.record["parity"]["detection_disagreements"] = 9
    promotion.seal()
    failing_candidate = _snapshot(promotion.candidate_dir)
    assert not promotion.verdict().passed
    assert _snapshot(promotion.dest) == before_dest
    assert _snapshot(promotion.candidate_dir) == failing_candidate


def test_a_refused_candidate_leaves_the_shipped_model_untouched(promotion: Promotion) -> None:
    promotion.record["acceptance"]["onnx"]["targets"][0]["observed"] = 0.4
    promotion.seal()
    before = _snapshot(promotion.dest)

    with pytest.raises(pm.Refused, match=r"refusing to promote"):
        pm.promote(promotion.context())

    assert _snapshot(promotion.dest) == before
    for name, data in promotion.previous.items():
        assert (promotion.dest / name).read_bytes() == data


# ── atomicity, recovery and rollback ────────────────────────────────────────


def _crash_after(monkeypatch, replaces: int):
    """Let ``replaces`` atomic replaces happen, then fail like a killed process."""
    state = {"seen": 0}
    original = pm._replace

    def flaky(source: Path, target: Path) -> None:
        if state["seen"] >= replaces:
            raise OSError("simulated crash mid-promotion")
        state["seen"] += 1
        original(source, target)

    monkeypatch.setattr(pm, "_replace", flaky)
    return state


def test_a_crash_between_the_two_artifact_replaces_is_resolved_forward(
    promotion: Promotion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mixture is real for as long as the process is dead, and no longer.

    One artifact has been replaced and the other has not — a state in which a Mac
    would load the new model and everything else the old one. The journal names
    the intended end state, every staged file is present and hash-verified, so
    recovery completes the promotion rather than guessing.
    """
    _crash_after(monkeypatch, replaces=1)
    with pytest.raises(OSError, match=r"simulated crash"):
        pm.promote(promotion.context())

    assert (promotion.dest / ONNX).read_bytes() == promotion.artifacts[ONNX]
    assert (promotion.dest / TFLITE).read_bytes() == promotion.previous[TFLITE]
    assert (promotion.dest / pm.JOURNAL_NAME).is_file(), "a crash left no journal"
    assert not pm.verify_shipped(promotion.dest)["passed"]

    monkeypatch.undo()
    report = pm.recover(promotion.dest)

    assert report["action"] == "recover-forward"
    for name in (ONNX, TFLITE):
        assert filecmp.cmp(
            promotion.dest / name, promotion.candidate_dir / name, shallow=False
        ), f"{name} did not end up complete"
    assert not (promotion.dest / pm.JOURNAL_NAME).exists()
    assert pm.verify_shipped(promotion.dest)["passed"]
    assert [entry["action"] for entry in pm.read_ledger(promotion.dest)["entries"]] == [
        "recover-forward"
    ]


def test_a_crash_that_lost_a_staged_file_is_resolved_backward(
    promotion: Promotion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roll forward only when it is provable; otherwise put the old model back."""
    _crash_after(monkeypatch, replaces=1)
    with pytest.raises(OSError, match=r"simulated crash"):
        pm.promote(promotion.context())
    # The staged tflite never made it to disk intact — a tmp cleaner, a full
    # filesystem, an interrupted copy. Rolling forward is now unprovable.
    (promotion.dest / (TFLITE + ".incoming")).unlink()

    monkeypatch.undo()
    report = pm.recover(promotion.dest)

    assert report["action"] == "recover-revert"
    for name, data in promotion.previous.items():
        assert (promotion.dest / name).read_bytes() == data, f"{name} was not restored"
    assert not (promotion.dest / pm.JOURNAL_NAME).exists()
    assert pm.verify_shipped(promotion.dest)["passed"]
    assert [entry["action"] for entry in pm.read_ledger(promotion.dest)["entries"]] == [
        "recover-revert"
    ]


def test_recovery_is_idempotent_and_a_clean_destination_needs_none(
    promotion: Promotion,
) -> None:
    assert pm.recover(promotion.dest) == {"action": "none", "state": "clean"}
    pm.promote(promotion.context())
    before = _snapshot(promotion.dest)
    assert pm.recover(promotion.dest)["action"] == "none"
    assert _snapshot(promotion.dest) == before


def test_a_rollback_restores_the_previous_bytes_exactly(
    promotion: Promotion, tmp_path: Path
) -> None:
    """Byte-for-byte, compared against copies taken before anything moved."""
    keep = tmp_path / "before"
    keep.mkdir()
    for name in (ONNX, TFLITE, "SHA256SUMS"):
        shutil.copy2(promotion.dest / name, keep / name)

    pm.promote(promotion.context())
    assert (promotion.dest / ONNX).read_bytes() != (keep / ONNX).read_bytes()

    report = pm.rollback(promotion.dest)

    assert report["to"] == {name: _sha256(data) for name, data in promotion.previous.items()}
    for name in (ONNX, TFLITE, "SHA256SUMS"):
        assert filecmp.cmp(promotion.dest / name, keep / name, shallow=False), (
            f"{name} was not restored byte-for-byte"
        )
    assert pm.verify_shipped(promotion.dest)["passed"]
    assert [entry["action"] for entry in pm.read_ledger(promotion.dest)["entries"]] == [
        "promote",
        "rollback",
    ]


def test_a_rollback_refuses_when_the_preserved_bytes_no_longer_hash_right(
    promotion: Promotion,
) -> None:
    report = pm.promote(promotion.context())
    bundle = promotion.dest / report["rollback_bundle"]
    (bundle / TFLITE).write_bytes(b"not what was preserved")

    with pytest.raises(pm.Refused, match=r"does not hash to"):
        pm.rollback(promotion.dest)
    # And the shipped model is still the promoted one, not a half-restored mixture.
    assert (promotion.dest / ONNX).read_bytes() == promotion.artifacts[ONNX]
    assert (promotion.dest / TFLITE).read_bytes() == promotion.artifacts[TFLITE]


def test_rolling_back_twice_is_refused_rather_than_guessed_at(promotion: Promotion) -> None:
    pm.promote(promotion.context())
    pm.rollback(promotion.dest)
    with pytest.raises(pm.Refused, match=r"already the preserved one"):
        pm.rollback(promotion.dest)


def test_a_rewritten_ledger_entry_breaks_the_chain(promotion: Promotion) -> None:
    """The other half of the governed record: what has happened since.

    An entry cannot be edited or dropped without invalidating every entry after
    it, so the destination's history is as hard to rewrite as its authorization.
    """
    pm.promote(promotion.context())
    ledger_path = promotion.dest / pm.LEDGER_NAME
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["entries"][0]["candidate"] = "r9c2"
    ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    with pytest.raises(pm.LedgerError, match=r"hashes to"):
        pm.read_ledger(promotion.dest)
    assert not pm.verify_shipped(promotion.dest)["passed"]


def test_verify_reports_a_file_that_arrived_outside_the_gate(promotion: Promotion) -> None:
    pm.promote(promotion.context())
    (promotion.dest / TFLITE).write_bytes(b"copied in after the promotion")

    report = pm.verify_shipped(promotion.dest)

    assert not report["passed"]
    assert any("SHA256SUMS" in problem for problem in report["problems"])
    assert any("ledger" in problem for problem in report["problems"])


# ── the command line ────────────────────────────────────────────────────────


def test_the_cli_checks_promotes_verifies_and_rolls_back(
    promotion: Promotion, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit codes, because a pipeline stage reads those and not the prose."""
    common = [
        "--record", str(promotion.record_path),
        "--candidate-dir", str(promotion.candidate_dir),
        "--dest", str(promotion.dest),
        "--repo-root", str(promotion.repo),
        "--workflow", str(promotion.workflow),
        "--registry", str(promotion.registry),
    ]
    assert pm.main(["check", *common]) == 0
    assert "PROMOTABLE" in capsys.readouterr().out

    assert pm.main(["promote", *common]) == 0
    assert pm.main(["verify", "--dest", str(promotion.dest)]) == 0

    assert pm.main(["rollback", "--dest", str(promotion.dest)]) == 0
    for name, data in promotion.previous.items():
        assert (promotion.dest / name).read_bytes() == data

    promotion.record["parity"]["detection_disagreements"] = 2
    promotion.seal()
    assert pm.main(["check", *common]) == 1
    printed = capsys.readouterr().out
    assert "REFUSED" in printed
    assert "FAIL  parity" in printed


def test_the_cli_promote_exits_non_zero_on_a_refusal(
    promotion: Promotion, capsys: pytest.CaptureFixture[str]
) -> None:
    promotion.record["ci"]["conclusion"] = "cancelled"
    promotion.seal()
    code = pm.main(
        [
            "promote",
            "--record", str(promotion.record_path),
            "--candidate-dir", str(promotion.candidate_dir),
            "--dest", str(promotion.dest),
            "--repo-root", str(promotion.repo),
            "--workflow", str(promotion.workflow),
            "--registry", str(promotion.registry),
        ]
    )
    assert code == 1
    assert "REFUSED" in capsys.readouterr().err
    for name, data in promotion.previous.items():
        assert (promotion.dest / name).read_bytes() == data


# ── the arithmetic, against the predeclaration rather than against itself ────


def test_the_targets_are_the_five_the_predeclaration_fixed() -> None:
    """A gate with its own idea of the targets would validate its own opinion."""
    assert set(pm.ACCEPTANCE_TARGETS) == set(r8.TARGETS)
    for target_id, (name, limit, comparison) in r8.TARGETS.items():
        target = pm.ACCEPTANCE_TARGETS[target_id]
        assert (target.limit, target.comparison) == (limit, comparison)
        assert target.name == name


def test_the_bounds_are_the_predeclared_ones() -> None:
    """Every fixture bound above is checked against ``round8_config.json``.

    The tables in that file were computed independently of this module, so
    reproducing them is evidence that the gate's arithmetic is the programme's
    arithmetic and not a second opinion.
    """
    config = json.loads((WAKEWORD / "round8_config.json").read_text(encoding="utf-8"))
    power = config["statistical_power"]

    for key in ("target_1", "target_3", "target_4", "target_5"):
        for n_text, stated in power[key]["clean_run_bound_by_n"].items():
            recomputed = pm.binomial_upper_bound(0, int(n_text))
            assert abs(recomputed - stated) <= max(5e-5, stated * 0.02), (
                f"{key} n={n_text}: config {stated}, recomputed {recomputed}"
            )

    for block in power["target_2"]["clean_run_bound_by_source"].values():
        recomputed = pm.poisson_rate_upper_bound(0, block["hours"])
        assert abs(recomputed - block["bound_per_hour"]) <= max(
            0.002, block["bound_per_hour"] * 0.02
        )

    assert abs(
        pm.minimum_hours_for_rate(0.2) - power["target_2"]["hours_needed_at_0.2_per_hour"]
    ) <= 0.02
    # The fixture's own numbers, so a drift in either place is caught here.
    assert abs(pm.binomial_upper_bound(0, 144) - BOUND_144_POSITIVE_UTTERANCES) <= 5e-5
    assert abs(pm.binomial_upper_bound(0, 168) - BOUND_168_NEAR_PHRASE_UTTERANCES) <= 5e-5
    assert abs(pm.poisson_rate_upper_bound(0, 20.0) - BOUND_20_HOURS_PER_HOUR) <= 5e-4
    assert abs(
        pm.poisson_rate_upper_bound(0, UNDERPOWERED_HOURS) - UNDERPOWERED_BOUND_PER_HOUR
    ) <= 5e-4


def test_a_clean_run_bound_matches_its_closed_form_and_shrinks_with_the_sample() -> None:
    """The bisection has to reproduce the closed form, or it is a third answer."""
    for n in (12, 36, 144, 5000):
        assert abs(pm.binomial_upper_bound(0, n) - (1 - 0.05 ** (1 / n))) < 1e-9
    for hours in (0.05, 11.659, 20.0):
        assert abs(pm.poisson_rate_upper_bound(0, hours) - 2.995732 / hours) < 1e-4
    # Observing events can only widen a bound, never narrow it.
    assert pm.binomial_upper_bound(1, 144) > pm.binomial_upper_bound(0, 144)
    assert pm.poisson_rate_upper_bound(2, 20.0) > pm.poisson_rate_upper_bound(0, 20.0)
    # And the minimum-sample helpers agree with the bound they justify.
    for limit in (0.05, 0.02, 0.2):
        needed = pm.minimum_trials_for_proportion(limit)
        assert pm.binomial_upper_bound(0, needed) <= limit
        assert pm.binomial_upper_bound(0, needed - 1) > limit


# ── the real repository, read only ───────────────────────────────────────────


def test_the_real_workflow_produces_the_five_contexts_the_fixture_mirrors() -> None:
    """The default the gate ships with has to be the workflow that exists."""
    contexts = pm.required_ci_contexts(REPO / ".github" / "workflows" / "youtab-ci.yml")
    assert set(contexts) == set(CI_CONTEXTS), contexts


def test_the_shipped_pair_is_retired_and_cannot_be_promoted_as_a_new_candidate(
    promotion: Promotion,
) -> None:
    """The model this repository ships is itself refused, by content address.

    It stays in place because nothing better has qualified — but it was fitted on
    20,000 synthesized positives, both digests are in the registry, and a
    promotion that re-offered it as new would be laundering a retired artifact.
    Nothing here writes to ``tools/wakewords``; the bytes are copied out of it.
    """
    assert _sha256((SHIPPED / ONNX).read_bytes()) == SHIPPED_ONNX_SHA256
    assert _sha256((SHIPPED / TFLITE).read_bytes()) == SHIPPED_TFLITE_SHA256

    for name in (ONNX, TFLITE):
        promotion.set_artifact(name, (SHIPPED / name).read_bytes())
    promotion.rebaseline()

    real_registry = WAKEWORD / "retired_synthetic_artifacts.json"
    failed = promotion.failed(registry=real_registry)

    assert "retired_artifacts" in failed
    messages = promotion.messages("retired_artifacts", registry=real_registry)
    assert "shipped_synthetic_era_export" in messages
    assert ONNX in messages and TFLITE in messages
    # And it is refused for that reason alone: every other requirement is met, so
    # the refusal cannot be explained away as an incidental fixture problem.
    assert failed == {"retired_artifacts"}, failed


def test_the_real_registry_still_lists_both_shipped_artifacts() -> None:
    retired = pm.retired_digests(WAKEWORD / "retired_synthetic_artifacts.json")
    assert retired[SHIPPED_ONNX_SHA256].startswith("shipped_synthetic_era_export")
    assert retired[SHIPPED_TFLITE_SHA256].startswith("shipped_synthetic_era_export")


def test_the_shipped_sums_still_describe_the_shipped_bytes() -> None:
    """Read-only, and the control that this suite never wrote to the real path."""
    lines = (SHIPPED / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    recorded = {name: digest for digest, _, name in (line.partition("  ") for line in lines)}
    assert recorded == {ONNX: SHIPPED_ONNX_SHA256, TFLITE: SHIPPED_TFLITE_SHA256}
    for name, digest in recorded.items():
        assert _sha256((SHIPPED / name).read_bytes()) == digest
