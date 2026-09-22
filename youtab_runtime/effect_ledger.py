"""WAVE-26 durable effect-level idempotency ledger (frozen contract 5/8).

Today the runtime has *no* effect-level ledger: a re-dispatch re-runs the whole
worker body and every side effect inside it, and the ``/api/runtime/v1/.../retry``
endpoint plus the in-process ``/v1/runs`` path create fresh work with no effect
guard at all. So a crash-then-requeue (or an operator retry) can post the same
webhook twice, write the same file twice, or complete the same kanban card twice.

This module gives every externally observable side effect a *stable identity* and
a durable, principal-bound state machine so that:

  * the same logical effect under retry/restart resolves to the **same**
    ``effect_id`` (derived from the authorized run + logical action + target
    scope, never from a client field and never from the wall clock);
  * an effect may **commit at most once** — a second commit is a no-op that
    returns ``committed`` (this is the idempotency guarantee);
  * a side effect stranded by a crash becomes ``unknown``
    ("whether it ran is unknown") and is **never** blindly retried;
  * one principal can **never** read or replay another principal's effect.

Design is grounded in ``cron/executions.py`` (the only durable, crash-safe
state machine in the tree today): fail-safe ``(pid, start_time)`` ownership,
``recover_interrupted`` marks provably-abandoned work ``unknown`` without ever
scheduling a retry, and terminal states are immutable.

Storage: the ledger lives in the **same** ``run_journal.db`` as the event
journal and is written through the journal's ``BEGIN IMMEDIATE`` single-writer
transaction. Each state transition is *atomically* paired with a ``category=
'effect'`` journal event (``dedupe_key = f"{effect_id}:{state}"``) inside one
transaction, so the ledger row and its audit event can never diverge across a
crash. The ledger owns its own ``effects`` table via an idempotent
``CREATE TABLE IF NOT EXISTS`` that coexists with the journal's schema.

Provider idempotency keys are derived deterministically from ``effect_id`` for
the services that actually accept them (billing / modal / browser per WAVE-26
recon). Model-inference calls expose no provider key, so for those the guard is
in-runtime only — we do **not** claim exactly-once where the external system
cannot prove it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from youtab_runtime import redaction
from youtab_runtime.run_journal import (
    EVENT_SCHEMA_VERSION,
    Principal,
    RunJournalError,
    _immediate_txn,
    _read_conn,
    default_db_path,
)
from youtab_runtime.run_states import (
    EFFECT_TERMINAL_STATES,
    EffectState,
    effect_transition_allowed,
)

#: Unique id for *this* OS process, recorded as the owner of any effect it
#: begins. Mirrors ``cron/executions._PROCESS_ID`` — used so ``recover_interrupted``
#: never rewrites an effect owned by the live process that is still driving it.
_PROCESS_ID = uuid.uuid4().hex

#: Effect id length in hex chars (128 bits) — enough to make accidental
#: collisions across the ledger negligible while keeping ids compact.
_EFFECT_ID_LEN = 32

#: States from which the caller is allowed to *perform* the underlying side
#: effect. Everything else (in_progress / any terminal / unknown /
#: reconciliation_required) means "do NOT execute": either someone else is
#: doing it, it is already settled, or its outcome is unproven and a blind
#: retry could double-apply it.
EXECUTABLE_STATES = frozenset({EffectState.PLANNED, EffectState.AUTHORIZED})

#: Providers that accept a client-supplied idempotency key (WAVE-26 recon R5:
#: browser provider ``X-Idempotency-Key``, modal ``x-idempotency-key``, billing
#: ``Idempotency-Key``). Model-inference providers are deliberately absent.
_PROVIDER_IDEMPOTENCY_SUPPORTED = frozenset(
    {"billing", "modal", "browser", "browserbase"}
)

_UNKNOWN_ON_RECOVER_DETAIL = (
    "Runtime restarted after this effect's owner exited before a durable "
    "terminal state; whether the side effect ran is unknown."
)


class EffectLedgerError(RunJournalError):
    """Fail-closed contract violation (bad principal / missing effect / etc.).

    Subclasses :class:`RunJournalError` so callers can catch either the journal
    or the ledger contract failures uniformly. The message is intentionally the
    same whether an effect is unknown or simply not owned by the caller, so the
    ledger never leaks the existence of another principal's effect.
    """


class EffectStateError(EffectLedgerError):
    """Raised when an illegal state transition is attempted (contract 8)."""


@dataclass(frozen=True)
class EffectRecord:
    """One row of the ``effects`` ledger (contract 5)."""

    effect_id: str
    tenant: str
    user: str
    run_id: str
    effect_type: str
    target_scope_digest: str
    state: EffectState
    provider_idempotency_key: Optional[str]
    attempts: int
    first_seen_seq: int
    last_update_seq: int
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def principal(self) -> Principal:
        return Principal(self.tenant, self.user)

    @property
    def is_terminal(self) -> bool:
        return self.state in EFFECT_TERMINAL_STATES

    @property
    def executable(self) -> bool:
        """True only if the caller may still perform the underlying side effect."""
        return self.state in EXECUTABLE_STATES


# --------------------------------------------------------------------------- #
# Identity derivation                                                          #
# --------------------------------------------------------------------------- #
def _normalize_path(raw: str) -> str:
    """Normalize a filesystem target so equivalent paths map to one identity.

    ``os.path.normpath`` collapses ``.``/``..``/duplicate separators and trailing
    slashes; ``os.path.normcase`` folds case + separators on Windows (a no-op on
    POSIX). We intentionally do NOT ``abspath`` — that would fold in the current
    working directory and make the same logical target hash differently between
    two processes with different CWDs, breaking determinism. Callers performing
    filesystem effects should pass an absolute path.
    """
    return os.path.normcase(os.path.normpath(raw))


def _normalize_url(raw: str) -> str:
    """Normalize a network target: lowercase scheme/host, drop default port,
    fragment and userinfo, and sort query params so retry stability is stable.

    Userinfo is stripped (it may carry credentials) and the query is preserved
    but sorted (two effects to the same endpoint with different meaningful query
    params are genuinely different effects). Only the normalized *digest* is
    persisted, so no raw query ever lands in storage.
    """
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port is not None:
        default = {"http": 80, "https": 443, "ws": 80, "wss": 443}.get(scheme)
        if parts.port != default:
            netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    # Fragment dropped (never affects the server-side effect).
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_target_scope(logical_action: str, target_scope: Any) -> str:
    """Return a canonical, deterministic string for a target scope.

    Dispatches on the ``logical_action`` family (the verb prefix before the
    first dot) so a path, URL, recipient or board+column each normalize the way
    that makes "the same effect" collapse to one identity:
      * ``fs.*``      -> normalized path
      * ``net.*``     -> normalized URL
      * ``message.*`` -> lowercased recipient token
      * anything else -> canonical JSON (dict/list) or stripped string.
    """
    if isinstance(target_scope, (dict, list)):
        return json.dumps(
            target_scope, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, default=repr,
        )
    text = str(target_scope).strip()
    family = str(logical_action).split(".", 1)[0].lower() if logical_action else ""
    if family == "fs":
        return _normalize_path(text)
    if family == "net":
        return _normalize_url(text)
    if family == "message":
        return text.lower()
    return text


def compute_effect_id(
    run_id: str,
    principal: Principal,
    logical_action: str,
    target_scope: Any,
) -> str:
    """Derive the stable ``effect_id`` for one logical effect.

    ``effect_id = sha256(canonical(run_id, tenant, user, logical_action,
    normalized_target_scope))[:32]``. Because the principal is folded into the
    hash, two different principals performing "the same" fs.write to the same
    path on the same run get **different** effect ids — a principal can never
    address, and therefore never replay, another principal's effect.
    """
    if not isinstance(principal, Principal):
        raise EffectLedgerError("principal must be a Principal instance")
    if not isinstance(run_id, str) or not run_id.strip():
        raise EffectLedgerError("run_id must be a non-empty string")
    if not isinstance(logical_action, str) or not logical_action.strip():
        raise EffectLedgerError("logical_action must be a non-empty string")
    normalized = normalize_target_scope(logical_action, target_scope)
    canonical = json.dumps(
        [run_id, principal.tenant, principal.user, logical_action, normalized],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_EFFECT_ID_LEN]


def _target_scope_digest(logical_action: str, target_scope: Any) -> str:
    normalized = normalize_target_scope(logical_action, target_scope)
    return hashlib.sha256(
        ("scope:" + normalized).encode("utf-8")
    ).hexdigest()[:_EFFECT_ID_LEN]


def derive_provider_idempotency_key(
    effect_id: str, provider: Optional[str]
) -> Optional[str]:
    """Deterministically derive a provider idempotency key from ``effect_id``.

    Only returns a key for providers that actually accept one (billing / modal /
    browser); returns ``None`` otherwise (e.g. model inference), because we must
    not imply exactly-once where the external system cannot enforce it. The key
    is a pure function of ``effect_id`` + provider, so a retry sends the *same*
    key and the provider deduplicates server-side.
    """
    if not provider:
        return None
    name = str(provider).strip().lower()
    if name not in _PROVIDER_IDEMPOTENCY_SUPPORTED:
        return None
    return hashlib.sha256(
        f"{name}:{effect_id}".encode("utf-8")
    ).hexdigest()[:_EFFECT_ID_LEN]


# --------------------------------------------------------------------------- #
# Schema + row mapping                                                         #
# --------------------------------------------------------------------------- #
def _ensure_effects_table(conn) -> None:
    """Create the ``effects`` table if absent (idempotent, coexists with the
    run_journal schema on the same DB file)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS effects (
             effect_id              TEXT PRIMARY KEY,
             tenant                 TEXT NOT NULL,
             user                   TEXT NOT NULL,
             run_id                 TEXT NOT NULL,
             effect_type            TEXT NOT NULL,
             target_scope_digest    TEXT NOT NULL,
             state                  TEXT NOT NULL,
             provider_idempotency_key TEXT,
             attempts               INTEGER NOT NULL DEFAULT 0,
             first_seen_seq         INTEGER NOT NULL,
             last_update_seq        INTEGER NOT NULL,
             detail_json            TEXT NOT NULL
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_effects_principal_run "
        "ON effects(tenant, user, run_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_effects_state "
        "ON effects(state)"
    )


