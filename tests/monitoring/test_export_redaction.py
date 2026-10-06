"""Export redaction tests — the security-critical layer.

Invariants:
  * One unconditional scrub: secrets AND PII, no modes, no knobs.
  * Fails CLOSED: if the redactor can't run, the raw string is never emitted.
  * Structure (subsystem names, error codes) survives; free-text PII does not.
"""

from __future__ import annotations

from unittest import mock

import agent.monitoring.redaction as R


def test_secret_key_always_stripped():
    fake_key = "sk-ant-api03-" + "A" * 24  # constructed to dodge literal-scrubbers
    out = R.redact_for_export(f"calling with key {fake_key} and moving on")
    assert out is not None
    assert fake_key not in out




def test_bearer_header_stripped():
    out = R.redact_for_export("Authorization: Bearer abc.def-ghi_jkl")
    assert out is not None
    assert "abc.def-ghi_jkl" not in out








def test_structure_preserved():
    out = R.redact_for_export("platform.slack entered fatal after auth_failed")
    assert out is not None
    assert "platform.slack" in out
    assert "auth_failed" in out


def test_fails_closed_when_redactor_unavailable():
    with mock.patch("agent.redact.redact_sensitive_text", side_effect=RuntimeError):
        out = R.redact_for_export("secret sauce sk-live-key")
    assert out == "[redaction-unavailable]"


def test_email_addresses_are_still_redacted():
    for address in (
        "a.b+tag@example.co.uk",
        "x@y.zz",
        "USER_99%adm@mail-server.example.org",
        "first.last@sub.domain.example.com",
    ):
        out = R.redact_for_export(f"mail {address} now")
        assert out is not None
        assert address not in out
        assert "[email]" in out


def test_pii_scrub_stays_cheap_on_adversarial_input():
    """``redact_for_export`` runs on every string leaving the process, tool and
    model output included, so its patterns must stay linear. An unbounded ``+``
    before the required ``@`` made ``sub`` quadratic: ~3.5 s of CPU for a 64 KB
    string. Budget is far above the linear cost and far below the quadratic one,
    so this fails loudly on a regression without flaking on a slow runner."""
    import time

    payload = "a" * 32768 + "@" + "b" * 32768
    started = time.perf_counter()
    R.redact_for_export(payload)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"redact_for_export took {elapsed:.2f}s on {len(payload)} chars"
