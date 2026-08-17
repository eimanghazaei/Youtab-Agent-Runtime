"""Round 8 is human-only, and every way of breaking that has to be a red test.

Background
----------
Rounds 1-7 were fitted on synthesized speech and none of them qualified.
Synthetic training is retired: Round 8 and every later candidate in this phase
is trained, validated and qualified exclusively on real human recordings and
real recorded environmental audio.

The reason this needs tests rather than a policy document is that every way of
violating it is *quiet*. A TTS clip in the positives, a near phrase relabelled
to look human, a previous round's feature tensor loaded as a starting point, the
sealed speaker ingested by a hurried ``--split``, a speaker on both sides of a
line — none of those raise anything on their own. Each one produces a dataset
that builds, a model that trains and a number that means something other than
what it says.

So each test below is paired with a mutation: a one-line change to
``scripts/wakeword/build_human_dataset.py`` that would let exactly that
violation through. Every one of them was applied, run, observed red, and
reverted against a byte backup. A guard nobody has watched fail is a guard
nobody has tested.

These tests are hermetic. They build miniature manifests and generate every WAV
under ``tmp_path``. The real human recordings are not in this repository and
must never be: nothing here reads them, no path here points at a capture drive,
and the speaker labels are assembled from fragments so this file does not itself
become a tracked record keyed by speaker — the same convention
``tests/tools/test_wakeword_no_human_data_committed.py`` uses for its own
control cases.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import wave
from pathlib import Path
from typing import Callable

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import build_dataset  # noqa: E402
import build_human_dataset as round8  # noqa: E402
import freeze_manifest  # noqa: E402

MODULE_SOURCE = (WAKEWORD / "build_human_dataset.py").read_text(encoding="utf-8")

# Speaker labels, assembled rather than written. The label itself is not private
# data; a tracked file that pairs the key "speaker" with one is the shape of a
# transcript, and the commit gate forbids it.
FIRST_TRAIN = "E" + "001"
SEALED_EVAL = "E" + "002"
TRAIN = "E" + "003"
OTHER_TRAIN = "E" + "004"
VALIDATION = "E" + "005"
SEALED_QUALIFICATION = ("E" + "006", "E" + "007")

#: A speaker who is deliberately not in the registry.
UNAPPROVED = "human-r8-a"

POSITIVE = "positive_human"
NEAR = "near_phrase_human"
FREE = "free_speech_human"

#: The TTS-era category names, written out here rather than read from the
#: module under test.
#:
#: A test parametrized over ``round8.SYNTHETIC_CATEGORIES`` would silently lose
#: a case when somebody deleted an entry from that set — the deletion is exactly
#: the regression being guarded against, and it would present as a smaller,
#: still-green test run. So the list is stated independently and the module's
#: table is checked against it.
#: TTS-era names with no real-derivation meaning. Refused on sight.
#:
#: ``near_phrase`` and ``free_speech`` are deliberately NOT here. They are what
#: ``human_derive.py`` calls a real speaker's negatives, so refusing them by
#: name made the frozen sealed-evaluation manifest un-ingestible — and that
#: manifest cannot be regenerated, because its digest is what a qualification
#: result is traced to. They are normalised instead, and a synthetic clip
#: wearing one is caught by provenance; see
#: ``test_a_derivation_category_is_normalised_not_refused`` and
#: ``test_a_renamed_synthetic_clip_is_refused_by_provenance_not_by_name``.
TTS_ERA_CATEGORIES = (
    "positive",
    "hardneg",
    "confusable",
    "softneg",
    "common",
    "synthesized_speech",
)

#: Names the derivation writes, which must be accepted and mapped.
DERIVATION_CATEGORIES = ("near_phrase", "free_speech", "positive_close")

#: (clip path, category, label, original, excluded). Two usable positives, two
#: usable negatives, one excluded take, spread over three originals — so every
#: expected count below is a literal rather than a formula that could agree with
#: a wrong implementation.
DEFAULT_CLIPS: tuple[tuple[str, str, int, str, bool], ...] = (
    ("clips/positive_human/p_000.wav", POSITIVE, 1, "01_close_normal.m4a", False),
    ("clips/positive_human/p_001.wav", POSITIVE, 1, "01_close_normal.m4a", False),
    ("clips/positive_human/p_002.wav", POSITIVE, 1, "01_close_normal.m4a", True),
    ("clips/near_phrase_human/n_000.wav", NEAR, 0, "05_near_phrases.m4a", False),
    ("clips/free_speech_human/f_000.wav", FREE, 0, "06_free_speech.m4a", False),
)

USABLE_POSITIVES = 2
USABLE_NEGATIVES = 2
EXCLUDED_CLIPS = 1
ORIGINALS = 3

#: Clip lengths, in seconds, by category. The free-speech take is long enough to
#: tile into several windows, which is the only negative path where
#: ``--negative-windows`` does anything.
SECONDS = {POSITIVE: 0.7, NEAR: 0.9, FREE: 4.5}


# ── the sink ─────────────────────────────────────────────────────────────────


class RecordingSink:
    """Stands in for ``FeatureSink``: keeps windows instead of embedding them.

    The real sink loads openWakeWord's ONNX front end and runs two models over
    every window; neither the package nor its pinned models belong in a unit
    test's environment.

    The preallocation contract *is* mirrored exactly, because that is the part
    a new emitter breaks: the real sink raises when more windows arrive than
    ``begin`` was told to expect, and raises again at ``finish`` when fewer did.
    Reproducing both means an off-by-N in the planned count fails here instead
    of hours into a build.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self.planned: int | None = None
        self.windows: list[np.ndarray] = []
        self.labels: list[int] = []
        self.groups: list[int] = []
        self.categories: list[str] = []
        self.settings: list[dict] = []

    def begin(self, total: int) -> None:
        self.planned = total

    def add(self, window: np.ndarray, label: int, group: int, category: str,
            params: dict) -> None:
        if self.planned is None:
            raise AssertionError("add() before begin(); the real sink has no array yet")
        if len(self.labels) >= self.planned:
            raise AssertionError(
                f"emitted more windows than planned ({len(self.labels) + 1} > "
                f"{self.planned}); the preallocation is short"
            )
        self.windows.append(window)
        self.labels.append(label)
        self.groups.append(group)
        self.categories.append(category)
        self.settings.append(params)

    def finish(self) -> None:
        if self.planned != len(self.labels):
            raise AssertionError(
                f"planned {self.planned} windows but emitted {len(self.labels)}; "
                "the surplus would ship as zero-filled rows"
            )

    @property
    def features(self) -> np.ndarray:
        """What ``main()`` saves. Waveforms here, since nothing embedded them."""
        if not self.windows:
            return np.zeros((0, build_dataset.WINDOW_SAMPLES), dtype=np.int16)
        return np.stack(self.windows)


