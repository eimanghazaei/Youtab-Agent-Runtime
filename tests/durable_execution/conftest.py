"""Shared fixtures for durable-execution tests."""

import pytest

from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture(autouse=True)
def authority_fail_stops(monkeypatch):
    """Record durable run-authority fail-stops instead of exiting the test process.

    Production terminates the process (``os._exit``) when the exclusive run
    authority is lost; a test asserts on this list instead.
    """
    calls = []
    monkeypatch.setattr(
        APIServerAdapter, "_authority_fail_stop", staticmethod(lambda: calls.append(True))
    )
    return calls
