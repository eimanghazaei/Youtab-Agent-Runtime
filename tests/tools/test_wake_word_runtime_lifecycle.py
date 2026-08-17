"""Lifecycle, ownership, cleanup, and what must NOT survive a session.

``test_wake_word.py`` covers the happy path of the singleton — one owner starts,
a callback pauses, a stream failure releases — and the machine lock being
released when a child process exits *cleanly*. This file covers the rest of the
lifecycle, and specifically the properties that are only interesting when
something goes wrong:

* every exit path closes the microphone stream and the engine, including the one
  where ``InputStream()`` constructs and ``start()`` then fails, which leaves a
  half-open stream holding the device;
* the machine lock is released when the owning process is KILLED, not asked to
  stop — a lease that needed cooperative cleanup would strand the microphone
  after a crash, on a file that lives in the user's home directory;
* nothing carries between sessions. A second ``start_listening`` after a
  ``stop_listening`` must not inherit the first session's cooldown, its
  confirmation streak, its dead-microphone flag, or its engine.

The cross-session assertions are on *state*, never on elapsed wall time. A
"session 2 fired faster than the cooldown" test would pass on a leaky runtime
too, just later — so what is asserted is the exact expression the fire gate
evaluates, plus object identity.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import tools.wake_word as ww

from .test_wake_word import (
    _SCHEDULING_CEILING_S,
    _await_child_event,
    _Frame,
    _poll_until,
)


# ── doubles ──────────────────────────────────────────────────────────────────


class _Stream:
    """A stream that reads forever until closed."""

    instances: list["_Stream"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def read(self, n):
        time.sleep(0.002)
        # ``_Frame`` rather than a bare list: the runtime's peak probe runs
        # ``abs(frame).max()``, and on a plain list that raises, is swallowed,
        # and is treated as *loud* — so a bare list silently disables the
        # dead-microphone path this file needs to observe.
        return _Frame([0] * n), False

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _UnstartableStream(_Stream):
    """Constructs fine, then refuses to start.

    A real case: a device that opens but cannot run at 16 kHz mono, or one
    another process claimed between the open and the start. PortAudio has
    already allocated the stream at that point, so whoever opened it owns the
    device until it is closed.
    """

    def start(self):
        raise OSError("Invalid sample rate")


class _CountingEngine:
    frame_length = 4

    def __init__(self, fire: bool = False, raises: bool = False):
        self.fire = fire
        self.raises = raises
        self.closed = False
        self.resets = 0
        self.processed = 0

    def process(self, frame) -> bool:
        self.processed += 1
        if self.raises:
            raise RuntimeError("engine exploded")
        return self.fire

    def reset(self) -> None:
        self.resets += 1

    def close(self) -> None:
        self.closed = True


def _audio(monkeypatch, stream_cls=_Stream):
    """Fake sounddevice; returns the list of streams it handed out."""
    streams: list[_Stream] = []

    def _input_stream(**kwargs):
        stream = stream_cls(**kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(
        ww, "_import_audio",
        lambda: (types.SimpleNamespace(InputStream=_input_stream), None),
    )
    return streams


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A wake-word singleton with a private lock file and a fake microphone.

    The module-level singleton is process-global, so the fixture also guarantees
    it is torn down: a test that leaves ``_detector`` set would hand its state
    to the next one, which is the very failure mode this file is about.
    """
    lock_path = tmp_path / "wake.lock"
    monkeypatch.setattr(ww, "_lock_path", lambda: lock_path)
    engines: list[_CountingEngine] = []

    state = types.SimpleNamespace(
        lock_path=lock_path,
        engines=engines,
        streams=_audio(monkeypatch),
        builds=0,
        order=[],
    )

    def _build(cfg):
        state.builds += 1
        state.order.append("build_engine")
        engine = _CountingEngine(fire=state.fire)
        engines.append(engine)
        return engine

    state.fire = False
    monkeypatch.setattr(ww, "_build_engine", _build)

    real_acquire = ww._acquire_machine_lock

    def _acquire(path=None):
        state.order.append("acquire_lock")
        return real_acquire(path)

    monkeypatch.setattr(ww, "_acquire_machine_lock", _acquire)
    yield state

    with ww._detector_lock:
        detector, handle = ww._detector, ww._detector_file_lock
        ww._detector = None
        ww._detector_owner = None
        ww._detector_file_lock = None
    if detector is not None:
        try:
            detector.stop()
        finally:
            ww._release_machine_lock(handle)