# ── fixtures on disk ─────────────────────────────────────────────────────────


def _sha256(path: Path) -> str:
    """Hashed here independently of the module under test."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wav(path: Path, seconds: float, seed: int) -> Path:
    """A 16 kHz mono 16-bit WAV of deterministic noise.

    Noise rather than silence so a window that was quietly replaced by zeros —
    or padded where it should have been filled — is visible in the assertions
    about window content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    samples = rng.normal(0.0, 0.2, int(seconds * build_dataset.SAMPLE_RATE)) * 32767.0
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(build_dataset.SAMPLE_RATE)
        handle.writeframes(np.clip(samples, -32768, 32767).astype("<i2").tobytes())
    return path


def write_speaker(
    root: Path,
    *,
    speaker: str,
    split: str,
    clips: tuple = DEFAULT_CLIPS,
    originals_on_disk: bool = True,
    mutate: Callable[[dict], None] | None = None,
    freeze: bool = True,
) -> Path:
    """A derived speaker: originals, full-length decodes, clips and a manifest.

    Shaped like what ``human_derive.py`` writes — clip paths relative to the
    manifest's own directory, a per-original ``files`` list carrying the source
    hash, and the frozen ``MANIFEST.json.sha256`` sidecar the exclusion step
    produces. Every hash is computed from bytes that exist, so a refusal is
    never just a fixture that could not be read.

    ``mutate`` receives the assembled manifest and may break exactly one thing.
    The freeze is computed afterwards, so an injected defect does not also show
    up as an unfrozen manifest and pass the test for the wrong reason.
    """
    originals_dir = root / ("originals" if originals_on_disk else "originals-absent")
    sources = sorted({source for _, _, _, source, _ in clips})

    files = []
    for index, name in enumerate(sources):
        # The originals are never decoded by the builder, only hashed, so
        # deterministic bytes stand in for the container a phone recorded.
        original = originals_dir / name
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(f"original {name}".encode() * (32 + index))
        source_sha256 = _sha256(original)

        # The full-length 16 kHz decode: one-to-one with the original, and the
        # layer that can still be verified when the capture drive is not mounted.
        full = _write_wav(root / "full" / f"{Path(name).stem}.wav", 1.5, seed=10 + index)
        files.append(
            {
                "source_file": name,
                "source_sha256": source_sha256,
                "full_16k": full.relative_to(root).as_posix(),
                "full_16k_sha256": _sha256(full),
            }
        )
        if not originals_on_disk:
            original.unlink()

    rows = []
    for index, (relative, category, label, source, excluded) in enumerate(clips):
        audio = _write_wav(root / relative, SECONDS.get(category, 0.7), seed=100 + index)
        rows.append(
            {
                "clip": relative,
                "sha256": _sha256(audio),
                "speaker": speaker,
                "split": split,
                "label": label,
                "category": category,
                "source_file": source,
                "source_sha256": next(
                    f["source_sha256"] for f in files if f["source_file"] == source
                ),
                "excluded": excluded,
            }
        )

    manifest = {
        "speaker": speaker,
        "split": split,
        "usage": f"derived for the {split} split",
        "source_dir": str(originals_dir),
        "derived_dir": str(root),
        "files": files,
        "clips": rows,
        "usable_counts": {
            "usable_positives": sum(1 for r in rows if r["label"] == 1 and not r["excluded"]),
            "usable_negatives": sum(1 for r in rows if r["label"] == 0 and not r["excluded"]),
            "excluded": sum(1 for r in rows if r["excluded"]),
            "total_clips": len(rows),
        },
    }
    if mutate is not None:
        mutate(manifest)

    path = root / "MANIFEST.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="")
    if freeze:
        path.with_name(path.name + ".sha256").write_text(
            f"{_sha256(path)}  {path.name}\n", encoding="utf-8", newline="\n"
        )
    return path


def write_corpus(
    root: Path,
    *,
    usage: str = "training",
    count: int = 3,
    seconds: float = 1.0,
    names: tuple[str, ...] = (),
) -> Path:
    """A frozen recorded corpus, frozen by the tool that freezes the real ones.

    ``freeze_manifest.freeze`` is called for real rather than imitated: it is
    what produces the manifests ``acquire_common_voice.py`` writes, and its
    ``usage`` field is the gate this stage leans on.
    """
    clips = root / "clips"
    chosen = names or tuple(f"rec_{i:03d}.wav" for i in range(count))
    for index, name in enumerate(chosen):
        _write_wav(clips / name, seconds, seed=500 + index)
    out = root / "corpus.manifest.json"
    freeze_manifest.freeze(
        clips,
        out,
        dataset="fixture-corpus",
        split="train",
        usage=usage,
        note="hermetic fixture; no real corpus is read by these tests",
    )
    return out


