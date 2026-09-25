"""WAVE-27 ambient egress context.

The audited egress boundary (:mod:`youtab_runtime.egress_audit` /
:mod:`youtab_runtime.egress_guard_http`) needs a ``(run_id, principal)`` to
journal a decision against a run. Most in-process outbound call sites — the
SSRF-safe httpx callers in ``tools``/``gateway``/``plugins``, the CLI, the
dashboard plane, and bootstrap — do not have a run/principal threaded into
scope. Rather than change every call signature (which would be a large,
untestable, non-mechanical migration), the runtime sets an **ambient** context
while it executes a run, and the audited factories read it.

Adoption status (WAVE-27, honest): this is the mechanism the real agent worker
is INTENDED to adopt by wrapping run execution in :func:`egress_run_context`, but
that production wiring is **not yet in place** — today the context is entered only
by the benchmark harness and tests. Until the real worker adopts it,
``in_run_context()`` is False in production, so ``tools/url_safety`` builds the
plain (still SSRF-guarded) client and the per-request egress journal is exercised
by the deterministic benchmark, not live runs. Wiring the real worker (and
verifying it end-to-end with a live provider) is ``PENDING_OWNER_ACTION`` — see
``docs/security/EGRESS_EXCEPTIONS.md``. SSRF connect-time enforcement and the CI
lint gate are unaffected by this and apply regardless.

Two zones:

* **In-run**: when a run enters :func:`egress_run_context`, outbound traffic is
  attributed and journalled to that run + principal — where the agentic security
  model cares about egress (network side effects judged from observable state).
* **System / out-of-run**: CLI, dashboard, bootstrap and platform-infra egress
  that is not part of an agent run. There is no run to attribute to; the
  well-defined :func:`system_principal` / :data:`SYSTEM_RUN_ID` fallback is used.
  Per-request journaling for this zone is intentionally opt-in (see
  ``tools/url_safety`` observe wiring) so high-volume infra traffic does not
  flood the system journal; construction-time enforcement (the SSRF guard and
  the CI lint gate that bans un-audited client construction) still applies.

This module has no side effects and imports only the journal ``Principal`` type,
so it is safe to import from any layer.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from youtab_runtime.run_journal import Principal

#: Well-known system principal / run id for egress that is not part of a run.
SYSTEM_TENANT = "_system"
SYSTEM_USER = "_system"
SYSTEM_RUN_ID = "_system"


def system_principal() -> Principal:
    """The fallback principal for out-of-run (CLI/dashboard/bootstrap) egress."""
    return Principal(SYSTEM_TENANT, SYSTEM_USER)


@dataclass(frozen=True)
class EgressContext:
    """The run + principal outbound traffic is currently attributed to."""

    run_id: str
    principal: Principal


_current: "contextvars.ContextVar[Optional[EgressContext]]" = contextvars.ContextVar(
    "youtab_egress_context", default=None
)


def current_context() -> EgressContext:
    """Return the active egress context, or the SYSTEM fallback if none is set."""
    ctx = _current.get()
    if ctx is None:
        return EgressContext(run_id=SYSTEM_RUN_ID, principal=system_principal())
    return ctx


def in_run_context() -> bool:
    """True iff an explicit (non-system) run context is currently active."""
    return _current.get() is not None


@contextmanager
def egress_run_context(run_id: str, principal: Principal) -> Iterator[None]:
    """Attribute outbound egress within this block to ``(run_id, principal)``.

    Re-entrant/nestable via contextvars; always restores the prior context.
    """
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal instance")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    token = _current.set(EgressContext(run_id=run_id, principal=principal))
    try:
        yield
    finally:
        _current.reset(token)