def _lock_is_free(path: Path) -> bool:
    try:
        handle = ww._acquire_machine_lock(path)
    except ww.WakeWordInUse:
        return False
    ww._release_machine_lock(handle)
    return True


# ── enable / disable lifecycle ───────────────────────────────────────────────


def test_an_owner_is_mandatory(wired):
    with pytest.raises(ValueError, match="owner must not be None"):
        ww.start_listening(lambda: None, owner=None, config={})
    assert wired.builds == 0
    assert _lock_is_free(wired.lock_path)


def test_the_lock_is_taken_before_any_model_is_loaded(wired):
    """Ordering, not just presence.

    Two surfaces racing to arm would otherwise both load a model — tens of MB
    and a second of CPU each — before one of them discovered it had lost.
    """
    owner = object()
    ww.start_listening(lambda: None, owner=owner, config={})
    assert wired.order[:2] == ["acquire_lock", "build_engine"]
    assert ww.stop_listening(owner=owner) is True


def test_a_second_owner_is_refused_and_the_first_keeps_the_microphone(wired):
    first, second = object(), object()
    ww.start_listening(lambda: None, owner=first, config={})

    with pytest.raises(ww.WakeWordInUse):
        ww.start_listening(lambda: None, owner=second, config={})

    assert wired.builds == 1, "the refused owner built an engine anyway"
    assert ww.owns_listener(first) is True
    assert ww.owns_listener(second) is False
    assert ww.is_listening() is True
    assert wired.engines[0].closed is False
    assert ww.stop_listening(owner=second) is False
    assert ww.stop_listening(owner=first) is True


def test_the_same_owner_re_arming_reuses_the_engine(wired):
    """Idempotent for the same owner: rebuild nothing, just resume."""
    owner = object()
    first = ww.start_listening(lambda: None, owner=owner, config={})
    ww.pause_listening(owner=owner)
    second = ww.start_listening(lambda: None, owner=owner, config={})

    assert second is first
    assert wired.builds == 1
    assert ww.is_listening() is True
    assert wired.engines[0].closed is False
    assert ww.stop_listening(owner=owner) is True


def test_pause_and_resume_are_refused_to_a_non_owner(wired):
    owner, other = object(), object()
    ww.start_listening(lambda: None, owner=owner, config={})

    assert ww.pause_listening(owner=other) is False
    assert ww.is_listening() is True                 # untouched
    assert ww.resume_listening(owner=other) is False
    assert ww.pause_listening(owner=owner) is True
    assert ww.is_listening() is False
    assert ww.resume_listening(owner=other) is False
    assert ww.is_listening() is False, "a non-owner resumed the microphone"
    assert ww.resume_listening(owner=owner) is True
    assert ww.is_listening() is True
    assert ww.stop_listening(owner=owner) is True


def test_pause_keeps_the_engine_and_cycles_only_the_stream(wired):
    """The documented split: pause/resume is cheap, stop tears down.

    Also the runaway-loop guard — ``reset()`` on every (re)start is what stops
    audio captured before a voice turn from re-firing the moment the wake word
    resumes.
    """
    owner = object()
    ww.start_listening(lambda: None, owner=owner, config={})
    engine = wired.engines[0]
    assert engine.resets == 1, "the engine was not reset when the stream opened"

    ww.pause_listening(owner=owner)
    assert wired.streams[0].closed is True
    assert engine.closed is False

    ww.resume_listening(owner=owner)
    _poll_until(lambda: len(wired.streams) == 2, "a second stream to be opened")
    assert engine.resets == 2, "stale audio could re-fire after a resume"
    assert wired.streams[1] is not wired.streams[0]

    assert ww.stop_listening(owner=owner) is True
    assert engine.closed is True
    assert wired.streams[1].closed is True


def test_stop_clears_every_piece_of_module_state(wired):
    owner = object()
    ww.start_listening(lambda: None, owner=owner, config={})
    assert ww.stop_listening(owner=owner) is True

    assert ww._detector is None
    assert ww._detector_owner is None
    assert ww._detector_file_lock is None
    assert ww.is_listening() is False
    assert ww.audio_is_silent() is False
    assert ww.get_last_match() is None
    assert _lock_is_free(wired.lock_path), "the machine lease outlived the session"


# ── resource cleanup on failure paths ────────────────────────────────────────


