#!/usr/bin/env python3
"""Decide whether the five production targets are *demonstrated*, not whether the numbers look good.

Why this is a separate tool from ``evaluate_model.py``
-----------------------------------------------------
``evaluate_model.py`` measures. It streams the corpus through
``tools.wake_word._OpenWakeWordEngine`` and reports false-reject and
false-accept rates per category, per backend, per threshold. Every number in it
is a point estimate, and a point estimate cannot say whether it is evidence.

That distinction is not academic here. Every ``0.000/h`` this project has ever
recorded for the recorded-speech target came from the Speech Commands eval
partition, which is 11.659 hours of window time. Zero events in 11.659 hours
bounds the rate at 3/11.659 = 0.257/h at 95% confidence — *above* the 0.2/h
target. The measurement was clean, the arithmetic was right, and the claim was
still unsupported: one observed event would have put it at 0.086/h and the
difference between those two worlds was never in the data. This harness exists
so that number can never again be printed next to the word "pass".

So the question it answers is not "is the estimate under the limit" but "does
the evidence present bound the quantity below the limit". Those differ exactly
when the sample is too small, which is the normal case for a wake word measured
on a handful of consented speakers.

The five targets, unchanged
---------------------------
1. false rejects <= 5% of positive utterances
2. recorded-speech false activations <= 0.2 per hour
3. deliberate near-miss false accepts <= 2% of near-phrase utterances
4. background-only false accepts = 0
5. ONNX/TFLite parity: zero detection disagreements, latency and memory measured

Derived power requirements, with their arithmetic
------------------------------------------------
Nothing below is a chosen number; each is solved from the target itself at
alpha = 0.05, and printed by ``qualify.py power`` so it can be checked by hand.

**Target 2, a rate.** Observing zero events in *t* hours bounds the rate at
95% by the one-sided exact Poisson limit -ln(0.05)/t = 2.99573/t, or by the
rule of three's rounder 3/t. Requiring that bound to sit at or below 0.2/h
gives t >= 14.979 h exactly and t >= 15.0 h by the rule of three; this harness
requires the larger, **15 hours**. The 11.659 h that produced every historical
"0.000/h" is 3.34 h short and can only ever bound 0.257/h.

**Targets 1 and 3, proportions.** The exact Clopper-Pearson one-sided 95%
upper limit for zero events in *n* trials is 1 - 0.05^(1/n). Solving
1 - 0.05^(1/n) <= 0.05 gives n >= 58.4, so **n >= 59**; the rule of three's
3/0.05 gives 60, and the larger is required: **60 positive utterances**.
Solving <= 0.02 gives n >= 148.3, so n >= 149; 3/0.02 = 150, and the larger is
required: **150 near-phrase utterances**. Those are the counts for a *clean*
run. One observed event needs more, which is why the gate is the interval and
not the count: the count is only the necessary condition, checked first so an
underpowered set is refused before its point estimate is ever compared.

**Targets 4 and 5 are bounded per window, and no sample size is invented for
them.** Both are ``== 0`` targets, and the predeclared design bounds both by
window count rather than by time: ``target_4.clean_run_bound_per_window`` is
0.047924 at n = 61 and ``target_5.clean_run_bound_by_n`` runs from 0.002991 at
n = 1,000 to 9.4e-05 at n = 31,986. Every entry in both tables is the exact
per-window Clopper-Pearson limit 1 - 0.05^(1/n), and this module reproduces all
eight of them.

So target 4 is gated as what the design says it is — ``demonstrable_as_a_rate:
false``, "met or not met as a count; it is a claim about one or two rooms" — and
target 5 is gated on zero disagreements, which the design makes fatal, plus full
window coverage and latency and memory present. Neither gets a minimum *n*,
because the design declares none and substituting one here would move the bar a
result is judged against after the fact. What replaces it is disclosure: every
row prints its per-window bound, target 4 carries
``generalises_beyond_the_windows_measured: False``, and the per-frame score
delta is reported and not gated, exactly as ``frame_score_delta: "reported, not
gated"`` requires.

An earlier version of this file inherited target 2's 0.2/h bar for both, in
hours. It is recorded here because the reasoning was seductive — parity should
not be the loosest link — and it was still wrong twice over: it contradicted
``demonstrable_as_a_rate: false`` head-on, and it failed a corpus the design
expected to pass. A harness that moves a predeclared bar mid-flight is the same
defect as a threshold retuned on a sealed set, and it is the reason rounds 6 and
7's negative results can be trusted at all.

The unit of evidence is the utterance
-------------------------------------
``build_dataset`` cuts seven deterministic framings from one spoken phrase.
Seven framings of one utterance are **one trial**: they are the same person
saying the same thing into the same microphone once. Counting them as seven
multiplies *n* by seven, shrinks every interval by sqrt(7), and turns 36
utterances into a 252-trial claim that no recording session supports. So every
count here is taken over a grouping key -- ``Utterance.utterance_id`` -- and the
aggregation rules are the ones ``round8_config.json`` predeclares: a positive
utterance is a miss if none of its framings fires, a near phrase is a false
accept if any of its framings fires. Duration is accumulated per utterance for
the same reason: summing the duration of overlapping framings would inflate the
hours in the denominator of the one target measured per hour.

Interval methods, named because a bound without its method is not a bound
------------------------------------------------------------------------
* **Gating bound, proportions:** exact Clopper-Pearson one-sided 95% upper
  limit. This is the method ``round8_config.json`` predeclares, and its
  clean-run figures (0.0798 at n=36, 0.0521 at n=56) are reproduced here.
* **Reported interval, proportions:** Wilson score, two-sided 95%. Also the
  predeclared method: it reproduces the record's [0.0237, 0.0969] for 7/144.
* **Gating bound, rates:** the more conservative of the exact one-sided 95%
  Poisson upper limit and the rule of three.
* **Reported interval, rates:** exact Poisson, two-sided 95%.

A two-sided 95% gate would be stricter still (72 and 183 trials rather than 60
and 150). That is not the predeclared bar and is not applied, but it is
computed and reported so the choice is visible rather than assumed.

Separation, freezing and consumption
------------------------------------
* **As-recorded is official; level-matched is diagnostic.** Not a flag on one
  record -- two blocks, built by different functions, and the diagnostic block
  is asserted to carry no verdict at all.
* **Freeze before opening a seal.** Candidate, threshold at full precision,
  confirmation frames, runtime version, repository commit, every manifest
  digest and *both* backend artifact digests. An incomplete freeze is a
  refusal, so a sealed set cannot be measured against a candidate that could
  still change.
* **Opening a sealed set spends it.** The ledger records the freeze digest, and
  a second opening under any different freeze -- a retuned threshold, a
  re-exported artifact, another candidate -- is refused. A failed candidate
  cannot come back after tuning and use the same sealed set as its final
  qualification.

Synthetic audio cannot become evidence
--------------------------------------
Three independent layers, none of which is a naming convention:

1. **Provenance whitelist.** Only ``recorded-human`` and ``recorded-corpus``
   utterances are evidence. Anything else -- including the tones the unit tests
   use to exercise the arithmetic -- is refused by ``qualify()`` before a single
   verdict is computed.
2. **Content addressing.** Every declared ``source_sha256`` is looked up in
   ``retired_synthetic_artifacts.json``. A byte-identical copy of a retired
   artifact is refused wherever it sits and whatever it is called.
3. **A tone guard on the audio itself.** ``spectral_concentration`` refuses
   audio with at least half its AC energy in one FFT bin and its two
   neighbours. Measured: the 33 committed real fixtures top out at 0.162, a
   pure sine reads >= 0.9996. This is a *tone* guard and is honest about its
   scope -- synthesized speech reads 0.018-0.063, indistinguishable from real
   speech, which is what layers 1 and 2 are for.

Usage::

    qualify.py power
    qualify.py freeze --candidate r8b --threshold 0.9991 --out freeze.json \\
        --onnx candidates/r8b/hey_youtab.onnx --tflite candidates/r8b/hey_youtab.tflite \\
        --manifest E006=<sha256> --manifest E007=<sha256> \\
        --runtime-version 0.19.1 --repo-commit <sha>
    qualify.py open-sealed --freeze freeze.json --ledger seals.json --dataset E006
    qualify.py measure --corpus data/features_r8b --dataset E006 \\
        --provenance recorded-human --models candidates/r8b --freeze freeze.json \\
        --ledger seals.json --out evidence_E006.json
    qualify.py report --official evidence_E006.json --diagnostic evidence_E006_lm.json \\
        --freeze freeze.json --ledger seals.json --out qualification.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parents[2]

#: One-sided confidence level for every gating bound. 0.05 is what
#: ``round8_config.json`` predeclares and what the rule of three's 3 encodes.
ALPHA = 0.05

#: The rule of three's numerator. -ln(0.05) = 2.99573; the rounder 3 is
#: conventional and slightly conservative, and this harness gates on whichever
#: of the two is stricter rather than picking one.
RULE_OF_THREE = 3.0

#: Two-sided 95% normal quantile, for the Wilson interval.
WILSON_Z = 1.959963984540054

#: The token every for-want-of-evidence refusal carries, so a report can say
#: which targets were refused for sample size rather than for quality. Written
#: in one place and matched in one place.
UNDERPOWERED = "underpowered"

FRAME = 1280
SAMPLE_RATE = 16000

# --------------------------------------------------------------------------
# The targets. Limits are the product's, not this file's; nothing here may
# weaken one, and `comparison` is carried so a report states the direction it
# tested rather than implying it.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    id: int
    name: str
    limit: float
    comparison: str
    unit: str
    kind: str  # proportion | rate | count | parity


TARGETS: tuple[Target, ...] = (
    Target(1, "false rejects", 0.05, "<=", "fraction of positive utterances", "proportion"),
    Target(2, "recorded-speech false activations", 0.2, "<=", "per hour", "rate"),
    Target(3, "deliberate near-miss false accepts", 0.02, "<=",
           "fraction of near-phrase utterances", "proportion"),
    Target(4, "background-only false accepts", 0.0, "==", "count", "count"),
    Target(5, "ONNX/TFLite detection parity", 0.0, "==",
           "detection disagreements, with latency and memory measured", "parity"),
)

TARGETS_BY_ID: Mapping[int, Target] = {target.id: target for target in TARGETS}

#: Categories, in the names ``build_human_dataset.py`` emits. ``free_speech_human``
#: joins ``recorded_speech`` under target 2 because consented free conversation
#: is recorded human speech that is not the wake phrase, which is exactly what
#: that target counts.
POSITIVE_CATEGORY = "positive_human"
NEAR_PHRASE_CATEGORIES = frozenset({"near_phrase_human"})
RECORDED_SPEECH_CATEGORIES = frozenset({"recorded_speech", "free_speech_human"})
BACKGROUND_CATEGORIES = frozenset({"background_only"})
EVIDENCE_CATEGORIES = (
    frozenset({POSITIVE_CATEGORY})
    | NEAR_PHRASE_CATEGORIES
    | RECORDED_SPEECH_CATEGORIES
    | BACKGROUND_CATEGORIES
)

#: Synthetic-era category names with no real-derivation meaning. Mirrors
#: ``build_human_dataset.SYNTHETIC_CATEGORIES``; declared here rather than
#: imported because that module is 1700 lines of ingest and this one must stay
#: importable without it. The name is not the guard -- provenance is -- but a
#: row carrying one of these was written by a tool that does not know this
#: contract, and half-understanding it is worse than refusing it.
SYNTHETIC_CATEGORIES = frozenset(
    {"positive", "hardneg", "confusable", "softneg", "common", "common_speech",
     "synthesized_speech", "near_phrase", "free_speech"}
)

#: The only provenances that may be evidence.
EVIDENCE_PROVENANCE = frozenset({"recorded-human", "recorded-corpus"})

#: Conditionings, and which one may be official. Level matching rescales the
#: audio and answers a diagnostic question ("is the failure a level failure");
#: it is not what the user's microphone delivers, so it can never carry a
#: verdict.
AS_RECORDED = "as-recorded"
LEVEL_MATCHED = "level-matched"
CONDITIONINGS = (AS_RECORDED, LEVEL_MATCHED)

#: At or above this share of AC spectral energy in one bin and its two
#: neighbours, audio is a tone and not speech. Measured margin: worst of the 33
#: committed real fixtures 0.162, any pure sine >= 0.9996.
TONE_CONCENTRATION_LIMIT = 0.5

RETIRED_REGISTRY = Path(__file__).resolve().with_name("retired_synthetic_artifacts.json")

_EPS = 3e-16
_FPMIN = 1e-300


class QualificationError(RuntimeError):
    """Base class for every refusal this harness makes."""


class SyntheticEvidenceError(QualificationError):
    """Something that cannot be evidence was offered as evidence."""


class IncompleteFreezeError(QualificationError):
    """A sealed set was reached for before the candidate was pinned down."""


class SealedSetConsumedError(QualificationError):
    """A sealed set was opened once already, under a different freeze."""


class SealNotOpenError(QualificationError):
    """A sealed set is being measured without a recorded opening."""


class EvidenceError(QualificationError):
    """The evidence bundle is not self-consistent."""


# --------------------------------------------------------------------------
# Exact interval arithmetic, in the standard library only.
#
# scipy is not a dependency of this repository and a qualification bound is
# the last thing that should depend on an optional import resolving. These are
# the Numerical Recipes continued fractions for the regularized incomplete
# beta and gamma functions, inverted by bisection: ~120 lines, deterministic,
# and checked in the tests against closed forms (1 - alpha^(1/n) for a clean
# proportion, -ln(alpha) for a clean rate) and against published chi-square
# quantiles.
# --------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Lentz's continued fraction for the incomplete beta function."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """``I_x(a, b)`` — the Beta(a, b) CDF."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(p: float, a: float, b: float) -> float:
    """Inverse Beta CDF by bisection.

    Bisection rather than Newton: 200 halvings of [0, 1] reach the double's
    resolution, the CDF is monotone so it cannot diverge, and a qualification
    bound is computed once per report. Robustness is worth more here than
    iterations.
    """
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if regularized_incomplete_beta(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def clopper_pearson_upper(events: int, trials: int, alpha: float = ALPHA) -> float:
    """Exact one-sided upper limit on a proportion. 1.0 when every trial fired."""
    _check_counts(events, trials)
    if trials == 0:
        return 1.0
    if events >= trials:
        return 1.0
    return _beta_quantile(1.0 - alpha, events + 1, trials - events)


def clopper_pearson_lower(events: int, trials: int, alpha: float = ALPHA) -> float:
    """Exact one-sided lower limit on a proportion. 0.0 when nothing fired."""
    _check_counts(events, trials)
    if trials == 0 or events <= 0:
        return 0.0
    return _beta_quantile(alpha, events, trials - events + 1)


def wilson_interval(
    events: int, trials: int, z: float = WILSON_Z
) -> tuple[float, float]:
    """Two-sided Wilson score interval.

    Reported rather than gated on. It is the interval ``round8_config.json``
    records (its [0.0237, 0.0969] for 7/144 is reproduced by this function),
    it behaves at small *n* where the normal approximation does not, and it is
    what a reader recognises as a confidence interval.
    """
    _check_counts(events, trials)
    if trials == 0:
        return (0.0, 1.0)
    p = events / trials
    denominator = 1.0 + z * z / trials
    centre = (p + z * z / (2.0 * trials)) / denominator
    half = (z / denominator) * math.sqrt(p * (1.0 - p) / trials + z * z / (4.0 * trials * trials))
    return (max(0.0, centre - half), min(1.0, centre + half))


def _regularized_lower_gamma(a: float, x: float) -> float:
    """``P(a, x)`` — the Gamma(a, 1) CDF, series below the crossover and CF above."""
    if x <= 0.0:
        return 0.0
    if x < a + 1.0:
        ap = a
        total = delta = 1.0 / a
        for _ in range(1000):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * _EPS:
                break
        return total * math.exp(-x + a * math.log(x) - math.lgamma(a))
    b = x + 1.0 - a
    c = 1.0 / _FPMIN
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = b + an / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return 1.0 - math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def _gamma_quantile(p: float, a: float) -> float:
    """Inverse Gamma(a, 1) CDF by bisection, bracket grown until it contains p."""
    lo, hi = 0.0, max(1.0, a)
    while _regularized_lower_gamma(a, hi) < p:
        hi *= 2.0
        if hi > 1e12:  # pragma: no cover - unreachable for the alphas used here
            break
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _regularized_lower_gamma(a, mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def poisson_upper_mean(events: int, alpha: float = ALPHA) -> float:
    """Exact one-sided upper limit on a Poisson mean.

    Solves ``P(X <= k | mu) = alpha``. For k = 0 this is -ln(alpha) = 2.99573,
    which is the exact number the rule of three rounds to 3.
    """
    if events < 0:
        raise ValueError("events must not be negative")
    return _gamma_quantile(1.0 - alpha, events + 1)


def poisson_lower_mean(events: int, alpha: float = ALPHA) -> float:
    """Exact one-sided lower limit on a Poisson mean. 0.0 for zero events."""
    if events < 0:
        raise ValueError("events must not be negative")
    if events == 0:
        return 0.0
    return _gamma_quantile(alpha, events)


def rate_upper_bound(events: int, hours: float, alpha: float = ALPHA) -> dict:
    """Per-hour rate bound, by both methods, with the conservative one named.

    An empty observation window is not a rate of zero; it is no information,
    and it is reported as an unbounded rate rather than as a pass.
    """
    if hours <= 0.0:
        return {
            "hours": float(hours),
            "events": int(events),
            "point_estimate_per_hour": None,
            "poisson_exact_upper_per_hour": None,
            "rule_of_three_upper_per_hour": None,
            "bound_per_hour": None,
            "bound_method": "none — zero hours of audio bounds nothing",
            "interval_per_hour": [None, None],
            "interval_method": "exact Poisson, two-sided 95%",
        }
    exact = poisson_upper_mean(events, alpha) / hours
    # The rule of three is a zero-event statement; above zero events the exact
    # Poisson limit is the only one of the two that is defined, and reporting
    # 3/t there would understate the bound.
    rule = (RULE_OF_THREE / hours) if events == 0 else None
    bound = max(exact, rule) if rule is not None else exact
    method = (
        "rule of three (3/t), the more conservative of it and the exact "
        "one-sided 95% Poisson limit"
        if rule is not None and rule >= exact
        else "exact one-sided 95% Poisson upper limit"
    )
    return {
        "hours": float(hours),
        "events": int(events),
        "point_estimate_per_hour": events / hours,
        "poisson_exact_upper_per_hour": exact,
        "rule_of_three_upper_per_hour": rule,
        "bound_per_hour": bound,
        "bound_method": method,
        "interval_per_hour": [
            poisson_lower_mean(events, alpha / 2.0) / hours,
            poisson_upper_mean(events, alpha / 2.0) / hours,
        ],
        "interval_method": "exact Poisson, two-sided 95%",
    }


def proportion_bound(events: int, trials: int, alpha: float = ALPHA) -> dict:
    """Proportion bound and interval, each with its method named."""
    _check_counts(events, trials)
    if trials == 0:
        return {
            "trials": 0,
            "events": int(events),
            "point_estimate": None,
            "bound": None,
            "bound_method": "none — zero trials bounds nothing",
            "interval": [None, None],
            "interval_method": "Wilson score, two-sided 95%",
            "clopper_pearson_two_sided_95": [None, None],
        }
    return {
        "trials": int(trials),
        "events": int(events),
        "point_estimate": events / trials,
        "bound": clopper_pearson_upper(events, trials, alpha),
        "bound_method": "exact Clopper-Pearson one-sided 95% upper limit",
        "interval": list(wilson_interval(events, trials)),
        "interval_method": "Wilson score, two-sided 95%",
        "clopper_pearson_two_sided_95": [
            clopper_pearson_lower(events, trials, alpha / 2.0),
            clopper_pearson_upper(events, trials, alpha / 2.0),
        ],
    }


def _g(value: float | None) -> str:
    """Format a bound for a human-readable claim, or say it does not exist.

    A bound of ``None`` means no evidence, and "no evidence" must not render as
    a number — least of all as a zero.
    """
    return "n/a (no evidence)" if value is None else f"{value:.6g}"


def _check_counts(events: int, trials: int) -> None:
    if trials < 0 or events < 0:
        raise ValueError("counts must not be negative")
    if events > trials:
        raise ValueError(f"{events} events in {trials} trials is impossible")


# --------------------------------------------------------------------------
# Power: how much evidence each target needs before it can be demonstrated at
# all. Solved from the target, not chosen, and printed with the arithmetic.
# --------------------------------------------------------------------------


def minimum_trials_for_proportion(limit: float, alpha: float = ALPHA) -> dict:
    """Smallest *n* whose best possible outcome bounds a proportion at ``limit``.

    "Best possible" is zero events; anything worse needs more trials. So this is
    a necessary condition and never a sufficient one, which is why the verdict
    checks the interval as well.
    """
    if not 0.0 < limit < 1.0:
        raise ValueError("a proportion limit must be strictly between 0 and 1")
    exact = 1
    while clopper_pearson_upper(0, exact, alpha) > limit:
        exact += 1
    rule = math.ceil(RULE_OF_THREE / limit)
    two_sided = 1
    while clopper_pearson_upper(0, two_sided, alpha / 2.0) > limit:
        two_sided += 1
    return {
        "required_trials": max(exact, rule),
        "exact_clopper_pearson_trials": exact,
        "rule_of_three_trials": rule,
        "required_trials_if_two_sided_95": two_sided,
        "clean_run_bound_at_required": clopper_pearson_upper(0, max(exact, rule), alpha),
        "derivation": (
            f"1 - {alpha}^(1/n) <= {limit} solves to n >= "
            f"{math.log(alpha) / math.log1p(-limit):.4f}, so n >= {exact} exactly; "
            f"the rule of three gives ceil({RULE_OF_THREE}/{limit}) = {rule}; "
            f"the larger, {max(exact, rule)}, is required. A two-sided 95% gate "
            f"would need {two_sided}."
        ),
    }


def minimum_hours_for_rate(limit: float, alpha: float = ALPHA) -> dict:
    """Hours of audio whose best possible outcome bounds a rate at ``limit``."""
    if limit <= 0.0:
        raise ValueError("a rate limit must be positive")
    exact_mean = poisson_upper_mean(0, alpha)
    exact_hours = exact_mean / limit
    rule_hours = RULE_OF_THREE / limit
    required = max(exact_hours, rule_hours)
    return {
        "required_hours": required,
        "exact_poisson_hours": exact_hours,
        "rule_of_three_hours": rule_hours,
        "clean_run_bound_at_required": RULE_OF_THREE / required,
        "derivation": (
            f"zero events in t hours bounds the rate at {exact_mean:.5f}/t exactly "
            f"(= -ln({alpha})/t) or {RULE_OF_THREE}/t by the rule of three; "
            f"requiring that <= {limit}/h gives t >= {exact_hours:.4f} h and "
            f"t >= {rule_hours:.4f} h respectively, so {required:.4f} h is required"
        ),
    }


def power_requirements() -> dict:
    """The derived requirement for every target, data-independent."""
    target_1 = minimum_trials_for_proportion(TARGETS_BY_ID[1].limit)
    target_2 = minimum_hours_for_rate(TARGETS_BY_ID[2].limit)
    target_3 = minimum_trials_for_proportion(TARGETS_BY_ID[3].limit)
    # Targets 4 and 5 are bounded per WINDOW, not per hour.
    #
    # An earlier version of this function had both inherit target 2's 0.2/h bar
    # in hours. That reasoning is appealing -- parity should not be the loosest
    # link -- and it contradicts the predeclared design, which is binding here
    # precisely so that a mid-flight improvement cannot rewrite the bar a result
    # will be judged against. `round8_config.json` records
    # `target_4.clean_run_bound_per_window` and
    # `target_5.clean_run_bound_by_n/<n>`, both indexed by window count, and
    # says of target 4: `demonstrable_as_a_rate: false`, "met or not met as a
    # count; it is a claim about one or two rooms".
    #
    # It is also the better statistic. A parity disagreement can occur on any
    # window, not only during speech, so decisions are its natural denominator;
    # converting to hours divides by an arbitrary frame rate and then compares
    # against a limit defined for a different event class. Gating on hours made
    # Round 8's projected corpus fail target 5 on a technicality while its 31,986
    # compared windows bound the disagreement rate at 9.4e-05.
    # No minimum n is invented for either. The predeclared design does not
    # declare one, and inventing a threshold here would repeat the mistake this
    # comment describes -- replacing one unpredeclared bar with another. What is
    # gated is what the design actually states: target 4 is met or not met as a
    # count, target 5 requires zero disagreements and is fatal. Both report their
    # clean-run per-window bound so a reader can see how weak or strong the
    # evidence is, and target 4 additionally carries the design's own
    # `demonstrable_as_a_rate: false`.
    return {
        "alpha": ALPHA,
        "sided": "one-sided for every gating bound",
        "unit_of_evidence": "utterance; several framings of one utterance are one trial",
        "1": {**target_1, "unit": "positive utterances"},
        "2": {**target_2, "unit": "hours of recorded human speech"},
        "3": {**target_3, "unit": "near-phrase utterances"},
        "4": {
            "minimum": 1,
            "unit": "background-only windows",
            "gate": "count: met when zero activations occurred on the windows present",
            "demonstrable_as_a_rate": False,
            "why_per_window": (
                "the predeclared design records target 4 with a per-window "
                "clean-run bound and `demonstrable_as_a_rate: false` -- \"met or "
                "not met as a count; it is a claim about one or two rooms\". The "
                "per-window bound is reported so the weakness of that claim is "
                "visible, and it is deliberately not converted into an hourly rate"
            ),
        },
        "5": {
            "minimum": 1,
            "unit": "windows compared on both backends",
            "gate": "zero detection disagreements; the design makes one fatal",
            "why_per_window": (
                "zero disagreements over n decisions bounds the disagreement rate "
                "at 3/n per DECISION. A disagreement can occur on any window, not "
                "only during speech, so windows are the denominator the "
                "predeclared design uses (`clean_run_bound_by_n`)"
            ),
            "also_required": [
                "every window of the official evidence compared on both backends",
                "latency p50/p95/p99 present per backend",
                "peak memory present per backend",
            ],
        },
    }


# --------------------------------------------------------------------------
# Evidence model. Utterance is the unit; Window is a framing of one.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameDelta:
    """Per-frame ONNX-vs-TFLite score differences for one window.

    Aggregates rather than the raw vector: the max and the sum plus the frame
    count reconstruct both figures the target asks for exactly, and an evidence
    bundle stays a file a human can open.
    """

    max_abs: float
    sum_abs: float
    frames: int


@dataclass(frozen=True)
class Window:
    """One framing of one utterance, as the product engine decided it."""

    window_index: int
    fired: Mapping[str, bool]
    delta: FrameDelta | None = None


@dataclass(frozen=True)
class Utterance:
    """The unit of evidence: one thing a person said, once."""

    utterance_id: str
    category: str
    provenance: str
    audio_seconds: float
    windows: tuple[Window, ...]
    dataset: str = ""
    speaker: str = ""
    source_sha256: str = ""


@dataclass(frozen=True)
class Measurement:
    """One conditioning of one candidate over one or more datasets."""

    conditioning: str
    backends: tuple[str, ...]
    threshold: float
    confirmation_frames: int
    candidate_id: str
    utterances: tuple[Utterance, ...]
    runtime_cost: Mapping[str, Mapping] = field(default_factory=dict)
    sealed_datasets: tuple[str, ...] = ()
    window_seconds: float = 2.0
    note: str = ""

    def datasets(self) -> tuple[str, ...]:
        return tuple(sorted({u.dataset for u in self.utterances if u.dataset}))


def measurement_to_json(measurement: Measurement) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "scripts/wakeword/qualify.py",
        "conditioning": measurement.conditioning,
        "backends": list(measurement.backends),
        "threshold": measurement.threshold,
        "threshold_hex": float(measurement.threshold).hex(),
        "confirmation_frames": measurement.confirmation_frames,
        "candidate_id": measurement.candidate_id,
        "window_seconds": measurement.window_seconds,
        "sealed_datasets": list(measurement.sealed_datasets),
        "runtime_cost": dict(measurement.runtime_cost),
        "note": measurement.note,
        "utterances": [
            {
                "utterance_id": u.utterance_id,
                "category": u.category,
                "provenance": u.provenance,
                "audio_seconds": u.audio_seconds,
                "dataset": u.dataset,
                "speaker": u.speaker,
                "source_sha256": u.source_sha256,
                "windows": [
                    {
                        "window_index": w.window_index,
                        "fired": dict(w.fired),
                        **(
                            {"delta": {"max_abs": w.delta.max_abs,
                                       "sum_abs": w.delta.sum_abs,
                                       "frames": w.delta.frames}}
                            if w.delta is not None
                            else {}
                        ),
                    }
                    for w in u.windows
                ],
            }
            for u in measurement.utterances
        ],
    }


def measurement_from_json(payload: Mapping) -> Measurement:
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise EvidenceError(
            f"evidence bundle is schema_version {version!r}; this tool reads "
            f"{SCHEMA_VERSION}. Refusing to interpret it."
        )
    utterances = []
    for row in payload.get("utterances", []):
        windows = tuple(
            Window(
                window_index=int(w["window_index"]),
                fired={str(k): bool(v) for k, v in w["fired"].items()},
                delta=(
                    FrameDelta(
                        max_abs=float(w["delta"]["max_abs"]),
                        sum_abs=float(w["delta"]["sum_abs"]),
                        frames=int(w["delta"]["frames"]),
                    )
                    if w.get("delta") is not None
                    else None
                ),
            )
            for w in row["windows"]
        )
        utterances.append(
            Utterance(
                utterance_id=str(row["utterance_id"]),
                category=str(row["category"]),
                provenance=str(row["provenance"]),
                audio_seconds=float(row["audio_seconds"]),
                windows=windows,
                dataset=str(row.get("dataset", "")),
                speaker=str(row.get("speaker", "")),
                source_sha256=str(row.get("source_sha256", "")),
            )
        )
    threshold = (
        float.fromhex(payload["threshold_hex"])
        if payload.get("threshold_hex")
        else float(payload["threshold"])
    )
    return Measurement(
        conditioning=str(payload["conditioning"]),
        backends=tuple(str(b) for b in payload["backends"]),
        threshold=threshold,
        confirmation_frames=int(payload["confirmation_frames"]),
        candidate_id=str(payload.get("candidate_id", "")),
        utterances=tuple(utterances),
        runtime_cost=dict(payload.get("runtime_cost", {})),
        sealed_datasets=tuple(str(d) for d in payload.get("sealed_datasets", [])),
        window_seconds=float(payload.get("window_seconds", 2.0)),
        note=str(payload.get("note", "")),
    )


# --------------------------------------------------------------------------
# Layer 1 and 2 of the synthetic bar: provenance, and content addressing.
# --------------------------------------------------------------------------


def retired_hashes(registry: Path = RETIRED_REGISTRY) -> dict[str, str]:
    """SHA-256 -> name for every artifact retired from this project.

    A missing registry is a refusal, not a pass. The registry is the only thing
    that recognises a synthetic artifact that has been renamed and moved, and a
    run that cannot read it cannot make the claim it is about to make.
    """
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyntheticEvidenceError(
            f"cannot read the retired-artifact registry ({registry.name}): {exc}. "
            "Refusing to qualify without it: it is what recognises a retired "
            "synthetic artifact that has been renamed."
        ) from exc
    entries = payload.get("artifacts")
    if not entries:
        raise SyntheticEvidenceError(
            f"{registry.name} lists no artifacts. An empty registry recognises "
            "nothing and would pass everything."
        )
    return {str(e["sha256"]).lower(): str(e.get("name", "?")) for e in entries}


def assert_admissible(
    measurement: Measurement, registry: Path = RETIRED_REGISTRY
) -> None:
    """Refuse anything that cannot be model evidence, before any statistic runs.

    Deliberately ordered ahead of every computation in ``qualify``: a refusal
    that arrives after the numbers have been printed has already lost, because
    the numbers are what gets copied into a report.
    """
    if measurement.conditioning not in CONDITIONINGS:
        raise EvidenceError(
            f"unknown conditioning {measurement.conditioning!r}; expected one of "
            f"{CONDITIONINGS}"
        )
    if not measurement.utterances:
        raise EvidenceError(
            "no utterances. An empty measurement demonstrates nothing and must "
            "not be reported as a clean run."
        )
    if not measurement.backends:
        raise EvidenceError("no backends named; parity is a two-backend claim")

    seen: set[str] = set()
    retired = retired_hashes(registry)
    for utterance in measurement.utterances:
        if utterance.utterance_id in seen:
            raise EvidenceError(
                f"duplicate utterance_id {utterance.utterance_id!r}. The grouping "
                "key is the unit of evidence; two rows sharing one would be "
                "counted as two trials of one utterance."
            )
        seen.add(utterance.utterance_id)

        if utterance.provenance not in EVIDENCE_PROVENANCE:
            raise SyntheticEvidenceError(
                f"{utterance.utterance_id}: provenance {utterance.provenance!r} "
                f"cannot be evidence. Only {sorted(EVIDENCE_PROVENANCE)} may be. "
                "Synthetic audio is retired from this project's training and "
                "qualification entirely."
            )
        if utterance.category in SYNTHETIC_CATEGORIES:
            raise SyntheticEvidenceError(
                f"{utterance.utterance_id}: category {utterance.category!r} is a "
                "synthetic-era name with no real-derivation meaning"
            )
        if utterance.category not in EVIDENCE_CATEGORIES:
            raise EvidenceError(
                f"{utterance.utterance_id}: category {utterance.category!r} is not "
                f"one of {sorted(EVIDENCE_CATEGORIES)}"
            )
        digest = utterance.source_sha256.lower()
        if digest and digest in retired:
            raise SyntheticEvidenceError(
                f"{utterance.utterance_id}: sha256 {digest} is the retired "
                f"artifact {retired[digest]!r}. A byte-identical copy is refused "
                "wherever it sits and whatever it is called."
            )
        if not utterance.windows:
            raise EvidenceError(f"{utterance.utterance_id}: no windows")
        if utterance.audio_seconds <= 0.0:
            raise EvidenceError(
                f"{utterance.utterance_id}: audio_seconds must be positive; hours "
                "are the denominator of the one target measured per hour"
            )
        # An utterance may not claim more audio than the windows that covered
        # it. This is the one-sided guard that matters: hours are a
        # *denominator*, so overstating them lowers a per-hour rate and makes a
        # target easier to pass, and it would also hide activations, because one
        # utterance contributes at most one event however long it claims to be.
        # Understating is safe and normal — seven overlapping framings cover 14
        # window-seconds of a 2-second phrase — so only the inflating direction
        # is refused.
        capacity = len(utterance.windows) * measurement.window_seconds
        if utterance.audio_seconds > capacity + 1e-6:
            raise EvidenceError(
                f"{utterance.utterance_id}: claims {utterance.audio_seconds:g} s of "
                f"audio but only {len(utterance.windows)} window(s) of "
                f"{measurement.window_seconds:g} s covered it. Hours are a "
                "denominator; inflating them lowers every per-hour rate, and an "
                "utterance can only ever contribute one activation event."
            )
        for window in utterance.windows:
            missing = set(measurement.backends) - set(window.fired)
            if missing:
                raise EvidenceError(
                    f"{utterance.utterance_id} window {window.window_index}: no "
                    f"decision recorded for {sorted(missing)}"
                )


# --------------------------------------------------------------------------
# Layer 3 of the synthetic bar: the audio itself.
# --------------------------------------------------------------------------


def spectral_concentration(samples: Sequence[float] | "object", neighborhood: int = 2) -> float:
    """Share of AC spectral energy in the strongest bin and its neighbours.

    A pure tone is one line in the spectrum; speech is never one line. This is
    the property that makes a generated tone *unable* to enter a measurement
    even if its provenance is mislabelled and its bytes are new, so it is a
    structural bar rather than a policy.

    Honest about its scope: it separates tones from speech, not synthesized
    speech from real speech. The committed TTS fixtures read 0.018-0.063, in
    the same band as the real recordings, and the guards for those are the
    provenance whitelist and the hash registry.
    """
    import numpy as np  # noqa: PLC0415 — keeps the statistics importable without numpy

    audio = np.asarray(samples, dtype=np.float64).reshape(-1)
    if audio.size < 8:
        return 0.0
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(audio.size))) ** 2
    spectrum[0] = 0.0  # DC is a level, not a tone
    total = float(spectrum.sum())
    if total <= 0.0:
        return 0.0
    peak = int(np.argmax(spectrum))
    lo = max(0, peak - neighborhood)
    hi = min(spectrum.size, peak + neighborhood + 1)
    return float(spectrum[lo:hi].sum() / total)


