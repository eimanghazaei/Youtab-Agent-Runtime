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

from typing import Optional


def _root(agent) -> Optional[str]:
    return getattr(agent, "_execution_tree_root", None)


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
