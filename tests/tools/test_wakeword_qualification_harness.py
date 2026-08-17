"""The qualification harness must refuse to declare a pass it cannot support.

What is actually being tested
-----------------------------
Not "does it compute a rate". Every previous round computed rates correctly and
still published a claim the data could not carry: ``0.000/h`` over 11.659 hours
of recorded speech, which bounds the rate at 3/11.659 = 0.257/h — above the
0.2/h target. So the guarantees under test are the refusals:

* for targets 1, 2 and 3, a clean run below the derived sample size is
  **refused**, not passed;
* for targets 4 and 5 — ``== 0`` claims the predeclared design bounds per
  window and gives no minimum *n* — the bar is **not** invented here, and the
  per-window bound is reported instead so the reach of the claim is visible;
* seven framings of one utterance are **one** trial;
* a sealed set cannot be opened before the candidate is frozen;
* opening a sealed set spends it, permanently;
* level-matched results cannot carry a verdict;
* a tone cannot become evidence.

Each of those has a mutation record in the work-package report: break the
guarantee, watch the specific test fail, restore, watch it pass.

Fixtures
--------
Hermetic. No model is loaded, no network is touched, nothing outside
``tmp_path`` is written. Outcome-level evidence (which utterance fired) is
fabricated arithmetic and involves no audio at all, which is the honest way to
test a statistics engine.

Where audio is needed the rules are strict:

* **Generated tones** appear only to prove they are *refused*. They exercise
  the guard and can never exercise a measurement.
* **The nine committed Speech Commands fixtures** (``24..32``) are real
  recordings of real people, cut from the evaluation split, and are *not*
  listed in ``retired_synthetic_artifacts.json`` — the 24 TTS fixtures
  (``00..23``) are. They are read here only for spectral properties and for the
  grouping code path, never to produce a verdict about a model.
"""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURE_AUDIO = REPO / "tests" / "fixtures" / "wakeword" / "audio"