def assert_not_tone(samples, label: str, limit: float = TONE_CONCENTRATION_LIMIT) -> float:
    """Refuse tone-like audio as evidence, and say what it measured."""
    value = spectral_concentration(samples)
    if value >= limit:
        raise SyntheticEvidenceError(
            f"{label}: {value:.4f} of its spectral energy sits in one FFT bin and "
            f"its neighbours (limit {limit}). That is a tone, not speech. Tones "
            "exist in this project only to exercise arithmetic in unit tests and "
            "are structurally barred from becoming model evidence."
        )
    return value


def read_wav_int16(path: Path):
    """Read a 16 kHz mono 16-bit WAV as int16, refusing anything else.

    int16 and not float32, deliberately and loudly.
    ``_OpenWakeWordEngine.process`` hands the frame to openWakeWord, which
    expects the int16 range; float32 in [-1, 1] arrives 32768x too quiet and
    produces a flat low score band that looks exactly like a model that learnt
    nothing. That failure has cost this project time before, so the dtype is a
    contract here and not a convention.
    """
    import wave  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1:
            raise EvidenceError(f"{path.name}: {handle.getnchannels()} channels, expected mono")
        if handle.getsampwidth() != 2:
            raise EvidenceError(f"{path.name}: {handle.getsampwidth() * 8}-bit, expected 16")
        if handle.getframerate() != SAMPLE_RATE:
            raise EvidenceError(f"{path.name}: {handle.getframerate()} Hz, expected {SAMPLE_RATE}")
        raw = handle.readframes(handle.getnframes())
    audio = np.frombuffer(raw, dtype="<i2")
    if audio.dtype != np.int16:  # pragma: no cover - defensive
        raise EvidenceError(f"{path.name}: decoded to {audio.dtype}, expected int16")
    return audio


