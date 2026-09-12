"""WAVE-30H Gate-2 regression: a managed worker can reach terminal completion.

Two defects jointly prevented EVERY managed run from completing (no managed
kanban run had ever been authorized to call its own completion tool):

  Phase A — the real CLI worker path (``YoutabCLI.chat`` → ``run_conversation``,
    taken by the non-``-Q`` dispatcher-spawned kanban worker AND the pre-warmed
    pooled worker, both of which run ``youtab … --cli chat -q "work kanban task
    <id>"``) never called ``establish_managed_admission``. The agent reached a
    tool with no ``_admitted_command`` and the authority gate (correctly)
    fail-closed on every call. The establish call had only ever been wired into
    the ``-Q``/``--quiet`` machine-readable branch of ``main()``.

  Phase B — the ingress-frozen capability manifest EXCLUDED the kanban lifecycle
    completion tools (``kanban_complete``/``kanban_block``/``kanban_heartbeat``),
    because their availability gate (``_check_kanban_mode``) is keyed on the
    worker-only ``YOUTAB_AGENT_KANBAN_TASK`` env, which is absent in the ingress
    process. The worker re-admits that frozen manifest, so the gate denied its
    own completion tool even once admission was established.

These tests prove both fixes hermetically (no model, no network). The live
end-to-end completion through ECO/Ollama is reported separately.

Security invariants exercised here (Owner Gate-2 constraints):
  * the completion tool is authorized ONLY if explicitly present in the frozen
    Simorgh-authorized capability manifest (constraint 8);
  * the lifecycle tools are NEVER globally authorized — a grant that does not
    authorize the ``kanban`` toolset (or ``"*"``) still excludes them
    (constraint 7), even with the execution-context hint;
  * deferring the worker-only execution-context gate at freeze time does not
    loosen ``"*"`` or use ``dynamic_inclusion`` (constraint 9): inclusion stays
    intersected with the grant envelope.
"""
from __future__ import annotations

from pathlib import Path

import model_tools  # noqa: F401 — populate the full tool registry (realistic ingress)

from tools.registry import registry
from youtab_runtime.policy import AuthorityBoundary, EffectClass, ToolIntent

from youtab_agent_cli import capability_manifest as cm
from tests.youtab_runtime.helpers import keypair, signed_envelope

KANBAN_CTX = frozenset({"kanban"})
_FORBIDDEN = AuthorityBoundary.FORBIDDEN_TOOLSETS


def _names(allowed, *, context=frozenset()):
    pairs = registry.capability_manifest_pairs(
        set(allowed), exclude_toolsets=_FORBIDDEN, context_available_toolsets=context
    )
    return [name for name, _ in pairs]


# ── Phase B: the frozen manifest represents the completion tool correctly ─────


def test_completion_tool_present_under_star_with_execution_context():
    names = _names({"*"}, context=KANBAN_CTX)
    assert "kanban_complete" in names
    assert "kanban_block" in names
    assert "kanban_heartbeat" in names


def test_completion_tool_present_under_explicit_kanban_grant():
    names = _names({"kanban"}, context=KANBAN_CTX)
    assert "kanban_complete" in names


def test_completion_tool_absent_without_execution_context_hint():
    # Regression against the exact Phase-B defect: without the execution-context
    # hint the worker-only availability gate drops the completion tool even under
    # the "*" full envelope — which is what blocked every managed run.
    assert "kanban_complete" not in _names({"*"})


def test_completion_tool_never_globally_authorized():
    # Constraint 7: a grant that does not authorize the kanban toolset (or "*")
    # must NOT receive the lifecycle tools, even WITH the execution-context hint.
    for grant in (["file"], ["browser"], ["web"], ["memory"]):
        assert "kanban_complete" not in _names(set(grant), context=KANBAN_CTX), grant


