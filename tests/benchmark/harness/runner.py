"""Benchmark runner — isolated fixture per run, bounded waits, no arbitrary sleeps.

Drives each scenario through a seam, hands the assembled :class:`Observation` to
the scenario's state-based oracle, and records a :class:`BenchmarkRecord`. Every
run gets a fresh isolated ``YOUTAB_AGENT_HOME`` + workspace so concurrent runs
never share journal/workspace state. Restart/recovery uses the real
:mod:`youtab_runtime.harness_process` controller (bounded readiness/exit waits).

A ``real_provider`` scenario run in deterministic mode is never executed: it is
short-circuited to an honest ``unknown`` record carrying
``OWNER_LIVE_PROVIDER_ACTION_REQUIRED`` — never coerced to pass or 0.
"""

from __future__ import annotations

import concurrent.futures
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from youtab_runtime import redaction

from . import metrics as _metrics
from .oracles import ORACLES
from .recorder import Recorder
from .schema import (
    MODE_DETERMINISTIC,
    MODE_REAL_PROVIDER,
    OWNER_LIVE_PROVIDER_ACTION_REQUIRED,
    BenchmarkRecord,
    Observation,
    Outcome,
    Scenario,
    Verdict,
    platform_tag,
    runtime_head,
)
from .seam import DeterministicSubstrateSeam, IsolatedHome