# --------------------------------------------------------------------------
# Freezing, and the ledger that makes opening a sealed set irreversible.
# --------------------------------------------------------------------------


def _canonical(body: Mapping) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: Every field a freeze must carry before a seal may be opened. Named as a
#: table so the refusal can list what is missing instead of failing on the
#: first one it happens to check.
FREEZE_REQUIRED = (
    "candidate_id",
    "threshold_hex",
    "confirmation_frames",
    "runtime_version",
    "repo_commit",
    "manifest_sha256",
    "artifact_sha256",
)

FREEZE_REQUIRED_BACKENDS = ("onnx", "tflite")


@dataclass(frozen=True)
class FreezeRecord:
    """Everything that must stop moving before a sealed set is opened."""

    candidate_id: str
    threshold: float
    confirmation_frames: int
    runtime_version: str
    repo_commit: str
    manifest_sha256: Mapping[str, str]
    artifact_sha256: Mapping[str, str]
    frozen_utc: str = ""
    note: str = ""

    def body(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            # Hex, not a decimal literal. The validation-selected threshold is
            # an observed negative score such as 0.9990234375; round-tripping
            # it through a shortened decimal would silently move the operating
            # point the sealed measurement was frozen at.
            "threshold_hex": float(self.threshold).hex(),
            "threshold_decimal": repr(float(self.threshold)),
            "confirmation_frames": int(self.confirmation_frames),
            "runtime_version": self.runtime_version,
            "repo_commit": self.repo_commit,
            "manifest_sha256": {k: v for k, v in sorted(self.manifest_sha256.items())},
            "artifact_sha256": {k: v for k, v in sorted(self.artifact_sha256.items())},
            "frozen_utc": self.frozen_utc or _utc_now(),
            "note": self.note,
        }

    def digest(self) -> str:
        """Identity of the freeze.

        ``frozen_utc`` is excluded on purpose: re-recording the same candidate,
        threshold and artifacts a minute later is the same freeze, and a
        timestamp inside the digest would make every re-read look like a new
        candidate and defeat the consumption check.
        """
        body = {k: v for k, v in self.body().items() if k != "frozen_utc"}
        return hashlib.sha256(_canonical(body)).hexdigest()

    def missing(self) -> list[str]:
        """Which required fields are absent or empty."""
        body = self.body()
        gaps = []
        for name in FREEZE_REQUIRED:
            value = body.get(name)
            if value in (None, "", {}, []):
                gaps.append(name)
        artifacts = body.get("artifact_sha256")
        if isinstance(artifacts, dict):
            for backend in FREEZE_REQUIRED_BACKENDS:
                if not artifacts.get(backend):
                    gaps.append(f"artifact_sha256.{backend}")
        for name, value in (body.get("manifest_sha256") or {}).items():
            if not _is_sha256(str(value)):
                gaps.append(f"manifest_sha256.{name} (not a sha256)")
        for name, value in (body.get("artifact_sha256") or {}).items():
            if not _is_sha256(str(value)):
                gaps.append(f"artifact_sha256.{name} (not a sha256)")
        return sorted(set(gaps))

    def assert_complete(self) -> None:
        gaps = self.missing()
        if gaps:
            raise IncompleteFreezeError(
                "the candidate is not frozen: "
                + ", ".join(gaps)
                + ". A sealed set may not be opened against something that can "
                "still change — the whole value of the set is that nobody has "
                "tuned against it, and a measurement traceable to nothing "
                "specific cannot show that."
            )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def freeze_from_json(payload: Mapping) -> FreezeRecord:
    threshold = (
        float.fromhex(payload["threshold_hex"])
        if payload.get("threshold_hex")
        else float(payload.get("threshold", "nan"))
    )
    return FreezeRecord(
        candidate_id=str(payload.get("candidate_id", "")),
        threshold=threshold,
        confirmation_frames=int(payload.get("confirmation_frames", 0)),
        runtime_version=str(payload.get("runtime_version", "")),
        repo_commit=str(payload.get("repo_commit", "")),
        manifest_sha256=dict(payload.get("manifest_sha256", {})),
        artifact_sha256=dict(payload.get("artifact_sha256", {})),
        frozen_utc=str(payload.get("frozen_utc", "")),
        note=str(payload.get("note", "")),
    )


