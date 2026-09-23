"""Tests for the WAVE-26 shared redaction policy (contract 6)."""

from __future__ import annotations

from youtab_runtime import redaction as R


def test_secret_shaped_keys_are_redacted():
    out = R.redact_mapping(
        {
            "api_key": "sk-abcdefghijklmnop",
            "Authorization": "Bearer zzzzzzzzzzzzzzzzzzzz",
            "password": "hunter2hunter2hunter2",
            "nested": {"client_secret": "shh", "keep": "value"},
            "keep_top": "ok",
        }
    )
    assert out["api_key"] == R.REDACTED
    assert out["Authorization"] == R.REDACTED
    assert out["password"] == R.REDACTED
    assert out["nested"]["client_secret"] == R.REDACTED
    assert out["nested"]["keep"] == "value"
    assert out["keep_top"] == "ok"


def test_inline_secret_scrubbed_from_free_text():
    msg = "call failed with token Bearer abcdefghijklmnopqrstuvwxyz012345"
    assert "abcdefghijklmnop" not in R.scrub_text(msg)
    assert R.REDACTED in R.scrub_text(msg)


def test_long_high_entropy_value_scrubbed():
    secret = "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8S9T0"  # 40 chars
    assert R.REDACTED in R.scrub_text(f"leaked={secret}")


def test_redact_url_drops_query_and_userinfo():
    out = R.redact_url("https://user:pass@example.com:8443/v1/runs/abc123?token=secret")
    assert out["scheme"] == "https"
    assert out["host"] == "example.com"
    assert out["port"] == 8443
    assert out["had_query"] is True
    assert out["had_userinfo"] is True
    # The secret query value must never appear anywhere in the record.
    assert "secret" not in str(out)
    assert "pass" not in str(out)


def test_path_shape_masks_ids():
    out = R.redact_url("https://h/api/runtime/v1/runs/run_9f3a2b1c/events")
    # Numeric/long id-looking segments collapse to :var.
    assert ":var" in out["path_shape"]
    assert "runs" in out["path_shape"]


def test_redact_headers_allowlist_only():
    out = R.redact_headers(
        {
            "Authorization": "Bearer x",
            "Cookie": "sid=1",
            "Content-Type": "application/json",
            "X-Correlation-Id": "corr-1",
        }
    )
    assert "authorization" not in out
    assert "cookie" not in out
    assert out["content-type"] == "application/json"
    assert out["x-correlation-id"] == "corr-1"


def test_redact_result_is_digest_plus_summary_not_raw_secret():
    res = {"api_key": "sk-supersecretvalue123456", "data": "ok"}
    out = R.redact_result(res)
    assert "digest" in out and len(out["digest"]) == 64
    assert "type" in out
    # summary is scrubbed of inline secrets
    assert "supersecretvalue" not in out["summary"]


def test_digest_is_stable_and_order_independent():
    a = R.digest({"x": 1, "y": 2})
    b = R.digest({"y": 2, "x": 1})
    assert a == b


def test_bytes_become_digest_reference():
    out = R.redact_mapping({"blob": b"\x00\x01\x02rawbytes"})
    assert out["blob"]["_len"] == 11  # 3 control bytes + "rawbytes"
    assert len(out["blob"]["_bytes_digest"]) == 64


def test_container_truncation_bounds_size():
    out = R.redact_mapping({"items": list(range(1000))})
    # Truncated to the cap plus a marker.
    assert len(out["items"]) <= R._MAX_ARG_ITEMS + 1
    assert out["items"][-1] == {"_truncated_items": 1000 - R._MAX_ARG_ITEMS}
