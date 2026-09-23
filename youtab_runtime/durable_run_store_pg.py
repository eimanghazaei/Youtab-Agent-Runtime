"""PostgreSQL backend for the canonical RunStore (server/enterprise).

Same authority, interface and semantics as the SQLite backend — one run authority,
one task-id space, one (absent-on-base) Lane-1 effect boundary. NOT a second
ledger/receipt type. Real row-level locking (SELECT ... FOR UPDATE / atomic
UPDATE ... WHERE state=...) gives native concurrency; SQLite's DELETE-mode
serialization is not needed here.

Fail-closed: constructing this without psycopg or a DSN raises — server mode never
silently falls back to SQLite.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from youtab_runtime.durable_run_store import (
    _TRANSITIONS,
    TERMINAL_STATES,
    DurableRunError,
    IdempotencyConflict,
    InvalidTransition,
    RunIdentity,
    RunState,
    WaitResult,
)

SCHEMA_VERSION = 2


class PostgresRunStore:
    """RunStore backend on PostgreSQL. Implements the same contract as
    SqliteRunStore. Requires psycopg (v3) and a DSN."""

    def __init__(self, dsn: Optional[str] = None):
        try:
            import psycopg  # noqa: F401
        except Exception as exc:  # fail closed — no silent SQLite fallback
            raise DurableRunError(
                "PostgresRunStore requires psycopg (v3); server durability is "
                "unavailable without it (fail-closed, no SQLite fallback)"
            ) from exc
        if not dsn:
            raise DurableRunError(
                "PostgresRunStore requires a DSN (server durability fail-closed)"
            )
        self._dsn: str = str(dsn)
        self._psycopg = __import__("psycopg")
        self._init_schema()

    def _conn(self):
        # A short-lived connection per operation (autocommit off; explicit commit).
        return self._psycopg.connect(self._dsn, autocommit=False)

    # -- schema + migration ------------------------------------------------- #
    def _init_schema(self) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)")
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                        organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                        principal_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                        operation TEXT NOT NULL DEFAULT 'run', parent_task_id TEXT,
                        delegation_id TEXT, idempotency_key TEXT, request_digest TEXT,
                        state TEXT NOT NULL, progress_seq BIGINT NOT NULL DEFAULT 0,
                        last_progress_at DOUBLE PRECISION, checkpoint_ref TEXT, result_ref TEXT,
                        error_ref TEXT, execution_deadline DOUBLE PRECISION, lease_owner TEXT,
                        lease_epoch BIGINT NOT NULL DEFAULT 0, lease_expiry DOUBLE PRECISION,
                        heartbeat_at DOUBLE PRECISION, cancel_requested_at DOUBLE PRECISION,
                        cancel_reason TEXT, attempts BIGINT NOT NULL DEFAULT 0,
                        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL
                    )"""
                )
                # v1 -> v2 migration (idempotent): add operation, replace an old
                # global unique index with the scoped one. Preserve existing rows.
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='runs'")
                cols = {r[0] for r in cur.fetchall()}
                if "operation" not in cols:
                    cur.execute("ALTER TABLE runs ADD COLUMN operation TEXT NOT NULL DEFAULT 'run'")
                cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname='ux_runs_idempotency'")
                idx = cur.fetchone()
                if idx and idx[0] and "tenant_id" not in idx[0]:
                    cur.execute("DROP INDEX ux_runs_idempotency")
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_idempotency ON runs"
                    "(tenant_id, workspace_id, principal_id, operation, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
                )
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS run_events (
                        id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                        seq BIGINT NOT NULL, event_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                        payload JSONB, created_at DOUBLE PRECISION NOT NULL, UNIQUE(run_id, seq))"""
                )
                cur.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('version',%s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                    (str(SCHEMA_VERSION),),
                )
            conn.commit()

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _row(cur, row) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {d.name: row[i] for i, d in enumerate(cur.description)}

    def _next_seq(self, cur, run_id: str) -> int:
        # Lock the run row so per-run seq is monotonic under concurrency.
        cur.execute("SELECT 1 FROM runs WHERE run_id=%s FOR UPDATE", (run_id,))
        cur.execute("SELECT COALESCE(MAX(seq),0) FROM run_events WHERE run_id=%s", (run_id,))
        return int(cur.fetchone()[0]) + 1

    def _append_event(self, cur, run_id: str, kind: str, payload: Optional[dict]) -> int:
        seq = self._next_seq(cur, run_id)
        cur.execute(
            "INSERT INTO run_events(run_id, seq, event_id, kind, payload, created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (run_id, seq, uuid.uuid4().hex, kind, json.dumps(payload or {}), time.time()),
        )
        return seq

    # -- public API --------------------------------------------------------- #
    def create_run(self, identity: RunIdentity, *, initial_state: RunState = RunState.QUEUED) -> Dict[str, Any]:
        now = time.time()
        with self._conn() as conn:
            with conn.cursor() as cur:
                if identity.idempotency_key:
                    cur.execute(
                        "SELECT * FROM runs WHERE tenant_id=%s AND workspace_id=%s AND principal_id=%s "
                        "AND operation=%s AND idempotency_key=%s",
                        (identity.tenant_id, identity.workspace_id, identity.principal_id,
                         identity.operation, identity.idempotency_key),
                    )
                    existing = self._row(cur, cur.fetchone())
                    if existing is not None:
                        conn.commit()
                        if (identity.request_digest or None) != (existing.get("request_digest") or None):
                            raise IdempotencyConflict(
                                f"scoped idempotency key {identity.idempotency_key!r} exists with a "
                                f"different request_digest"
                            )
                        return existing
                cur.execute(
                    """INSERT INTO runs(run_id,task_id,tenant_id,organization_id,workspace_id,
                            principal_id,agent_id,operation,parent_task_id,delegation_id,
                            idempotency_key,request_digest,state,progress_seq,execution_deadline,
                            lease_epoch,attempts,created_at,updated_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,0,0,%s,%s)""",
                    (identity.run_id, identity.task_id, identity.tenant_id, identity.organization_id,
                     identity.workspace_id, identity.principal_id, identity.agent_id, identity.operation,
                     identity.parent_task_id, identity.delegation_id, identity.idempotency_key,
                     identity.request_digest, initial_state.value, identity.execution_deadline, now, now),
                )
                self._append_event(cur, identity.run_id, "accepted", {"state": initial_state.value})
                cur.execute("SELECT * FROM runs WHERE run_id=%s", (identity.run_id,))
                row = self._row(cur, cur.fetchone())
            assert row is not None
            conn.commit()
            return row

    def admit(self, identity: RunIdentity, *, owner: str,
              initial_state: RunState = RunState.RUNNING) -> Dict[str, Any]:
        now = time.time()
        with self._conn() as conn:
            with conn.cursor() as cur:
                if identity.idempotency_key:
                    cur.execute(
                        "SELECT * FROM runs WHERE tenant_id=%s AND workspace_id=%s AND principal_id=%s "
                        "AND operation=%s AND idempotency_key=%s",
                        (identity.tenant_id, identity.workspace_id, identity.principal_id,
                         identity.operation, identity.idempotency_key),
                    )
                    existing = self._row(cur, cur.fetchone())
                    if existing is not None:
                        conn.commit()
                        if (identity.request_digest or None) != (existing.get("request_digest") or None):
                            raise IdempotencyConflict("scoped idempotency key exists with different digest")
                        return existing
                cur.execute(
                    """INSERT INTO runs(run_id,task_id,tenant_id,organization_id,workspace_id,
                            principal_id,agent_id,operation,parent_task_id,delegation_id,
                            idempotency_key,request_digest,state,progress_seq,execution_deadline,
                            lease_owner,lease_epoch,lease_expiry,heartbeat_at,attempts,created_at,updated_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,1,%s,%s,1,%s,%s)""",
                    (identity.run_id, identity.task_id, identity.tenant_id, identity.organization_id,
                     identity.workspace_id, identity.principal_id, identity.agent_id, identity.operation,
                     identity.parent_task_id, identity.delegation_id, identity.idempotency_key,
                     identity.request_digest, initial_state.value, identity.execution_deadline,
                     owner, now + 900.0, now, now, now),
                )
                self._append_event(cur, identity.run_id, "accepted", {"state": "QUEUED"})
                self._append_event(cur, identity.run_id, "admitted",
                                   {"owner": owner, "state": initial_state.value})
                cur.execute("SELECT * FROM runs WHERE run_id=%s", (identity.run_id,))
                row = self._row(cur, cur.fetchone())
            assert row is not None
            conn.commit()
            return row

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,))
                return self._row(cur, cur.fetchone())

    def is_stop_requested(self, run_id: str, *, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        row = self.get_run(run_id)
        if row is None:
            return True
        if RunState(row["state"]) in (RunState.CANCELLING, RunState.CANCELLED,
                                      RunState.FAILED, RunState.SUCCEEDED):
            return True
        dl = row.get("execution_deadline")
        return dl is not None and now >= float(dl)

    def transition(self, run_id: str, to_state: RunState, *, kind: Optional[str] = None,
                   payload: Optional[dict] = None, result_ref: Optional[str] = None,
                   error_ref: Optional[str] = None) -> Dict[str, Any]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM runs WHERE run_id=%s FOR UPDATE", (run_id,))
                r = cur.fetchone()
                if r is None:
                    raise DurableRunError(f"run not found: {run_id}")
                cur_state = RunState(r[0])
                if to_state != cur_state and to_state not in _TRANSITIONS.get(cur_state, frozenset()):
                    raise InvalidTransition(f"{cur_state.value} -> {to_state.value} is not allowed")
                now = time.time()
                cur.execute(
                    "UPDATE runs SET state=%s, updated_at=%s, result_ref=COALESCE(%s,result_ref), "
                    "error_ref=COALESCE(%s,error_ref) WHERE run_id=%s",
                    (to_state.value, now, result_ref, error_ref, run_id),
                )
                self._append_event(cur, run_id, kind or f"state.{to_state.value.lower()}",
                                   {**(payload or {}), "state": to_state.value})
                cur.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,))
                row = self._row(cur, cur.fetchone())
            assert row is not None
            conn.commit()
            return row

    def set_state(self, run_id: str, to_state: RunState, *, result_ref: Optional[str] = None,
                  error_ref: Optional[str] = None, kind: Optional[str] = None,
                  payload: Optional[dict] = None, strict: bool = True) -> Optional[Dict[str, Any]]:
        if strict:
            return self.transition(run_id, to_state, kind=kind, payload=payload,
                                   result_ref=result_ref, error_ref=error_ref)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM runs WHERE run_id=%s FOR UPDATE", (run_id,))
                r = cur.fetchone()
                if r is None:
                    conn.commit()
                    return None
                cur_state = RunState(r[0])
                if cur_state in TERMINAL_STATES or cur_state == to_state:
                    conn.commit()
                    return self.get_run(run_id)
                now = time.time()
                cur.execute(
                    "UPDATE runs SET state=%s, updated_at=%s, result_ref=COALESCE(%s,result_ref), "
                    "error_ref=COALESCE(%s,error_ref) WHERE run_id=%s",
                    (to_state.value, now, result_ref, error_ref, run_id),
                )
                self._append_event(cur, run_id, kind or f"state.{to_state.value.lower()}",
                                   {**(payload or {}), "state": to_state.value, "adapter": True})
                cur.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,))
                row = self._row(cur, cur.fetchone())
            assert row is not None
            conn.commit()
            return row

    def record_progress(self, run_id: str, *, step: Optional[str] = None,
                        checkpoint_ref: Optional[str] = None, metric: Optional[dict] = None) -> int:
        with self._conn() as conn:
            with conn.cursor() as cur:
                now = time.time()
                seq = self._append_event(cur, run_id, "progress",
                                         {"step": step, "checkpoint_ref": checkpoint_ref, "metric": metric})
                cur.execute(
                    "UPDATE runs SET progress_seq=%s, last_progress_at=%s, updated_at=%s, "
                    "checkpoint_ref=COALESCE(%s,checkpoint_ref) WHERE run_id=%s",
                    (seq, now, now, checkpoint_ref, run_id),
                )
            conn.commit()
            return seq

    def append_event(self, run_id: str, kind: str, payload: Optional[dict] = None) -> int:
        with self._conn() as conn:
            with conn.cursor() as cur:
                seq = self._append_event(cur, run_id, kind, payload)
                cur.execute("UPDATE runs SET updated_at=%s WHERE run_id=%s", (time.time(), run_id))
            conn.commit()
            return seq

    def get_events(self, run_id: str, *, from_seq: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT run_id, seq, event_id, kind, payload, created_at FROM run_events "
                    "WHERE run_id=%s AND seq>%s ORDER BY seq ASC LIMIT %s",
                    (run_id, int(from_seq), int(limit)),
                )
                out = []
                for row in cur.fetchall():
                    payload = row[4] if isinstance(row[4], dict) else json.loads(row[4] or "{}")
                    out.append({"run_id": row[0], "seq": row[1], "event_id": row[2],
                                "kind": row[3], "payload": payload, "created_at": row[5]})
                return out

    def claim(self, run_id: str, owner: str, *, ttl_seconds: float = 900.0) -> Optional[int]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                now = time.time()
                cur.execute("SELECT state, lease_epoch FROM runs WHERE run_id=%s FOR UPDATE", (run_id,))
                r = cur.fetchone()
                if r is None or RunState(r[0]) != RunState.QUEUED:
                    conn.rollback()
                    return None
                epoch = int(r[1]) + 1
                cur.execute(
                    "UPDATE runs SET state=%s, lease_owner=%s, lease_epoch=%s, lease_expiry=%s, "
                    "heartbeat_at=%s, attempts=attempts+1, updated_at=%s WHERE run_id=%s",
                    (RunState.CLAIMED.value, owner, epoch, now + ttl_seconds, now, now, run_id),
                )
                self._append_event(cur, run_id, "claimed", {"owner": owner, "epoch": epoch})
            conn.commit()
            return epoch

    def heartbeat(self, run_id: str, owner: str, epoch: int, *, ttl_seconds: float = 900.0) -> bool:
        with self._conn() as conn:
            with conn.cursor() as cur:
                now = time.time()
                cur.execute(
                    "UPDATE runs SET heartbeat_at=%s, lease_expiry=%s, updated_at=%s "
                    "WHERE run_id=%s AND lease_owner=%s AND lease_epoch=%s",
                    (now, now + ttl_seconds, now, run_id, owner, int(epoch)),
                )
                ok = cur.rowcount == 1
            conn.commit()
            return ok

    def detect_stalled(self, *, stall_threshold: float, liveness_window: float = 3600.0,
                       now: Optional[float] = None) -> List[str]:
        now = now if now is not None else time.time()
        stalled: List[str] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT run_id, heartbeat_at, last_progress_at, created_at FROM runs "
                            "WHERE state IN ('RUNNING','CLAIMED','WAITING_CHILD') FOR UPDATE")
                for run_id, hb, lp, created in cur.fetchall():
                    hb = hb or 0
                    last_prog = lp if lp is not None else (created or 0)
                    alive = (now - hb) <= liveness_window if hb else False
                    if alive and (now - last_prog) > stall_threshold:
                        cur.execute("UPDATE runs SET state='STALLED', updated_at=%s WHERE run_id=%s", (now, run_id))
                        self._append_event(cur, run_id, "state.stalled",
                                           {"reason": "no_progress", "stall_threshold": stall_threshold})
                        stalled.append(run_id)
            conn.commit()
            return stalled

    def reconcile_dead_owner(self, is_alive, *, operation: Optional[str] = None) -> List[str]:
        reconciled: List[str] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                q = ("SELECT run_id, lease_owner FROM runs WHERE state IN "
                     "('QUEUED','CLAIMED','RUNNING','WAITING_CHILD','WAITING_APPROVAL','PAUSING','PAUSED','STALLED','CANCELLING')")
                params: tuple = ()
                if operation is not None:
                    q += " AND operation=%s"
                    params = (operation,)
                q += " FOR UPDATE"
                cur.execute(q, params)
                now = time.time()
                for run_id, owner in cur.fetchall():
                    if not owner or not str(owner).startswith("pid:"):
                        continue
                    try:
                        pid = int(str(owner).split(":", 1)[1])
                    except (ValueError, IndexError):
                        continue
                    if is_alive(pid):
                        continue
                    cur.execute("UPDATE runs SET state='UNKNOWN', updated_at=%s WHERE run_id=%s", (now, run_id))
                    self._append_event(cur, run_id, "state.unknown",
                                       {"reason": "owner_process_dead", "owner": owner})
                    reconciled.append(run_id)
            conn.commit()
            return reconciled

    def enforce_execution_deadlines(self, *, now: Optional[float] = None) -> List[str]:
        now = now if now is not None else time.time()
        enforced: List[str] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT run_id, state FROM runs WHERE execution_deadline IS NOT NULL "
                    "AND execution_deadline < %s AND state IN "
                    "('QUEUED','CLAIMED','RUNNING','WAITING_CHILD','WAITING_APPROVAL','PAUSING','PAUSED','STALLED') FOR UPDATE",
                    (now,),
                )
                for run_id, state in cur.fetchall():
                    cur_state = RunState(state)
                    target = (RunState.CANCELLING
                              if RunState.CANCELLING in _TRANSITIONS.get(cur_state, frozenset())
                              else RunState.FAILED)
                    cur.execute(
                        "UPDATE runs SET state=%s, cancel_requested_at=%s, cancel_reason=%s, updated_at=%s WHERE run_id=%s",
                        (target.value, now, "execution_deadline_reached", now, run_id),
                    )
                    self._append_event(cur, run_id, "state.deadline_enforced",
                                       {"reason": "execution_deadline_reached", "to": target.value})
                    enforced.append(run_id)
            conn.commit()
            return enforced

    def request_cancel(self, run_id: str, *, reason: str, by: str) -> Dict[str, Any]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM runs WHERE run_id=%s FOR UPDATE", (run_id,))
                r = cur.fetchone()
                if r is None:
                    raise DurableRunError(f"run not found: {run_id}")
                cur_state = RunState(r[0])
                now = time.time()
                cur.execute("UPDATE runs SET cancel_requested_at=%s, cancel_reason=%s, updated_at=%s WHERE run_id=%s",
                            (now, f"{by}:{reason}", now, run_id))
                if RunState.CANCELLING in _TRANSITIONS.get(cur_state, frozenset()):
                    cur.execute("UPDATE runs SET state=%s WHERE run_id=%s", (RunState.CANCELLING.value, run_id))
                    self._append_event(cur, run_id, "state.cancelling", {"by": by, "reason": reason})
                else:
                    self._append_event(cur, run_id, "cancel_requested",
                                       {"by": by, "reason": reason, "note": "terminal — no-op"})
                cur.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,))
                row = self._row(cur, cur.fetchone())
            assert row is not None
            conn.commit()
            return row

    def wait_for_terminal(self, run_id: str, *, wait_timeout: float, poll: float = 0.05) -> WaitResult:
        deadline = time.time() + wait_timeout
        while True:
            row = self.get_run(run_id)
            if row is None:
                raise DurableRunError(f"run not found: {run_id}")
            state = RunState(row["state"])
            terminal = state in TERMINAL_STATES
            if terminal or time.time() >= deadline:
                return WaitResult(
                    task_id=row["task_id"], run_id=run_id, state=state.value, terminal=terminal,
                    last_progress_seq=int(row["progress_seq"] or 0), checkpoint_ref=row["checkpoint_ref"],
                    result_ref=row["result_ref"],
                    reconnect=f"/v1/runs/{run_id}/events?from_seq={int(row['progress_seq'] or 0)}",
                )
            time.sleep(min(poll, max(0.0, deadline - time.time())))
