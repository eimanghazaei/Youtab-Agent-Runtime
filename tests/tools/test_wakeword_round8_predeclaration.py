"""Round 8 is predeclared, and the predeclaration cannot rot into a document.

Background
----------
Rounds 6 and 7 were written before their data existed, and that is the only
reason their negative results are trustworthy: every decision that could have
been influenced by seeing a result was fixed in advance. Round 8 gets the same
treatment, with one difference — the design is also machine-readable
(``scripts/wakeword/round8_config.json``), so the parts of it that are
arithmetic can be *checked* rather than reviewed.

This file does three jobs, and they are different jobs:

1. It runs ``round8_config.check`` over the committed config, so a config that
   drifted out of internal consistency fails the build.
2. It proves ``check`` has teeth, by mutating a loaded copy in each of the ways
   a hurried edit would and asserting that each mutation is caught. Without
   this, a refactor that broke every check would leave job 1 quietly green.
3. It pins the things that are *policy* rather than arithmetic — the immutable
   splits, the five unchanged targets, fresh initialization, the
   5/5-before-sealed gate, the ban on returning to synthetic data — against both
   the config and the design document, because those are the parts a later
   round would be tempted to soften.

The hash fields are asserted **empty**. Round 8 has not run; the recordings it
needs do not exist. A filled hash in this file's state would mean either that
somebody invented one, or that the round executed and nobody updated the status.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WAKEWORD = REPO / "scripts" / "wakeword"
CONFIG_PATH = WAKEWORD / "round8_config.json"
DESIGN_PATH = WAKEWORD / "ROUND8_DESIGN.md"

sys.path.insert(0, str(WAKEWORD))

import round8_config as r8  # noqa: E402


@pytest.fixture(scope="module")
def config() -> dict:
    return r8.load(CONFIG_PATH)


# ── the committed config ─────────────────────────────────────────────────────


def test_the_committed_config_is_complete_and_self_consistent(config: dict) -> None:
    problems = r8.check(config)
    assert not problems, "\n  ".join(["round8_config.json has problems:", *problems])


def test_the_config_is_valid_json_and_stable_to_reserialize() -> None:
    """A config that is going to be hashed has to be a file, not a formatting.

    ``freeze_manifest`` hashes canonical JSON, so the config being re-serializable
    without loss is what makes "the config we ran" a checkable claim later.
    """
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert json.loads(json.dumps(parsed)) == parsed


def test_every_hash_field_is_empty_because_round_8_has_not_run(config: dict) -> None:
    assert r8._hash_state(config) == "empty"
    assert config["status"] == "predeclared-not-executed"
    for entry in config["manifests"]:
        assert entry["manifest_sha256"] == "", entry["dataset"]
        assert "hash_state" in entry, entry["dataset"]
    assert config["initialization"]["epoch0_checkpoint_sha256"] == ""
    # And the config says so in words as well as in structure, because the
    # person who fills these in reads the note, not the schema.
    assert "filled and frozen before execution" in config["note"]


def test_the_cli_check_exits_zero_on_the_committed_config() -> None:
    result = subprocess.run(
        [sys.executable, str(WAKEWORD / "round8_config.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout


# ── the arithmetic ───────────────────────────────────────────────────────────


def test_the_parameter_formula_reproduces_the_recorded_candidate() -> None:
    """192,961 is a measured number from r1-r7, not a derivation.

    If the formula does not reproduce it, every parameter count in the design is
    a guess, including the ones that justify the capacity axis.
    """
    assert r8.parameter_count([128, 128, 64]) == 192961
    assert r8.parameter_count([192, 192, 96]) == 369249
    assert r8.parameter_count([256, 256, 128]) == 598785
    assert r8.parameter_count([384, 384, 192]) == 1217601


def test_the_arms_go_down_and_span_two_orders_of_magnitude(config: dict) -> None:
    arms = config["variation"]["arms"]
    counts = [arm["parameters"] for arm in arms]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 192961, "the only measured architecture is not the control"
    assert counts[0] / counts[-1] > 30, "the capacity set is too narrow to measure a slope"
    assert len(arms) == 3


# ── exactly three arms, and the bound is exact at both ends ──────────────────


def _with_arms(config: dict, widths: list[list[int]]) -> dict:
    """A copy of the config whose capacity set is ``widths``, otherwise valid.

    Every other field the arms imply is kept consistent — parameter counts are
    recomputed, ``runs`` matches, the order stays downward — so a refusal below is
    the arm *count* and nothing else. A four-arm config that also miscounted its
    parameters would be refused for the wrong reason and would prove nothing
    about the bound.
    """
    edited = copy.deepcopy(config)
    edited["variation"]["arms"] = [
        {
            "id": f"r8{chr(ord('a') + index)}",
            "channels": width,
            "parameters": r8.parameter_count(width),
        }
        for index, width in enumerate(widths)
    ]
    edited["variation"]["runs"] = len(widths)
    return edited


def test_the_committed_config_declares_exactly_the_three_predeclared_widths(
    config: dict,
) -> None:
    declared = [tuple(arm["channels"]) for arm in config["variation"]["arms"]]
    assert tuple(declared) == r8.ARMS == ((128, 128, 64), (32, 32, 16), (8, 8, 4))
    assert config["variation"]["runs"] == 3
    assert config["variation"]["arm_count_is_exact"] is True


def test_a_fourth_capacity_arm_is_refused(config: dict) -> None:
    """The edit the Owner decision names: a fourth width added before a run.

    It used to validate, because the validator accepted "2 to 4 arms" while the
    design said "do not add a fourth width". A config edit is not an Owner
    decision, so the count is the bound and the refusal has to say which decision
    it is enforcing.
    """
    four = _with_arms(config, [[128, 128, 64], [32, 32, 16], [8, 8, 4], [4, 4, 2]])
    problems = r8.check(four)

    assert problems, "a four-arm config validated"
    named = [p for p in problems if r8.STOP_CONDITION in p]
    assert named, (
        "the refusal does not quote the stop condition it enforces; a bare count "
        f"mismatch reads as a typo to fix: {problems}"
    )
    assert "exactly 3" in named[0], named[0]
    assert "Owner decision" in named[0], named[0]


def test_a_two_arm_config_is_refused_too(config: dict) -> None:
    """The bound is exact, not an upper limit.

    The narrowest arm is dropped rather than the control, so the (128, 128, 64)
    baseline is still present and the refusal cannot be the missing control. Two
    arms would still produce a "capacity–accuracy relation" — two points and a
    line through them — which is the failure mode a lower bound prevents.
    """
    two = _with_arms(config, [[128, 128, 64], [32, 32, 16]])
    problems = r8.check(two)

    assert problems, "a two-arm config validated"
    assert not [p for p in problems if "192,961" in p], (
        f"the control is still present, so this is not why it was refused: {problems}"
    )
    named = [p for p in problems if r8.STOP_CONDITION in p]
    assert named, f"the refusal does not quote the stop condition: {problems}"
    assert "has 2 arms" in named[0], named[0]


def test_the_three_widths_themselves_are_pinned_not_only_their_number(
    config: dict,
) -> None:
    """Three arms of the wrong widths is a different experiment.

    Swapping (32, 32, 16) for (64, 64, 32) keeps the count, the order and the
    control, and halves the range the slope is measured over. The widths are part
    of the predeclaration, so they are checked as well as counted.
    """
    swapped = _with_arms(config, [[128, 128, 64], [64, 64, 32], [8, 8, 4]])
    problems = r8.check(swapped)

    assert problems, "an arm width was changed and the config still validated"
    assert [p for p in problems if "bounds which widths run" in p], problems


def test_the_stop_condition_the_refusals_quote_is_in_the_config_and_the_document(
    config: dict,
) -> None:
    """A validator that quoted a clause nothing carried would be citing itself."""
    assert r8.STOP_CONDITION in config["variation"]["stop_condition"].lower()
    assert r8.STOP_CONDITION in DESIGN_PATH.read_text(encoding="utf-8").lower()
    # And the document states the bound as exact, in the words the code enforces.
    assert "Exactly three arms" in DESIGN_PATH.read_text(encoding="utf-8")


def test_the_clean_run_bound_agrees_with_the_rule_of_three() -> None:
    """The rule of three is the approximation the recording package reasons in.

    It has to agree with the exact bound to within a few percent, or the
    package's arithmetic and this config's arithmetic are two different claims.
    """
    for n in (36, 56, 60, 72, 112, 144, 150):
        exact = r8.clean_run_upper_bound(n)
        approximate = 3.0 / n
        assert exact < approximate, n
        assert abs(exact - approximate) / approximate < 0.05, n
    assert r8.clean_run_upper_bound(60) == pytest.approx(0.0487, abs=5e-4)
    assert r8.clean_run_upper_bound(150) == pytest.approx(0.0198, abs=5e-4)


def test_fifteen_hours_is_what_the_point_two_per_hour_target_needs() -> None:
    """The number that decides whether target 2 is measurable at all."""
    assert r8.hours_for_poisson_bound(0.2) == pytest.approx(14.98, abs=0.01)
    # And what the corpora on hand actually bound, which is the finding.
    assert 3.0 / 11.659 > 0.2, "the eval partition would have demonstrated 0.2/h"
    assert r8.hours_for_poisson_bound(0.2) > 7.926, "validation hours would suffice"


def test_the_loss_weight_rule_is_arithmetic_with_no_free_choice(config: dict) -> None:
    anchor = config["loss"]["r6c1_anchor"]
    share = r8.positive_loss_mass_share(
        anchor["positive_windows"],
        anchor["hard_negative_windows"],
        anchor["other_negative_windows"],
        negative_weight=anchor["negative_weight"],
        hard_negative_weight=anchor["hard_negative_weight"],
    )
    assert share == pytest.approx(anchor["positive_loss_mass_share"], abs=5e-5)

    train = config["dataset_projection"]["train"]
    positives = train["positive_human"]["windows"]
    hard = train["near_phrase_human"]["windows"]
    other = train["total_windows"] - positives - hard
    weight = r8.derived_negative_weight(positives, hard, other, anchor_share=share)
    assert weight == pytest.approx(config["loss"]["negative_weight"], abs=5e-4)
    # The rule is a fixed point: applying the derived weights must return the
    # anchor share. That is what makes it a derivation rather than a fitted
    # number.
    assert r8.positive_loss_mass_share(
        positives, hard, other, negative_weight=weight, hard_negative_weight=2 * weight
    ) == pytest.approx(share, abs=1e-6)


def test_the_offset_grid_is_derived_from_the_pipelines_own_constants() -> None:
    """Seven offsets is a consequence, not a choice.

    If either ``PHRASE_END_JITTER`` or the runtime's frame length ever moves,
    this fails rather than letting the config keep a stale grid that nothing
    points at any more.
    """
    grid = r8.offset_grid_from_pipeline()
    assert grid == [2, 3, 4, 5, 6, 7, 8]
    assert len(grid) == 7


# ── V6: the predeclaration is reconciled with the builder that will run ───────


def test_the_predeclared_windowing_matches_the_real_round8_builder() -> None:
    """V6 raised the builder to the predeclaration, so they now agree.

    ``check`` derives the offset grid from ``build_dataset.PHRASE_END_JITTER``
    (the retired synthetic builder) and the config predeclares 7 phrase-anchored
    offsets, projecting ``near_phrase_human`` as ``utterances x 7`` with
    ``loss.negative_weight`` 0.1423 derived from those counts. Round 8 is built
    by ``build_human_dataset``: V6 set its ``TRAILING_OFFSETS_S`` to the seven
    offsets that land on frames [2..8] and listed ``near_phrase_human`` in
    ``PHRASE_ANCHORED_NEGATIVES`` so the builder phrase-anchors it with that same
    ladder instead of tiling it. The predeclared numbers did not move — the
    builder rose to them — so the divergence is now empty.
    """
    problems = r8.builder_windowing_divergence()
    assert problems == [], f"builder still diverges from the predeclaration: {problems}"
    # The dedicated CLI check passes, and it is folded into --check too, so the
    # reconciliation can never silently drift again.
    assert r8.main(["--check-builder-windowing"]) == 0
    assert r8.main(["--check"]) == 0


def test_the_builder_windowing_check_is_not_vacuous() -> None:
    """A config that no longer matches the real builder is reported.

    Without this, a divergence function that returned ``[]`` unconditionally
    would 'agree' with the builder above while checking nothing. Pointing the
    predeclaration back at the retired four-offset ladder must diverge from the
    builder's real seven-offset one.
    """
    drifted = copy.deepcopy(r8.load())
    window = drifted["window_construction"]
    window["phrase_anchored_offsets_frames"] = [2, 4, 6, 8]
    window["offsets_per_phrase_anchored_utterance"] = 4
    for split in ("train", "validation"):
        entry = drifted["dataset_projection"][split]["near_phrase_human"]
        entry["windows"] = entry["utterances"] * 4
    problems = r8.builder_windowing_divergence(drifted)
    assert problems, "a config that disagrees with the real builder reported nothing"
    joined = " | ".join(problems)
    assert "TRAILING_OFFSETS_S" in joined
    assert "offsets_per_phrase_anchored_utterance" in joined


# ── policy that is not arithmetic ────────────────────────────────────────────


def test_the_splits_are_the_immutable_ones(config: dict) -> None:
    assert config["splits"]["train"] == ["E001", "E003", "E004"]
    # E002 was reassigned from sealed to validation by Owner decision; the sealed
    # final holdout is E006/E007 only.
    assert config["splits"]["validation"] == ["E005", "E002"]
    assert config["splits"]["sealed"] == ["E006", "E007"]
    members = (
        config["splits"]["train"] + config["splits"]["validation"] + config["splits"]["sealed"]
    )
    assert len(members) == len(set(members)), "a dataset is in two splits"


def test_the_e002_reassignment_is_recorded_as_an_amendment_and_a_consumption(
    config: dict,
) -> None:
    """The move stays immutable-with-a-record, not silently mutated.

    ``splits.immutable`` is still true, and the amendment names the Owner
    decision and its reason. The consumption record marks E002 permanently out of
    the sealed set, mirroring ``build_human_dataset.CONSUMED_FOR_VALIDATION``.
    """
    splits = config["splits"]
    assert splits["immutable"] is True
    amendment = splits["amendment"]
    assert amendment["authorized"] == "owner"
    assert "E002" in amendment["change"]
    assert amendment["version"] >= 2

    consumed = splits["consumed"]["E002"]
    assert consumed["for"] == "validation"
    assert consumed["no_longer_sealed_holdout"] is True
    assert consumed["authorized"] == "owner"
    # E002 is no longer named as a never-selected-on speaker: as validation it
    # is selected on.
    assert "E002" not in config["selection"]["never_selected_on"]


def test_validation_selects_and_nothing_else_does(config: dict) -> None:
    selection = config["selection"]
    assert selection["selected_on"] == "validation only"
    for sealed in config["splits"]["sealed"]:
        assert sealed in selection["never_selected_on"]
    allowed = config["splits"]["validation_may_be_used_for"]
    assert set(allowed) == {"threshold selection", "candidate selection", "epoch selection"}


def test_initialization_is_fresh_and_names_no_synthetic_ancestor(config: dict) -> None:
    init = config["initialization"]
    assert init["fresh"] is True
    assert init["from_synthetic_checkpoint"] is False
    assert init["seed"] == init["torch_manual_seed"] == init["numpy_default_rng"]
    assert init["seed"] != 20260807, "the synthetic era's seed is reused"


def test_the_five_targets_are_unchanged(config: dict) -> None:
    limits = {entry["id"]: entry["limit"] for entry in config["targets"]}
    assert limits == {1: 0.05, 2: 0.2, 3: 0.02, 4: 0, 5: 0}
    assert config["targets_unchanged_from"] == "ROUND6_DESIGN.md"


def test_both_artifacts_come_from_one_candidate_and_parity_is_absolute(config: dict) -> None:
    export = config["export_and_parity"]
    assert export["both_artifacts_from_one_frozen_candidate"] is True
    assert export["evaluated_independently_through_the_product_runtime"] is True
    assert export["required_detection_disagreements"] == 0
    assert export["detection_disagreement_is_fatal"] is True
    assert set(export["exported_by"]) == {
        "train_model.export_onnx",
        "train_model.export_tflite",
    }


def test_no_sealed_set_opens_before_five_of_five_on_validation(config: dict) -> None:
    gate = config["validation_gate"]
    assert "5/5" in gate["rule"]
    assert "BEFORE any sealed dataset is opened" in gate["rule"]
    assert "5/5" in config["sealed_opening_rule"]["precondition"]
    # And the honest half: three of the five cannot be demonstrated on E005, so
    # the gate has to say which ones it is only failing to contradict.
    forms = {entry["id"]: entry for entry in gate["per_target_e005_form"]}
    assert forms[2]["demonstrates_target"] is False
    assert forms[3]["demonstrates_target"] is False
    assert forms[4]["demonstrates_target"] is False
    assert "not contradicted" in gate["reporting_rule"]


def test_returning_to_synthetic_data_is_forbidden_not_discouraged(config: dict) -> None:
    failure = config["if_validation_fails"]
    assert "synthetic" in failure["step_3_forbidden"].lower()
    lowered = " ".join(str(v).lower() for v in failure.values())
    assert "one evidence-backed structural correction" in lowered
    for forbidden in ("re-tuning the threshold on any sealed set", "excluding difficult recordings"):
        assert forbidden in " | ".join(failure["also_forbidden"])


def test_the_prohibition_list_covers_the_owner_decision(config: dict) -> None:
    prohibited = " | ".join(config["policy"]["prohibited"]).lower()
    for needle in (
        "tts",
        "libritts",
        "vctk",
        "voice conversion",
        "pitch shift",
        "time stretch",
        "speed augmentation",
        "gain augmentation",
        "generated background noise",
        "generated impulse responses",
        "artificial reverberation",
        "any previous synthetic feature tensor",
    ):
        assert needle in prohibited, needle
    allowed = " | ".join(config["policy"]["allowed_processing"]).lower()
    assert "resample" in allowed and "mono" in allowed and "deterministic" in allowed


def test_recorded_corpora_are_negatives_only_and_carry_a_licence(config: dict) -> None:
    """Speech Commands is real human audio, so it is permitted — as negatives.

    The distinction is the whole point: a recorded corpus may be a negative and
    may never be a positive speaker, and each one has to carry licence, hash and
    source or the human-only claim is unverifiable.
    """
    corpora = config["recorded_negative_corpora"]
    keys = {entry["key"] for entry in corpora}
    assert keys == {"speech-commands", "common-voice"}
    for entry in corpora:
        assert "negatives only" in entry["role"]
        assert "never a positive speaker" in entry["role"]
        assert entry["licence"]
    speech_commands = next(e for e in corpora if e["key"] == "speech-commands")
    assert speech_commands["real_human_recordings"] is True
    assert len(speech_commands["archive_sha256"]) == 64, "the pinned corpus has no digest"
    # The one that is not fetched yet has empty hash fields, and says why.
    common_voice = next(e for e in corpora if e["key"] == "common-voice")
    assert common_voice["lock_sha256"] == ""
    assert common_voice["manifest_sha256"] == ""
    assert common_voice["blocked_on"]


def test_generated_noise_is_out_and_the_background_split_is_disjoint(config: dict) -> None:
    background = config["background_recordings"]
    excluded = set(background["generated_and_therefore_excluded"])
    assert excluded == {"pink_noise.wav", "white_noise.wav"}
    usable = set(background["real_and_usable"])
    assert not excluded & usable
    assigned: list[str] = []
    for names in background["assignment_disjoint_by_recording"].values():
        assigned.extend(names)
    assert sorted(assigned) == sorted(usable)
    assert len(assigned) == len(set(assigned))
    # The correction the config used to demand of the builder is done, so the
    # field that demanded it is gone. Leaving it would say the rule is pending
    # while the code already enforces it.
    assert "correction_required" not in background
    assert "VALIDATION_NOISE" in background["correction_applied"]


def test_the_builder_hands_no_generated_noise_to_any_split() -> None:
    """The rule lives in ``build_dataset.py``'s constants, so that is what is read.

    ``VALIDATION_NOISE`` named ``pink_noise.wav`` for seven rounds while every
    document said generated noise was prohibited, because no guard connected the
    two. This is that guard, and it reads the builder's source with ``ast`` for
    the same reason ``round8_config`` does — importing it would pull numpy in to
    check three tuples.
    """
    consts = r8._module_constants(
        r8.BUILD_DATASET,
        {"GENERATED_BACKGROUND_NAMES", "VALIDATION_NOISE", "EVAL_NOISE"},
    )
    generated = consts["GENERATED_BACKGROUND_NAMES"]
    assert generated == frozenset({"pink_noise.wav", "white_noise.wav"})
    for name in ("VALIDATION_NOISE", "EVAL_NOISE"):
        assert not set(consts[name]) & generated, f"build_dataset.{name} names generated noise"
    assert consts["VALIDATION_NOISE"] == ("doing_the_dishes.wav",)

    # Non-vacuity: the reader has to be able to see a generated name if one were
    # there, or "no generated name in the tuple" would be a statement about a
    # broken ast walk.
    assert set(consts["VALIDATION_NOISE"] + consts["EVAL_NOISE"]) == {
        "doing_the_dishes.wav",
        "running_tap.wav",
        "dude_miaowing.wav",
    }


def test_both_builders_name_the_same_generated_files() -> None:
    """One rule, one list.

    ``build_human_dataset.py`` is the active Round 8 builder and
    ``build_dataset.py`` is the retired one; a second, disagreeing definition of
    "generated" would mean the exclusion depended on which stage ran.

    Either shape passes: the same literal in both files, or the active builder
    taking the retired module's constant directly — it already imports that
    module for the window geometry. Insisting on the literal would fail the
    better of the two, so what is asserted is agreement, not duplication.
    """
    retired = r8._module_constants(r8.BUILD_DATASET, {"GENERATED_BACKGROUND_NAMES"})[
        "GENERATED_BACKGROUND_NAMES"
    ]
    human = WAKEWORD / "build_human_dataset.py"
    active = r8._module_constants(human, {"GENERATED_BACKGROUND_NAMES"}).get(
        "GENERATED_BACKGROUND_NAMES"
    )
    if active is None:
        source = human.read_text(encoding="utf-8")
        assert "GENERATED_BACKGROUND_NAMES = build_dataset.GENERATED_BACKGROUND_NAMES" in source, (
            "build_human_dataset defines GENERATED_BACKGROUND_NAMES as something "
            "this cannot read and does not take it from build_dataset either"
        )
    else:
        assert active == retired


def test_the_design_document_and_the_config_agree_on_the_headline_claims() -> None:
    """The document is the argument; the config is what runs. They must match."""
    text = DESIGN_PATH.read_text(encoding="utf-8")
    config = r8.load(CONFIG_PATH)
    assert "human-only" in text.lower()
    # The one varied axis, named as one.
    assert "exactly one axis" in text
    for arm in config["variation"]["arms"]:
        assert f"{arm['parameters']:,}" in text, arm["id"]
    # The stop condition, the gate, and the ban.
    assert "Stop condition" in text
    assert "before any sealed dataset is opened" in text.lower()
    assert "forbidden" in text.lower()
    # The two numbers that decide whether the targets are measurable at all.
    assert "14.98" in text
    assert "not demonstrable" in text.lower()


def test_no_hash_is_invented_in_advance() -> None:
    """The one digest either file may contain is the corpus that is already pinned.

    A predeclaration whose hashes were guessed would be worse than one with none:
    it would look frozen. So the whole-file check is "no 64-hex string anywhere
    except the Speech Commands archive digest, which was pinned long before this
    round and lives in ``assets.py``".
    """
    import re

    sys.path.insert(0, str(WAKEWORD))
    import assets  # noqa: PLC0415

    permitted = {asset.sha256 for asset in assets.ALL_ASSETS}
    digest = re.compile(r"\b[0-9a-f]{64}\b")
    for path in (DESIGN_PATH, CONFIG_PATH):
        found = set(digest.findall(path.read_text(encoding="utf-8")))
        invented = found - permitted
        assert not invented, f"{path.name} carries a digest nothing has produced: {invented}"
    # Non-vacuity: the pattern has to find the one digest that is legitimately
    # there, or "no invented hashes" would be a statement about a broken regex.
    assert digest.findall(CONFIG_PATH.read_text(encoding="utf-8"))


def test_the_design_document_carries_no_speaker_keyed_record() -> None:
    """The commit gate's own pattern, applied here so the failure is local.

    ``tests/tools/test_wakeword_no_human_data_committed.py`` would catch a
    speaker-keyed record or a capture-root marker anywhere in the repository,
    but it reports it as "some tracked file". These files are the ones most
    likely to acquire one, so they are checked where the fix is obvious.

    The forbidden markers are assembled from fragments, the same convention the
    gate's own control test uses, so this file does not become a tracked file
    containing them.
    """
    import re

    keyed = re.compile(r"\bspeaker_?(?:id)?[\"']?\s*[:=]\s*[\"']?E\d{3}\b", re.IGNORECASE)
    directory_marker = "speaker" + "_e0"
    for path in (DESIGN_PATH, CONFIG_PATH, WAKEWORD / "round8_config.py", Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert not keyed.search(text), f"{path.name} contains a speaker-keyed record"
        assert directory_marker not in text.replace("\\", "/").lower(), path.name

    # Non-vacuity: the two patterns have to match the things they forbid.
    assert keyed.search('{"speaker_id": "E00' + '2"}')
    assert directory_marker in ("datasets/" + directory_marker.upper() + "01/take.m4a").lower()
    assert not keyed.search("the package is handed to speaker E005 before the session")


# ── proof that the checks have teeth ─────────────────────────────────────────


def _mutate(config: dict, mutate) -> list[str]:
    broken = copy.deepcopy(config)
    mutate(broken)
    return r8.check(broken)


MUTATIONS = {
    "a target limit is loosened": lambda c: c["targets"].__setitem__(
        0, {**c["targets"][0], "limit": 0.10}
    ),
    "a sealed dataset is moved into training": lambda c: c["splits"].__setitem__(
        "train", ["E001", "E003", "E004", "E006"]
    ),
    "the validation speaker is changed": lambda c: c["splits"].__setitem__(
        "validation", ["E002"]
    ),
    "a parameter count is wrong": lambda c: c["variation"]["arms"][1].__setitem__(
        "parameters", 12345
    ),
    "the control architecture is dropped": lambda c: c["variation"].__setitem__(
        "arms", c["variation"]["arms"][1:]
    ),
    "the arms become an open sweep": lambda c: c["variation"].__setitem__(
        "arms",
        [{"id": f"x{i}", "channels": [i, i, i], "parameters": r8.parameter_count([i, i, i])}
         for i in range(120, 108, -1)],
    ),
    "a fourth width is added": lambda c: c["variation"].update(
        {
            "arms": [
                *c["variation"]["arms"],
                {"id": "r8d", "channels": [4, 4, 2], "parameters": r8.parameter_count([4, 4, 2])},
            ],
            "runs": 4,
        }
    ),
    "an arm is dropped": lambda c: c["variation"].update(
        {"arms": c["variation"]["arms"][:-1], "runs": 2}
    ),
    "the stop condition is emptied": lambda c: c["variation"].__setitem__("stop_condition", ""),
    "the stop condition stops forbidding a fourth width": lambda c: c["variation"].__setitem__(
        "stop_condition", "If no arm meets all five targets on E005 validation, stop."
    ),
    "initialization comes from a synthetic checkpoint": lambda c: c["initialization"].__setitem__(
        "from_synthetic_checkpoint", True
    ),
    "the synthetic era's seed is reused": lambda c: c["initialization"].update(
        {"seed": 20260807, "torch_manual_seed": 20260807, "numpy_default_rng": 20260807}
    ),
    "one hash is invented in advance": lambda c: c["manifests"][0].__setitem__(
        "manifest_sha256", "0" * 64
    ),
    "augmentation is switched back on": lambda c: c["window_construction"].__setitem__(
        "reverb_fraction", 0.5
    ),
    "the speech prefix is switched back on": lambda c: c["window_construction"].__setitem__(
        "positive_speech_prefix_fraction", 0.3
    ),
    "the offset grid drifts from the pipeline": lambda c: c["window_construction"].__setitem__(
        "phrase_anchored_offsets_frames", [4]
    ),
    "the window projection stops adding up": lambda c: c["dataset_projection"]["train"][
        "positive_human"
    ].__setitem__("windows", 9999),
    "the derived loss weight is hand-edited": lambda c: c["loss"].__setitem__(
        "negative_weight", 3.0
    ),
    "the hard-to-ordinary ratio is broken": lambda c: c["loss"].__setitem__(
        "hard_negative_weight", 1.0
    ),
    "epochs are scaled up to restore the step count": lambda c: c["schedule"].__setitem__(
        "epochs", 339
    ),
    "a parity disagreement becomes a tolerance": lambda c: c["export_and_parity"].__setitem__(
        "detection_disagreement_is_fatal", False
    ),
    "the parity denominator is inflated": lambda c: c["validation_gate"][
        "per_target_e005_form"
    ][4].__setitem__("n", 72000),
    "the sealed gate is softened": lambda c: c["validation_gate"].__setitem__(
        "rule", "open the sealed sets when the candidate looks promising"
    ),
    "generated noise is used as background": lambda c: c["background_recordings"][
        "real_and_usable"
    ].__setitem__("pink_noise.wav", 60.0),
    "the generated-noise exclusion list is trimmed": lambda c: c[
        "background_recordings"
    ].__setitem__("generated_and_therefore_excluded", ["pink_noise.wav"]),
    "a background recording is used by two splits": lambda c: c["background_recordings"][
        "assignment_disjoint_by_recording"
    ].__setitem__("train", ["exercise_bike.wav", "doing_the_dishes.wav"]),
    "a prohibition is dropped": lambda c: c["policy"].__setitem__(
        "prohibited", [p for p in c["policy"]["prohibited"] if "pitch" not in p.lower()]
    ),
    "pitch shift is smuggled into allowed processing": lambda c: c["policy"][
        "allowed_processing"
    ].append("small pitch shift"),
    "target 2 is declared demonstrable": lambda c: c["statistical_power"][
        "target_2"
    ].__setitem__("demonstrable", True),
    "a clean-run bound is overstated": lambda c: c["statistical_power"]["target_1"][
        "clean_run_bound_by_n"
    ].__setitem__("36", 0.05),
    "the target 3 shortfall is hidden": lambda c: c["statistical_power"]["target_3"].__setitem__(
        "shortfall_utterances", 0
    ),
    "the tie-break starts preferring bigger": lambda c: c["selection"][
        "candidate_tie_break"
    ].__setitem__("prefer", "more parameters"),
    "selection moves off validation": lambda c: c["selection"].__setitem__(
        "selected_on", "eval"
    ),
    "the validation margin is dropped": lambda c: c["selection"].__setitem__(
        "validation_margin", 1.0
    ),
    "the freeze list loses the artifacts": lambda c: c["sealed_opening_rule"].__setitem__(
        "freeze_before_open", ["the checkpoint"]
    ),
    "returning to synthetic data becomes allowed": lambda c: c["if_validation_fails"].__setitem__(
        "step_3_forbidden", "nothing in particular"
    ),
    "a prerequisite code change is dropped": lambda c: c.__setitem__(
        "prerequisite_code_changes", ["nothing to do"]
    ),
    "a required section disappears": lambda c: c.pop("statistical_power"),
    "the human-only policy is relaxed": lambda c: c["policy"].__setitem__(
        "training_data", "mostly human"
    ),
}


@pytest.mark.parametrize("description", sorted(MUTATIONS))
def test_check_catches_the_edit(config: dict, description: str) -> None:
    problems = _mutate(config, MUTATIONS[description])
    assert problems, f"check() did not notice: {description}"


def test_the_mutation_battery_is_not_vacuous(config: dict) -> None:
    """A no-op mutation must produce no problems.

    Without this, a ``check`` that returned a problem unconditionally would pass
    every case above while checking nothing.
    """
    assert not _mutate(config, lambda c: None)
    assert len(MUTATIONS) >= 30, f"only {len(MUTATIONS)} mutations"