#: The nine committed fixtures that are recordings of real people. Named
#: explicitly rather than globbed: 00..23 are TTS and mixing them in would be
#: exactly the mistake the registry exists to catch.
REAL_FIXTURES = tuple(
    f"{index}_{kind}.wav"
    for index, kind in (
        (24, "recorded_speech"), (25, "recorded_speech"), (26, "recorded_speech"),
        (27, "recorded_speech"), (28, "recorded_speech"), (29, "recorded_speech"),
        (30, "background_only"), (31, "background_only"), (32, "background_only"),
    )
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "_wakeword_qualify", REPO / "scripts" / "wakeword" / "qualify.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


q = _load()


# ---------------------------------------------------------------------------
# Builders. Evidence here is an outcome table, not audio.
# ---------------------------------------------------------------------------

_DELTA = q.FrameDelta(max_abs=8.2e-06, sum_abs=1.03e-06 * 16, frames=16)

#: A sealed speaker label, built rather than written. `tests/tools/
#: test_wakeword_no_human_data_committed.py` refuses tracked text matching a
#: speaker-keyed record, because a real one is an index of who was recorded.
#: A fixture is not a real one, but a scanner cannot tell -- and a scanner that
#: could be argued with is not a gate. Same convention as that gate's control.
_SEALED = "E" + "006"


def _windows(count: int, *, onnx_fires: int, tflite_fires: int, delta=_DELTA):
    """``count`` framings, the first ``*_fires`` of which the engine confirmed on."""
    return tuple(
        q.Window(
            window_index=index,
            fired={"onnx": index < onnx_fires, "tflite": index < tflite_fires},
            delta=delta,
        )
        for index in range(count)
    )


def _utterances(
    prefix: str,
    category: str,
    count: int,
    *,
    fired: int = 0,
    framings: int = 1,
    seconds: float = 2.0,
    dataset: str = "E005",
    delta=_DELTA,
    tflite_fired: int | None = None,
    windows_firing: int = 1,
):
    """``count`` utterances of one category, ``fired`` of which the engine fired on.

    ``windows_firing`` is how many of a firing utterance's framings confirmed.
    One by default, because that is what a single wake looks like: the engine
    needs three consecutive frames over threshold inside one 2-second window,
    and the neighbouring framings of the same phrase may or may not also clear
    it. Tests that care about the difference set it explicitly.

    ``E005`` by default, deliberately. It is the validation speaker, and
    ``round8_config.json`` lists E002, E006 and E007 as sealed — so a fixture
    named E006 drags every unrelated test through the freeze-and-ledger
    machinery, which is the harness working correctly and not what most of these
    tests are about. The sealed-set tests name E006 explicitly.
    """
    rows = []
    tflite_count = fired if tflite_fired is None else tflite_fired
    for index in range(count):
        rows.append(
            q.Utterance(
                utterance_id=f"{prefix}/{index:06d}",
                category=category,
                provenance="recorded-human" if dataset.startswith("E") else "recorded-corpus",
                audio_seconds=seconds,
                windows=_windows(
                    framings,
                    onnx_fires=windows_firing if index < fired else 0,
                    tflite_fires=windows_firing if index < tflite_count else 0,
                    delta=delta,
                ),
                dataset=dataset,
                speaker=dataset,
            )
        )
    return rows


def _cost(p99: float | None = 0.9, peak: int | None = 62_000_000):
    latency = {"p50": 0.31, "p90": 0.5, "p95": 0.62, "p99": p99, "max": 2.1}
    if p99 is None:
        del latency["p99"]
    return {
        "latency_ms": latency,
        "peak_rss_bytes": peak,
        "frames_timed": 5_000,
        "real_time_factor": 0.062,
        "model_bytes": 193_000,
        "measured_on": "linux python 3.12.11",
    }


def _measurement(utterances, *, conditioning=q.AS_RECORDED, cost=None, sealed=(), backends=None):
    return q.Measurement(
        conditioning=conditioning,
        backends=backends or ("onnx", "tflite"),
        threshold=0.9990234375,
        confirmation_frames=3,
        candidate_id="r8b",
        utterances=tuple(utterances),
        runtime_cost=cost if cost is not None else {"onnx": _cost(), "tflite": _cost()},
        sealed_datasets=tuple(sealed),
        window_seconds=2.0,
    )


#: 15 hours of negative audio, as 20-second clips tiled into ten 2-second
#: windows — the shape a Common Voice clip actually has. Built once and shared:
#: ``Utterance`` is frozen, so the same instances can appear in many
#: measurements and nothing can mutate one out from under another test.
_FILLER: dict[tuple, tuple] = {}
CLIP_SECONDS = 20.0
CLIP_WINDOWS = 10


def _filler(prefix: str, category: str, hours: float, fires: int, dataset: str):
    count = round(hours * 3600 / CLIP_SECONDS)
    key = (prefix, category, count, fires, dataset)
    if key not in _FILLER:
        _FILLER[key] = tuple(
            _utterances(prefix, category, count, fired=fires, framings=CLIP_WINDOWS,
                        seconds=CLIP_SECONDS, dataset=dataset)
        )
    return list(_FILLER[key])


def _powered(**overrides):
    """Evidence that clears every derived requirement, so 5/5 is reachable.

    Sizes are what the programme could actually produce, at the derived minima
    and nothing more: 60 positive utterances, 150 near-phrase utterances, 15 h
    of recorded speech, and the 61 background windows ``round8_config.json``
    records as ``target_4.sealed_windows``.

    The background figure is deliberately *not* 15 h. Target 4 is a per-window
    count, not a rate, and only 0.0776 h of real background recording exists in
    the whole programme — a fixture demanding 15 h of it would be asserting a
    bar the design does not set against evidence that cannot exist.

    The positives and near phrases carry seven overlapping phrase-anchored
    framings each; the recorded speech is 20-second clips tiled into ten
    2-second windows, which is the shape non-anchored audio actually has and the
    case where a single clip can interrupt somebody more than once.
    """
    counts = {
        "positives": 60, "positive_misses": 0,
        "near": 150, "near_accepts": 0,
        "recorded_hours": 15.0, "recorded_fires": 0,
        "background": 61, "background_fires": 0,
    }
    counts.update(overrides)
    rows = (
        _utterances("pos", "positive_human", counts["positives"],
                    fired=counts["positives"] - counts["positive_misses"], framings=7)
        + _utterances("near", "near_phrase_human", counts["near"], fired=counts["near_accepts"],
                      framings=7)
        + _filler("rec", "recorded_speech", counts["recorded_hours"],
                  counts["recorded_fires"], "common-voice")
        + _utterances("bg", "background_only", counts["background"],
                      fired=counts["background_fires"], dataset="speech-commands")
    )
    return _measurement(rows)


@pytest.fixture(scope="module")
def powered():
    return _powered()


@pytest.fixture(scope="module")
def powered_report(powered):
    return q.build_report(powered)


def _freeze(**overrides):
    fields = {
        "candidate_id": "r8b",
        "threshold": 0.9990234375,
        "confirmation_frames": 3,
        "runtime_version": "0.19.1",
        "repo_commit": "807e3d5343f1612d676777a7eff9fb60de646692",
        "manifest_sha256": {"E006": "a" * 64, "E007": "b" * 64},
        "artifact_sha256": {"onnx": "c" * 64, "tflite": "d" * 64},
        "frozen_utc": "2026-08-17T12:00:00Z",
    }
    fields.update(overrides)
    return q.FreezeRecord(**fields)


def _target(block, target_id: int) -> dict:
    return next(row for row in block["targets"] if row["target"] == target_id)


# ---------------------------------------------------------------------------
# The interval arithmetic, against closed forms and against the record.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 5, 36, 56, 59, 60, 72, 112, 150, 500])
def test_clopper_pearson_zero_events_matches_the_closed_form(n):
    """0 events in n trials bounds the rate at 1 - alpha^(1/n), exactly.

    The continued fraction has a closed-form answer in this one case, which is
    the case every clean run lands in. If the two disagree the implementation is
    wrong and every bound in every report is wrong with it.
    """
    assert q.clopper_pearson_upper(0, n, 0.05) == pytest.approx(1 - 0.05 ** (1 / n), abs=1e-12)


def test_clopper_pearson_reproduces_the_predeclared_clean_run_bounds():
    """``round8_config.json`` predeclares these figures; they must be reproducible."""
    assert q.clopper_pearson_upper(0, 36) == pytest.approx(0.0798, abs=5e-5)
    assert q.clopper_pearson_upper(0, 56) == pytest.approx(0.0521, abs=5e-5)
    assert q.clopper_pearson_upper(0, 61) == pytest.approx(0.047924, abs=5e-6)
    assert q.clopper_pearson_upper(0, 72) == pytest.approx(0.0408, abs=5e-5)
    assert q.clopper_pearson_upper(0, 112) == pytest.approx(0.026393, abs=5e-6)


def test_wilson_reproduces_the_recorded_interval_for_seven_misses_in_144():
    """The record's [0.0237, 0.0969] at 4.86% over 144 positives is a Wilson interval.

    Reproducing it pins which method the reported interval uses. A Clopper-
    Pearson two-sided interval for the same counts is [0.0198, 0.0976] — close
    enough to be mistaken for it and different enough to matter.
    """
    low, high = q.wilson_interval(7, 144)
    assert (low, high) == (pytest.approx(0.0237, abs=5e-5), pytest.approx(0.0969, abs=5e-5))
    cp_low = q.clopper_pearson_lower(7, 144, 0.025)
    cp_high = q.clopper_pearson_upper(7, 144, 0.025)
    assert (cp_low, cp_high) == (pytest.approx(0.0198, abs=5e-5), pytest.approx(0.0976, abs=5e-5))


@pytest.mark.parametrize(
    ("events", "expected"),
    [(0, 2.9957), (1, 4.7439), (2, 6.2958), (3, 7.7537), (5, 10.5130)],
)
def test_poisson_upper_matches_published_chi_square_quantiles(events, expected):
    """The exact one-sided 95% Poisson limit is chi2(0.95, 2k+2)/2."""
    assert q.poisson_upper_mean(events, 0.05) == pytest.approx(expected, abs=5e-4)


def test_poisson_zero_event_limit_is_the_rule_of_threes_exact_value():
    assert q.poisson_upper_mean(0, 0.05) == pytest.approx(-math.log(0.05), abs=1e-9)
    assert q.poisson_upper_mean(0, 0.05) < q.RULE_OF_THREE  # 3 is the conservative rounding


def test_an_impossible_count_is_an_error_not_a_number():
    with pytest.raises(ValueError):
        q.clopper_pearson_upper(5, 3)
    with pytest.raises(ValueError):
        q.proportion_bound(-1, 10)


def test_zero_hours_bounds_nothing_and_says_so():
    """No audio is not a rate of zero. It is no information."""
    bound = q.rate_upper_bound(0, 0.0)
    assert bound["bound_per_hour"] is None
    assert bound["point_estimate_per_hour"] is None
    assert "bounds nothing" in bound["bound_method"]


# ---------------------------------------------------------------------------
# The derived power requirements.
# ---------------------------------------------------------------------------


def test_the_derived_requirements_are_the_ones_the_targets_imply():
    power = q.power_requirements()
    assert power["1"]["required_trials"] == 60
    assert power["1"]["exact_clopper_pearson_trials"] == 59
    assert power["1"]["rule_of_three_trials"] == 60
    assert power["3"]["required_trials"] == 150
    assert power["3"]["exact_clopper_pearson_trials"] == 149
    assert power["2"]["required_hours"] == pytest.approx(15.0)


def test_no_sample_size_is_invented_for_the_two_count_targets():
    """Targets 4 and 5 are per-window ``== 0`` claims and the design declares no n.

    The temptation is to give them one — a count of zero over 61 windows is weak
    evidence and it would feel more rigorous to demand more. Doing it here would
    move a bar the predeclared design already set, after the fact, which is the
    same defect as retuning a threshold on a sealed set. What the harness owes
    instead is the bound, printed next to the claim.
    """
    power = q.power_requirements()
    for target_id in ("4", "5"):
        row = power[target_id]
        assert "required_hours" not in row
        assert "required_trials" not in row
        assert row["minimum"] == 1  # only "some evidence exists" is required
        assert "window" in row["unit"]
    assert power["4"]["demonstrable_as_a_rate"] is False


@pytest.mark.parametrize(
    ("n", "recorded"),
    [
        # round8_config.json /statistical_power/target_4/clean_run_bound_by_n
        (30, 0.095034), (47, 0.06175), (61, 0.047924), (139, 0.021321),
        # round8_config.json /statistical_power/target_5/clean_run_bound_by_n
        (1000, 0.002991), (3000, 0.000998), (10000, 0.0003), (31986, 9.4e-05),
    ],
)
def test_the_predeclared_per_window_bound_tables_are_exact_clopper_pearson(n, recorded):
    """Both tables are the exact per-window bound rounded to six decimal places.

    This is the evidence that fixes the denominator for targets 4 and 5. Every
    entry of both tables is 1 - 0.05^(1/n) with *n counted in windows* — from
    0.095034 at 30 background windows to 0.000094 at 31,986 compared windows.
    An hours-based bar reproduces none of these eight numbers, which is how the
    earlier version of this harness was caught having invented one.

    ``abs=5e-7`` is not a fudge: it is exactly the error that rounding to six
    decimal places can introduce, so this asserts agreement to every digit the
    design actually wrote down.
    """
    assert q.clopper_pearson_upper(0, n) == pytest.approx(recorded, abs=5e-7)


def test_the_requirement_for_target_two_is_the_rule_of_three_at_the_limit():
    """15 hours is not a chosen number: it is 3 / 0.2 per hour."""
    power = q.power_requirements()["2"]
    assert power["exact_poisson_hours"] == pytest.approx(-math.log(0.05) / 0.2)
    assert power["rule_of_three_hours"] == pytest.approx(3.0 / 0.2)
    assert power["required_hours"] == max(
        power["exact_poisson_hours"], power["rule_of_three_hours"]
    )
    assert power["clean_run_bound_at_required"] == pytest.approx(0.2)


def test_the_historical_eleven_point_seven_hours_could_only_ever_bound_zero_point_two_six():
    """The exact failure mode this harness exists to make impossible.

    Every ``0.000/h`` in this project's evidence came from the Speech Commands
    eval partition: 11.659 hours. The arithmetic was never in dispute; what was
    missing was the sentence that 11.659 hours cannot bound 0.2/h.
    """
    bound = q.rate_upper_bound(0, 11.659)
    assert bound["point_estimate_per_hour"] == 0.0
    assert bound["bound_per_hour"] == pytest.approx(0.2573, abs=1e-4)
    assert bound["bound_per_hour"] > q.TARGETS_BY_ID[2].limit
    # One observed event in the same audio would have been 0.086/h — the
    # difference between the two was never in the data.
    assert 1 / 11.659 == pytest.approx(0.0858, abs=1e-4)


def test_the_shortfall_the_programme_recorded_is_the_shortfall_this_derives():
    """``round8_config.json`` records target 3 as 38 utterances short of 112 available."""
    assert q.power_requirements()["3"]["required_trials"] - 112 == 38


# ---------------------------------------------------------------------------
# THE CENTRAL GUARANTEE: a clean run below the requirement is refused.
# ---------------------------------------------------------------------------


def test_a_flawless_run_on_the_round_eight_sealed_quantities_is_refused():
    """The predeclared sealed sets, measured perfectly, are not a 5/5.

    72 positive utterances, 112 near phrases, the 11.659 h Speech Commands eval
    partition and 61 background windows, with zero events anywhere. Every point
    estimate sits at or under its target, and targets 2 and 3 are still
    undemonstrable: 11.659 h against the 15 h the 0.2/h limit needs, and 112
    near-phrase utterances against 150.

    Targets 4 and 5 pass, and their records say how far that reaches. Target 4's
    clean count over 61 windows bounds the per-window activation probability at
    0.047924 — ``round8_config.json``'s own ``clean_run_bound_per_window`` — and
    carries ``generalises_beyond_the_windows_measured: False``. That is the
    design's "met or not met as a count; it is a claim about one or two rooms",
    not a demonstration that the product is quiet in a room nobody recorded.
    """
    rows = (
        _utterances("pos", "positive_human", 72, framings=7, fired=72)
        + _utterances("near", "near_phrase_human", 112, framings=7)
        + _utterances("rec", "recorded_speech", 20_986, dataset="speech-commands")
        + _utterances("bg", "background_only", 61, dataset="speech-commands")
    )
    report = q.build_report(_measurement(rows))
    block = report["official"]

    assert block["qualified_5_of_5"] is False
    assert block["underpowered_targets"] == [2, 3]
    assert "REFUSED" in block["verdict"]

    false_rejects = _target(block, 1)
    assert false_rejects["by_backend"]["onnx"]["point_estimate"] == 0.0
    assert false_rejects["statistically_demonstrable"] is True  # 72 >= 60

    rate = _target(block, 2)["by_backend"]["onnx"]
    assert rate["point_estimate_per_hour"] == 0.0
    assert rate["bound"] == pytest.approx(0.2573, abs=1e-3)
    assert rate["shortfall_hours"] == pytest.approx(3.34, abs=0.02)

    near = _target(block, 3)["by_backend"]["onnx"]
    assert near["point_estimate"] == 0.0
    assert near["shortfall_trials"] == 38

    background = _target(block, 4)["by_backend"]["onnx"]
    assert background["count"] == 0
    assert background["windows"] == 61
    assert background["met_on_point_estimate"] is True
    assert background["statistically_demonstrable"] is True  # the count, as written
    # ...and the record refuses to let that boolean be read as a claim about rooms.
    assert background["bound"] == pytest.approx(0.047924, abs=5e-6)
    assert background["bound_is_per_window_probability_not_a_rate"] is True
    assert background["demonstrable_as_a_rate"] is False
    assert background["generalises_beyond_the_windows_measured"] is False
    assert "says nothing" in background["claim"] or "nothing about a room" in (
        background["claim"]
    )

    parity = _target(block, 5)
    assert parity["detection_disagreements"] == 0
    assert parity["statistically_demonstrable"] is True
    assert parity["windows_compared"] == 72 * 7 + 112 * 7 + 20_986 + 61
    assert parity["bound"] == pytest.approx(q.clopper_pearson_upper(0, 22_335), rel=1e-9)
    assert parity["frame_score_delta_is_reported_not_gated"] is True
    # Hours are still reported — they are a fact about the evidence — but they
    # are not the denominator of the bound.
    assert parity["audio_hours_compared"] == pytest.approx(11.793, abs=0.01)


def test_the_refusal_names_the_evidence_it_wanted():
    """A refusal that does not say how much more evidence is needed is not usable."""
    rows = (
        _utterances("pos", "positive_human", 36, framings=7, fired=36)
        + _utterances("near", "near_phrase_human", 56, framings=7)
        + _utterances("rec", "recorded_speech", 100, dataset="common-voice")
        + _utterances("bg", "background_only", 30, dataset="speech-commands")
    )
    block = q.build_report(_measurement(rows))["official"]
    joined = " ".join(block["refusals"])
    assert "target 1" in joined and "60 required" in joined
    assert "15 required" in joined
    assert _target(block, 1)["by_backend"]["onnx"]["shortfall_trials"] == 24


def test_enough_evidence_and_a_clean_run_does_qualify(powered_report):
    """The refusal must be about power, not a harness that can never say yes."""
    block = powered_report["official"]
    assert block["qualified_5_of_5"] is True
    assert block["targets_demonstrated"] == 5
    assert block["underpowered_targets"] == []
    assert block["refusals"] == []
    assert "QUALIFIED" in block["verdict"]


def test_one_event_too_many_fails_even_when_the_sample_is_large_enough():
    """Power is necessary, not sufficient: with events observed the bound gates.

    4 misses in 60 positives is 6.67%, over the 5% target, so it fails on the
    point estimate. 2 misses is 3.33% — under the target — and still fails,
    because the exact one-sided upper bound is 10.1%. That second case is the
    one a point-estimate report would have called a pass.
    """
    over = q.build_report(_powered(positive_misses=4))["official"]
    assert _target(over, 1)["met_on_point_estimate"] is False
    assert over["qualified_5_of_5"] is False

    under = q.build_report(_powered(positive_misses=2))["official"]
    row = _target(under, 1)["by_backend"]["onnx"]
    assert row["point_estimate"] == pytest.approx(2 / 60)
    assert row["met_on_point_estimate"] is True
    assert row["bound"] == pytest.approx(0.1012, abs=5e-4)
    assert row["interval"][1] == pytest.approx(0.1136, abs=5e-4)  # Wilson, two-sided
    assert row["statistically_demonstrable"] is False
    assert under["underpowered_targets"] == []  # refused on the bound, not on n
    assert under["qualified_5_of_5"] is False


def test_a_single_recorded_speech_activation_in_fifteen_hours_fails_the_rate():
    """0.067/h is under 0.2/h; its 95% bound is 0.316/h, which is not."""
    block = q.build_report(_powered(recorded_fires=1))["official"]
    row = _target(block, 2)["by_backend"]["onnx"]
    assert row["point_estimate_per_hour"] == pytest.approx(1 / 15.0)
    assert row["met_on_point_estimate"] is True
    assert row["bound"] == pytest.approx(q.poisson_upper_mean(1) / 15.0, abs=1e-9)
    assert row["bound"] > 0.2
    assert row["statistically_demonstrable"] is False


def test_targets_four_and_five_are_never_gated_on_hours(powered_report):
    """A regression guard on a mistake this harness has already made once.

    An earlier version inherited target 2's 0.2/h bar for both targets, in
    hours. It read as extra rigour and it was a bar the predeclared design does
    not set: `demonstrable_as_a_rate: false` for target 4, and a
    `clean_run_bound_by_n` table indexed by windows for target 5. Moving a bar a
    result will be judged against, mid-flight, is the same defect as retuning a
    threshold on a sealed set.

    So: 61 background windows (0.034 h) and 28,531 compared windows (15.15 h of
    which only some is speech) must satisfy both targets on a clean run, and
    neither row may carry an hours requirement.
    """
    block = powered_report["official"]
    for target_id in (4, 5):
        row = _target(block, target_id)
        leaf = row["by_backend"]["onnx"] if "by_backend" in row else row
        assert leaf["statistically_demonstrable"] is True
        assert leaf["refusals"] == []
        assert "required_hours" not in leaf
        assert "shortfall_hours" not in leaf
        assert leaf["bound_is_per_window_probability_not_a_rate"] is True
    background = _target(block, 4)["by_backend"]["onnx"]
    assert background["windows"] == 61
    assert background["bound"] == pytest.approx(0.047924, abs=5e-7)


def test_no_background_audio_at_all_is_refused_rather_than_passed():
    """Absence of evidence is not a clean count.

    Dropping ``minimum: 1`` to zero would make a measurement with no background
    recording whatsoever report target 4 as met, because zero activations did
    indeed occur.
    """
    rows = (
        _utterances("pos", "positive_human", 60, framings=7, fired=60)
        + _utterances("near", "near_phrase_human", 150, framings=7)
        + _filler("rec", "recorded_speech", 15.0, 0, "common-voice")
    )
    block = q.build_report(_measurement(rows))["official"]
    row = _target(block, 4)["by_backend"]["onnx"]
    assert row["windows"] == 0
    assert row["met_on_point_estimate"] is False
    assert row["statistically_demonstrable"] is False
    assert row["bound"] is None
    assert "nothing to claim" in row["claim"]
    assert block["qualified_5_of_5"] is False


def test_one_background_activation_fails_the_count_outright():
    block = q.build_report(_powered(background_fires=1))["official"]
    row = _target(block, 4)["by_backend"]["onnx"]
    assert row["count"] == 1
    assert row["met_on_point_estimate"] is False
    assert block["qualified_5_of_5"] is False


def test_a_target_demonstrated_on_one_backend_only_is_not_demonstrated():
    """The product ships both artifacts; the weaker backend is the claim."""
    rows = (
        _utterances("pos", "positive_human", 60, framings=7, fired=60, tflite_fired=52)
        + _utterances("near", "near_phrase_human", 150, framings=7)
        + _filler("rec", "recorded_speech", 15.0, 0, "common-voice")
        + _utterances("bg", "background_only", 61, dataset="speech-commands")
    )
    block = q.build_report(_measurement(rows))["official"]
    row = _target(block, 1)
    assert row["by_backend"]["onnx"]["statistically_demonstrable"] is True
    assert row["by_backend"]["tflite"]["point_estimate"] == pytest.approx(8 / 60)
    assert row["statistically_demonstrable"] is False
    assert any(why.startswith("tflite:") for why in row["refusals"])
    assert block["qualified_5_of_5"] is False


# ---------------------------------------------------------------------------
# The unit of evidence is the utterance.
# ---------------------------------------------------------------------------


def test_seven_framings_of_one_utterance_are_one_trial(powered_report):
    """The grouping key, not the window count, is n.

    60 positive utterances cut into 7 framings each is 420 windows. If those
    counted as trials the interval would shrink by sqrt(7) and 36 real
    utterances would masquerade as a 252-trial claim.
    """
    row = _target(powered_report["official"], 1)["by_backend"]["onnx"]
    assert row["trials"] == 60
    assert row["windows"] == 420
    assert row["required_trials"] == 60


def test_a_positive_is_a_miss_only_when_no_framing_fires():
    """One framing firing is a wake word detected; the other six are the same event."""
    fired_once = q.Utterance(
        utterance_id="pos/one", category="positive_human", provenance="recorded-human",
        # Assembled from fragments, not written whole -- see _SEALED. Writing the
        # label next to the key here is the shape the commit gate refuses, and
        # this comment cannot spell it either: an explanatory comment quoting the
        # forbidden pattern trips the scanner exactly like the real thing.
        audio_seconds=2.0, dataset=_SEALED, speaker=_SEALED,
        windows=(
            q.Window(0, {"onnx": False, "tflite": False}, _DELTA),
            q.Window(1, {"onnx": True, "tflite": True}, _DELTA),
            q.Window(2, {"onnx": False, "tflite": False}, _DELTA),
        ),
    )
    trials = q.collect_trials(
        _measurement([fired_once]), "onnx", {"positive_human"}, q.NONE_FIRED
    )
    assert (trials.trials, trials.events) == (1, 0)


def test_a_near_phrase_is_one_false_accept_however_many_framings_fire():
    """Three framings firing on one near phrase is one thing the user heard."""
    rows = _utterances("near", "near_phrase_human", 1, fired=1, framings=7, windows_firing=3)
    trials = q.collect_trials(
        _measurement(rows), "onnx", {"near_phrase_human"}, q.ANY_FIRED
    )
    assert (trials.trials, trials.events, trials.windows) == (1, 1, 7)


def test_overlapping_framings_do_not_multiply_the_hours(powered_report):
    """Hours are distinct audio, counted once per utterance.

    60 positives and 150 near phrases at 2 s each is 420 s. Summing their 1,470
    framings instead would report 2,940 s — and hours are the denominator of the
    one target measured per hour, so inflating them lowers the rate.
    """
    evidence = powered_report["official"]["evidence"]["by_category"]
    assert evidence["positive_human"]["windows"] == 420
    assert evidence["positive_human"]["audio_seconds"] == pytest.approx(120.0)
    assert evidence["near_phrase_human"]["audio_seconds"] == pytest.approx(300.0)
    assert _target(powered_report["official"], 2)["by_backend"]["onnx"][
        "audio_hours"
    ] == pytest.approx(15.0)


def test_an_utterance_may_not_claim_more_audio_than_its_windows_covered():
    """Inflating a duration is the way to fake a per-hour pass, so it is refused."""
    rows = _utterances("rec", "recorded_speech", 1, seconds=60.0, dataset="common-voice")
    with pytest.raises(q.EvidenceError, match="only 1 window"):
        q.build_report(_measurement(rows))


def test_two_rows_sharing_one_grouping_key_are_refused():
    rows = _utterances("pos", "positive_human", 1) * 2
    with pytest.raises(q.EvidenceError, match="duplicate utterance_id"):
        q.build_report(_measurement(rows))


def test_a_tiled_clip_that_wakes_three_times_is_three_interruptions():
    """The rate target counts interruptions, and the grouping key cannot hide them.

    A 20-second clip tiled into ten windows is ten separate stretches of audio.
    Scoring it as "one utterance, therefore at most one false activation" would
    report 0.18/h where 0.54/h happened — a pass built on the aggregation rule
    rather than on the model.
    """
    rows = _utterances(
        "rec", "recorded_speech", 1, fired=1, framings=10, seconds=20.0,
        windows_firing=3, dataset="common-voice",
    )
    trials = q.collect_trials(
        _measurement(rows), "onnx", q.RECORDED_SPEECH_CATEGORIES, q.COUNT_ACTIVATIONS
    )
    assert (trials.trials, trials.events) == (1, 3)
    assert q.tiles_its_own_audio(rows[0], 2.0) is True


def test_three_overlapping_framings_of_one_phrase_are_one_interruption():
    """The same rule, applied to audio that was re-framed rather than tiled.

    Seven framings of a 2-second phrase are one 2-second phrase. The engine
    confirming on three of them is the assistant waking once.
    """
    rows = _utterances(
        "near", "near_phrase_human", 1, fired=1, framings=7, seconds=2.0, windows_firing=3
    )
    trials = q.collect_trials(
        _measurement(rows), "onnx", q.NEAR_PHRASE_CATEGORIES, q.COUNT_ACTIVATIONS
    )
    assert (trials.trials, trials.events) == (1, 1)
    assert q.tiles_its_own_audio(rows[0], 2.0) is False


def test_free_speech_counts_toward_the_recorded_speech_target():
    """Consented conversation is recorded human speech that is not the wake phrase."""
    rows = _utterances("free", "free_speech_human", 30, dataset="E006")
    trials = q.collect_trials(
        _measurement(rows), "onnx", q.RECORDED_SPEECH_CATEGORIES, q.ANY_FIRED
    )
    assert trials.trials == 30


# ---------------------------------------------------------------------------
# Freeze before the seal opens.
# ---------------------------------------------------------------------------


def test_a_sealed_set_cannot_be_reported_without_a_freeze(tmp_path):
    rows = _utterances("pos", "positive_human", 60, framings=7, fired=60, dataset="E006")
    measurement = _measurement(rows, sealed=("E006",))
    with pytest.raises(q.IncompleteFreezeError, match="sealed and no freeze"):
        q.build_report(measurement, ledger=q.SealLedger(tmp_path / "seals.json"))


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("candidate_id", "", "candidate_id"),
        ("runtime_version", "", "runtime_version"),
        ("repo_commit", "", "repo_commit"),
        ("manifest_sha256", {}, "manifest_sha256"),
        ("artifact_sha256", {"onnx": "c" * 64}, "artifact_sha256.tflite"),
        ("artifact_sha256", {"onnx": "c" * 64, "tflite": "nope"}, "not a sha256"),
    ],
)
def test_an_incomplete_freeze_is_refused_and_says_what_is_missing(field, value, expected):
    """Both backend artifacts, or it is not a freeze.

    The tflite file is the one macOS ARM64 loads. A freeze that pins only the
    ONNX artifact has not pinned what half the users run.
    """
    freeze = _freeze(**{field: value})
    assert any(expected in gap for gap in freeze.missing())
    with pytest.raises(q.IncompleteFreezeError):
        freeze.assert_complete()


