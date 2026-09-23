"""Truthful terminal results for gateway start, stop and restart.

A spawned child is not a completed operation. The control surface used to
answer ``{"ok": true}`` the instant ``subprocess.Popen`` returned, which is a
claim about the *dispatch* dressed up as a claim about the *outcome*. Every way
the operation can actually fail happens strictly after that moment: the child
exits non-zero, the supervised slot is not registered, the gateway comes down
and does not come back, the whole thing hangs. The dashboard rendered all of
them as success, so the one question an operator asks a lifecycle control --
"did it work?" -- had no truthful answer anywhere in the system.

The contract here is therefore: the POST **accepts** the request and returns
``202`` with a job id, and the job reaches a terminal state only when something
authoritative has been observed. Two things are checked, in order, and both
must agree:

1. **The child's exit status.** Non-zero is a failure and the code is carried
   through to the caller rather than flattened into a boolean.
2. **The gateway's actual liveness**, polled from the same signal
   ``/api/status`` reads. A child that exits ``0`` while leaving no running
   gateway is a *failure* -- this is exactly the shape the two-container
   topology defect had, where ``youtab gateway restart`` returned cleanly
   having restarted nothing the dashboard could see.

Neither check alone is sufficient. Exit status alone trusts a process that may
have done nothing; liveness alone would call a no-op success because something
unrelated was already running.

Bounded, not unbounded
----------------------
Both phases carry deadlines. An operation that never finishes resolves to
``failed``/``timeout`` rather than sitting in ``pending`` forever, because a
control that stays "in progress" indefinitely is the same lie in a slower
voice. The child is deliberately *not* killed on timeout: interrupting a
restart mid-flight is more destructive than reporting honestly that it has not
finished, and the recorded pid lets an operator act.

Nothing here decides *who* may call it. Authorization lives in
:mod:`youtab_agent_cli.authz`; this module assumes the caller already passed it.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)


# --- Vocabulary -------------------------------------------------------------


class JobState(StrEnum):
    """Where a lifecycle operation is. ``pending`` is never a result."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: States a caller may stop polling on.
TERMINAL_STATES: frozenset[str] = frozenset({JobState.SUCCEEDED, JobState.FAILED})


class Reason(StrEnum):
    """Why a job reached its terminal state.

    Machine-readable and stable: the UI selects wording from these, and they
    appear in audit records. A reason never carries a value read out of the
    environment or the child's output -- see :func:`redact`.
    """

    COMPLETED = "completed"
    ALREADY_RUNNING = "already_running"
    ALREADY_STOPPED = "already_stopped"
    CHILD_EXIT_NONZERO = "child_exit_nonzero"
    NOT_RUNNING_AFTER_START = "not_running_after_start"
    NOT_RUNNING_AFTER_RESTART = "not_running_after_restart"
    STILL_RUNNING_AFTER_STOP = "still_running_after_stop"
    TIMEOUT = "timeout"
    SPAWN_FAILED = "spawn_failed"
    NOT_REGISTERED = "not_registered"
    STORM_BLOCKED = "storm_blocked"


#: The verbs this module manages, and the action name each maps to.
VERB_ACTIONS: dict[str, str] = {
    "start": "gateway-start",
    "stop": "gateway-stop",
    "restart": "gateway-restart",
}

#: Verbs that must leave a *running* gateway behind to count as successful.
_EXPECT_RUNNING: frozenset[str] = frozenset({"start", "restart"})


# --- Redaction --------------------------------------------------------------
#
# Job detail carries the tail of a child's log so a failure is diagnosable in
# the UI. That log is written by a process whose environment holds provider
# keys, and a lifecycle failure is exactly when someone pastes the output into
# a ticket. Everything leaving this module through the API is scrubbed.

_REDACTED = "[redacted]"

