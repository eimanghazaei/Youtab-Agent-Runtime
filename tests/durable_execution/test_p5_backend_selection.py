"""P5 — backend selection is fail-closed in server mode (matrix a-e).

Product configuration contract (env prefix YOUTAB_AGENT_, single mechanism):
- YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND: unset -> sqlite (the intentionally
  supported single-node durable backend for desktop/local); 'postgres' selects
  the server/enterprise backend.
- YOUTAB_AGENT_DURABLE_PG_DSN: the PostgreSQL DSN for the postgres backend.

An enterprise that REQUIRES PostgreSQL sets the backend to 'postgres' explicitly;
then a missing/malformed/unreachable DSN REFUSES startup (fail-closed) rather than
silently opening a SQLite database. SQLite remains for explicitly supported
desktop/local modes. Failures occur at construction — before any run is accepted
or the server can report healthy — and never expose credentials.
"""

import os

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from youtab_runtime.durable_run_store import create_run_store

_ADMIN = os.environ.get("YOUTAB_TEST_PG_DSN",
                        "postgresql://youtab:devpass@127.0.0.1:55432/durable")
_BACKEND = "YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND"
_DSN = "YOUTAB_AGENT_DURABLE_PG_DSN"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv(_BACKEND, raising=False)
    monkeypatch.delenv(_DSN, raising=False)
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))


# (a) backend unset -> the documented sqlite default (desktop/local).
def test_a_unset_backend_selects_sqlite_default():
    store = create_run_store()
    assert type(store).__name__ == "SqliteRunStore"
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    assert type(adapter._run_store).__name__ == "SqliteRunStore"


# (b) backend=postgres, DSN unset -> refuse (fail closed, no run accepted).
def test_b_postgres_without_dsn_fails_closed(monkeypatch):
    monkeypatch.setenv(_BACKEND, "postgres")
    with pytest.raises(Exception) as ei:
        create_run_store()
    assert "SQLite" not in str(ei.value) or "no SQLite fallback" in str(ei.value)
    with pytest.raises(Exception):
        APIServerAdapter(PlatformConfig(enabled=True, extra={}))


# (c) malformed DSN -> refuse; no credentials in the error.
def test_c_malformed_dsn_fails_closed_no_cred_leak(monkeypatch):
    monkeypatch.setenv(_BACKEND, "postgres")
    monkeypatch.setenv(_DSN, "not-a-valid-dsn://secretuser:secretpw@nowhere")
    with pytest.raises(Exception) as ei:
        APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    msg = str(ei.value)
    assert "secretpw" not in msg and "secretuser" not in msg


# (d) unreachable PostgreSQL -> refuse (before accepting a run); no cred leak.
def test_d_unreachable_postgres_fails_closed(monkeypatch):
    monkeypatch.setenv(_BACKEND, "postgres")
    monkeypatch.setenv(_DSN, "postgresql://secretuser:secretpw@127.0.0.1:5999/nodb?connect_timeout=2")
    with pytest.raises(Exception) as ei:
        APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    assert "secretpw" not in str(ei.value)


# (e) reachable PostgreSQL -> the PostgreSQL backend is selected.
def _pg_reachable() -> bool:
    try:
        import psycopg
        with psycopg.connect(_ADMIN, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="no reachable PostgreSQL (ENV-UNAVAILABLE)")
def test_e_reachable_postgres_selects_pg_backend(monkeypatch):
    monkeypatch.setenv(_BACKEND, "postgres")
    monkeypatch.setenv(_DSN, _ADMIN)
    store = create_run_store()
    assert type(store).__name__ == "PostgresRunStore"
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    assert type(adapter._run_store).__name__ == "PostgresRunStore"