def test_execution_context_does_not_sweep_in_forbidden_toolsets():
    # Authority-bearing toolsets stay excluded regardless of the context hint.
    names = _names({"*"}, context=KANBAN_CTX | _FORBIDDEN)
    for forbidden in _FORBIDDEN:
        assert not any(
            registry.get_toolset_for_tool(n) == forbidden for n in names
        ), forbidden


# ── build_ingress_binding threads the execution context, grant-gated ──────────


def test_build_ingress_binding_authorizes_completion_when_grant_allows():
    private, _ = keypair()
    env = signed_envelope(private, allowed_toolsets=("*",))
    binding = cm.build_ingress_binding(
        env, registry=registry, context_available_toolsets=KANBAN_CTX
    )
    assert binding.authorizes("kanban_complete") is True
    # manifest integrity: the hash is bound to the contents.
    assert binding.manifest_hash


def test_build_ingress_binding_excludes_completion_for_unauthorizing_grant():
    private, _ = keypair()
    env = signed_envelope(private, allowed_toolsets=("file",))
    binding = cm.build_ingress_binding(
        env, registry=registry, context_available_toolsets=KANBAN_CTX
    )
    assert binding.authorizes("kanban_complete") is False


# ── decide_tool: completion tool executes only if in the frozen manifest ──────


def _admit(binding, *, allowed=("*",)):
    private, public = keypair()
    env = signed_envelope(private, allowed_toolsets=allowed)
    boundary = AuthorityBoundary()
    return boundary, boundary.admit(env, public, capability_binding=binding)


def test_gate_authorizes_completion_tool_when_in_frozen_manifest():
    # The exact binding the fixed ingress freeze now produces (kanban_complete
    # included) → the worker is authorized to complete its task.
    private, _ = keypair()
    env = signed_envelope(private, allowed_toolsets=("*",))
    binding = cm.build_ingress_binding(
        env, registry=registry, context_available_toolsets=KANBAN_CTX
    )
    boundary, admitted = _admit(binding)
    decision = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name="kanban_complete",
            toolset="kanban",
            effect_class=EffectClass.NONE,
            arguments={"result": "4"},
        ),
    )
    assert decision.execute_in_runtime is True


def test_gate_denies_completion_tool_absent_from_frozen_manifest():
    # The pre-fix manifest (no execution-context hint) excludes kanban_complete →
    # the gate fail-closes, which is the defect's observable symptom.
    private, _ = keypair()
    env = signed_envelope(private, allowed_toolsets=("*",))
    binding = cm.build_ingress_binding(env, registry=registry)  # no context hint
    boundary, admitted = _admit(binding)
    decision = boundary.decide_tool(
        admitted,
        ToolIntent(
            tool_name="kanban_complete",
            toolset="kanban",
            effect_class=EffectClass.NONE,
            arguments={"result": "4"},
        ),
    )
    assert decision.execute_in_runtime is False
    assert "capability manifest" in decision.reason


# ── Phase A: the real CLI worker path wires managed admission ─────────────────


def test_chat_path_establishes_managed_admission():
    """``YoutabCLI.chat`` (the path the real non-``-Q`` kanban worker AND the
    pooled worker take) must establish the admitted context before running the
    conversation. Guards against regressing the Phase-A wiring. (Behavioural proof
    is the live E2E + the subprocess integration test.)"""
    src = Path(__file__).resolve().parents[2] / "cli.py"
    text = src.read_text(encoding="utf-8")
    # The establish call must be present inside the chat() turn handler, before
    # the human-facing single-query path delegates to it. WAVE-30H Batch4 #1 threads
    # the verified pre-admission snapshot into the call (no reload), so match the
    # call head rather than the exact arity.
    assert "establish_managed_admission(\n" in text or "establish_managed_admission(agent" in text
    # And the ingress freeze must thread the execution-context toolsets.
    runtime_src = (src.parent / "youtab_agent_cli" / "web_routers" / "runtime.py").read_text(
        encoding="utf-8"
    )
    assert "_WORKER_EXECUTION_CONTEXT_TOOLSETS" in runtime_src
    assert "context_available_toolsets=" in runtime_src
