"""One durable shared budget for a whole managed execution tree (WAVE-30H R5).

The Simorgh grant's :class:`ReasoningEnvelopeV2` is the authoritative ceiling for
the ENTIRE execution tree rooted at ``root_run_id`` — iterations, total tokens,
cost (micro-units), spawn depth, concurrent agents, retries and a wall-clock
deadline. Unlike the €-denominated benchmark ``campaign_budget`` (which only
exists when a live-benchmark campaign is configured), this budget is enforced
UNCONDITIONALLY for managed runs and never depends on any benchmark env var.

The key invariant (ADR-0002 R5): a delegated child agent inherits the *remaining*
root budget — it debits the SAME row keyed on ``root_run_id`` — and never
receives a fresh or unlimited budget. The root run seeds the row once
(idempotently); children and retries only ``consume`` against it.

Durability + atomicity mirror ``campaign_budget``: owner-only dir/file, WAL with
fallback, and every mutation under a single ``BEGIN IMMEDIATE`` transaction so a
debit is a check-and-record with no TOCTOU window — safe across threads AND
processes (the tree can span delegated worker subprocesses).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

SCHEMA_VERSION = 1
_lock = threading.RLock()
_secured_paths: set[str] = set()

# Bounded retry for acquiring the shared write lock when a transient SQLITE_BUSY
# ("database is locked") outlives ``busy_timeout`` under heavy cross-process
# contention (many spawned workers debiting one tree on a slow host, especially
# with a DELETE-journal fallback). Retrying the ACQUIRE is safe and does not relax
# the ceiling: a locked BEGIN IMMEDIATE never started a transaction.
_TXN_MAX_ATTEMPTS = 8
_TXN_BACKOFF_S = 0.05


# --- errors -----------------------------------------------------------------


class TreeBudgetError(RuntimeError):
    """Base class for execution-tree budget failures (all fail-closed)."""


class TreeBudgetExceeded(TreeBudgetError):
    """A quantitative dimension (iterations/tokens/cost) would be exceeded."""

    def __init__(self, dimension: str, *, requested: int, remaining: int) -> None:
        super().__init__(
            f"execution-tree {dimension} budget exceeded "
            f"(requested {requested}, remaining {remaining})"
        )
        self.dimension = dimension
        self.requested = requested
        self.remaining = remaining


class TreeDepthExceeded(TreeBudgetError):
    def __init__(self, depth: int, ceiling: int) -> None:
        super().__init__(f"spawn depth {depth} exceeds max_spawn_depth {ceiling}")
        self.depth = depth
        self.ceiling = ceiling


class TreeConcurrencyExceeded(TreeBudgetError):
    def __init__(self, live: int, ceiling: int) -> None:
        super().__init__(
            f"concurrent agents {live + 1} would exceed max_concurrent_agents {ceiling}"
        )
        self.ceiling = ceiling


class TreeRetryExceeded(TreeBudgetError):
    def __init__(self, attempts: int, ceiling: int) -> None:
        super().__init__(f"retry attempts {attempts} exceed max_retries {ceiling}")
        self.attempts = attempts
        self.ceiling = ceiling


class TreeDeadlineExceeded(TreeBudgetError):
    def __init__(self, now: datetime, deadline: datetime) -> None:
        super().__init__(f"execution-tree deadline {deadline.isoformat()} passed")
        self.now = now
        self.deadline = deadline


# --- pure adapters (no I/O) -------------------------------------------------


@dataclass(frozen=True)
class TreeBudgetParams:
    max_iterations: int
    max_spawn_depth: int
    max_concurrent_agents: int
    max_total_tokens: int
    max_cost_micros: int
    max_retries: int
    deadline_at: datetime


def reasoning_to_tree_params(reasoning: Any) -> TreeBudgetParams:
    """Map a grant ``ReasoningEnvelopeV2`` (or an equivalent mapping) to params.

    Pure and total: accepts either a pydantic model with the attributes or a
    plain mapping; ``deadline_at`` may be a datetime or an ISO string.
    """
    def _get(name: str):
        if isinstance(reasoning, Mapping):
            return reasoning[name]
        return getattr(reasoning, name)

    deadline = _get("deadline_at")
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline)
    if deadline.tzinfo is None:
        raise ValueError("deadline_at must be timezone-aware")
    return TreeBudgetParams(
        max_iterations=int(_get("max_iterations")),
        max_spawn_depth=int(_get("max_spawn_depth")),
        max_concurrent_agents=int(_get("max_concurrent_agents")),
        max_total_tokens=int(_get("max_total_tokens")),
        max_cost_micros=int(_get("max_cost_micros")),
        max_retries=int(_get("max_retries")),
        deadline_at=deadline.astimezone(UTC),
    )


def reasoning_to_run_limits(reasoning: Any):
    """Bridge the grant reasoning to the loop's existing :class:`RunLimits`.

    Maps the dimensionless ceilings (iterations, total tokens, retries) the agent
    loop already gates on. Cost stays in the tree budget's own micro-unit
    dimension (``max_cost_micros``), NOT forced into the €-campaign ledger.
    """
    from youtab_runtime.run_limits import RunLimits

    params = reasoning_to_tree_params(reasoning)
    return RunLimits(
        max_total_tokens=params.max_total_tokens,
        max_iterations=params.max_iterations,
        max_retries=params.max_retries,
    )


# --- durable store ----------------------------------------------------------


@dataclass(frozen=True)
class TreeBudgetSnapshot:
    root_run_id: str
    iterations_used: int
    tokens_used: int
    cost_micros_used: int
    retries_used: int
    live_agents: int
    max_iterations: int
    max_total_tokens: int
    max_cost_micros: int
    max_retries: int
    max_spawn_depth: int
    max_concurrent_agents: int
    deadline_at: datetime

    def remaining(self, dimension: str) -> int:
        return {
            "iterations": self.max_iterations - self.iterations_used,
            "tokens": self.max_total_tokens - self.tokens_used,
            "cost_micros": self.max_cost_micros - self.cost_micros_used,
        }[dimension]


def _default_db_path() -> Path:
    home = os.environ.get("YOUTAB_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".youtab-agent-runtime"
    )
    return Path(home) / "runtime" / "execution_tree_budget.db"


def _resolve_db_path(db_path: Optional[str | os.PathLike]) -> Path:
    return Path(db_path) if db_path is not None else _default_db_path()


def _secure_dir(parent: Path) -> None:
    parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
        return
    try:
        from youtab_agent_cli.windows_acl import (
            pywin32_available,
            secure_directory_owner_only,
        )

        if pywin32_available():
            secure_directory_owner_only(parent)
    except Exception:  # noqa: BLE001 - hardening is best-effort
        pass


def _secure_file(db_path: Path) -> None:
    if os.name == "posix":
        try:
            if db_path.exists():
                os.chmod(db_path, 0o600)
        except OSError:
            pass
        return
    try:
        from youtab_agent_cli.windows_acl import apply_owner_only_dacl, pywin32_available

        if pywin32_available() and db_path.exists():
            apply_owner_only_dacl(db_path)
    except Exception:  # noqa: BLE001 - best-effort
        pass


def _connect(db_path: Path) -> sqlite3.Connection:
    key = str(db_path)
    first_time = key not in _secured_paths
    if first_time:
        _secure_dir(db_path.parent)
    conn = sqlite3.connect(db_path, timeout=10)
    if first_time:
        _secure_file(db_path)
        _secured_paths.add(key)
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        from youtab_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="runtime/execution_tree_budget.db")
    except Exception:  # noqa: BLE001 - WAL is an optimization; correctness holds without it
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
    conn.execute("PRAGMA synchronous=FULL")
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS execution_tree (
                 root_run_id           TEXT PRIMARY KEY,
                 max_iterations        INTEGER NOT NULL,
                 max_spawn_depth       INTEGER NOT NULL,
                 max_concurrent_agents INTEGER NOT NULL,
                 max_total_tokens      INTEGER NOT NULL,
                 max_cost_micros       INTEGER NOT NULL,
                 max_retries           INTEGER NOT NULL,
                 deadline_at           TEXT NOT NULL,
                 iterations_used       INTEGER NOT NULL DEFAULT 0,
                 tokens_used           INTEGER NOT NULL DEFAULT 0,
                 cost_micros_used      INTEGER NOT NULL DEFAULT 0,
                 retries_used          INTEGER NOT NULL DEFAULT 0,
                 live_agents           INTEGER NOT NULL DEFAULT 0,
                 created_at            TEXT NOT NULL
               )"""
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    # Concurrency permits as a durable MEMBERSHIP table (not a raw counter): one
    # row per live agent instance, so a permit cannot leak on crash — a dead
    # owner's row is reclaimable by reap_dead_agents (R5 + R7). Idempotent create
    # so both fresh and pre-existing DBs converge.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS execution_tree_agent (
             root_run_id TEXT NOT NULL,
             agent_id    TEXT NOT NULL,
             depth       INTEGER NOT NULL,
             pid         INTEGER,
             incarnation TEXT,
             acquired_at TEXT NOT NULL,
             PRIMARY KEY (root_run_id, agent_id)
           )"""
    )


def _is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


@contextmanager
def _immediate_txn(db_path: Path) -> Iterator[sqlite3.Connection]:
    with _lock:
        # Acquire the shared write lock with a bounded retry on transient
        # "database is locked". A locked BEGIN IMMEDIATE never started a
        # transaction and ``_initialize_schema`` is idempotent (IF NOT EXISTS), so
        # retrying the acquire cannot double-debit or corrupt state. The debit
        # itself still runs inside ONE BEGIN IMMEDIATE..COMMIT — atomicity and the
        # ceiling check are unchanged. On exhaustion the OperationalError is
        # re-raised (fail-loud), never silently skipped.
        conn: Optional[sqlite3.Connection] = None
        for attempt in range(_TXN_MAX_ATTEMPTS):
            conn = _connect(db_path)
            try:
                _initialize_schema(conn)
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                break  # write lock held
            except sqlite3.OperationalError as exc:
                conn.close()
                conn = None
                if not _is_locked_error(exc) or attempt == _TXN_MAX_ATTEMPTS - 1:
                    raise
                time.sleep(_TXN_BACKOFF_S * (attempt + 1))
        assert conn is not None  # loop broke with a held lock or already re-raised
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _clock() -> str:
    return datetime.now(UTC).isoformat()


def _load(conn: sqlite3.Connection, root_run_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM execution_tree WHERE root_run_id = ?", (root_run_id,)
    ).fetchone()
    if row is None:
        raise TreeBudgetError(
            f"no execution-tree budget opened for root_run_id {root_run_id!r}"
        )
    return row


def _active_agents(conn: sqlite3.Connection, root_run_id: str) -> int:
    """Live concurrent-agent count = number of membership rows (leak-safe truth)."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM execution_tree_agent WHERE root_run_id = ?",
            (root_run_id,),
        ).fetchone()[0]
    )


