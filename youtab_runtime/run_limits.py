"""Authoritative per-run limit + cost enforcement for live benchmarking (WAVE-30B §8/§10/§11).

The runtime — not the harness — is the authority on limits. A run is created with a
:class:`RunLimits` set; the agent loop consults a :class:`RunLimitEnforcer` at the
top of every iteration (BEFORE any provider call that iteration, retries and
failover included) and after every provider call:

* ``pre_iteration`` atomically **reserves** a conservative worst-case EUR amount
  for the upcoming iteration (single-call worst case × a retry/failover
  multiplier) in the durable campaign ledger and checks the request / token /
  failure limits. It returns a stop reason (and the loop breaks) when a ceiling
  is reached — nothing is sent once a limit is hit.
* ``observe_call`` **reconciles** each completed provider call against reported
  usage, accumulates tokens, and counts failures for the failure-threshold /
  total-token gates.

Reservation-before-send + reconcile-after makes the €10 campaign ceiling
authoritative even across retries and failover: those reuse the iteration's
conservative reservation, and any residual is caught fail-closed on the next
iteration's reserve. Server-side ceilings (:data:`RUN_CEILINGS`) clamp every
per-run value so no run can request the old unbounded 500-iteration behaviour.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Callable, Optional

# --- server-side upper bounds (a run may request less, never more) ----------
# Chosen to sit at/above the WAVE-30B Stage-3 full profile (49 scenarios, 8
# iters/scenario, 392 requests, 1,000,000 tokens, 2h) while refusing the legacy
# unbounded 500-iteration / no-cost behaviour.
RUN_CEILINGS = {
    "max_iterations": 50,
    "max_requests": 500,
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 64_000,
    "max_total_tokens": 1_000_000,
    "max_runtime_seconds": 7_200,
    "max_concurrency": 8,
    "max_retries": 3,
    "failure_threshold": 1_000,
}

# The absolute hard cap for any single campaign, in EUR. A run's max_cost_eur may
# be lower but never higher.
CAMPAIGN_CEILING_EUR = Decimal("10.00")

_INT_FIELDS = (
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "max_iterations",
    "max_requests",
    "max_retries",
    "max_runtime_seconds",
    "max_concurrency",
    "failure_threshold",
)


class RunLimitError(ValueError):
    """A per-run limit set is invalid (negative, non-numeric, ...)."""


@dataclass(frozen=True)
class RunLimits:
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    max_total_tokens: Optional[int] = None
    max_iterations: Optional[int] = None
    max_requests: Optional[int] = None
    max_retries: Optional[int] = None
    max_runtime_seconds: Optional[int] = None
    max_concurrency: Optional[int] = None
    max_cost_eur: Optional[str] = None  # Decimal serialized as string
    failure_threshold: Optional[int] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @property
    def cost_ceiling(self) -> Optional[Decimal]:
        return Decimal(self.max_cost_eur) if self.max_cost_eur is not None else None

    @classmethod
    def validate_and_clamp(cls, raw: Optional[dict]) -> "RunLimits":
        """Coerce a raw create-run limit dict into a clamped, validated RunLimits.

        Unknown keys are ignored. Negative/zero/non-numeric values are rejected.
        Each value is clamped DOWN to its server ceiling; max_cost_eur is clamped
        to the €10 campaign ceiling.
        """
        raw = raw or {}
        values: dict = {}
        for field in _INT_FIELDS:
            if field not in raw or raw[field] is None:
                continue
            try:
                iv = int(raw[field])
            except (TypeError, ValueError) as exc:
                raise RunLimitError(f"{field} must be an integer") from exc
            if iv <= 0:
                raise RunLimitError(f"{field} must be positive")
            ceiling = RUN_CEILINGS.get(field)
            values[field] = min(iv, ceiling) if ceiling is not None else iv

        if raw.get("max_cost_eur") is not None:
            try:
                cost = Decimal(str(raw["max_cost_eur"]))
            except Exception as exc:  # noqa: BLE001
                raise RunLimitError("max_cost_eur must be a decimal amount") from exc
            if cost <= 0:
                raise RunLimitError("max_cost_eur must be positive")
            values["max_cost_eur"] = str(min(cost, CAMPAIGN_CEILING_EUR))

        return cls(**values)


@dataclass(frozen=True)
class EnforcerSnapshot:
    requests_made: int
    total_tokens: int
    consecutive_failures: int
    total_failures: int
    remaining_eur: Optional[str]
    stopped_reason: Optional[str]


class RunLimitEnforcer:
    """Run-scoped, authoritative limit + budget enforcer.

    ``worst_case_eur_fn(model, provider) -> Decimal`` and
    ``actual_eur_fn(model, provider, input_tokens, output_tokens, ...) ->
    Optional[Decimal]`` are injected so the enforcer is unit-testable without the
    pricing engine; the runtime wires the campaign_budget.price_* bridges.
    """

    def __init__(
        self,
        *,
        run_id: str,
        campaign_id: str,
        limits: RunLimits,
        model: str,
        provider: Optional[str] = None,
        reserve_fn: Callable[..., object],
        reconcile_fn: Callable[..., object],
        worst_case_eur_fn: Callable[..., Decimal],
        actual_eur_fn: Callable[..., Optional[Decimal]],
        retry_multiplier: int = 1,
    ) -> None:
        self.run_id = run_id
        self.campaign_id = campaign_id
        self.limits = limits
        self.model = model
        self.provider = provider
        self._reserve = reserve_fn
        self._reconcile = reconcile_fn
        self._worst_case = worst_case_eur_fn
        self._actual = actual_eur_fn
        self.retry_multiplier = max(1, int(retry_multiplier))

        self.requests_made = 0
        self.total_tokens = 0
        self.consecutive_failures = 0
        self.total_failures = 0
        self.stopped_reason: Optional[str] = None
        self._remaining_eur: Optional[Decimal] = None
        # Per-run cost accounting (in addition to the shared campaign ceiling):
        # reserved-so-far for THIS run, and per-iteration accumulated actuals so a
        # within-iteration retry reconcile SUMS rather than overwrites (M3).
        self._run_reserved_eur = Decimal("0")
        self._iter_actual: dict[int, Decimal] = {}
        self._iter_reserved: dict[int, Decimal] = {}

    # -- pre-send gate -------------------------------------------------------

    def pre_iteration(self, api_call_count: int) -> Optional[str]:
        """Called at loop top before the iteration's provider call(s).

        Returns a stop reason (loop should break) or None to proceed. Reserves a
        conservative worst-case amount for this iteration before anything is sent.
        """
        if self.stopped_reason:
            return self.stopped_reason

        lim = self.limits
        if lim.max_requests is not None and self.requests_made >= lim.max_requests:
            return self._stop("max_requests")
        if lim.max_total_tokens is not None and self.total_tokens >= lim.max_total_tokens:
            return self._stop("max_total_tokens")
        if (
            lim.failure_threshold is not None
            and self.consecutive_failures >= lim.failure_threshold
        ):
            return self._stop("failure_threshold")

        # Reserve worst-case EUR for this iteration (single call worst case ×
        # retry/failover multiplier). Fail closed on pricing/budget.
        try:
            per_call = self._worst_case(model=self.model, provider=self.provider)
        except Exception:  # noqa: BLE001 - PricingUnavailable and friends
            return self._stop("pricing_unavailable")
        reserve_amount = Decimal(per_call) * Decimal(self.retry_multiplier)

        # Per-run cost cap (limits.max_cost_eur), enforced BEFORE the shared
        # campaign reservation so a run cannot blow its own sub-budget even if the
        # campaign still has room.
        run_cap = lim.cost_ceiling
        if run_cap is not None and (self._run_reserved_eur + reserve_amount) > run_cap:
            return self._stop("run_cost_cap")

        try:
            self._reserve(
                self.campaign_id,
                api_request_id=f"{self.run_id}:iter:{api_call_count}",
                amount_eur=reserve_amount,
                model=self.model,
                provider=self.provider,
            )
        except Exception as exc:  # noqa: BLE001 - BudgetExceeded / ledger error
            if exc.__class__.__name__ == "BudgetExceeded":
                return self._stop("budget_ceiling")
            return self._stop("budget_error")

        self._run_reserved_eur += reserve_amount
        self._iter_reserved[api_call_count] = reserve_amount
        self.requests_made += 1
        return None

    # -- post-send reconcile -------------------------------------------------

    def observe_call(
        self,
        *,
        api_call_count: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        ok: bool = True,
    ) -> Optional[str]:
        """Called after a provider call completes. Reconciles the iteration's
        reservation with observed usage, accumulates tokens, tracks failures."""
        self.total_tokens += int(input_tokens) + int(output_tokens)
        if ok:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            self.total_failures += 1

        actual = None
        try:
            actual = self._actual(
                model=self.model,
                provider=self.provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )
        except Exception:  # noqa: BLE001 - keep the conservative reservation
            actual = None
        if actual is not None:
            # A within-iteration retry fires observe_call more than once for the
            # same iteration; SUM the attempts' actuals rather than overwrite so no
            # billed attempt is undercounted (M3).
            prior = self._iter_actual.get(api_call_count, Decimal("0"))
            self._iter_actual[api_call_count] = prior + Decimal(actual)
            reconcile_actual: Optional[str] = str(self._iter_actual[api_call_count])
        else:
            reconcile_actual = None  # unknown usage: keep the conservative reservation
        try:
            self._reconcile(
                self.campaign_id,
                api_request_id=f"{self.run_id}:iter:{api_call_count}",
                actual_eur=reconcile_actual,
            )
        except Exception:  # noqa: BLE001 - reconcile is best-effort; reservation stands
            pass

        lim = self.limits
        if lim.max_total_tokens is not None and self.total_tokens >= lim.max_total_tokens:
            return self._stop("max_total_tokens")
        if (
            lim.failure_threshold is not None
            and self.consecutive_failures >= lim.failure_threshold
        ):
            return self._stop("failure_threshold")
        return None

    def note_failure(self) -> Optional[str]:
        """Record a failed provider call (exception / error response) WITHOUT
        touching the ledger — the conservative reservation for the failed call is
        kept (it may have been billable). Feeds the failure-threshold gate."""
        self.consecutive_failures += 1
        self.total_failures += 1
        lim = self.limits
        if (
            lim.failure_threshold is not None
            and self.consecutive_failures >= lim.failure_threshold
        ):
            return self._stop("failure_threshold")
        return None

    # -- helpers -------------------------------------------------------------

    def _stop(self, reason: str) -> str:
        self.stopped_reason = reason
        return reason

    def snapshot(self) -> EnforcerSnapshot:
        return EnforcerSnapshot(
            requests_made=self.requests_made,
            total_tokens=self.total_tokens,
            consecutive_failures=self.consecutive_failures,
            total_failures=self.total_failures,
            remaining_eur=(str(self._remaining_eur) if self._remaining_eur is not None else None),
            stopped_reason=self.stopped_reason,
        )


# --- live-run wiring (WAVE-30B C1 fix) --------------------------------------


def attach_run_limit_enforcer(
    agent,
    *,
    limits: RunLimits,
    run_id: str,
    campaign_id: str,
    fx_usd_to_eur,
    fx_source: str,
    fx_asof: str,
    model: str,
    provider: Optional[str] = None,
    safety_margin: str = "0.15",
    db_path: Optional[str] = None,
) -> "RunLimitEnforcer":
    """Open the shared campaign, build a RunLimitEnforcer wired to the durable
    ledger + pricing bridges, attach it as ``agent._run_limit_enforcer``, and clamp
    ``agent.max_iterations`` to the run's limit. Raises on any failure — a live
    run that cannot construct a budget enforcer must NOT proceed unbudgeted.
    """
    from decimal import Decimal as _D

    from youtab_runtime import campaign_budget as cb

    # Open (idempotently) the ONE shared €10 campaign.
    cb.open_campaign(
        campaign_id,
        ceiling_eur=str(CAMPAIGN_CEILING_EUR),
        fx_usd_to_eur=str(fx_usd_to_eur),
        fx_source=fx_source,
        fx_asof=fx_asof,
        safety_margin=safety_margin,
        db_path=db_path,
    )

    wc_in = int(limits.max_input_tokens or 4096)
    wc_out = int(limits.max_output_tokens or 512)
    fx = _D(str(fx_usd_to_eur))

    def _worst_case(*, model, provider):
        return cb.price_worst_case_eur(
            model, max_input_tokens=wc_in, max_output_tokens=wc_out,
            fx_usd_to_eur=fx, safety_margin=safety_margin, provider=provider,
        )

    def _actual(*, model, provider, input_tokens, output_tokens,
                cache_read_tokens=0, cache_write_tokens=0):
        return cb.price_actual_eur(
            model, input_tokens=input_tokens, output_tokens=output_tokens,
            fx_usd_to_eur=fx, cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens, provider=provider,
        )

    def _reserve(campaign_id, **kw):
        return cb.reserve(campaign_id, db_path=db_path, **kw)

    def _reconcile(campaign_id, **kw):
        return cb.reconcile(campaign_id, db_path=db_path, **kw)

    # Reserve conservatively enough to cover this iteration's retries (M1): one
    # call worst-case × (1 + max_retries). Failover is disabled in the stage
    # profiles; if enabled, the campaign ceiling still fails closed next iteration.
    retry_multiplier = 1 + int(limits.max_retries or 0)

    enforcer = RunLimitEnforcer(
        run_id=run_id,
        campaign_id=campaign_id,
        limits=limits,
        model=model,
        provider=provider,
        reserve_fn=_reserve,
        reconcile_fn=_reconcile,
        worst_case_eur_fn=_worst_case,
        actual_eur_fn=_actual,
        retry_multiplier=retry_multiplier,
    )
    agent._run_limit_enforcer = enforcer
    # Clamp the loop's iteration bound to the run limit (the loop already honours
    # agent.max_iterations + agent.iteration_budget).
    if limits.max_iterations is not None:
        try:
            current = int(getattr(agent, "max_iterations", limits.max_iterations) or limits.max_iterations)
            agent.max_iterations = min(current, int(limits.max_iterations))
            budget = getattr(agent, "iteration_budget", None)
            if budget is not None and hasattr(budget, "max_total"):
                budget.max_total = min(int(budget.max_total), int(limits.max_iterations))
        except Exception:  # noqa: BLE001 - clamp is best-effort on top of the gate
            pass
    return enforcer


def _fx_from_env(env) -> Optional[tuple]:
    rate = (env.get("YOUTAB_AGENT_BENCHMARK_FX_USD_EUR") or "").strip()
    source = (env.get("YOUTAB_AGENT_BENCHMARK_FX_SOURCE") or "").strip()
    asof = (env.get("YOUTAB_AGENT_BENCHMARK_FX_ASOF") or "").strip()
    if not (rate and source and asof):
        return None
    return rate, source, asof


def attach_enforcer_from_environment(agent, *, run_id: str, limits_dict, env=None):
    """Wire a RunLimitEnforcer for a kanban runtime worker when a live-benchmark
    campaign is configured in the environment. Returns the enforcer, or None when
    no campaign is configured (normal runs). Raises RunLimitError when a campaign
    IS configured but enforcement cannot be constructed (fail-closed).
    """
    import os as _os

    env = env if env is not None else _os.environ
    campaign_id = (env.get("YOUTAB_AGENT_BENCHMARK_CAMPAIGN_ID") or "").strip()
    if not campaign_id:
        return None  # not a live-benchmark run — no enforcement, normal behaviour
    fx = _fx_from_env(env)
    if fx is None:
        raise RunLimitError(
            "live-benchmark campaign configured but FX snapshot "
            "(YOUTAB_AGENT_BENCHMARK_FX_USD_EUR/_SOURCE/_ASOF) is missing — refusing"
        )
    limits = RunLimits.validate_and_clamp(limits_dict or {})
    model = str(getattr(agent, "model", "") or "")
    provider = getattr(agent, "provider", None)
    if not model:
        raise RunLimitError("cannot build a run-limit enforcer without a model name")
    return attach_run_limit_enforcer(
        agent,
        limits=limits,
        run_id=run_id,
        campaign_id=campaign_id,
        fx_usd_to_eur=fx[0],
        fx_source=fx[1],
        fx_asof=fx[2],
        model=model,
        provider=provider,
        safety_margin=(env.get("YOUTAB_AGENT_BENCHMARK_FX_MARGIN") or "0.15").strip() or "0.15",
    )
