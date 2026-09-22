"""Canonical durable Run/Event store for Youtab Agent Runtime durable execution.

This is the SINGLE source of truth for task/run *execution* state: identity,
state machine, append-only monotonic events, checkpoints/result references,
attempts, progress, leases, cancellation and terminal results.

Ownership boundaries (do NOT duplicate here):
- external side-effect idempotency / UNKNOWN-after-effect / immutable receipts
  live in the Lane-1 effect ledger — consumed through the typed ``EffectLedger``
  Protocol below, never reimplemented.
- semantic/episodic memory lives in Memory/Simorgh.
- signed identity/scope/authorization lives in the Gateway.
- the UI is a consumer only.

Core invariant: a caller's synchronous wait, an HTTP request, a UI/SSE
connection, a parent agent or a model/tool call timing out or disconnecting must
NEVER silently cancel or destroy a durable task. ``wait_timeout`` ends only the
caller's wait; the durable run continues under its own lease. Only explicit
authorized cancellation, a persisted ``execution_deadline``, a safety violation
or an unrecoverable terminal failure may stop a run.

Local desktop uses SQLite (per-run monotonic sequence, foreign keys, busy
timeout, crash-safe transactional state+event writes). The public interface is
kept compatible with a future PostgreSQL implementation; SQLite is local
correctness evidence only, not multi-host scale evidence.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, cast, runtime_checkable

from youtab_constants import get_youtab_home

SCHEMA_VERSION = 2
_BUSY_TIMEOUT_MS = 5000


class RunState(str, Enum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_CHILD = "WAITING_CHILD"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    STALLED = "STALLED"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


TERMINAL_STATES = frozenset(
    {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
)

# Validated transition table. Terminal states are immutable EXCEPT a permitted
# reconciliation escape from UNKNOWN/RECONCILIATION_REQUIRED. A generic transport
# timeout never appears here — it is not a transition at all.
_TRANSITIONS: Dict[RunState, frozenset] = {
    RunState.QUEUED: frozenset({RunState.CLAIMED, RunState.CANCELLING, RunState.CANCELLED, RunState.UNKNOWN}),
    RunState.CLAIMED: frozenset({RunState.RUNNING, RunState.STALLED, RunState.CANCELLING, RunState.FAILED, RunState.UNKNOWN}),
    RunState.RUNNING: frozenset({
        RunState.WAITING_CHILD, RunState.WAITING_APPROVAL, RunState.PAUSING,
        RunState.STALLED, RunState.CANCELLING, RunState.SUCCEEDED, RunState.FAILED,
        RunState.UNKNOWN, RunState.RECONCILIATION_REQUIRED,
    }),
    RunState.WAITING_CHILD: frozenset({RunState.RUNNING, RunState.STALLED, RunState.CANCELLING, RunState.FAILED, RunState.UNKNOWN}),
    RunState.WAITING_APPROVAL: frozenset({RunState.RUNNING, RunState.CANCELLING, RunState.FAILED, RunState.CANCELLED, RunState.UNKNOWN}),
    RunState.PAUSING: frozenset({RunState.PAUSED, RunState.RUNNING, RunState.CANCELLING, RunState.UNKNOWN}),
    RunState.PAUSED: frozenset({RunState.RUNNING, RunState.CANCELLING, RunState.CANCELLED, RunState.UNKNOWN}),
    RunState.STALLED: frozenset({RunState.RUNNING, RunState.CANCELLING, RunState.FAILED, RunState.PAUSED, RunState.UNKNOWN, RunState.RECONCILIATION_REQUIRED}),
    RunState.CANCELLING: frozenset({RunState.CANCELLED, RunState.FAILED, RunState.UNKNOWN}),
    # Terminal:
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
    # Ambiguity states may be reconciled to a real terminal or back to running.
    RunState.UNKNOWN: frozenset({RunState.RECONCILIATION_REQUIRED, RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED, RunState.RUNNING}),
    RunState.RECONCILIATION_REQUIRED: frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED, RunState.RUNNING, RunState.UNKNOWN}),
}


class DurableRunError(RuntimeError):
    pass


class InvalidTransition(DurableRunError):
    pass


class IdempotencyConflict(DurableRunError):
    """Same idempotency key, different request digest — fails closed."""


class LeaseError(DurableRunError):
    pass


# --------------------------------------------------------------------------- #
# Lane-1 effect-ledger boundary (typed Protocol; NEVER reimplemented here)
# --------------------------------------------------------------------------- #
@runtime_checkable
class EffectLedger(Protocol):
    """Typed boundary to the canonical Lane-1 effect ledger (IR-1).

    Durable Execution consults this BEFORE retrying any external effect:
    committed -> return receipt; unknown -> reconciliation; not-executed -> safe.
    """

    def begin_effect(self, effect_id: str, principal: str, target_scope_digest: str) -> Dict[str, Any]: ...

    def commit_effect(self, effect_id: str, receipt: Dict[str, Any]) -> Dict[str, Any]: ...

    def lookup(self, effect_id: str) -> Dict[str, Any]:
        """Return {'status': 'absent'|'in_flight'|'committed'|'unknown', 'receipt': ...}."""
        ...


class AbsentEffectLedger:
    """Fail-closed default used until the Lane-1 ledger is integrated on the base.

    It never fabricates a committed/absent verdict: every lookup is UNKNOWN, so
    callers must route to reconciliation rather than blind-retry. This preserves
    the safety invariant while the real boundary is wired by Master Integrator.
    """

    def begin_effect(self, effect_id, principal, target_scope_digest):
        raise DurableRunError(
            "canonical Lane-1 effect ledger is not integrated on this base; "
            "external effects must be gated by the real ledger (IR-1)"
        )

    def commit_effect(self, effect_id, receipt):
        raise DurableRunError("Lane-1 effect ledger not integrated (IR-1)")

    def lookup(self, effect_id):
        return {"status": "unknown", "receipt": None,
                "detail": "Lane-1 effect ledger absent on base; reconcile, do not blind-retry"}


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RunIdentity:
    task_id: str
    run_id: str
    tenant_id: str
    organization_id: str
    workspace_id: str
    principal_id: str
    agent_id: str
    operation: str = "run"
    parent_task_id: Optional[str] = None
    delegation_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_digest: Optional[str] = None
    execution_deadline: Optional[float] = None


@dataclass(frozen=True)
class WaitResult:
    """Typed non-terminal response returned when a caller's wait elapses.

    Never a bare ``summary=None`` — always carries durable identity + recovery.
    """
    task_id: str
    run_id: str
    state: str
    terminal: bool
    last_progress_seq: int
    checkpoint_ref: Optional[str]
    result_ref: Optional[str]
    reconnect: str


# --------------------------------------------------------------------------- #
# One typed interface; backend-specific implementations (no two authorities)
# --------------------------------------------------------------------------- #
@runtime_checkable
class RunStore(Protocol):
    """The single typed run-store contract. SQLite (desktop/offline) and a future
    PostgreSQL (server/enterprise) implementation share identical state and
    idempotency semantics behind this interface. There is never synchronization
    between two authoritative stores — a deployment selects exactly one backend."""

    def create_run(self, identity: RunIdentity, *, initial_state: RunState = ...) -> Dict[str, Any]: ...
    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]: ...
    def transition(self, run_id: str, to_state: RunState, **kw: Any) -> Dict[str, Any]: ...
    def record_progress(self, run_id: str, **kw: Any) -> int: ...
    def append_event(self, run_id: str, kind: str, payload: Optional[dict] = ...) -> int: ...
    def get_events(self, run_id: str, *, from_seq: int = ..., limit: int = ...) -> List[Dict[str, Any]]: ...
    def claim(self, run_id: str, owner: str, *, ttl_seconds: float = ...) -> Optional[int]: ...
    def heartbeat(self, run_id: str, owner: str, epoch: int, *, ttl_seconds: float = ...) -> bool: ...
    def detect_stalled(self, *, stall_threshold: float, liveness_window: float = ..., now: Optional[float] = ...) -> List[str]: ...
    def request_cancel(self, run_id: str, *, reason: str, by: str) -> Dict[str, Any]: ...
    def wait_for_terminal(self, run_id: str, *, wait_timeout: float, poll: float = ...) -> WaitResult: ...


class SqliteRunStore:
    """SQLite implementation of :class:`RunStore` — local/offline Desktop backend.
    Local correctness evidence only (fenced claim under DELETE mode), NOT
    multi-host scale evidence; server/enterprise uses the PostgreSQL backend."""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = Path(db_path) if db_path else (get_youtab_home() / "durable_runs.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- connection --------------------------------------------------------- #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            from youtab_state import apply_wal_with_fallback
            apply_wal_with_fallback(conn, db_label="durable_runs.db")
        except Exception:
            pass
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    operation TEXT NOT NULL DEFAULT 'run',
                    parent_task_id TEXT,
                    delegation_id TEXT,
                    idempotency_key TEXT,
                    request_digest TEXT,
                    state TEXT NOT NULL,
                    progress_seq INTEGER NOT NULL DEFAULT 0,
                    last_progress_at REAL,
                    checkpoint_ref TEXT,
                    result_ref TEXT,
                    error_ref TEXT,
                    execution_deadline REAL,
                    lease_owner TEXT,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expiry REAL,
                    heartbeat_at REAL,
                    cancel_requested_at REAL,
                    cancel_reason TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            # --- migrations (idempotent, crash-safe inside this transaction) --- #
            # v1 -> v2: a pre-existing runs table may lack the `operation` column
            # and may carry the OLD global unique index on idempotency_key alone.
            # Preserve existing rows/events/results; add the column; replace the
            # index with the scoped one. CREATE ... IF NOT EXISTS never REPLACES a
            # same-named index, so the old global index must be dropped explicitly.
            existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            if "operation" not in existing_cols:
                conn.execute("ALTER TABLE runs ADD COLUMN operation TEXT NOT NULL DEFAULT 'run'")
            old_idx = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_runs_idempotency'"
            ).fetchone()
            if old_idx and old_idx[0] and "tenant_id" not in old_idx[0]:
                conn.execute("DROP INDEX ux_runs_idempotency")

            # Idempotency is SCOPED, never global: the same key in a different
            # tenant/workspace/principal/operation is an independent task, and a
            # scoped lookup cannot leak a foreign tenant's task existence.
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_idempotency
                   ON runs(tenant_id, workspace_id, principal_id, operation, idempotency_key)
                   WHERE idempotency_key IS NOT NULL"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    payload TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    UNIQUE(run_id, seq)
                )"""
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _row_to_dict(cur, row) -> Dict[str, Any]:
        return {d[0]: row[i] for i, d in enumerate(cur.description)}

    def _next_seq(self, conn, run_id: str) -> int:
        r = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id=?", (run_id,)).fetchone()
        return int(r[0]) + 1

    def _append_event_locked(self, conn, run_id: str, kind: str, payload: Optional[dict]) -> int:
        seq = self._next_seq(conn, run_id)
        conn.execute(
            "INSERT INTO run_events(run_id, seq, event_id, kind, payload, created_at) VALUES(?,?,?,?,?,?)",
            (run_id, seq, uuid.uuid4().hex, kind, json.dumps(payload or {}), time.time()),
        )
        return seq

    # -- public API --------------------------------------------------------- #
    def create_run(self, identity: RunIdentity, *, initial_state: RunState = RunState.QUEUED) -> Dict[str, Any]:
        """Durably accept a run BEFORE work starts. Idempotent by idempotency_key
        + request_digest: same key+digest returns the existing run; same key with
        a different digest fails closed (IdempotencyConflict)."""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if identity.idempotency_key:
                # SCOPED lookup — cannot see another tenant/workspace/principal/
                # operation's run, so no cross-tenant existence leak.
                cur = conn.execute(
                    "SELECT * FROM runs WHERE tenant_id=? AND workspace_id=? AND principal_id=? "
                    "AND operation=? AND idempotency_key=?",
                    (identity.tenant_id, identity.workspace_id, identity.principal_id,
                     identity.operation, identity.idempotency_key),
                )
                existing = cur.fetchone()
                if existing is not None:
                    row = self._row_to_dict(cur, existing)
                    conn.execute("COMMIT")
                    if (identity.request_digest or None) != (row.get("request_digest") or None):
                        raise IdempotencyConflict(
                            f"scoped idempotency key {identity.idempotency_key!r} exists with a "
                            f"different request_digest"
                        )
                    return row
            conn.execute(
                """INSERT INTO runs(run_id, task_id, tenant_id, organization_id, workspace_id,
                        principal_id, agent_id, operation, parent_task_id, delegation_id,
                        idempotency_key, request_digest, state, progress_seq, execution_deadline,
                        lease_epoch, attempts, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,0,0,?,?)""",
                (identity.run_id, identity.task_id, identity.tenant_id, identity.organization_id,
                 identity.workspace_id, identity.principal_id, identity.agent_id, identity.operation,
                 identity.parent_task_id, identity.delegation_id, identity.idempotency_key,
                 identity.request_digest, initial_state.value, identity.execution_deadline, now, now),
            )
            self._append_event_locked(conn, identity.run_id, "accepted",
                                      {"state": initial_state.value})
            cur = conn.execute("SELECT * FROM runs WHERE run_id=?", (identity.run_id,))
            row = self._row_to_dict(cur, cur.fetchone())
            conn.execute("COMMIT")
            return row
        except Exception:
            # Control-flow raises (e.g. IdempotencyConflict) can occur AFTER a
            # COMMIT on the idempotent-return path; a ROLLBACK then has no active
            # transaction. Tolerate that so the real exception surfaces.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.close()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
            row = cur.fetchone()
            return self._row_to_dict(cur, row) if row else None
        finally:
            conn.close()

    def transition(self, run_id: str, to_state: RunState, *, kind: Optional[str] = None,
                   payload: Optional[dict] = None, result_ref: Optional[str] = None,
                   error_ref: Optional[str] = None) -> Dict[str, Any]:
        """Validated, durable, event-producing state transition."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("SELECT state FROM runs WHERE run_id=?", (run_id,))
            r = cur.fetchone()
            if r is None:
                raise DurableRunError(f"run not found: {run_id}")
            cur_state = RunState(r[0])
            if to_state != cur_state and to_state not in _TRANSITIONS.get(cur_state, frozenset()):
                raise InvalidTransition(f"{cur_state.value} -> {to_state.value} is not allowed")
            now = time.time()
            conn.execute(
                "UPDATE runs SET state=?, updated_at=?, "
                "result_ref=COALESCE(?, result_ref), error_ref=COALESCE(?, error_ref) WHERE run_id=?",
                (to_state.value, now, result_ref, error_ref, run_id),
            )
            self._append_event_locked(conn, run_id, kind or f"state.{to_state.value.lower()}",
                                      {**(payload or {}), "state": to_state.value})
            cur = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
            row = self._row_to_dict(cur, cur.fetchone())
            conn.execute("COMMIT")
            return row
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_progress(self, run_id: str, *, step: Optional[str] = None,
                        checkpoint_ref: Optional[str] = None, metric: Optional[dict] = None) -> int:
        """Advance durable progress (distinct from liveness heartbeat). Returns the
        new progress_seq. Requires a real progress signal — never token traffic."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            seq = self._append_event_locked(conn, run_id, "progress",
                                            {"step": step, "checkpoint_ref": checkpoint_ref, "metric": metric})
            conn.execute(
                "UPDATE runs SET progress_seq=?, last_progress_at=?, updated_at=?, "
                "checkpoint_ref=COALESCE(?, checkpoint_ref) WHERE run_id=?",
                (seq, now, now, checkpoint_ref, run_id),
            )
            conn.execute("COMMIT")
            return seq
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def append_event(self, run_id: str, kind: str, payload: Optional[dict] = None) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            seq = self._append_event_locked(conn, run_id, kind, payload)
            conn.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (time.time(), run_id))
            conn.execute("COMMIT")
            return seq
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_events(self, run_id: str, *, from_seq: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
        """Reconnect/replay: events with seq > from_seq, ascending, bounded."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT run_id, seq, event_id, kind, payload, created_at FROM run_events "
                "WHERE run_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
                (run_id, int(from_seq), int(limit)),
            )
            out = []
            for row in cur.fetchall():
                out.append({
                    "run_id": row[0], "seq": row[1], "event_id": row[2],
                    "kind": row[3], "payload": json.loads(row[4] or "{}"), "created_at": row[5],
                })
            return out
        finally:
            conn.close()

    # -- lease / liveness --------------------------------------------------- #
    def claim(self, run_id: str, owner: str, *, ttl_seconds: float = 900.0) -> Optional[int]:
        """CAS claim QUEUED->CLAIMED with a fencing epoch. Returns the new epoch
        on success, None if already claimed by someone else."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            cur = conn.execute("SELECT state, lease_epoch FROM runs WHERE run_id=?", (run_id,))
            r = cur.fetchone()
            if r is None or RunState(r[0]) != RunState.QUEUED:
                conn.execute("ROLLBACK")
                return None
            epoch = int(r[1]) + 1
            conn.execute(
                "UPDATE runs SET state=?, lease_owner=?, lease_epoch=?, lease_expiry=?, "
                "heartbeat_at=?, attempts=attempts+1, updated_at=? WHERE run_id=? AND state='QUEUED'",
                (RunState.CLAIMED.value, owner, epoch, now + ttl_seconds, now, now, run_id),
            )
            self._append_event_locked(conn, run_id, "claimed", {"owner": owner, "epoch": epoch})
            conn.execute("COMMIT")
            return epoch
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def heartbeat(self, run_id: str, owner: str, epoch: int, *, ttl_seconds: float = 900.0) -> bool:
        """Liveness ONLY. Fenced: a stale epoch cannot renew. Does not advance progress."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            cur = conn.execute(
                "UPDATE runs SET heartbeat_at=?, lease_expiry=?, updated_at=? "
                "WHERE run_id=? AND lease_owner=? AND lease_epoch=?",
                (now, now + ttl_seconds, now, run_id, owner, int(epoch)),
            )
            ok = cur.rowcount == 1
            conn.execute("COMMIT")
            return ok
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def detect_stalled(self, *, stall_threshold: float, liveness_window: float = 3600.0,
                       now: Optional[float] = None) -> List[str]:
        """A live-but-not-progressing worker becomes STALLED (visible, recoverable),
        never killed. Stall = heartbeat within ``liveness_window`` (still alive) AND
        no durable progress within ``stall_threshold``. Liveness and progress use
        SEPARATE windows on purpose — a healthy heartbeat is not progress."""
        now = now if now is not None else time.time()
        conn = self._connect()
        stalled: List[str] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "SELECT run_id, heartbeat_at, last_progress_at, created_at FROM runs "
                "WHERE state IN ('RUNNING','CLAIMED','WAITING_CHILD')"
            )
            rows = cur.fetchall()
            for run_id, hb, lp, created in rows:
                hb = hb or 0
                last_prog = lp if lp is not None else (created or 0)
                # Alive (heartbeat within the liveness window) but no durable
                # progress beyond the stall threshold — two independent windows.
                alive = (now - hb) <= liveness_window if hb else False
                no_progress = (now - last_prog) > stall_threshold
                if alive and no_progress:
                    conn.execute("UPDATE runs SET state='STALLED', updated_at=? WHERE run_id=?", (now, run_id))
                    self._append_event_locked(conn, run_id, "state.stalled",
                                              {"reason": "no_progress", "stall_threshold": stall_threshold})
                    stalled.append(run_id)
            conn.execute("COMMIT")
            return stalled
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- cancellation ------------------------------------------------------- #
    def request_cancel(self, run_id: str, *, reason: str, by: str) -> Dict[str, Any]:
        """Explicit authorized cancellation — the ONLY caller-driven stop path.
        Records intent + moves toward CANCELLING; does not itself kill a process."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("SELECT state FROM runs WHERE run_id=?", (run_id,))
            r = cur.fetchone()
            if r is None:
                raise DurableRunError(f"run not found: {run_id}")
            cur_state = RunState(r[0])
            now = time.time()
            conn.execute(
                "UPDATE runs SET cancel_requested_at=?, cancel_reason=?, updated_at=? WHERE run_id=?",
                (now, f"{by}:{reason}", now, run_id),
            )
            if RunState.CANCELLING in _TRANSITIONS.get(cur_state, frozenset()):
                conn.execute("UPDATE runs SET state=? WHERE run_id=?", (RunState.CANCELLING.value, run_id))
                self._append_event_locked(conn, run_id, "state.cancelling", {"by": by, "reason": reason})
            else:
                self._append_event_locked(conn, run_id, "cancel_requested",
                                          {"by": by, "reason": reason, "note": "terminal — no-op"})
            cur = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
            row = self._row_to_dict(cur, cur.fetchone())
            conn.execute("COMMIT")
            return row
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- caller wait (decoupled from lifetime) ------------------------------ #
    def wait_for_terminal(self, run_id: str, *, wait_timeout: float, poll: float = 0.05) -> WaitResult:
        """End the CALLER's synchronous wait after ``wait_timeout`` — WITHOUT ever
        cancelling or destroying the run. On timeout returns a typed non-terminal
        WaitResult with durable identity + reconnect action (never bare summary=None)."""
        deadline = time.time() + wait_timeout
        while True:
            row = self.get_run(run_id)
            if row is None:
                raise DurableRunError(f"run not found: {run_id}")
            state = RunState(row["state"])
            terminal = state in TERMINAL_STATES
            if terminal or time.time() >= deadline:
                return WaitResult(
                    task_id=row["task_id"], run_id=run_id, state=state.value,
                    terminal=terminal, last_progress_seq=int(row["progress_seq"] or 0),
                    checkpoint_ref=row["checkpoint_ref"], result_ref=row["result_ref"],
                    reconnect=f"/v1/runs/{run_id}/events?from_seq={int(row['progress_seq'] or 0)}",
                )
            time.sleep(min(poll, max(0.0, deadline - time.time())))


# Backward-compatible alias: the canonical name callers use.
DurableRunStore = SqliteRunStore


class PostgresRunStore:
    """Server/enterprise backend placeholder — same RunStore contract.

    Not implemented on this base: server deployment integration is a separate,
    later work item. It is declared here so the typed interface and factory make
    the backend choice explicit; SQLite must never be presented as multi-host
    distributed-execution evidence.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        raise NotImplementedError(
            "PostgresRunStore is not integrated on this base; use the SQLite "
            "backend for local/desktop, and wire the Postgres backend when the "
            "server persistence layer is integrated (same RunStore contract)."
        )


def create_run_store(backend: str = "sqlite", **kwargs: Any) -> RunStore:
    """Select the single canonical run-store backend for this deployment.

    ``sqlite`` -> local/offline Desktop; ``postgres`` -> server/enterprise.
    Exactly one authoritative store per deployment — never both.
    """
    backend = (backend or "sqlite").lower()
    if backend == "sqlite":
        return cast(RunStore, SqliteRunStore(**kwargs))
    if backend in ("postgres", "postgresql", "pg"):
        return cast(RunStore, PostgresRunStore(**kwargs))
    raise ValueError(f"unknown run-store backend: {backend!r}")
