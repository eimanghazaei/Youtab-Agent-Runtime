"""Establish the sealed Simorgh admitted execution context in a managed worker.

WAVE-30H R3 — the worker side of the admission-to-execution contract. A managed
run is dispatched to a worker subprocess (see ``kanban_db._default_spawn``); the
ingress endpoint already verified the Simorgh grant, consumed its single-use
nonce, and persisted it as a ``runtime_execution_grant`` event. The sealed
``AdmittedCommand`` carries a per-process proof and cannot cross the process
boundary, so the worker must reconstruct it here: load the persisted grant,
re-verify it (signature/expiry/scope) via ``re_admit_worker_grant`` (which does
NOT re-burn the nonce), and attach it to the agent so the tool_executor
authority gate can consume it before every real tool call.

Fail-closed: in ``managed`` trust mode a kanban run with no valid grant raises
:class:`ManagedWorkerAdmissionError` and the caller MUST refuse to execute.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ManagedWorkerAdmissionError(RuntimeError):
    """A managed run could not establish its admitted context — refuse to run."""


# WAVE-30H: the generic "missing binding -> proceed" fallback is REMOVED. A managed
# run with no binding fails closed by default. A bounded migration window for
# GENUINELY pre-binding-contract runs is available ONLY behind BOTH of these
# explicitly-set, audited, fail-closed flags (never "forever", never global):
#   * _LEGACY_QUARANTINE_ENV   — a FUTURE ISO date the window stays open until;
#   * _LEGACY_CREATED_BEFORE_ENV — the epoch (int seconds) the binding contract
#     shipped. Only a run CREATED BEFORE this epoch is genuinely legacy and eligible
#     for the window; a post-contract run always persists a binding in its create
#     txn, so a missing binding on it is a defect/tamper and fails closed regardless.
# WAVE-30H #9 closes the previous "any future date bypasses missing-binding for ALL
# runs" fail-open: the window now requires positive pre-contract evidence AND a
# successful audit write.
_LEGACY_QUARANTINE_ENV = "YOUTAB_MANAGED_BINDING_LEGACY_UNTIL"
_LEGACY_CREATED_BEFORE_ENV = "YOUTAB_MANAGED_BINDING_LEGACY_CREATED_BEFORE"


def _legacy_binding_quarantine_active() -> bool:
    """True iff the legacy-binding migration flag names a FUTURE ISO date.

    Absent, unparseable, or past -> False (fail closed). The flag cannot be set to a
    non-date, so it can never be made permanent (mirrors the trust-mode posture).
    """
    raw = (os.environ.get(_LEGACY_QUARANTINE_ENV) or "").strip()
    if not raw:
        return False
    try:
        from datetime import datetime, timezone

        until = datetime.fromisoformat(raw)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < until
    except Exception:  # noqa: BLE001 — any parse failure fails closed
        return False


def _legacy_contract_cutoff_epoch() -> "int | None":
    """The operator-declared epoch the binding contract shipped, or None.

    A run is genuinely legacy ONLY if it was created BEFORE this epoch. Absent or
    unparseable -> None, so NO run qualifies for the quarantine (fail closed). This
    is what scopes the migration window to real pre-contract runs instead of every
    missing-binding run."""
    raw = (os.environ.get(_LEGACY_CREATED_BEFORE_ENV) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _enforce_effective_binding(
    agent, envelope, binding, task_id, *,
    require_client_match, authoritative_run_id, authoritative_workspace,
    created_at=None,
):
    """Validate the canonical effective binding and dispatch EXCLUSIVELY from it.

    Raises :class:`ManagedWorkerAdmissionError` (fail closed) on any invalid binding
    BEFORE the caller opens a provider client or consumes budget. Presence,
    integrity (hash), corruption, cross-tenant, and run/workspace SCOPE are enforced
    for every managed run; a fully-resolved substrate and provider/model/endpoint-
    drift-free dispatch are additionally enforced for a real-model worker
    (``require_client_match``).

    ``authoritative_run_id`` is the dispatcher-assigned run this worker process is
    actually executing (``YOUTAB_AGENT_KANBAN_TASK``) and ``authoritative_workspace``
    is the workspace the run's re-verified grant was admitted under — BOTH sourced
    from the persisted run/task context, INDEPENDENT of the binding payload's own
    self-reported ``run_id``/``workspace``. Comparing the binding's scope against
    them catches a wholesale, self-consistent binding lifted from ANOTHER run or
    workspace of the SAME tenant (which the self-hash and the tenant check cannot).
    """
    from youtab_agent_cli import effective_binding as _eb

    # 1. Present (no silent legacy fallback). WAVE-30H #9: the migration window is
    # NOT global — it applies ONLY to a run that (a) the operator opened the window
    # for (future-dated flag), AND (b) is GENUINELY pre-contract (created before the
    # operator-declared contract epoch), AND (c) whose quarantine audit write
    # SUCCEEDS. A post-contract run always persists a binding in its create txn, so a
    # missing binding on it is a defect/tamper and fails closed. Unknown created_at,
    # absent cutoff, or a swallowed audit all fail closed.
    if binding is None:
        cutoff = _legacy_contract_cutoff_epoch()
        genuinely_legacy = (
            _legacy_binding_quarantine_active()
            and cutoff is not None
            and created_at is not None
            and int(created_at) < cutoff
        )
        if genuinely_legacy:
            if not _audit_binding_quarantine(task_id, "missing_binding"):
                raise ManagedWorkerAdmissionError(
                    f"managed run {task_id} legacy-quarantine audit write failed; "
                    "refusing (no unaudited proceed)"
                )
            return
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} has no effective binding; refusing "
            "(no fallback, no standalone downgrade)"
        )
    # 2. Corrupt / malformed.
    if binding.get("__corrupt__"):
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} effective binding is corrupt; refusing"
        )
    # 3. Integrity — the self-hash must match (tamper/forgery fails closed).
    if not _eb.verify_binding(binding):
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} effective binding hash mismatch "
            "(tampered/malformed); refusing"
        )
    # 3b. Version policy (WAVE-30H #F7). The binding_hash algorithm is versioned;
    # verify_binding already fails closed on an UNKNOWN version, but be explicit and
    # apply the legacy-version migration policy. A pre-contract (v1) binding predates
    # model-digest hash coverage, so it can NEVER cryptographically possess attested
    # model-digest protection, and it is accepted ONLY inside the same bounded/audited
    # migration window as a missing binding (default = drain/fail-closed: runs must
    # upgrade to the current binding contract).
    _bver = binding.get("binding_version")
    if _bver not in _eb.SUPPORTED_BINDING_VERSIONS:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} unsupported binding_version {_bver!r}; refusing"
        )
    if _bver < _eb.CURRENT_BINDING_VERSION:
        if binding.get("digest_status") == "attested":
            raise ManagedWorkerAdmissionError(
                f"managed run {task_id} legacy v{_bver} binding claims attested model "
                "digest it cannot cryptographically possess; refusing"
            )
        _cutoff = _legacy_contract_cutoff_epoch()
        _legacy_ok = (
            _legacy_binding_quarantine_active()
            and _cutoff is not None
            and created_at is not None
            and int(created_at) < _cutoff
        )
        if not _legacy_ok:
            raise ManagedWorkerAdmissionError(
                f"managed run {task_id} legacy binding_version v{_bver} refused "
                "(drain: runs must upgrade to the current binding contract); refusing"
            )
        if not _audit_binding_quarantine(task_id, f"legacy_binding_v{_bver}"):
            raise ManagedWorkerAdmissionError(
                f"managed run {task_id} legacy-binding audit write failed; refusing"
            )
    # 5. Cross-tenant (defense in depth; ingress already binds tenant).
    env_tenant = getattr(envelope, "tenant_id", None)
    if binding.get("tenant") and env_tenant and binding.get("tenant") != env_tenant:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} binding tenant mismatch; refusing"
        )
    # 6. Run-scope — the binding must belong to THE RUN this worker is executing,
    # not merely to some run of the same tenant. ``authoritative_run_id`` is the
    # dispatcher-assigned run (YOUTAB_AGENT_KANBAN_TASK), an INDEPENDENT source from
    # the binding's self-reported run_id, so a wholesale same-tenant binding copied
    # from another run (valid self-hash, matching tenant) is caught here. A binding
    # with no run scope is malformed/stale and refused. Enforced for EVERY managed
    # run (before any provider client or budget debit), not only real-model workers.
    b_run = binding.get("run_id")
    if not b_run:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} effective binding carries no run scope "
            "(stale/malformed); refusing"
        )
    if authoritative_run_id and b_run != authoritative_run_id:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} binding run_id does not match the executing run "
            "(cross-run/stale binding substitution); refusing"
        )
    # 7. Workspace-scope — the binding's workspace must equal the workspace this run
    # was admitted under (the re-verified grant envelope's workspace_id). Defends
    # against a same-tenant CROSS-WORKSPACE binding substitution. Both sides
    # normalize the unscoped sentinel so an unscoped run is not a spurious mismatch.
    from youtab_agent_cli.runtime_command_auth import WORKSPACE_UNSCOPED as _WS_UNSCOPED

    b_ws = binding.get("workspace") or _WS_UNSCOPED
    auth_ws = authoritative_workspace or _WS_UNSCOPED
    if b_ws != auth_ws:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} binding workspace does not match the admitted "
            "workspace (cross-workspace binding substitution); refusing"
        )
    if not require_client_match:
        return  # deterministic/stub worker: no provider client, no substrate to match
    # 4. Fully resolved — a real-model worker must have a concrete bound substrate.
    if (
        binding.get("model_identifier_status") != "resolved"
        or not binding.get("model")
        or not binding.get("provider")
    ):
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} effective binding is not fully resolved; refusing"
        )
    # 9. Dispatch-from-it — the worker's resolved substrate must EQUAL the binding
    # (no config drift). Fingerprint compares the normalized endpoint authority.
    a_provider = (getattr(agent, "provider", None) or "").strip().lower()
    a_model = (getattr(agent, "model", None) or "").strip()
    if a_provider and a_provider != str(binding.get("provider") or "").lower():
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} worker provider drifted from the bound identity; refusing"
        )
    if a_model and a_model != str(binding.get("model") or ""):
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} worker model drifted from the bound identity; refusing"
        )
    a_base = getattr(agent, "base_url", None)
    if a_base is not None and _eb.compute_endpoint_fingerprint(a_base) != binding.get(
        "endpoint_fingerprint"
    ):
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} worker endpoint drifted from the bound identity; refusing"
        )
    # 10. Model-manifest digest (WAVE-30H #7). When the binding pins an ATTESTED
    # manifest digest, re-probe the live manifest at the worker's (now drift-checked)
    # endpoint and constant-time compare — the LAST point before the provider client
    # is built and budget is debited. Closes the tag->manifest TOCTOU (a tag
    # re-pointed between the preflight attestation and execution). Only for an
    # attested binding; an ordinary not_probed run is never probed here (no regression).
    if binding.get("digest_status") == "attested":
        import hmac as _hmac

        from agent.model_metadata import query_ollama_model_digest as _probe

        _live = (
            _probe(binding.get("model"), getattr(agent, "base_url", "") or "") or ""
        ).strip().lower()
        _pinned = str(binding.get("model_digest") or "").strip().lower()
        if not _live or not _pinned or not _hmac.compare_digest(_live, _pinned):
            raise ManagedWorkerAdmissionError(
                f"managed run {task_id} model manifest digest drifted from the attested "
                "binding (tag re-pointed); refusing"
            )


def _audit_binding_quarantine(task_id, reason) -> bool:
    """Record an auditable event that a managed run proceeded under the legacy
    migration flag (so production can assert zero such events).

    Returns True iff the audit event was durably written. WAVE-30H #9: a swallowed
    audit-write failure must NOT let the run proceed unaudited — the caller refuses
    when this returns False."""
    try:
        from youtab_agent_cli import kanban_db as kb

        conn = kb.connect()
        try:
            with kb.write_txn(conn):
                kb._append_event(
                    conn, task_id, "runtime_binding_quarantine",
                    {"reason": reason, "flag_until": os.environ.get(_LEGACY_QUARANTINE_ENV)},
                )
        finally:
            conn.close()
        return True
    except Exception:  # noqa: BLE001 — audit failure fails closed (caller refuses)
        logger.warning("could not record binding quarantine audit for %s", task_id)
        return False


class _ManagedGrantContext:
    """The loaded + re-admitted grant for a managed kanban worker (no side effects)."""

    __slots__ = ("task_id", "admitted", "capability_binding", "binding", "created_at")

    def __init__(self, task_id, admitted, capability_binding, binding, created_at):
        self.task_id = task_id
        self.admitted = admitted
        self.capability_binding = capability_binding
        self.binding = binding
        self.created_at = created_at


def _load_managed_grant_context():
    """Load + re-admit THIS kanban worker's persisted grant. Pure verification.

    Returns None when this is not a managed kanban worker (standalone trust mode, or
    not a dispatched kanban worker). Raises :class:`ManagedWorkerAdmissionError`
    (fail closed) on a missing grant, a manifest integrity failure, or a grant
    re-admission failure (bad signature / expiry / scope). Attaches nothing, opens no
    budget, installs no namespace — safe to run BEFORE the agent (and its provider
    client) is constructed.
    """
    from youtab_runtime import managed_execution as mx

    if mx.current_trust_mode() is not mx.TrustMode.MANAGED:
        return None  # standalone: no managed admission

    task_id = (os.environ.get("YOUTAB_AGENT_KANBAN_TASK") or "").strip()
    if not task_id:
        # Managed trust mode but not a kanban-dispatched worker. The ingress
        # endpoint is the admission authority for managed runs; nothing to
        # reconstruct here (the tool_executor gate still fails closed without an
        # admitted context).
        return None

    from youtab_agent_cli import effective_binding as _eb
    from youtab_agent_cli import kanban_db as kb

    grant_header = None
    manifest_payload = None
    binding_payload = None
    created_at = None
    conn = kb.connect()
    try:
        events = list(kb.list_events(conn, task_id))
        for event in events:
            kind = getattr(event, "kind", None)
            payload = getattr(event, "payload", None) or {}
            if kind == "runtime_execution_grant":
                grant_header = payload.get("grant") or grant_header
            elif kind == "runtime_capability_manifest":
                manifest_payload = payload or manifest_payload
        # The canonical immutable binding this run was bound to (None for a legacy
        # run with no binding event).
        binding_payload = _eb.effective_binding_from_events(events)
        # created_at scopes the legacy-binding quarantine to genuinely pre-contract
        # runs (WAVE-30H #9); best-effort — unknown fails closed at the gate.
        try:
            _task = kb.get_task(conn, task_id)
            created_at = getattr(_task, "created_at", None)
        except Exception:  # noqa: BLE001
            created_at = None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if not grant_header:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} has no persisted Simorgh execution grant; "
            "refusing to execute (no bypass, no standalone fallback)"
        )

    # Reconstruct the SAME capability binding the ingress froze (correction 2), so
    # the worker enforces exactly the tool set authorized at admission — never a
    # tool registered after. A tampered persisted manifest fails the hash check.
    capability_binding = None
    if manifest_payload is not None:
        from youtab_agent_cli import capability_manifest as cm

        try:
            capability_binding = cm.binding_from_persisted(manifest_payload)
        except ValueError as exc:
            raise ManagedWorkerAdmissionError(
                f"managed run {task_id} capability manifest integrity failure"
            ) from exc

    try:
        admitted = mx.re_admit_worker_grant(
            grant_header=grant_header,
            boundary=mx.AuthorityBoundary(),
            public_keys=mx.load_brain_public_keys(),
            capability_binding=capability_binding,
        )
    except mx.ManagedAdmissionError as exc:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} grant re-admission failed: {exc.code}"
        ) from exc

    return _ManagedGrantContext(
        task_id, admitted, capability_binding, binding_payload, created_at
    )


@dataclass(frozen=True)
class ManagedPreadmission:
    """One immutable, verified pre-admission snapshot for a managed run.

    WAVE-30H Batch4 #1: the CLI previously loaded the grant + binding THREE times
    (``preadmit_managed_run`` -> ``managed_bound_identity`` -> ``establish_managed_
    admission``), a check-to-use TOCTOU where a later reload could observe a
    different binding than the one the route was validated against. This snapshot is
    produced by a SINGLE :func:`_load_managed_grant_context` load, carries every
    field the downstream stages need (bound identity for the route gate + the loaded
    grant context for final admission), and is threaded through pre-admission, route
    validation, and ``establish_managed_admission`` so every stage uses the SAME
    verified binding. Frozen => no stage can mutate it between check and use."""

    __slots__ = ("context",)
    context: "_ManagedGrantContext"

    @property
    def task_id(self) -> str:
        return self.context.task_id

    @property
    def binding(self) -> Optional[dict]:
        return self.context.binding

    def bound_identity(self) -> Dict[str, Any]:
        """The ``{task_id, provider, model, endpoint_fingerprint}`` the route gate
        compares against — derived from the already-verified binding on this
        snapshot (no reload)."""
        b = self.context.binding or {}
        return {
            "task_id": self.context.task_id,
            "provider": (b.get("provider") or ""),
            "model": (b.get("model") or ""),
            "endpoint_fingerprint": b.get("endpoint_fingerprint"),
        }


def load_verified_preadmission() -> "Optional[ManagedPreadmission]":
    """Load + verify THIS managed run's grant/binding EXACTLY ONCE and snapshot it.

    Returns ``None`` for a standalone / non-kanban run (no managed constraints).
    For a managed run it loads the persisted grant + binding a SINGLE time, runs the
    client-independent fail-closed gate (presence / integrity / corruption /
    cross-tenant / run-scope / workspace-scope — ``require_client_match=False``, so
    NO provider client and NO network), and returns an immutable
    :class:`ManagedPreadmission` the caller threads through the route gate and
    :func:`establish_managed_admission`. Raises :class:`ManagedWorkerAdmissionError`
    (fail closed) on any invalid grant/binding — BEFORE any credential resolution,
    client construction, socket, or budget. Replaces the
    ``preadmit_managed_run()`` + ``managed_bound_identity()`` double-load.
    """
    ctx = _load_managed_grant_context()
    if ctx is None:
        return None
    env = ctx.admitted.envelope
    _enforce_effective_binding(
        None, env, ctx.binding, ctx.task_id,
        require_client_match=False,
        authoritative_run_id=ctx.task_id,
        authoritative_workspace=getattr(env, "workspace_id", None),
        created_at=ctx.created_at,
    )
    # The client-independent gate above already refuses a missing/corrupt binding;
    # be explicit so a managed run can never carry a None/corrupt binding past here.
    b = ctx.binding
    if not isinstance(b, dict) or b.get("__corrupt__"):
        raise ManagedWorkerAdmissionError(
            f"managed run {ctx.task_id} has no usable effective binding "
            "(missing/corrupt); refusing before credential resolution"
        )
    return ManagedPreadmission(context=ctx)


def preadmit_managed_run() -> bool:
    """Fail-closed managed gate that runs BEFORE the agent/provider client is built.

    WAVE-30H #2: the CLI previously constructed ``AIAgent`` (which resolves a provider
    client and performs Ollama/OpenRouter/LM-Studio HTTP) BEFORE admission, so a
    managed run with a missing / forged / expired grant or a missing / corrupt /
    tampered / cross-tenant / cross-run / cross-workspace binding did all that client
    construction and network I/O before the gate refused. This runs the
    CLIENT-INDEPENDENT checks first (no agent needed), so the caller can refuse and
    exit BEFORE any client or network. The provider/model/endpoint DRIFT + attested
    model-digest re-probe still run post-construction in
    :func:`establish_managed_admission` (``require_client_match=True``). Returns False
    when this is not a managed kanban worker; raises
    :class:`ManagedWorkerAdmissionError` (fail closed) on any invalid grant/binding.
    """
    ctx = _load_managed_grant_context()
    if ctx is None:
        return False
    env = ctx.admitted.envelope
    _enforce_effective_binding(
        None, env, ctx.binding, ctx.task_id,
        require_client_match=False,
        authoritative_run_id=ctx.task_id,
        authoritative_workspace=getattr(env, "workspace_id", None),
        created_at=ctx.created_at,
    )
    return True


def managed_bound_identity():
    """The BOUND provider/model/endpoint identity for the current managed run, or None.

    Returns ``None`` when this is not a managed kanban worker (standalone trust mode,
    or not a dispatched kanban worker) — the CLI treats a ``None`` return as "no
    managed constraints apply". For a managed run it returns the bound substrate
    identity ``{task_id, provider, model, endpoint_fingerprint}`` derived from the
    persisted, already-verified effective binding. Reuses the same pure verification
    as :func:`preadmit_managed_run` (no nonce re-burn, no authority broadening),
    and FAILS CLOSED (:class:`ManagedWorkerAdmissionError`) when the managed binding
    is missing or corrupt — so a managed run can never resolve credentials for, or
    construct a provider client on, an UNBOUND substrate.

    Used by the CLI to (a) forbid credential fallback for managed runs and (b) compare
    the resolved route to the binding BEFORE any provider client is constructed
    (WAVE-30H Batch3 #1). The full post-construction drift + model-digest re-probe in
    :func:`establish_managed_admission` still runs as the last line of defense.
    """
    ctx = _load_managed_grant_context()
    if ctx is None:
        return None
    b = ctx.binding
    if not isinstance(b, dict) or b.get("__corrupt__"):
        raise ManagedWorkerAdmissionError(
            f"managed run {ctx.task_id} has no usable effective binding "
            "(missing/corrupt); refusing before credential resolution"
        )
    return {
        "task_id": ctx.task_id,
        "provider": (b.get("provider") or ""),
        "model": (b.get("model") or ""),
        "endpoint_fingerprint": b.get("endpoint_fingerprint"),
    }


def assert_route_matches_binding(identity, *, provider, model, base_url) -> None:
    """STRICT pure route-vs-binding gate — NO provider client, NO network, NO budget.

    ``identity`` is a bound-identity dict (from
    :meth:`ManagedPreadmission.bound_identity` / :func:`managed_bound_identity`;
    ``None`` for a standalone run => no-op). For a managed run the PROPOSED provider,
    model, AND endpoint fingerprint must ALL be present AND equal to the bound
    substrate; a missing/unresolved field OR any drift raises
    :class:`ManagedWorkerAdmissionError`.

    WAVE-30H Batch4 #1: this now runs against the PURELY-PLANNED route (see
    :func:`runtime_provider.plan_runtime_route`) BEFORE credential resolution, so a
    managed run whose configured route drifts from — or underspecifies — its binding
    is refused before any OAuth mint / key refresh / credential-pool I/O / provider
    client / socket. The previous "compare each field only when BOTH sides are
    present" was the defect: an absent side slipped through. An absent side now FAILS
    CLOSED. The full post-construction drift + attested model-digest re-probe in
    :func:`establish_managed_admission` remains the last line of defense.
    """
    if not identity:
        return
    from youtab_agent_cli import effective_binding as _eb

    task_id = identity.get("task_id") or "?"

    r_provider = (provider or "").strip().lower()
    b_provider = str(identity.get("provider") or "").strip().lower()
    if not r_provider or not b_provider or r_provider != b_provider:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} resolved provider "
            f"{r_provider or '<missing>'!r} drifted from the bound identity "
            f"{b_provider or '<missing>'!r}; refusing before client construction"
        )
    r_model = (model or "").strip()
    b_model = str(identity.get("model") or "").strip()
    if not r_model or not b_model or r_model != b_model:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} resolved model drifted from the bound identity "
            "(missing or mismatched); refusing before client construction"
        )
    b_fp = identity.get("endpoint_fingerprint")
    r_fp = _eb.compute_endpoint_fingerprint(base_url) if base_url else ""
    if not base_url or not b_fp or r_fp != b_fp:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} resolved endpoint drifted from the bound identity "
            "(missing or mismatched); refusing before client construction"
        )


def establish_managed_admission(agent, *, snapshot: "Optional[ManagedPreadmission]" = None) -> bool:
    """Attach the sealed Simorgh AdmittedCommand to ``agent`` for a managed run.

    Returns True when an admitted context was established; False when this is not
    a managed kanban run (local-standalone trust mode, or not a dispatched
    kanban worker). Raises :class:`ManagedWorkerAdmissionError` (fail closed)
    when a managed kanban run has no valid persisted grant.

    WAVE-30H Batch4 #1: when the caller already produced a verified
    :class:`ManagedPreadmission` (the CLI paths do, via
    :func:`load_verified_preadmission`), it is passed in as ``snapshot`` and REUSED
    here — the grant/binding are NOT reloaded, so final admission enforces the exact
    same binding the pre-credential route gate validated (no check-to-use TOCTOU).
    Callers without a snapshot (e.g. the pooled worker) still load here. This runs
    the full gate (idempotent, no nonce re-burn) and adds the provider/model/endpoint
    drift + attested model-digest re-probe now that the agent's substrate is known.
    """
    ctx = snapshot.context if snapshot is not None else _load_managed_grant_context()
    if ctx is None:
        return False
    task_id = ctx.task_id
    admitted = ctx.admitted
    binding_payload = ctx.binding
    agent._admitted_command = admitted
    env = admitted.envelope
    # WAVE-30H: PRE-DISPATCH binding enforcement. The worker validates the canonical
    # effective binding and dispatches EXCLUSIVELY from it — inside this function,
    # which returns BEFORE run_conversation constructs any provider client or debits
    # any budget. Fail-closed (ManagedWorkerAdmissionError -> worker exit 3): a
    # missing / corrupt / tampered(hash) / cross-tenant binding is refused, and for a
    # real-model worker an unresolved binding or any provider/model/endpoint drift
    # from the bound identity is refused. The deterministic/stub worker (no provider
    # client) enforces presence/integrity/tenant/run-scope/workspace-scope only.
    #
    # The authoritative run/workspace context is sourced from the PERSISTED run/task
    # context — the dispatcher-assigned run this worker serves (task_id ==
    # YOUTAB_AGENT_KANBAN_TASK) and the workspace the run's re-verified grant was
    # admitted under (env.workspace_id) — NEVER from the binding payload's own
    # self-reported scope, so a cross-run/cross-workspace substituted binding fails
    # closed here, before any provider client or budget debit.
    _require_client_match = bool(
        getattr(agent, "provider", None) and getattr(agent, "model", None)
    )
    _enforce_effective_binding(
        agent, env, binding_payload, task_id,
        require_client_match=_require_client_match,
        authoritative_run_id=task_id,
        authoritative_workspace=getattr(env, "workspace_id", None),
        created_at=ctx.created_at,
    )
    # Passed enforcement — attach the binding as the SOLE dispatch authority so a
    # delegated child inherits the EXACT parent identity (or fails closed).
    agent._runtime_effective_binding = (
        binding_payload
        if isinstance(binding_payload, dict) and not binding_payload.get("__corrupt__")
        else None
    )
    # R5: open the ONE durable shared execution-tree budget from the grant's
    # reasoning, keyed on root_run_id, UNCONDITIONALLY for this managed run (never
    # gated on a benchmark campaign env var). The root run seeds it; delegated
    # children re-open the SAME key and only debit the remaining budget. Expose
    # the root id + reasoning-derived RunLimits so the agent loop can enforce.
    env = admitted.envelope
    reasoning = getattr(env, "reasoning", None)
    if reasoning is not None and hasattr(reasoning, "max_cost_micros"):
        from youtab_runtime import execution_tree_budget as etb

        root_run_id = getattr(env, "root_run_id", None) or getattr(env, "task_id")
        try:
            etb.open_tree(
                root_run_id, etb.reasoning_to_tree_params(reasoning)
            )
            agent._execution_tree_root = root_run_id
            agent._execution_tree_limits = etb.reasoning_to_run_limits(reasoning)
        except Exception as exc:  # noqa: BLE001 - a managed run must not proceed unbudgeted
            raise ManagedWorkerAdmissionError(
                f"managed run {task_id} could not open its execution-tree budget"
            ) from exc

    # R4 (corrected): physically namespace the memory store by the grant's
    # tenant/workspace so this managed run can never read or write another
    # tenant's memory. The namespace is installed as an IMMUTABLE PER-RUN context
    # (contextvars), taken from the admitted grant envelope — NOT a mutable
    # process-global env var, which would leak across concurrent runs in a
    # process serving more than one. This worker is a single-run subprocess, so
    # setting it once here scopes the whole run. Read by
    # tools.memory_tool.get_memory_dir; sanitized there against path traversal.
    try:
        from tools import memory_tool as _mt

        _mt.set_memory_namespace(env.tenant_id, env.workspace_id)
    except Exception:  # noqa: BLE001 — memory tool optional; fail-closed handled below
        # If the memory tool cannot be imported the run has no memory surface to
        # isolate; the tool_executor authority gate still governs every call.
        logger.warning("memory tool unavailable; no per-run namespace installed")
    logger.info(
        "managed run %s: Simorgh admitted execution context established", task_id
    )
    return True