def test_a_stream_that_opens_but_cannot_start_is_still_closed(monkeypatch, wired):
    """The half-open stream case.

    ``InputStream()`` succeeding and ``start()`` failing leaves an allocated
    PortAudio stream holding the device. Leaking it makes the *next* attempt
    fail for a reason this attempt created — the classic "it works after a
    reboot" bug.
    """
    wired.streams = _audio(monkeypatch, _UnstartableStream)
    owner = object()

    with pytest.raises(RuntimeError, match="Failed to open"):
        ww.start_listening(lambda: None, owner=owner, config={})

    assert len(wired.streams) == 1
    assert wired.streams[0].started is False
    assert wired.streams[0].closed is True, "the half-open stream held the device"
    assert wired.engines[0].closed is True
    assert ww.owns_listener(owner) is False
    assert _lock_is_free(wired.lock_path)


def test_an_engine_that_fails_to_build_releases_the_lease(monkeypatch, wired):
    def _boom(cfg):
        raise RuntimeError("no model on disk")

    monkeypatch.setattr(ww, "_build_engine", _boom)
    owner = object()

    with pytest.raises(RuntimeError, match="no model on disk"):
        ww.start_listening(lambda: None, owner=owner, config={})

    assert ww.owns_listener(owner) is False
    assert ww._detector is None
    assert ww._detector_file_lock is None
    assert _lock_is_free(wired.lock_path)


def test_an_engine_that_raises_on_every_frame_does_not_leak_the_stream(monkeypatch, wired):
    """A broken engine is survivable; a stuck stream is not.

    ``process()`` raising is swallowed per frame by design — one bad frame must
    not end the session — and the loop is still rate-limited by the device read,
    so this must not become a spin. What matters here is that the eventual stop
    still closes everything.
    """
    engine = _CountingEngine(raises=True)
    monkeypatch.setattr(ww, "_build_engine", lambda cfg: engine)
    owner = object()

    ww.start_listening(lambda: None, owner=owner, config={})
    _poll_until(lambda: engine.processed > 3, "the engine to be driven a few times")
    assert ww.is_listening() is True

    assert ww.stop_listening(owner=owner) is True
    assert engine.closed is True
    assert wired.streams[0].closed is True
    assert _lock_is_free(wired.lock_path)


def test_a_raising_wake_callback_does_not_wedge_the_detector(monkeypatch, wired):
    """``_callback_inflight`` must clear even when the callback explodes.

    It is set before the dispatch thread starts and cleared in that thread's
    ``finally``. If an exception skipped the clear, the flag would stay set for
    the life of the session and no later wake would ever be dispatched — a wake
    word that works exactly once.
    """
    calls = []
    done = threading.Event()

    def _on_wake():
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise RuntimeError("callback exploded")
        done.set()

    wired.fire = True
    owner = object()
    detector = ww.start_listening(_on_wake, owner=owner, config={})
    detector.cooldown = 0.0        # the cooldown is not what is under test here

    _poll_until(lambda: len(calls) >= 1, "the first wake callback")
    _poll_until(
        lambda: not detector._callback_inflight.is_set(),
        "the in-flight flag to clear after the callback raised",
    )
    _poll_until(done.is_set, "a second wake after the first callback raised")
    assert len(calls) >= 2
    assert ww.stop_listening(owner=owner) is True


# ── the machine lock ─────────────────────────────────────────────────────────


def test_the_lock_file_is_a_runtime_artifact_not_state(monkeypatch):
    """Where the lease lives matters: it must be re-creatable at any time."""
    path = ww._lock_path()
    assert path.name == "wake-word.lock"
    assert path.parent.name == "runtime"


def _hold_lock_until_killed(path: str, ready) -> None:
    """Child: take the machine lock, announce it, then sleep until killed.

    Never released and never closed on this side — the parent kills the process,
    so the only thing that can free the lease is the operating system tearing
    down the file handle.

    The wait is a plain ``time.sleep``, deliberately, and NOT a
    ``multiprocessing.Event``. Killing a process that is blocked in
    ``Event.wait()`` leaves the shared ``Condition``'s sleeper count
    incremented, and the next ``set()`` from any process then blocks forever
    inside ``notify()`` (``multiprocessing/synchronize.py``) waiting for a
    sleeper that no longer exists. That is a deadlock in the test harness, not a
    finding about the product, and it took a 60 s ``timeout -s INT`` and a
    ``KeyboardInterrupt`` traceback to see. A sleep has no shared state to
    corrupt.
    """
    from tools import wake_word

    handle = wake_word._acquire_machine_lock(Path(path))
    assert handle is not None
    ready.set()
    time.sleep(_SCHEDULING_CEILING_S)


