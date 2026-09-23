"""ADR-0005 — the real filesystem / shell / code-execution tools must be
classified so ``AuthorityBoundary.decide_tool`` refuses to self-authorize a
managed filesystem or shell mutation (fail closed).

Before this fix every fs-touching tool inherited the registry default
``side_effect_class="none"``, so ``decide_tool`` authorized ``write_file`` /
``patch`` / ``terminal`` / ``execute_code`` / ``process`` as ``NONE`` and a
managed run could mutate the filesystem or run arbitrary shell with **no** grant,
signed authorization, workspace binding, idempotency, lease or receipt — the
bypass surfaced by the managed-run fs inventory. This test locks the
classification and the resulting fail-closed refusal, and is inert in
local-standalone (the authority gate is only consulted in managed mode).
"""

from __future__ import annotations

import pytest

from youtab_runtime.policy import AuthorityBoundary, EffectClass, ToolIntent

from .helpers import keypair, signed_envelope

# Import the tool modules so their ``registry.register(...)`` import-time side
# effects run and the real entries exist to inspect.
import tools.file_tools  # noqa: F401,E402
import tools.terminal_tool  # noqa: F401,E402
import tools.code_execution_tool  # noqa: F401,E402
import tools.process_registry  # noqa: F401,E402
from tools.registry import registry  # noqa: E402

# The authoritative classification for the real tools (regression lock).
_EXPECTED = {
    "read_file": "read",
    "search_files": "read",
    "write_file": "write",
    "patch": "write",
    "terminal": "process",
    "execute_code": "process",
    "process": "process",
}

# Effectful tools must never be self-authorized by the runtime in managed mode.
_EFFECTFUL = ("write_file", "patch", "terminal", "execute_code", "process")
# Reads are admissible by the task contract (their grant-scoping is enforced
# separately by the folder grant), but must NOT be classified as an external
# effect (which would wrongly block legitimate managed reads).
_READS = ("read_file", "search_files")


@pytest.mark.parametrize("name,expected", sorted(_EXPECTED.items()))
def test_real_fs_tool_side_effect_class(name: str, expected: str) -> None:
    entry = registry.get_entry(name)
    assert entry is not None, f"{name} is not registered"
    assert entry.side_effect_class == expected, (
        f"{name} classified {entry.side_effect_class!r}, expected {expected!r} "
        "(a default 'none' reopens the managed-run fs bypass)"
    )


def _admit() -> AuthorityBoundary:
    private, public = keypair()
    envelope = signed_envelope(private)
    boundary = AuthorityBoundary()
    return boundary, boundary.admit(envelope, public)


@pytest.mark.parametrize("name", _EFFECTFUL)
def test_effectful_fs_tool_is_refused_in_managed_mode(name: str) -> None:
    boundary, admitted = _admit()
    entry = registry.get_entry(name)
    decision = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name=name,
            toolset="safe",
            effect_class=EffectClass(entry.side_effect_class),
            arguments={},
        ),
    )
    # Never executes in-runtime: the runtime cannot self-authorize an external
    # effect — it either emits an EffectProposal or refuses outright.
    assert decision.execute_in_runtime is False


@pytest.mark.parametrize("name", _READS)
def test_read_fs_tool_is_admitted_in_managed_mode(name: str) -> None:
    boundary, admitted = _admit()
    entry = registry.get_entry(name)
    decision = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name=name,
            toolset="safe",
            effect_class=EffectClass(entry.side_effect_class),
            arguments={},
        ),
    )
    assert decision.execute_in_runtime is True
    assert decision.proposal is None
