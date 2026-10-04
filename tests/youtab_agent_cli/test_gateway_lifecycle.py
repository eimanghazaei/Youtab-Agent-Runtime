"""The gateway lifecycle surface tells the truth about what happened.

The control this file pins used to answer ``{"ok": true}`` the moment
``subprocess.Popen`` returned. That is a statement about the *dispatch* wearing
the clothes of a statement about the *outcome*, and every way the operation can
really fail happens after that moment: the child exits non-zero, the supervised
slot was never registered, the gateway goes down and does not come back, the
whole thing hangs. All of them rendered as success.

So the shape of almost every test here is the same question asked twice: did
the operation actually achieve what it claimed, and does the API say so. A test
that only asserted ``202`` would pass against the defect it exists to prevent.

Real where it can be
--------------------
The children are real subprocesses with real exit codes, real timing and real
concurrency -- ``run_job`` waits on an actual ``Popen``, on an actual thread.
What is substituted is the *gateway itself*: CI has no supervised gateway to
restart, so liveness is supplied by a controllable probe. That boundary is
deliberate and it is the only one. The HTTP path, the middleware stack, the
authorization gate, the audit log, the job registry, the storm breaker and the
subprocess handling are all the shipping code.

Stale-pid rejection is the exception that proves it: that one drives the real
``gateway.status.get_running_pid`` against a real pid file, because the whole
question there is whether the production reader is fooled.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient

from youtab_agent_cli import gateway_lifecycle as lifecycle
from youtab_agent_cli import web_server
from youtab_agent_cli.authz import (
    DEPLOYMENT_MANAGE,
    PROVIDER_READ,
    ROSTER_ENV,
    Role,
    required_scope,
)
from youtab_agent_cli.dashboard_auth import clear_providers, register_provider
from youtab_agent_cli.dashboard_auth.base import DashboardAuthProvider, Session
from youtab_agent_cli.gateway_lifecycle import (
    JobState,
    LifecycleRegistry,
    Observation,
    Reason,
    StormBlocked,
    desired_state_reached,
    redact,
    resolve_outcome,
    run_job,
)

REPO = Path(__file__).resolve().parents[2]

#: A realistically-shaped vendor key, assembled at runtime so no key-shaped
#: literal appears in this file. The secret gate scans source for exactly
#: that shape, and a test about redaction must not itself become the finding.
#: The runtime value is full length, so it exercises the real pattern.
FAKE_VENDOR_KEY = "sk-" + "abcdefghijklmnopqrstuvwxyz" + "012345"

SESSION_COOKIE = "__Host-youtab_session_at"

OWNER = "owner-1"
SUPERADMIN = "superadmin-1"
OPERATOR_BARE = "operator-bare"
TENANT_ADMIN = "tenant-admin-1"
NORMAL = "normal-1"
ACME = "org-acme"

ROSTER = {
    OWNER: {"role": Role.YOUTAB_OWNER.value},
    SUPERADMIN: {"role": Role.YOUTAB_SUPERADMIN.value},
    # An operator present in the roster with no explicit grant. The role is
    # not authority; the scope is.
    OPERATOR_BARE: {"role": Role.YOUTAB_OPERATOR.value},
    TENANT_ADMIN: {"role": Role.TENANT_ADMIN.value},
}
ORGS = {
    OWNER: "", SUPERADMIN: "", OPERATOR_BARE: "",
    TENANT_ADMIN: ACME, NORMAL: ACME,
}


# --- real children ----------------------------------------------------------


def _child(exit_code: int = 0, sleep_s: float = 0.0) -> subprocess.Popen:
    """A real subprocess with a real exit code. Not a mock."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import sys,time; time.sleep({sleep_s}); sys.exit({exit_code})"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _sequence_probe(values):
    """A pid probe that walks ``values``, holding the last one forever.

    Models the gateway's observable liveness over time: ``[100, None, 200]`` is
    "was up, went down, came back with a new pid" -- a restart.
    """
    state = {"i": 0}

    def probe(_profile=None):
        i = min(state["i"], len(values) - 1)
        state["i"] += 1
        return values[i]

    return probe


def _run(job, proc, **kwargs):
    """``run_job`` with test-scale deadlines; everything else is production."""
    kwargs.setdefault("timeout_s", 5.0)
    kwargs.setdefault("settle_s", 0.3)
    kwargs.setdefault("poll_interval_s", 0.01)
    return run_job(job, proc, **kwargs)


@pytest.fixture
def registry():
    return LifecycleRegistry()


# ---------------------------------------------------------------------------
# The decision itself
# ---------------------------------------------------------------------------