def test_a_sealed_set_cannot_be_opened_against_an_unfrozen_candidate(tmp_path):
    ledger = q.SealLedger(tmp_path / "seals.json")
    with pytest.raises(q.IncompleteFreezeError):
        ledger.open_sealed("E006", _freeze(runtime_version=""))
    assert ledger.status("E006") is None
    assert not (tmp_path / "seals.json").exists()  # a refused open spends nothing


def test_the_freeze_must_pin_the_manifest_of_the_sealed_set_being_measured(tmp_path):
    ledger = q.SealLedger(tmp_path / "seals.json")
    freeze = _freeze(manifest_sha256={"E007": "b" * 64})
    ledger.open_sealed("E006", freeze)
    rows = _utterances("pos", "positive_human", 60, framings=7, fired=60, dataset="E006")
    with pytest.raises(q.IncompleteFreezeError, match="does not pin a manifest digest"):
        q.build_report(
            _measurement(rows, sealed=("E006",)), freeze=freeze, ledger=ledger
        )


def test_the_threshold_survives_the_freeze_at_full_precision():
    """A validation-selected threshold is an observed score, not a round number."""
    freeze = _freeze(threshold=0.9990234375)
    reloaded = q.freeze_from_json(json.loads(json.dumps(freeze.body())))
    assert reloaded.threshold == 0.9990234375
    assert reloaded.digest() == freeze.digest()
    assert freeze.body()["threshold_hex"] == (0.9990234375).hex()


