"""Microsoft GraphRAG is PROHIBITED across the Youtab Agent Runtime (Owner ADR).

Fails CI if Microsoft GraphRAG is reintroduced as an active dependency, import,
configuration, or installed package. Targets ONLY the Microsoft GraphRAG product
surface (``graphrag`` / ``graph-rag`` / ``graph_rag`` / ``microsoft/graphrag``);
it deliberately does NOT touch the legitimate, required capabilities — graph
databases, graph/knowledge memory, vector stores, or the engine's own per-agent
memory.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[2]  # repo root
# Source packages that must never import Microsoft GraphRAG.
SOURCE_ROOTS = [
    ENGINE_ROOT / "youtab_agent_cli",
    ENGINE_ROOT / "agent",
    ENGINE_ROOT / "tools",
    ENGINE_ROOT / "gateway",
    ENGINE_ROOT / "run_agent.py",
    ENGINE_ROOT / "cli.py",
]

_DEP_TOKEN = re.compile(r"(^|[^A-Za-z0-9_./-])graphrag([^A-Za-z0-9_]|$)", re.IGNORECASE)
_IMPORT = re.compile(r"^\s*(?:import|from)\s+graphrag(?:\b|\.)")


def test_graphrag_is_not_importable():
    assert (
        importlib.util.find_spec("graphrag") is None
    ), "Microsoft GraphRAG is prohibited but resolves as an importable package."


def test_no_dependency_manifest_declares_graphrag():
    offenders = []
    for name in (
        "pyproject.toml",
        "requirements.txt",
        "poetry.lock",
        "constraints.txt",
    ):
        p = ENGINE_ROOT / name
        if not p.exists():
            continue
        for i, line in enumerate(
            p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            if _DEP_TOKEN.search(line):
                offenders.append(f"{p.name}:{i}: {line.strip()}")
    assert not offenders, "Microsoft GraphRAG dependency declared:\n" + "\n".join(
        offenders
    )


def _py_files():
    for root in SOURCE_ROOTS:
        if root.is_file() and root.suffix == ".py":
            yield root
        elif root.is_dir():
            yield from root.rglob("*.py")


def test_no_source_imports_graphrag():
    offenders = []
    for p in _py_files():
        for i, line in enumerate(
            p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            if _IMPORT.match(line) or "microsoft/graphrag" in line.lower():
                offenders.append(f"{p.relative_to(ENGINE_ROOT)}:{i}: {line.strip()}")
    assert not offenders, "Microsoft GraphRAG imported in source:\n" + "\n".join(
        offenders
    )