def _build_snapshot(conn: sqlite3.Connection, root_run_id: str) -> TreeBudgetSnapshot:
    row = _load(conn, root_run_id)
    return TreeBudgetSnapshot(
        root_run_id=row["root_run_id"],
        iterations_used=row["iterations_used"],
        tokens_used=row["tokens_used"],
        cost_micros_used=row["cost_micros_used"],
        retries_used=row["retries_used"],
        live_agents=_active_agents(conn, root_run_id),
        max_iterations=row["max_iterations"],
        max_total_tokens=row["max_total_tokens"],
        max_cost_micros=row["max_cost_micros"],
        max_retries=row["max_retries"],
        max_spawn_depth=row["max_spawn_depth"],
        max_concurrent_agents=row["max_concurrent_agents"],
        deadline_at=datetime.fromisoformat(row["deadline_at"]),
    )


def open_tree(
    root_run_id: str,
    params: TreeBudgetParams,
    *,
    db_path: Optional[str | os.PathLike] = None,
) -> TreeBudgetSnapshot:
    """Idempotently seed the shared budget for ``root_run_id`` (root run only).

    The FIRST writer (the root run) creates the row with the grant's ceilings; a
    child/retry that calls ``open_tree`` again is a no-op — it MUST NOT reset the
    consumed counters or widen the ceilings. Returns the current snapshot.
    """
    if not root_run_id:
        raise TreeBudgetError("root_run_id is required")
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        existing = conn.execute(
            "SELECT * FROM execution_tree WHERE root_run_id = ?", (root_run_id,)
        ).fetchone()
        if existing is not None:
            return _build_snapshot(conn, root_run_id)  # child/retry: never reseed
        conn.execute(
            "INSERT INTO execution_tree (root_run_id, max_iterations, max_spawn_depth, "
            "max_concurrent_agents, max_total_tokens, max_cost_micros, max_retries, "
            "deadline_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                root_run_id,
                params.max_iterations,
                params.max_spawn_depth,
                params.max_concurrent_agents,
                params.max_total_tokens,
                params.max_cost_micros,
                params.max_retries,
                params.deadline_at.astimezone(UTC).isoformat(),
                _clock(),
            ),
        )
        return _build_snapshot(conn, root_run_id)