def test_the_machine_lock_is_released_when_the_owning_process_is_killed(tmp_path):
    """A crash must not strand the microphone.

    ``test_wake_word.py`` covers the clean-exit case, where the child's own
    ``finally`` could plausibly be what releases the lease. This one removes
    that possibility: SIGKILL (``TerminateProcess`` on Windows) runs no Python
    at all — no ``finally``, no ``flock(LOCK_UN)``, no ``close()`` — so a lease
    that is free afterwards is one the kernel released with the file handle.
    Anything advisory-but-cooperative, such as a PID written into the lock file,
    would fail here, and the user would be told the wake word is "already owned"
    by a process that no longer exists.
    """
    lock_path = tmp_path / "wake.lock"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(target=_hold_lock_until_killed,
                          args=(str(lock_path), ready))
    process.start()
    try:
        _await_child_event(ready, process, "the child to take the machine lock")

        # Non-vacuity: the lease is genuinely held while the child lives.
        with pytest.raises(ww.WakeWordInUse):
            ww._acquire_machine_lock(lock_path)

        process.kill()
        process.join(_SCHEDULING_CEILING_S)
        assert process.is_alive() is False
        assert process.exitcode is not None, (
            f"the killed child was still running after "
            f"{_SCHEDULING_CEILING_S:.0f}s"
        )
        # The exact code is platform-defined (-SIGKILL on POSIX, a Windows
        # termination code otherwise); what matters is that it is not a clean
        # exit, because a clean exit would mean cooperative cleanup ran and this
        # would be the test that already exists.
        assert process.exitcode != 0, (
            "the child exited cleanly, so this proves nothing about a crash"
        )

        handle = ww._acquire_machine_lock(lock_path)
        ww._release_machine_lock(handle)
    finally:
        # Cleanup must never replace the reported failure — same discipline as
        # test_wake_word.py's lock test.
        cleanup_error = None
        if process.is_alive():
            try:
                process.kill()
                process.join(_SCHEDULING_CEILING_S)
            except BaseException as exc:  # noqa: BLE001 - see above
                cleanup_error = exc
        if cleanup_error is not None and sys.exc_info()[0] is None:
            raise cleanup_error


def test_the_lock_survives_a_reader_that_only_opens_the_file(tmp_path):
    """Opening the lock file must not be mistaken for holding the lease.

    Status surfaces read paths under the runtime directory; an implementation
    that treated existence as ownership would report the wake word as in use
    forever, since the file is never deleted.
    """
    lock_path = tmp_path / "wake.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as spectator:
        assert spectator is not None
        handle = ww._acquire_machine_lock(lock_path)
        ww._release_machine_lock(handle)
    assert lock_path.exists()
    handle = ww._acquire_machine_lock(lock_path)
    ww._release_machine_lock(handle)


# ── no state leakage between sessions ────────────────────────────────────────


def test_a_second_session_starts_with_a_clean_engine_and_detector(wired):
    """stop → start builds everything again; nothing is recycled.

    Scores, feature buffers and confirmation streaks all live on the engine, so
    "no leakage between sessions" reduces to: the engine from session one is
    closed and never touched again, and session two gets its own.
    """
    owner = object()
    first = ww.start_listening(lambda: None, owner=owner, config={})
    assert ww.stop_listening(owner=owner) is True

    second = ww.start_listening(lambda: None, owner=owner, config={})
    try:
        assert second is not first
        assert wired.builds == 2
        assert wired.engines[1] is not wired.engines[0]
        assert wired.engines[0].closed is True
        assert wired.engines[1].closed is False
        assert second.engine is wired.engines[1]
        assert wired.engines[1].resets == 1
    finally:
        assert ww.stop_listening(owner=owner) is True