class Build:
    """One build's inputs, and the two ways of running it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.out = tmp_path / "round8"
        self.human = write_speaker(tmp_path / "derived_a", speaker=TRAIN, split="train")

    def args(self, **overrides) -> argparse.Namespace:
        values = {
            "source": [f"human={self.human}"],
            "out": self.out,
            "split": "train",
            "positive_windows": 2,
            "negative_windows": 1,
            "batch_size": 8,
            "chunk": 8,
            "ncpu": 1,
            "keep_audio": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def run(self, **overrides) -> tuple[RecordingSink, dict]:
        sink = RecordingSink()
        stats = round8.build_windows(self.args(**overrides), sink)
        sink.finish()
        return sink, stats

    def refuse(self, pattern: str, **overrides) -> str:
        """Assert the build is refused, and that nothing was emitted first."""
        sink = RecordingSink()
        with pytest.raises(round8.Refused, match=pattern) as raised:
            round8.build_windows(self.args(**overrides), sink)
        assert sink.planned is None and sink.labels == [], (
            "the refusal came after windows had already been emitted; a "
            "half-written dataset is worse than none"
        )
        return str(raised.value)


@pytest.fixture()
def build(tmp_path: Path) -> Build:
    return Build(tmp_path)


# ── the registry ─────────────────────────────────────────────────────────────


def test_the_registry_is_the_owner_approved_assignment() -> None:
    """Split assignment is a decision, recorded before any audio is read.

    Asserted as a whole mapping rather than key by key: a speaker silently
    *added* is as much a change as a speaker moved, and only equality catches
    both.
    """
    assert round8.SPEAKER_SPLITS == {
        FIRST_TRAIN: "train",
        # E002 was the sealed evaluation holdout; the Owner reassigned it to
        # validation (see the consumption tests below). It is now a held-out
        # validation voice, not a sealed one.
        SEALED_EVAL: "validation",
        TRAIN: "train",
        OTHER_TRAIN: "train",
        VALIDATION: "validation",
        SEALED_QUALIFICATION[0]: "qualification_sealed",
        SEALED_QUALIFICATION[1]: "qualification_sealed",
    }
    sealed = {name for name, split in round8.SPEAKER_SPLITS.items()
              if split in round8.SEALED_SPLITS}
    # E002 has left the sealed set; the sealed final holdout is E006/E007 only.
    assert sealed == {*SEALED_QUALIFICATION}

    # No speaker may be reachable from two splits, which is what makes the
    # registry a function rather than a suggestion.
    assert len(round8.SPEAKER_SPLITS) == len(set(round8.SPEAKER_SPLITS))
    # ``eval_sealed`` stays defined but now binds no speaker, and is still not a
    # buildable tensor split.
    assert "eval_sealed" not in round8.BUILDABLE_SPLITS.values()
    assert "eval_sealed" in round8.SEALED_SPLITS


def test_e002_is_a_validation_speaker_reachable_on_the_validation_split() -> None:
    """The Owner-approved reassignment, asserted directly.

    E002 is bound to ``validation``, and ``validation`` is a buildable split
    whose one accepted registry split is exactly ``validation`` — so E002 is
    reachable as a validation tensor, which is what selection needs.
    """
    assert round8.SPEAKER_SPLITS[SEALED_EVAL] == "validation"
    assert round8.BUILDABLE_SPLITS["validation"] == "validation"
    assert round8.SPEAKER_SPLITS[SEALED_EVAL] not in round8.SEALED_SPLITS


def test_the_consumption_record_marks_e002_no_longer_sealed() -> None:
    """A durable, machine-readable record that E002 is spent for validation.

    The record is what makes the move a governed one-way decision rather than a
    line in a diff: it names the role E002 was spent for, asserts it is no longer
    a sealed holdout, and carries the reason and the authorization.
    """
    record = round8.CONSUMED_FOR_VALIDATION[SEALED_EVAL]
    assert record["for"] == "validation"
    assert record["no_longer_sealed_holdout"] is True
    assert record["authorized"] == "owner"
    assert "sealed" in record["reason"] and "validation" in record["reason"]


def test_re_sealing_a_consumed_speaker_is_refused_by_the_guard() -> None:
    """The safety net: a consumed speaker can never be bound back to a seal.

    Proved by mutation. A registry that re-binds E002 to a sealed split — the
    exact silent regression the consumption exists to prevent — must trip the
    guard, and the guard is the same one that runs at import.
    """
    # The live registry is consistent, so the guard is silent on it.
    round8.assert_consumed_speakers_not_sealed()

    for sealed_split in sorted(round8.SEALED_SPLITS):
        mutated = dict(round8.SPEAKER_SPLITS)
        mutated[SEALED_EVAL] = sealed_split
        with pytest.raises(round8.Refused, match="one-way|re-sealing|consumed"):
            round8.assert_consumed_speakers_not_sealed(mutated)

    # A consumed speaker vanishing from the registry is refused too: the
    # consumption is a promise about a binding, so the binding cannot disappear.
    with pytest.raises(round8.Refused, match="absent from SPEAKER_SPLITS"):
        round8.assert_consumed_speakers_not_sealed({})


def test_an_unapproved_speaker_is_refused(build: Build, tmp_path: Path) -> None:
    """Approval is a registry entry, never a plausible-looking manifest."""
    stray = write_speaker(tmp_path / "stray", speaker=UNAPPROVED, split="train")
    message = build.refuse("not in the approved registry", source=[f"human={stray}"])
    assert UNAPPROVED in message

    # Non-vacuity: the identical fixture under an approved label builds, so the
    # refusal is the registry and not the manifest being unreadable.
    approved = write_speaker(tmp_path / "approved", speaker=OTHER_TRAIN, split="train")
    sink, _ = build.run(source=[f"human={approved}"])
    assert sink.labels


def test_an_unfrozen_or_edited_manifest_is_refused(tmp_path: Path) -> None:
    """"Approved" has to name one exact derivation.

    An unfrozen manifest can be edited between the decision and the build — a
    take un-excluded, a category renamed — and nothing afterwards can tell.
    """
    unfrozen = write_speaker(
        tmp_path / "unfrozen", speaker=TRAIN, split="train", freeze=False
    )
    build = Build(tmp_path / "work")
    build.refuse("not frozen", source=[f"human={unfrozen}"])

    edited = write_speaker(tmp_path / "edited", speaker=TRAIN, split="train")
    body = json.loads(edited.read_text(encoding="utf-8"))
    body["usage"] = "quietly re-scoped after freezing"
    edited.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8", newline="")
    message = build.refuse("has changed since it was frozen", source=[f"human={edited}"])
    assert "MANIFEST.json.sha256" in message


# ── splits, seals and speakers crossing lines ────────────────────────────────


@pytest.mark.parametrize("split", ["train", "validation"])
@pytest.mark.parametrize("speaker", [*SEALED_QUALIFICATION])
def test_a_sealed_speaker_is_refused_in_training_and_validation(
    tmp_path: Path, speaker: str, split: str
) -> None:
    """The seal is the only measurement of a voice nothing was fitted on.

    E006 and E007 only: E002 left the sealed set by Owner decision, so it is no
    longer refused here (see ``test_the_reassigned_speaker_builds_on_validation``).
    The sealed machinery is unchanged for every speaker still bound to a sealed
    split. Checked against the registry rather than against the manifest's own
    word, and on every flag combination: ``--split train`` is exactly the
    invocation that would ingest it, so the refusal cannot depend on the caller
    having passed the right flag.
    """
    sealed = write_speaker(
        tmp_path / "sealed",
        speaker=speaker,
        split=round8.SPEAKER_SPLITS[speaker],
    )
    build = Build(tmp_path / "work")
    message = build.refuse("sealed", source=[f"human={sealed}"], split=split)
    assert speaker in message and "not overridable" in message


def test_the_reassigned_speaker_builds_on_validation_and_nowhere_else(tmp_path: Path) -> None:
    """E002 was the sealed evaluation speaker and is now validation.

    The refusal that used to block it on every split is gone on the *validation*
    path — E002 builds into a validation tensor for selection — and is still
    present everywhere else: training (validation is not training) and
    qualification (that is E006 and E007's sealed job) both still refuse it.
    """
    build = Build(tmp_path / "work")
    e002 = write_speaker(tmp_path / "e002", speaker=SEALED_EVAL, split="validation")

    # The reassignment: E002 builds as a validation tensor, where before it was
    # refused on sight.
    sink, stats = build.run(source=[f"human={e002}"], split="validation")
    assert stats["samples_by_speaker"] == {SEALED_EVAL: 4}
    assert sink.labels

    # But only there. Validation is not training, and qualification is sealed.
    build.refuse("may not ingest it", source=[f"human={e002}"], split="train")
    build.refuse("may not ingest it", source=[f"human={e002}"], split="qualification")


def test_a_qualification_sealed_speaker_is_still_buildable_only_on_qualification(
    tmp_path: Path,
) -> None:
    """E006/E007's sealed machinery is untouched by E002 leaving it.

    A qualification-sealed speaker builds on the qualification split and is
    refused on train/validation — the guarantee the seal is worth.
    """
    build = Build(tmp_path / "work")
    ok = write_speaker(
        tmp_path / "qual", speaker=SEALED_QUALIFICATION[0], split="qualification_sealed"
    )
    sink, stats = build.run(source=[f"human={ok}"], split="qualification")
    assert stats["samples_by_speaker"] == {SEALED_QUALIFICATION[0]: 4}
    assert sink.labels

    for refused_split in ("train", "validation"):
        message = build.refuse(
            "sealed", source=[f"human={ok}"], split=refused_split
        )
        assert SEALED_QUALIFICATION[0] in message and "not overridable" in message


@pytest.mark.parametrize(
    ("speaker", "declared"),
    [(TRAIN, "validation"), (VALIDATION, "train"), (TRAIN, "eval_sealed")],
)
def test_a_declared_split_that_disagrees_with_the_registry_is_refused(
    tmp_path: Path, speaker: str, declared: str
) -> None:
    """Refused on the disagreement itself, not resolved in favour of either.

    Whichever record is wrong, a build that picks one puts the speaker somewhere
    nobody approved — and the one that reads as harmless (a training speaker
    declaring itself validation) is the one that scores the model against a
    voice it was fitted on.
    """
    odd = write_speaker(tmp_path / "odd", speaker=speaker, split=declared)
    build = Build(tmp_path / "work")
    split = "train" if declared != "train" else "validation"
    message = build.refuse("registry binds it to", source=[f"human={odd}"], split=split)
    assert declared in message and round8.SPEAKER_SPLITS[speaker] in message


def test_a_train_speaker_is_refused_on_the_validation_split(tmp_path: Path) -> None:
    """One person is a handful of voices; validating on a fitted voice is not a
    measurement of anything. The manifest is entirely valid — only the split it
    is asked for is wrong."""
    build = Build(tmp_path / "work")
    build.refuse("may not ingest it", split="validation")

    # Non-vacuity: the validation speaker builds on that same split.
    validation = write_speaker(tmp_path / "val", speaker=VALIDATION, split="validation")
    _, stats = build.run(source=[f"human={validation}"], split="validation")
    assert stats["samples_by_split"] == {"validation": 4}


def test_a_clip_row_from_another_speaker_or_split_is_refused(build: Build) -> None:
    """A row copied out of another manifest is how one speaker crosses splits.

    The header can be entirely correct while a single clip row carries somebody
    else's label, or the same speaker's other split. Both are refused, because
    both put one person's audio on two sides of a line inside one build.
    """
    def other_speaker(manifest: dict) -> None:
        manifest["clips"][0]["speaker"] = SEALED_EVAL

    def other_split(manifest: dict) -> None:
        manifest["clips"][1]["split"] = "validation"

    for mutate, expected in ((other_speaker, SEALED_EVAL), (other_split, "validation")):
        path = write_speaker(
            build.tmp / f"rows_{expected}", speaker=TRAIN, split="train", mutate=mutate
        )
        message = build.refuse("two sides of a split", source=[f"human={path}"])
        assert expected in message


def test_one_speaker_may_not_appear_under_two_splits(build: Build) -> None:
    """The backstop, unit-tested directly.

    The registry makes this unreachable through the loaders, which is exactly
    why it is asserted rather than reasoned about: it is the guard for a future
    source type that derives a split from something other than the registry.
    """
    def sample(speaker: str, split: str) -> round8.Sample:
        return round8.Sample(
            path=Path("clip.wav"), category=POSITIVE, label=1, speaker=speaker,
            split=split, source_type="human", source_file="01.m4a",
            source_sha256="a" * 64, sha256="b" * 64, licence="consented",
            manifest_sha256="c" * 64,
        )

    # Non-vacuity: one speaker on one split is accepted, so the failure below is
    # the crossing and not the helper rejecting everything.
    round8.assert_one_split_per_speaker([sample(TRAIN, "train"), sample(OTHER_TRAIN, "train")])

    with pytest.raises(round8.Refused, match="more than one split"):
        round8.assert_one_split_per_speaker(
            [sample(TRAIN, "train"), sample(TRAIN, "validation")]
        )


def test_the_same_speaker_supplied_twice_is_refused(build: Build) -> None:
    """One voice weighted as two, and per-speaker counts that read as two people."""
    second = write_speaker(build.tmp / "again", speaker=TRAIN, split="train")
    build.refuse(
        "supplied twice", source=[f"human={build.human}", f"human={second}"]
    )

    # Non-vacuity: two *different* approved training speakers build together.
    other = write_speaker(build.tmp / "other", speaker=OTHER_TRAIN, split="train")
    _, stats = build.run(source=[f"human={build.human}", f"human={other}"])
    assert stats["samples_by_speaker"] == {TRAIN: 4, OTHER_TRAIN: 4}


# ── categories, labels and the rename that must not work ─────────────────────


def test_every_tts_era_category_is_recorded_as_synthetic() -> None:
    """The module's table has to cover the Owner decision's whole list.

    Paired with the parametrized test below: this fails if a name is dropped
    from the table, that one fails if a dropped name is then accepted. Either
    alone would let a deletion pass as a smaller test run.
    """
    missing = set(TTS_ERA_CATEGORIES) - round8.SYNTHETIC_CATEGORIES
    assert not missing, f"these TTS-era names are no longer refused: {sorted(missing)}"
    # None of them may also be an approved human category, or the two tables
    # would disagree about the same string.
    assert round8.SYNTHETIC_CATEGORIES.isdisjoint(round8.HUMAN_CATEGORIES)
    # And the two tables must not fight over a derivation name either: an alias
    # that is also listed as synthetic would be normalised on one path and
    # refused on the other, depending on evaluation order.
    assert round8.SYNTHETIC_CATEGORIES.isdisjoint(round8.DERIVED_CATEGORY_ALIASES)


@pytest.mark.parametrize("name", DERIVATION_CATEGORIES)
def test_a_derivation_category_is_normalised_not_refused(
    build: Build, name: str
) -> None:
    """``human_derive.py``'s own names must load, not be rejected.

    Refusing them by name was the first design, and it was wrong: the sealed
    evaluation manifest declares ``near_phrase`` and is frozen at a digest that
    a qualification result is traced to, so it cannot be re-derived to satisfy a
    naming convention. A rule that forces a sealed set to be rewritten has
    broken the thing it exists to protect.

    Safe because the name was never the guard — every row is provenance-verified
    unconditionally, which is what the rename test opposite this one proves.
    """
    def rename(manifest: dict) -> None:
        clip = manifest["clips"][3]
        clip["category"] = name
        clip["label"] = 1 if name.startswith("positive_") else 0

    path = write_speaker(
        build.tmp / f"derived_{name}", speaker=TRAIN, split="train", mutate=rename
    )
    _, stats = build.run(source=[f"human={path}"])

    # Non-vacuity: it did not merely fail to raise, it emitted the window under
    # the mapped name.
    expected = (
        round8.HUMAN_POSITIVE_CATEGORY
        if name.startswith("positive_")
        else round8.DERIVED_CATEGORY_ALIASES[name]
    )
    assert expected in stats["samples_by_category"], stats["samples_by_category"]
    assert stats["synthetic_samples"] == 0


@pytest.mark.parametrize("name", TTS_ERA_CATEGORIES)
def test_a_synthetic_era_category_is_a_hard_error(build: Build, name: str) -> None:
    """A TTS group's name with no real-derivation meaning.

    A row carrying one is either a synthetic clip or a manifest written by a
    tool that does not know this contract, and a builder that accepted them
    could not tell which.
    """
    def rename(manifest: dict) -> None:
        manifest["clips"][3]["category"] = name

    path = write_speaker(
        build.tmp / f"cat_{name}", speaker=TRAIN, split="train", mutate=rename
    )
    message = build.refuse("synthetic-era category", source=[f"human={path}"])
    assert name in message and "_human" in message


def test_an_unknown_category_is_refused_rather_than_skipped(build: Build) -> None:
    """Skipping it would train on part of a manifest and report all of it."""
    def whisper(manifest: dict) -> None:
        manifest["clips"][0]["category"] = "whispered_human"

    path = write_speaker(
        build.tmp / "whisper", speaker=TRAIN, split="train", mutate=whisper
    )
    build.refuse("not in the approved table", source=[f"human={path}"])


@pytest.mark.parametrize(("index", "label"), [(0, 0), (3, 1)])
def test_a_label_that_disagrees_with_its_category_is_refused(
    build: Build, index: int, label: int
) -> None:
    """The label is what training optimises, so a disagreement is not a detail.

    A near miss carrying label 1 teaches the model to fire on the speaker's
    ordinary talking; a positive carrying label 0 teaches it not to fire on the
    wake phrase. Neither is recoverable from the other record.
    """
    def relabel(manifest: dict) -> None:
        manifest["clips"][index]["label"] = label

    path = write_speaker(
        build.tmp / f"label_{index}_{label}", speaker=TRAIN, split="train", mutate=relabel
    )
    build.refuse("that category is label", source=[f"human={path}"])


def test_a_clip_derived_from_a_tts_recording_is_refused(build: Build) -> None:
    """A fully corroborated provenance chain that points into the TTS tree.

    The hash chain is intact here — ``files`` names the same synthetic path with
    the same digest — so the only thing that can catch it is recognising the
    tree it came from.
    """
    def from_tts(manifest: dict) -> None:
        synthetic = "data/tts/positive_train/000123.wav"
        manifest["files"][0]["source_file"] = synthetic
        for row in manifest["clips"]:
            if row["source_file"] == "01_close_normal.m4a":
                row["source_file"] = synthetic

    # The derived tree itself is named something innocuous on purpose, so the
    # refusal is about the recording the manifest points at rather than about
    # where the manifest happens to sit.
    path = write_speaker(
        build.tmp / "derived_b", speaker=TRAIN, split="train", mutate=from_tts
    )
    message = build.refuse("synthetic-era marker", source=[f"human={path}"])
    assert "data/tts" in message


def test_a_renamed_synthetic_clip_is_refused_by_provenance_not_by_name(
    build: Build,
) -> None:
    """The subtle one: ``positive`` renamed to ``positive_human``.

    The category now looks exactly like a real human positive, carries the right
    label and points at a plausible original. What it cannot do is corroborate
    that original against the manifest's own record of the recordings, because no
    recording of it was ever made. Provenance is what is checked; the string was
    never the guard.
    """
    def renamed(manifest: dict) -> None:
        # What a `positive` row looks like after somebody renamed it: an
        # approved category, the right label, a source that no recording
        # session ever produced. The path deliberately does not match any
        # synthetic marker, so the marker check cannot be what catches it.
        manifest["clips"][0]["category"] = POSITIVE
        manifest["clips"][0]["source_file"] = "generated/near/000001.wav"
        manifest["clips"][0]["source_sha256"] = "d" * 64

    path = write_speaker(
        build.tmp / "renamed", speaker=TRAIN, split="train", mutate=renamed
    )
    message = build.refuse("does not contain", source=[f"human={path}"])
    assert "relabelled to look human" in message

    # Non-vacuity: the category name really is accepted on its own, so the
    # refusal above is provenance and nothing else.
    assert POSITIVE in round8.HUMAN_CATEGORIES
    sink, _ = build.run()
    assert sink.categories.count(POSITIVE) == USABLE_POSITIVES * 2


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    [
        ("source_file", None, "names no 'source_file'"),
        ("source_file", "", "names no 'source_file'"),
        ("source_sha256", None, "which is not a"),
        ("source_sha256", "deadbeef", "which is not a"),
    ],
)
def test_a_sample_without_provenance_is_refused(
    build: Build, field: str, value: object, pattern: str
) -> None:
    """No source recording, no verified hash, no sample.

    A window with no provenance cannot be shown to be a recording of a person at
    all, so it cannot be in a dataset whose entire claim is that every window
    is one.
    """
    def strip(manifest: dict) -> None:
        if value is None:
            manifest["clips"][0].pop(field, None)
        else:
            manifest["clips"][0][field] = value

    path = write_speaker(
        build.tmp / f"prov_{field}_{value}", speaker=TRAIN, split="train", mutate=strip
    )
    build.refuse(pattern, source=[f"human={path}"])


# ── hashes: the originals, the clips and the corpora ─────────────────────────


def test_a_clip_whose_bytes_changed_is_refused(build: Build) -> None:
    """Fail closed on a mismatch: the audio on disk is not what was adjudicated."""
    root = build.tmp / "flipped"
    path = write_speaker(root, speaker=TRAIN, split="train")
    _write_wav(root / DEFAULT_CLIPS[0][0], 0.7, seed=9999)  # re-recorded, same name

    message = build.refuse("not the recorded", source=[f"human={path}"])
    assert DEFAULT_CLIPS[0][0] in message


def test_every_original_is_hash_verified_against_the_manifest(build: Build) -> None:
    """Both layers, and the report says which one answered.

    The originals live on the capture machine and are usually not mounted, so
    "verified" and "offline" have to be different numbers. A build that could
    not tell them apart would let an unmounted drive read as a clean
    verification of every recording.
    """
    _, stats = build.run()
    human = stats["sources"][0]
    assert (human["originals"], human["originals_verified"], human["originals_offline"]) == (
        ORIGINALS, ORIGINALS, 0
    )

    # A byte changed in an original, with everything else consistent.
    root = build.tmp / "tampered"
    path = write_speaker(root, speaker=TRAIN, split="train")
    (root / "originals" / "01_close_normal.m4a").write_bytes(b"re-exported")
    build.refuse("the recording changed after it was derived", source=[f"human={path}"])

    # Capture drive absent: the full-length decode carries the verification, and
    # the report says so rather than claiming the originals were checked.
    offline_path = write_speaker(
        build.tmp / "offline", speaker=TRAIN, split="train", originals_on_disk=False
    )
    _, offline_stats = build.run(source=[f"human={offline_path}"])
    assert offline_stats["sources"][0]["originals_offline"] == ORIGINALS
    assert offline_stats["sources"][0]["originals_verified"] == 0

    # ...and that layer is really checked, not merely counted.
    _write_wav(build.tmp / "offline" / "full" / "01_close_normal.wav", 1.5, seed=4242)
    build.refuse("changed after it was frozen", source=[f"human={offline_path}"])


def test_an_original_with_no_verifiable_layer_at_all_is_refused(build: Build) -> None:
    """Neither the recording nor a decode of it: nothing establishes it existed."""
    def drop_decode(manifest: dict) -> None:
        manifest["files"][0].pop("full_16k")
        manifest["files"][0].pop("full_16k_sha256")

    path = write_speaker(
        build.tmp / "nolayer",
        speaker=TRAIN,
        split="train",
        originals_on_disk=False,
        mutate=drop_decode,
    )
    build.refuse("no verifiable full-length decode", source=[f"human={path}"])


def test_a_recorded_corpus_is_hash_verified_and_usage_gated(build: Build) -> None:
    """Real corpora are negatives, verified per file, and gated by usage.

    ``freeze_manifest``'s usage field is what stops a corpus the model was
    fitted on being used to qualify it: a set that was trained against is not a
    measurement of anything.
    """
    manifest = write_corpus(build.tmp / "corpus")
    sink, stats = build.run(source=[f"human={build.human}", f"speech_commands={manifest}"])

    corpus = stats["sources"][1]
    assert corpus["files_used"] == 3 and corpus["usage"] == "training"
    assert corpus["licence"].startswith("CC BY 4.0")
    assert sink.categories.count("recorded_speech") == 3
    assert stats["samples_by_category"]["recorded_speech"] == 3

    # Fitted-on negatives cannot qualify a model.
    qual = write_speaker(
        build.tmp / "q", speaker=SEALED_QUALIFICATION[1], split="qualification_sealed"
    )
    build.refuse(
        "does not permit evaluation",
        source=[f"human={qual}", f"speech_commands={manifest}"],
        split="qualification",
    )

    # A sealed corpus cannot be trained on.
    sealed = write_corpus(build.tmp / "sealed_corpus", usage="sealed-evaluation")
    build.refuse(
        "sealed evaluation set",
        source=[f"human={build.human}", f"speech_commands={sealed}"],
    )

    # And the per-file hashes are checked, not just listed.
    _write_wav(build.tmp / "corpus" / "clips" / "rec_001.wav", 1.0, seed=31337)
    build.refuse(
        "not the frozen",
        source=[f"human={build.human}", f"speech_commands={manifest}"],
    )


def test_generated_background_recordings_are_excluded_and_counted(build: Build) -> None:
    """Two of Speech Commands' six background files are synthesised noise.

    Generated background is prohibited exactly as generated speech is. They are
    named, counted and reported rather than filtered out of sight, because an
    invisible filter is indistinguishable from a filter that stopped working.
    """
    manifest = write_corpus(
        build.tmp / "rooms",
        names=("doing_the_dishes.wav", "pink_noise.wav", "white_noise.wav", "running_tap.wav"),
    )
    sink, stats = build.run(
        source=[f"human={build.human}", f"recorded_background={manifest}"]
    )

    rooms = stats["sources"][1]
    assert rooms["generated_excluded"] == ["pink_noise.wav", "white_noise.wav"]
    assert rooms["files_listed"] == 4 and rooms["files_used"] == 2
    assert sink.categories.count("background_only") == 2

    used = {
        record["source"] for record in sink.settings
        if record["source_type"] == "recorded_background"
    }
    assert used == {"doing_the_dishes.wav", "running_tap.wav"}
    assert round8.GENERATED_BACKGROUND_NAMES.isdisjoint(used)


# ── previous rounds: tensors and checkpoints ─────────────────────────────────


@pytest.mark.parametrize(
    "directory",
    ["data/features", "data/features_r7", "data/features_r6c1", "data/tts"],
)
def test_a_previous_synthetic_tree_is_neither_readable_nor_writable(
    build: Build, directory: str
) -> None:
    """Every earlier round's tensors, and the generated clip tree.

    Reading one would train on synthesized speech under a human-only name;
    writing into one would leave a Round 8 dataset indistinguishable from the
    synthetic dataset beside it.
    """
    build.refuse("synthetic-era marker", out=build.tmp / directory / "out")

    stray = build.tmp / directory / "speaker" / "MANIFEST.json"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("{}", encoding="utf-8")
    build.refuse("synthetic-era marker", source=[f"human={stray}"])

    tensor = build.tmp / directory / "checkpoint.pt"
    tensor.parent.mkdir(parents=True, exist_ok=True)
    tensor.write_bytes(b"weights")
    with pytest.raises(round8.Refused, match="synthetic-era marker"):
        round8.refuse_synthetic_initialization(tensor)


def test_a_synthetic_candidate_is_refused_as_an_initialization_checkpoint(
    build: Build,
) -> None:
    """A synthetic model fine-tuned on real data is still a synthetic model.

    Nothing about the artifact says so afterwards, which is why the test is
    evidence rather than a path shape: the checkpoint has to sit beside the
    contract this stage writes, and that contract has to assert zero synthetic
    samples.
    """
    candidate = build.tmp / "candidates" / "r7" / "checkpoint.pt"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"round 7 weights")

    with pytest.raises(round8.Refused, match="has no DATASET_CONTRACT.json"):
        round8.refuse_synthetic_initialization(candidate)

    for contract in (
        {"human_only": False, "synthetic_samples": 0},
        {"human_only": True, "synthetic_samples": 12},
        {"round": 8},
    ):
        (candidate.parent / round8.CONTRACT_FILENAME).write_text(
            json.dumps(contract), encoding="utf-8"
        )
        with pytest.raises(round8.Refused, match="does not assert a human-only dataset"):
            round8.refuse_synthetic_initialization(candidate)

    # Non-vacuity: the contract a real Round 8 build writes is accepted, so the
    # refusals above are the evidence failing and not the check refusing
    # everything.
    _, stats = build.run()
    (candidate.parent / round8.CONTRACT_FILENAME).write_text(
        json.dumps(stats["contract"]), encoding="utf-8"
    )
    round8.refuse_synthetic_initialization(candidate)


# ── the flags ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("flag", sorted(round8.REFUSED_FLAGS))
def test_every_synthetic_data_flag_aborts_rather_than_being_ignored(
    build: Build, flag: str
) -> None:
    """Ignoring one is worse than rejecting it.

    A sweep arm that passed ``--rir-count 200`` and silently got a dataset with
    no impulse responses in it would be written down as an arm that had them.
    Both spellings abort, because ``--flag=value`` never reaches a positional
    scan that only looks at bare tokens.
    """
    for argv in ([flag, "200"], [f"{flag}=200"]):
        with pytest.raises(round8.Refused, match="is refused") as raised:
            round8.main([*argv, "--out", str(build.out), "--split", "train"])
        assert round8.REFUSED_FLAGS[flag] in str(raised.value)


def test_the_parser_defines_none_of_the_refused_flags() -> None:
    """The refusal table and the parser must not drift into agreement.

    Read out of the source rather than out of a constructed parser: a flag added
    to ``add_argument`` *and* left in the table would be accepted by argparse and
    rejected by the pre-scan, which is a confusing pair of behaviours to have to
    diagnose. This makes adding it fail here instead.
    """
    defined = {
        node.args[0].value
        for node in ast.walk(ast.parse(MODULE_SOURCE))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    # Non-vacuity: the scan has to have found the parser it is judging.
    assert {"--source", "--out", "--split"} <= defined, defined
    assert not defined & set(round8.REFUSED_FLAGS), defined & set(round8.REFUSED_FLAGS)


def test_an_unknown_source_type_is_refused_rather_than_skipped(build: Build) -> None:
    """A dropped source is a dataset nobody can reconstruct from its command."""
    for spec in ("tts=/x/manifest.json", "synthetic_positives=/x", "human"):
        with pytest.raises(round8.Refused):
            round8.parse_source(spec)

    message = build.refuse("is not recognised", source=["libritts=/x/m.json"])
    assert "human" in message and "speech_commands" in message

    # Non-vacuity: every advertised type parses.
    for kind in round8.SOURCE_TYPES:
        assert round8.parse_source(f"{kind}=/x/m.json") == (kind, Path("/x/m.json"))


# ── what is emitted, and what the stats claim about it ───────────────────────


def test_the_stats_assert_zero_synthetic_samples_and_count_every_axis(
    build: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run through ``main()``, so this covers the files that land on disk.

    ``synthetic_samples`` has to be *computed* from what was emitted. Hard-coded
    it is a comment that survives every regression it was written to catch, so
    the second half of this test makes one emitted window synthetic and requires
    the build to refuse rather than to report a zero it never measured.
    """
    monkeypatch.setattr(build_dataset, "FeatureSink", RecordingSink)
    corpus = write_corpus(build.tmp / "corpus")
    assert round8.main([
        "--source", f"human={build.human}",
        "--source", f"speech_commands={corpus}",
        "--out", str(build.out),
        "--split", "train",
        "--positive-windows", "2",
    ]) == 0

    stats = json.loads((build.out / "stats_train.json").read_text(encoding="utf-8"))
    assert stats["synthetic_samples"] == 0
    assert stats["human_only"] is True
    assert stats["samples_by_speaker"] == {TRAIN: 4, "corpus:speech_commands": 3}
    assert stats["samples_by_category"] == {
        POSITIVE: 2, NEAR: 1, FREE: 1, "recorded_speech": 3
    }
    assert stats["samples_by_split"] == {"train": 7}
    assert stats["windows_by_category"] == {
        POSITIVE: 4, NEAR: 1, FREE: 1, "recorded_speech": 3
    }
    assert stats["windows"] == 9 and stats["positives"] == 4

    contract = json.loads((build.out / round8.CONTRACT_FILENAME).read_text(encoding="utf-8"))
    assert contract == stats["contract"]
    assert contract["human_only"] is True and contract["synthetic_samples"] == 0
    assert [s["manifest_sha256"] for s in contract["sources"]] == [
        _sha256(build.human), _sha256(corpus)
    ]

    # The zero is a measurement, not a constant. With one sample made synthetic,
    # both guards have to fire: the check before emission, and — with that one
    # disabled — the assertion over what was actually emitted, which is the
    # thing that makes `synthetic_samples: 0` mean anything.
    real = round8.is_synthetic
    monkeypatch.setattr(
        round8,
        "is_synthetic",
        lambda sample: "planted" if sample.category == FREE else real(sample),
    )

    def rebuild(out: str) -> None:
        round8.main([
            "--source", f"human={build.human}",
            "--out", str(build.tmp / out),
            "--split", "train",
        ])

    with pytest.raises(round8.Refused, match="cannot be shown to be real recordings"):
        rebuild("second")

    monkeypatch.setattr(round8, "assert_real_provenance", lambda samples: None)
    with pytest.raises(round8.Refused, match="are not real recordings"):
        rebuild("third")


def test_the_preallocation_counts_every_window(build: Build) -> None:
    """``begin`` is told the total before a single window exists.

    Too small aborts the run part-way; too large finishes short and leaves
    zero-filled rows that train as silent negatives. ``RecordingSink``
    reproduces both, so this holds the arithmetic rather than the sink's
    tolerance for it.
    """
    for positives, negatives in ((1, 1), (2, 1), (4, 3)):
        sink, stats = build.run(positive_windows=positives, negative_windows=negatives)
        # The near-phrase take is 0.9 s and tiles once whatever is asked for;
        # the free-speech take is 4.5 s and tiles up to `negatives` times.
        expected = USABLE_POSITIVES * positives + 1 + min(negatives, 2)
        assert sink.planned == expected == len(sink.labels)
        assert stats["windows"] == expected

    with pytest.raises(round8.Refused, match="--positive-windows must be"):
        build.run(positive_windows=len(round8.TRAILING_OFFSETS_S) + 1)
    with pytest.raises(round8.Refused, match="--negative-windows must be"):
        build.run(negative_windows=0)


def test_group_ids_keep_one_recording_together(build: Build) -> None:
    """Every window from one recording shares a group id.

    A later grouped split of this dataset has to keep them on one side; group
    ids are how, and a positive whose four windows scattered across ids would
    let three of them be validated against the fourth.
    """
    sink, _ = build.run(positive_windows=4, negative_windows=1)

    groups: dict[int, set[str]] = {}
    for group, record in zip(sink.groups, sink.settings):
        groups.setdefault(group, set()).add(record["clip"])
    assert all(len(clips) == 1 for clips in groups.values())
    assert len(groups) == USABLE_POSITIVES + USABLE_NEGATIVES
    # Non-vacuity: "one clip per group" is trivial if every group has one
    # window, and the positives are windowed four times each.
    assert max(sink.groups.count(g) for g in groups) == 4


def test_the_audio_is_the_recording_and_nothing_was_done_to_it(build: Build) -> None:
    """No reverb, no mixed noise, no re-levelling, no draw of any kind.

    This is the allowed-processing list asserted on the bytes: a positive window
    is the recording placed at a fixed trailing offset, sample for sample, with
    silence elsewhere. Any augmentation — a gain, a room, a background bed —
    fails this, and augmentation applied to real recordings is what would make
    "the model works on real voices" a claim about audio nobody recorded.
    """
    sink, _ = build.run(positive_windows=1, negative_windows=1)
    clip = build_dataset.read_wav16(build.tmp / "derived_a" / DEFAULT_CLIPS[0][0])
    expected = (clip * 32767.0).astype(np.int16)

    positive = next(
        window for window, category in zip(sink.windows, sink.categories)
        if category == POSITIVE
    )
    end = build_dataset.WINDOW_SAMPLES - int(
        round(round8.TRAILING_OFFSETS_S[0] * build_dataset.SAMPLE_RATE)
    )
    start = end - expected.size
    assert np.array_equal(positive[start:end], expected)
    assert not positive[:start].any() and not positive[end:].any()

    # Non-vacuity: the recording is not silence, so "equal" above is a real
    # comparison and the zero-padding assertions are not trivially true.
    assert np.abs(expected).max() > 1000

    # And the whole build is reproducible: nothing consumed a random draw.
    again, _ = build.run(positive_windows=1, negative_windows=1)
    assert np.array_equal(np.stack(sink.windows), np.stack(again.windows))
    assert sink.settings == again.settings


def test_a_source_that_contributes_nothing_is_refused(build: Build) -> None:
    """Every clip excluded is a command-line mistake, not a configuration.

    Building on quietly would report a speaker as present in a dataset that
    contains none of their audio.
    """
    def exclude_all(manifest: dict) -> None:
        for row in manifest["clips"]:
            row["excluded"] = True

    path = write_speaker(
        build.tmp / "allout", speaker=TRAIN, split="train", mutate=exclude_all
    )
    build.refuse("contributes", source=[f"human={path}"])

    with pytest.raises(round8.Refused, match="nothing to build from"):
        build.run(source=[])


def test_excluded_takes_stay_out_and_the_rest_are_used(build: Build) -> None:
    """``excluded`` is a listener's judgement, and the only thing enforcing it."""
    for relative, _, _, _, _ in DEFAULT_CLIPS:
        assert (build.tmp / "derived_a" / relative).is_file(), (
            "every fixture clip must exist, or 'excluded' cannot be told apart "
            "from 'not found'"
        )

    sink, stats = build.run()
    used = {record["clip"] for record in sink.settings}
    assert "p_002.wav" not in used, "an excluded take was ingested"
    assert len(used) == USABLE_POSITIVES + USABLE_NEGATIVES
    assert stats["sources"][0]["clips_excluded"] == EXCLUDED_CLIPS
    assert stats["sources"][0]["clips_used"] == USABLE_POSITIVES + USABLE_NEGATIVES

    for flag in (None, "true", 1):
        def bad(manifest: dict, value=flag) -> None:
            if value is None:
                manifest["clips"][0].pop("excluded")
            else:
                manifest["clips"][0]["excluded"] = value

        path = write_speaker(
            build.tmp / f"flag_{flag}", speaker=TRAIN, split="train", mutate=bad
        )
        build.refuse("not a boolean", source=[f"human={path}"])


def test_every_window_carries_its_provenance_to_disk(build: Build) -> None:
    """The per-window record is what makes the claim checkable later.

    "This model was fitted on those recordings" is a statement about specific
    bytes; without the hashes beside each window it is an assertion.
    """
    sink, _ = build.run()
    for record in sink.settings:
        assert record["source"] and len(record["source_sha256"]) == 64
        assert len(record["clip_sha256"]) == 64 and len(record["manifest_sha256"]) == 64
        assert record["licence"]
        assert record["category"].endswith("_human")
        assert record["speaker"] == TRAIN and record["split"] == "train"
        assert round8.synthetic_source_marker(record["source"]) is None