def consume(
    root_run_id: str,
    *,
    iterations: int = 0,
    tokens: int = 0,
    cost_micros: int = 0,
    db_path: Optional[str | os.PathLike] = None,
) -> TreeBudgetSnapshot:
    """Atomically debit the SHARED tree budget. Fail-closed on any dimension.

    The root run and every delegated child call this against the SAME
    ``root_run_id``, so the tree cannot collectively exceed the grant's ceilings.
    Raises :class:`TreeBudgetExceeded` (no partial debit) if any dimension would
    be exceeded.
    """
    if iterations < 0 or tokens < 0 or cost_micros < 0:
        raise TreeBudgetError("consume amounts must be non-negative")
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        row = _load(conn, root_run_id)
        checks = (
            ("iterations", iterations, row["iterations_used"], row["max_iterations"]),
            ("tokens", tokens, row["tokens_used"], row["max_total_tokens"]),
            ("cost_micros", cost_micros, row["cost_micros_used"], row["max_cost_micros"]),
        )
        for dim, delta, used, ceiling in checks:
            if delta and used + delta > ceiling:
                raise TreeBudgetExceeded(
                    dim, requested=delta, remaining=ceiling - used
                )
        conn.execute(
            "UPDATE execution_tree SET iterations_used = iterations_used + ?, "
            "tokens_used = tokens_used + ?, cost_micros_used = cost_micros_used + ? "
            "WHERE root_run_id = ?",
            (iterations, tokens, cost_micros, root_run_id),
        )
        return _build_snapshot(conn, root_run_id)


