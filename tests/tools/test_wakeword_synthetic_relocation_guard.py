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

import ast
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = REPO_ROOT / "scripts" / "wakeword"
sys.path.insert(0, str(WAKEWORD))

import build_dataset  # noqa: E402
import build_human_dataset as round8  # noqa: E402
import freeze_manifest  # noqa: E402

from tests.tools.test_wakeword_round8_human_only import (  # noqa: E402
    DEFAULT_CLIPS,
    OTHER_TRAIN,
    POSITIVE,
    TRAIN,
    Build,
    write_corpus,
    write_speaker,
)
from tests.tools.test_wakeword_round8_human_only import _write_wav as write_wav  # noqa: E402

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


def _can_hardlink() -> bool:
    """Whether this platform lets an unprivileged process create a hard link.

    Probed rather than assumed, the same way ``_can_symlink`` probes its own
    capability: every POSIX filesystem and NTFS allow it, FAT-family volumes and
    some network mounts do not. The guard under test is not platform-specific --
    only the ability to *stage* the attack is -- so CI (Linux) always runs it.
    """
    try:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            source.write_text("x", encoding="utf-8")
            os.link(source, Path(directory) / "lnk")
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


def substitute(manifest_path: Path, *, clip: str, seed: int = 4242) -> str:
    """Put bytes the registry has never been shown at ``clip``, and re-freeze.

    The mirror image of ``relocate``: same fresh, self-consistent manifest, but
    the audio is a wav generated here. It stands in for one of the 207,210
    retired TTS clips the registry does not list -- a hermetic test cannot carry
    real TTS audio, and the point is precisely that content addressing does not
    recognise these bytes.
    """
    target = manifest_path.parent / clip
    write_wav(target, 0.7, seed=seed)
    digest = _sha256(target)

    def edit(body: dict) -> None:
        rows = [row for row in body["clips"] if row["clip"] == clip]
        assert len(rows) == 1, f"{clip} is not one row of {manifest_path}"
        rows[0]["sha256"] = digest

    refreeze(manifest_path, edit)
    return digest


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
    # The fraction, to the digit the registry records it to. The assertion here
    # used to be ``listed < clips_in_corpora / 100``: true, and twenty times
    # weaker than the truth -- it would have passed just as happily at 2,000
    # clips, which is the slack an over-claim grows into.
    assert sample["coverage_percent"] == round(
        listed / sample["clips_in_corpora"] * 100, 3
    )
    assert sample["coverage_percent"] < 0.05, sample["coverage_percent"]
    assert "0.043%" in sample["coverage_claim"]
    # And the same number is in the prose of the stage that enforces it, so the
    # module cannot go on describing a coverage the registry does not have.
    enforcing = (WAKEWORD / "build_human_dataset.py").read_text(encoding="utf-8")
    assert "0.043%" in enforcing and "207,300" in enforcing
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


# ── the link with no metadata ────────────────────────────────────────────────