class SealLedger:
    """A permanent record of which sealed sets have been spent, and on what.

    The rule this implements is not "warn twice". A sealed set's entire value is
    that no candidate has been tuned against it; measuring it publishes that
    information to whoever reads the result, and no amount of care puts it
    back. So the first opening is written down against the freeze digest, and a
    second opening under a different freeze — a retuned threshold, a
    re-exported artifact, a different arm — is refused. Re-reading the same
    freeze is idempotent, because that is the same measurement and not a second
    one.
    """

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise QualificationError(
                    f"{path.name} is schema_version {payload.get('schema_version')!r}; "
                    f"this tool reads {SCHEMA_VERSION}"
                )
            self.entries = dict(payload.get("sealed_sets", {}))

    def status(self, dataset: str) -> dict | None:
        return self.entries.get(dataset)

    def open_sealed(self, dataset: str, freeze: FreezeRecord) -> dict:
        """Spend a sealed set, or refuse. Persists before returning."""
        freeze.assert_complete()
        digest = freeze.digest()
        existing = self.entries.get(dataset)
        if existing is not None:
            if existing.get("freeze_digest") == digest:
                return existing
            raise SealedSetConsumedError(
                f"{dataset} was opened on {existing.get('opened_utc')} against "
                f"candidate {existing.get('candidate_id')!r} at threshold "
                f"{existing.get('threshold_decimal')} (freeze "
                f"{str(existing.get('freeze_digest'))[:12]}...). It is CONSUMED. "
                f"This freeze is {digest[:12]}..., a different candidate, "
                "threshold or artifact. A candidate that failed cannot be tuned "
                "and then qualified on the same sealed set: the set is no longer "
                "unseen, and re-recording the speaker does not restore it."
            )
        entry = {
            "dataset": dataset,
            "status": "CONSUMED",
            "opened_utc": _utc_now(),
            "freeze_digest": digest,
            "candidate_id": freeze.candidate_id,
            "threshold_hex": float(freeze.threshold).hex(),
            "threshold_decimal": repr(float(freeze.threshold)),
            "artifact_sha256": dict(sorted(freeze.artifact_sha256.items())),
        }
        self.entries[dataset] = entry
        self._persist()
        return entry

    def assert_open(self, dataset: str, freeze: FreezeRecord) -> dict:
        """Refuse to measure or report on a sealed set that was never opened."""
        entry = self.entries.get(dataset)
        if entry is None:
            raise SealNotOpenError(
                f"{dataset} is sealed and has no recorded opening. Freeze the "
                "candidate and open it explicitly (`qualify.py open-sealed`) so "
                "the moment it was spent is on the record."
            )
        if entry.get("freeze_digest") != freeze.digest():
            raise SealedSetConsumedError(
                f"{dataset} was opened against freeze "
                f"{str(entry.get('freeze_digest'))[:12]}..., not "
                f"{freeze.digest()[:12]}.... A sealed set measures one frozen "
                "candidate, once."
            )
        return entry

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "schema_version": SCHEMA_VERSION,
            "tool": "scripts/wakeword/qualify.py",
            "note": (
                "A sealed set listed here is spent. Nothing removes an entry; "
                "the file is the record that it was opened."
            ),
            "sealed_sets": dict(sorted(self.entries.items())),
        }
        # Write-then-replace: a ledger truncated by a crash mid-write would
        # read as "never opened", which is the one wrong answer it must not
        # give.
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        os.replace(temporary, self.path)


def _round8_config_path(config: Path | None) -> Path:
    return config if config is not None else Path(__file__).resolve().with_name(
        "round8_config.json"
    )


def known_datasets(config: Path | None = None) -> set[str]:
    """Every dataset name a measurement is allowed to carry.

    The speaker-split registry (``round8_config.json`` ``splits`` — the same
    E-ids ``build_human_dataset.SPEAKER_SPLITS`` binds) plus the recorded
    negative corpora keys. A name outside this set is either a typo or a
    seal-evasion, and either way cannot be scored into an official result. A
    config that cannot be read is a refusal, not an empty allow-list, for the
    same reason it is in ``sealed_datasets``.
    """
    path = _round8_config_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError(
            f"{path.name} is unreadable ({exc}); refusing to validate dataset "
            "names against a registry that is not there"
        ) from exc
    names: set[str] = set()
    splits = payload.get("splits", {})
    for role in ("train", "validation", "sealed"):
        names |= {str(member) for member in splits.get(role, [])}
    for entry in payload.get("recorded_negative_corpora", []):
        key = entry.get("key")
        if key:
            names.add(str(key))
    return names