def register_retry(
    root_run_id: str, *, db_path: Optional[str | os.PathLike] = None
) -> TreeBudgetSnapshot:
    """Atomically count one retry against the tree; fail-closed past max_retries."""
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        row = _load(conn, root_run_id)
        if row["retries_used"] + 1 > row["max_retries"]:
            raise TreeRetryExceeded(row["retries_used"] + 1, row["max_retries"])
        conn.execute(
            "UPDATE execution_tree SET retries_used = retries_used + 1 "
            "WHERE root_run_id = ?",
            (root_run_id,),
        )
        return _build_snapshot(conn, root_run_id)


def acquire_agent_slot(
    root_run_id: str,
    agent_id: str,
    *,
    depth: int,
    pid: Optional[int] = None,
    incarnation: Optional[str] = None,
    db_path: Optional[str | os.PathLike] = None,
) -> TreeBudgetSnapshot:
    """Atomically acquire a concurrency permit for ONE agent instance.

    A permit is a durable MEMBERSHIP ROW keyed by ``(root_run_id, agent_id)`` —
    NOT a raw counter — so a permit cannot leak on crash: a dead owner's row is
    reclaimable by :func:`reap_dead_agents`. Fail-closed on spawn depth
    (:class:`TreeDepthExceeded`) and concurrency (:class:`TreeConcurrencyExceeded`).

    IDEMPOTENT: re-acquiring the SAME ``agent_id`` (e.g. a retry of the same
    instance) is a no-op that succeeds without consuming a second permit. Records
    ``pid``/``incarnation`` so a crashed owner's permit can be reclaimed (R7).
    """
    if not agent_id:
        raise TreeBudgetError("agent_id is required to acquire a concurrency permit")
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        row = _load(conn, root_run_id)
        if depth > row["max_spawn_depth"]:
            raise TreeDepthExceeded(depth, row["max_spawn_depth"])
        already = conn.execute(
            "SELECT 1 FROM execution_tree_agent WHERE root_run_id = ? AND agent_id = ?",
            (root_run_id, agent_id),
        ).fetchone()
        if already is None:
            live = _active_agents(conn, root_run_id)
            if live + 1 > row["max_concurrent_agents"]:
                raise TreeConcurrencyExceeded(live, row["max_concurrent_agents"])
            conn.execute(
                "INSERT INTO execution_tree_agent (root_run_id, agent_id, depth, "
                "pid, incarnation, acquired_at) VALUES (?,?,?,?,?,?)",
                (root_run_id, agent_id, int(depth), pid, incarnation, _clock()),
            )
        return _build_snapshot(conn, root_run_id)