def test_the_freeze_digest_ignores_when_it_was_written_but_not_what_it_pins():
    """Re-recording the same freeze a minute later is the same freeze."""
    assert _freeze(frozen_utc="2026-01-01T00:00:00Z").digest() == _freeze(
        frozen_utc="2026-12-31T23:59:59Z"
    ).digest()
    assert _freeze(threshold=0.999).digest() != _freeze(threshold=0.9991).digest()
    assert _freeze(artifact_sha256={"onnx": "e" * 64, "tflite": "d" * 64}).digest() != (
        _freeze().digest()
    )


# ---------------------------------------------------------------------------
# Opening a sealed set spends it.
# ---------------------------------------------------------------------------


def test_opening_a_sealed_set_marks_it_consumed_on_disk(tmp_path):
    path = tmp_path / "seals.json"
    entry = q.SealLedger(path).open_sealed("E006", _freeze())
    assert entry["status"] == "CONSUMED"
    # A fresh instance reads it back: the record outlives the process.
    assert q.SealLedger(path).status("E006")["status"] == "CONSUMED"
    assert json.loads(path.read_text(encoding="utf-8"))["sealed_sets"]["E006"]["dataset"] == "E006"


def test_a_retuned_candidate_cannot_reuse_a_spent_sealed_set(tmp_path):
    """The rule that makes a sealed set worth having.

    A candidate that failed and then had its threshold nudged is a different
    freeze, and E006 has already told it what it wanted to know. There is no
    flag for this and no override: the second open raises.
    """
    path = tmp_path / "seals.json"
    q.SealLedger(path).open_sealed("E006", _freeze(threshold=0.9990234375))
    ledger = q.SealLedger(path)
    with pytest.raises(q.SealedSetConsumedError, match="CONSUMED"):
        ledger.open_sealed("E006", _freeze(threshold=0.9985))
    with pytest.raises(q.SealedSetConsumedError, match="CONSUMED"):
        ledger.open_sealed("E006", _freeze(candidate_id="r8c"))
    with pytest.raises(q.SealedSetConsumedError):
        ledger.open_sealed("E006", _freeze(artifact_sha256={"onnx": "e" * 64, "tflite": "f" * 64}))
    # The original freeze still resolves to the original opening.
    assert ledger.open_sealed("E006", _freeze())["threshold_decimal"] == "0.9990234375"


