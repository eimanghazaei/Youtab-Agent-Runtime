"""Durable cumulative cost ceiling for a live-benchmark campaign (WAVE-30B §9).

A campaign (canary + pilot + full live benchmark, including retries and failover
calls) shares ONE hard cumulative budget in EUR. This module is the authoritative
ledger: every provider call must atomically *reserve* a conservative worst-case
amount BEFORE transmission and *reconcile* it against reported usage afterward.
A reservation that would push the committed total past the ceiling is refused —
that is how the campaign fails closed at the ceiling.

Design (mirrors youtab_runtime.run_journal):

* SQLite at ``<YOUTAB_AGENT_HOME>/runtime/campaign_budget.db`` (or an explicit
  ``db_path`` for tests), WAL + ``synchronous=FULL``, born owner-only (POSIX
  0700 dir / 0600 file, Windows protected DACL).
* All mutations run under ``BEGIN IMMEDIATE`` behind an in-process ``RLock`` +
  ``busy_timeout``, so concurrent workers (same or different processes) cannot
  both reserve past the ceiling — the reservation check and insert are one
  atomic step.
* Reservations are keyed on a retry-stable ``api_request_id`` and are idempotent:
  a retry of the same provider call reuses its reservation (no double count, no
  budget reset). A failover to a *new* provider call uses a *new*
  ``api_request_id`` and therefore consumes fresh budget — it cannot reset the
  campaign total.

Committed = Σ over reservations of ``actual_eur`` when the row is reconciled with
a known amount, else ``reserved_eur`` (a reserved-but-unreconciled or
unknown-usage call keeps its full conservative reservation). Amounts are
``Decimal`` throughout; no floats. This ledger is pure accounting — the USD→EUR
pricing bridge lives in :func:`price_worst_case_eur` / :func:`price_actual_eur`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Optional

CAMPAIGN_BUDGET_SCHEMA_VERSION = 1

_ZERO = Decimal("0")
_lock = threading.RLock()
_secured_paths: set[str] = set()


class BudgetError(RuntimeError):
    """Base class for campaign-budget failures. Callers must fail closed."""


class BudgetExceeded(BudgetError):
    """A reservation would push the committed total past the campaign ceiling."""


class PricingUnavailable(BudgetError):
    """A provider call cannot be priced (missing/stale pricing, unknown model,
    or no currency conversion) — the call must be refused, not made blind."""


class CampaignNotOpen(BudgetError):
    """No campaign with this id has been opened."""


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    campaign_id: str
    api_request_id: str
    reserved_eur: Decimal
    actual_eur: Optional[Decimal]
    state: str  # "reserved" | "reconciled" | "released"
    model: Optional[str]
    provider: Optional[str]


@dataclass(frozen=True)
class CampaignStatus:
    campaign_id: str
    ceiling_eur: Decimal
    committed_eur: Decimal
    remaining_eur: Decimal
    reservation_count: int
    fx_usd_to_eur: Decimal
    fx_source: str
    fx_asof: str
    safety_margin: Decimal
    status: str


# --- default DB location ----------------------------------------------------


def _default_db_path() -> Path:
    home = os.environ.get("YOUTAB_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".youtab-agent-runtime"
    )
    return Path(home) / "runtime" / "campaign_budget.db"


def _resolve_db_path(db_path: Optional[str | os.PathLike]) -> Path:
    return Path(db_path) if db_path is not None else _default_db_path()


# --- owner-only securing (same policy as run_journal) -----------------------


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
    except Exception:  # noqa: BLE001 - hardening is best-effort, never fatal
        import logging

        logging.getLogger(__name__).warning(
            "campaign_budget: could not secure dir %s", parent, exc_info=True
        )


def _secure_file(db_path: Path) -> None:
    if os.name == "posix":
        try:
            if db_path.exists():
                os.chmod(db_path, 0o600)
        except OSError:
            pass
        return
    try:
        from youtab_agent_cli.windows_acl import (
            apply_owner_only_dacl,
            pywin32_available,
        )

        if pywin32_available() and db_path.exists():
            apply_owner_only_dacl(db_path)
    except Exception:  # noqa: BLE001 - best-effort; inheritance is the primary path
        import logging

        logging.getLogger(__name__).warning(
            "campaign_budget: could not apply owner-only DACL to %s",
            db_path,
            exc_info=True,
        )


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

        apply_wal_with_fallback(conn, db_label="runtime/campaign_budget.db")
    except Exception:  # noqa: BLE001 - WAL is an optimization; correctness holds without it
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
    conn.execute("PRAGMA synchronous=FULL")

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS campaign (
                 campaign_id   TEXT PRIMARY KEY,
                 ceiling_eur   TEXT NOT NULL,
                 fx_usd_to_eur TEXT NOT NULL,
                 fx_source     TEXT NOT NULL,
                 fx_asof       TEXT NOT NULL,
                 safety_margin TEXT NOT NULL,
                 status        TEXT NOT NULL,
                 created_at    TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reservation (
                 reservation_id TEXT PRIMARY KEY,
                 campaign_id    TEXT NOT NULL,
                 api_request_id TEXT NOT NULL,
                 reserved_eur   TEXT NOT NULL,
                 actual_eur     TEXT,
                 state          TEXT NOT NULL,
                 model          TEXT,
                 provider       TEXT,
                 created_at     TEXT NOT NULL,
                 reconciled_at  TEXT
               )"""
        )
        # Idempotency: at most one reservation per (campaign, api_request_id).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reservation_key "
            "ON reservation(campaign_id, api_request_id)"
        )
        conn.execute(f"PRAGMA user_version={CAMPAIGN_BUDGET_SCHEMA_VERSION}")