def release_agent_slot(
    root_run_id: str,
    agent_id: str,
    *,
    db_path: Optional[str | os.PathLike] = None,
) -> None:
    """Atomically release an agent's permit. IDEMPOTENT — releasing an
    already-released (or never-acquired) permit is a no-op, so a
    success/failure/timeout/cancellation ``finally`` can always call it safely."""
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        conn.execute(
            "DELETE FROM execution_tree_agent WHERE root_run_id = ? AND agent_id = ?",
            (root_run_id, agent_id),
        )


def active_agent_count(
    root_run_id: str, *, db_path: Optional[str | os.PathLike] = None
) -> int:
    """Current live-agent permit count for the tree."""
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        return _active_agents(conn, root_run_id)


def reap_dead_agents(
    root_run_id: str,
    *,
    is_alive,
    db_path: Optional[str | os.PathLike] = None,
) -> list[str]:
    """Reclaim permits whose owning process/incarnation is dead (no leak on crash).

    ``is_alive(pid, incarnation) -> bool`` decides liveness — the caller wires the
    R7 PID-incarnation check so a recycled PID never keeps a stale permit alive
    NOR reclaims a genuinely live one. Rows with no recorded pid are left intact
    (in-process agents rely on their ``finally`` release). Returns the reclaimed
    ``agent_id`` list.
    """
    path = _resolve_db_path(db_path)
    reclaimed: list[str] = []
    with _immediate_txn(path) as conn:
        rows = conn.execute(
            "SELECT agent_id, pid, incarnation FROM execution_tree_agent "
            "WHERE root_run_id = ? AND pid IS NOT NULL",
            (root_run_id,),
        ).fetchall()
        for r in rows:
            if not is_alive(r["pid"], r["incarnation"]):
                conn.execute(
                    "DELETE FROM execution_tree_agent WHERE root_run_id = ? AND agent_id = ?",
                    (root_run_id, r["agent_id"]),
                )
                reclaimed.append(r["agent_id"])
    return reclaimed


def check_deadline(
    root_run_id: str,
    *,
    now: Optional[datetime] = None,
    db_path: Optional[str | os.PathLike] = None,
) -> None:
    """Raise :class:`TreeDeadlineExceeded` if the tree's wall-clock deadline passed."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        row = _load(conn, root_run_id)
        deadline = datetime.fromisoformat(row["deadline_at"])
        if moment > deadline:
            raise TreeDeadlineExceeded(moment, deadline)


def snapshot(
    root_run_id: str, *, db_path: Optional[str | os.PathLike] = None
) -> TreeBudgetSnapshot:
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        return _build_snapshot(conn, root_run_id)
