"""A synthetic clip must stay refused after it has been renamed and moved.

Background
----------
``build_human_dataset.py`` refused synthetic material on two things: the shape of
a path (``data/features*``, ``data/tts``, the generator and TTS corpus names) and
corroboration of a clip's ``source_file`` against the manifest's own ``files``
list. Both are real guards and both are tested. Neither of them looks at the
audio.

So this attack read clean end to end:

    copy one synthetic wav out of ``data/tts`` into a directory with an innocent
    name, write a *fresh* manifest that hashes the copy correctly and gives it an
    innocent ``source_file`` with an innocent ``source_sha256``, freeze it, and
    build.

Every path rule passes -- no marker appears anywhere. Every hash self-checks --
the clip hashes to what the manifest records, the manifest hashes to its own
sidecar, and the fabricated original corroborates the row it vouches for. The
Owner requirement is the answer to it: "Provenance must be based on frozen
manifests and hashes, not directory names alone." A name is what an attacker
chooses; the bytes are not.

What closed it is content addressing. ``scripts/wakeword/retired_synthetic_artifacts.json``
records the SHA-256 of known-synthetic artifacts and the builder consults it for
every clip, every corroborating digest, every corpus file and every
initialization checkpoint, so a byte-identical copy is refused wherever it sits
and whatever it is called. What that registry does and does not cover is written
into the registry itself and asserted below, because a guard that is believed to
cover more than it does is worse than one nobody trusts.

These tests are hermetic. Every manifest and every derived tree is built under
``tmp_path``; nothing reads the real human recordings or the training tree. The
one file they read out of the repository is a *synthetic* committed fixture --
``tests/fixtures/wakeword/audio/00_positive_clean.wav`` and its siblings are cut
from the synthetic pipeline's own evaluation split by
``scripts/wakeword/make_test_fixture.py``, which is exactly what makes them the
honest stand-in for a clip lifted out of ``data/tts``: no person ever spoke into
a microphone to produce one, and they are already in the registry.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import build_human_dataset as round8  # noqa: E402
import freeze_manifest  # noqa: E402

from tests.tools.test_wakeword_round8_human_only import (  # noqa: E402
    DEFAULT_CLIPS,
    POSITIVE,
    TRAIN,
    Build,
    write_corpus,
    write_speaker,
)

#: A synthetic clip that is already in this repository. Cut out of the synthetic
#: pipeline's evaluation split, 16 kHz mono 16-bit, and a TTS positive: the
#: closest thing to "a wav copied out of data/tts" that a hermetic test can have.
SYNTHETIC_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "wakeword" / "audio" / "00_positive_clean.wav"

#: A second one, so a test needing two distinct registered artifacts does not
#: have to reuse the first and pass on a coincidence.
SYNTHETIC_FIXTURE_2 = REPO_ROOT / "tests" / "fixtures" / "wakeword" / "audio" / "21_synthesized_speech.wav"

#: The synthetic-era detector this repository ships. Fitted on 20,000 synthesized
#: positives (``tools/wakewords/MODEL_CARD.md``), which is what makes it the
#: honest stand-in for ``candidates/r7/checkpoint.pt`` in the initialization test:
#: a real, tracked, retired artifact rather than bytes a test invented.
SHIPPED_EXPORT = REPO_ROOT / "tools" / "wakewords" / "hey_youtab.onnx"

#: The clip row the attacks below overwrite: an approved human category, in a
#: directory whose name is one of the approved human category names. Nothing
#: about the path is suspicious, which is the point.
INNOCENT_CLIP = DEFAULT_CLIPS[0][0]

#: The original the first clip row is derived from, as ``DEFAULT_CLIPS`` names it.
INNOCENT_ORIGINAL = DEFAULT_CLIPS[0][3]

#: Registry entries whose bytes are in this repository, so CI can re-verify them.
#: The rest of the registry describes the retired training tree, which is not
#: present in CI -- that being the whole point of addressing it by content.
TRACKED_KINDS = frozenset({"shipped_synthetic_era_export", "synthetic_fixture"})


@pytest.fixture()
def build(tmp_path: Path) -> Build:
    """The same one-build harness the Round 8 suite uses.

    Declared here rather than imported so this file's fixtures are visible in
    it: ``Build.refuse`` also asserts that nothing was emitted before the
    refusal, which is half of what each attack below has to show.
    """
    return Build(tmp_path)


def _sha256(path: Path) -> str:
    """Hashed here, independently of the module under test."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _can_symlink() -> bool:
    """Whether this platform lets an unprivileged process create a symlink.

    Windows needs Developer Mode or an elevated shell; POSIX does not. Probed
    the same way ``tests/cli/test_worktree_security.py`` probes it. The guard
    under test is not platform-specific -- only the ability to *stage* the attack
    is -- so CI (Linux) always runs it.
    """
    try:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            source.write_text("x", encoding="utf-8")
            (Path(directory) / "lnk").symlink_to(source)
        return True
    except (OSError, NotImplementedError):
        return False


