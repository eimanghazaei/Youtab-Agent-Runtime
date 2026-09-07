"""Memory-store tenant/workspace namespacing (WAVE-30H R4).

Proves managed runs get a physically isolated memory directory (per
tenant/workspace) and that standalone behaviour is unchanged, with traversal
safety on hostile ids.

Post-review correction 4: the namespace is an IMMUTABLE PER-RUN context
(contextvars), NOT a mutable process-global env var. These tests prove:
  * the enforcement point reads the per-run context;
  * a process serving CONCURRENT runs keeps every run's namespace isolated
    (threads and asyncio tasks), which a shared env var could not do;
  * the legacy env transport only SEEDS the context once and is never the
    per-operation source of truth.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import threading

import pytest

mt = importlib.import_module("tools.memory_tool")


@pytest.fixture(autouse=True)
def _clear_ns(monkeypatch):
    """Each test starts with an unset namespace in this thread's context."""
    token = mt._MEMORY_NAMESPACE.set(None)
    monkeypatch.delenv(mt.MEMORY_NAMESPACE_ENV, raising=False)
    yield
    mt._MEMORY_NAMESPACE.reset(token)


def test_standalone_flat_path_unchanged():
    base = mt.get_youtab_home() / "memories"
    assert mt.get_memory_dir() == base


def test_managed_namespaced_by_tenant_workspace():
    with mt.memory_namespace_scope("tenant-alpha", "ws-1"):
        base = mt.get_youtab_home() / "memories"
        assert mt.get_memory_dir() == base / "tenant-alpha" / "ws-1"
    # scope exits -> back to standalone
    assert mt.get_memory_dir() == mt.get_youtab_home() / "memories"


def test_two_tenants_get_distinct_dirs():
    with mt.memory_namespace_scope("tenant-a", "-"):
        a = mt.get_memory_dir()
    with mt.memory_namespace_scope("tenant-b", "-"):
        b = mt.get_memory_dir()
    assert a != b


def test_path_traversal_is_neutralized():
    with mt.memory_namespace_scope("../../etc", "../evil"):
        resolved = mt.get_memory_dir()
    base = mt.get_youtab_home() / "memories"
    # Stays under the memories root; no ".." components survive.
    assert base in resolved.parents or resolved == base
    assert ".." not in resolved.parts


def test_unscoped_workspace_hyphen_is_kept():
    with mt.memory_namespace_scope("tenant-alpha", "-"):
        base = mt.get_youtab_home() / "memories"
        assert mt.get_memory_dir() == base / "tenant-alpha" / "-"


def test_set_and_reset_token_round_trips():
    token = mt.set_memory_namespace("t1", "w1")
    assert mt.get_memory_dir().parts[-2:] == ("t1", "w1")
    mt.reset_memory_namespace(token)
    assert mt.get_memory_dir() == mt.get_youtab_home() / "memories"


# ── concurrency isolation: the property a shared env var cannot provide ───────


def test_concurrent_threads_do_not_cross_contaminate():
    """N threads each scope a distinct namespace; none sees another's."""
    base = mt.get_youtab_home() / "memories"
    results: dict[int, object] = {}
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        # Each thread gets a fresh contextvars context, so a .set here is private.
        with mt.memory_namespace_scope(f"tenant-{i}", f"ws-{i}"):
            barrier.wait()  # force real overlap: all inside their scope at once
            results[i] = mt.get_memory_dir()
            # After the barrier, re-read must still be this thread's namespace.
            if mt.get_memory_dir() != base / f"tenant-{i}" / f"ws-{i}":
                errors.append(f"thread {i} saw a foreign namespace")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    for i in range(8):
        assert results[i] == base / f"tenant-{i}" / f"ws-{i}"
    # All distinct.
    assert len({str(v) for v in results.values()}) == 8


def test_concurrent_asyncio_tasks_do_not_cross_contaminate():
    """Interleaved asyncio tasks each keep their own namespace across awaits."""
    base = mt.get_youtab_home() / "memories"

    async def run() -> None:
        seen: dict[int, object] = {}

        async def task(i: int) -> None:
            # A task copies its parent context at creation; a .set inside the
            # task is private to it. Set, yield control, then re-read.
            mt.set_memory_namespace(f"tenant-{i}", f"ws-{i}")
            await asyncio.sleep(0)  # let siblings run and set their own
            seen[i] = mt.get_memory_dir()

        await asyncio.gather(*(task(i) for i in range(8)))
        for i in range(8):
            assert seen[i] == base / f"tenant-{i}" / f"ws-{i}", seen[i]
        assert len({str(v) for v in seen.values()}) == 8

    # Run under a fresh context so the tasks' .set() cannot leak back here.
    ctx = contextvars.copy_context()
    ctx.run(lambda: asyncio.run(run()))
    # The outer context is untouched by the tasks.
    assert mt.get_memory_dir() == base


def test_legacy_env_only_seeds_once_and_is_not_per_operation(monkeypatch):
    """The env transport seeds the context once; later env mutation is ignored."""
    monkeypatch.setenv(mt.MEMORY_NAMESPACE_ENV, "seed-tenant/seed-ws")
    base = mt.get_youtab_home() / "memories"
    # First read seeds the contextvar from env.
    assert mt.get_memory_dir() == base / "seed-tenant" / "seed-ws"
    # Mutating the process-global env now must NOT change the per-run namespace.
    monkeypatch.setenv(mt.MEMORY_NAMESPACE_ENV, "attacker/other")
    assert mt.get_memory_dir() == base / "seed-tenant" / "seed-ws"