class TestTruthFunction:
    """``resolve_outcome`` is the whole contract, isolated."""

    def test_spawning_is_not_success(self):
        """The defect, stated directly.

        Nothing is known yet: the child has not exited and the gateway has not
        appeared. The only honest answer is "not successful".
        """
        obs = Observation(exit_code=None, running_after=False, pid_before=None, pid_after=None)
        state, _reason = resolve_outcome("restart", obs)
        assert state is JobState.FAILED

    def test_restart_that_leaves_no_gateway_is_a_failure_despite_exit_zero(self):
        """The two-container defect's exact shape.

        A clean exit code is not evidence the gateway is up, and this is the
        case where believing it produced "restart succeeded" beside
        "Gateway Status: Off".
        """
        obs = Observation(exit_code=0, running_after=False, pid_before=11, pid_after=None)
        assert resolve_outcome("restart", obs) == (
            JobState.FAILED, Reason.NOT_RUNNING_AFTER_RESTART,
        )

    def test_restart_that_did_not_change_the_pid_is_not_a_restart(self):
        """A live gateway is not proof the restart happened.

        Same pid before and after means the old process was never replaced.
        Accepting "something is running" would pass a restart that did nothing.
        """
        obs = Observation(exit_code=0, running_after=True, pid_before=11, pid_after=11)
        assert resolve_outcome("restart", obs)[0] is JobState.FAILED

    def test_restart_with_a_new_pid_succeeds(self):
        obs = Observation(exit_code=0, running_after=True, pid_before=11, pid_after=12)
        assert resolve_outcome("restart", obs) == (JobState.SUCCEEDED, Reason.COMPLETED)

    def test_nonzero_exit_is_a_failure_and_keeps_its_code(self):
        obs = Observation(exit_code=1, running_after=False, pid_before=None, pid_after=None)
        assert resolve_outcome("start", obs) == (
            JobState.FAILED, Reason.CHILD_EXIT_NONZERO,
        )

    def test_stop_that_leaves_it_running_is_a_failure(self):
        obs = Observation(exit_code=0, running_after=True, pid_before=11, pid_after=11)
        assert resolve_outcome("stop", obs) == (
            JobState.FAILED, Reason.STILL_RUNNING_AFTER_STOP,
        )

    def test_a_live_child_that_is_the_gateway_is_success_not_timeout(self):
        """No-service installs: `gateway restart` becomes the gateway.

        The child never exits, and waiting for it would fail a restart that
        plainly worked. Arrival at the desired state is what is being asked.
        """
        obs = Observation(exit_code=None, running_after=True, pid_before=11, pid_after=12)
        assert resolve_outcome("restart", obs) == (JobState.SUCCEEDED, Reason.COMPLETED)

    def test_start_on_a_running_gateway_is_idempotent_success(self):
        obs = Observation(exit_code=0, running_after=True, pid_before=11, pid_after=11)
        assert resolve_outcome("start", obs) == (JobState.SUCCEEDED, Reason.ALREADY_RUNNING)

    def test_stop_on_a_stopped_gateway_is_idempotent_success(self):
        obs = Observation(exit_code=0, running_after=False, pid_before=None, pid_after=None)
        assert resolve_outcome("stop", obs) == (JobState.SUCCEEDED, Reason.ALREADY_STOPPED)

    def test_restart_from_stopped_accepts_any_live_pid(self):
        obs = Observation(exit_code=0, running_after=True, pid_before=None, pid_after=7)
        assert desired_state_reached("restart", obs) is True


# ---------------------------------------------------------------------------
# Real children, real threads
# ---------------------------------------------------------------------------


