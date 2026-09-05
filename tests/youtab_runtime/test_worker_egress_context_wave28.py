"""WAVE-28 §6.4 — egress_run_context wired into the REAL run/orchestrator path.

Chokepoint under test
---------------------
The real agent run funnels through ``agent.conversation_loop.run_conversation``
— the exact function ``run_agent:main`` and ``AIAgent.run_conversation`` forward
into, and which the kanban runtime worker reaches when it is spawned as
``youtab -p <profile> chat -q "work kanban task <id>"`` (see
``youtab_agent_cli.kanban_db._default_spawn``). That spawn exports the durable
run identity into the worker's environment:

    YOUTAB_AGENT_KANBAN_TASK        -> run_id
    YOUTAB_AGENT_TENANT             -> principal tenant
    YOUTAB_AGENT_KANBAN_CREATED_BY  -> principal user

WAVE-28 makes ``run_conversation`` a thin wrapper that enters
``youtab_runtime.egress_context.egress_run_context(run_id, Principal(...))``
around the real loop (``_run_conversation_impl``) whenever those identifiers are
present, so every tool/provider egress during the run is attributed to the run
and cleaned up on completion / error / cancellation.

What these tests exercise for real (NOT mocked)
-----------------------------------------------
* the REAL ``run_conversation`` entry point and its env-based identity
  resolution (``_egress_run_context_from_env``),
* the REAL ambient ``egress_context`` (contextvar set/reset, propagation),
* the REAL audited egress adapter (``egress_adapters.audited_requests_request``)
  + audit boundary + run journal, with redaction.

Only the ~6k-line loop body (``_run_conversation_impl``) is replaced with a
controlled probe that performs the "fake tool / stubbed provider transport"
egress attempt the task authorizes — the network transport itself
(``requests.request``) is stubbed so no live LLM provider is required.

PENDING_OWNER_ACTION (documented, not a repo blocker)
-----------------------------------------------------
The single sub-path NOT provable here is a genuine end-to-end run where the real
model loop drives a live provider HTTP call inside the ambient context. That
needs a reachable provider + credentials (see docs/security/EGRESS_EXCEPTIONS.md
and the WAVE-27 residual note in youtab_runtime/egress_context.py). The wiring,
env resolution, context propagation, attribution, redaction, and cleanup are all
proven here against the real chokepoint; only the live-provider hop is deferred.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import agent.conversation_loop as cl
from youtab_runtime import egress_adapters as adp
from youtab_runtime import egress_audit as ea
from youtab_runtime.egress_audit import EgressPolicy
from youtab_runtime.egress_context import (
    SYSTEM_RUN_ID,
    current_context,
    in_run_context,
    system_principal,
)
from youtab_runtime.run_journal import Principal, list_events

ALLOWED_HOST = "provider.internal.bench"


@pytest.fixture(autouse=True)
def _offline_deny_posture():
    """Deterministic, offline egress policy: deny everything except one host.

    Under the deny posture the classifier authorizes an exact-allowlist host
    without any DNS lookup, so the audited path is offline-definitive and the
    only permitted destination is our synthetic provider.
    """
    ea.set_policy_provider(
        lambda: EgressPolicy(
            network_deny=True, allowlist=frozenset({ALLOWED_HOST})
        )
    )
    yield
    ea.reset_policy_provider()


def _set_run_env(monkeypatch, *, run_id, tenant, user):
    """Emulate the kanban worker's spawn env (the real run-identity source)."""
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", run_id)
    monkeypatch.setenv("YOUTAB_AGENT_TENANT", tenant)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_CREATED_BY", user)