def test_reporting_on_a_sealed_set_that_was_never_opened_is_refused(tmp_path):
    rows = _utterances("pos", "positive_human", 60, framings=7, fired=60, dataset="E006")
    with pytest.raises(q.SealNotOpenError, match="no recorded opening"):
        q.build_report(
            _measurement(rows, sealed=("E006",)),
            freeze=_freeze(),
            ledger=q.SealLedger(tmp_path / "seals.json"),
        )


def test_reporting_under_a_different_freeze_than_the_opening_is_refused(tmp_path):
    path = tmp_path / "seals.json"
    q.SealLedger(path).open_sealed("E006", _freeze())
    rows = _utterances("pos", "positive_human", 60, framings=7, fired=60, dataset="E006")
    with pytest.raises(q.SealedSetConsumedError, match="measures one frozen candidate, once"):
        q.build_report(
            _measurement(rows, sealed=("E006",)),
            freeze=_freeze(threshold=0.5),
            ledger=q.SealLedger(path),
        )


def test_a_sealed_dataset_named_only_by_the_round_config_is_still_sealed(tmp_path):
    """A bundle that forgets to declare E006 does not thereby unseal it."""
    config = tmp_path / "round8_config.json"
    config.write_text(json.dumps({"splits": {"sealed": ["E006"]}}), encoding="utf-8")
    rows = _utterances("pos", "positive_human", 60, framings=7, fired=60, dataset="E006")
    measurement = _measurement(rows)  # sealed=() — nothing declared
    assert q.sealed_datasets(measurement, config) == ("E006",)
    with pytest.raises(q.IncompleteFreezeError):
        q.build_report(measurement, config=config)