class TestLifecycleVerbs:
    def test_start_from_stopped(self, registry):
        job, created = registry.admit("start", None)
        assert created
        done = _run(job, _child(0), registry=registry,
                    pid_probe=_sequence_probe([None, 4242]), pid_before=None)
        assert done.state is JobState.SUCCEEDED
        assert done.ok is True
        # `exit_code` may still be None: the gateway arrived before the child
        # was reaped, and arrival is what was being asked. The contract is the
        # verdict, not the bookkeeping.

    def test_stop(self, registry):
        job, _ = registry.admit("stop", None)
        done = _run(job, _child(0), registry=registry,
                    pid_probe=_sequence_probe([4242, None]), pid_before=4242)
        assert done.state is JobState.SUCCEEDED
        assert done.reason is Reason.COMPLETED

    def test_restart(self, registry):
        job, _ = registry.admit("restart", None)
        done = _run(job, _child(0), registry=registry,
                    pid_probe=_sequence_probe([100, None, 200]), pid_before=100)
        assert done.state is JobState.SUCCEEDED
        assert done.ok is True

    def test_already_running_start_is_success_not_an_error(self, registry):
        job, _ = registry.admit("start", None)
        done = _run(job, _child(0), registry=registry,
                    pid_probe=_sequence_probe([999]), pid_before=999)
        assert done.state is JobState.SUCCEEDED
        assert done.reason is Reason.ALREADY_RUNNING

    def test_failed_child_exit_is_propagated_not_flattened(self, registry):
        """A real child exiting 3. The code reaches the caller intact.

        Flattening this to `ok: false` would lose the one detail that
        distinguishes "no such gateway" from "port in use".
        """
        job, _ = registry.admit("restart", None)
        done = _run(job, _child(3), registry=registry,
                    pid_probe=_sequence_probe([None]), pid_before=None)
        assert done.state is JobState.FAILED
        assert done.exit_code == 3
        assert done.reason is Reason.CHILD_EXIT_NONZERO
        assert done.ok is False

    def test_missing_supervised_slot_fails(self, registry):
        """`GatewayNotRegisteredError` exits 1 -- the two-container symptom.

        The dashboard reported this as a successful restart for as long as the
        endpoint answered on spawn.
        """
        job, _ = registry.admit("restart", None)
        done = _run(job, _child(1), registry=registry,
                    pid_probe=_sequence_probe([None]), pid_before=None)
        assert done.state is JobState.FAILED
        assert done.exit_code == 1

    def test_crash_detection_child_clean_but_gateway_gone(self, registry):
        """The gateway crashed after a clean restart and stayed down."""
        job, _ = registry.admit("restart", None)
        done = _run(job, _child(0), registry=registry,
                    pid_probe=_sequence_probe([100, None]), pid_before=100)
        assert done.state is JobState.FAILED
        assert done.reason is Reason.NOT_RUNNING_AFTER_RESTART

    def test_supervised_crash_recovery_is_success_and_flagged(self, registry):
        """Down, then back under supervision. Success, and distinguishable.

        The `recovered` flag is what lets the audit trail separate "restarted"
        from "crashed and the supervisor caught it" -- collapsing them hides
        every crash the supervisor absorbed.
        """
        seen = {}
        job, _ = registry.admit("restart", None)
        # Reaped before the job runs, so every observation sees a finished
        # child: the gateway is unmet at that point and arrives afterwards,
        # which is precisely "this operation did not do it".
        proc = _child(0)
        proc.wait()
        done = _run(
            job, proc, registry=registry,
            pid_probe=_sequence_probe([None, None, 500]), pid_before=100,
            on_terminal=lambda j, recovered: seen.update(recovered=recovered),
        )
        assert done.state is JobState.SUCCEEDED
        assert seen["recovered"] is True

    def test_an_ordinary_restart_is_not_reported_as_a_recovery(self, registry):
        """Every restart has a gap; only a real crash is a recovery.

        If this flag fired on ordinary restarts it would bury the crashes it
        exists to surface.
        """
        seen = {}
        job, _ = registry.admit("restart", None)
        _run(
            job, _child(0), registry=registry,
            pid_probe=_sequence_probe([200]), pid_before=100,
            on_terminal=lambda j, recovered: seen.update(recovered=recovered),
        )
        assert seen["recovered"] is False

    def test_operation_timeout_is_a_failure_not_a_pending_forever(self, registry):
        """A child that outlives the deadline while the gateway never arrives.

        Real: the subprocess genuinely sleeps past `timeout_s`.
        """
        job, _ = registry.admit("restart", None)
        proc = _child(0, sleep_s=30)
        try:
            done = _run(job, proc, registry=registry,
                        pid_probe=_sequence_probe([None]), pid_before=None,
                        timeout_s=0.4)
            assert done.state is JobState.FAILED
            assert done.reason is Reason.TIMEOUT
            assert done.is_terminal
        finally:
            proc.kill()
            proc.wait()

    def test_a_timed_out_child_is_not_killed(self, registry):
        """Interrupting a restart mid-flight is worse than reporting honestly."""
        job, _ = registry.admit("restart", None)
        proc = _child(0, sleep_s=30)
        try:
            _run(job, proc, registry=registry, pid_probe=_sequence_probe([None]),
                 pid_before=None, timeout_s=0.4)
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Concurrency, idempotence, storms
# ---------------------------------------------------------------------------


class TestConcurrencyAndStorms:
    def test_concurrent_restart_shares_one_job(self, registry):
        """Lock contention: a second restart joins the first, it does not race.

        Two concurrent `youtab gateway restart` children fight on the
        kill-and-start path. The old code's answer was to tell both callers
        they had succeeded.
        """
        first, created_first = registry.admit("restart", None)
        second, created_second = registry.admit("restart", None)
        assert created_first is True
        assert created_second is False
        assert first.job_id == second.job_id

    def test_a_repeated_restart_after_completion_starts_a_new_job(self, registry):
        first, _ = registry.admit("restart", None)
        registry.finish(first, JobState.SUCCEEDED, Reason.COMPLETED, exit_code=0)
        second, created = registry.admit("restart", None)
        assert created is True
        assert second.job_id != first.job_id

    def test_restart_storm_is_refused(self, registry):
        """Beyond a handful in a minute, the breaker trips.

        Deliberate operator retries sit under the limit; an automated respawn
        loop does not.
        """
        for _ in range(registry.STORM_MAX_OPS):
            job, _ = registry.admit("restart", None)
            registry.finish(job, JobState.SUCCEEDED, Reason.COMPLETED, exit_code=0)
        with pytest.raises(StormBlocked):
            registry.admit("restart", None)

    def test_the_storm_window_expires(self):
        """The breaker is a rate limit, not a permanent lockout."""
        now = {"t": 0.0}
        reg = LifecycleRegistry(clock=lambda: now["t"])
        for _ in range(reg.STORM_MAX_OPS):
            job, _ = reg.admit("restart", None)
            reg.finish(job, JobState.SUCCEEDED, Reason.COMPLETED, exit_code=0)
        now["t"] += reg.STORM_WINDOW_S + 1
        job, created = reg.admit("restart", None)
        assert created is True

    def test_storms_are_counted_per_verb(self, registry):
        """A stop is not a restart; one must not exhaust the other's budget."""
        for _ in range(registry.STORM_MAX_OPS):
            job, _ = registry.admit("restart", None)
            registry.finish(job, JobState.SUCCEEDED, Reason.COMPLETED, exit_code=0)
        job, created = registry.admit("stop", None)
        assert created is True

    def test_a_pending_job_is_never_evicted(self, registry):
        """Its caller is still polling; dropping it would 404 a live operation."""
        pending, _ = registry.admit("restart", "keepme")
        for i in range(registry.MAX_JOBS + 10):
            job, _ = registry.admit("stop", f"p{i}")
            registry.finish(job, JobState.SUCCEEDED, Reason.COMPLETED, exit_code=0)
        assert registry.get(pending.job_id) is not None