@contextmanager
def _immediate_txn(db_path: Path) -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect(db_path)
        try:
            _initialize_schema(conn)
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()


# --- Decimal helpers --------------------------------------------------------


def _to_decimal(value, *, field: str) -> Decimal:
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BudgetError(f"{field} is not a valid decimal amount") from exc
    if d < 0:
        raise BudgetError(f"{field} must not be negative")
    return d


def _clock() -> str:
    # Avoid Date.now-style nondeterminism concerns in workflow scripts by using a
    # monotonic-free wall clock only for human-readable audit stamps.
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _committed(conn: sqlite3.Connection, campaign_id: str) -> Decimal:
    rows = conn.execute(
        "SELECT reserved_eur, actual_eur, state FROM reservation "
        "WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    total = _ZERO
    for r in rows:
        if r["state"] == "released":
            continue
        if r["state"] == "reconciled" and r["actual_eur"] is not None:
            total += Decimal(r["actual_eur"])
        else:
            total += Decimal(r["reserved_eur"])
    return total


def _load_campaign(conn: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM campaign WHERE campaign_id = ?", (campaign_id,)
    ).fetchone()
    if row is None:
        raise CampaignNotOpen(f"campaign {campaign_id!r} is not open")
    return row


def _status_from(conn: sqlite3.Connection, row: sqlite3.Row) -> CampaignStatus:
    ceiling = Decimal(row["ceiling_eur"])
    committed = _committed(conn, row["campaign_id"])
    count = conn.execute(
        "SELECT COUNT(*) FROM reservation WHERE campaign_id = ? AND state != 'released'",
        (row["campaign_id"],),
    ).fetchone()[0]
    return CampaignStatus(
        campaign_id=row["campaign_id"],
        ceiling_eur=ceiling,
        committed_eur=committed,
        remaining_eur=ceiling - committed,
        reservation_count=int(count),
        fx_usd_to_eur=Decimal(row["fx_usd_to_eur"]),
        fx_source=row["fx_source"],
        fx_asof=row["fx_asof"],
        safety_margin=Decimal(row["safety_margin"]),
        status=row["status"],
    )


# --- public API -------------------------------------------------------------


def open_campaign(
    campaign_id: str,
    *,
    ceiling_eur,
    fx_usd_to_eur,
    fx_source: str,
    fx_asof: str,
    safety_margin="0.15",
    db_path: Optional[str | os.PathLike] = None,
) -> CampaignStatus:
    """Open (idempotently) a campaign with a hard EUR ceiling and an FX snapshot.

    Re-opening an existing campaign with different parameters is refused — the
    ceiling and FX snapshot are immutable for the life of the campaign.
    """
    if not campaign_id:
        raise BudgetError("campaign_id is required")
    ceiling = _to_decimal(ceiling_eur, field="ceiling_eur")
    fx = _to_decimal(fx_usd_to_eur, field="fx_usd_to_eur")
    margin = _to_decimal(safety_margin, field="safety_margin")
    if ceiling <= 0:
        raise BudgetError("ceiling_eur must be positive")
    if fx <= 0:
        raise BudgetError("fx_usd_to_eur must be positive")
    if not fx_source or not fx_asof:
        raise BudgetError("fx_source and fx_asof are required (no unsourced FX)")
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        existing = conn.execute(
            "SELECT * FROM campaign WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
        if existing is not None:
            if (
                Decimal(existing["ceiling_eur"]) != ceiling
                or Decimal(existing["fx_usd_to_eur"]) != fx
                or Decimal(existing["safety_margin"]) != margin
            ):
                raise BudgetError(
                    f"campaign {campaign_id!r} already open with different "
                    f"ceiling/FX/margin — refusing to mutate an active budget"
                )
            return _status_from(conn, existing)
        conn.execute(
            "INSERT INTO campaign (campaign_id, ceiling_eur, fx_usd_to_eur, "
            "fx_source, fx_asof, safety_margin, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                campaign_id,
                str(ceiling),
                str(fx),
                fx_source,
                fx_asof,
                str(margin),
                "open",
                _clock(),
            ),
        )
        row = _load_campaign(conn, campaign_id)
        return _status_from(conn, row)


def reserve(
    campaign_id: str,
    *,
    api_request_id: str,
    amount_eur,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    db_path: Optional[str | os.PathLike] = None,
) -> Reservation:
    """Atomically reserve ``amount_eur`` for a provider call.

    Idempotent on ``api_request_id`` — a retry of the same call returns the
    existing reservation without reserving again. Raises :class:`BudgetExceeded`
    if committing the amount would exceed the ceiling (the call must NOT be made).
    """
    if not api_request_id:
        raise BudgetError("api_request_id is required")
    amount = _to_decimal(amount_eur, field="amount_eur")
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        row = _load_campaign(conn, campaign_id)
        existing = conn.execute(
            "SELECT * FROM reservation WHERE campaign_id = ? AND api_request_id = ?",
            (campaign_id, api_request_id),
        ).fetchone()
        if existing is not None:
            return _reservation_from(existing)
        committed = _committed(conn, campaign_id)
        ceiling = Decimal(row["ceiling_eur"])
        if committed + amount > ceiling:
            raise BudgetExceeded(
                f"reservation of €{amount} would exceed the campaign ceiling "
                f"€{ceiling} (committed €{committed}, remaining €{ceiling - committed})"
            )
        reservation_id = f"{campaign_id}:{api_request_id}"
        conn.execute(
            "INSERT INTO reservation (reservation_id, campaign_id, api_request_id, "
            "reserved_eur, actual_eur, state, model, provider, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                reservation_id,
                campaign_id,
                api_request_id,
                str(amount),
                None,
                "reserved",
                model,
                provider,
                _clock(),
            ),
        )
        return Reservation(
            reservation_id=reservation_id,
            campaign_id=campaign_id,
            api_request_id=api_request_id,
            reserved_eur=amount,
            actual_eur=None,
            state="reserved",
            model=model,
            provider=provider,
        )