def _dump_detail(detail: Optional[Dict[str, Any]]) -> str:
    """Serialise an effect's ``detail`` for the ``effects.detail_json`` column,
    redacted (numeric-preserving) so the ledger table never stores a raw secret.

    Fixes the WAVE-26 asymmetry where the mirror journal event was redacted but
    the ledger row's own ``detail_json`` was written raw. The ``_owner`` fencing
    fields (process_id / pid / process_started_at) survive redaction intact
    (they are short non-secret strings / integers), so ``recover_interrupted``
    can still read them back.
    """
    return json.dumps(
        redaction.redact_journal_payload(detail or {}),
        ensure_ascii=False, default=repr,
    )


def _row_to_record(row) -> EffectRecord:
    return EffectRecord(
        effect_id=row["effect_id"],
        tenant=row["tenant"],
        user=row["user"],
        run_id=row["run_id"],
        effect_type=row["effect_type"],
        target_scope_digest=row["target_scope_digest"],
        state=EffectState(row["state"]),
        provider_idempotency_key=row["provider_idempotency_key"],
        attempts=int(row["attempts"]),
        first_seen_seq=int(row["first_seen_seq"]),
        last_update_seq=int(row["last_update_seq"]),
        detail=json.loads(row["detail_json"]),
    )


def _select_owned(conn, effect_id: str, principal: Principal):
    """Return the row for ``effect_id`` *only* if owned by ``principal``.

    Cross-principal guard: the WHERE clause pins ``(tenant, user)`` so another
    principal's effect is simply invisible (no existence leak).
    """
    return conn.execute(
        "SELECT * FROM effects WHERE effect_id=? AND tenant=? AND user=?",
        (effect_id, principal.tenant, principal.user),
    ).fetchone()