def assert_known_dataset(name: str, config: Path | None = None) -> None:
    """Refuse a ``--dataset`` the split/corpus registry does not recognise.

    The seal check keys on the exact dataset string: ``sealed_datasets``
    intersects ``splits.sealed`` with ``{u.dataset}``. So an operator-typed name
    like ``E006_holdout`` — sealed audio under an unregistered label — makes that
    intersection empty and yields an OFFICIAL report over sealed audio with no
    freeze and no ledger. Validating the name against the registry before any
    audio is scored closes that path: a sealed speaker can only be measured under
    its registered id, which is the id the seal machinery watches.
    """
    allowed = known_datasets(config)
    if name not in allowed:
        raise QualificationError(
            f"dataset {name!r} is not a known Round 8 dataset. The registry is "
            f"{sorted(allowed)}; a name outside it evades the seal machinery "
            "(naming sealed audio 'E006_holdout' makes sealed_datasets() empty and "
            "produces an official report over sealed audio with no freeze). "
            "Refusing before any audio is scored."
        )


def sealed_datasets(measurement: Measurement, config: Path | None = None) -> tuple[str, ...]:
    """Which of the measured datasets are sealed — the union of two sources.

    The bundle's own declaration, plus ``round8_config.json``'s ``splits.sealed``
    when it is present. A union rather than a preference: a bundle that forgot
    to declare E006 as sealed does not thereby make it unsealed, and a config
    that cannot be parsed is a refusal rather than a silently empty list.
    """
    declared = set(measurement.sealed_datasets)
    path = _round8_config_path(config)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise QualificationError(
                f"{path.name} is unreadable ({exc}); refusing to guess which "
                "datasets are sealed"
            ) from exc
        declared |= {str(name) for name in payload.get("splits", {}).get("sealed", [])}
    present = {u.dataset for u in measurement.utterances}
    return tuple(sorted(declared & present))


# --------------------------------------------------------------------------
# Aggregation. This is where the unit of evidence is enforced.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Trials:
    """Counts over utterances, never over windows."""

    trials: int
    events: int
    seconds: float
    windows: int
    utterance_ids: tuple[str, ...]
    speakers: tuple[str, ...]

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0


#: How a category's framings collapse into an utterance-level outcome. The
#: first two are ``round8_config.json``'s ``selection.utterance_aggregation``.
ANY_FIRED = "any-window-fired"
NONE_FIRED = "no-window-fired"

#: The third rule, for the one target that is a *rate* rather than a
#: proportion. A rate counts interruptions, and a 20-second recording that woke
#: the assistant three times interrupted somebody three times. ANY_FIRED would
#: score that as one, which is the wrong direction for a rate: it would report
#: 0.067/h where 0.2/h happened.
COUNT_ACTIVATIONS = "count-activations"


def tiles_its_own_audio(utterance: Utterance, window_seconds: float) -> bool:
    """Do this utterance's windows cover distinct audio, or re-frame the same audio?

    The distinction decides how activations are counted, and it is answerable
    from the record rather than by asking: *n* windows of ``window_seconds``
    cover at most ``n * window_seconds``, so if the utterance's own duration
    needs more than ``n - 1`` of them, the windows must be laid end to end.

    * Seven phrase-anchored framings of a 2-second phrase: 2 > 12 is false, so
      they overlap and the seven decisions are seven views of one event.
    * A 20-second Common Voice clip in ten 2-second tiles: 20 > 18 is true, so
      each tile is its own stretch of audio and its own chance to interrupt.
    * A single window is always its own audio.
    """
    return utterance.audio_seconds > window_seconds * (len(utterance.windows) - 1) + 1e-9


def collect_trials(
    measurement: Measurement,
    backend: str,
    categories: Iterable[str],
    rule: str,
) -> Trials:
    """Collapse framings into utterance-level trials.

    The two lines that matter are ``trials += 1`` and ``seconds +=
    utterance.audio_seconds``: **one** trial and **one** duration per
    utterance, whatever number of framings it was cut into. Counting per window
    instead would multiply *n* by seven on phrase-anchored audio, shrink every
    interval by sqrt(7), and inflate the hours in the denominator of the
    per-hour target — all three in the direction of a false pass, which is why
    it is enforced here rather than left to each caller.

    ``events`` is the one figure that is *not* capped at one per utterance,
    and only under ``COUNT_ACTIVATIONS``: see that rule's note. Proportions stay
    per-utterance because that is what their denominator counts.
    """
    if rule not in (ANY_FIRED, NONE_FIRED, COUNT_ACTIVATIONS):
        raise ValueError(f"unknown aggregation rule {rule!r}")
    wanted = set(categories)
    trials = events = windows = 0
    seconds = 0.0
    identifiers: list[str] = []
    speakers: set[str] = set()
    for utterance in measurement.utterances:
        if utterance.category not in wanted:
            continue
        trials += 1
        seconds += utterance.audio_seconds
        windows += len(utterance.windows)
        identifiers.append(utterance.utterance_id)
        if utterance.speaker:
            speakers.add(utterance.speaker)
        fired_windows = sum(1 for w in utterance.windows if bool(w.fired[backend]))
        if rule == COUNT_ACTIVATIONS:
            events += (
                fired_windows
                if tiles_its_own_audio(utterance, measurement.window_seconds)
                else min(1, fired_windows)
            )
        elif (rule == ANY_FIRED and fired_windows) or (
            rule == NONE_FIRED and not fired_windows
        ):
            events += 1
    return Trials(
        trials=trials,
        events=events,
        seconds=seconds,
        windows=windows,
        utterance_ids=tuple(identifiers),
        speakers=tuple(sorted(speakers)),
    )


def evidence_summary(measurement: Measurement) -> dict:
    """Sample counts and audio hours per category, plus who they came from."""
    by_category: dict[str, dict] = {}
    for utterance in measurement.utterances:
        row = by_category.setdefault(
            utterance.category,
            {"utterances": 0, "windows": 0, "audio_seconds": 0.0,
             "speakers": set(), "datasets": set()},
        )
        row["utterances"] += 1
        row["windows"] += len(utterance.windows)
        row["audio_seconds"] += utterance.audio_seconds
        if utterance.speaker:
            row["speakers"].add(utterance.speaker)
        if utterance.dataset:
            row["datasets"].add(utterance.dataset)
    for row in by_category.values():
        row["audio_hours"] = row["audio_seconds"] / 3600.0
        row["speakers"] = sorted(row["speakers"])
        row["datasets"] = sorted(row["datasets"])
    return {
        "utterances": len(measurement.utterances),
        "windows": sum(len(u.windows) for u in measurement.utterances),
        "audio_seconds": sum(u.audio_seconds for u in measurement.utterances),
        "audio_hours": sum(u.audio_seconds for u in measurement.utterances) / 3600.0,
        "by_category": dict(sorted(by_category.items())),
        "datasets": list(measurement.datasets()),
    }


# --------------------------------------------------------------------------
# Verdicts. One function decides demonstrability for every target.
# --------------------------------------------------------------------------


def demonstrability(
    *,
    met: bool,
    bound: float | None,
    limit: float,
    have: float,
    need: float,
    unit: str,
    extra_refusals: Sequence[str] = (),
) -> tuple[bool, list[str]]:
    """Is the target *demonstrated*, and if not, exactly why not.

    Three independent conditions, all required, in the order that matters:

    1. **Enough evidence.** ``have >= need``. This is the one the project has
       failed silently before. Below it the target cannot be demonstrated by
       any outcome, so the point estimate is not consulted — a "0.000/h" over
       11.7 hours is refused here and never reaches a verdict line.
    2. **The point estimate satisfies the target.** Necessary, never
       sufficient.
    3. **The bound satisfies the target.** With enough evidence and a clean
       run this follows, but with events observed it is the binding condition.

    Every refusal is returned rather than raised: a report that says which of
    the five targets is unsupported, and by how much, is what tells the
    programme what to record next. A single exception says only "no".
    """
    refusals: list[str] = list(extra_refusals)
    powered = have >= need
    if not powered:
        refusals.append(
            f"{UNDERPOWERED}: {have:.4g} {unit} present, {need:.4g} required for any "
            f"outcome to bound this target at {limit:g}"
        )
    if not met:
        refusals.append("the observed result does not meet the target")
    if bound is None:
        refusals.append("no bound could be computed from the evidence present")
    elif bound > limit:
        refusals.append(
            f"the 95% upper bound is {bound:.6g}, above the target {limit:g}"
        )
    return (not refusals), refusals


def _proportion_target(target: Target, trials: Trials, power: Mapping) -> dict:
    stats = proportion_bound(trials.events, trials.trials)
    met = stats["point_estimate"] is not None and stats["point_estimate"] <= target.limit
    demonstrable, refusals = demonstrability(
        met=met,
        bound=stats["bound"],
        limit=target.limit,
        have=float(trials.trials),
        need=float(power["required_trials"]),
        unit=str(power["unit"]),
    )
    return {
        **_target_head(target),
        "trials": trials.trials,
        "events": trials.events,
        "windows": trials.windows,
        "audio_hours": trials.hours,
        "speakers": list(trials.speakers),
        "point_estimate": stats["point_estimate"],
        "bound": stats["bound"],
        "bound_method": stats["bound_method"],
        "interval": stats["interval"],
        "interval_method": stats["interval_method"],
        "clopper_pearson_two_sided_95": stats["clopper_pearson_two_sided_95"],
        "required_trials": power["required_trials"],
        "shortfall_trials": max(0, int(power["required_trials"]) - trials.trials),
        "power_derivation": power["derivation"],
        "met_on_point_estimate": met,
        "statistically_demonstrable": demonstrable,
        "refusals": refusals,
    }


def _rate_target(target: Target, trials: Trials, power: Mapping) -> dict:
    stats = rate_upper_bound(trials.events, trials.hours)
    met = (
        stats["point_estimate_per_hour"] is not None
        and stats["point_estimate_per_hour"] <= target.limit
    )
    demonstrable, refusals = demonstrability(
        met=met,
        bound=stats["bound_per_hour"],
        limit=target.limit,
        have=trials.hours,
        need=float(power["required_hours"]),
        unit=str(power["unit"]),
    )
    return {
        **_target_head(target),
        "trials": trials.trials,
        "events": trials.events,
        "windows": trials.windows,
        "audio_hours": trials.hours,
        "speakers": list(trials.speakers),
        "point_estimate_per_hour": stats["point_estimate_per_hour"],
        "bound": stats["bound_per_hour"],
        "bound_method": stats["bound_method"],
        "poisson_exact_upper_per_hour": stats["poisson_exact_upper_per_hour"],
        "rule_of_three_upper_per_hour": stats["rule_of_three_upper_per_hour"],
        "interval": stats["interval_per_hour"],
        "interval_method": stats["interval_method"],
        "required_hours": power["required_hours"],
        "shortfall_hours": max(0.0, float(power["required_hours"]) - trials.hours),
        "power_derivation": power["derivation"],
        "met_on_point_estimate": met,
        "statistically_demonstrable": demonstrable,
        "refusals": refusals,
    }


