"""Real-background-loop MCP tests split out of test_mcp_tool.py.

These tests each spin a REAL asyncio event loop on a background thread (via
``loop.run_forever`` or ``_ensure_mcp_loop``) and drive cross-thread cancel /
drain / shutdown paths with 30s wall-clock ceilings. Under CI's ``-j3`` the
ProactorEventLoop's cross-thread cancellation lag grows under contention, so
when they lived in the ~80-test ``test_mcp_tool.py`` the whole FILE occasionally
exceeded its per-file wall-clock budget. Splitting them into this sibling file
keeps each file's real-loop count small without touching any ceiling or adding
retries. Test logic is unchanged from the original.

All tests use mocks -- no real MCP servers or subprocesses are started.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


class TestRunOnMCPLoopInterrupts:
    @staticmethod
    def _run_with_future(mcp_mod, future):
        loop = MagicMock()
        loop.is_running.return_value = True

        async def _unused_call():
            return "unused"

        def _schedule(coro, scheduled_loop, **_kwargs):
            assert scheduled_loop is loop
            coro.close()
            return future

        with patch.object(mcp_mod, "_mcp_loop", loop):
            with patch("agent.async_utils.safe_schedule_threadsafe", side_effect=_schedule):
                return mcp_mod._run_on_mcp_loop(_unused_call(), timeout=1)

    def test_interrupt_cancels_waiting_mcp_call(self):
        import tools.mcp_tool as mcp_mod
        from tools.interrupt import set_interrupt

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        cancelled = threading.Event()

        async def _slow_call():
            try:
                await asyncio.sleep(5)
                return "done"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        old_loop = mcp_mod._mcp_loop
        old_thread = mcp_mod._mcp_thread
        mcp_mod._mcp_loop = loop
        mcp_mod._mcp_thread = thread

        waiter_tid = threading.current_thread().ident

        def _interrupt_soon():
            time.sleep(0.02)
            set_interrupt(True, waiter_tid)

        interrupter = threading.Thread(target=_interrupt_soon, daemon=True)
        interrupter.start()

        try:
            with pytest.raises(InterruptedError, match="User sent a new message"):
                mcp_mod._run_on_mcp_loop(_slow_call(), timeout=10)

            # Generous ceiling: the CancelledError must propagate to the slow
            # call, but cross-thread asyncio scheduling can lag well past 2s
            # under -j3 CPU contention on the CI runner (the assertion is
            # unchanged — this only absorbs scheduling latency, it is not a
            # fixed sleep and does not reduce concurrency).
            deadline = time.time() + 30
            while time.time() < deadline and not cancelled.is_set():
                time.sleep(0.01)
            assert cancelled.is_set()
        finally:
            set_interrupt(False, waiter_tid)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            loop.close()
            mcp_mod._mcp_loop = old_loop
            mcp_mod._mcp_thread = old_thread

    def test_timeout_reports_elapsed_and_configured_timeout(self):
        import tools.mcp_tool as mcp_mod

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        cancelled = threading.Event()

        async def _slow_call():
            try:
                await asyncio.sleep(5)
                return "done"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        old_loop = mcp_mod._mcp_loop
        old_thread = mcp_mod._mcp_thread
        mcp_mod._mcp_loop = loop
        mcp_mod._mcp_thread = thread

        try:
            # 0.1s is the floor the MCP loop clamps short timeouts to.
            with pytest.raises(TimeoutError, match=r"MCP call timed out after .*configured timeout: 0.1s"):
                mcp_mod._run_on_mcp_loop(_slow_call(), timeout=0.1)

            # Generous ceiling: the CancelledError must propagate to the slow
            # call, but cross-thread asyncio scheduling can lag well past 2s
            # under -j3 CPU contention on the CI runner (the assertion is
            # unchanged — this only absorbs scheduling latency, it is not a
            # fixed sleep and does not reduce concurrency).
            deadline = time.time() + 30
            while time.time() < deadline and not cancelled.is_set():
                time.sleep(0.01)
            assert cancelled.is_set()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            loop.close()
            mcp_mod._mcp_loop = old_loop
            mcp_mod._mcp_thread = old_thread

# ---------------------------------------------------------------------------
# Tool registration (discovery + register)
# ---------------------------------------------------------------------------


class TestShutdown:

    def test_shutdown_drains_parked_server_after_bounded_wait_expires(self):
        """The public shutdown path drains a parked server if graceful shutdown stalls.

        This exercises the production ownership path: a real ``MCPServerTask``
        is registered, its parked waiter owns child tasks on the shared loop,
        and the bounded wait for the scheduled shutdown expires. The loop owner
        must still cancel and drain that waiter before closing the loop.
        """
        import tools.mcp_tool as mcp_mod
        from tools.mcp_tool import MCPServerTask, shutdown_mcp_servers

        shutdown_started = threading.Event()
        parked_task_done = threading.Event()
        scheduled_shutdown = {}
        schedule_count = 0

        class StalledShutdownServer(MCPServerTask):
            async def shutdown(self):
                shutdown_started.set()
                await asyncio.Event().wait()

        server = StalledShutdownServer("parked")
        with mcp_mod._lock:
            mcp_mod._servers.clear()
            mcp_mod._server_connecting.clear()
        mcp_mod._ensure_mcp_loop()
        with mcp_mod._lock:
            loop = mcp_mod._mcp_loop
        assert loop is not None

        async def install_parked_waiter():
            task = asyncio.create_task(server._wait_for_reconnect_or_shutdown())
            server._task = task
            task.add_done_callback(lambda _task: parked_task_done.set())
            await asyncio.sleep(0)
            return task

        parked_task = asyncio.run_coroutine_threadsafe(
            install_parked_waiter(), loop
        ).result(timeout=2)
        with mcp_mod._lock:
            mcp_mod._servers[server.name] = server

        def schedule_then_report_timeout(coro, target_loop, **_kwargs):
            nonlocal schedule_count
            schedule_count += 1
            future = asyncio.run_coroutine_threadsafe(coro, target_loop)
            if schedule_count > 1:
                return future

            scheduled_shutdown["future"] = future

            class TimedOutFuture:
                def result(self, timeout):
                    assert timeout > 0
                    assert shutdown_started.wait(timeout=2)
                    raise TimeoutError("simulated bounded MCP shutdown timeout")

            return TimedOutFuture()

        try:
            with patch(
                "agent.async_utils.safe_schedule_threadsafe",
                side_effect=schedule_then_report_timeout,
            ):
                shutdown_mcp_servers()

            assert loop.is_closed()
            assert parked_task_done.is_set(), (
                "parked MCPServerTask was not drained before its loop closed"
            )
            assert parked_task.done()
            assert scheduled_shutdown["future"].done()
            assert schedule_count == 2
        finally:
            with mcp_mod._lock:
                mcp_mod._servers.clear()
                mcp_mod._server_connecting.clear()
            mcp_mod._stop_mcp_loop()

    def test_shutdown_deregisters_registered_tools(self):
        """shutdown_mcp_servers removes MCP tools and their raw alias."""
        import tools.mcp_tool as mcp_mod
        from tools.mcp_tool import MCPServerTask, shutdown_mcp_servers, _servers
        from tools.registry import registry
        from toolsets import resolve_toolset, validate_toolset

        _servers.clear()
        registry.register(
            name="mcp__test__ping",
            toolset="mcp-test",
            schema={
                "name": "mcp__test__ping",
                "description": "Ping",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda *_args, **_kwargs: "{}",
        )
        registry.register_toolset_alias("test", "mcp-test")

        server = MCPServerTask("test")
        server._registered_tool_names = ["mcp__test__ping"]
        _servers["test"] = server

        mcp_mod._ensure_mcp_loop()
        try:
            assert validate_toolset("test") is True
            assert "mcp__test__ping" in resolve_toolset("test")
            shutdown_mcp_servers()
        finally:
            mcp_mod._mcp_loop = None
            mcp_mod._mcp_thread = None

        assert "mcp__test__ping" not in registry.get_all_tool_names()
        assert validate_toolset("test") is False

    def test_shutdown_is_parallel(self):
        """Multiple servers are shut down in parallel via asyncio.gather."""
        import tools.mcp_tool as mcp_mod
        from tools.mcp_tool import shutdown_mcp_servers, _servers
        import time

        _servers.clear()

        # 4 servers each taking `delay` to shut down. `delay` is generous so the
        # parallel-vs-serial signal survives -j3 scheduler jitter: serial would
        # be ~4*delay, parallel ~1*delay, and the assertion sits between them.
        delay = 0.5
        for i in range(4):
            mock_server = MagicMock()
            mock_server.name = f"srv_{i}"
            async def slow_shutdown():
                await asyncio.sleep(delay)
            mock_server.shutdown = slow_shutdown
            _servers[f"srv_{i}"] = mock_server

        mcp_mod._ensure_mcp_loop()
        try:
            start = time.monotonic()
            shutdown_mcp_servers()
            elapsed = time.monotonic() - start
        finally:
            mcp_mod._mcp_loop = None
            mcp_mod._mcp_thread = None

        assert len(_servers) == 0
        # Parallel is ~1*delay; serial would be ~4*delay. Assert comfortably
        # below the serial total (a serial regression fails) while leaving ample
        # headroom above 1*delay for -j3 scheduler jitter.
        assert elapsed < delay * 4 * 0.6, (
            f"Shutdown took {elapsed:.3f}s, expected ~{delay}s parallel "
            f"(serial would be ~{delay * 4:.2f}s)"
        )


# ---------------------------------------------------------------------------
# _build_safe_env
# ---------------------------------------------------------------------------