#: Env var names whose *values* are scrubbed wherever they appear literally.
#: Matched on the name, so a provider key added later is covered without this
#: list being updated.
_SENSITIVE_ENV_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|SESSION|AUTH|SIGNATURE|SALT|PRIVATE)",
    re.IGNORECASE,
)

#: Shortest env value worth scrubbing. Below this, a literal match is far more
#: likely to be a coincidence in ordinary prose than a leaked secret, and
#: redacting e.g. a two-character value would corrupt unrelated text.
_MIN_REDACTABLE_ENV_VALUE = 8

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # JWTs, including the Cloudflare Access assertion.
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"),
    # Authorization headers of any scheme.
    re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # Vendor-prefixed API keys (OpenAI, Anthropic, Google, GitHub, Slack, ...).
    re.compile(r"\b(sk|pk|rk|ak)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    # KEY=value / "secret": "value" assignments, whatever the value looks like.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Z0-9_]*)"
        r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
    ),
)


def redact(text: str, environ: Optional[dict[str, str]] = None) -> str:
    """Scrub secrets from text that is about to leave the process.

    Two passes. The pattern pass catches recognisable credential shapes. The
    environment pass catches everything else by matching the *literal values*
    of sensitively-named variables in this process's own environment, which is
    what makes an unrecognised provider's key redactable without this module
    knowing its format.
    """
    if not text:
        return text
    source = os.environ if environ is None else environ
    for name, value in source.items():
        if not value or len(value) < _MIN_REDACTABLE_ENV_VALUE:
            continue
        if _SENSITIVE_ENV_RE.search(name) and value in text:
            text = text.replace(value, _REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
        else:
            text = pattern.sub(_REDACTED, text)
    return text


# --- The truth function -----------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """Everything known about an operation at one instant.

    ``pid_before`` is sampled *before* the child is spawned, which is what
    makes a restart distinguishable from "something was already running". A
    restart that observes a live gateway is not thereby successful -- that may
    still be the old process, and reporting success on it is how a restart that
    silently did nothing looks green.
    """

    exit_code: Optional[int]
    """``None`` while the child is still alive."""
    running_after: bool
    pid_before: Optional[int]
    pid_after: Optional[int]
    deadline_passed: bool = False


def desired_state_reached(verb: str, obs: Observation) -> bool:
    """Has the gateway actually arrived where this verb was meant to put it?

    ``restart`` demands a *different* pid, not merely a live one. ``start``
    accepts a live gateway however it got there, because starting something
    already started is a satisfied request, not a failed one.
    """
    if verb == "stop":
        return not obs.running_after
    if not obs.running_after:
        return False
    if verb == "restart":
        return obs.pid_before is None or (
            obs.pid_after is not None and obs.pid_after != obs.pid_before
        )
    return True


def resolve_outcome(verb: str, obs: Observation) -> tuple[JobState, Reason]:
    """Decide the terminal state from observed facts. Pure, so it is testable.

    Order matters. The desired state is checked *first*, because a child that
    is still alive is not a failure when that child is itself the gateway --
    which is exactly what ``youtab gateway restart`` becomes on an install with
    no service manager. Reaching the goal is the point; how the child behaved
    while getting there is only evidence.
    """
    if desired_state_reached(verb, obs):
        if verb == "start" and obs.pid_before is not None:
            return JobState.SUCCEEDED, Reason.ALREADY_RUNNING
        if verb == "stop" and obs.pid_before is None:
            return JobState.SUCCEEDED, Reason.ALREADY_STOPPED
        return JobState.SUCCEEDED, Reason.COMPLETED

    # Goal not reached. A non-zero child is the most specific explanation
    # available, so it wins over the generic "did not get there".
    if obs.exit_code is not None and obs.exit_code != 0:
        return JobState.FAILED, Reason.CHILD_EXIT_NONZERO
    if obs.exit_code == 0:
        if verb == "stop":
            return JobState.FAILED, Reason.STILL_RUNNING_AFTER_STOP
        return (
            JobState.FAILED,
            Reason.NOT_RUNNING_AFTER_RESTART
            if verb == "restart"
            else Reason.NOT_RUNNING_AFTER_START,
        )
    # Child still alive and the gateway never arrived.
    return JobState.FAILED, Reason.TIMEOUT


# --- Jobs -------------------------------------------------------------------


@dataclass
class LifecycleJob:
    """One lifecycle operation and everything known about its outcome."""

    job_id: str
    verb: str
    action: str
    profile: Optional[str]
    state: JobState = JobState.PENDING
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    reason: Optional[Reason] = None
    detail: str = ""
    started_at: float = 0.0
    finished_at: Optional[float] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def ok(self) -> Optional[bool]:
        """``None`` while pending -- deliberately not ``False``.

        A caller that renders "not ok" for an operation still in flight is
        making the same class of claim this module exists to prevent, just in
        the pessimistic direction.
        """
        if not self.is_terminal:
            return None
        return self.state == JobState.SUCCEEDED

    def public(self) -> dict[str, Any]:
        """The API view. Every free-text field is redacted on the way out."""
        return {
            "job_id": self.job_id,
            "action": self.action,
            "verb": self.verb,
            "profile": self.profile,
            "state": self.state.value,
            "ok": self.ok,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "reason": self.reason.value if self.reason else None,
            "detail": redact(self.detail),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class StormBlocked(RuntimeError):
    """Too many lifecycle operations in too short a window."""


class LifecycleRegistry:
    """Jobs, single-flight admission, and the restart-storm breaker.

    Single-flight is keyed by ``(verb, profile)``: a second restart arriving
    while one is in flight returns *the same job* rather than spawning a rival
    child. Two concurrent ``youtab gateway restart`` processes race each other
    on the kill-and-start path, and the older code's answer to that was to hand
    both callers a success.
    """

    #: Retained finished jobs. Bounded so a long-lived dashboard does not grow
    #: a job record per restart forever.
    MAX_JOBS = 64
    #: Storm breaker: at most this many *starts* of a given (verb, profile)
    #: inside the window. Chosen to sit above deliberate operator retries and
    #: below an automated respawn loop, matching the gateway's own breaker.
    STORM_MAX_OPS = 5
    STORM_WINDOW_S = 60.0

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._jobs: dict[str, LifecycleJob] = {}
        self._order: list[str] = []
        self._inflight: dict[tuple[str, Optional[str]], str] = {}
        self._recent: dict[tuple[str, Optional[str]], list[float]] = {}

    # -- storm breaker --

    def _check_storm(self, key: tuple[str, Optional[str]]) -> None:
        now = self._clock()
        window = [t for t in self._recent.get(key, []) if now - t < self.STORM_WINDOW_S]
        self._recent[key] = window
        if len(window) >= self.STORM_MAX_OPS:
            raise StormBlocked(
                f"too many {key[0]} operations in {int(self.STORM_WINDOW_S)}s"
            )

    def _record_op(self, key: tuple[str, Optional[str]]) -> None:
        self._recent.setdefault(key, []).append(self._clock())

    # -- admission --

    def inflight_job(self, verb: str, profile: Optional[str]) -> Optional[LifecycleJob]:
        with self._lock:
            job_id = self._inflight.get((verb, profile))
            if job_id is None:
                return None
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                self._inflight.pop((verb, profile), None)
                return None
            return job

    def admit(self, verb: str, profile: Optional[str]) -> tuple[LifecycleJob, bool]:
        """Reserve a job for ``(verb, profile)``.

        Returns ``(job, created)``. ``created`` is ``False`` when an identical
        operation was already in flight and is being shared -- which is what
        makes a double-clicked Restart idempotent instead of a race.
        """
        key = (verb, profile)
        with self._lock:
            existing = self.inflight_job(verb, profile)
            if existing is not None:
                return existing, False
            self._check_storm(key)
            job = LifecycleJob(
                job_id=uuid.uuid4().hex,
                verb=verb,
                action=VERB_ACTIONS[verb],
                profile=profile,
                started_at=time.time(),
            )
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._inflight[key] = job.job_id
            self._record_op(key)
            self._evict()
            return job, True

    def _evict(self) -> None:
        while len(self._order) > self.MAX_JOBS:
            oldest = self._order.pop(0)
            job = self._jobs.get(oldest)
            # Never evict something still running: its caller is still polling.
            if job is not None and not job.is_terminal:
                self._order.append(oldest)
                return
            self._jobs.pop(oldest, None)

    def get(self, job_id: str) -> Optional[LifecycleJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def record_pid(self, job: LifecycleJob, pid: Optional[int]) -> None:
        """Publish the child's pid SYNCHRONOUSLY under the admission lock.

        The pid is known the instant the child is spawned, but ``run_job`` only
        assigns ``job.pid`` on the background thread. A second caller reusing an
        in-flight job (``admit`` -> single-flight branch) could otherwise observe
        ``job.pid is None`` before that thread runs — the concurrency defect where
        an enable/restart reuse returned ``restart_pid: null``. Recording it here,
        before the response is built and before the job thread starts, closes that
        publish-ordering window for both the creating and the reusing caller.
        """
        with self._lock:
            job.pid = pid

    def finish(
        self,
        job: LifecycleJob,
        state: JobState,
        reason: Reason,
        *,
        exit_code: Optional[int] = None,
        detail: str = "",
    ) -> LifecycleJob:
        with self._lock:
            job.state = state
            job.reason = reason
            job.exit_code = exit_code
            job.detail = detail
            job.finished_at = time.time()
            self._inflight.pop((job.verb, job.profile), None)
            return job


#: Process-wide registry. The dashboard is one process per container.
REGISTRY = LifecycleRegistry()


# --- Observation ------------------------------------------------------------


def default_pid_probe(profile: Optional[str]) -> Optional[int]:
    """The pid of the running gateway for ``profile``, or ``None``.

    Uses the same helper ``/api/status`` reads, so a job's verdict and the
    status readout can never disagree -- if they could, the UI would show a
    successful restart beside "Gateway: Off", which is the confusion this whole
    change exists to end.

    That helper validates the recorded pid against the live process table and
    the runtime lock before returning it, so a stale pid file left behind by an
    ungracefully-killed gateway reads as *not running* rather than as a healthy
    gateway that happens to be dead.
    """
    from gateway.status import get_running_pid
    from youtab_agent_cli import profiles as profiles_mod

    name = (profile or "default").strip() or "default"
    profile_dir = profiles_mod.get_profile_dir(name)
    try:
        return get_running_pid(profile_dir / "gateway.pid", cleanup_stale=False)
    except Exception:
        # A probe that errors is not evidence of health.
        return None


# --- Execution --------------------------------------------------------------

#: Overall bound on one operation. Generous: a restart drains in-flight turns.
DEFAULT_TIMEOUT_S = 120.0
#: Grace granted *after* a clean child exit for the gateway to appear. A
#: restarted gateway needs a moment to take its lock and write its pid, and
#: judging the instant the child exits would fail a healthy restart.
DEFAULT_SETTLE_S = 30.0
DEFAULT_POLL_INTERVAL_S = 0.5


def run_job(
    job: LifecycleJob,
    proc: subprocess.Popen,
    *,
    registry: LifecycleRegistry = REGISTRY,
    pid_probe: Optional[Callable[[Optional[str]], Optional[int]]] = None,
    pid_before: Optional[int] = None,
    # Resolved from the module constants at call time rather than bound as
    # default arguments, which would freeze them at import and make the
    # constants above documentation rather than configuration.
    timeout_s: Optional[float] = None,
    settle_s: Optional[float] = None,
    poll_interval_s: Optional[float] = None,
    tail: Optional[Callable[[], str]] = None,
    on_terminal: Optional[Callable[[LifecycleJob, bool], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> LifecycleJob:
    """Drive one job to a terminal state. Blocking; callers run it on a thread.

    The loop watches the child and the gateway together rather than waiting for
    the child and only then looking. That matters in both directions:

    * On an install with no service manager, ``youtab gateway restart`` *becomes*
      the gateway and never exits. Waiting for it would time out on a restart
      that plainly worked, so a live child whose gateway has arrived is a
      success.
    * A child that exits ``0`` having achieved nothing is a failure, so a clean
      exit only opens a bounded settling window; it does not end the question.

    ``on_terminal(job, recovered)`` is invoked once, after the job is terminal,
    for audit. Never raises: an unresolvable job is *failed*, because an
    exception escaping here would strand the record in ``pending`` and leave the
    UI showing a spinner in place of a result.
    """
    probe = pid_probe or default_pid_probe
    timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    settle_s = DEFAULT_SETTLE_S if settle_s is None else settle_s
    poll_interval_s = DEFAULT_POLL_INTERVAL_S if poll_interval_s is None else poll_interval_s
    job.pid = proc.pid
    recovered = False

    try:
        started = clock()
        exited_at: Optional[float] = None
        exit_code: Optional[int] = None
        # True once the operation's own child has finished while the gateway is
        # still *not* where it was asked to be. If the goal is reached after
        # that, this operation did not achieve it -- something else did, which
        # in practice is the supervisor restarting a process that crashed. That
        # is the distinction "recovered" records, and it is why the flag is not
        # simply "we had to wait": every restart has a gap, and treating a slow
        # but ordinary restart as a recovery would bury the real crashes.
        child_done_and_unmet = False
        obs: Observation

        while True:
            exit_code = proc.poll()
            if exit_code is not None and exited_at is None:
                exited_at = clock()

            pid_after = probe(job.profile)
            obs = Observation(
                exit_code=exit_code,
                running_after=pid_after is not None,
                pid_before=pid_before,
                pid_after=pid_after,
            )
            reached = desired_state_reached(job.verb, obs)
            if reached:
                break
            if exit_code is not None:
                child_done_and_unmet = True
            # A child that failed outright cannot be waited out.
            if exit_code is not None and exit_code != 0:
                break
            # Clean exit: allow a bounded settling window, then conclude.
            if exit_code == 0 and exited_at is not None and clock() - exited_at >= settle_s:
                break
            if clock() - started >= timeout_s:
                obs = Observation(
                    exit_code=exit_code,
                    running_after=obs.running_after,
                    pid_before=pid_before,
                    pid_after=obs.pid_after,
                    deadline_passed=True,
                )
                break
            sleep(poll_interval_s)

        state, reason = resolve_outcome(job.verb, obs)
        recovered = state == JobState.SUCCEEDED and child_done_and_unmet
        finished = registry.finish(
            job, state, reason,
            exit_code=obs.exit_code,
            detail=tail() if tail else "",
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("gateway lifecycle job %s failed to resolve", job.job_id)
        finished = registry.finish(
            job, JobState.FAILED, Reason.SPAWN_FAILED, detail=f"{type(exc).__name__}",
        )
        recovered = False

    if on_terminal is not None:
        try:
            on_terminal(finished, recovered)
        except Exception:  # an audit sink must not change the outcome
            _log.exception("gateway lifecycle audit sink failed")
    return finished


def start_job_thread(
    job: LifecycleJob, proc: subprocess.Popen, **kwargs: Any
) -> threading.Thread:
    """Resolve ``job`` on a daemon thread so the POST can return ``202``."""
    thread = threading.Thread(
        target=run_job,
        args=(job, proc),
        kwargs=kwargs,
        name=f"gateway-lifecycle-{job.verb}-{job.job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return thread