def _count_target(target: Target, trials: Trials, power: Mapping) -> dict:
    """Target 4: a count, judged as a count, with the reach of that count stated.

    The target is ``== 0`` over the background windows present, and
    ``round8_config.json`` is explicit that this is all it is:
    ``demonstrable_as_a_rate: false``, "met or not met as a count; it is a
    claim about one or two rooms". So the gate is the count, and the bound is
    the exact per-window one-sided limit the design tabulates
    (``clean_run_bound_per_window``: 0.047924 at n = 61) rather than an hourly
    rate the design deliberately does not ask for.

    ``statistically_demonstrable`` is therefore true for a clean count, and it
    would over-claim on its own: the design's reporting rule says target 4 is
    "never reported as demonstrated" in the sense of generalising. That is why
    every row carries ``generalises_beyond_the_windows_measured: False`` and the
    per-window bound next to the boolean — the claim is "no activation occurred
    on these N windows", which is an observation, not an inference about rooms.
    """
    # Per *window*, not per utterance: background audio is tiled, so a window is
    # the opportunity, and under COUNT_ACTIVATIONS the event count can exceed
    # the utterance count when one tiled recording fires more than once.
    events = min(trials.events, trials.windows)
    per_window = proportion_bound(events, trials.windows)
    met = trials.windows > 0 and trials.events == 0
    demonstrable, refusals = demonstrability(
        met=met,
        # The count is the gate, so the quantity compared against the limit is
        # the count itself. No interval stands between an observation and its
        # own value.
        bound=float(trials.events),
        limit=target.limit,
        have=float(trials.windows),
        need=float(power["minimum"]),
        unit=str(power["unit"]),
    )
    return {
        **_target_head(target),
        "trials": trials.trials,
        "events": trials.events,
        "windows": trials.windows,
        # Reported because the brief asks for audio hours per category, and
        # deliberately NOT the denominator of the bound: see why_per_window.
        "audio_hours": trials.hours,
        "count": trials.events,
        "bound": per_window["bound"],
        "bound_method": (
            "exact Clopper-Pearson one-sided 95% upper limit, per background window"
        ),
        "bound_is_per_window_probability_not_a_rate": True,
        "interval": per_window["interval"],
        "interval_method": per_window["interval_method"],
        "minimum_windows": power["minimum"],
        "demonstrable_as_a_rate": power["demonstrable_as_a_rate"],
        "generalises_beyond_the_windows_measured": False,
        "claim": (
            f"{trials.events} activation(s) on the {trials.windows} background "
            f"window(s) measured ({trials.hours:.4g} h), bounding the per-window "
            f"activation probability at {_g(per_window['bound'])}. It says nothing "
            "about a room that was not recorded."
            if trials.windows
            else "no background-only audio was measured, so there is nothing to claim"
        ),
        "why_per_window": power["why_per_window"],
        "met_on_point_estimate": met,
        "statistically_demonstrable": demonstrable,
        "refusals": refusals,
    }


def parity_result(measurement: Measurement, power: Mapping) -> dict:
    """Target 5: do the two backends decide the same thing, and at what cost.

    Disagreements are counted per *window*, because a window is a decision and
    a decision is what the user experiences: one build wakes, the other does
    not. The window is also the denominator the predeclared design uses —
    ``target_5.clean_run_bound_by_n`` is indexed by window count, and every
    entry in it is the exact per-window Clopper-Pearson limit (0.002991 at
    n = 1000, 9.4e-05 at n = 31,986), which this function reproduces.

    The gate is what the design states and nothing more: zero detection
    disagreements (``detection_disagreement_is_fatal: true``), both backends
    measured, every window compared, and latency and memory present. The
    per-frame score delta is measured and reported and deliberately **not**
    gated — ``frame_score_delta: "reported, not gated"``. No minimum window
    count is invented, because the design declares none; the bound is printed
    instead, so a comparison over 1,000 windows cannot be mistaken for one over
    31,986.

    Hours are still accumulated per *utterance* and reported, because hours are
    a claim about how much distinct audio was compared and overlapping framings
    of one utterance are not more audio. They are a description of the evidence
    here, not the denominator of the bound.
    """
    pair = ("onnx", "tflite")
    missing_backends = [b for b in pair if b not in measurement.backends]
    windows = disagreements = compared_frames = 0
    utterances_disagreeing: list[str] = []
    max_abs = 0.0
    sum_abs = 0.0
    windows_without_delta = 0
    seconds = 0.0
    for utterance in measurement.utterances:
        seconds += utterance.audio_seconds
        disagreed = False
        for window in utterance.windows:
            windows += 1
            if not missing_backends:
                if bool(window.fired["onnx"]) != bool(window.fired["tflite"]):
                    disagreements += 1
                    disagreed = True
            if window.delta is None:
                windows_without_delta += 1
                continue
            max_abs = max(max_abs, window.delta.max_abs)
            sum_abs += window.delta.sum_abs
            compared_frames += window.delta.frames
        if disagreed:
            utterances_disagreeing.append(utterance.utterance_id)

    cost, cost_refusals = _runtime_cost_view(measurement, pair)
    hours = seconds / 3600.0
    extra: list[str] = list(cost_refusals)
    if missing_backends:
        extra.append(
            f"parity is a two-backend claim and {missing_backends} was not measured"
        )
    if windows_without_delta:
        extra.append(
            f"{windows_without_delta} of {windows} windows carry no per-frame score "
            "difference, so the numerical half of the target is not covered"
        )
    per_window = proportion_bound(min(disagreements, windows), windows)
    met = not missing_backends and disagreements == 0
    demonstrable, refusals = demonstrability(
        met=met,
        # The gate is the disagreement count itself: the design makes one
        # disagreement fatal, so there is no interval to clear, only a zero to
        # hold.
        bound=float(disagreements),
        limit=TARGETS_BY_ID[5].limit,
        have=float(windows),
        need=float(power["minimum"]),
        unit=str(power["unit"]),
        extra_refusals=extra,
    )
    target = TARGETS_BY_ID[5]
    return {
        **_target_head(target),
        "windows_compared": windows,
        "audio_hours_compared": hours,
        "detection_disagreements": disagreements,
        "detection_agreement_rate": (windows - disagreements) / windows if windows else None,
        "utterances_with_disagreement": utterances_disagreeing,
        "max_absolute_frame_score_difference": max_abs if compared_frames else None,
        "mean_absolute_frame_score_difference": (
            sum_abs / compared_frames if compared_frames else None
        ),
        "frames_compared": compared_frames,
        "windows_without_frame_deltas": windows_without_delta,
        "frame_score_delta_is_reported_not_gated": True,
        "runtime_cost": cost,
        "bound": per_window["bound"],
        "bound_method": (
            "exact Clopper-Pearson one-sided 95% upper limit, per compared window"
        ),
        "bound_is_per_window_probability_not_a_rate": True,
        "interval": per_window["interval"],
        "interval_method": per_window["interval_method"],
        "minimum_windows": power["minimum"],
        "claim": (
            f"{disagreements} detection disagreement(s) over {windows} compared "
            f"window(s) ({hours:.4g} h), bounding the per-window disagreement "
            f"probability at {_g(per_window['bound'])}."
        ),
        "why_per_window": power["why_per_window"],
        "met_on_point_estimate": met,
        "statistically_demonstrable": demonstrable,
        "refusals": refusals,
    }


#: Latency percentiles target 5 requires. p90 is welcome and not required; p99
#: is required because the tail is where a frame gets dropped.
REQUIRED_PERCENTILES = ("p50", "p95", "p99")


def _runtime_cost_view(measurement: Measurement, backends: Sequence[str]) -> tuple[dict, list[str]]:
    """Latency and memory per backend, and a refusal for anything absent.

    "Measured" is the operative word in target 5. A missing p99 is not a
    detail: it is the difference between knowing the tail and assuming it, and
    a qualification that assumes it has not measured latency.
    """
    view: dict[str, dict] = {}
    refusals: list[str] = []
    for backend in backends:
        cost = measurement.runtime_cost.get(backend) or {}
        latency = cost.get("latency_ms") or {}
        row = {
            "latency_ms": {
                name: (float(latency[name]) if latency.get(name) is not None else None)
                for name in REQUIRED_PERCENTILES
            },
            "peak_rss_bytes": cost.get("peak_rss_bytes"),
            "frames_timed": cost.get("frames_timed"),
            "real_time_factor": cost.get("real_time_factor"),
            "model_bytes": cost.get("model_bytes"),
            "measured_on": cost.get("measured_on"),
        }
        for name in REQUIRED_PERCENTILES:
            if row["latency_ms"][name] is None:
                refusals.append(f"{backend}: latency {name} was not measured")
        if not row["peak_rss_bytes"]:
            refusals.append(f"{backend}: peak memory was not measured")
        view[backend] = row
    return view, refusals


def _target_head(target: Target) -> dict:
    return {
        "target": target.id,
        "name": target.name,
        "limit": target.limit,
        "comparison": target.comparison,
        "unit": target.unit,
    }


def backend_targets(measurement: Measurement, backend: str, power: Mapping) -> list[dict]:
    """Targets 1-4 for one backend. Each backend must pass on its own."""
    return [
        _proportion_target(
            TARGETS_BY_ID[1],
            collect_trials(measurement, backend, {POSITIVE_CATEGORY}, NONE_FIRED),
            power["1"],
        ),
        _rate_target(
            TARGETS_BY_ID[2],
            collect_trials(
                measurement, backend, RECORDED_SPEECH_CATEGORIES, COUNT_ACTIVATIONS
            ),
            power["2"],
        ),
        _proportion_target(
            TARGETS_BY_ID[3],
            collect_trials(measurement, backend, NEAR_PHRASE_CATEGORIES, ANY_FIRED),
            power["3"],
        ),
        _count_target(
            TARGETS_BY_ID[4],
            collect_trials(
                measurement, backend, BACKGROUND_CATEGORIES, COUNT_ACTIVATIONS
            ),
            power["4"],
        ),
    ]


def _merge_across_backends(per_backend: Mapping[str, list[dict]], parity: dict) -> list[dict]:
    """One verdict per target, taking the worst backend.

    ``all()`` and not ``any()``: the product ships both artifacts, so a target
    demonstrated on ONNX and not on TFLite is not demonstrated. The per-backend
    detail stays attached so the asymmetry is visible rather than averaged
    away.
    """
    merged: list[dict] = []
    for target in TARGETS[:4]:
        rows = {backend: _row_for(target.id, results) for backend, results in per_backend.items()}
        merged.append(
            {
                **_target_head(target),
                "statistically_demonstrable": bool(rows)
                and all(r["statistically_demonstrable"] for r in rows.values()),
                "met_on_point_estimate": bool(rows) and all(
                    r["met_on_point_estimate"] for r in rows.values()
                ),
                "refusals": sorted(
                    {f"{backend}: {why}" for backend, r in rows.items() for why in r["refusals"]}
                ),
                "by_backend": rows,
            }
        )
    merged.append(parity)
    return merged


def _row_for(target_id: int, results: Sequence[Mapping]) -> dict:
    for row in results:
        if row["target"] == target_id:
            return dict(row)
    raise KeyError(target_id)  # pragma: no cover - internal


def _result_block(measurement: Measurement, power: Mapping) -> dict:
    """Everything computed from one measurement, without any verdict."""
    per_backend = {
        backend: backend_targets(measurement, backend, power)
        for backend in measurement.backends
    }
    parity = parity_result(measurement, power["5"])
    return {
        "conditioning": measurement.conditioning,
        "candidate_id": measurement.candidate_id,
        "threshold": measurement.threshold,
        "threshold_hex": float(measurement.threshold).hex(),
        "confirmation_frames": measurement.confirmation_frames,
        "backends": list(measurement.backends),
        "evidence": evidence_summary(measurement),
        "targets": _merge_across_backends(per_backend, parity),
        "by_backend": per_backend,
        "parity": parity,
    }


def official_block(measurement: Measurement, power: Mapping) -> dict:
    """The as-recorded result, with the verdict. The only block that carries one."""
    if measurement.conditioning != AS_RECORDED:
        raise EvidenceError(
            f"the official block is as-recorded only; this measurement is "
            f"{measurement.conditioning!r}. Level-matched audio is not what a "
            "microphone delivers, so it cannot carry a qualification verdict."
        )
    block = _result_block(measurement, power)
    demonstrated = [t for t in block["targets"] if t["statistically_demonstrable"]]
    refusals = sorted(
        {f"target {t['target']}: {why}" for t in block["targets"] for why in t["refusals"]}
    )
    underpowered = sorted(
        t["target"]
        for t in block["targets"]
        if any(UNDERPOWERED in why for why in t["refusals"])
    )
    qualified = len(demonstrated) == len(TARGETS)
    block.update(
        {
            "official": True,
            "targets_demonstrated": len(demonstrated),
            "targets_total": len(TARGETS),
            "underpowered_targets": underpowered,
            "qualified_5_of_5": qualified,
            "verdict": _verdict_line(qualified, demonstrated, underpowered),
            "refusals": refusals,
        }
    )
    return block


