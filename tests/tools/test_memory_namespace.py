"""Memory-store tenant/workspace namespacing (WAVE-30H R4).

Proves managed runs get a physically isolated memory directory (per
tenant/workspace) and that standalone behaviour is unchanged, with traversal
safety on hostile ids.
"""

from __future__ import annotations

import importlib

import pytest

mt = importlib.import_module("tools.memory_tool")


@pytest.fixture(autouse=True)
def _clear_ns(monkeypatch):
    monkeypatch.delenv(mt.MEMORY_NAMESPACE_ENV, raising=False)
    yield


def test_standalone_flat_path_unchanged():
    base = mt.get_youtab_home() / "memories"
    assert mt.get_memory_dir() == base


def test_managed_namespaced_by_tenant_workspace(monkeypatch):
    monkeypatch.setenv(mt.MEMORY_NAMESPACE_ENV, "tenant-alpha/ws-1")
    base = mt.get_youtab_home() / "memories"
    assert mt.get_memory_dir() == base / "tenant-alpha" / "ws-1"


def test_two_tenants_get_distinct_dirs(monkeypatch):
    monkeypatch.setenv(mt.MEMORY_NAMESPACE_ENV, "tenant-a/-")
    a = mt.get_memory_dir()
    monkeypatch.setenv(mt.MEMORY_NAMESPACE_ENV, "tenant-b/-")
    b = mt.get_memory_dir()
    assert a != b


def test_path_traversal_is_neutralized(monkeypatch):
    monkeypatch.setenv(mt.MEMORY_NAMESPACE_ENV, "../../etc/../evil/tenant")
    resolved = mt.get_memory_dir()
    base = mt.get_youtab_home() / "memories"
    # Stays under the memories root; no ".." components survive.
    assert base in resolved.parents or resolved == base
    assert ".." not in resolved.parts


def test_unscoped_workspace_hyphen_is_kept(monkeypatch):
    monkeypatch.setenv(mt.MEMORY_NAMESPACE_ENV, "tenant-alpha/-")
    base = mt.get_youtab_home() / "memories"
    assert mt.get_memory_dir() == base / "tenant-alpha" / "-"
