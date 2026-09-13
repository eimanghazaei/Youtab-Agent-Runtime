"""Agent-loop gate for the managed execution-tree budget (WAVE-30H R5).

Thin, unit-testable bridge between the conversation loop and the durable shared
``execution_tree_budget``. Active ONLY for managed runs (``agent`` carries
``_execution_tree_root``, set by ``worker_admission`` from the grant); a normal /
local-standalone run has no root and every function is a no-op, so the hot loop
is wholly unaffected.

Every failure path stops the run fail-closed: a run that cannot debit its shared
budget must not keep calling a provider.
"""

from __future__ import annotations

import os
from typing import Optional


def _root(agent) -> Optional[str]:
    # A managed root_run_id is always a non-empty STRING. Requiring str (not just
    # truthiness) makes every gate a no-op for a non-managed agent AND robust to
    # test doubles (a MagicMock auto-vivifies attributes as truthy Mocks).
    root = getattr(agent, "_execution_tree_root", None)
    return root if isinstance(root, str) and root else None


def _agent_instance_id(agent) -> str:
    return str(getattr(agent, "_subagent_id", None) or f"agent-{id(agent)}")


def acquire_delegation_permit(agent) -> None:
    """Acquire the managed max_concurrent_agents permit for a delegated child.

    No-op for a non-managed run (no ``_execution_tree_root``). Raises
    ``TreeConcurrencyExceeded`` / ``TreeDepthExceeded`` when the grant's tree
    ceiling is reached — the caller MUST refuse to run the child (fail-closed).
    Records the host pid + incarnation so a crash-leaked permit is reclaimable by
    the reaper (R5/R7). Idempotent per agent instance. Pair with
    :func:`release_delegation_permit` in a ``finally``.
    """
    root = _root(agent)
    if not root:
        return
    from youtab_runtime import execution_tree_budget as etb
    from youtab_runtime import process_incarnation as pi

    agent_id = _agent_instance_id(agent)
    depth = int(getattr(agent, "_delegate_depth", 1) or 1)
    etb.acquire_agent_slot(
        root,
        agent_id,
        depth=depth,
        pid=os.getpid(),
        incarnation=pi.current_incarnation(),
    )
    agent._delegation_permit = (root, agent_id)


def release_delegation_permit(agent) -> None:
    """Release a delegated child's concurrency permit. Idempotent and safe to call
    from any lifecycle end (success, failure, timeout, cancellation, retry)."""
    permit = getattr(agent, "_delegation_permit", None)
    if not permit:
        return
    root, agent_id = permit
    from youtab_runtime import execution_tree_budget as etb

    try:
        etb.release_agent_slot(root, agent_id)
    finally:
        agent._delegation_permit = None


def execution_tree_pre_iteration(agent, *, now=None) -> Optional[str]:
    """Loop-top hard gate. Returns a stop reason, or None to proceed.

    Enforces the shared tree deadline and debits ONE iteration against the whole
    tree (root + all delegated children share the same counter). A carried-over
    token-saturation stop from a prior iteration is honoured here too.
    """
    root = _root(agent)
    if not root:
        return None
    # A token debit in a prior iteration may have saturated the shared budget.
    carried = getattr(agent, "_tree_token_stop", None)
    if carried:
        return carried
    from youtab_runtime import execution_tree_budget as etb

    try:
        etb.check_deadline(root, now=now)
    except etb.TreeDeadlineExceeded:
        return "tree_deadline"
    except etb.TreeBudgetError:
        return "tree_budget_error"
    except Exception:  # noqa: BLE001 — SEC-9 #3: any infra failure fails CLOSED
        return "tree_budget_error"
    try:
        etb.consume(root, iterations=1)
    except etb.TreeBudgetExceeded as exc:
        return f"tree_{exc.dimension}"
    except etb.TreeBudgetError:
        return "tree_budget_error"
    except Exception:  # noqa: BLE001 — SEC-9 #3: DB lock / I/O / malformed => fail closed
        return "tree_budget_error"
    # SEC-9 #2: before the loop's provider call, refuse a PAID call the signed
    # monetary ceiling cannot afford (``max_cost_micros == 0`` => no paid calls at
    # all) or that we cannot price under a positive ceiling (fail closed). A
    # verified local-zero engine is never gated here.
    return execution_tree_cost_precheck(agent)