@pytest.mark.skipif(not _can_hardlink(), reason="this filesystem has no hard links")
def test_a_hard_linked_synthetic_clip_is_refused_by_content_and_nothing_else(
    build: Build, tmp_path: Path
) -> None:
    """HARD LINKS: the indirection that has nothing to detect.

    A symlink is a file whose contents are a path, which is why
    ``resolve_within`` can walk it. A hard link is not indirection at the path
    layer at all: it is a second *name* for one inode. There is no link bit, no
    target, no difference between the two names, and ``resolve()`` returns the
    reference itself -- so the escaping-name rule, the symlink walk and the
    containment check are all satisfied, and each of them is *run* below and
    passes.

    Which leaves the bytes. This is the vector that settles whether content
    addressing is a convenience layered on top of the path rules or the actual
    backstop: here it is the only layer that can fire at all.
    """
    root = tmp_path / "derived_hardlink"
    path = write_speaker(root, speaker=TRAIN, split="train")

    outside = tmp_path / "outside"
    outside.mkdir()
    stash = outside / "innocent.wav"
    shutil.copyfile(SYNTHETIC_FIXTURE, stash)

    clip = root / INNOCENT_CLIP
    clip.unlink()
    os.link(stash, clip)
    digest = _sha256(clip)

    def declare(body: dict) -> None:
        for row in body["clips"]:
            if row["clip"] == INNOCENT_CLIP:
                row["sha256"] = digest

    refreeze(path, declare)

    # The attack is staged, and it is invisible: one inode, two names, and
    # resolving the reference cannot reach the other one.
    assert clip.samefile(stash), "the two names have to be one inode"
    assert os.stat(clip).st_nlink == 2, "this is not actually a hard link"
    assert not clip.is_symlink()
    assert clip.resolve() != stash.resolve(), (
        "resolution must not reveal the other name -- that is the whole problem"
    )

    # Non-vacuity: every path-based guard is run here and admits the reference.
    probe = clip
    while probe != root:
        assert not probe.is_symlink(), probe
        probe = probe.parent
    assert round8.resolve_within(INNOCENT_CLIP, root, "clip") == clip
    assert_no_marker_fires(path)

    message = build.refuse("retired synthetic artifact", source=[f"human={path}"])
    assert digest in message, message
    assert INNOCENT_CLIP in message.replace("\\", "/"), message

    # Non-vacuity, the other direction: the same hard link, to bytes nobody
    # retired, builds. So what was refused is the content and not the link.
    clean_root = tmp_path / "derived_hardlink_clean"
    clean = write_speaker(clean_root, speaker=TRAIN, split="train")
    clean_clip = clean_root / INNOCENT_CLIP
    spare = outside / "spare.wav"
    shutil.copyfile(clean_clip, spare)
    clean_clip.unlink()
    os.link(spare, clean_clip)
    assert os.stat(clean_clip).st_nlink == 2
    sink, stats = build.run(source=[f"human={clean}"])
    assert sink.labels and stats["synthetic_samples"] == 0


# ── archives ─────────────────────────────────────────────────────────────────


