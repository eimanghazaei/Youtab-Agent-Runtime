"""Microsoft GraphRAG is PROHIBITED across the Youtab Agent Runtime (Owner ADR).

Fails CI if Microsoft GraphRAG is reintroduced as an active dependency, import,
configuration, or installed package. Targets ONLY the Microsoft GraphRAG product
surface (``graphrag`` / ``graph-rag`` / ``graph_rag`` / ``microsoft/graphrag``);
it deliberately does NOT touch the legitimate, required capabilities — graph
databases, graph/knowledge memory, vector stores, or the engine's own per-agent
memory.
"""

from __future__ import annotations

import importlib.metadata
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


def test_no_installed_distribution_provides_graphrag():
    """Runtime (behavioural) layer: no INSTALLED distribution is graphrag.

    ``test_graphrag_is_not_importable`` only catches a package whose top-level
    import name is exactly ``graphrag``. A distribution can ship under a
    different import name than its PyPI project name, so a ``pip install
    graphrag`` (or ``graph-rag``) that exposes a differently-named module would
    slip past ``find_spec``. This scans the actually-installed distribution
    metadata for the same prohibited token the manifest scan uses, so the gate
    fails on a real installation in the running environment — independent of
    source text and manifests. Additive: it does not relax any other layer.
    """
    offenders = []
    for dist in importlib.metadata.distributions():
        # ``Name`` is the canonical project name; fall back to metadata key.
        name = (dist.metadata.get("Name") or "").strip()
        if name and _DEP_TOKEN.search(name):
            version = dist.version or "?"
            offenders.append(f"{name}=={version}")
    assert not offenders, (
        "Microsoft GraphRAG is installed as a distribution in this environment: "
        + ", ".join(sorted(set(offenders)))
    )


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