def refreeze(manifest_path: Path, edit) -> None:
    """Apply ``edit`` to the manifest and re-freeze it over the result.

    Every attack in this file is a *fresh, internally consistent* manifest, so
    the sidecar is always recomputed. A stale digest anywhere would be caught by
    a guard that already existed and the test would pass for the wrong reason.
    """
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    edit(body)
    manifest_path.write_text(
        json.dumps(body, indent=2) + "\n", encoding="utf-8", newline=""
    )
    manifest_path.with_name(manifest_path.name + ".sha256").write_text(
        f"{_sha256(manifest_path)}  {manifest_path.name}\n", encoding="utf-8", newline="\n"
    )


def relocate(manifest_path: Path, *, clip: str, artifact: Path) -> str:
    """Put ``artifact``'s bytes at ``clip`` and re-freeze the manifest around them.

    This is the attack, performed exactly as it would be: the copy is hashed and
    the manifest records that hash, so the row is internally consistent and every
    hash in the chain self-checks.
    """
    target = manifest_path.parent / clip
    shutil.copyfile(artifact, target)
    digest = _sha256(target)

    def edit(body: dict) -> None:
        rows = [row for row in body["clips"] if row["clip"] == clip]
        assert len(rows) == 1, f"{clip} is not one row of {manifest_path}"
        rows[0]["sha256"] = digest

    refreeze(manifest_path, edit)
    return digest