class Runner:
    def __init__(
        self,
        recorder: Recorder,
        *,
        mode: str = MODE_DETERMINISTIC,
        seam: Optional[Any] = None,
        repo_root: Optional[Path] = None,
        tenant: str = "bench-tenant",
        user: str = "bench-user",
        work_root: Optional[Path] = None,
    ) -> None:
        self.recorder = recorder
        self.mode = mode
        self.repo_root = repo_root or Path(__file__).resolve().parents[3]
        self.seam = seam or DeterministicSubstrateSeam(repo_root=self.repo_root)
        self.tenant = tenant
        self.user = user
        self.work_root = Path(work_root) if work_root else None
        self._head = runtime_head(self.repo_root)

    # -- one (scenario, repetition) ---------------------------------------
    def run_scenario(self, scenario: Scenario, repetition: int = 0) -> Dict[str, Any]:
        run_id = f"bench-{scenario.id}-{repetition}-{uuid.uuid4().hex[:8]}"
        principal = {"tenant": self.tenant, "user": self.user}
        base_provenance = {
            "seam": getattr(self.seam, "name", "unknown"),
            "price_table_version": None,
            "expected_verdict": scenario.expected_verdict,
            "adversarial_variant": scenario.adversarial_variant,
            "required_tool_count": len(scenario.params.get("required_tools", [])),
        }

        # real_provider dimensions are not judgeable offline.
        if self.mode == MODE_DETERMINISTIC and scenario.mode == MODE_REAL_PROVIDER:
            verdict = Verdict.unknown(
                "real_provider dimension not judgeable in deterministic mode",
                source=["state"], action=OWNER_LIVE_PROVIDER_ACTION_REQUIRED,
            )
            return self._emit_record(
                scenario, repetition, run_id, principal, verdict,
                observation=None, wall_ms=None,
                provenance={**base_provenance,
                            "action": OWNER_LIVE_PROVIDER_ACTION_REQUIRED},
            )

        from .executors import CapabilityUnavailable

        home_ctx = IsolatedHome(root=self.work_root)
        t0 = time.monotonic()
        try:
            obs: Observation = self.seam.run(
                scenario, home=home_ctx.home, workspace=home_ctx.workspace,
                tenant=self.tenant, user=self.user, run_id=run_id,
            )
            wall_ms = (time.monotonic() - t0) * 1000.0
            oracle = ORACLES[scenario.oracle]
            oracle_params = obs.provenance.get("_oracle_params", scenario.params)
            verdict = oracle(obs, oracle_params)
            per_rec = _metrics.per_record_metrics(obs, verdict, wall_ms=wall_ms)
            return self._emit_record(
                scenario, repetition, obs.run_id, principal, verdict,
                observation=obs, wall_ms=wall_ms,
                provenance={**base_provenance, "platform": obs.platform},
                per_record_metrics=per_rec,
            )
        except CapabilityUnavailable as exc:
            # A host capability gap (e.g. cannot prove process ownership) — an
            # honest unknown, NOT a harness error and NOT a runtime defect.
            wall_ms = (time.monotonic() - t0) * 1000.0
            # WAVE-27: scrub the persisted string form of the exception so a
            # host-capability error carrying a URL/token/arg never lands raw in
            # the durable artifact. This changes only the stored STRING — the
            # verdict outcome (unknown) and the CI gate's truthiness checks on
            # provenance.capability_unavailable are unaffected.
            verdict = Verdict.unknown(
                redaction.redact_error(f"host capability unavailable: {exc}"),
                source=["state"])
            return self._emit_record(
                scenario, repetition, run_id, principal, verdict,
                observation=None, wall_ms=wall_ms,
                provenance={**base_provenance,
                            "capability_unavailable": redaction.redact_error(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - a harness error is an honest unknown
            wall_ms = (time.monotonic() - t0) * 1000.0
            # WAVE-27: as above — scrub the seam/harness exception string before
            # it is persisted, without altering the unknown verdict outcome.
            verdict = Verdict.unknown(
                redaction.redact_error(f"harness/seam error: {exc}"),
                source=["state"])
            return self._emit_record(
                scenario, repetition, run_id, principal, verdict,
                observation=None, wall_ms=wall_ms,
                provenance={**base_provenance,
                            "harness_error": redaction.redact_error(exc)},
            )
        finally:
            home_ctx.close()

    def _emit_record(
        self,
        scenario: Scenario,
        repetition: int,
        run_id: str,
        principal: Dict[str, str],
        verdict: Verdict,
        *,
        observation: Optional[Observation],
        wall_ms: Optional[float],
        provenance: Dict[str, Any],
        per_record_metrics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        verdict_str = (verdict.outcome.value if hasattr(verdict.outcome, "value")
                       else str(verdict.outcome))
        self_reported = (observation.self_reported_success if observation is not None
                         else scenario.self_reported_success)
        metrics = per_record_metrics or {"latency_ms": wall_ms}
        record = BenchmarkRecord(
            scenario_id=scenario.id,
            scenario_family=scenario.family,
            repetition=repetition,
            mode=self.mode,
            runtime_head=self._head,
            platform_tag=platform_tag(),
            engine_pinned=scenario.engine or (
                "deterministic-worker" if self.mode == MODE_DETERMINISTIC else None),
            principal=principal,
            run_id=run_id,
            verdict=verdict_str,
            self_reported_success=bool(self_reported),
            honesty_divergence=False,  # recorder computes the real value
            observation_source=verdict.observation_source,
            metrics=metrics,
            evidence_refs=verdict.evidence_refs,
            provenance=provenance,
            reason=verdict.reason,
        )
        return self.recorder.record(record)

    # -- whole bank -------------------------------------------------------
    def run_all(
        self,
        scenarios: List[Scenario],
        *,
        repetitions: int = 1,
        max_workers: int = 1,
    ) -> List[Dict[str, Any]]:
        """Run every (scenario, repetition). ``max_workers>1`` uses a bounded
        thread pool across scenarios (each still fully isolated). Default is
        sequential for reproducible ordering."""
        jobs = [(s, rep) for s in scenarios for rep in range(repetitions)]
        rows: List[Dict[str, Any]] = []
        if max_workers <= 1:
            for s, rep in jobs:
                rows.append(self.run_scenario(s, rep))
            return rows
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(self.run_scenario, s, rep) for s, rep in jobs]
            for f in concurrent.futures.as_completed(futs):
                rows.append(f.result())
        return rows

    @staticmethod
    def verdict_matches_expected(row: Dict[str, Any], scenario: Scenario) -> bool:
        """Deterministic CI gate helper: the observed verdict must equal the
        verdict a correctly-functioning runtime should yield for this scenario.

        A verdict of ``unknown`` caused by a HOST capability gap (recorded in
        ``provenance.capability_unavailable``) is accepted: the host could not
        exercise the dimension, which is not a runtime failure. On a capable host
        (e.g. CI ubuntu) the scenario yields its real expected verdict instead."""
        if (row.get("provenance") or {}).get("capability_unavailable") \
                and row["verdict"] == "unknown":
            return True
        return row["verdict"] == scenario.expected_verdict
