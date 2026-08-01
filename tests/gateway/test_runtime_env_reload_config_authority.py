"""Regression tests for gateway per-turn env reload preserving config authority.

Issue #19158: startup bridges config.yaml agent.max_turns into
YOUTAB_AGENT_MAX_ITERATIONS, but a later per-turn load_dotenv(..., override=True)
can restore a stale .env YOUTAB_AGENT_MAX_ITERATIONS value before the next turn.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from gateway import run as gateway_run


def test_reload_runtime_env_preserves_config_max_turns(tmp_path: Path, monkeypatch) -> None:
    youtab_home = tmp_path / ".youtab-agent-runtime"
    youtab_home.mkdir()
    (youtab_home / "config.yaml").write_text(
        yaml.safe_dump({"agent": {"max_turns": 9000}}),
        encoding="utf-8",
    )
    (youtab_home / ".env").write_text(
        "YOUTAB_AGENT_MAX_ITERATIONS=90\nOPENROUTER_API_KEY=fresh-key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "_youtab_home", youtab_home)
    monkeypatch.setenv("YOUTAB_AGENT_MAX_ITERATIONS", "9000")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    gateway_run._reload_runtime_env_preserving_config_authority()

    assert os.environ["OPENROUTER_API_KEY"] == "fresh-key"
    assert os.environ["YOUTAB_AGENT_MAX_ITERATIONS"] == "9000"


