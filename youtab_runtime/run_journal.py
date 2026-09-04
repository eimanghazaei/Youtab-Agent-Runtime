"""WAVE-26 durable per-run event journal (frozen contracts 1-4).

This is the single append-only, per-run, principal-bound, monotonically
sequenced event store that the WAVE-26 usage/tool observer (agent 2), egress
audit (agent 3) and effect ledger (agent 4/5) all write through. It exists so
capability is judged from observable, ordered, durable state rather than from an
agent's self-report, and so six subsystems cannot invent divergent event stores.

Design (grounded in ``cron/executions.py`` and the ``youtab_state`` conventions):
  * SQLite at ``<YOUTAB_AGENT_HOME>/runtime/run_journal.db`` (resolved per call so
    an isolated benchmark ``YOUTAB_AGENT_HOME`` gets its own journal), WAL with
    the repo's network-fs fallback, ``synchronous=FULL``.
  * ``seq`` is a per-``run_id`` monotonically increasing integer assigned inside a
    ``BEGIN IMMEDIATE`` write transaction, so ordering is safe across the several
    processes that touch one run (API process + worker subprocess).
  * Ordering authority is ``seq``; wall-clock ``ts_unix_ns`` is for reporting only.
  * Idempotent append: an event carrying a ``dedupe_key`` is inserted at most once
    per ``(run_id, category, dedupe_key)`` — usage keys on the retry-stable
    ``api_request_id`` so retries never double-count, effect transitions key on
    ``(effect_id, state)``.
  * Every append requires a non-null principal ``(tenant, user)``; reads are
    principal-scoped so one principal can never read another's run.

Pre-run ``process`` events (before a durable ``run_id`` exists) pass the harness
launch token as ``run_id`` so the sequence and isolation invariants still hold;
the launch token is also echoed in the payload.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

RUN_JOURNAL_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = "run_event.v1"

#: Legal event categories (contract 3). Kind is a category-specific verb the
#: emitter chooses; the substrate does not constrain kinds beyond non-empty.
CATEGORIES = frozenset(
    {"usage", "tool_call", "tool_result", "egress", "effect", "process", "lifecycle"}
)

_PROCESS_ID = uuid.uuid4().hex
_lock = threading.RLock()


class RunJournalError(ValueError):
    """Raised on a fail-closed contract violation (bad principal/category)."""


@dataclass(frozen=True)
class Principal:
    """Authenticated principal ``(tenant, user)`` (frozen contract 2).

    Roles are authorization input, not identity, and are intentionally absent
    here. A blank tenant or user is a fail-closed error — the journal never
    stores an unattributed event.
    """

    tenant: str
    user: str

    def __post_init__(self) -> None:
        if not isinstance(self.tenant, str) or not self.tenant.strip():
            raise RunJournalError("principal.tenant must be a non-empty string")
        if not isinstance(self.user, str) or not self.user.strip():
            raise RunJournalError("principal.user must be a non-empty string")


@dataclass(frozen=True)
class RunEvent:
    """One immutable journal row."""

    seq: int
    event_id: str
    schema_version: str
    ts_unix_ns: int
    tenant: str
    user: str
    run_id: str
    correlation_id: Optional[str]
    category: str
    kind: str
    dedupe_key: Optional[str]
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def principal(self) -> Principal:
        return Principal(self.tenant, self.user)


def default_db_path() -> Path:
    """Resolve the journal path for the *current* YOUTAB_AGENT_HOME.

    Resolved per call (not cached at import) so an isolated benchmark home always
    gets its own journal file.
    """
    from youtab_constants import get_youtab_home

    return get_youtab_home().resolve() / "runtime" / "run_journal.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    # Best-effort private perms; on Windows this is a no-op (see WAVE-26 agent 1
    # for the DACL story on *secret* files — the journal stores only redacted
    # data, so 0o600 best-effort is sufficient here).
    try:
        if os.name == "posix" and db_path.exists():
            os.chmod(db_path, 0o600)
    except OSError:
        pass
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from youtab_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    apply_wal_with_fallback(conn, db_label="runtime/run_journal.db")
    conn.execute("PRAGMA synchronous=FULL")

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS run_events (
                 run_id         TEXT NOT NULL,
                 seq            INTEGER NOT NULL,
                 event_id       TEXT NOT NULL,
                 schema_version TEXT NOT NULL,
                 ts_unix_ns     INTEGER NOT NULL,
                 tenant         TEXT NOT NULL,
                 user           TEXT NOT NULL,
                 correlation_id TEXT,
                 category       TEXT NOT NULL,
                 kind           TEXT NOT NULL,
                 dedupe_key     TEXT,
                 payload_json   TEXT NOT NULL,
                 PRIMARY KEY (run_id, seq)
               )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_events_principal "
            "ON run_events(tenant, user, run_id, seq)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_events_category "
            "ON run_events(run_id, category, seq)"
        )
        # Idempotent-append guard: at most one row per (run_id, category,
        # dedupe_key) when a dedupe_key is supplied.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_events_dedupe "
            "ON run_events(run_id, category, dedupe_key) "
            "WHERE dedupe_key IS NOT NULL"
        )
        conn.execute(f"PRAGMA user_version={RUN_JOURNAL_SCHEMA_VERSION}")


@contextmanager
def _immediate_txn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection, take the write lock (BEGIN IMMEDIATE), commit/rollback,
    and always close. The in-process RLock serialises same-process writers; the
    IMMEDIATE transaction plus busy_timeout serialises cross-process writers.
    """
    with _lock:
        conn = _connect(db_path)
        try:
            _initialize_schema(conn)
            conn.isolation_level = None  # manual transaction control
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()


