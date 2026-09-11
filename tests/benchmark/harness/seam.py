"""Benchmark seams — how a scenario is driven and how its state is observed.

Two seams, one observation contract:

* :class:`DeterministicSubstrateSeam` (CI-required, offline) — runs a named
  deterministic executor that produces REAL WAVE-26 substrate state at an
  isolated ``YOUTAB_AGENT_HOME`` / journal DB, then reads that durable state back
  (journal events by ``seq`` + effect ledger rows) into an :class:`Observation`.
  No model, no network, no production home.

* :class:`HttpRuntimeSeam` (isolated-local / real-provider) — drives the durable
  ``/api/runtime/v1`` plane through the REAL signing client
  (:class:`tests.benchmark.harness.auth_client.AuthClient`) with NO auth bypass,
  and reads the durable run detail + ordered event cursor + artifacts. Used only
  when an Owner provides a base URL + secret file; never in CI.

Both yield an :class:`Observation`; oracles never learn which seam produced it.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .executors import EXECUTORS, ExecContext
from .schema import Observation, Scenario, hash_tree, platform_tag


# --------------------------------------------------------------------------- #
# Deterministic substrate seam                                                 #
# --------------------------------------------------------------------------- #
class DeterministicSubstrateSeam:
    """Offline seam that produces + observes real WAVE-26 substrate state."""

    name = "deterministic_substrate"

    def __init__(self, *, repo_root: Optional[Path] = None) -> None:
        self.repo_root = repo_root

    def run(self, scenario: Scenario, *, home: Path, workspace: Path,
            tenant: str, user: str, run_id: str) -> Observation:
        from youtab_runtime import run_journal as rj

        db_path = home / "runtime" / "run_journal.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        pre_hash = hash_tree(workspace)

        ctx = ExecContext(
            home=home, db_path=db_path, workspace=workspace,
            tenant=tenant, user=user, run_id=run_id,
            params=dict(scenario.params), repo_root=self.repo_root,
        )
        executor = EXECUTORS[scenario.executor]
        result = executor(ctx)

        # ctx.run_id may have been re-pointed by the executor (concurrency picks a
        # specific worker run to observe). Read the durable journal for THAT run.
        observed_run_id = ctx.run_id
        principal = rj.Principal(tenant, user)
        run_events = list(rj.list_events(observed_run_id, principal, limit=5000,
                                         db_path=db_path))
        # `process` events are keyed by the harness launch token, not run_id (a
        # restart uses a fresh token), so gather them across tokens from this
        # run's isolated journal and merge, de-duped by event_id and ordered by
        # wall-clock so the cross-launch process timeline (killed -> recovered)
        # is visible to the oracles.
        proc_events = rj.list_events_by_category(principal, "process",
                                                 db_path=db_path)
        seen_ids = {e.event_id for e in run_events}
        merged = run_events + [e for e in proc_events if e.event_id not in seen_ids]
        merged.sort(key=lambda e: (e.ts_unix_ns, e.seq))
        events = [self._event_dict(e) for e in merged]
        effects = self._effects(observed_run_id, principal, db_path)

        oracle_params = dict(scenario.params)
        oracle_params.update(result.provenance)

        obs = Observation(
            run_id=observed_run_id, tenant=tenant, user=user,
            mode="deterministic", seam=self.name, platform=platform_tag(),
            events=events, effects=effects, artifacts=[],
            durable_status=result.durable_status,
            workspace=workspace, pre_hash=pre_hash,
            self_reported_success=result.self_reported_success,
            reported_usage=result.reported_usage,
            timings={}, engine_pinned=result.engine_pinned or scenario.engine,
            provenance=result.provenance,
        )
        # Stash oracle params on the observation so the runner can pass them in.
        obs.provenance = dict(obs.provenance)
        obs.provenance["_oracle_params"] = oracle_params
        return obs

    @staticmethod
    def _event_dict(e: Any) -> Dict[str, Any]:
        return {
            "seq": e.seq,
            "event_id": e.event_id,
            "run_id": e.run_id,
            "category": e.category,
            "kind": e.kind,
            "correlation_id": e.correlation_id,
            "dedupe_key": e.dedupe_key,
            "payload": e.payload,
        }

    @staticmethod
    def _effects(run_id: str, principal: Any, db_path: Path) -> List[Dict[str, Any]]:
        from youtab_runtime import effect_ledger as el

        out: List[Dict[str, Any]] = []
        for r in el.list_effects(run_id, principal, db_path=db_path):
            out.append({
                "effect_id": r.effect_id,
                "effect_type": r.effect_type,
                "state": r.state.value if hasattr(r.state, "value") else str(r.state),
                "attempts": r.attempts,
                "first_seen_seq": r.first_seen_seq,
                "last_update_seq": r.last_update_seq,
                "target_scope_digest": r.target_scope_digest,
                "provider_idempotency_key": r.provider_idempotency_key,
            })
        return out


# --------------------------------------------------------------------------- #
# Durable HTTP seam (real signing; no bypass) — local_runtime / real_provider  #
# --------------------------------------------------------------------------- #
class HttpRuntimeSeam:
    """Drives the durable ``/api/runtime/v1`` plane via the real signing client.

    Requires a running runtime (Owner-side) and the service secret. NEVER used in
    the deterministic CI job. Reads only observable, principal-scoped state:
    durable run detail, the ordered event cursor, and artifacts.
    """

    name = "runtime_http"

    def __init__(self, base_url: str, service_secret: str, *, tenant: str,
                 user: str, roles=("member",), timeout: float = 60.0,
                 limits: dict | None = None, engine: str | None = None,
                 require_engine: bool = False) -> None:
        from youtab_runtime.run_journal import Principal
        from .auth_client import AuthClient

        self._principal = Principal(tenant, user)
        self._client = AuthClient(base_url, service_secret, self._principal,
                                  roles=roles, timeout=timeout)
        self.tenant = tenant
        self.user = user
        # Authoritative per-run limits sent on every create_run (WAVE-30B §8).
        self._limits = dict(limits) if limits else None
        # Track-level engine binding (WAVE-30D §B1). When set, every create_run
        # pins this engine (e.g. Track A ``eco.v01``) unless the scenario carries
        # its own; ``require_engine`` makes a missing binding fail closed rather
        # than dispatch on the worker's default model.
        self._engine = (engine or "").strip() or None
        self._require_engine = bool(require_engine)
        # WAVE-30H: the substrate digest attested at the last preflight; sent on
        # create so the run is created ONLY if the runtime binds the same substrate.
        self._expected_binding_digest: str | None = None
        # WAVE-30H #7: the model-manifest digest attested at preflight; sent on create
        # so the worker can re-probe + fail closed on a tag->manifest re-point.
        self._expected_model_digest: str | None = None

    def preflight(self) -> dict:
        """The runtime's authenticated safety posture (for the live-run gate).

        Passes the bound engine so the posture carries the effective per-engine
        attestation the live gate verifies, and captures the attested substrate
        digest to pin the subsequent create (atomic preflight->create)."""
        posture = self._client.preflight(engine=self._engine)
        from youtab_agent_cli import effective_binding as _eb

        # Fail closed (WAVE-30H hardening): a PRESENT binding MUST yield a digest.
        # Never swallow a derivation error into None — that would silently disable the
        # atomic preflight->create pin and let an unpinned run proceed. None is used
        # ONLY for the legitimate "no binding present" case; a derivation error
        # propagates and cli.py maps it to a fatal (exit 6) refusal.
        binding = posture.get("effective_binding")
        self._expected_binding_digest = (
            _eb.binding_digest(binding) if isinstance(binding, dict) and binding else None
        )
        # The attested model-manifest digest (WAVE-30H #7), forwarded on create so the
        # worker re-probes and fails closed on a tag->manifest re-point. Absent when
        # the runtime did not attest one (ordinary not_probed run) -> not pinned.
        self._expected_model_digest = (
            (posture.get("engine_attestation") or {}).get("ollama_model_digest") or None
        )
        return posture

    def close(self) -> None:
        self._client.close()

    def run(self, scenario: Scenario, *, home: Path, workspace: Path,
            tenant: str, user: str, run_id: str,
            wait_timeout: float = 120.0) -> Observation:
        # Create + dispatch the run through the true security boundary.
        _max_runtime = None
        if self._limits and self._limits.get("max_runtime_seconds") is not None:
            _max_runtime = int(self._limits["max_runtime_seconds"])
        # Engine binding. Fail CLOSED when an engine is required but none resolves
        # (never run the worker default) AND when a scenario tries to override the
        # attested track engine (the live gate verified only the bound engine, so
        # a per-scenario engine must not silently dispatch an unattested one).
        engine = scenario.engine or self._engine
        if self._require_engine:
            if not engine:
                raise RuntimeError(
                    "local_runtime requires a bound engine (Track A eco.v01); none "
                    "resolved — refusing to dispatch on the worker's default model"
                )
            if self._engine and scenario.engine and scenario.engine != self._engine:
                raise RuntimeError(
                    f"scenario engine {scenario.engine!r} conflicts with the bound "
                    f"track engine {self._engine!r} — refusing to dispatch an "
                    "unattested engine"
                )
            engine = self._engine  # the attested engine is authoritative
        resp = self._client.create_run(
            agent=scenario.params.get("agent", "default"),
            task=scenario.params.get("task", scenario.title),
            engine=engine,
            max_runtime_seconds=_max_runtime,
            limits=self._limits,
            idempotency_key=scenario.params.get("idempotency_key"),
            expected_binding_digest=self._expected_binding_digest,
            expected_model_digest=self._expected_model_digest,
        )
        resp.raise_for_status()
        created = resp.json()
        durable_run_id = created.get("run_id") or run_id
        terminal = self._client.wait_terminal(durable_run_id, timeout=wait_timeout)
        events = terminal.get("events", [])
        detail_resp = self._client.get_run(durable_run_id)
        detail = detail_resp.json() if detail_resp.status_code == 200 else {}
        artifacts = self._artifacts(durable_run_id)

        return Observation(
            run_id=durable_run_id, tenant=tenant, user=user,
            mode=scenario.mode, seam=self.name, platform=platform_tag(),
            events=events, effects=detail.get("effects", []),
            artifacts=artifacts,
            durable_status=str(detail.get("status") or terminal.get("status") or ""),
            workspace=workspace, pre_hash=hash_tree(workspace),
            self_reported_success=bool(detail.get("result")),
            reported_usage=detail.get("usage"),
            timings={}, engine_pinned=engine,
            # Capture, from the run detail (non-secret names only):
            #  * bound_*      — the runtime's canonical effective binding (the
            #    identity the run was BOUND to, from runtime_effective_binding);
            #  * dispatched_* — what the worker ACTUALLY executed on (closing-run
            #    metadata). The recorder reconciles attested == bound == dispatched
            #    and fails closed on any drift (WAVE-30H 3-way check).
            provenance=self._identity_provenance(detail),
        )

    @staticmethod
    def _identity_provenance(detail: Dict[str, Any]) -> Dict[str, Any]:
        binding = detail.get("runtime_effective_binding")
        if not isinstance(binding, dict):
            binding = {}
        return {
            "seam": HttpRuntimeSeam.name,
            "bound_provider": binding.get("provider"),
            "bound_model": binding.get("model"),
            "bound_binding_version": binding.get("binding_version"),
            "dispatched_provider": detail.get("provider"),
            "dispatched_model": detail.get("model"),
        }

    def _artifacts(self, run_id: str) -> List[Dict[str, Any]]:
        try:
            resp = self._client.list_artifacts(run_id)
            if resp.status_code == 200:
                return resp.json().get("artifacts", [])
        except Exception:
            pass
        return []


# --------------------------------------------------------------------------- #
# Isolated-home fixture management                                             #
# --------------------------------------------------------------------------- #
class IsolatedHome:
    """A throwaway ``YOUTAB_AGENT_HOME`` + workspace pair, removed on close.

    The home is NEVER the real platform home (the harness process module also
    hard-refuses that); this class only allocates a fresh temp dir per run so
    concurrent runs never share journal/workspace state.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        base = None
        if root is not None:
            Path(root).mkdir(parents=True, exist_ok=True)
            base = str(root)
        self.home = Path(tempfile.mkdtemp(prefix="youtab-bench-", dir=base))
        self.workspace = self.home / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def __enter__(self) -> "IsolatedHome":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