# --------------------------------------------------------------------------- #
# Atomic journal-event emission (same transaction as the ledger write)        #
# --------------------------------------------------------------------------- #
def _append_effect_event(
    conn,
    run_id: str,
    principal: Principal,
    effect_id: str,
    effect_type: str,
    target_scope_digest: str,
    state: EffectState,
    provider_idempotency_key: Optional[str],
    detail: Dict[str, Any],
    correlation_id: Optional[str],
) -> int:
    """Insert a ``category='effect'`` journal row *inside the caller's already
    open BEGIN IMMEDIATE transaction* and return its per-run ``seq``.

    This mirrors ``run_journal.append_event`` deliberately rather than calling
    it: ``append_event`` opens its own transaction, and nesting a second
    BEGIN IMMEDIATE on the same DB file would deadlock. Emitting the event here
    keeps the ledger row and its audit event atomic — a crash can never leave a
    committed effect without its committed event, or vice versa. Dedupe key is
    ``f"{effect_id}:{state}"`` so a re-asserted transition is a no-op append.
    """
    dedupe_key = f"{effect_id}:{state.value}"
    existing = conn.execute(
        "SELECT seq, tenant, user FROM run_events "
        "WHERE run_id=? AND category='effect' AND dedupe_key=?",
        (run_id, dedupe_key),
    ).fetchone()
    if existing is not None:
        if existing["tenant"] != principal.tenant or existing["user"] != principal.user:
            raise EffectLedgerError("effect event belongs to a different principal")
        return int(existing["seq"])

    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM run_events WHERE run_id=?",
        (run_id,),
    ).fetchone()
    seq = int(seq_row["next"])
    payload = {
        "effect_id": effect_id,
        "effect_type": effect_type,
        "target_scope_digest": target_scope_digest,
        "state": state.value,
        "provider_idempotency_key": provider_idempotency_key,
        "detail": detail or {},
    }
    # This row is inserted DIRECTLY into run_events (it shares the caller's open
    # BEGIN IMMEDIATE txn) and so bypasses append_event's redaction chokepoint —
    # redact the whole payload here so the invariant "no run_events row holds a
    # raw secret" still holds. Numeric-preserving so counts survive.
    payload_json = json.dumps(
        redaction.redact_journal_payload(payload), sort_keys=True,
        ensure_ascii=False, default=repr,
    )
    conn.execute(
        """INSERT INTO run_events
             (run_id, seq, event_id, schema_version, ts_unix_ns, tenant,
              user, correlation_id, category, kind, dedupe_key, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'effect', ?, ?, ?)""",
        (run_id, seq, f"{run_id}:{seq}", EVENT_SCHEMA_VERSION, time.time_ns(),
         principal.tenant, principal.user, correlation_id, state.value,
         dedupe_key, payload_json),
    )
    return seq


