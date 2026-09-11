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

logger = logging.getLogger(__name__)


class ManagedWorkerAdmissionError(RuntimeError):
    """A managed run could not establish its admitted context — refuse to run."""


# WAVE-30H: the generic "missing binding -> proceed" fallback is REMOVED. A managed
# run with no binding fails closed by default. A bounded migration window for
# pre-binding legacy runs is available ONLY behind this explicitly-dated, audited,
# fail-closed-on-expiry/parse-error flag (never "forever").
_LEGACY_QUARANTINE_ENV = "YOUTAB_MANAGED_BINDING_LEGACY_UNTIL"


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


def _enforce_effective_binding(agent, envelope, binding, task_id, *, require_client_match):
    """Validate the canonical effective binding and dispatch EXCLUSIVELY from it.

    Raises :class:`ManagedWorkerAdmissionError` (fail closed) on any invalid binding
    BEFORE the caller opens a provider client or consumes budget. Presence,
    integrity (hash), corruption and cross-tenant are enforced for every managed
    run; a fully-resolved substrate and provider/model/endpoint-drift-free dispatch
    are additionally enforced for a real-model worker (``require_client_match``).
    """
    from youtab_agent_cli import effective_binding as _eb

    # 1. Present (no silent legacy fallback).
    if binding is None:
        if _legacy_binding_quarantine_active():
            _audit_binding_quarantine(task_id, "missing_binding")
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
    # 5. Cross-tenant (defense in depth; ingress already binds tenant).
    env_tenant = getattr(envelope, "tenant_id", None)
    if binding.get("tenant") and env_tenant and binding.get("tenant") != env_tenant:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} binding tenant mismatch; refusing"
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


def _audit_binding_quarantine(task_id, reason) -> None:
    """Record an auditable event that a managed run proceeded under the legacy
    migration flag (so production can assert zero such events)."""
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
    except Exception:  # noqa: BLE001 — auditing must never mask the run
        logger.warning("could not record binding quarantine audit for %s", task_id)


def establish_managed_admission(agent) -> bool:
    """Attach the sealed Simorgh AdmittedCommand to ``agent`` for a managed run.

    Returns True when an admitted context was established; False when this is not
    a managed kanban run (local-standalone trust mode, or not a dispatched
    kanban worker). Raises :class:`ManagedWorkerAdmissionError` (fail closed)
    when a managed kanban run has no valid persisted grant.
    """
    from youtab_runtime import managed_execution as mx

    if mx.current_trust_mode() is not mx.TrustMode.MANAGED:
        return False  # standalone: no managed admission

    task_id = (os.environ.get("YOUTAB_AGENT_KANBAN_TASK") or "").strip()
    if not task_id:
        # Managed trust mode but not a kanban-dispatched worker. The ingress
        # endpoint is the admission authority for managed runs; there is nothing
        # to reconstruct here. The tool_executor gate still fails closed if a
        # tool is reached without an admitted context.
        return False

    from youtab_agent_cli import kanban_db as kb

    grant_header = None
    manifest_payload = None
    binding_payload = None
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
        # WAVE-30H: the canonical immutable effective binding this run was bound to
        # (None for a legacy run with no binding event).
        from youtab_agent_cli import effective_binding as _eb

        binding_payload = _eb.effective_binding_from_events(events)
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

    agent._admitted_command = admitted
    env = admitted.envelope
    # WAVE-30H: PRE-DISPATCH binding enforcement. The worker validates the canonical
    # effective binding and dispatches EXCLUSIVELY from it — inside this function,
    # which returns BEFORE run_conversation constructs any provider client or debits
    # any budget. Fail-closed (ManagedWorkerAdmissionError -> worker exit 3): a
    # missing / corrupt / tampered(hash) / cross-tenant binding is refused, and for a
    # real-model worker an unresolved binding or any provider/model/endpoint drift
    # from the bound identity is refused. The deterministic/stub worker (no provider
    # client) enforces presence/integrity/tenant only.
    _require_client_match = bool(
        getattr(agent, "provider", None) and getattr(agent, "model", None)
    )
    _enforce_effective_binding(
        agent, env, binding_payload, task_id,
        require_client_match=_require_client_match,
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