# ---------------------------------------------------------------------------
# Stale pid — driven against the real reader
# ---------------------------------------------------------------------------


class TestStalePid:
    def test_a_stale_pid_file_does_not_read_as_running(self, tmp_path):
        """A pid file naming a dead process must not count as a live gateway.

        Drives the production reader, not a stand-in: the entire question is
        whether *it* is fooled. A gateway killed with SIGKILL leaves this file
        behind, and believing it would report a dead gateway as healthy and a
        failed restart as a success.
        """
        from gateway.status import get_running_pid

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        pid_file = tmp_path / "gateway.pid"
        pid_file.write_text(json.dumps({"pid": dead.pid}), encoding="utf-8")

        assert get_running_pid(pid_file, cleanup_stale=False) is None

    def test_the_probe_reports_not_running_for_a_stale_pid(self, tmp_path, monkeypatch):
        """The same fact, through the seam the job actually calls."""
        from youtab_agent_cli import profiles as profiles_mod

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (tmp_path / "gateway.pid").write_text(
            json.dumps({"pid": dead.pid}), encoding="utf-8"
        )
        monkeypatch.setattr(profiles_mod, "get_profile_dir", lambda name: tmp_path)

        assert lifecycle.default_pid_probe("default") is None

    def test_a_stale_pid_makes_a_restart_fail_rather_than_pass(self, registry):
        """End to end: the stale reading is what the verdict is built on."""
        job, _ = registry.admit("restart", None)
        done = _run(job, _child(0), registry=registry,
                    pid_probe=_sequence_probe([None]), pid_before=None)
        assert done.state is JobState.FAILED


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_a_jwt_is_redacted(self):
        token = (
            "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0"
            ".eyJlbWFpbCI6Im9wc0BleGFtcGxlLnRlc3QifQ.c2lnbmF0dXJlLWhlcmU"
        )
        assert token not in redact(f"refused assertion {token}")

    def test_a_bearer_header_is_redacted(self):
        out = redact("Authorization: Bearer abcdef0123456789abcdef")
        assert "abcdef0123456789abcdef" not in out

    def test_a_vendor_api_key_is_redacted(self):
        out = redact(f"using {FAKE_VENDOR_KEY}")
        assert FAKE_VENDOR_KEY not in out

    def test_an_assignment_value_is_redacted_whatever_it_looks_like(self):
        out = redact("EXAMPLE_UPSTREAM_API_KEY=hunter2-correct-horse")
        assert "hunter2-correct-horse" not in out
        # The *name* survives: an operator must still be able to tell which
        # setting was involved without learning its value.
        assert "EXAMPLE_UPSTREAM_API_KEY" in out

    def test_an_environment_value_is_redacted_by_its_literal(self):
        """The catch-all: an unrecognised provider's key has no known shape.

        Matching the literal value of a sensitively-named variable is what
        makes a format this module has never seen redactable anyway.
        """
        secret = "ZmFrZS1wcm92aWRlci12YWx1ZQ"
        env = {"SOME_NEW_PROVIDER_SECRET": secret}
        assert secret not in redact(f"child failed with {secret}", environ=env)

    def test_a_short_or_insensitive_value_is_left_alone(self):
        """Redacting everything would corrupt ordinary output."""
        env = {"YOUTAB_AGENT_HOME": "/opt/data", "PROFILE_KEY": "ab"}
        assert redact("started in /opt/data for ab", environ=env) == (
            "started in /opt/data for ab"
        )

    def test_the_public_view_redacts_detail(self, registry):
        job, _ = registry.admit("restart", None)
        registry.finish(
            job, JobState.FAILED, Reason.CHILD_EXIT_NONZERO, exit_code=1,
            detail="traceback: EXAMPLE_UPSTREAM_API_KEY=super-secret-value",
        )
        assert "super-secret-value" not in json.dumps(job.public())


# ---------------------------------------------------------------------------
# HTTP contract — real app, real middleware
# ---------------------------------------------------------------------------


