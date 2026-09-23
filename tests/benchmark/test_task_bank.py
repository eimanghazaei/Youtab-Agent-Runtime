"""Task-bank integrity (marked ``benchmark``; out of default collection).

Validates the versioned manifest without running any scenario: >=40 scenarios,
all 20 families covered, unique ids, every oracle/executor name resolvable, and
real_provider scenarios declared ``unknown`` for deterministic mode. Fails loudly
(never vacuously): it first asserts the bank is non-empty.
"""

from __future__ import annotations

import pytest

from tests.benchmark.harness.executors import EXECUTORS
from tests.benchmark.harness.oracles import ORACLES
from tests.benchmark.harness.schema import FAMILIES
from tests.benchmark.harness.taskbank import MIN_SCENARIOS, load_scenarios, validate

pytestmark = pytest.mark.benchmark


def test_manifest_validates_and_meets_floor() -> None:
    scenarios = validate()
    assert len(scenarios) >= MIN_SCENARIOS, "task bank below the required floor"


def test_all_families_covered() -> None:
    scenarios = load_scenarios()
    covered = {s.family for s in scenarios}
    assert set(FAMILIES) <= covered, sorted(set(FAMILIES) - covered)


def test_every_family_has_at_least_one_scenario() -> None:
    scenarios = load_scenarios()
    for family in FAMILIES:
        assert any(s.family == family for s in scenarios), f"no scenario for {family}"


def test_oracle_and_executor_names_resolve() -> None:
    for s in load_scenarios():
        assert s.oracle in ORACLES, f"{s.id}: unknown oracle {s.oracle}"
        assert s.executor in EXECUTORS, f"{s.id}: unknown executor {s.executor}"


def test_scenario_ids_unique() -> None:
    ids = [s.id for s in load_scenarios()]
    assert len(ids) == len(set(ids)), "duplicate scenario ids"


def test_real_provider_scenarios_expect_unknown() -> None:
    for s in load_scenarios():
        if s.mode == "real_provider":
            assert s.expected_verdict == "unknown", (
                f"{s.id}: real_provider must be unknown in deterministic mode"
            )
