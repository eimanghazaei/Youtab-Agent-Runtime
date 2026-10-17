"""Regression: empty-body HTTP 4xx errors must still surface a real provider message.

Reported on Windows (#36109): an LLM API call returned HTTP 400 with an *empty*
parsed SDK ``body`` ({}), so ``_summarize_api_error`` fell through to the bare
``str(error)`` path and the user saw only "HTTP 400" with no provider detail.
The SDK leaves ``body`` empty in this case, but the underlying httpx
``response`` still carries the real payload in ``.text``. These tests lock the
contract: when ``body`` is empty, fall back to ``response.text`` (parsing a JSON
``error.message`` / ``message`` when present) so logs and CLI show the real
provider error. This is a diagnostic improvement and is platform-agnostic.
"""

from types import SimpleNamespace
from typing import Any

import httpx

from run_agent import AIAgent


def _make_empty_body_error(response_text: str, status_code: int = 400) -> Exception:
    """Mimic an OpenAI-SDK error whose parsed body is empty but whose httpx
    response still holds the payload text."""
    err = Exception("")  # str(error) is empty/uninformative on this path
    err.status_code = status_code
    err.body = {}  # empty dict — the #36109 trigger
    err.response = SimpleNamespace(text=response_text)
    return err


def test_empty_body_falls_back_to_response_json_error_message():
    """A JSON payload with error.message is surfaced (not a bare HTTP 400)."""
    err = _make_empty_body_error(
        '{"error": {"message": "model `foo` does not exist", "type": "invalid_request_error"}}'
    )
    summary = AIAgent._summarize_api_error(err)
    assert "HTTP 400" in summary
    assert "model `foo` does not exist" in summary


def test_provider_error_summary_never_echoes_a_synthetic_key(monkeypatch):
    from agent import redact

    key = "fw-" + "A" * 40
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)

    body_error = _make_empty_body_error("")
    body_error.body = {"error": {"message": f"rejected {key}"}}
    raw_error = RuntimeError(f"provider rejected {key}")

    for error in (body_error, raw_error):
        summary = AIAgent._summarize_api_error(error)
        assert key not in summary
        assert "rejected" in summary


def test_failed_account_refresh_does_not_log_exception_secret(monkeypatch, caplog):
    import youtab_agent_cli.auth as auth

    key = "fw-" + "A" * 40

    def fail_refresh(**_kwargs):
        raise RuntimeError(f"upstream echoed {key}")

    monkeypatch.setattr(auth, "resolve_youtab_runtime_credentials", fail_refresh)
    agent = AIAgent.__new__(AIAgent)
    agent.provider = "youtab"
    agent.api_mode = "chat_completions"

    with caplog.at_level("DEBUG"):
        assert agent._try_refresh_youtab_client_credentials() is False

    assert key not in caplog.text
    assert "RuntimeError" in caplog.text






def test_unread_streaming_response_does_not_crash_and_falls_back_to_exception_message():
    """Unread streaming responses must not replace the real provider error."""

    class _StreamingError(Exception):
        def __init__(self):
            super().__init__("Gemini HTTP 429: quota exceeded")
            self.status_code = 429
            self.response: Any = None

    err = _StreamingError()

    class _UnreadStreamingResponse:
        @property
        def text(self):
            raise httpx.ResponseNotRead()

    err.response = _UnreadStreamingResponse()
    summary = AIAgent._summarize_api_error(err)
    assert "HTTP 429" in summary
    assert "Gemini HTTP 429: quota exceeded" in summary