def reconcile(
    campaign_id: str,
    *,
    api_request_id: str,
    actual_eur,
    db_path: Optional[str | os.PathLike] = None,
) -> Reservation:
    """Atomically reconcile a reservation against reported usage.

    ``actual_eur=None`` means usage was unknown/incomplete — the conservative
    reservation is KEPT (the row stays counted at ``reserved_eur``). A known
    amount replaces the reservation in the committed total.
    """
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        _load_campaign(conn, campaign_id)
        existing = conn.execute(
            "SELECT * FROM reservation WHERE campaign_id = ? AND api_request_id = ?",
            (campaign_id, api_request_id),
        ).fetchone()
        if existing is None:
            raise BudgetError(
                f"no reservation for api_request_id {api_request_id!r} to reconcile"
            )
        if existing["state"] == "released":
            raise BudgetError("cannot reconcile a released reservation")
        actual_str: Optional[str]
        if actual_eur is None:
            actual_str = None  # unknown usage: keep the reservation counted
        else:
            _actual_d = _to_decimal(actual_eur, field="actual_eur")
            actual_str = str(_actual_d)
            # A reconciled actual exceeding the reservation means the pre-call
            # worst-case (× retry multiplier) under-estimated this call. The
            # committed total absorbs the true amount and the NEXT reserve fails
            # closed at the ceiling, but flag it so the miscalibration is visible.
            if _actual_d > Decimal(existing["reserved_eur"]):
                import logging

                logging.getLogger(__name__).warning(
                    "campaign_budget: reconciled actual €%s exceeds reservation €%s "
                    "for %s (raise the reservation worst-case / retry multiplier)",
                    _actual_d, existing["reserved_eur"], existing["reservation_id"],
                )
        conn.execute(
            "UPDATE reservation SET actual_eur = ?, state = 'reconciled', "
            "reconciled_at = ? WHERE reservation_id = ?",
            (actual_str, _clock(), existing["reservation_id"]),
        )
        updated = conn.execute(
            "SELECT * FROM reservation WHERE reservation_id = ?",
            (existing["reservation_id"],),
        ).fetchone()
        return _reservation_from(updated)