# --------------------------------------------------------------------------- #
# Ownership fencing (generalized from cron/executions.py)                      #
# --------------------------------------------------------------------------- #
def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time

        return get_process_start_time(pid)
    except Exception:
        return None


def _capture_owner() -> Dict[str, Any]:
    """Fingerprint the current process as the owner of an effect it begins."""
    pid = os.getpid()
    return {
        "process_id": _PROCESS_ID,
        "pid": pid,
        "process_started_at": _process_start_time(pid),
    }


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    """Fail-safe liveness of an effect's owner (mirrors cron ``_owner_is_live``).

    Inability to *prove* the owner is dead must never rewrite durable state, so
    every uncertain branch returns ``True`` (treat as still-live -> leave alone).
    """
    try:
        from gateway.status import _pid_exists

        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # cannot prove death -> do not touch the effect
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    return current is not None and current == started_at


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def begin_effect(
    run_id: str,
    principal: Principal,
    logical_action: str,
    target_scope: Any,
    *,
    provider: Optional[str] = None,
    authorize: bool = True,
    correlation_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
) -> EffectRecord:
    """Register (or look up) an effect and return its current record.

    Idempotent by construction: the ``effect_id`` is derived from
    ``(run_id, principal, logical_action, target_scope)``, so a retry/restart
    that calls ``begin_effect`` with the same inputs returns the **existing**
    row in whatever state it reached (e.g. ``committed`` or ``unknown``) rather
    than creating a duplicate. Callers gate the side effect on
    :func:`should_execute` / :attr:`EffectRecord.executable`.

    A fresh effect starts ``authorized`` (it is derived from the already
    authorized run + logical action); pass ``authorize=False`` for the
    ``planned`` -> later ``authorized`` two-step.
    """
    if not isinstance(principal, Principal):
        raise EffectLedgerError("principal must be a Principal instance")
    effect_id = compute_effect_id(run_id, principal, logical_action, target_scope)
    scope_digest = _target_scope_digest(logical_action, target_scope)
    provider_key = derive_provider_idempotency_key(effect_id, provider)
    initial = EffectState.AUTHORIZED if authorize else EffectState.PLANNED
    seed_detail: Dict[str, Any] = dict(detail or {})
    seed_detail["_owner"] = _capture_owner()
    if provider:
        seed_detail.setdefault("provider", str(provider).strip().lower())

    path = db_path or default_db_path()
    with _immediate_txn(path) as conn:
        _ensure_effects_table(conn)
        existing = _select_owned(conn, effect_id, principal)
        if existing is not None:
            return _row_to_record(existing)

        # Defensive: an effect_id present under a *different* principal can only
        # arise from a sha256 collision (principal is folded into the hash).
        collision = conn.execute(
            "SELECT 1 FROM effects WHERE effect_id=?", (effect_id,)
        ).fetchone()
        if collision is not None:
            raise EffectLedgerError("effect not found or not owned")

        seq = _append_effect_event(
            conn, run_id, principal, effect_id, logical_action, scope_digest,
            initial, provider_key, seed_detail, correlation_id,
        )
        conn.execute(
            """INSERT INTO effects
                 (effect_id, tenant, user, run_id, effect_type,
                  target_scope_digest, state, provider_idempotency_key,
                  attempts, first_seen_seq, last_update_seq, detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (effect_id, principal.tenant, principal.user, run_id, logical_action,
             scope_digest, initial.value, provider_key, seq, seq,
             _dump_detail(seed_detail)),
        )
        row = conn.execute(
            "SELECT * FROM effects WHERE effect_id=?", (effect_id,)
        ).fetchone()
    return _row_to_record(row)


def _transition(
    effect_id: str,
    principal: Principal,
    dst: EffectState,
    *,
    detail: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    bump_attempts: bool = False,
    db_path: Optional[Path] = None,
) -> EffectRecord:
    """Move an owned effect to ``dst`` atomically with its journal event.

    * Re-asserting the current state is a no-op that returns the record — this
      is how ``mark_committed`` on an already-committed effect returns
      ``committed`` (the idempotency guarantee), without re-appending events.
    * A terminal state is immutable: any *other* transition out of it raises.
    * Illegal transitions raise :class:`EffectStateError`.
    """
    if not isinstance(principal, Principal):
        raise EffectLedgerError("principal must be a Principal instance")
    path = db_path or default_db_path()
    with _immediate_txn(path) as conn:
        _ensure_effects_table(conn)
        row = _select_owned(conn, effect_id, principal)
        if row is None:
            # Leak-safe: same message whether unknown id or not owned.
            raise EffectLedgerError("effect not found or not owned")
        current = EffectState(row["state"])
        if current == dst:
            return _row_to_record(row)  # idempotent re-assert (e.g. commit-once)
        if not effect_transition_allowed(current, dst):
            raise EffectStateError(
                f"illegal effect transition {current.value} -> {dst.value}"
            )

        merged = json.loads(row["detail_json"])
        if detail:
            merged.update(detail)
        attempts = int(row["attempts"]) + (1 if bump_attempts else 0)
        seq = _append_effect_event(
            conn, row["run_id"], principal, effect_id, row["effect_type"],
            row["target_scope_digest"], dst, row["provider_idempotency_key"],
            merged, correlation_id,
        )
        conn.execute(
            "UPDATE effects SET state=?, attempts=?, last_update_seq=?, "
            "detail_json=? WHERE effect_id=? AND tenant=? AND user=?",
            (dst.value, attempts, seq, _dump_detail(merged),
             effect_id, principal.tenant, principal.user),
        )
        updated = conn.execute(
            "SELECT * FROM effects WHERE effect_id=?", (effect_id,)
        ).fetchone()
    return _row_to_record(updated)


def mark_authorized(effect_id, principal, **kw) -> EffectRecord:
    """planned -> authorized."""
    return _transition(effect_id, principal, EffectState.AUTHORIZED, **kw)


def mark_in_progress(effect_id, principal, **kw) -> EffectRecord:
    """authorized -> in_progress (bumps the attempt counter)."""
    kw.setdefault("bump_attempts", True)
    return _transition(effect_id, principal, EffectState.IN_PROGRESS, **kw)


def mark_committed(effect_id, principal, **kw) -> EffectRecord:
    """in_progress -> committed. At most once: a second call on an already
    committed effect is a no-op that returns ``committed`` — THE idempotency
    guarantee that prevents a crash-then-requeue from double-applying."""
    return _transition(effect_id, principal, EffectState.COMMITTED, **kw)


def mark_failed(effect_id, principal, **kw) -> EffectRecord:
    """-> failed (terminal). The side effect provably did not take hold."""
    return _transition(effect_id, principal, EffectState.FAILED, **kw)


def mark_cancelled(effect_id, principal, **kw) -> EffectRecord:
    """-> cancelled (terminal)."""
    return _transition(effect_id, principal, EffectState.CANCELLED, **kw)


def mark_unknown(effect_id, principal, **kw) -> EffectRecord:
    """-> unknown (non-terminal-safe). Outcome unproven; never blind-retry."""
    return _transition(effect_id, principal, EffectState.UNKNOWN, **kw)


def mark_reconciliation_required(effect_id, principal, **kw) -> EffectRecord:
    """-> reconciliation_required (non-terminal-safe)."""
    return _transition(
        effect_id, principal, EffectState.RECONCILIATION_REQUIRED, **kw
    )


def should_execute(record: EffectRecord) -> bool:
    """Return True only if the caller may still perform the side effect.

    Advisory read for a *point-in-time* check. It is NOT a concurrency-safe
    claim: two callers can both observe an executable state. To gate a real
    side effect under concurrency/retry/restart use :func:`try_claim`, which
    atomically wins the effect for exactly one caller. An ``unknown`` /
    ``in_progress`` / terminal effect returns ``False``: a crash-stranded effect
    must be reconciled, never blindly re-executed.
    """
    return record.state in EXECUTABLE_STATES


def try_claim(
    effect_id: str,
    principal: Principal,
    *,
    correlation_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
) -> tuple[bool, EffectRecord]:
    """Atomically claim an effect for execution; return ``(won, record)``.

    Exactly ONE caller wins: the one that finds the effect in an executable
    state (``planned``/``authorized``) and flips it to ``in_progress`` inside the
    single-writer ``BEGIN IMMEDIATE`` transaction. Every concurrent caller, and
    any retry/restart after the claim, gets ``won=False`` with the current
    record and MUST NOT perform the side effect (it is in progress elsewhere,
    already committed, failed, or crash-stranded → ``unknown``).

    This closes the check-then-act race that ``should_execute`` +
    ``mark_in_progress`` leaves open (``mark_in_progress`` on an already
    ``in_progress`` effect is an idempotent no-op that would let a second caller
    proceed). Callers that win perform the effect then call
    :func:`mark_committed`; on failure they call :func:`mark_unknown`.
    """
    if not isinstance(principal, Principal):
        raise EffectLedgerError("principal must be a Principal instance")
    path = db_path or default_db_path()
    with _immediate_txn(path) as conn:
        _ensure_effects_table(conn)
        row = _select_owned(conn, effect_id, principal)
        if row is None:
            raise EffectLedgerError("effect not found or not owned")
        current = EffectState(row["state"])
        if current not in EXECUTABLE_STATES:
            # Another caller already claimed/settled it (BEGIN IMMEDIATE means
            # we observe a committed prior transition, never a half-written one).
            return (False, _row_to_record(row))
        merged = json.loads(row["detail_json"])
        if detail:
            merged.update(detail)
        attempts = int(row["attempts"]) + 1
        seq = _append_effect_event(
            conn, row["run_id"], principal, effect_id, row["effect_type"],
            row["target_scope_digest"], EffectState.IN_PROGRESS,
            row["provider_idempotency_key"], merged, correlation_id,
        )
        conn.execute(
            "UPDATE effects SET state=?, attempts=?, last_update_seq=?, "
            "detail_json=? WHERE effect_id=? AND tenant=? AND user=?",
            (EffectState.IN_PROGRESS.value, attempts, seq, _dump_detail(merged),
             effect_id, principal.tenant, principal.user),
        )
        updated = conn.execute(
            "SELECT * FROM effects WHERE effect_id=?", (effect_id,)
        ).fetchone()
    return (True, _row_to_record(updated))


def get_effect(
    effect_id: str, principal: Principal, *, db_path: Optional[Path] = None
) -> Optional[EffectRecord]:
    """Return the effect if owned by ``principal``, else ``None`` (no leak)."""
    if not isinstance(principal, Principal):
        raise EffectLedgerError("principal must be a Principal instance")
    path = db_path or default_db_path()
    with _read_conn(path) as conn:
        _ensure_effects_table(conn)
        row = _select_owned(conn, effect_id, principal)
    return _row_to_record(row) if row is not None else None


def list_effects(
    run_id: str,
    principal: Principal,
    *,
    state: Optional[EffectState] = None,
    limit: int = 500,
    db_path: Optional[Path] = None,
) -> List[EffectRecord]:
    """List a run's effects scoped to the owning principal (no cross-principal
    visibility)."""
    if not isinstance(principal, Principal):
        raise EffectLedgerError("principal must be a Principal instance")
    path = db_path or default_db_path()
    clauses = ["run_id=?", "tenant=?", "user=?"]
    params: List[Any] = [run_id, principal.tenant, principal.user]
    if state is not None:
        clauses.append("state=?")
        params.append(EffectState(state).value)
    params.append(max(1, min(int(limit), 5000)))
    with _read_conn(path) as conn:
        _ensure_effects_table(conn)
        rows = conn.execute(
            "SELECT * FROM effects WHERE " + " AND ".join(clauses)
            + " ORDER BY first_seen_seq ASC, effect_id ASC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def get_effect_in_workspace(
    effect_id: str,
    principal: Principal,
    workspace_id: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[EffectRecord]:
    """Canonical workspace-scoped receipt lookup for managed callers (Owner item 6).

    Returns the effect only if it is owned by ``principal`` AND bound to
    ``workspace_id`` (the workspace stored in the effect detail at creation). A
    lookup from another workspace — even by the same tenant + user — returns
    ``None`` (not-found), never the record or its metadata: the workspace boundary
    fails closed. Managed APIs (effect/receipt reads, settle, reconcile) must use
    this rather than the principal-only :func:`get_effect`, so a workspace-A
    receipt is invisible to a workspace-B caller.
    """
    record = get_effect(effect_id, principal, db_path=db_path)
    if record is None:
        return None
    if record.detail.get("workspace") != workspace_id:
        return None
    return record


def list_effects_in_workspace(
    run_id: str,
    principal: Principal,
    workspace_id: str,
    *,
    state: Optional[EffectState] = None,
    limit: int = 500,
    db_path: Optional[Path] = None,
) -> List[EffectRecord]:
    """List a run's effects scoped to the owning principal AND the bound workspace.

    Cross-workspace effects of the same principal are filtered out (workspace-scoped
    listing, Owner item 6) so a managed listing endpoint cannot enumerate another
    workspace's receipts."""
    return [
        r
        for r in list_effects(
            run_id, principal, state=state, limit=limit, db_path=db_path
        )
        if r.detail.get("workspace") == workspace_id
    ]