def _verdict_line(qualified: bool, demonstrated: Sequence[Mapping], underpowered: Sequence[int]) -> str:
    if qualified:
        return (
            "QUALIFIED — all five targets are met and statistically demonstrable "
            "on every measured backend with the evidence present"
        )
    if underpowered:
        return (
            f"REFUSED — {len(demonstrated)}/{len(TARGETS)} targets demonstrable. "
            f"Target(s) {list(underpowered)} are underpowered: the evidence "
            "present cannot bound them below their limits whatever the point "
            "estimate says."
        )
    return (
        f"NOT QUALIFIED — {len(demonstrated)}/{len(TARGETS)} targets demonstrable "
        "with adequate evidence; see refusals."
    )


def diagnostic_block(measurement: Measurement, power: Mapping) -> dict:
    """The level-matched result. Structurally incapable of carrying a verdict."""
    if measurement.conditioning == AS_RECORDED:
        raise EvidenceError(
            "as-recorded results are official, not diagnostic. Passing them here "
            "would file the authoritative measurement where nobody looks for it."
        )
    block = _result_block(measurement, power)
    block.update(
        {
            "official": False,
            "why_not_official": (
                "Level matching rescales the recording. It answers whether a "
                "failure is a level failure, which is worth knowing and is not "
                "what a microphone delivers. No verdict is computed from it."
            ),
        }
    )
    _assert_no_verdict(block)
    return block


#: Keys that may appear only in the official block. Asserted rather than
#: documented, because "diagnostic" written in a note has never stopped a
#: favourable number from being quoted.
VERDICT_KEYS = ("qualified_5_of_5", "verdict", "targets_demonstrated", "underpowered_targets")


def _assert_no_verdict(block: Mapping) -> None:
    leaked = [key for key in VERDICT_KEYS if key in block]
    if leaked:  # pragma: no cover - guards a future edit, not current behaviour
        raise QualificationError(
            f"the diagnostic block carries verdict keys {leaked}. Level-matched "
            "results are separated structurally, not by a flag."
        )


def build_report(
    official: Measurement,
    *,
    freeze: FreezeRecord | None = None,
    ledger: SealLedger | None = None,
    diagnostic: Measurement | None = None,
    registry: Path = RETIRED_REGISTRY,
    config: Path | None = None,
) -> dict:
    """The whole qualification record, refusals included.

    Order is deliberate: admissibility first, then the seal check, then the
    statistics. Nothing about the model is computed until it is established
    that the evidence may be used at all.
    """
    assert_admissible(official, registry)
    if diagnostic is not None:
        assert_admissible(diagnostic, registry)

    # Every dataset a report certifies must be a registered split/corpus name.
    # A hand-crafted bundle labelled 'E006_holdout' would otherwise slip sealed
    # audio past `sealed_datasets` (which intersects on the exact id) and be
    # certified with no freeze — the report-side half of the same evasion
    # `measure` is guarded against above.
    for source in (official, diagnostic):
        if source is not None:
            for name in {u.dataset for u in source.utterances if u.dataset}:
                assert_known_dataset(name, config)

    seals = _check_seals(official, freeze, ledger, config)
    if diagnostic is not None:
        seals.update(_check_seals(diagnostic, freeze, ledger, config))

    power = power_requirements()
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "scripts/wakeword/qualify.py",
        "generated_utc": _utc_now(),
        "targets": [
            {"id": t.id, "name": t.name, "limit": t.limit,
             "comparison": t.comparison, "unit": t.unit}
            for t in TARGETS
        ],
        "interval_methods": {
            "proportion_gate": "exact Clopper-Pearson one-sided 95% upper limit",
            "proportion_interval": "Wilson score, two-sided 95%",
            "rate_gate": (
                "the more conservative of the exact one-sided 95% Poisson upper "
                "limit and the rule of three (3/t)"
            ),
            "rate_interval": "exact Poisson, two-sided 95%",
            "alpha": ALPHA,
            "implementation": (
                "standard-library incomplete beta and gamma functions inverted by "
                "bisection; no scipy, and checked against closed forms in the tests"
            ),
        },
        "power_requirements": power,
        "freeze": freeze.body() if freeze is not None else None,
        "freeze_digest": freeze.digest() if freeze is not None else None,
        "seals": seals,
        "official": official_block(official, power),
        "diagnostic": (
            diagnostic_block(diagnostic, power) if diagnostic is not None else None
        ),
    }
    return report


def _check_seals(
    measurement: Measurement,
    freeze: FreezeRecord | None,
    ledger: SealLedger | None,
    config: Path | None,
) -> dict:
    """Refuse to report on a sealed set that was not frozen and opened."""
    names = sealed_datasets(measurement, config)
    if not names:
        return {}
    if freeze is None:
        raise IncompleteFreezeError(
            f"{list(names)} are sealed and no freeze was supplied. The candidate, "
            "its threshold, the runtime version, every manifest digest and both "
            "backend artifact digests must be frozen before a sealed set is "
            "opened."
        )
    freeze.assert_complete()
    if ledger is None:
        raise SealNotOpenError(
            f"{list(names)} are sealed and no ledger was supplied. Opening a "
            "sealed set is an event that has to be written down."
        )
    for name in names:
        if name not in freeze.manifest_sha256:
            raise IncompleteFreezeError(
                f"the freeze does not pin a manifest digest for the sealed set "
                f"{name}. A result over bytes nobody hashed is traceable to "
                "nothing."
            )
    return {name: ledger.assert_open(name, freeze) for name in names}


# --------------------------------------------------------------------------
# Measuring. Thin, and deliberately so: `evaluate_model.py` already drives the
# product engine correctly, so this reuses it rather than growing a second
# copy of the streaming loop that could drift from the runtime contract.
# --------------------------------------------------------------------------


def _load_evaluate_model(repo_root: Path = REPO_ROOT):
    """Import ``evaluate_model.py`` by path. ``scripts/wakeword`` is not a package."""
    import importlib.util  # noqa: PLC0415

    path = Path(__file__).resolve().with_name("evaluate_model.py")
    spec = importlib.util.spec_from_file_location("_wakeword_evaluate_model", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(repo_root))
    spec.loader.exec_module(module)
    return module


def peak_rss_bytes() -> int:
    """High-water-mark resident set size of this process, in bytes.

    ``psutil`` is a core dependency of this repository (pyproject pins
    7.2.2) and exposes the Windows peak directly; on POSIX the high-water mark
    lives in ``getrusage``, in kilobytes on Linux and bytes on the BSDs. Same
    field, different unit — getting that wrong is a 1024x error in a number
    nobody would question.
    """
    if sys.platform == "win32":
        import psutil  # noqa: PLC0415

        return int(psutil.Process().memory_info().peak_wset)
    import resource  # noqa: PLC0415

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def _cost_probe(job: tuple) -> dict:
    """Time one backend and read its peak memory, in a process of its own.

    A child, for the reason ``evaluate_model._cost_probe`` gives: read in the
    parent, the second backend inherits the first one's footprint and the
    report attributes both to whichever ran last. One untimed clip first, so
    the runtime's one-off lazy allocation does not land in p99 as if the user
    paid it on every frame.
    """
    import time  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    audio_path, clips, repo_root, model_path, framework, threshold, confirmation = job
    module = _load_evaluate_model(Path(repo_root))
    audio = np.ascontiguousarray(np.load(audio_path, mmap_mode="r")[:clips])
    if audio.dtype != np.int16:
        raise EvidenceError(
            f"{Path(audio_path).name} is {audio.dtype}; the engine wants int16. "
            "float32 in [-1, 1] arrives 32768x too quiet and scores flat."
        )
    engine = module._engine(Path(model_path), framework, threshold, confirmation)
    frames_per_clip = audio.shape[1] // FRAME

    engine.reset()
    for f in range(frames_per_clip):
        engine.process(audio[0][f * FRAME : (f + 1) * FRAME])

    per_frame_ms: list[float] = []
    wall0 = time.perf_counter()
    for index in range(audio.shape[0]):
        engine.reset()
        clip = audio[index]
        for f in range(frames_per_clip):
            start = time.perf_counter_ns()
            engine.process(clip[f * FRAME : (f + 1) * FRAME])
            per_frame_ms.append((time.perf_counter_ns() - start) / 1e6)
    wall = time.perf_counter() - wall0

    ms = np.asarray(per_frame_ms, dtype=np.float64)
    audio_seconds = audio.shape[0] * frames_per_clip * FRAME / SAMPLE_RATE
    return {
        "latency_ms": {
            "mean": float(ms.mean()),
            "p50": float(np.percentile(ms, 50)),
            "p90": float(np.percentile(ms, 90)),
            "p95": float(np.percentile(ms, 95)),
            "p99": float(np.percentile(ms, 99)),
            "max": float(ms.max()),
        },
        "frames_timed": int(ms.size),
        "frame_seconds": FRAME / SAMPLE_RATE,
        "real_time_factor": float(wall / audio_seconds),
        "peak_rss_bytes": peak_rss_bytes(),
        "model_bytes": int(Path(model_path).stat().st_size),
        "measured_on": f"{sys.platform} python {sys.version.split()[0]}",
    }


def backend_cost(
    audio_path: Path,
    clips: int,
    model_path: Path,
    framework: str,
    threshold: float,
    confirmation: int,
    repo_root: Path = REPO_ROOT,
) -> dict:
    from concurrent.futures import ProcessPoolExecutor  # noqa: PLC0415

    job = (str(audio_path), clips, str(repo_root), str(model_path),
           framework, threshold, confirmation)
    with ProcessPoolExecutor(max_workers=1) as pool:
        return pool.submit(_cost_probe, job).result()


