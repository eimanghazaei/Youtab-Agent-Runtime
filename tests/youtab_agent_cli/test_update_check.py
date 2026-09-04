"""Tests for the update check mechanism in youtab_agent_cli.banner."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return cached value without calling git."""
    from youtab_agent_cli.banner import check_for_updates
    from youtab_agent_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "youtab-agent-runtime"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}), encoding="utf-8")

    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    with patch("youtab_agent_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()






def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import youtab_agent_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    # Deterministic non-blocking proof: the check blocks until released, so a
    # regression that ran it INLINE would hang inside prefetch_update_check()
    # (caught by the test timeout), while the correct backgrounded version
    # returns immediately with the result not yet set. This is timing-free — no
    # wall-clock bound to flake under -j3 CI contention.
    release = threading.Event()

    def _blocking_check(*_a, **_k):
        release.wait()
        return 5

    with patch.object(banner, "check_for_updates", _blocking_check):
        banner.prefetch_update_check()
        # The check is still blocked; the result cannot be set yet unless
        # prefetch wrongly ran the check on the calling thread.
        assert banner._update_result is None

        release.set()
        assert banner._update_check_done.wait(timeout=10)
        assert banner._update_result == 5




