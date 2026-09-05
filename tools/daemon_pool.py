"""Shared daemon-thread ThreadPoolExecutor.

Stdlib ``ThreadPoolExecutor`` workers are non-daemon AND are registered in
``concurrent.futures.thread._threads_queues``, whose atexit hook
(``_python_exit``) joins every worker unconditionally — even after
``shutdown(wait=False)``.  A single wedged worker (tool blocked on network
I/O, hung provider daemon, stuck subagent) therefore blocks interpreter
exit forever.  This is the root cause of multi-minute CLI exits on long
sessions: every abandoned concurrent-tool batch leaves workers that the
exit hook insists on joining.

``DaemonThreadPoolExecutor`` spawns daemon workers and skips the
``_threads_queues`` registration, so:

  - ``_python_exit`` never joins them, and
  - the interpreter's non-daemon thread join at shutdown skips them.

Semantics are otherwise identical (initializer/initargs, work queue,
idle-thread reuse).  Use it for any pool whose work is best-effort or
independently interruptible and must never hold the process open:
concurrent tool execution, background memory sync, catalog fan-out,
subagent timeout wrappers.  Do NOT use it for work that must complete
before exit (durable writes) — those belong on foreground threads with
explicit bounded joins.
"""

from __future__ import annotations

import inspect
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker

__all__ = ["DaemonThreadPoolExecutor"]


def _legacy_worker_contract_supported() -> bool:
    """Return True when CPython exposes the private ThreadPoolExecutor worker
    contract this class reproduces.

    ``_adjust_thread_count`` below reproduces CPython's worker-spawn for
    versions 3.8–3.13: a module-level ``_worker(executor_reference,
    work_queue, initializer, initargs)`` plus ``_initializer``/``_initargs``
    attributes on the executor. CPython 3.14 refactored this into a
    ``_WorkerContext`` and a three-argument ``_worker``, removing those
    attributes — so the legacy path raises ``AttributeError`` deep inside a
    thread spawn. Probing the actual signature (rather than a version number)
    keeps this honest if the internals shift again, and lets the boundary be
    reported with a clear, actionable message.
    """
    try:
        params = list(inspect.signature(_worker).parameters)
    except (TypeError, ValueError):
        return False
    return "initializer" in params and "initargs" in params


_LEGACY_WORKER_CONTRACT_SUPPORTED = _legacy_worker_contract_supported()


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor variant whose workers do not block process exit."""

    def _adjust_thread_count(self) -> None:
        # Mirrors CPython's implementation (3.8–3.13) with two changes:
        # daemon=True and no _threads_queues registration.
        #
        # Enforce the supported-interpreter boundary here, at the point of the
        # actual incompatibility, with a clear message instead of the cryptic
        # AttributeError CPython 3.14 would otherwise raise. The project pins
        # `requires-python = ">=3.11,<3.14"`; running outside that band is
        # unsupported (see docs/ops/WINDOWS_SUPPORT.md).
        if not _LEGACY_WORKER_CONTRACT_SUPPORTED:
            raise RuntimeError(
                "DaemonThreadPoolExecutor relies on the CPython 3.8-3.13 "
                "ThreadPoolExecutor worker internals, which this interpreter "
                f"(Python {sys.version.split()[0]}) does not provide. This "
                "project supports Python >=3.11,<3.14 (see pyproject "
                "requires-python); run it on a supported interpreter."
            )
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            t.start()
            self._threads.add(t)