def recover_interrupted(
    *,
    run_id: Optional[str] = None,
    principal: Optional[Principal] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Mark provably-abandoned effects ``unknown``; never schedule a retry.

    Generalizes ``cron/executions.recover_interrupted_executions``: scan effects
    stranded in a non-terminal state (planned / authorized / in_progress), and
    for each whose recorded owner process is *provably* gone (fail-safe
    ``(pid, start_time)`` check), transition it to ``unknown`` with the detail
    "whether the side effect ran is unknown". Effects owned by the live current
    process, or whose death cannot be proven, are left untouched. Returns the
    number of effects transitioned.

    Optional ``run_id`` / ``principal`` narrow the sweep; each recovered effect's
    ``unknown`` event is emitted under that effect's own stored principal.
    """
    path = db_path or default_db_path()
    non_terminal = (
        EffectState.PLANNED.value,
        EffectState.AUTHORIZED.value,
        EffectState.IN_PROGRESS.value,
    )
    changed = 0
    with _immediate_txn(path) as conn:
        _ensure_effects_table(conn)
        clauses = ["state IN ({})".format(",".join("?" * len(non_terminal)))]
        params: List[Any] = list(non_terminal)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if principal is not None:
            clauses.append("tenant=? AND user=?")
            params.extend([principal.tenant, principal.user])
        rows = conn.execute(
            "SELECT * FROM effects WHERE " + " AND ".join(clauses), params
        ).fetchall()

        for row in rows:
            detail = json.loads(row["detail_json"])
            owner = detail.get("_owner") or {}
            # Effects owned by *this* live process are still being driven.
            if owner.get("process_id") == _PROCESS_ID:
                continue
            pid = owner.get("pid")
            if pid is not None and _owner_is_live(int(pid), owner.get("process_started_at")):
                continue
            # Owner provably gone (or no fencing info): outcome is unknown.
            current = EffectState(row["state"])
            if not effect_transition_allowed(current, EffectState.UNKNOWN):
                continue
            detail["recovery"] = _UNKNOWN_ON_RECOVER_DETAIL
            row_principal = Principal(row["tenant"], row["user"])
            seq = _append_effect_event(
                conn, row["run_id"], row_principal, row["effect_id"],
                row["effect_type"], row["target_scope_digest"],
                EffectState.UNKNOWN, row["provider_idempotency_key"],
                detail, None,
            )
            conn.execute(
                "UPDATE effects SET state=?, last_update_seq=?, detail_json=? "
                "WHERE effect_id=?",
                (EffectState.UNKNOWN.value, seq, _dump_detail(detail),
                 row["effect_id"]),
            )
            changed += 1
    return changed