def test_a_second_session_inherits_no_cooldown(wired):
    """The fire gate must not be armed by the previous session's fire.

    Asserted as state rather than as elapsed time: the gate is
    ``now - _last_fire >= cooldown``, so a fresh detector's ``_last_fire`` of
    0.0 against a monotonic clock is what makes the first utterance of a new
    session audible. A timing-based test would pass on a leaky runtime too, just
    two seconds later.
    """
    wired.fire = True
    fires = []
    owner = object()

    first = ww.start_listening(lambda: fires.append(1), owner=owner, config={})
    _poll_until(lambda: len(fires) >= 1, "the first session to fire")
    assert first._last_fire > 0.0, "the first session did not arm its cooldown"
    assert ww.stop_listening(owner=owner) is True

    second = ww.start_listening(lambda: fires.append(2), owner=owner, config={})
    try:
        assert second._last_fire == 0.0
        assert time.monotonic() - second._last_fire >= second.cooldown, (
            "a fresh session's fire gate is already closed"
        )
        assert second.cooldown == ww._FIRE_COOLDOWN_SECONDS
        _poll_until(lambda: 2 in fires, "the second session to fire")
    finally:
        assert ww.stop_listening(owner=owner) is True


def test_pause_and_resume_deliberately_keep_the_cooldown(wired):
    """Non-vacuity for the test above: the cooldown is not simply always clear.

    Across a pause the detector object survives, and keeping ``_last_fire`` is
    what stops one "hey youtab" from firing again the instant the microphone
    comes back from a voice turn.
    """
    wired.fire = True
    owner = object()
    detector = ww.start_listening(lambda: None, owner=owner, config={})
    _poll_until(lambda: detector._last_fire > 0.0, "the first fire")
    armed = detector._last_fire

    ww.pause_listening(owner=owner)
    ww.resume_listening(owner=owner)
    try:
        assert ww._detector is detector
        assert detector._last_fire == armed, "a resume cleared the cooldown"
    finally:
        assert ww.stop_listening(owner=owner) is True


def test_a_second_session_inherits_no_dead_microphone_verdict(monkeypatch, wired):
    """``audio_silent`` is a property of a stream, not of the machine.

    A muted microphone in session one must not make session two report a dead
    device before it has read a frame — that reading drives a user-visible
    "listening but the microphone appears silent" state.
    """
    monkeypatch.setattr(ww, "_SILENCE_ALERT_SECONDS", 0.001)
    owner = object()
    first = ww.start_listening(lambda: None, owner=owner, config={})
    _poll_until(lambda: first.audio_silent, "the silent-stream flag to trip")
    assert ww.audio_is_silent() is True
    assert ww.stop_listening(owner=owner) is True

    second = ww.start_listening(lambda: None, owner=owner, config={})
    try:
        assert second.audio_silent is False
        assert second._silent_frames == 0
    finally:
        assert ww.stop_listening(owner=owner) is True


def test_a_second_session_takes_the_lease_again_rather_than_keeping_it(wired):
    """Two sessions, two acquisitions — and the lease is free in between."""
    owner = object()
    ww.start_listening(lambda: None, owner=owner, config={})
    assert ww.stop_listening(owner=owner) is True
    assert _lock_is_free(wired.lock_path)

    ww.start_listening(lambda: None, owner=owner, config={})
    try:
        assert wired.order.count("acquire_lock") >= 2
        assert _lock_is_free(wired.lock_path) is False
    finally:
        assert ww.stop_listening(owner=owner) is True


def test_a_second_session_may_be_owned_by_someone_else(wired):
    """Releasing means releasing: a different surface can arm next.

    ``owner`` identity is held by reference, so a stale reference kept anywhere
    would lock the microphone to a dead surface for the life of the process.
    """
    first, second = object(), object()
    ww.start_listening(lambda: None, owner=first, config={})
    assert ww.stop_listening(owner=first) is True

    ww.start_listening(lambda: None, owner=second, config={})
    try:
        assert ww.owns_listener(second) is True
        assert ww.owns_listener(first) is False
    finally:
        assert ww.stop_listening(owner=second) is True


def test_the_environment_this_runs_in_can_signal_its_own_children():
    """A guard on the guard.

    ``tests/conftest.py`` falls back to a static child set when ``psutil`` is
    missing, and then declines to signal anything born after fixture setup — so
    on an interpreter without psutil the kill test above would be inconclusive
    rather than failing. Assert the tool exists instead of hoping.
    """
    import importlib.util

    assert importlib.util.find_spec("psutil") is not None, (
        "psutil is missing, so the live-system guard cannot signal test children "
        "and the process-death lock test cannot be trusted"
    )
    assert hasattr(multiprocessing.get_context("spawn").Process, "kill")
    assert os.name in ("posix", "nt")