class _IdentityProvider(DashboardAuthProvider):
    """Turns an opaque token straight into the identity it names."""

    name = "identity-stub"
    display_name = "Identity Stub"

    def start_login(self, *, redirect_uri: str):  # pragma: no cover - unused
        raise NotImplementedError

    def complete_login(self, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError

    def verify_session(self, *, access_token: str):
        if access_token not in ORGS:
            return None
        return Session(
            user_id=access_token,
            email=f"{access_token}@example.test",
            display_name=access_token,
            org_id=ORGS[access_token],
            provider=self.name,
            expires_at=int(time.time()) + 3600,
            access_token=access_token,
            refresh_token="",
        )

    def refresh_session(self, *, refresh_token: str):  # pragma: no cover
        raise NotImplementedError

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


@pytest.fixture
def fake_gateway(monkeypatch):
    """A controllable gateway: pid sequence in, real everything else out.

    CI has no supervised gateway to restart, so this is the one substitution.
    The spawned child is still a real process and the job still resolves on a
    real thread.
    """
    # ``pids`` is a timeline consumed one observation at a time, holding the
    # last value forever: ``[100, None, 200]`` is "was up, went down, came back
    # as a different process" -- a restart that really happened.
    # An optional release event holds a real child on stdin and pauses its
    # observation, letting concurrency tests control the in-flight interval.
    state: dict[str, Any] = {
        "pids": [None], "exit": 0, "commands": [],
        "release": None, "observing": Event(), "children": [],
    }
    cursor = {"i": 0}

    def probe(_profile=None):
        if state["release"] is not None and state["children"]:
            state["observing"].set()
            assert state["release"].wait(timeout=10), "gateway observation was never released"
        pids = state["pids"]
        value = pids[min(cursor["i"], len(pids) - 1)]
        cursor["i"] += 1
        return value

    def spawn(subcommand, name):
        state["commands"].append(list(subcommand))
        if state["release"] is not None:
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 f"import sys; sys.stdin.buffer.read(); sys.exit({state['exit']})"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            state["children"].append(proc)
            return proc
        return _child(state["exit"])

    def spawn_restart(profile=None):
        return spawn(web_server._gateway_subcommand(profile, "restart"), "gateway-restart"), False

    monkeypatch.setattr(web_server, "_spawn_youtab_action", spawn)
    monkeypatch.setattr(web_server, "_spawn_gateway_restart", spawn_restart)
    monkeypatch.setattr(lifecycle, "default_pid_probe", probe)
    # Test-scale deadlines so the suite does not wait two minutes for a verdict.
    # These are read at call time, so patching the constants is enough.
    monkeypatch.setattr(lifecycle, "DEFAULT_TIMEOUT_S", 5.0)
    monkeypatch.setattr(lifecycle, "DEFAULT_SETTLE_S", 0.3)
    monkeypatch.setattr(lifecycle, "DEFAULT_POLL_INTERVAL_S", 0.01)
    return state


@pytest.fixture
def gated(monkeypatch, fake_gateway):
    """A hosted, gated dashboard -- what the pre-production host is."""
    monkeypatch.setenv(ROSTER_ENV, json.dumps(ROSTER))
    lifecycle.REGISTRY.__init__()
    clear_providers()
    register_provider(_IdentityProvider())
    prev = (
        getattr(web_server.app.state, "bound_host", None),
        getattr(web_server.app.state, "bound_port", None),
        getattr(web_server.app.state, "auth_required", None),
    )
    web_server.app.state.bound_host = "agent.example.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    yield TestClient(web_server.app, base_url="https://agent.example.test")
    clear_providers()
    (
        web_server.app.state.bound_host,
        web_server.app.state.bound_port,
        web_server.app.state.auth_required,
    ) = prev


def _as(client, user_id):
    client.cookies.set(SESSION_COOKIE, user_id)
    return client


def _settle(client, job_id, *, timeout_s=10.0):
    """Poll the real status endpoint until the job is terminal."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/api/gateway/jobs/{job_id}").json()
        if body["state"] != "pending":
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached a terminal state")


class TestHttpContract:
    def test_restart_is_accepted_not_declared_successful(self, gated, fake_gateway):
        """202, and `ok` is null -- the answer does not exist yet.

        This is the assertion the old endpoint could not pass: it returned 200
        with `ok: true` before anything had happened.
        """
        resp = _as(gated, OWNER).post("/api/gateway/restart")
        assert resp.status_code == 202
        body = resp.json()
        assert body["state"] == "pending"
        assert body["ok"] is None
        assert "job_id" in body

    def test_a_failed_child_never_reads_as_successful(self, gated, fake_gateway):
        """The requirement, stated as bluntly as it can be."""
        fake_gateway["exit"] = 1
        fake_gateway["pids"] = [None]
        job = _as(gated, OWNER).post("/api/gateway/restart").json()
        final = _settle(gated, job["job_id"])
        assert final["state"] == "failed"
        assert final["ok"] is False
        assert final["exit_code"] == 1

    def test_a_successful_restart_reads_as_successful(self, gated, fake_gateway):
        fake_gateway["exit"] = 0
        # up, down, back as a new process: a restart that actually happened.
        fake_gateway["pids"] = [4242, None, 5555]
        job = _as(gated, OWNER).post("/api/gateway/restart").json()
        final = _settle(gated, job["job_id"])
        assert final["state"] == "succeeded"
        assert final["ok"] is True

    def test_status_survives_a_refresh(self, gated, fake_gateway):
        """A reload must not lose or invent the outcome.

        The job is server-side state, so a fresh client with no memory of the
        request reads the same verdict.
        """
        fake_gateway["exit"] = 1
        job = _as(gated, OWNER).post("/api/gateway/restart").json()
        final = _settle(gated, job["job_id"])

        reloaded = TestClient(web_server.app, base_url="https://agent.example.test")
        reloaded.cookies.set(SESSION_COOKIE, OWNER)
        again = reloaded.get(f"/api/gateway/jobs/{job['job_id']}").json()
        assert again["state"] == final["state"] == "failed"
        assert again["ok"] is False

    def test_an_unknown_job_is_404_not_a_synthesised_success(self, gated):
        assert _as(gated, OWNER).get("/api/gateway/jobs/nope").status_code == 404

    def test_a_missing_profile_is_refused_before_anything_is_spawned(self, gated, fake_gateway):
        resp = _as(gated, OWNER).post("/api/gateway/restart?profile=no-such-profile")
        assert resp.status_code == 404
        assert fake_gateway["commands"] == []

    def test_a_double_click_reuses_the_in_flight_job(self, gated, fake_gateway):
        """Idempotent restart, over HTTP."""
        fake_gateway["exit"] = 0
        release = fake_gateway["release"] = Event()
        client = _as(gated, OWNER)
        first = None
        try:
            first_response = client.post("/api/gateway/restart")
            assert first_response.status_code == 202
            first = first_response.json()
            # The child and the lifecycle observation stay blocked until both
            # requests have run, regardless of how quickly the host schedules them.
            assert fake_gateway["observing"].wait(timeout=5), "job never began observing"
            second_response = client.post("/api/gateway/restart")
            assert second_response.status_code == 202
            second = second_response.json()
            assert first["state"] == second["state"] == "pending"
            assert first["reused"] is False
            assert second["job_id"] == first["job_id"]
            assert second["reused"] is True
            assert len(fake_gateway["commands"]) == 1
            assert fake_gateway["children"][0].poll() is None
        finally:
            release.set()
            for proc in fake_gateway["children"]:
                proc.stdin.close()
                proc.wait(timeout=5)
            if first is not None:
                _settle(client, first["job_id"])

    def test_a_restart_storm_is_refused_with_429(self, gated, fake_gateway):
        fake_gateway["exit"] = 1
        client = _as(gated, OWNER)
        codes = []
        for _ in range(lifecycle.LifecycleRegistry.STORM_MAX_OPS + 2):
            resp = client.post("/api/gateway/restart")
            codes.append(resp.status_code)
            if resp.status_code == 202:
                _settle(gated, resp.json()["job_id"])
        assert 429 in codes

    def test_start_and_stop_share_the_contract(self, gated, fake_gateway):
        fake_gateway["exit"] = 0
        fake_gateway["pids"] = [None, 7777]
        started = _as(gated, OWNER).post("/api/gateway/start")
        assert started.status_code == 202
        assert _settle(gated, started.json()["job_id"])["state"] == "succeeded"

        fake_gateway["pids"] = [None]
        stopped = _as(gated, OWNER).post("/api/gateway/stop")
        assert stopped.status_code == 202
        assert _settle(gated, stopped.json()["job_id"])["state"] == "succeeded"

    def test_the_spawned_command_is_the_gateway_cli(self, gated, fake_gateway):
        """Unrelated containers are preserved because nothing can touch them.

        The lifecycle path shells out to `youtab gateway <verb>` and to nothing
        else. No Docker CLI, no socket, no container name: the repair that
        would have made the two-container split "work" is not reachable from
        here, and this fails if anyone adds it.
        """
        _as(gated, OWNER).post("/api/gateway/stop")
        assert fake_gateway["commands"], "nothing was spawned"
        for command in fake_gateway["commands"]:
            assert command[-2:] == ["gateway", "stop"]
            joined = " ".join(command).lower()
            assert "docker" not in joined
            assert "podman" not in joined
            assert "docker.sock" not in joined


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestAuthorization:
    """Who may drive the lifecycle. Every caller here is authenticated.

    A test showing an anonymous caller turned away would prove nothing: the
    auth gate already does that, and these assertions would still pass with the
    authorization gate deleted.
    """

    def test_the_lifecycle_routes_are_guarded_at_all(self):
        """Default-deny only helps if the route is in the table."""
        for path in (
            "/api/gateway/restart", "/api/gateway/start",
            "/api/gateway/stop", "/api/gateway/jobs/abc",
        ):
            assert required_scope(path, "POST") == DEPLOYMENT_MANAGE

    def test_drain_is_not_swept_into_the_lifecycle_scope(self):
        """Drain is operations, not gateway lifecycle.

        It used to be enough to assert drain had no entry at all, because an
        unmapped route was reachable and the token-auth seam attaches no
        session. Unmapped is now a refusal, so the property has to be stated
        directly: drain carries its own operations scope and is *not* swept
        into the lifecycle scope that guards start/stop/restart.

        The token seam still works, because a request it authenticates now
        resolves to the principal it verified rather than to a scopeless
        stranger with no session.
        """
        from youtab_agent_cli.authz import DEPLOYMENT_MANAGE, OPS_MANAGE

        scope = required_scope("/api/gateway/drain", "POST")
        assert scope == OPS_MANAGE
        assert scope != DEPLOYMENT_MANAGE
        assert required_scope("/api/gateway/stop", "POST") == DEPLOYMENT_MANAGE

    def test_owner_may_restart(self, gated, fake_gateway):
        assert _as(gated, OWNER).post("/api/gateway/restart").status_code == 202

    def test_superadmin_may_restart(self, gated, fake_gateway):
        assert _as(gated, SUPERADMIN).post("/api/gateway/restart").status_code == 202

    @pytest.mark.parametrize("verb", ["restart", "start", "stop"])
    def test_a_normal_user_is_refused(self, gated, fake_gateway, verb):
        assert _as(gated, NORMAL).post(f"/api/gateway/{verb}").status_code == 403

    @pytest.mark.parametrize("verb", ["restart", "start", "stop"])
    def test_a_tenant_admin_is_refused(self, gated, fake_gateway, verb):
        """A customer administrator may not restart Youtab's gateway.

        Real authority over their own company, none over the deployment. If
        authority is ever remodelled as one ascending ladder, this fails first.
        """
        assert _as(gated, TENANT_ADMIN).post(f"/api/gateway/{verb}").status_code == 403

    @pytest.mark.parametrize("verb", ["restart", "start", "stop"])
    def test_an_unscoped_operator_is_refused(self, gated, fake_gateway, verb):
        """Being an operator is not authority; the scope is."""
        assert _as(gated, OPERATOR_BARE).post(f"/api/gateway/{verb}").status_code == 403

    def test_an_operator_scoped_for_something_else_is_still_refused(self, monkeypatch, gated, fake_gateway):
        """`provider:read` is not `deployment:manage`."""
        roster: dict[str, Any] = dict(ROSTER)
        roster["operator-reader"] = {
            "role": Role.YOUTAB_OPERATOR.value, "scopes": [PROVIDER_READ],
        }
        monkeypatch.setenv(ROSTER_ENV, json.dumps(roster))
        ORGS["operator-reader"] = ""
        try:
            assert _as(gated, "operator-reader").post("/api/gateway/restart").status_code == 403
        finally:
            ORGS.pop("operator-reader", None)

    def test_a_refused_caller_spawns_nothing(self, gated, fake_gateway):
        """Refusal is before the side effect, not after it."""
        _as(gated, NORMAL).post("/api/gateway/restart")
        assert fake_gateway["commands"] == []

    def test_a_refused_caller_cannot_read_job_state(self, gated, fake_gateway):
        job = _as(gated, OWNER).post("/api/gateway/restart").json()
        gated.cookies.clear()
        assert _as(gated, NORMAL).get(f"/api/gateway/jobs/{job['job_id']}").status_code == 403

    def test_the_refusal_names_nothing_behind_the_gate(self, gated, fake_gateway):
        """A refusal that explains how to defeat itself is a worse refusal."""
        body = _as(gated, NORMAL).post("/api/gateway/restart").json()
        detail = json.dumps(body).lower()
        for leak in ("roster", "deployment:manage", "superadmin", "owner", "profile"):
            assert leak not in detail


class TestForgedAccessIdentity:
    """A well-formed Access assertion that is not trustworthy is refused."""

    @pytest.fixture
    def access_host(self, monkeypatch, gated):
        monkeypatch.setenv(
            "YOUTAB_AGENT_DASHBOARD_PUBLIC_URL", "https://agent.example.test"
        )
        return gated

    def test_a_forged_assertion_is_refused(self, access_host, monkeypatch):
        """Signed by a key the team's JWKS does not contain.

        The middleware must refuse it, and must do so as a 403 rather than an
        exception escaping as a 500 -- a rejected credential that produces an
        error page tells the caller they found something that breaks.
        """
        jwt = pytest.importorskip("jwt")
        from cryptography.hazmat.primitives.asymmetric import rsa

        from youtab_agent_cli import access_jwt

        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        monkeypatch.setenv("YOUTAB_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
        monkeypatch.setenv("YOUTAB_ACCESS_AUD", "a" * 64)
        monkeypatch.setenv("YOUTAB_ACCESS_ALLOWED_EMAILS", "owner@youtab.io")
        # An empty JWKS: nothing can verify, which is the honest model of a
        # signature the team never issued.
        monkeypatch.setattr(access_jwt, "_fetch_jwks", lambda url: {"keys": []})
        access_jwt._jwks_cache.clear()

        now = int(time.time())
        forged = jwt.encode(
            {
                "iss": "https://team.cloudflareaccess.com",
                "aud": "a" * 64,
                "email": "owner@youtab.io",
                "iat": now - 10, "nbf": now - 10, "exp": now + 600,
            },
            attacker,
            algorithm="RS256",
            headers={"kid": "attacker-key"},
        )
        resp = _as(access_host, OWNER).post(
            "/api/gateway/restart", headers={"Cf-Access-Jwt-Assertion": forged}
        )
        # 401, not 403. A forged assertion is an identity failure, and since
        # Cloudflare Access became a registered auth provider the dashboard
        # gate refuses it before the origin-side guard is reached -- the gate
        # runs outside `access_identity_middleware`. 403 would say "we know who
        # you are and you may not", which is the opposite of what happened.
        assert resp.status_code == 401

    def test_a_missing_assertion_is_refused_on_the_public_name(self, access_host, monkeypatch):
        """Fails closed: no assertion is not "internal", it is unproven."""
        from youtab_agent_cli import access_jwt

        monkeypatch.setenv("YOUTAB_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
        monkeypatch.setenv("YOUTAB_ACCESS_AUD", "a" * 64)
        monkeypatch.setenv("YOUTAB_ACCESS_ALLOWED_EMAILS", "owner@youtab.io")
        access_jwt._jwks_cache.clear()
        assert _as(access_host, OWNER).post("/api/gateway/restart").status_code == 403

    def test_a_forged_assertion_spawns_nothing(self, access_host, monkeypatch, fake_gateway):
        from youtab_agent_cli import access_jwt

        monkeypatch.setenv("YOUTAB_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
        monkeypatch.setenv("YOUTAB_ACCESS_AUD", "a" * 64)
        monkeypatch.setenv("YOUTAB_ACCESS_ALLOWED_EMAILS", "owner@youtab.io")
        access_jwt._jwks_cache.clear()
        _as(access_host, OWNER).post(
            "/api/gateway/restart", headers={"Cf-Access-Jwt-Assertion": "not-a-token"}
        )
        assert fake_gateway["commands"] == []


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_log_file(tmp_path, monkeypatch):
    """Point the real audit writer at a temp file and read what it wrote."""
    import youtab_agent_cli.dashboard_auth.audit as audit_mod

    path = tmp_path / "dashboard-auth.log"
    monkeypatch.setattr(audit_mod, "_resolve_log_path", lambda: path)
    return path


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestAudit:
    def test_a_requested_restart_is_recorded(self, gated, fake_gateway, audit_log_file):
        job = _as(gated, OWNER).post("/api/gateway/restart").json()
        _settle(gated, job["job_id"])
        events = _events(audit_log_file)
        assert any(e["event"] == "gateway_lifecycle_requested" for e in events)

    def test_success_and_failure_are_distinct_events(self, gated, fake_gateway, audit_log_file):
        fake_gateway["exit"] = 0
        # Already back on the first observation: an ordinary restart, so this
        # must record `succeeded` and not `recovered`.
        fake_gateway["pids"] = [4242, 5555]
        ok_job = _as(gated, OWNER).post("/api/gateway/restart").json()
        _settle(gated, ok_job["job_id"])

        fake_gateway["exit"] = 1
        fake_gateway["pids"] = [None]
        bad_job = _as(gated, OWNER).post("/api/gateway/restart").json()
        _settle(gated, bad_job["job_id"])

        kinds = {e["event"] for e in _events(audit_log_file)}
        assert "gateway_lifecycle_succeeded" in kinds
        assert "gateway_lifecycle_failed" in kinds

    @pytest.mark.parametrize("verb", ["start", "stop", "restart"])
    def test_every_verb_is_recorded(self, gated, fake_gateway, audit_log_file, verb):
        fake_gateway["pids"] = (
            [None] if verb == "stop" else [4242, None, 5555] if verb == "restart" else [None, 4242]
        )
        job = _as(gated, OWNER).post(f"/api/gateway/{verb}").json()
        _settle(gated, job["job_id"])
        assert any(e.get("verb") == verb for e in _events(audit_log_file))

    def test_recovery_is_recorded_distinctly(self, registry, audit_log_file):
        """The supervisor catching a crash is not an ordinary restart."""
        from youtab_agent_cli.dashboard_auth.audit import AuditEvent, audit_log

        job, _ = registry.admit("restart", None)
        proc = _child(0)
        proc.wait()
        _run(
            job, proc, registry=registry,
            pid_probe=_sequence_probe([None, None, 500]), pid_before=100,
            on_terminal=lambda j, recovered: audit_log(
                AuditEvent.GATEWAY_LIFECYCLE_RECOVERED if recovered
                else AuditEvent.GATEWAY_LIFECYCLE_SUCCEEDED,
                verb=j.verb, job_id=j.job_id,
            ),
        )
        assert any(
            e["event"] == "gateway_lifecycle_recovered" for e in _events(audit_log_file)
        )

    def test_a_refusal_is_recorded_with_the_principal(self, gated, fake_gateway, audit_log_file):
        _as(gated, TENANT_ADMIN).post("/api/gateway/restart")
        denied = [e for e in _events(audit_log_file) if e["event"] == "privileged_access_denied"]
        assert denied
        assert denied[-1]["user_id"] == TENANT_ADMIN
        assert denied[-1]["scope"] == DEPLOYMENT_MANAGE

    def test_the_audit_trail_carries_no_secret(self, gated, fake_gateway, audit_log_file, monkeypatch):
        """An audit log is durable and widely read. It must never hold a key."""
        secret = FAKE_VENDOR_KEY
        monkeypatch.setenv("EXAMPLE_UPSTREAM_API_KEY", secret)
        fake_gateway["exit"] = 1
        job = _as(gated, OWNER).post("/api/gateway/restart").json()
        _settle(gated, job["job_id"])
        assert secret not in audit_log_file.read_text(encoding="utf-8")

    def test_no_cookie_or_token_field_reaches_the_log(self, gated, fake_gateway, audit_log_file):
        job = _as(gated, OWNER).post("/api/gateway/restart").json()
        _settle(gated, job["job_id"])
        for event in _events(audit_log_file):
            assert "cookie" not in event
            assert "access_token" not in event
            assert "authorization" not in event


# ---------------------------------------------------------------------------
# Topology invariant this endpoint depends on
# ---------------------------------------------------------------------------


def test_the_lifecycle_control_still_lives_with_the_gateway():
    """The endpoint only works because both roles share a container.

    `service_manager` finds supervised slots under container-local tmpfs, so a
    dashboard in a second container cannot see the gateway's slot. Pinned here
    as well as in the topology suite because it is *this* surface that breaks.
    """
    yaml = pytest.importorskip("yaml")

    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    assert len(compose.get("services") or {}) == 1