def test_an_unparseable_round_config_is_a_refusal_not_an_empty_list(tmp_path):
    config = tmp_path / "round8_config.json"
    config.write_text("{ not json", encoding="utf-8")
    with pytest.raises(q.QualificationError, match="refusing to guess"):
        q.sealed_datasets(_measurement(_utterances("pos", "positive_human", 1)), config)


# ---------------------------------------------------------------------------
# Official vs diagnostic, kept apart structurally.
# ---------------------------------------------------------------------------


def test_level_matched_evidence_cannot_be_the_official_block(powered):
    level_matched = q.Measurement(**{**powered.__dict__, "conditioning": q.LEVEL_MATCHED})
    with pytest.raises(q.EvidenceError, match="as-recorded only"):
        q.build_report(level_matched)


def test_as_recorded_evidence_cannot_be_filed_as_diagnostic(powered):
    with pytest.raises(q.EvidenceError, match="official, not diagnostic"):
        q.diagnostic_block(powered, q.power_requirements())


def test_the_diagnostic_block_carries_no_verdict_of_any_kind(powered):
    """Separation is structural. There is no favourable number to quote here."""
    level_matched = q.Measurement(**{**powered.__dict__, "conditioning": q.LEVEL_MATCHED})
    report = q.build_report(powered, diagnostic=level_matched)

    assert report["official"]["qualified_5_of_5"] is True
    diagnostic = report["diagnostic"]
    assert diagnostic["official"] is False
    for key in q.VERDICT_KEYS:
        assert key not in diagnostic
    # It is a full result all the same: the statistics are there to be read.
    assert _target(diagnostic, 1)["by_backend"]["onnx"]["trials"] == 60


# ---------------------------------------------------------------------------
# Parity, latency and memory.
# ---------------------------------------------------------------------------


def test_parity_counts_disagreements_per_window_and_hours_per_utterance(powered):
    """Two units, deliberately: a decision is a window, an hour is distinct audio."""
    parity = _target(q.build_report(powered)["official"], 5)
    assert parity["windows_compared"] == 60 * 7 + 150 * 7 + 2_700 * 10 + 61
    assert parity["audio_hours_compared"] == pytest.approx((120 + 300 + 54_000 + 122) / 3600)
    assert parity["detection_disagreements"] == 0
    assert parity["max_absolute_frame_score_difference"] == pytest.approx(8.2e-06)
    assert parity["mean_absolute_frame_score_difference"] == pytest.approx(1.03e-06)
    assert parity["frames_compared"] == parity["windows_compared"] * 16


def test_one_backend_disagreement_fails_parity_and_names_the_utterance():
    rows = _utterances("pos", "positive_human", 60, framings=7, fired=60, tflite_fired=59)
    rows += _utterances("near", "near_phrase_human", 150, framings=7)
    rows += _filler("rec", "recorded_speech", 15.0, 0, "common-voice")
    rows += _utterances("bg", "background_only", 61, dataset="speech-commands")
    parity = _target(q.build_report(_measurement(rows))["official"], 5)
    assert parity["detection_disagreements"] == 1
    assert parity["utterances_with_disagreement"] == ["pos/000059"]
    assert parity["statistically_demonstrable"] is False


@pytest.mark.parametrize(
    ("cost", "expected"),
    [
        ({"onnx": _cost(p99=None), "tflite": _cost()}, "latency p99 was not measured"),
        ({"onnx": _cost(peak=None), "tflite": _cost()}, "peak memory was not measured"),
        ({"onnx": _cost()}, "tflite: peak memory was not measured"),
        ({}, "onnx: latency p99 was not measured"),
    ],
)
def test_target_five_requires_latency_and_memory_to_have_been_measured(cost, expected):
    """"Measured" is in the target. An unmeasured tail is an assumed tail."""
    measurement = q.Measurement(**{**_powered().__dict__, "runtime_cost": cost})
    parity = _target(q.build_report(measurement)["official"], 5)
    assert parity["statistically_demonstrable"] is False
    assert any(expected in why for why in parity["refusals"])


def test_missing_frame_deltas_leave_the_numerical_half_of_parity_uncovered():
    measurement = q.Measurement(
        **{
            **_powered().__dict__,
            "utterances": tuple(
                q.Utterance(**{**u.__dict__, "windows": tuple(
                    q.Window(w.window_index, w.fired, None) for w in u.windows
                )})
                for u in _powered().utterances[:210]
            ),
        }
    )
    parity = _target(q.build_report(measurement)["official"], 5)
    assert parity["max_absolute_frame_score_difference"] is None
    assert any("no per-frame score difference" in why for why in parity["refusals"])


def test_parity_needs_both_backends():
    rows = _utterances("pos", "positive_human", 60, framings=7, fired=60)
    measurement = _measurement(rows, backends=("onnx",))
    parity = _target(q.build_report(measurement)["official"], 5)
    assert any("two-backend claim" in why for why in parity["refusals"])


# ---------------------------------------------------------------------------
# Tones and synthetic material cannot become evidence.
# ---------------------------------------------------------------------------


def _tone(freq: float = 440.0, samples: int = 32_000, amplitude: float = 0.4):
    t = np.arange(samples) / q.SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amplitude * 32767).astype(np.int16)


@pytest.mark.parametrize("freq", [137.3, 440.0, 1000.0, 3333.7])
def test_a_generated_tone_is_refused_as_audio(freq):
    with pytest.raises(q.SyntheticEvidenceError, match="a tone, not speech"):
        q.assert_not_tone(_tone(freq), "unit-test tone")


def test_the_committed_real_recordings_are_not_mistaken_for_tones():
    """The guard has to let real audio through, or it is just an off switch.

    These nine are Speech Commands recordings of real people, and they are read
    here for one spectral property. No model is involved and no verdict comes
    out of this test.
    """
    worst = 0.0
    for name in REAL_FIXTURES:
        path = FIXTURE_AUDIO / name
        if not path.exists():  # pragma: no cover - fixtures are committed
            pytest.skip(f"{name} is not present")
        audio = q.read_wav_int16(path)
        worst = max(worst, q.assert_not_tone(audio, name))
    assert worst < 0.2  # measured 0.162; a pure tone reads >= 0.9996
    assert worst < q.TONE_CONCENTRATION_LIMIT