def release(
    campaign_id: str,
    *,
    api_request_id: str,
    db_path: Optional[str | os.PathLike] = None,
) -> None:
    """Release a reservation whose provider call is KNOWN not to have been billed.

    Use only when it is certain no bytes reached the provider; when in doubt, keep
    the reservation (in-flight/billable calls stay accounted for conservatively).
    """
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        conn.execute(
            "UPDATE reservation SET state = 'released' "
            "WHERE campaign_id = ? AND api_request_id = ? AND state = 'reserved'",
            (campaign_id, api_request_id),
        )


def status(
    campaign_id: str, *, db_path: Optional[str | os.PathLike] = None
) -> CampaignStatus:
    path = _resolve_db_path(db_path)
    with _immediate_txn(path) as conn:
        row = _load_campaign(conn, campaign_id)
        return _status_from(conn, row)


def remaining_eur(
    campaign_id: str, *, db_path: Optional[str | os.PathLike] = None
) -> Decimal:
    return status(campaign_id, db_path=db_path).remaining_eur


def _reservation_from(row: sqlite3.Row) -> Reservation:
    return Reservation(
        reservation_id=row["reservation_id"],
        campaign_id=row["campaign_id"],
        api_request_id=row["api_request_id"],
        reserved_eur=Decimal(row["reserved_eur"]),
        actual_eur=(Decimal(row["actual_eur"]) if row["actual_eur"] is not None else None),
        state=row["state"],
        model=row["model"],
        provider=row["provider"],
    )


# --- USD→EUR pricing bridge (fail-closed) -----------------------------------


def _usd_cost_or_fail(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    request_count: int = 1,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Decimal:
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
    except Exception as exc:  # noqa: BLE001
        raise PricingUnavailable(f"pricing engine unavailable: {exc}") from exc
    usage = CanonicalUsage(
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_read_tokens=int(cache_read_tokens),
        cache_write_tokens=int(cache_write_tokens),
        request_count=int(request_count),
    )
    result = estimate_usage_cost(
        model, usage, provider=provider, base_url=base_url, api_key=api_key
    )
    if result.amount_usd is None or result.status == "unknown":
        raise PricingUnavailable(
            f"no usable pricing for model {model!r} (provider={provider!r}) — "
            f"refusing to price a live call blind"
        )
    return Decimal(result.amount_usd)


def price_worst_case_eur(
    model: str,
    *,
    max_input_tokens: int,
    max_output_tokens: int,
    fx_usd_to_eur,
    safety_margin="0.15",
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    request_count: int = 1,
) -> Decimal:
    """Conservative pre-call EUR reservation for a single provider request.

    Prices the maximum possible input + output (+ one request) at the model's
    USD rate, converts to EUR at the campaign FX snapshot, and adds the safety
    margin. Fail-closed (:class:`PricingUnavailable`) if the model cannot be
    priced — a call that cannot be priced must not be made.
    """
    fx = _to_decimal(fx_usd_to_eur, field="fx_usd_to_eur")
    margin = _to_decimal(safety_margin, field="safety_margin")
    usd = _usd_cost_or_fail(
        model,
        input_tokens=max_input_tokens,
        output_tokens=max_output_tokens,
        request_count=request_count,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
    )
    return (usd * fx * (Decimal("1") + margin)).quantize(Decimal("0.000001"))


def price_actual_eur(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    fx_usd_to_eur,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    request_count: int = 1,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[Decimal]:
    """Post-call EUR reconciliation from reported usage. Returns None when usage
    cannot be priced (caller keeps the conservative reservation)."""
    fx = _to_decimal(fx_usd_to_eur, field="fx_usd_to_eur")
    try:
        usd = _usd_cost_or_fail(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            request_count=request_count,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
        )
    except PricingUnavailable:
        return None
    return (usd * fx).quantize(Decimal("0.000001"))