def execution_tree_debit_tokens(
    agent, *, input_tokens: int = 0, output_tokens: int = 0
) -> Optional[str]:
    """Debit actual tokens against the shared tree after a provider call.

    The tokens are already spent, so on overflow we saturate the remaining budget
    (best effort) and return a stop reason; the caller records it so the loop
    stops at the next iteration boundary rather than making another paid call.
    """
    root = _root(agent)
    if not root:
        return None
    used = int(input_tokens) + int(output_tokens)
    if used <= 0:
        return None
    from youtab_runtime import execution_tree_budget as etb

    try:
        etb.consume(root, tokens=used)
        return None
    except etb.TreeBudgetExceeded:
        try:
            remaining = etb.snapshot(root).remaining("tokens")
            if remaining > 0:
                etb.consume(root, tokens=remaining)
        except Exception:  # noqa: BLE001 — best-effort saturation; never re-raise
            pass
        return "tree_tokens"
    except etb.TreeBudgetError:
        return "tree_budget_error"
    except Exception:  # noqa: BLE001 — SEC-9 #3: DB lock / I/O / malformed usage
        # The debit failed on an infrastructure error, not a budget ceiling. The
        # tokens are already spent; fail CLOSED with a durable stop so the loop
        # cannot make another provider call, rather than swallowing and continuing.
        return "tree_budget_error"


def execution_tree_cost_precheck(agent) -> Optional[str]:
    """Pre-call monetary gate (SEC-9 #2). Returns a stop reason, or None to allow.

    Enforced against the SIGNED grant's ``max_cost_micros`` (opened into the tree
    at admission — never a hard-coded amount). Rules, in order:

    * non-managed run (no tree root) => no-op;
    * a verified local-zero engine (``classify_local_zero``) => allowed (€0);
    * a subscription-included route => allowed (not a metered paid call);
    * ``max_cost_micros == 0`` (a "spend nothing" grant): a KNOWN-PAID route is
      refused (``tree_cost_micros``); an unpriced/local route costs nothing and is
      allowed;
    * a POSITIVE ceiling with an unpriceable non-local route: we cannot enforce the
      euro ceiling => fail closed (``tree_cost_pricing_unavailable``);
    * a positive ceiling, priced route, budget exhausted => ``tree_cost_micros``.

    Fails closed (``tree_budget_error``) on any pricing/accounting error.
    """
    root = _root(agent)
    if not root:
        return None
    provider = getattr(agent, "provider", None)
    base_url = getattr(agent, "base_url", None)
    model = getattr(agent, "model", None)
    api_key = getattr(agent, "api_key", "") or ""
    try:
        from agent import usage_pricing as up
        from youtab_runtime import execution_tree_budget as etb

        if up.classify_local_zero(provider, base_url):
            return None
        route = up.resolve_billing_route(model, provider=provider, base_url=base_url)
        if getattr(route, "billing_mode", None) == "subscription_included":
            return None
        known_paid = up.has_known_pricing(model, provider, base_url, api_key)
        snap = etb.snapshot(root)
        if snap.max_cost_micros == 0:
            # "spend nothing": a known-paid provider is refused; an unpriced route
            # (deterministic/local) costs nothing and proceeds.
            return "tree_cost_micros" if known_paid else None
        if not known_paid:
            # A positive euro ceiling is set but we have no verified pricing for a
            # potentially-paid route — the ceiling cannot be enforced. Fail closed.
            return "tree_cost_pricing_unavailable"
        if snap.remaining("cost_micros") <= 0:
            return "tree_cost_micros"
        return None
    except Exception:  # noqa: BLE001 — SEC-9 #2/#3: pricing/accounting failure fails closed
        return "tree_budget_error"


def execution_tree_debit_cost(agent, *, amount_usd) -> Optional[str]:
    """Debit the ACTUAL provider cost of the call just made against the shared tree
    (SEC-9 #2). ``amount_usd`` is the priced cost (USD); it is converted to
    micro-USD with ceil rounding (conservative — accumulated rounding can never
    drift the tree over the signed ceiling). No-op for a non-managed run or a
    zero/none cost. On overflow the remaining budget is saturated and a durable
    stop is returned; any infra failure fails closed (``tree_budget_error``)."""
    root = _root(agent)
    if not root:
        return None
    if amount_usd is None:
        return None
    try:
        import math

        micros = int(math.ceil(float(amount_usd) * 1_000_000))
    except (TypeError, ValueError):
        return None
    if micros <= 0:
        return None
    from youtab_runtime import execution_tree_budget as etb

    try:
        etb.consume(root, cost_micros=micros)
        return None
    except etb.TreeBudgetExceeded:
        try:
            remaining = etb.snapshot(root).remaining("cost_micros")
            if remaining > 0:
                etb.consume(root, cost_micros=remaining)
        except Exception:  # noqa: BLE001 — best-effort saturation; never re-raise
            pass
        return "tree_cost_micros"
    except etb.TreeBudgetError:
        return "tree_budget_error"
    except Exception:  # noqa: BLE001 — SEC-9 #3: infra failure fails closed
        return "tree_budget_error"