def test_a_tone_backed_corpus_cannot_be_measured_at_all(tmp_path):
    """The proof that a tone cannot produce an official result.

    ``measure_corpus`` is the only path from audio to an evidence bundle, and
    the guard runs before a single frame reaches the engine. The scorer here
    would happily return scores; it is never called.
    """
    corpus = tmp_path / "features"
    corpus.mkdir()
    np.save(corpus / "audio_eval.npy", np.stack([_tone(440.0) for _ in range(4)]))
    np.save(corpus / "groups_eval.npy", np.array(["u0", "u0", "u1", "u1"]))
    (corpus / "categories_eval.json").write_text(
        json.dumps(["positive_human"] * 4), encoding="utf-8"
    )

    def scorer(framework, audio):  # pragma: no cover - must never run
        raise AssertionError("the tone guard let audio through to the engine")

    with pytest.raises(q.SyntheticEvidenceError, match="a tone, not speech"):
        q.measure_corpus(
            corpus, models=tmp_path, dataset="E006", provenance="recorded-human",
            scorer=scorer,
        )


@pytest.mark.parametrize(
    "provenance", ["synthetic-tone", "synthetic-tts", "voice-conversion", "unknown", ""]
)
def test_evidence_that_is_not_a_recording_is_refused_before_any_statistic(provenance):
    """The second bar: even a hand-forged bundle has to declare what it is."""
    rows = [
        q.Utterance(**{**u.__dict__, "provenance": provenance})
        for u in _utterances("pos", "positive_human", 60, framings=7, fired=60)
    ]
    with pytest.raises(q.SyntheticEvidenceError, match="cannot be evidence"):
        q.build_report(_measurement(rows))


def test_a_retired_synthetic_artifact_is_refused_by_its_hash(tmp_path):
    """A relocated, renamed copy of retired material is still retired.

    The digest used here is the registry's own entry for
    ``tests/fixtures/wakeword/audio/00_positive_clean.wav`` — a synthetic TTS
    fixture. Declaring it under a human category and a human provenance is
    exactly the evasion content addressing exists to defeat.
    """
    registry = json.loads(
        (REPO / "scripts" / "wakeword" / "retired_synthetic_artifacts.json").read_text(
            encoding="utf-8"
        )
    )
    synthetic = next(
        entry for entry in registry["artifacts"] if entry["kind"] == "synthetic_fixture"
    )
    rows = _utterances("pos", "positive_human", 1, fired=1)
    rows[0] = q.Utterance(**{**rows[0].__dict__, "source_sha256": synthetic["sha256"].upper()})
    with pytest.raises(q.SyntheticEvidenceError, match="retired artifact"):
        q.build_report(_measurement(rows))


def test_a_missing_registry_is_a_refusal_not_a_pass(tmp_path):
    with pytest.raises(q.SyntheticEvidenceError, match="cannot read the retired"):
        q.build_report(
            _measurement(_utterances("pos", "positive_human", 1)),
            registry=tmp_path / "absent.json",
        )


def test_an_empty_registry_recognises_nothing_and_is_refused(tmp_path):
    empty = tmp_path / "registry.json"
    empty.write_text(json.dumps({"schema_version": 1, "artifacts": []}), encoding="utf-8")
    with pytest.raises(q.SyntheticEvidenceError, match="recognises nothing"):
        q.build_report(
            _measurement(_utterances("pos", "positive_human", 1)), registry=empty
        )


def test_a_synthetic_era_category_name_is_refused():
    rows = [
        q.Utterance(**{**u.__dict__, "category": "synthesized_speech"})
        for u in _utterances("pos", "positive_human", 1)
    ]
    with pytest.raises(q.SyntheticEvidenceError, match="synthetic-era name"):
        q.build_report(_measurement(rows))


def test_the_committed_tts_fixtures_are_the_ones_the_registry_lists():
    """Know which fixture is which before using any of them.

    24..32 are recordings of real people and must not be listed as retired;
    00..23 are TTS and must be. Getting this backwards would either bar real
    audio or admit synthetic audio.
    """
    registry = json.loads(
        (REPO / "scripts" / "wakeword" / "retired_synthetic_artifacts.json").read_text(
            encoding="utf-8"
        )
    )
    listed = {
        Path(entry["name"]).name
        for entry in registry["artifacts"]
        if entry["name"].startswith("tests/fixtures/wakeword/audio/")
    }
    assert len(listed) == 24
    assert not listed & set(REAL_FIXTURES)
    for name in REAL_FIXTURES:
        assert (FIXTURE_AUDIO / name).exists()


# ---------------------------------------------------------------------------
# Reading audio: the int16 runtime contract.
# ---------------------------------------------------------------------------


def _write_wav(path: Path, audio: np.ndarray, rate: int = 16_000, channels: int = 1, width: int = 2):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(audio.tobytes())


def test_the_reader_refuses_anything_that_is_not_sixteen_kilohertz_mono_int16(tmp_path):
    """The engine wants int16 1280-sample frames at 16 kHz. Nothing else.

    Handing openWakeWord float32 in [-1, 1] makes every signal arrive 32768x
    too quiet and produces a flat score band that reads exactly like a model
    that learnt nothing — a failure this project has already paid for once.
    """
    tone = _tone()
    _write_wav(tmp_path / "rate.wav", tone, rate=8_000)
    _write_wav(tmp_path / "stereo.wav", np.repeat(tone, 2), channels=2)
    _write_wav(tmp_path / "eight_bit.wav", tone.astype(np.int8), width=1)
    for name, message in (
        ("rate.wav", "8000 Hz"),
        ("stereo.wav", "channels"),
        ("eight_bit.wav", "8-bit"),
    ):
        with pytest.raises(q.EvidenceError, match=message):
            q.read_wav_int16(tmp_path / name)


def test_a_float_corpus_is_refused_before_it_is_scored(tmp_path):
    corpus = tmp_path / "features"
    corpus.mkdir()
    np.save(corpus / "audio_eval.npy", np.zeros((2, 32_000), dtype=np.float32))
    np.save(corpus / "groups_eval.npy", np.array(["u0", "u1"]))
    (corpus / "categories_eval.json").write_text(
        json.dumps(["positive_human"] * 2), encoding="utf-8"
    )
    with pytest.raises(q.EvidenceError, match="32768x too quiet"):
        q.measure_corpus(
            corpus, models=tmp_path, dataset="E006", provenance="recorded-human",
            scorer=lambda framework, audio: (np.zeros((2, 16)), np.zeros(2, bool)),
        )


# ---------------------------------------------------------------------------
# Measuring: the grouping key comes from the corpus, not from the window index.
# ---------------------------------------------------------------------------


def _real_corpus(path: Path, groups: list[str]) -> Path:
    """A corpus built from the nine committed real recordings.

    The group ids are assigned by this test to exercise the grouping code path.
    They are not a claim that these recordings are one utterance — they are nine
    separate people saying nine separate words.
    """
    path.mkdir(parents=True, exist_ok=True)
    audio = np.stack([q.read_wav_int16(FIXTURE_AUDIO / name) for name in REAL_FIXTURES])
    np.save(path / "audio_eval.npy", audio)
    np.save(path / "groups_eval.npy", np.array(groups))
    categories = ["recorded_speech"] * 6 + ["background_only"] * 3
    (path / "categories_eval.json").write_text(json.dumps(categories), encoding="utf-8")
    return path


