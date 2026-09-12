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
    try:
        etb.consume(root, iterations=1)
    except etb.TreeBudgetExceeded as exc:
        return f"tree_{exc.dimension}"
    except etb.TreeBudgetError:
        return "tree_budget_error"
    return None


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
        except etb.TreeBudgetError:
            pass
        return "tree_tokens"
    except etb.TreeBudgetError:
        return "tree_budget_error"
