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
import threading
import time
import uuid
import zlib
from typing import Any, Dict, List, Optional

from youtab_runtime.durable_run_authority import (
    RUNS_AUTHORITY_SCOPE,
    AuthorityHeld,
    AuthorityLost,
    InstanceAuthority,
    holder_metadata,
    new_instance_id,
)
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

# Advisory-lock key space for the run authority: pg_try_advisory_lock(int4, int4)
# with a fixed Youtab namespace ("YTAR") and a CRC of the authority scope. The
# collision domain is the connected database (advisory locks are per-database).
_AUTHORITY_LOCK_NAMESPACE = 0x59544152
_DEFAULT_SUPERVISE_INTERVAL = 2.0

# True iff backend %s holds the scope's advisory lock in the current database.
_LOCK_HELD_SQL = (
    "EXISTS(SELECT 1 FROM pg_locks l WHERE l.locktype='advisory' AND l.granted "
    "AND l.pid=%s AND l.classid::bigint=%s AND l.objid::bigint=%s AND l.objsubid=2 "
    "AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database()))"
)


def _authority_lock_key(scope: str) -> int:
    return zlib.crc32(scope.encode("utf-8")) & 0x7FFFFFFF


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
        # Exclusive run authority once acquired (server durable mode); every
        # write is then fenced against it. None = unfenced.
        self._authority: Optional["PostgresInstanceAuthority"] = None
        try:
            self._init_schema()
        except DurableRunError:
            raise
        except Exception as exc:
            # Fail closed BEFORE any run is accepted or the server reports healthy.
            # Never surface the DSN/credentials in the error — report the class only.
            raise DurableRunError(
                f"PostgreSQL backend unavailable at startup ({type(exc).__name__}); "
                f"server durability fail-closed (no SQLite fallback)"
            ) from None

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
                # Additive: exclusive run-execution authority epoch per scope.
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS runtime_authority (
                        scope TEXT PRIMARY KEY, epoch BIGINT NOT NULL,
                        instance_id TEXT NOT NULL, host TEXT, os_pid BIGINT,
                        backend_pid BIGINT, acquired_at DOUBLE PRECISION NOT NULL)"""
                )
                cur.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('version',%s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                    (str(SCHEMA_VERSION),),
                )
            conn.commit()

    # -- run authority (store-enforced singleton) ----------------------------- #
    def _fence(self, cur) -> None:
        """Verify, inside the caller's write transaction, that this store's run
        authority is current: the epoch row still names this instance AND the
        recorded lock-holding backend still holds the advisory lock. The epoch
        row is read FOR SHARE, so a takeover's epoch bump waits for this write to
        finish, and this write fails if the takeover already committed."""
        authority = self._authority
        if authority is None:
            return
        authority.ensure_held()
        cur.execute(
            f"SELECT a.epoch, a.instance_id, {_LOCK_HELD_SQL} FROM runtime_authority a "
            "WHERE a.scope=%s FOR SHARE OF a",
            (authority.backend_pid, _AUTHORITY_LOCK_NAMESPACE, authority.lock_key,
             authority.scope),
        )
        row = cur.fetchone()
        if row is None or int(row[0]) != authority.epoch or row[1] != authority.instance_id:
            authority.mark_lost("superseded by another instance")
            raise AuthorityLost("run authority superseded by another instance")
        if not row[2]:
            authority.mark_lost("advisory lock no longer held")
            raise AuthorityLost("run authority advisory lock no longer held")

    def acquire_instance_authority(self, *, scope: Optional[str] = None,
                                   supervise_interval: Optional[float] = None
                                   ) -> "PostgresInstanceAuthority":
        """Acquire the exclusive run-execution authority for this database.

        Opens a dedicated autocommit session (TCP keepalives, statement timeout),
        takes ``pg_try_advisory_lock`` without waiting, then durably bumps the
        scope epoch. Raises AuthorityHeld while another live session holds it.
        The returned authority supervises its session and reports loss; every
        write through this store object is fenced from now on."""
        scope = scope or RUNS_AUTHORITY_SCOPE
        key = _authority_lock_key(scope)
        conn = self._psycopg.connect(
            self._dsn, autocommit=True, connect_timeout=10,
            keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=3,
            tcp_user_timeout=10000,
        )
        try:
            conn.execute("SET statement_timeout = 5000")
            backend_pid = int(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            got = conn.execute("SELECT pg_try_advisory_lock(%s, %s)",
                               (_AUTHORITY_LOCK_NAMESPACE, key)).fetchone()[0]
            if not got:
                holder = conn.execute(
                    "SELECT epoch, host, os_pid FROM runtime_authority WHERE scope=%s", (scope,)
                ).fetchone()
                detail = (f" (epoch={holder[0]} host={holder[1]} pid={holder[2]})"
                          if holder else "")
                raise AuthorityHeld(
                    "another Runtime instance holds the durable run authority for this "
                    f"database{detail}"
                )
            instance_id = new_instance_id()
            meta = holder_metadata()
            epoch = int(conn.execute(
                "INSERT INTO runtime_authority(scope, epoch, instance_id, host, os_pid, "
                "backend_pid, acquired_at) VALUES(%s, 1, %s, %s, %s, %s, %s) "
                "ON CONFLICT (scope) DO UPDATE SET epoch=runtime_authority.epoch+1, "
                "instance_id=EXCLUDED.instance_id, host=EXCLUDED.host, "
                "os_pid=EXCLUDED.os_pid, backend_pid=EXCLUDED.backend_pid, "
                "acquired_at=EXCLUDED.acquired_at RETURNING epoch",
                (scope, instance_id, meta["host"], meta["os_pid"], backend_pid, time.time()),
            ).fetchone()[0])
        except Exception:
            conn.close()
            raise
        authority = PostgresInstanceAuthority(
            scope=scope, instance_id=instance_id, epoch=epoch, conn=conn,
            backend_pid=backend_pid, lock_key=key,
            supervise_interval=(supervise_interval if supervise_interval is not None
                                else _DEFAULT_SUPERVISE_INTERVAL),
        )
        self._authority = authority
        authority.start_supervision()
        return authority

    def reconcile_prior_instances(self, authority) -> List[str]:
        """Move every nonterminal ``/v1/runs`` run owned by a prior instance to
        UNKNOWN. Callable only while holding ``authority`` (fenced): exclusive
        ownership proves any other owner is gone, whatever its old PID was.
        Ownerless rows and terminal rows are unchanged; nothing is resubmitted."""
        if self._authority is not authority:
            raise DurableRunError("reconcile_prior_instances requires this store's held authority")
        reconciled: List[str] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                self._fence(cur)
                cur.execute(
                    "SELECT run_id, lease_owner FROM runs WHERE state IN "
                    "('QUEUED','CLAIMED','RUNNING','WAITING_CHILD','WAITING_APPROVAL','PAUSING',"
                    "'PAUSED','STALLED','CANCELLING') AND operation='run' "
                    "AND lease_owner IS NOT NULL AND lease_owner <> %s FOR UPDATE",
                    (authority.owner,),
                )
                now = time.time()
                for run_id, owner in cur.fetchall():
                    cur.execute("UPDATE runs SET state='UNKNOWN', updated_at=%s WHERE run_id=%s",
                                (now, run_id))
                    self._append_event(cur, run_id, "state.unknown", {
                        "reason": "prior_instance_superseded", "owner": owner,
                        "authority_epoch": authority.epoch,
                    })
                    reconciled.append(run_id)
            conn.commit()
            return reconciled

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
                self._fence(cur)
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
                self._fence(cur)
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
                self._fence(cur)
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

    def open_approval(self, run_id: str, *, approval_id: str,
                      kind: str = "approval_request",
                      payload: Optional[dict] = None) -> Dict[str, Any]:
        """Atomically open an approval on a RUNNING run — expected-from-state (PG).

        Advisory-fenced transaction: the run row is taken ``FOR UPDATE``; a
        from-state CAS ``UPDATE ... WHERE state='RUNNING'`` (rowcount 1) moves it
        to WAITING_APPROVAL and appends one ``approval_request`` event with
        ``approval_id``. Exact-id retry while WAITING_APPROVAL is idempotent; a
        different id raises ``ApprovalAlreadyOpen`` (pending id not replaced).
        Mirrors the SqliteRunStore semantics; see it for the full contract."""
        from youtab_runtime.durable_run_store import ApprovalAlreadyOpen
        if not isinstance(approval_id, str) or not approval_id.strip():
            raise ValueError("approval_id must be a nonempty string")
        with self._conn() as conn:
            with conn.cursor() as cur:
                self._fence(cur)
                cur.execute("SELECT state FROM runs WHERE run_id=%s FOR UPDATE", (run_id,))
                r = cur.fetchone()
                if r is None:
                    raise DurableRunError(f"run not found: {run_id}")
                cur_state = RunState(r[0])
                if cur_state == RunState.WAITING_APPROVAL:
                    cur.execute(
                        "SELECT payload FROM run_events WHERE run_id=%s AND kind='approval_request' "
                        "ORDER BY seq DESC LIMIT 1",
                        (run_id,),
                    )
                    ev = cur.fetchone()
                    open_id = None
                    if ev is not None:
                        raw = ev[0]
                        d = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                        open_id = (d or {}).get("approval_id")
                    if open_id == approval_id:
                        cur.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,))
                        row = self._row(cur, cur.fetchone())
                        assert row is not None
                        conn.commit()
                        return row
                    raise ApprovalAlreadyOpen(
                        f"run {run_id} already has a different open approval; not replacing it"
                    )
                if cur_state != RunState.RUNNING:
                    raise InvalidTransition(
                        f"cannot open approval from {cur_state.value} (expected RUNNING)"
                    )
                now = time.time()
                cur.execute(
                    "UPDATE runs SET state=%s, updated_at=%s WHERE run_id=%s AND state='RUNNING'",
                    (RunState.WAITING_APPROVAL.value, now, run_id),
                )
                if cur.rowcount != 1:
                    raise ApprovalAlreadyOpen(
                        f"run {run_id} is no longer RUNNING (concurrent open)"
                    )
                self._append_event(
                    cur, run_id, kind,
                    {**(payload or {}), "approval_id": approval_id,
                     "state": RunState.WAITING_APPROVAL.value},
                )
                cur.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,))
                row = self._row(cur, cur.fetchone())
            assert row is not None
            conn.commit()
            return row

    def decide_open_approval(self, run_id: str, *, approval_id: str, to_state: RunState,
                             kind: str, payload: Optional[dict] = None,
                             result_ref: Optional[str] = None,
                             error_ref: Optional[str] = None) -> Dict[str, Any]:
        """Atomically resolve the OPEN approval — id-bound, single-use (PG).

        Advisory-fenced transaction: the run row is taken ``FOR UPDATE``, must be
        WAITING_APPROVAL, ``approval_id`` must equal the latest ``approval_request``
        event's ``payload.approval_id``, ``to_state`` must be a valid target, and
        the state UPDATE is a CAS on ``state='WAITING_APPROVAL'`` (rowcount 1) so
        exactly one decision wins. Deny leaves ``result_ref`` NULL. Mirrors the
        SqliteRunStore semantics; see it for the full contract."""
        from youtab_runtime.durable_run_store import ApprovalNotOpen
        with self._conn() as conn:
            with conn.cursor() as cur:
                self._fence(cur)
                cur.execute("SELECT state FROM runs WHERE run_id=%s FOR UPDATE", (run_id,))
                r = cur.fetchone()
                if r is None:
                    raise DurableRunError(f"run not found: {run_id}")
                cur_state = RunState(r[0])
                if cur_state != RunState.WAITING_APPROVAL:
                    raise ApprovalNotOpen(
                        f"run {run_id} is not awaiting approval (state={cur_state.value})"
                    )
                if to_state not in _TRANSITIONS.get(RunState.WAITING_APPROVAL, frozenset()):
                    raise InvalidTransition(f"WAITING_APPROVAL -> {to_state.value} is not allowed")
                cur.execute(
                    "SELECT payload FROM run_events WHERE run_id=%s AND kind='approval_request' "
                    "ORDER BY seq DESC LIMIT 1",
                    (run_id,),
                )
                ev = cur.fetchone()
                open_id = None
                if ev is not None:
                    raw = ev[0]
                    d = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                    open_id = (d or {}).get("approval_id")
                if not open_id or open_id != approval_id:
                    raise ApprovalNotOpen(
                        f"approval_id does not match the open approval for run {run_id}"
                    )
                now = time.time()
                cur.execute(
                    "UPDATE runs SET state=%s, updated_at=%s, "
                    "result_ref=COALESCE(%s,result_ref), error_ref=COALESCE(%s,error_ref) "
                    "WHERE run_id=%s AND state='WAITING_APPROVAL'",
                    (to_state.value, now, result_ref, error_ref, run_id),
                )
                if cur.rowcount != 1:
                    raise ApprovalNotOpen(
                        f"approval for run {run_id} is no longer open (already decided)"
                    )
                self._append_event(
                    cur, run_id, kind,
                    {**(payload or {}), "approval_id": approval_id, "state": to_state.value},
                )
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
                self._fence(cur)
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
                self._fence(cur)
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
                self._fence(cur)
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
                self._fence(cur)
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
                self._fence(cur)
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
                self._fence(cur)
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
                self._fence(cur)
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
                self._fence(cur)
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
                self._fence(cur)
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


class PostgresInstanceAuthority(InstanceAuthority):
    """Run authority held as a session advisory lock on a dedicated connection.

    A daemon supervisor re-verifies, every ``supervise_interval`` seconds on that
    same session, that the lock is still granted to it and the epoch row still
    names this instance. Any failure (session or database loss, statement
    timeout, supersession) is reported as authority loss, the fail-stop trigger.
    PostgreSQL itself releases the lock as soon as the session ends (process
    crash, network loss detected by keepalive/tcp_user_timeout, server restart).
    """

    def __init__(self, *, scope: str, instance_id: str, epoch: int, conn,
                 backend_pid: int, lock_key: int, supervise_interval: float):
        super().__init__(scope=scope, instance_id=instance_id, epoch=epoch)
        self.backend_pid = int(backend_pid)
        self.lock_key = int(lock_key)
        self._conn = conn
        self._conn_lock = threading.Lock()
        self._interval = max(0.05, float(supervise_interval))
        self._stop = threading.Event()

    def start_supervision(self) -> None:
        threading.Thread(
            target=self._supervise, name="youtab-run-authority", daemon=True
        ).start()

    def verify(self) -> None:
        """One supervision check; marks loss and raises AuthorityLost on failure."""
        self.ensure_held()
        try:
            with self._conn_lock:
                row = self._conn.execute(
                    f"SELECT {_LOCK_HELD_SQL}, "
                    "(SELECT epoch FROM runtime_authority WHERE scope=%s), "
                    "(SELECT instance_id FROM runtime_authority WHERE scope=%s)",
                    (self.backend_pid, _AUTHORITY_LOCK_NAMESPACE, self.lock_key,
                     self.scope, self.scope),
                ).fetchone()
        except Exception as exc:
            reason = f"authority session failed ({type(exc).__name__})"
            self.mark_lost(reason)
            raise AuthorityLost(reason) from None
        if not row[0]:
            self.mark_lost("advisory lock no longer held")
            raise AuthorityLost("advisory lock no longer held")
        if row[1] is None or int(row[1]) != self.epoch or row[2] != self.instance_id:
            self.mark_lost("superseded by another instance")
            raise AuthorityLost("superseded by another instance")

    def _supervise(self) -> None:
        while not self._stop.wait(self._interval):
            if not self.held:
                return
            try:
                self.verify()
            except Exception:
                return

    def _release_backend(self) -> None:
        self._stop.set()
        try:
            with self._conn_lock:
                self._conn.execute("SELECT pg_advisory_unlock(%s, %s)",
                                   (_AUTHORITY_LOCK_NAMESPACE, self.lock_key))
        finally:
            self._conn.close()