def _clear_run_env(monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_TASK", raising=False)
    monkeypatch.delenv("YOUTAB_AGENT_TENANT", raising=False)
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_CREATED_BY", raising=False)


def _install_probe(monkeypatch, probe):
    """Replace ONLY the run-loop body; the wrapper + egress_context are real."""
    monkeypatch.setattr(cl, "_run_conversation_impl", probe)


def _stub_transport(monkeypatch, status=200):
    """Stub the underlying requests transport so no real socket is opened."""

    class _Resp:
        status_code = status

    calls = {"n": 0}

    def _fake_request(method, url, **kw):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr("requests.request", _fake_request)
    return calls


# --------------------------------------------------------------------------- #
# helper resolution (the wiring's decision function, exercised directly)
# --------------------------------------------------------------------------- #
def test_helper_enters_context_when_env_complete(monkeypatch):
    _set_run_env(monkeypatch, run_id="run-h", tenant="tenantH", user="userH")
    with cl._egress_run_context_from_env():
        assert in_run_context()
        ctx = current_context()
        assert ctx.run_id == "run-h"
        assert ctx.principal == Principal("tenantH", "userH")
    assert not in_run_context()


def test_helper_is_noop_when_env_incomplete(monkeypatch):
    # Only run_id present — tenant/user missing -> no attribution context.
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", "run-partial")
    monkeypatch.delenv("YOUTAB_AGENT_TENANT", raising=False)
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_CREATED_BY", raising=False)
    with cl._egress_run_context_from_env():
        assert not in_run_context()
        assert current_context().run_id == SYSTEM_RUN_ID


# --------------------------------------------------------------------------- #
# attribution: a real run attributes egress to its run_id/principal
# --------------------------------------------------------------------------- #
def test_run_attributes_egress_to_run_principal(monkeypatch):
    _set_run_env(monkeypatch, run_id="run-A", tenant="tenantA", user="userA")
    calls = _stub_transport(monkeypatch, status=201)

    seen = {}

    def _probe(agent, user_message, *a, **k):
        ctx = current_context()
        seen["in_run"] = in_run_context()
        seen["run_id"] = ctx.run_id
        seen["principal"] = ctx.principal
        # Real egress through the audited adapter, attributed via the ambient ctx.
        resp = adp.audited_requests_request(
            "POST",
            f"https://{ALLOWED_HOST}/v1/chat",
            adapter="wave28-probe",
            enforce=True,
        )
        seen["status"] = resp.status_code
        return {"ok": True, "echo": user_message}

    _install_probe(monkeypatch, _probe)

    result = cl.run_conversation(object(), "hello-world")

    # Wrapper forwards args + return value unchanged.
    assert result == {"ok": True, "echo": "hello-world"}
    assert seen["in_run"] is True
    assert seen["run_id"] == "run-A"
    assert seen["principal"] == Principal("tenantA", "userA")
    assert seen["status"] == 201
    assert calls["n"] == 1  # the stub transport was actually reached (authorized)

    # The run journal records the egress under the run's attribution.
    events = list_events("run-A", Principal("tenantA", "userA"), category="egress")
    kinds = [e.kind for e in events]
    assert "requested" in kinds
    assert "authorized" in kinds
    assert "attempted" in kinds
    assert "succeeded" in kinds
    for e in events:
        assert e.run_id == "run-A"
        assert e.tenant == "tenantA"
        assert e.user == "userA"
        assert e.payload.get("host") == ALLOWED_HOST


# --------------------------------------------------------------------------- #
# propagation across async tasks spawned within the run
# --------------------------------------------------------------------------- #
def test_context_propagates_to_async_tasks(monkeypatch):
    _set_run_env(monkeypatch, run_id="run-async", tenant="tenantX", user="userX")
    _stub_transport(monkeypatch)

    seen = {}

    def _probe(agent, user_message, *a, **k):
        async def _child():
            # A coroutine scheduled as a task within the run context must see the
            # same ambient egress context (contextvars copy into created tasks).
            return current_context()

        async def _driver():
            task = asyncio.create_task(_child())
            child_ctx = await task
            # A concurrently-egressing async tool also carries the attribution.
            await adp.audited_aiohttp_request(
                _FakeSession(),
                "GET",
                f"https://{ALLOWED_HOST}/v1/ping",
                adapter="wave28-async",
                enforce=True,
            )
            return child_ctx

        seen["child_ctx"] = asyncio.run(_driver())
        return {}

    class _FakeSession:
        async def request(self, method, url, **kw):
            class _R:
                status = 200

            return _R()

    _install_probe(monkeypatch, _probe)
    cl.run_conversation(object(), "async")

    assert seen["child_ctx"].run_id == "run-async"
    assert seen["child_ctx"].principal == Principal("tenantX", "userX")

    # The async egress landed in the journal under the run's attribution too.
    events = list_events("run-async", Principal("tenantX", "userX"), category="egress")
    assert events
    assert all(e.run_id == "run-async" for e in events)


# --------------------------------------------------------------------------- #
# no stale leakage between sequential runs
# --------------------------------------------------------------------------- #
def test_no_leakage_between_sequential_runs(monkeypatch):
    seen = []

    def _probe(agent, user_message, *a, **k):
        seen.append(current_context())
        return {}

    _install_probe(monkeypatch, _probe)

    _set_run_env(monkeypatch, run_id="run-1", tenant="t1", user="u1")
    cl.run_conversation(object(), "a")
    # Context exited cleanly after run 1.
    assert not in_run_context()
    assert current_context().run_id == SYSTEM_RUN_ID

    _set_run_env(monkeypatch, run_id="run-2", tenant="t2", user="u2")
    cl.run_conversation(object(), "b")
    assert not in_run_context()

    assert seen[0].run_id == "run-1"
    assert seen[0].principal == Principal("t1", "u1")
    assert seen[1].run_id == "run-2"
    assert seen[1].principal == Principal("t2", "u2")
    # After both runs, egress falls back to the SYSTEM sentinel.
    assert current_context().principal == system_principal()


# --------------------------------------------------------------------------- #
# cleanup on exception and on cancellation
# --------------------------------------------------------------------------- #
def test_context_exits_on_exception(monkeypatch):
    _set_run_env(monkeypatch, run_id="run-err", tenant="te", user="ue")

    def _probe(agent, user_message, *a, **k):
        assert in_run_context()
        assert current_context().run_id == "run-err"
        raise RuntimeError("boom")

    _install_probe(monkeypatch, _probe)

    with pytest.raises(RuntimeError, match="boom"):
        cl.run_conversation(object(), "x")

    # Even though the run raised, the ambient context was restored.
    assert not in_run_context()
    assert current_context().run_id == SYSTEM_RUN_ID


def test_context_exits_on_cancellation(monkeypatch):
    _set_run_env(monkeypatch, run_id="run-cancel", tenant="tc", user="uc")

    def _probe(agent, user_message, *a, **k):
        assert in_run_context()
        raise asyncio.CancelledError()

    _install_probe(monkeypatch, _probe)

    with pytest.raises(asyncio.CancelledError):
        cl.run_conversation(object(), "x")

    assert not in_run_context()
    assert current_context().run_id == SYSTEM_RUN_ID


# --------------------------------------------------------------------------- #
# fail-closed-to-SYSTEM when the run context is absent
# --------------------------------------------------------------------------- #
def test_absent_run_env_falls_back_to_system(monkeypatch):
    _clear_run_env(monkeypatch)
    _stub_transport(monkeypatch)

    seen = {}

    def _probe(agent, user_message, *a, **k):
        seen["in_run"] = in_run_context()
        seen["ctx"] = current_context()
        # An out-of-run egress attributes to SYSTEM (design: no deny, sentinel).
        adp.audited_requests_request(
            "GET",
            f"https://{ALLOWED_HOST}/v1/ping",
            adapter="wave28-system",
            enforce=True,
        )
        return {}

    _install_probe(monkeypatch, _probe)
    cl.run_conversation(object(), "x")

    assert seen["in_run"] is False
    assert seen["ctx"].run_id == SYSTEM_RUN_ID
    assert seen["ctx"].principal == system_principal()

    # The egress was journaled under the SYSTEM principal, not a run principal.
    sys_events = list_events(
        SYSTEM_RUN_ID, system_principal(), category="egress"
    )
    assert sys_events
    assert all(e.run_id == SYSTEM_RUN_ID for e in sys_events)


# --------------------------------------------------------------------------- #
# redaction: journal records carry no secret material
# --------------------------------------------------------------------------- #
def test_journal_records_are_redacted(monkeypatch):
    _set_run_env(monkeypatch, run_id="run-redact", tenant="tr", user="ur")
    _stub_transport(monkeypatch, status=200)

    secret = "SUPERSECRET-TOKEN-abc123XYZ"

    def _probe(agent, user_message, *a, **k):
        # Secret in BOTH the query string and an Authorization header.
        adp.audited_requests_request(
            "POST",
            f"https://{ALLOWED_HOST}/v1/messages?api_key={secret}&x=1",
            adapter="wave28-redact",
            enforce=True,
            headers={"Authorization": f"Bearer {secret}"},
            json={"prompt": "hi", "token": secret},
        )
        return {}

    _install_probe(monkeypatch, _probe)
    cl.run_conversation(object(), "x")

    events = list_events("run-redact", Principal("tr", "ur"), category="egress")
    assert events

    blob = json.dumps([e.payload for e in events])
    assert secret not in blob
    assert "api_key" not in blob  # query never enters the journal
    for e in events:
        ps = e.payload.get("path_shape")
        if ps:
            assert secret not in ps
            assert "api_key" not in ps
        # Only destination metadata is journaled.
        assert e.payload.get("host") == ALLOWED_HOST