def test_an_archive_is_not_a_manifest_and_extraction_launders_nothing(
    build: Build, tmp_path: Path
) -> None:
    """ARCHIVES: a zip attacks three different things, so this asserts three.

    *Nothing is unpacked here.* The stage imports no archive machinery, so there
    is no code path that can be handed a member list at all. Asserted against the
    module's own import table rather than trusted: a ``zipfile`` or a
    ``shutil.unpack_archive`` in this module would move member selection inside
    the stage, where a freeze covers none of it -- an archive's members are chosen
    at extraction time, by whoever built the archive.

    *An archive handed in as a manifest is refused with an instruction*, not a
    decode traceback from somewhere inside the loader.

    *Extraction launders nothing.* Tar the derivation and extract it elsewhere:
    every path changes, every mtime changes, and not one byte does -- so a retired
    artifact is still recognised on the other side. And the member that escapes on
    extraction lands outside the tree, where the manifest cannot name it without
    a reference that climbs out of its own directory.
    """
    module = ast.parse((WAKEWORD / "build_human_dataset.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    unpackers = {"zipfile", "tarfile", "gzip", "bz2", "lzma", "shutil", "py7zr", "rarfile"}
    assert not imported & unpackers, sorted(imported & unpackers)
    # Non-vacuity: the import table was actually read.
    assert {"json", "wave", "numpy", "build_dataset"} <= imported, sorted(imported)

    archive = tmp_path / "speaker_submission.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("MANIFEST.json", json.dumps({"spea" + "ker": TRAIN, "split": "train"}))
        bundle.writestr(INNOCENT_CLIP, SYNTHETIC_FIXTURE.read_bytes())
    # Non-vacuity: the archive really does hold a well-formed manifest and a clip,
    # so what is refused is the container rather than its contents.
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == sorted(["MANIFEST.json", INNOCENT_CLIP])
    build.refuse("is not text at all", source=[f"human={archive}"])

    # A submission that arrives as a tar, is extracted, and is built from.
    staged = tmp_path / "staged"
    manifest = write_speaker(staged, speaker=TRAIN, split="train")
    relocated = relocate(manifest, clip=INNOCENT_CLIP, artifact=SYNTHETIC_FIXTURE_2)
    escaping = DEFAULT_CLIPS[3][0]

    tarball = tmp_path / "speaker_submission.tar"
    with tarfile.open(tarball, "w") as bundle:
        bundle.add(staged, arcname="submission")
        # The classic archive attack: a member whose name climbs out of whatever
        # directory it is extracted into. Innocent bytes on purpose -- this half
        # is about the escape, so the content address must not be what refuses it.
        bundle.add(staged / escaping, arcname="../escaped/leaked.wav")

    extracted = tmp_path / "extracted"
    with tarfile.open(tarball) as bundle:
        # Deliberately unsanitised, because a sanitising extractor is not the
        # thing under test: the question is what this stage does with a tree that
        # was extracted by something that did not sanitise.
        bundle.extractall(extracted, filter="fully_trusted")

    escaped = tmp_path / "escaped" / "leaked.wav"
    # Non-vacuity: the escape actually happened.
    assert escaped.is_file(), "the staged archive escape did not write outside"
    assert round8.retired_artifact(_sha256(escaped)) is None

    through = extracted / "submission" / manifest.name
    assert_no_marker_fires(through)
    message = build.refuse("retired synthetic artifact", source=[f"human={through}"])
    assert relocated in message, message

    # And the escaped member cannot be reached from the manifest: the only way to
    # name it is a reference that leaves the frozen tree.
    def point_at_the_escape(body: dict) -> None:
        row = next(r for r in body["clips"] if r["clip"] == INNOCENT_CLIP)
        row["clip"] = f"../../escaped/{escaped.name}"
        row["sha256"] = _sha256(escaped)

    refreeze(through, point_at_the_escape)
    build.refuse("climbs out of its own directory", source=[f"human={through}"])

    # Non-vacuity: the same tar of the same tree, with audio nobody synthesized,
    # extracts and builds -- so the refusals above are the member and not the
    # round trip through an archive.
    clean_staged = tmp_path / "clean_staged"
    clean_manifest = write_speaker(clean_staged, speaker=TRAIN, split="train")
    clean_tarball = tmp_path / "clean_submission.tar"
    with tarfile.open(clean_tarball, "w") as bundle:
        bundle.add(clean_staged, arcname="submission")
    clean_extracted = tmp_path / "clean_extracted"
    with tarfile.open(clean_tarball) as bundle:
        bundle.extractall(clean_extracted, filter="data")
    sink, stats = build.run(
        source=[f"human={clean_extracted / 'submission' / clean_manifest.name}"]
    )
    assert sink.labels and stats["synthetic_samples"] == 0


# ── digests: prefixes, near misses and a truncated hash function ─────────────


def test_a_digest_is_compared_whole_and_never_coerced(
    build: Build, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PARTIAL HASHES, and a collision simulated at the validation layer.

    "The hashes are checked" can be false in four ways, so there are four claims:

    * the registry is exact-match over all 64 characters -- no prefix of a
      registered digest is a hit, and neither is a digest agreeing in 63 of them;
    * it is case-insensitive, so uppercasing a declared digest is not an escape;
    * a declared digest that is not 64 hex characters is *refused* rather than
      padded, truncated, or compared as far as it goes; and
    * if the hash function itself were ever shortened -- a cheaper checksum
      dropped in, a digest sliced to save space -- the build refuses instead of
      passing with a content check that silently matches nothing. That last one is
      the collision simulated: a truncated digest is exactly the state in which
      unrelated files begin to collide, and the answer has to be a refusal.
    """
    registered = _sha256(SYNTHETIC_FIXTURE)
    # Non-vacuity: the whole digest is a hit, so every miss below is the
    # comparison and not a registry that has never seen this artifact.
    assert round8.retired_artifact(registered) is not None
    assert round8.retired_artifact(registered.upper()) is not None
    for near in (
        registered[:12],
        registered[:32],
        registered[:63],
        registered[:63] + ("0" if registered[63] != "0" else "1"),
        registered[:32] + "0" * 32,
        registered + "0",
    ):
        assert round8.retired_artifact(near) is None, near

    for malformed in (registered[:63], registered + "a", "", "not a digest", 0, None):
        assert not round8._hex64(malformed), malformed
    assert round8._hex64(registered) and round8._hex64(registered.upper())

    # A clip row declaring a prefix of its own correct digest: refused for not
    # being a SHA-256, rather than accepted because what is there matches.
    truncated = write_speaker(tmp_path / "derived_truncated", speaker=TRAIN, split="train")

    def truncate(body: dict) -> None:
        row = next(r for r in body["clips"] if r["clip"] == INNOCENT_CLIP)
        row["sha256"] = row["sha256"][:40]

    refreeze(truncated, truncate)
    build.refuse("which is not a SHA-256", source=[f"human={truncated}"])

    # One character different, in the last position. A near miss is a miss.
    nearly = write_speaker(tmp_path / "derived_nearly", speaker=TRAIN, split="train")

    def last_character(body: dict) -> None:
        row = next(r for r in body["clips"] if r["clip"] == INNOCENT_CLIP)
        row["sha256"] = row["sha256"][:63] + ("0" if row["sha256"][63] != "0" else "1")

    refreeze(nearly, last_character)
    build.refuse("hashes to", source=[f"human={nearly}"])

    # And the shortened hash function. The recordings are real and the manifest is
    # honest about them; only the digests are half-length.
    honest = write_speaker(tmp_path / "derived_honest", speaker=TRAIN, split="train")
    sink, stats = build.run(source=[f"human={honest}"])
    assert sink.labels and stats["synthetic_samples"] == 0

    whole = build_dataset.sha256_file
    monkeypatch.setattr(
        build_dataset,
        "sha256_file",
        lambda path: whole(path)[:32] if str(path).endswith(".wav") else whole(path),
    )
    # The registry cannot recognise a truncated digest -- proved above -- so the
    # content check would quietly stop working. What refuses the build instead is
    # the full-digest comparison against the frozen manifest.
    build.refuse("hashes to", source=[f"human={honest}"])


# ── one bad clip in an otherwise legitimate build ────────────────────────────


def test_a_mixed_human_and_synthetic_batch_refuses_the_whole_build(
    build: Build, tmp_path: Path
) -> None:
    """MIXED BATCHES: the build is refused, never the row.

    One approved speaker whose recordings are real, one approved speaker with a
    single retired artifact among their clips, in one command. Dropping the bad
    row would be the worst outcome available: a dataset that is human-only,
    trains, qualifies, and is one clip smaller than the manifest it names, with
    nothing in the stats saying which clip or why.

    Asserted in both orders, because those are different paths through
    ``load_sources`` -- in one of them there are already verified samples in hand
    when the refusal happens -- and ``Build.refuse`` additionally asserts that not
    one window was emitted before it.
    """
    good = write_speaker(tmp_path / "clean_speaker", speaker=TRAIN, split="train")
    bad = write_speaker(tmp_path / "one_bad_clip", speaker=OTHER_TRAIN, split="train")
    digest = relocate(bad, clip=INNOCENT_CLIP, artifact=SYNTHETIC_FIXTURE)
    assert_no_marker_fires(bad)

    for order in ([good, bad], [bad, good]):
        message = build.refuse(
            "retired synthetic artifact",
            source=[f"human={manifest}" for manifest in order],
        )
        assert digest in message, message

    # Non-vacuity, and the sharp end of it: with that one clip replaced by a real
    # recording the very same pair of manifests builds and both speakers are in
    # the dataset. So the refusal above cost a legitimate speaker their entire
    # build, which is the intended price.
    also_good = write_speaker(
        tmp_path / "clean_speaker_two", speaker=OTHER_TRAIN, split="train"
    )
    _, stats = build.run(source=[f"human={good}", f"human={also_good}"])
    assert sorted(stats["samples_by_speaker"]) == sorted([TRAIN, OTHER_TRAIN])
    assert stats["samples_by_category"][POSITIVE] == 4
    assert stats["synthetic_samples"] == 0


# ── the name that used to skip the content check ─────────────────────────────


def test_a_retired_artifact_delivered_as_generated_background_is_still_hashed(
    build: Build, tmp_path: Path
) -> None:
    """An exclusion decided by name was a way to skip the check by name.

    Two of Speech Commands' six background files are synthesised noise, so
    ``pink_noise.wav`` and ``white_noise.wav`` are excluded from the background
    pool. That exclusion used to be decided *before* the file was resolved and
    hashed, which turned the filename into a bypass: a retired artifact delivered
    under that name was dropped, counted in ``generated_excluded``, and the build
    then reported ``synthetic_samples: 0`` -- true of the windows it emitted, and
    silent about the retired material sitting in the corpus it was built from.

    A dropped row is not a refusal. The corpus is compromised either way and the
    operator has to be told, so the file is verified first and excluded second.
    """
    corpus_root = tmp_path / "rooms"
    write_corpus(
        corpus_root, names=("doing_the_dishes.wav", "pink_noise.wav", "running_tap.wav")
    )
    shutil.copyfile(SYNTHETIC_FIXTURE, corpus_root / "clips" / "pink_noise.wav")
    manifest = corpus_root / "relocated.manifest.json"
    freeze_manifest.freeze(
        corpus_root / "clips",
        manifest,
        dataset="fixture-corpus",
        split="train",
        usage="training",
        note="hermetic fixture; a retired artifact wearing the generated-noise name",
    )

    # Non-vacuity: the name really is one the loader excludes, so this is about
    # the bypass and not about an ordinary corpus file being caught.
    assert "pink_noise.wav" in round8.GENERATED_BACKGROUND_NAMES

    message = build.refuse(
        "retired synthetic artifact",
        source=[f"human={build.human}", f"recorded_background={manifest}"],
    )
    assert "pink_noise.wav" in message.replace("\\", "/"), message

    # ...and the exclusion still happens, for files that are what they say they
    # are: verified, then named, counted and left out of the pool.
    clean = write_corpus(
        tmp_path / "clean_rooms",
        names=("doing_the_dishes.wav", "pink_noise.wav", "running_tap.wav"),
    )
    _, stats = build.run(
        source=[f"human={build.human}", f"recorded_background={clean}"]
    )
    assert stats["sources"][1]["generated_excluded"] == ["pink_noise.wav"]
    assert stats["sources"][1]["files_used"] == 2
    assert stats["synthetic_samples"] == 0


# ── provenance that is not forged, and points at the retired tree ────────────


def test_a_manifest_that_names_the_retired_tree_as_its_capture_root_is_refused(
    build: Build, tmp_path: Path
) -> None:
    """FORGED PROVENANCE with nothing forged: the originals really are in data/tts.

    Every other provenance attack has to lie about something. This one does not.
    ``source_dir`` is where a manifest says the capture session lives; it is the
    only input path this stage is handed *inside* a manifest, and it was the one
    path nothing judged. So a manifest could name a directory under ``data/tts``,
    have its originals hashed out of the retired tree, and make the build report
    ``originals_verified: 3`` -- a positive verification claim, correct in every
    digit, about synthesized speech, in the field that exists to establish that a
    person was recorded at all.

    The clips here are honest recordings and every digest agrees. The only thing
    wrong is where the recordings they are attributed to live.
    """
    root = tmp_path / "derived_capture"
    path = write_speaker(root, speaker=TRAIN, split="train")

    retired = tmp_path / "wakeword_training" / "data" / "tts" / "positive_close"
    retired.mkdir(parents=True)
    for entry in json.loads(path.read_text(encoding="utf-8"))["files"]:
        shutil.copyfile(
            root / "originals" / entry["source_file"], retired / entry["source_file"]
        )

    def rehome(body: dict) -> None:
        body["source_dir"] = str(retired)

    refreeze(path, rehome)

    # Non-vacuity: nothing *inside* the manifest is refusable. Every reference is
    # relative, marker-free and ``..``-free; each original is exactly the bytes
    # the manifest records; and none of them is in the registry.
    assert_no_marker_fires(path)
    for entry in json.loads(path.read_text(encoding="utf-8"))["files"]:
        original = retired / entry["source_file"]
        assert _sha256(original) == entry["source_sha256"]
        assert round8.retired_artifact(entry["source_sha256"]) is None

    message = build.refuse("source_dir", source=[f"human={path}"])
    assert "data/tts" in message, message

    # Non-vacuity: the same manifest with its capture root where a capture root
    # belongs builds, and reports the same three originals as verified.
    def home(body: dict) -> None:
        body["source_dir"] = str(root / "originals")

    refreeze(path, home)
    _, stats = build.run(source=[f"human={path}"])
    assert stats["sources"][0]["originals_verified"] == 3


# ── the corpus manifest: its root, and its own digest ────────────────────────


def test_a_corpus_root_that_leaves_the_manifests_own_directory_is_refused(
    build: Build, tmp_path: Path
) -> None:
    """PATH TRAVERSAL, one level above every reference that was being checked.

    Each corpus file is checked for containment -- inside the root. The root came
    from ``root_name``, joined to the manifest's own directory without being
    judged at all, so ``root_name: "../../data/tts/positive_close"`` moved the
    whole containment frame into the retired tree and every per-file check then
    passed against it, on a manifest whose digests were all correct.

    ``freeze_manifest`` records ``root.resolve().name`` -- a basename, one
    component -- which is what makes the rule a rule rather than a guess. The
    Windows-style separator is in the list because the platform the build runs on
    must not decide what the guard sees.
    """
    corpus_root = tmp_path / "rooms"
    manifest = write_corpus(corpus_root)
    body = json.loads(manifest.read_text(encoding="utf-8"))
    # Non-vacuity: this is a real, tool-produced, internally consistent freeze and
    # the basename is what the tool wrote.
    assert body["root_name"] == "clips"

    elsewhere = tmp_path / "inbox" / "corpus.manifest.json"
    elsewhere.parent.mkdir(parents=True)
    for escape in ("../rooms/clips", "..", "/etc", "clips/../clips", "sub/clips", "sub\\clips"):
        body["root_name"] = escape
        # Re-signed over the edit, so the manifest's own digest cannot be what
        # refuses it: this test is about the root and nothing else.
        body["manifest_sha256"] = freeze_manifest.manifest_digest(body)
        elsewhere.write_text(json.dumps(body, indent=2), encoding="utf-8")
        build.refuse(
            "root_name",
            source=[f"human={build.human}", f"speech_commands={elsewhere}"],
        )

    # Non-vacuity: the manifest beside its own data, with the basename the tool
    # writes, builds and contributes its three recordings.
    _, stats = build.run(
        source=[f"human={build.human}", f"speech_commands={manifest}"]
    )
    assert stats["sources"][1]["files_used"] == 3


def test_an_edited_corpus_manifest_is_refused_by_its_own_recorded_digest(
    build: Build, tmp_path: Path
) -> None:
    """ALTERED MANIFESTS, on the half that had no integrity check at all.

    A human manifest is frozen by a ``MANIFEST.json.sha256`` sidecar and the
    builder checks it (``test_an_unfrozen_or_edited_manifest_is_refused``). A
    corpus manifest has no sidecar; what it has is the ``manifest_sha256``
    ``freeze_manifest`` computes over its own canonical body -- and that field was
    never read here. So the sealed-evaluation gate, which is the whole point of
    the usage field, was defeated by editing one word in the file it reads: a
    sealed corpus became a training corpus while every per-file digest in the
    manifest stayed correct, which is exactly what makes the edit invisible.

    The honest limit of the fix is asserted at the end: a recorded digest is a
    self-consistency anchor, not a signature. Recomputing it over the edited body
    is a *fresh* freeze and this stage cannot tell that from an honest one.
    ``freeze_manifest`` does also write a ``<manifest>.sha256`` sidecar over the
    file's own bytes -- the same artifact ``_require_frozen`` demands of a human
    manifest -- so requiring it here is the next step available. That is a change
    to what an operator has to carry beside a corpus, which is not a decision this
    stage makes on its own.
    """
    sealed = write_corpus(tmp_path / "sealed_corpus", usage="sealed-evaluation")

    # Non-vacuity: unedited, this corpus is refused for exactly the right reason,
    # so the usage gate works and the edit below is what defeats it.
    build.refuse(
        "sealed evaluation set",
        source=[f"human={build.human}", f"speech_commands={sealed}"],
    )

    body = json.loads(sealed.read_text(encoding="utf-8"))
    recorded = body["manifest_sha256"]
    body["usage"] = "training"
    sealed.write_text(json.dumps(body, indent=2), encoding="utf-8")

    # The edit is invisible everywhere else: the file list is untouched and every
    # digest in it still describes the bytes on disk.
    for entry in body["files"]:
        on_disk = tmp_path / "sealed_corpus" / "clips" / entry["path"]
        assert _sha256(on_disk) == entry["sha256"]

    message = build.refuse(
        "manifest_sha256",
        source=[f"human={build.human}", f"speech_commands={sealed}"],
    )
    assert recorded in message, message

    # The documented residual: re-signed, the edit is a new freeze and this stage
    # reads it as one. The corpus is still verified file by file, and the usage
    # claim is worth exactly as much as the freeze nobody rewrote.
    body["manifest_sha256"] = freeze_manifest.manifest_digest(body)
    sealed.write_text(json.dumps(body, indent=2), encoding="utf-8")
    _, stats = build.run(
        source=[f"human={build.human}", f"speech_commands={sealed}"]
    )
    assert stats["sources"][1]["usage"] == "training"


# ── the root of the tree, and the checkpoint ─────────────────────────────────


@pytest.mark.skipif(not _can_symlink(), reason="symlinks need elevated privileges")
def test_a_derivation_reached_through_a_symlinked_root_is_refused(
    build: Build, tmp_path: Path
) -> None:
    """SYMLINKS, in the shape the containment walk structurally cannot see.

    ``test_a_clip_reached_through_a_symlink_is_refused`` covers a linked clip and
    a linked category directory; the walk catches both because it walks from the
    reference up to the root. This links the *root*. The walk stops there, every
    reference resolves to something inside it, and the marker rules were reading
    the path somebody typed rather than the path it leads to -- so
    ``--source human=inbox/E00x/MANIFEST.json`` built a whole speaker out of
    ``data/tts`` with every string in the command and in the manifest reading
    clean. The same blind spot applied on the way out, to ``--out``.
    """
    retired = tmp_path / "wakeword_training" / "data" / "tts" / "speaker"
    retired.parent.mkdir(parents=True)
    manifest = write_speaker(retired, speaker=TRAIN, split="train")

    # An innocent, absent capture root, so the ``source_dir`` rule cannot be what
    # fires and this test is about the resolved manifest path alone. The offline
    # layer verifies the originals through the full-length decodes, which sit
    # inside the linked tree and hash correctly.
    def innocent_capture_root(body: dict) -> None:
        body["source_dir"] = str(tmp_path / "capture_session")

    refreeze(manifest, innocent_capture_root)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    link = inbox / "E00x"
    link.symlink_to(retired, target_is_directory=True)
    through = link / manifest.name

    # Non-vacuity: the reference the operator gives and every reference inside the
    # manifest are marker-free, the containment guard is *run* and admits them,
    # the declared capture root is clean, and none of the bytes is a registered
    # artifact.
    assert round8.synthetic_tree_marker(
        json.loads(through.read_text(encoding="utf-8"))["source_dir"]
    ) is None
    assert round8.synthetic_tree_marker(through) is None
    assert round8.synthetic_tree_marker(link) is None
    assert round8.resolve_within(INNOCENT_CLIP, link, "clip").is_file()
    assert round8.retired_artifact(_sha256(retired / INNOCENT_CLIP)) is None
    assert_no_marker_fires(through)

    message = build.refuse("the resolved human manifest", source=[f"human={through}"])
    assert "data/tts" in message, message

    # Non-vacuity: the same derivation named directly, outside the retired tree,
    # builds.
    direct = write_speaker(tmp_path / "derived_direct", speaker=TRAIN, split="train")
    sink, _ = build.run(source=[f"human={direct}"])
    assert sink.labels

    # And the way out: a Round 8 dataset written through a link into a previous
    # round's tensor directory would be indistinguishable from the synthetic one
    # beside it.
    tensors = tmp_path / "wakeword_training" / "data" / "features_r7"
    tensors.mkdir(parents=True)
    out_link = inbox / "round8_out"
    out_link.symlink_to(tensors, target_is_directory=True)
    assert round8.synthetic_tree_marker(out_link) is None
    build.refuse("the resolved --out", source=[f"human={direct}"], out=out_link)


def test_a_rejected_candidate_checkpoint_is_refused_as_an_initialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A REJECTED CANDIDATE as a starting point, and the shipped model pinned.

    ``test_a_relocated_synthetic_checkpoint_is_refused_as_an_initialization``
    attacks with the export this repository ships, because those bytes are tracked
    here. The 11 rejected candidate checkpoints are not tracked -- they are on the
    training host, which is the whole reason they are addressed by content -- so
    that attack is staged the only honest way available: a real file, a real
    directory layout, a real contract beside it, and the *digest of the bytes*
    simulated to be the registered one.

    What that establishes is what needs establishing: the guard consults the
    registry as a whole rather than the two entries whose bytes CI can see, it
    names the artifact and its kind when it refuses, and it decides on the bytes
    before it reads the paperwork somebody laid out beside them.
    """
    registry = round8.load_retired_artifacts()

    # The shipped detector, pinned as a literal. Two independent records of these
    # bytes already exist -- the registry and the model card -- and this is the
    # third, in the test that attacks it, so a re-export cannot quietly become the
    # thing the initialization guard is checked against.
    shipped = "59d35d33703e105739daaa0284eef4629ff2aeaa973d6a3d4fe8183cf1699d79"
    assert _sha256(SHIPPED_EXPORT) == shipped
    assert registry[shipped]["kind"] == "shipped_synthetic_era_export"
    assert registry[shipped]["name"] == "tools/wakewords/hey_youtab.onnx"

    rejected = sorted(
        (digest, entry["name"])
        for digest, entry in registry.items()
        if entry["kind"] == "rejected_checkpoint"
    )
    # Non-vacuity: there is a group of them to attack with, and it is the size the
    # registry says it is.
    assert len(rejected) == 11, len(rejected)
    digest, name = rejected[0]
    assert not (REPO_ROOT / name).exists(), (
        "this attack simulates the digest precisely because the retired training "
        "tree is not in this repository -- if it were here, hash it instead"
    )

    init = tmp_path / "round8" / "round8_pretrained_init.pt"
    init.parent.mkdir(parents=True)
    init.write_bytes(b"whatever a resumed candidate's weights happen to be")
    contract = init.parent / round8.CONTRACT_FILENAME
    contract.write_text(
        json.dumps({"round": 8, "human_only": True, "synthetic_samples": 0}),
        encoding="utf-8",
    )

    # Non-vacuity: this exact file, in this exact directory, with this exact
    # contract, is accepted. The only thing that changes below is the digest.
    assert round8.synthetic_tree_marker(init) is None
    round8.refuse_synthetic_initialization(init)

    whole = build_dataset.sha256_file
    monkeypatch.setattr(
        build_dataset,
        "sha256_file",
        lambda path: digest if Path(path) == init else whole(path),
    )
    with pytest.raises(round8.Refused, match="retired synthetic artifact") as raised:
        round8.refuse_synthetic_initialization(init)
    assert digest in str(raised.value)
    assert name in str(raised.value)
    assert "rejected_checkpoint" in str(raised.value)

    # The content check runs before the contract is read, so the paperwork is not
    # what an operator has to get wrong for this to fire.
    contract.unlink()
    with pytest.raises(round8.Refused, match="retired synthetic artifact"):
        round8.refuse_synthetic_initialization(init)


def test_an_unregistered_relocation_is_refused_by_provenance_not_by_the_registry(
    build: Build, tmp_path: Path
) -> None:
    """MISSING REGISTRY ENTRIES: 90 of 207,300 clips is 0.043%, so this matters.

    The registry is an enumeration for what can be enumerated -- 11 rejected
    checkpoints, 22 exports, 22 frame-score tensors, 22 feature tensors, 24
    committed fixtures, both shipped backends -- and a *sample* of the TTS
    corpora. A clip that is not one of the 90 is not recognised by content, and
    nothing in this file may imply otherwise, so the limit is measured here rather
    than left to a reader's optimism: bytes the registry has never been shown do
    not stop a build.

    What stops one is the provenance chain, and the demonstration is the same
    relocation with one link of it missing. The other links have their own tests in
    the Round 8 suite -- ``test_a_sample_without_provenance_is_refused``,
    ``test_an_unfrozen_or_edited_manifest_is_refused``,
    ``test_an_original_with_no_verifiable_layer_at_all_is_refused`` and
    ``test_every_original_is_hash_verified_against_the_manifest`` -- and are cited
    rather than repeated.
    """
    path = write_speaker(tmp_path / "derived_unlisted", speaker=TRAIN, split="train")
    digest = substitute(path, clip=INNOCENT_CLIP)

    # The limit, measured: these bytes are not in the registry and the build
    # proceeds. Content addressing is not what protects this row.
    assert round8.retired_artifact(digest) is None
    _, stats = build.run(source=[f"human={path}"])
    assert stats["synthetic_samples"] == 0
    assert stats["samples_by_category"][POSITIVE] == 2

    # This is what protects it: the same relocation with the ``files`` entry that
    # corroborates the row removed. Nothing else changes -- the clip still hashes
    # to what the manifest records and the manifest is still frozen over it.
    orphaned = write_speaker(tmp_path / "derived_orphaned", speaker=TRAIN, split="train")
    substitute(orphaned, clip=INNOCENT_CLIP)

    def drop_the_corroboration(body: dict) -> None:
        row = next(r for r in body["clips"] if r["clip"] == INNOCENT_CLIP)
        body["files"] = [
            entry for entry in body["files"]
            if entry["source_file"] != row["source_file"]
        ]
        body["clips"] = [
            other for other in body["clips"]
            if other["clip"] == INNOCENT_CLIP
            or other["source_file"] != row["source_file"]
        ]

    refreeze(orphaned, drop_the_corroboration)
    build.refuse("does not contain", source=[f"human={orphaned}"])