def measure_corpus(
    corpus: Path,
    *,
    models: Path,
    dataset: str,
    provenance: str,
    conditioning: str = AS_RECORDED,
    candidate_id: str = "",
    threshold: float = 0.6,
    confirmation_frames: int = 3,
    backends: Sequence[str] = ("onnx", "tflite"),
    jobs: int = 4,
    latency_clips: int = 200,
    speaker: str = "",
    sealed: Sequence[str] = (),
    repo_root: Path = REPO_ROOT,
    scorer: Callable | None = None,
) -> Measurement:
    """Score a built corpus through the product engine, per backend.

    Reads the tensors ``build_dataset``/``build_human_dataset`` already emit:
    ``audio_eval.npy`` (int16 windows), ``y_eval.npy``, ``categories_eval.json``
    and ``groups_eval.npy``. ``groups_eval.npy`` is not optional — it is the
    source-utterance id, and without it there is no way to tell seven framings
    of one phrase from seven phrases.

    ``scorer`` exists so the aggregation can be exercised without openWakeWord
    installed; when it is None the real engine is used through
    ``evaluate_model.score_corpus``.
    """
    import numpy as np  # noqa: PLC0415

    # Validate the operator-typed dataset name against the split/corpus registry
    # before a single window is loaded. A seal-evading label must not reach the
    # engine, let alone a report.
    assert_known_dataset(dataset)

    audio_path = corpus / "audio_eval.npy"
    audio = np.load(audio_path, mmap_mode="r")
    categories = json.loads((corpus / "categories_eval.json").read_text(encoding="utf-8"))
    groups_path = corpus / "groups_eval.npy"
    if not groups_path.exists():
        raise EvidenceError(
            f"{corpus.name} has no groups_eval.npy. The unit of evidence is the "
            "utterance, and without the source-utterance ids every framing would "
            "be counted as an independent trial."
        )
    groups = np.load(groups_path)
    total = int(audio.shape[0])
    if not (len(categories) == len(groups) == total):
        raise EvidenceError(
            f"{corpus.name}: {total} windows, {len(categories)} categories, "
            f"{len(groups)} group ids — these must agree"
        )

    window_seconds = audio.shape[1] / SAMPLE_RATE
    if audio.dtype != np.int16:
        raise EvidenceError(
            f"{audio_path.name} is {audio.dtype}; the runtime contract is int16 "
            "1280-sample frames. float32 would arrive 32768x too quiet."
        )

    # The content-address of the tensor these windows were read from. Without
    # it, `assert_admissible`'s retired-by-content check (`if digest and digest
    # in retired`) is vacuous on this path: `Utterance.source_sha256` would
    # default to "" and a retired synthetic `audio_eval.npy` — the registry
    # lists those by whole-file digest — would score cleanly. Every utterance
    # from one corpus shares this digest, so the first one to reach the check
    # refuses the whole bundle.
    corpus_sha256 = sha256_file(audio_path)

    # The tone guard runs before any scoring: audio that is not speech must not
    # reach the engine at all, let alone a report.
    for index in range(total):
        assert_not_tone(np.asarray(audio[index]), f"{corpus.name} window {index}")

    fired: dict[str, "np.ndarray"] = {}
    scores: dict[str, "np.ndarray"] = {}
    cost: dict[str, dict] = {}
    module = None
    for framework in backends:
        model_path = models / f"hey_youtab.{framework}"
        if scorer is not None:
            frame_scores, engine_fired = scorer(framework, np.asarray(audio))
        else:
            module = module or _load_evaluate_model(repo_root)
            frame_scores, engine_fired = module.score_corpus(
                audio_path, total, model_path, framework, threshold,
                confirmation_frames, repo_root, jobs,
            )
            if latency_clips:
                cost[framework] = backend_cost(
                    audio_path, min(latency_clips, total), model_path, framework,
                    threshold, confirmation_frames, repo_root,
                )
        scores[framework] = np.asarray(frame_scores)
        fired[framework] = np.asarray(engine_fired, dtype=bool)

    delta = None
    if "onnx" in scores and "tflite" in scores:
        delta = np.abs(
            scores["onnx"].astype(np.float64) - scores["tflite"].astype(np.float64)
        )

    by_group: dict[str, list[int]] = {}
    for index in range(total):
        by_group.setdefault(str(groups[index]), []).append(index)

    utterances: list[Utterance] = []
    for group, indices in by_group.items():
        category = categories[indices[0]]
        if any(categories[i] != category for i in indices):
            raise EvidenceError(
                f"group {group} spans more than one category; a grouping key that "
                "crosses categories cannot be the unit of evidence"
            )
        windows = tuple(
            Window(
                window_index=int(i),
                fired={b: bool(fired[b][i]) for b in backends},
                delta=(
                    FrameDelta(
                        max_abs=float(delta[i].max()),
                        sum_abs=float(delta[i].sum()),
                        frames=int(delta[i].size),
                    )
                    if delta is not None
                    else None
                ),
            )
            for i in indices
        )
        utterances.append(
            Utterance(
                utterance_id=f"{dataset}/{group}",
                category=category,
                provenance=provenance,
                # One utterance's duration, once. Phrase-anchored framings
                # overlap, so summing their window lengths would invent audio
                # that was never recorded and inflate the per-hour denominator.
                audio_seconds=window_seconds,
                windows=windows,
                dataset=dataset,
                speaker=speaker or dataset,
                source_sha256=corpus_sha256,
            )
        )

    return Measurement(
        conditioning=conditioning,
        backends=tuple(backends),
        threshold=threshold,
        confirmation_frames=confirmation_frames,
        candidate_id=candidate_id,
        utterances=tuple(utterances),
        runtime_cost=cost,
        sealed_datasets=tuple(sealed),
        window_seconds=window_seconds,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def _print_power(power: Mapping) -> None:
    print("Derived power requirements (alpha = 0.05, one-sided, unit = utterance)\n")
    for target in TARGETS:
        row = power[str(target.id)]
        need = row.get("required_trials", row.get("required_hours", row.get("minimum")))
        print(f"  target {target.id}  {target.name}")
        print(f"    limit      {target.comparison} {target.limit:g} {target.unit}")
        print(f"    requires   {need:g} {row['unit']}")
        if "derivation" in row:
            print(f"    derivation {row['derivation']}")
        # Targets 4 and 5 have no sample size to derive: the predeclared design
        # judges them per window and declares no minimum n, so what is printed
        # is the gate and where it comes from rather than an invented threshold.
        if "gate" in row:
            print(f"    gate       {row['gate']}")
            print(f"    why        {row['why_per_window']}")
        print()


def _leaves(row: Mapping) -> list[Mapping]:
    """A merged target row's per-backend detail, or the row itself if it has none."""
    per_backend = row.get("by_backend")
    return list(per_backend.values()) if per_backend else [row]


def _print_report(report: Mapping) -> None:
    block = report["official"]
    print(f"candidate {block['candidate_id'] or '?'} @ threshold {block['threshold']!r} "
          f"({block['conditioning']}, OFFICIAL)")
    evidence = block["evidence"]
    print(f"  {evidence['utterances']} utterances, {evidence['windows']} windows, "
          f"{evidence['audio_hours']:.3f} h")
    for name, row in evidence["by_category"].items():
        print(f"    {name:20s} {row['utterances']:6d} utterances  "
              f"{row['windows']:7d} windows  {row['audio_hours']:8.3f} h")
    print()
    for row in block["targets"]:
        mark = "DEMONSTRATED" if row["statistically_demonstrable"] else "NOT DEMONSTRATED"
        print(f"  target {row['target']}  {row['name']}: {mark}")
        # The scope of a count claim goes on the same line as the word
        # DEMONSTRATED, because that line is what gets pasted into a report. The
        # predeclared design says target 4 is "never reported as demonstrated"
        # in the generalising sense, and a bare DEMONSTRATED next to a count over
        # 61 windows in one room is precisely that over-claim.
        for leaf in _leaves(row):
            if leaf.get("generalises_beyond_the_windows_measured") is False:
                print(f"      scope: {leaf['claim']}")
                break
        for why in row["refusals"]:
            print(f"      - {why}")
    print(f"\n  {block['verdict']}")
    if report.get("diagnostic"):
        print(f"\n  diagnostic ({report['diagnostic']['conditioning']}) computed "
              "separately and carries no verdict")


def _parse_pairs(values: Sequence[str], flag: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise SystemExit(f"{flag} wants NAME=VALUE, got {raw!r}")
        name, value = raw.split("=", 1)
        pairs[name.strip()] = value.strip()
    return pairs


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("power", help="print the derived power requirements and stop")

    freezer = sub.add_parser("freeze", help="pin a candidate before any seal opens")
    freezer.add_argument("--candidate", required=True)
    freezer.add_argument("--threshold", required=True, help="decimal or 0x hex float")
    freezer.add_argument("--confirmation-frames", type=int, default=3)
    freezer.add_argument("--runtime-version", required=True)
    freezer.add_argument("--repo-commit", required=True)
    freezer.add_argument("--onnx", type=Path, required=True)
    freezer.add_argument("--tflite", type=Path, required=True)
    freezer.add_argument("--manifest", action="append", default=[],
                         help="DATASET=SHA256, repeatable")
    freezer.add_argument("--note", default="")
    freezer.add_argument("--out", type=Path, required=True)

    opener = sub.add_parser("open-sealed", help="spend a sealed set, permanently")
    opener.add_argument("--freeze", type=Path, required=True)
    opener.add_argument("--ledger", type=Path, required=True)
    opener.add_argument("--dataset", required=True)

    measurer = sub.add_parser("measure", help="score a built corpus into an evidence bundle")
    measurer.add_argument("--corpus", type=Path, required=True)
    measurer.add_argument("--models", type=Path, required=True)
    measurer.add_argument("--dataset", required=True)
    measurer.add_argument("--provenance", required=True, choices=sorted(EVIDENCE_PROVENANCE))
    measurer.add_argument("--conditioning", default=AS_RECORDED, choices=CONDITIONINGS)
    measurer.add_argument("--candidate", default="")
    measurer.add_argument("--threshold", type=float, default=0.6)
    measurer.add_argument("--confirmation-frames", type=int, default=3)
    measurer.add_argument("--backends", nargs="+", default=["onnx", "tflite"],
                          choices=["onnx", "tflite"])
    measurer.add_argument("--jobs", type=int, default=4)
    measurer.add_argument("--latency-clips", type=int, default=200)
    measurer.add_argument("--sealed", nargs="*", default=[])
    measurer.add_argument("--freeze", type=Path, default=None)
    measurer.add_argument("--ledger", type=Path, default=None)
    measurer.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    measurer.add_argument("--out", type=Path, required=True)

    reporter = sub.add_parser("report", help="turn evidence into a qualification record")
    reporter.add_argument("--official", type=Path, required=True)
    reporter.add_argument("--diagnostic", type=Path, default=None)
    reporter.add_argument("--freeze", type=Path, default=None)
    reporter.add_argument("--ledger", type=Path, default=None)
    reporter.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)

    if args.command == "power":
        _print_power(power_requirements())
        return 0

    if args.command == "freeze":
        threshold = (
            float.fromhex(args.threshold)
            if args.threshold.lower().startswith("0x")
            else float(args.threshold)
        )
        record = FreezeRecord(
            candidate_id=args.candidate,
            threshold=threshold,
            confirmation_frames=args.confirmation_frames,
            runtime_version=args.runtime_version,
            repo_commit=args.repo_commit,
            manifest_sha256=_parse_pairs(args.manifest, "--manifest"),
            artifact_sha256={
                "onnx": sha256_file(args.onnx),
                "tflite": sha256_file(args.tflite),
            },
            frozen_utc=_utc_now(),
            note=args.note,
        )
        record.assert_complete()
        _write_json(args.out, record.body())
        print(f"froze {record.candidate_id} at {record.threshold!r}")
        print(f"  freeze digest {record.digest()}")
        print(f"  wrote {args.out}")
        return 0

    if args.command == "open-sealed":
        freeze = freeze_from_json(json.loads(args.freeze.read_text(encoding="utf-8")))
        ledger = SealLedger(args.ledger)
        entry = ledger.open_sealed(args.dataset, freeze)
        print(f"{args.dataset}: {entry['status']} since {entry['opened_utc']} "
              f"against {entry['candidate_id']} (freeze {entry['freeze_digest'][:12]}...)")
        return 0

    if args.command == "measure":
        freeze = (
            freeze_from_json(json.loads(args.freeze.read_text(encoding="utf-8")))
            if args.freeze
            else None
        )
        ledger = SealLedger(args.ledger) if args.ledger else None
        if args.dataset in set(args.sealed):
            if freeze is None or ledger is None:
                raise SystemExit(
                    f"{args.dataset} is sealed: --freeze and --ledger are required, "
                    "and the set must already be opened"
                )
            ledger.assert_open(args.dataset, freeze)
        measurement = measure_corpus(
            args.corpus,
            models=args.models,
            dataset=args.dataset,
            provenance=args.provenance,
            conditioning=args.conditioning,
            candidate_id=args.candidate,
            threshold=args.threshold,
            confirmation_frames=args.confirmation_frames,
            backends=tuple(args.backends),
            jobs=args.jobs,
            latency_clips=args.latency_clips,
            sealed=tuple(args.sealed),
            repo_root=args.repo_root,
        )
        _write_json(args.out, measurement_to_json(measurement))
        print(f"measured {len(measurement.utterances)} utterances -> {args.out}")
        return 0

    official = measurement_from_json(json.loads(args.official.read_text(encoding="utf-8")))
    diagnostic = (
        measurement_from_json(json.loads(args.diagnostic.read_text(encoding="utf-8")))
        if args.diagnostic
        else None
    )
    freeze = (
        freeze_from_json(json.loads(args.freeze.read_text(encoding="utf-8")))
        if args.freeze
        else None
    )
    ledger = SealLedger(args.ledger) if args.ledger else None
    report = build_report(
        official, freeze=freeze, ledger=ledger, diagnostic=diagnostic
    )
    _write_json(args.out, report)
    _print_report(report)
    print(f"\nwrote {args.out}")
    return 0 if report["official"]["qualified_5_of_5"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QualificationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
