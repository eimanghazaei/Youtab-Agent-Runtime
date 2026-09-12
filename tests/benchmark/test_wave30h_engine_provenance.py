"""WAVE-30H — benchmark provenance must record the EFFECTIVE attested engine.

Greptile blocker #1: ``Runner._emit_record`` persisted ``engine_pinned`` from
``scenario.engine`` only (with a deterministic-worker fallback), so a successful
engine-bound run whose scenario did not re-declare the engine was recorded with
``engine_pinned=null`` — losing the very provenance a Track A record exists to
prove. The authoritative source is the actual :class:`Observation` the seam
attests. These tests pin that contract, including the negative/null-guard cases.
"""
from __future__ import annotations

import types

import pytest

from tests.benchmark.harness.recorder import Recorder
from tests.benchmark.harness.runner import Runner
from tests.benchmark.harness.schema import (
    MODE_DETERMINISTIC,
    MODE_REAL_PROVIDER,
    Observation,
    Scenario,
    Verdict,
)

pytestmark = pytest.mark.benchmark


def _runner(tmp_path, mode=MODE_REAL_PROVIDER):
    return Runner(Recorder(tmp_path / "out"), mode=mode)


# -- pure resolution contract -------------------------------------------------

def test_resolve_prefers_effective_attested_engine(tmp_path):
    r = _runner(tmp_path)
    obs = types.SimpleNamespace(engine_pinned="eco.v01")
    scn = types.SimpleNamespace(engine=None)           # scenario did not re-declare
    assert r._resolve_engine_pinned(scn, obs) == "eco.v01"


def test_resolve_attested_overrides_even_a_declared_scenario_engine(tmp_path):
    # What actually executed wins over what was requested (provenance = reality).
    r = _runner(tmp_path)
    obs = types.SimpleNamespace(engine_pinned="eco.v01")
    scn = types.SimpleNamespace(engine="amour.v03")
    assert r._resolve_engine_pinned(scn, obs) == "eco.v01"


def test_resolve_falls_back_to_scenario_engine_without_observation(tmp_path):
    # No observation (an unknown/harness record) -> the requested engine is the
    # best available provenance.
    r = _runner(tmp_path)
    scn = types.SimpleNamespace(engine="amour.v03")
    assert r._resolve_engine_pinned(scn, None) == "amour.v03"


def test_resolve_deterministic_default_only_when_nothing_else(tmp_path):
    r = _runner(tmp_path, mode=MODE_DETERMINISTIC)
    scn = types.SimpleNamespace(engine=None)
    assert r._resolve_engine_pinned(scn, None) == "deterministic-worker"


def test_resolve_real_provider_no_engine_no_obs_is_none(tmp_path):
    # Honest null only when there is genuinely nothing to attest (no obs, no
    # requested engine, not deterministic) — never after a bound execution.
    r = _runner(tmp_path)
    scn = types.SimpleNamespace(engine=None)
    assert r._resolve_engine_pinned(scn, None) is None


# -- end-to-end persisted record ---------------------------------------------

def _scenario(engine=None):
    return Scenario(
        id="prov-1", family="duplicate_run", mode=MODE_REAL_PROVIDER,
        title="t", oracle="run_completed_ok", executor="noop", engine=engine,
    )


def test_track_a_record_never_persists_null_engine_after_bound_run(tmp_path):
    # The load-bearing guarantee: a successful engine-bound Observation is
    # recorded with the attested engine, NEVER engine_pinned=null, even though
    # the scenario itself did not carry the engine.
    r = _runner(tmp_path)
    obs = Observation(
        run_id="r1", tenant="t", user="u", mode=MODE_REAL_PROVIDER,
        seam="fake", platform="linux", engine_pinned="eco.v01",
    )
    row = r._emit_record(
        _scenario(engine=None), 0, "r1", {"tenant": "t", "user": "u"},
        Verdict.unknown("state-only", source=["state"]),
        observation=obs, wall_ms=1.0, provenance={},
    )
    assert row["engine_pinned"] == "eco.v01"
    assert row["engine_pinned"] is not None


def test_record_without_observation_uses_declared_engine(tmp_path):
    r = _runner(tmp_path)
    row = r._emit_record(
        _scenario(engine="amour.v03"), 0, "r1", {"tenant": "t", "user": "u"},
        Verdict.unknown("no obs", source=["state"]),
        observation=None, wall_ms=None, provenance={},
    )
    assert row["engine_pinned"] == "amour.v03"
