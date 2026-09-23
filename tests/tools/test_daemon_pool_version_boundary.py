"""Python support-boundary enforcement for the daemon pool (WAVE-25 crit 9).

``tools/daemon_pool.py`` reproduces CPython's private ThreadPoolExecutor worker
contract, which exists only in 3.8-3.13; 3.14 refactored it and the legacy path
raises AttributeError. The project therefore pins ``requires-python`` below
3.14. These tests pin BOTH ends of that decision: the declared boundary
excludes 3.14, and the pool both works on a supported interpreter and fails
with a clear, actionable message on an unsupported one.
"""

import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _requires_python() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["requires-python"]


def test_requires_python_excludes_3_14():
    spec = _requires_python()
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        specset = SpecifierSet(spec)
        assert Version("3.14.0") not in specset, spec
        assert Version("3.12.0") in specset, spec
        assert Version("3.13.0") in specset, spec
    except ImportError:  # packaging not installed — fall back to a string check
        assert "<3.14" in spec.replace(" ", ""), spec


def test_pool_works_on_supported_interpreter():
    if sys.version_info >= (3, 14):
        pytest.skip("interpreter is out of the supported band")
    from tools.daemon_pool import (
        DaemonThreadPoolExecutor,
        _LEGACY_WORKER_CONTRACT_SUPPORTED,
    )

    assert _LEGACY_WORKER_CONTRACT_SUPPORTED is True
    with DaemonThreadPoolExecutor(max_workers=2) as ex:
        results = sorted(f.result(timeout=5) for f in [ex.submit(pow, i, 2) for i in range(4)])
    assert results == [0, 1, 4, 9]


def test_unsupported_interpreter_fails_with_clear_message(monkeypatch):
    from tools import daemon_pool

    # Simulate an interpreter without the legacy worker contract (e.g. 3.14+)
    # without needing that interpreter installed.
    monkeypatch.setattr(daemon_pool, "_LEGACY_WORKER_CONTRACT_SUPPORTED", False)
    pool = daemon_pool.DaemonThreadPoolExecutor(max_workers=1)
    try:
        with pytest.raises(RuntimeError, match=r"requires-python|supported interpreter"):
            pool.submit(lambda: None)
    finally:
        pool.shutdown(wait=False)