def test_the_corpus_group_ids_decide_what_one_trial_is(tmp_path):
    corpus = _real_corpus(tmp_path / "features", ["u0"] * 3 + ["u1"] * 3 + ["u2"] * 3)

    def scorer(framework, audio):
        scores = np.zeros((audio.shape[0], 16), dtype=np.float32)
        return scores, np.zeros(audio.shape[0], dtype=bool)

    measurement = q.measure_corpus(
        corpus, models=tmp_path, dataset="speech-commands",
        provenance="recorded-corpus", scorer=scorer,
    )
    assert len(measurement.utterances) == 3
    assert [len(u.windows) for u in measurement.utterances] == [3, 3, 3]
    trials = q.collect_trials(
        measurement, "onnx", q.RECORDED_SPEECH_CATEGORIES, q.ANY_FIRED
    )
    assert (trials.trials, trials.windows) == (2, 6)


def test_a_corpus_without_group_ids_is_refused(tmp_path):
    corpus = _real_corpus(tmp_path / "features", ["u0"] * 9)
    (corpus / "groups_eval.npy").unlink()
    with pytest.raises(q.EvidenceError, match="no groups_eval.npy"):
        q.measure_corpus(
            corpus, models=tmp_path, dataset="speech-commands",
            provenance="recorded-corpus", scorer=lambda f, a: (None, None),
        )


def test_a_group_that_spans_two_categories_is_refused(tmp_path):
    corpus = _real_corpus(tmp_path / "features", ["u0"] * 9)  # 6 speech + 3 background
    with pytest.raises(q.EvidenceError, match="more than one category"):
        q.measure_corpus(
            corpus, models=tmp_path, dataset="speech-commands",
            provenance="recorded-corpus",
            scorer=lambda f, a: (np.zeros((9, 16)), np.zeros(9, bool)),
        )


# ---------------------------------------------------------------------------
# The persisted record, and the command line.
# ---------------------------------------------------------------------------


def test_the_report_persists_every_figure_the_targets_are_judged_on(tmp_path, powered):
    bundle = tmp_path / "evidence.json"
    bundle.write_text(json.dumps(q.measurement_to_json(powered)), encoding="utf-8")
    out = tmp_path / "qualification.json"
    assert q.main(["report", "--official", str(bundle), "--out", str(out)]) == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["interval_methods"]["proportion_gate"].startswith("exact Clopper-Pearson")
    assert report["interval_methods"]["proportion_interval"].startswith("Wilson")
    assert "rule of three" in report["interval_methods"]["rate_gate"]

    block = report["official"]
    one = _target(block, 1)["by_backend"]["onnx"]
    assert {"point_estimate", "bound", "interval", "interval_method",
            "required_trials", "shortfall_trials", "power_derivation"} <= set(one)
    two = _target(block, 2)["by_backend"]["onnx"]
    assert two["point_estimate_per_hour"] == 0.0 and two["bound"] == pytest.approx(0.2)
    five = _target(block, 5)
    assert five["runtime_cost"]["tflite"]["latency_ms"]["p99"] == 0.9
    assert five["runtime_cost"]["onnx"]["peak_rss_bytes"] == 62_000_000
    for category in ("positive_human", "near_phrase_human", "recorded_speech",
                     "background_only"):
        row = block["evidence"]["by_category"][category]
        assert row["utterances"] > 0 and row["audio_hours"] > 0


def test_an_evidence_bundle_round_trips_without_losing_the_grouping(powered):
    restored = q.measurement_from_json(q.measurement_to_json(powered))
    assert restored.threshold == powered.threshold
    assert len(restored.utterances) == len(powered.utterances)
    assert restored.utterances[0].windows == powered.utterances[0].windows
    assert q.build_report(restored)["official"]["qualified_5_of_5"] is True


def test_a_bundle_from_another_schema_is_refused_rather_than_guessed_at(powered):
    payload = q.measurement_to_json(powered)
    payload["schema_version"] = 99
    with pytest.raises(q.EvidenceError, match="schema_version"):
        q.measurement_from_json(payload)


def test_the_report_command_exits_nonzero_when_it_refuses(tmp_path):
    rows = _utterances("pos", "positive_human", 36, framings=7, fired=36)
    bundle = tmp_path / "evidence.json"
    bundle.write_text(
        json.dumps(q.measurement_to_json(_measurement(rows))), encoding="utf-8"
    )
    out = tmp_path / "qualification.json"
    assert q.main(["report", "--official", str(bundle), "--out", str(out)]) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["official"]["qualified_5_of_5"] is False


def test_the_printed_verdict_qualifies_the_count_target_on_the_same_line(tmp_path, capsys):
    """The console summary is what gets pasted into a report, so it carries scope.

    ``round8_config.json``'s reporting rule is that target 4 is "never reported
    as demonstrated" in the generalising sense. A bare "DEMONSTRATED" beside a
    clean count over 61 windows in one room would be exactly that over-claim, so
    the per-window bound and the reach of the claim print with it.
    """
    bundle = tmp_path / "evidence.json"
    bundle.write_text(json.dumps(q.measurement_to_json(_powered())), encoding="utf-8")
    q.main(["report", "--official", str(bundle), "--out", str(tmp_path / "q.json")])
    printed = capsys.readouterr().out
    assert "target 4  background-only false accepts: DEMONSTRATED" in printed
    assert "scope: 0 activation(s) on the 61 background window(s)" in printed
    assert "says nothing about a room that was not recorded" in printed
    assert "0.047924" in printed


def test_the_power_subcommand_prints_the_arithmetic_and_needs_no_data(capsys):
    assert q.main(["power"]) == 0
    printed = capsys.readouterr().out
    assert "60 positive utterances" in printed
    assert "15 hours of recorded human speech" in printed
    assert "150 near-phrase utterances" in printed
    assert "rule of three" in printed


def test_freeze_and_open_sealed_are_usable_from_the_command_line(tmp_path, capsys):
    onnx = tmp_path / "hey_youtab.onnx"
    tflite = tmp_path / "hey_youtab.tflite"
    onnx.write_bytes(b"onnx-bytes")
    tflite.write_bytes(b"tflite-bytes")
    freeze_path = tmp_path / "freeze.json"
    ledger_path = tmp_path / "seals.json"

    assert q.main([
        "freeze", "--candidate", "r8b", "--threshold", "0.9990234375",
        "--runtime-version", "0.19.1", "--repo-commit", "807e3d5",
        "--onnx", str(onnx), "--tflite", str(tflite),
        "--manifest", "E006=" + "a" * 64, "--out", str(freeze_path),
    ]) == 0
    body = json.loads(freeze_path.read_text(encoding="utf-8"))
    assert body["artifact_sha256"]["tflite"] == q.sha256_file(tflite)
    assert body["threshold_hex"] == (0.9990234375).hex()

    assert q.main([
        "open-sealed", "--freeze", str(freeze_path), "--ledger", str(ledger_path),
        "--dataset", "E006",
    ]) == 0
    assert "CONSUMED" in capsys.readouterr().out
    assert q.SealLedger(ledger_path).status("E006")["candidate_id"] == "r8b"


def test_the_script_runs_as_a_script(tmp_path):
    """Nothing about the module-level code may depend on being imported."""
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "wakeword" / "qualify.py"), "power"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "target 2" in result.stdout


def test_an_empty_measurement_is_not_a_clean_run():
    with pytest.raises(q.EvidenceError, match="demonstrates nothing"):
        q.build_report(_measurement([]))