def assert_no_marker_fires(manifest_path: Path) -> None:
    """Prove the path rules are all clean before asserting a refusal.

    Applied exactly where the builder applies each one, and no wider:
    ``synthetic_tree_marker`` judges *filesystem* paths (the manifest, the audio
    on disk) and ``synthetic_source_marker`` judges the short relative
    references the derivation writes *inside* the manifest. Widening either --
    running the source markers over an absolute path, say -- would match
    pytest's own ``tmp_path``, which is named after this test.

    Without this, a green test could be one where the pre-existing marker guard
    did the work and the content address was never consulted.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for path in (manifest_path, *(manifest_path.parent / r["clip"] for r in manifest["clips"])):
        assert round8.synthetic_tree_marker(path) is None, path
    for name in (
        *(row["clip"] for row in manifest["clips"]),
        *(row["source_file"] for row in manifest["clips"]),
        *(entry["source_file"] for entry in manifest["files"]),
        *(entry["full_16k"] for entry in manifest["files"]),
    ):
        assert round8.synthetic_source_marker(name) is None, name


# ── the registry itself ──────────────────────────────────────────────────────


def test_the_registry_is_committed_data_and_re_verifies_what_is_tracked() -> None:
    """The hashes have to be real, and the ones that can be checked are checked.

    Two halves, and they fail differently on purpose. The entries whose bytes are
    in this repository are re-hashed here on every run, so a mistyped or stale
    digest is a red test rather than a guard that silently matches nothing. The
    entries describing the retired training tree cannot be re-verified in CI --
    the tree is not there, which is exactly why they are addressed by content --
    so what is asserted about them is that they are well formed, unique, and
    present in the numbers this file claims.
    """
    registry = round8.load_retired_artifacts()

    # Pinned as a total, not a minimum: an entry silently *dropped* is the
    # regression this file exists to prevent, and a minimum would not see it.
    assert len(registry) == 193, f"the registry changed size: {len(registry)}"

    by_kind: dict[str, int] = {}
    for digest, entry in registry.items():
        assert digest == digest.lower() and len(digest) == 64
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
    assert by_kind == {
        "shipped_synthetic_era_export": 2,
        "synthetic_fixture": 24,
        "rejected_checkpoint": 11,
        "rejected_export": 22,
        "rejected_score_tensor": 22,
        "synthetic_feature_tensor": 22,
        "synthetic_corpus_sample": 90,
    }, by_kind

    tracked = {d: e for d, e in registry.items() if e["kind"] in TRACKED_KINDS}
    # Non-vacuity: there have to be tracked entries to re-verify, or the loop
    # below proves nothing by passing.
    assert len(tracked) == 26, len(tracked)
    for digest, entry in tracked.items():
        path = REPO_ROOT / entry["name"]
        assert path.is_file(), f"{entry['name']} is registered but not tracked"
        assert _sha256(path) == digest, (
            f"{entry['name']} no longer hashes to its registry entry. Either the "
            "file changed -- in which case the change is the finding -- or the "
            "registry records a hash of nothing."
        )

    # Cross-source agreement: the shipped detector's digests are also written in
    # its model card, by a different tool at a different time. Two independent
    # records of the same bytes is what makes either of them worth reading.
    card = (REPO_ROOT / "tools" / "wakewords" / "MODEL_CARD.md").read_text(encoding="utf-8")
    for digest, entry in registry.items():
        if entry["kind"] == "shipped_synthetic_era_export":
            assert digest in card, f"{entry['name']} is not the model card's hash"

    # No human recording may ever be listed here. The recorded fixtures are the
    # nearest thing in the repository to one, and they are deliberately absent:
    # they are Speech Commands recordings of real people, so calling them
    # retired synthetic material would be false.
    audio = REPO_ROOT / "tests" / "fixtures" / "wakeword" / "audio"
    recorded = sorted(audio.glob("*_recorded_speech.wav")) + sorted(audio.glob("*_background_only.wav"))
    assert len(recorded) == 9, len(recorded)
    for path in recorded:
        assert _sha256(path) not in registry, f"{path.name} is a recording, not synthetic"


def test_the_registry_states_the_one_limit_it_has_in_numbers() -> None:
    """The corpus sample is a demonstration, not coverage, and says so checkably.

    207,300 synthetic clips is not a hash list a source repository can carry, so
    that group is a documented sample. The risk with a sample is that it gets
    read as coverage later -- "the clips are content-addressed" -- and a build is
    trusted for a property it does not have. The numbers are asserted against the
    entries so the claim cannot drift away from the file.
    """
    body = json.loads((WAKEWORD / round8.RETIRED_ARTIFACTS_FILENAME).read_text(encoding="utf-8"))
    sample = body["corpus_sample"]
    listed = sum(1 for row in body["artifacts"] if row["kind"] == "synthetic_corpus_sample")

    assert sample["clips_sampled"] == listed
    assert sample["per_category"] * sample["categories"] == listed
    assert listed < sample["clips_in_corpora"] / 100, (
        "the sample is being described as though it were coverage"
    )
    # And the disclosure is in the file rather than only in this test.
    assert any("207,300" in text or "207300" in text for text in body["does_not_cover"])
    assert body["enforced_by"].startswith("scripts/wakeword/build_human_dataset.py")


@pytest.mark.parametrize(
    ("name", "text", "pattern"),
    [
        ("absent.json", None, "cannot be read"),
        ("empty.json", "", "not readable as JSON"),
        ("truncated.json", '{"schema_version": 1, "artifacts": [', "not readable as JSON"),
        ("list.json", "[]", "does not contain a registry object"),
        ("version.json", '{"schema_version": 99, "artifacts": [{"sha256": "%s", "kind": "k", "name": "n"}]}',
         "schema_version 99"),
        ("noartifacts.json", '{"schema_version": 1}', "lists no artifacts"),
        ("emptyartifacts.json", '{"schema_version": 1, "artifacts": []}', "lists no artifacts"),
        ("notobject.json", '{"schema_version": 1, "artifacts": ["%s"]}', "is not an object"),
        ("nohash.json", '{"schema_version": 1, "artifacts": [{"sha256": "nope", "kind": "k", "name": "n"}]}',
         "which is not a SHA-256"),
        ("noname.json", '{"schema_version": 1, "artifacts": [{"sha256": "%s", "kind": "k"}]}',
         "has no 'name'"),
        ("nokind.json", '{"schema_version": 1, "artifacts": [{"sha256": "%s", "name": "n"}]}',
         "has no 'kind'"),
        ("twice.json",
         '{"schema_version": 1, "artifacts": ['
         '{"sha256": "%s", "kind": "k", "name": "one"}, '
         '{"sha256": "%s", "kind": "k", "name": "two"}]}',
         "is listed twice"),
    ],
)
def test_a_registry_that_cannot_be_trusted_is_a_refusal_not_a_pass(
    build: Build,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    text: str | None,
    pattern: str,
) -> None:
    """Fail closed. Every one of these is a state where the check does nothing.

    A missing file, a truncated write, a schema nobody updated, an artifact list
    that lost its contents, an entry with no hash in it: in each case the build
    would run with the content address consulting nothing and would then write
    ``synthetic_samples: 0`` -- a measurement it did not make. So each one aborts
    the whole build, which is why this is asserted through ``build.refuse`` and
    not only against the loader.
    """
    registry = tmp_path / "registries" / name
    registry.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        registry.write_text(text.replace("%s", "a" * 64), encoding="utf-8")
    monkeypatch.setattr(round8, "RETIRED_ARTIFACTS_PATH", registry)

    with pytest.raises(round8.Refused, match=pattern):
        round8.load_retired_artifacts()
    build.refuse(pattern)

    # Non-vacuity: a well-formed registry at that same path loads and is used, so
    # every refusal above is the defect and not the redirection.
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [{"sha256": "b" * 64, "kind": "k", "name": "n"}],
            }
        ),
        encoding="utf-8",
    )
    assert list(round8.load_retired_artifacts()) == ["b" * 64]
    sink, stats = build.run()
    assert sink.labels and stats["synthetic_samples"] == 0


# ── the relocation attack ────────────────────────────────────────────────────


def test_a_relocated_synthetic_clip_is_refused_by_content_address(
    build: Build, tmp_path: Path
) -> None:
    """The attack this file exists for: a synthetic wav under an innocent name.

    The manifest is the canonical clean one -- an approved training speaker, a
    frozen sidecar, a corroborated ``files`` list, an approved category with the
    matching label -- and one clip's *bytes* are a synthetic artifact. Only the
    content decides this, and the assertions below prove no other guard could
    have: no marker fires on any path or reference in the manifest.
    """
    path = write_speaker(tmp_path / "derived_relocated", speaker=TRAIN, split="train")
    digest = relocate(path, clip=INNOCENT_CLIP, artifact=SYNTHETIC_FIXTURE)

    assert_no_marker_fires(path)

    message = build.refuse("retired synthetic artifact", source=[f"human={path}"])
    assert digest in message, message
    assert INNOCENT_CLIP in message.replace("\\", "/"), message

    # Non-vacuity: the same manifest, same speaker, same category, same clip
    # path -- with audio nobody synthesized -- builds. So the refusal is the
    # bytes and nothing else about the fixture.
    clean = write_speaker(tmp_path / "derived_clean", speaker=TRAIN, split="train")
    _, stats = build.run(source=[f"human={clean}"])
    assert stats["synthetic_samples"] == 0
    assert stats["samples_by_category"][POSITIVE] == 2


def test_a_relocated_synthetic_original_or_decode_is_refused(
    build: Build, tmp_path: Path
) -> None:
    """The other half of the provenance chain, relocated.

    A clip can be a real recording of a person and still be inadmissible if the
    thing it was derived *from* is retired synthetic material. Both layers the
    builder verifies are attacked here:

    * the corroborating original, reached under ``source_dir``; and
    * the full-length 16 kHz decode, which is what carries the verification when
      the capture drive is not mounted -- the layer that is *always* the one in
      use on a machine that is not the capture machine.
    """
    # The recording the manifest vouches for is itself a retired artifact, and
    # the manifest is entirely consistent about it.
    root = tmp_path / "derived_original"
    path = write_speaker(root, speaker=TRAIN, split="train")
    original = root / "originals" / INNOCENT_ORIGINAL
    shutil.copyfile(SYNTHETIC_FIXTURE, original)
    digest = _sha256(original)

    def declare(body: dict) -> None:
        for entry in body["files"]:
            if entry["source_file"] == INNOCENT_ORIGINAL:
                entry["source_sha256"] = digest
        for row in body["clips"]:
            if row["source_file"] == INNOCENT_ORIGINAL:
                row["source_sha256"] = digest

    refreeze(path, declare)
    assert_no_marker_fires(path)
    message = build.refuse("retired synthetic artifact", source=[f"human={path}"])
    assert digest in message and INNOCENT_ORIGINAL in message

    # The same bytes with the digest *not* declared: the manifest lies about its
    # own original, so this proves the check is on the bytes read from disk and
    # not only on what the manifest says about them.
    lying = tmp_path / "derived_lying"
    lying_path = write_speaker(lying, speaker=TRAIN, split="train")
    shutil.copyfile(SYNTHETIC_FIXTURE, lying / "originals" / INNOCENT_ORIGINAL)
    build.refuse("retired synthetic artifact", source=[f"human={lying_path}"])

    # And the offline layer: no capture drive, and the full-length decode the
    # verification falls back to is a retired artifact.
    offline = tmp_path / "derived_offline"
    offline_path = write_speaker(
        offline, speaker=TRAIN, split="train", originals_on_disk=False
    )
    decode = offline / "full" / f"{Path(INNOCENT_ORIGINAL).stem}.wav"
    shutil.copyfile(SYNTHETIC_FIXTURE_2, decode)
    decoded_digest = _sha256(decode)

    def declare_decode(body: dict) -> None:
        for entry in body["files"]:
            if entry["source_file"] == INNOCENT_ORIGINAL:
                entry["full_16k_sha256"] = decoded_digest

    refreeze(offline_path, declare_decode)
    message = build.refuse("retired synthetic artifact", source=[f"human={offline_path}"])
    assert decoded_digest in message

    # Non-vacuity: the offline layer is otherwise a build that works, so the
    # refusal is the content of the decode and not the missing capture drive.
    fine = write_speaker(
        tmp_path / "derived_fine", speaker=TRAIN, split="train", originals_on_disk=False
    )
    _, stats = build.run(source=[f"human={fine}"])
    assert stats["sources"][0]["originals_offline"] == 3


def test_a_relocated_synthetic_clip_in_a_recorded_corpus_is_refused(
    build: Build, tmp_path: Path
) -> None:
    """A recorded corpus is the other door into the dataset.

    ``freeze_manifest`` hashes whatever is in the directory it is pointed at, so
    a synthetic wav dropped in before the freeze produces a perfectly consistent
    frozen manifest of the wrong audio -- and the corpus loader's own checks (the
    schema, the usage gate, the per-file hashes) all pass on it.
    """
    corpus_root = tmp_path / "rooms"
    manifest = write_corpus(corpus_root)

    # Non-vacuity first, while the corpus is still what it says it is: this same
    # source builds and contributes its three recordings.
    _, stats = build.run(source=[f"human={build.human}", f"speech_commands={manifest}"])
    assert stats["sources"][1]["files_used"] == 3

    shutil.copyfile(SYNTHETIC_FIXTURE, corpus_root / "clips" / "rec_001.wav")
    relocated = corpus_root / "relocated.manifest.json"
    freeze_manifest.freeze(
        corpus_root / "clips",
        relocated,
        dataset="fixture-corpus",
        split="train",
        usage="training",
        note="hermetic fixture; a synthetic clip filed as a recorded negative",
    )

    message = build.refuse(
        "retired synthetic artifact",
        source=[f"human={build.human}", f"speech_commands={relocated}"],
    )
    assert "rec_001.wav" in message


def test_a_relocated_synthetic_checkpoint_is_refused_as_an_initialization(
    tmp_path: Path,
) -> None:
    """The prior art, restated: a retired candidate under a Round 8 name.

    Copy a synthetic-era artifact into a clean directory as
    ``round8_pretrained_init.pt`` and write the contract this stage writes beside
    it. The path check passes -- there is no ``candidates`` or ``data/features``
    anywhere in it. The evidence check passes -- the contract says ``human_only``
    with zero synthetic samples. Both of those are things a copy brings with it,
    and neither is a statement about the weights.

    A synthetic model with real data fine-tuned onto it is still a synthetic
    model, and nothing about the artifact says so afterwards. The content address
    is what says so.
    """
    init = tmp_path / "round8" / "round8_pretrained_init.pt"
    init.parent.mkdir(parents=True)
    shutil.copyfile(SHIPPED_EXPORT, init)
    contract = {"round": 8, "human_only": True, "synthetic_samples": 0}
    (init.parent / round8.CONTRACT_FILENAME).write_text(
        json.dumps(contract), encoding="utf-8"
    )

    # Non-vacuity: the path guard finds nothing in this layout, and the contract
    # beside it is one this stage accepts -- proved at the end of this test, where
    # the identical directory with different weights is accepted. So the refusal
    # below can only be the content address.
    assert round8.synthetic_tree_marker(init) is None

    with pytest.raises(round8.Refused, match="retired synthetic artifact") as raised:
        round8.refuse_synthetic_initialization(init)
    assert _sha256(SHIPPED_EXPORT) in str(raised.value)
    assert "hey_youtab.onnx" in str(raised.value)

    # ...and the same directory, the same name, the same contract, with weights
    # nobody retired: accepted. So the guard refuses these bytes and not this
    # filename.
    init.write_bytes(b"round 8 weights, fitted on real recordings only")
    round8.refuse_synthetic_initialization(init)

    # A checkpoint that is not there at all is a refusal too: an initialization
    # this stage cannot hash is one it cannot show is not a retired candidate.
    init.unlink()
    with pytest.raises(round8.Refused, match="is not a file"):
        round8.refuse_synthetic_initialization(init)


# ── the rename, and the indirection ──────────────────────────────────────────


def test_a_renamed_synthetic_clip_is_refused_by_name_then_by_provenance_then_by_content(
    build: Build, tmp_path: Path
) -> None:
    """RENAMED, in all three of the forms the rename can take.

    The first two are covered in full by the Round 8 suite and are re-asserted
    here because this file is the place the three vectors are read together:

    * ``test_a_synthetic_era_category_is_a_hard_error`` -- a TTS group name with
      no real-derivation meaning is refused on sight;
    * ``test_a_renamed_synthetic_clip_is_refused_by_provenance_not_by_name`` -- a
      clip wearing ``positive_human`` whose ``source_file`` the manifest's own
      ``files`` list does not corroborate.

    The third is the one this file adds, and it is the one that used to work: the
    rename *plus* a corroborated provenance line. Category approved, label
    correct, ``source_file`` present in ``files`` with a matching
    ``source_sha256``, every hash self-checking. Nothing about the names is
    wrong, so nothing about the names can refuse it.
    """
    def rename(body: dict) -> None:
        body["clips"][3]["category"] = "positive"  # the TTS group's own name

    named = write_speaker(
        tmp_path / "renamed_by_name", speaker=TRAIN, split="train", mutate=rename
    )
    build.refuse("synthetic-era category", source=[f"human={named}"])

    def uncorroborated(body: dict) -> None:
        body["clips"][0]["category"] = POSITIVE
        body["clips"][0]["source_file"] = "generated/near/000001.wav"
        body["clips"][0]["source_sha256"] = "d" * 64

    stray = write_speaker(
        tmp_path / "renamed_uncorroborated",
        speaker=TRAIN,
        split="train",
        mutate=uncorroborated,
    )
    build.refuse("does not contain", source=[f"human={stray}"])

    # The rename that survived both of those: a synthetic positive filed as
    # `positive_human`, in a directory named after that category, with a fully
    # corroborated provenance chain around it.
    path = write_speaker(tmp_path / "renamed_and_moved", speaker=TRAIN, split="train")
    digest = relocate(path, clip=INNOCENT_CLIP, artifact=SYNTHETIC_FIXTURE)
    assert_no_marker_fires(path)

    body = json.loads(path.read_text(encoding="utf-8"))
    row = next(r for r in body["clips"] if r["clip"] == INNOCENT_CLIP)
    # Non-vacuity for "the names all read correct": this row would be admitted by
    # every name-based and corroboration-based rule in the stage.
    assert row["category"] == POSITIVE and row["label"] == 1
    assert row["source_file"] in {f["source_file"] for f in body["files"]}
    assert row["source_sha256"] == next(
        f["source_sha256"] for f in body["files"] if f["source_file"] == row["source_file"]
    )
    assert row["sha256"] == digest == _sha256(path.parent / INNOCENT_CLIP)

    build.refuse("retired synthetic artifact", source=[f"human={path}"])


@pytest.mark.parametrize(
    ("case", "pattern"),
    [
        ("clip_traversal", "climbs out of its own directory"),
        ("decode_traversal", "climbs out of its own directory"),
        ("source_traversal", "climbs out of its own directory"),
        ("source_absolute", "is an absolute path"),
    ],
)
def test_an_indirectly_referenced_clip_or_original_is_refused(
    build: Build, tmp_path: Path, case: str, pattern: str
) -> None:
    """INDIRECTLY REFERENCED: a reference that leaves the frozen tree.

    A manifest's digest stands for one derivation. A reference that climbs out of
    the manifest's own directory, or names an absolute path, reaches bytes that
    derivation never covered -- and it does so while the manifest still reads like
    a self-contained record, because the escape is in a path and the path still
    resolves to a file that hashes correctly.

    The audio placed outside the tree here is a legitimate copy of the fixture's
    own clip, deliberately *not* a registered artifact: this test is about the
    indirection, so the content address must not be what refuses it. That the
    hashes all still match is the point -- indirection is not a hash failure.
    """
    root = tmp_path / "derived_indirect"
    path = write_speaker(root, speaker=TRAIN, split="train")
    outside = tmp_path / "outside"
    outside.mkdir()

    if case == "clip_traversal":
        moved = outside / "innocent.wav"
        shutil.copyfile(root / INNOCENT_CLIP, moved)

        def edit(body: dict) -> None:
            row = next(r for r in body["clips"] if r["clip"] == INNOCENT_CLIP)
            row["clip"] = f"../{outside.name}/{moved.name}"
            row["sha256"] = _sha256(moved)

    elif case == "decode_traversal":
        stem = Path(INNOCENT_ORIGINAL).stem
        moved = outside / f"{stem}.wav"
        shutil.copyfile(root / "full" / f"{stem}.wav", moved)

        def edit(body: dict) -> None:
            entry = next(f for f in body["files"] if f["source_file"] == INNOCENT_ORIGINAL)
            entry["full_16k"] = f"../{outside.name}/{moved.name}"
            entry["full_16k_sha256"] = _sha256(moved)

    else:
        escaped = (
            f"/{outside.name}/{INNOCENT_ORIGINAL}"
            if case == "source_absolute"
            else f"../{outside.name}/{INNOCENT_ORIGINAL}"
        )

        def edit(body: dict) -> None:
            for entry in body["files"]:
                if entry["source_file"] == INNOCENT_ORIGINAL:
                    entry["source_file"] = escaped
            for row in body["clips"]:
                if row["source_file"] == INNOCENT_ORIGINAL:
                    row["source_file"] = escaped

    refreeze(path, edit)
    if case == "decode_traversal":
        # The offline layer is only consulted when the capture drive is absent.
        (root / "originals" / INNOCENT_ORIGINAL).unlink()

    assert_no_marker_fires(path)
    build.refuse(pattern, source=[f"human={path}"])

    # Non-vacuity: the same fixture, unescaped, builds. Every refusal above is
    # the indirection and nothing about the tree it was staged in.
    direct = write_speaker(tmp_path / "derived_direct", speaker=TRAIN, split="train")
    sink, stats = build.run(source=[f"human={direct}"])
    assert sink.labels and stats["synthetic_samples"] == 0


def test_an_escaping_reference_is_refused_the_same_way_on_every_platform() -> None:
    """The platform the build runs on must not decide what the guard sees.

    A native ``Path`` on Linux reads ``C:/x``, ``\\\\host\\share\\x`` and
    ``..\\x`` as ordinary relative names, so a manifest written on Windows would
    be judged by one rule there and another one here. Each of those escapes the
    root it is supposed to be inside, on the platform that understands it.
    """
    for name in (
        "/etc/passwd",
        "C:/retired/checkpoint.pt",
        "c:\\retired\\checkpoint.pt",
        "\\\\host\\share\\clip.wav",
    ):
        with pytest.raises(round8.Refused, match="is an absolute path"):
            round8.refuse_escaping_name(name, "clip")

    for name in ("../outside/clip.wav", "..\\outside\\clip.wav", "a/../../b/clip.wav"):
        with pytest.raises(round8.Refused, match="climbs out of its own directory"):
            round8.refuse_escaping_name(name, "clip")

    # Non-vacuity: what the derivation actually writes -- a relative POSIX path
    # inside its own root -- is returned unchanged.
    for name in ("clips/positive_human/p_000.wav", "01_close_normal.m4a"):
        assert round8.refuse_escaping_name(name, "clip") == Path(name)


@pytest.mark.skipif(not _can_symlink(), reason="symlinks need elevated privileges")
def test_a_clip_reached_through_a_symlink_is_refused(build: Build, tmp_path: Path) -> None:
    """INDIRECTLY REFERENCED, the shape that is invisible in the manifest.

    A symlink is the one indirection a manifest cannot show: every string in it
    is a plain relative reference into its own tree, every hash matches, and the
    bytes that get read are wherever the link points -- chosen by whoever created
    the link rather than by the derivation that was adjudicated. Both shapes are
    refused: the reference itself being a link, and a *directory component* of it
    being one, which is the cheaper attack because it moves a whole category at
    once.
    """
    outside = tmp_path / "outside"
    outside.mkdir()

    # The leaf is a link.
    leaf_root = tmp_path / "derived_leaf"
    leaf = write_speaker(leaf_root, speaker=TRAIN, split="train")
    clip = leaf_root / INNOCENT_CLIP
    target = outside / "p_000.wav"
    shutil.copyfile(clip, target)  # same bytes, so the recorded hash still matches
    clip.unlink()
    clip.symlink_to(target)
    assert _sha256(clip) == _sha256(target), "the link must resolve to the same bytes"
    assert_no_marker_fires(leaf)
    build.refuse("reached through the symlink", source=[f"human={leaf}"])

    # A directory component is a link.
    dir_root = tmp_path / "derived_dir"
    manifest = write_speaker(dir_root, speaker=TRAIN, split="train")
    category = dir_root / Path(INNOCENT_CLIP).parent
    moved = outside / "positives"
    shutil.move(str(category), str(moved))
    category.symlink_to(moved, target_is_directory=True)
    assert (dir_root / INNOCENT_CLIP).is_file(), "the staged attack has to resolve"
    build.refuse("reached through the symlink", source=[f"human={manifest}"])

    # Non-vacuity: with the directory in place rather than linked, it builds.
    direct = write_speaker(tmp_path / "derived_direct", speaker=TRAIN, split="train")
    sink, _ = build.run(source=[f"human={direct}"])
    assert sink.labels