@contextmanager
def _read_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect(db_path)
        try:
            _initialize_schema(conn)
            yield conn
        finally:
            conn.close()


def _row_to_event(row: sqlite3.Row) -> RunEvent:
    return RunEvent(
        seq=int(row["seq"]),
        event_id=row["event_id"],
        schema_version=row["schema_version"],
        ts_unix_ns=int(row["ts_unix_ns"]),
        tenant=row["tenant"],
        user=row["user"],
        run_id=row["run_id"],
        correlation_id=row["correlation_id"],
        category=row["category"],
        kind=row["kind"],
        dedupe_key=row["dedupe_key"],
        payload=json.loads(row["payload_json"]),
    )


def append_event(
    run_id: str,
    principal: Principal,
    category: str,
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    correlation_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> RunEvent:
    """Append one event and return the stored row.

    If ``dedupe_key`` is set and a row already exists for
    ``(run_id, category, dedupe_key)``, this is a no-op and the existing row is
    returned (idempotent append). Otherwise a new monotonically increasing
    per-run ``seq`` is assigned.

    Fail-closed: an invalid principal, empty ``run_id``/``kind``, or unknown
    ``category`` raises :class:`RunJournalError` before any write.
    """
    if not isinstance(principal, Principal):
        raise RunJournalError("principal must be a Principal instance")
    if category not in CATEGORIES:
        raise RunJournalError(f"unknown event category: {category!r}")
    if not isinstance(run_id, str) or not run_id.strip():
        raise RunJournalError("run_id must be a non-empty string")
    if not isinstance(kind, str) or not kind.strip():
        raise RunJournalError("kind must be a non-empty string")

    path = db_path or default_db_path()
    payload = payload or {}
    try:
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                  default=repr)
    except Exception as exc:  # pragma: no cover - defensive
        raise RunJournalError(f"payload is not JSON-serialisable: {exc}") from exc

    with _immediate_txn(path) as conn:
        if dedupe_key is not None:
            existing = conn.execute(
                "SELECT * FROM run_events "
                "WHERE run_id=? AND category=? AND dedupe_key=?",
                (run_id, category, dedupe_key),
            ).fetchone()
            if existing is not None:
                # Idempotent: honour the first writer, ignore the duplicate.
                if (existing["tenant"] != principal.tenant
                        or existing["user"] != principal.user):
                    raise RunJournalError(
                        "dedupe_key belongs to a different principal")
                return _row_to_event(existing)

        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM run_events "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        seq = int(seq_row["next"])
        event_id = f"{run_id}:{seq}"
        ts = time.time_ns()
        conn.execute(
            """INSERT INTO run_events
                 (run_id, seq, event_id, schema_version, ts_unix_ns, tenant,
                  user, correlation_id, category, kind, dedupe_key, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, seq, event_id, EVENT_SCHEMA_VERSION, ts, principal.tenant,
             principal.user, correlation_id, category, kind, dedupe_key,
             payload_json),
        )
        row = conn.execute(
            "SELECT * FROM run_events WHERE run_id=? AND seq=?", (run_id, seq)
        ).fetchone()
    return _row_to_event(row)


def list_events(
    run_id: str,
    principal: Principal,
    *,
    after_seq: int = 0,
    category: Optional[str] = None,
    limit: int = 500,
    db_path: Optional[Path] = None,
) -> List[RunEvent]:
    """Return a run's events in ``seq`` order, scoped to the owning principal.

    A principal may only read runs it owns; events belonging to a different
    ``(tenant, user)`` are never returned (no existence leak — an unowned or
    unknown run simply yields an empty list).
    """
    if not isinstance(principal, Principal):
        raise RunJournalError("principal must be a Principal instance")
    path = db_path or default_db_path()
    clauses = ["run_id=?", "tenant=?", "user=?", "seq>?"]
    params: List[Any] = [run_id, principal.tenant, principal.user, int(after_seq)]
    if category is not None:
        clauses.append("category=?")
        params.append(category)
    params.append(max(1, min(int(limit), 5000)))
    with _read_conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_events WHERE " + " AND ".join(clauses)
            + " ORDER BY seq ASC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def list_events_by_category(
    principal: Principal,
    category: str,
    *,
    limit: int = 5000,
    db_path: Optional[Path] = None,
) -> List[RunEvent]:
    """Return a principal's events of one ``category`` across all ``run_id``s.

    Reads are principal-scoped (never cross-principal). This exists because
    ``process`` events are keyed by the harness *launch token* rather than the
    durable ``run_id`` (a restart uses a fresh token), so they cannot be found
    via :func:`list_events` for a run. In an isolated per-run journal every
    ``process`` event belongs to that one run, so a category sweep is the correct
    way to reconstruct the cross-launch process timeline. Ordered by (run_id, seq).
    """
    if not isinstance(principal, Principal):
        raise RunJournalError("principal must be a Principal instance")
    path = db_path or default_db_path()
    with _read_conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_events WHERE tenant=? AND user=? AND category=? "
            "ORDER BY run_id ASC, seq ASC LIMIT ?",
            (principal.tenant, principal.user, category, max(1, min(int(limit), 20000))),
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def latest_seq(run_id: str, *, db_path: Optional[Path] = None) -> int:
    """Return the highest seq recorded for ``run_id`` (0 if none)."""
    path = db_path or default_db_path()
    with _read_conn(path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM run_events WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return int(row["m"])
