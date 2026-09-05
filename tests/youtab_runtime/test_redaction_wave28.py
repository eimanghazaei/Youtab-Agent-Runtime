"""WAVE-28 §6.6 redaction hardening — adversarial state-based proofs.

Each control below has a test that FAILS if the control is removed. Items map to
the WAVE-28 task list:

  1. safe-verbatim keys cannot smuggle a credential-shaped value;
  2. numeric usage counts under secret-shaped keys survive (regression);
  3. secrets nested in lists/dicts/tuples at depth are redacted;
  4. base64/long-hex encoded credential material is caught (with the honest
     entropy-floor limit documented);
  5. split secrets are NOT reassembled (documented residual);
  6. Authorization Bearer/Basic redacted in header dict, string and mapping value;
  7. Cookie / Set-Cookie values redacted;
  8. URL userinfo + sensitive query params scrubbed (structured and in free text);
  9. secrets in exception str()/objects scrubbed through the redaction path;
 10. secrets in tool-call args and tool results are redacted.
"""

from __future__ import annotations

from youtab_runtime import redaction as R

# High-entropy fixtures reused across items.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"                       # AKIA + 16 = 20 chars
_GOOGLE_KEY = "AIza" + "Sy8bQ1w3rT5" + "K" * 24         # AIza + 35 = 39 chars
_GENERIC = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP1234"  # 45, no + /
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
# base64("user:password") == "dXNlcjpwYXNzd29yZA==" (20 chars incl padding).
_BASIC_B64 = "dXNlcjpwYXNzd29yZA=="


# --------------------------------------------------------------------------- #
# Item 1 — safe-verbatim keys must not become a bypass for real credentials    #
# --------------------------------------------------------------------------- #
def test_aws_key_under_structural_id_key_is_redacted():
    # "session_id" ends with "_id" -> safe-verbatim, but an AWS key is a
    # credential and must NOT be kept verbatim.
    out = R.redact_journal_payload({"session_id": _AWS_KEY})
    assert out["session_id"] == R.REDACTED
    assert _AWS_KEY not in str(out)


def test_bearer_token_under_hash_key_is_redacted():
    out = R.redact_journal_payload({"user_hash": "Bearer " + _GENERIC})
    assert out["user_hash"] == R.REDACTED
    assert _GENERIC not in str(out)


def test_jwt_under_digest_key_is_redacted():
    out = R.redact_journal_payload({"payload_digest": _JWT})
    assert out["payload_digest"] == R.REDACTED
    assert _JWT not in str(out)


def test_google_key_under_explicit_safe_verbatim_key_is_redacted():
    # "correlation_id" is an explicit member of _SAFE_VERBATIM_KEYS.
    out = R.redact_journal_payload({"correlation_id": _GOOGLE_KEY})
    assert out["correlation_id"] == R.REDACTED


def test_genuine_hex_digest_still_kept_verbatim_under_structural_key():
    d = "a" * 64  # shape-identical to a high-entropy secret, but real evidence
    out = R.redact_journal_payload({"result_digest": d})
    assert out["result_digest"] == d


def test_uuid_under_id_key_kept_verbatim():
    u = "550e8400-e29b-41d4-a716-446655440000"
    out = R.redact_journal_payload({"trace_id": u})
    assert out["trace_id"] == u


# --------------------------------------------------------------------------- #
# Item 2 — numeric usage counts under secret-shaped keys survive (regression)  #
# --------------------------------------------------------------------------- #
def test_numeric_usage_under_secret_shaped_keys_preserved():
    payload = {"api_key_calls": 42, "total_tokens": 1000, "refresh_token_uses": 3}
    out = R.redact_journal_payload(payload)
    assert out["api_key_calls"] == 42
    assert out["total_tokens"] == 1000
    assert out["refresh_token_uses"] == 3
    for v in out.values():
        assert isinstance(v, int)


# --------------------------------------------------------------------------- #
# Item 3 — secrets nested in lists/dicts/tuples at depth                        #
# --------------------------------------------------------------------------- #
def test_secret_in_tuple_nested_is_redacted():
    payload = {"outer": ({"inner": [{"token": "Bearer " + _GENERIC}]},)}
    out = R.redact_journal_payload(payload)
    assert _GENERIC not in str(out)
    # tuple is normalised to a list; the secret key is redacted at depth.
    assert out["outer"][0]["inner"][0]["token"] == R.REDACTED


def test_secret_in_deep_list_of_dicts_redacted_by_mapping():
    payload = {"a": [[{"b": {"api_key": "sk-" + _GENERIC}}]]}
    out = R.redact_mapping(payload)
    assert out["a"][0][0]["b"]["api_key"] == R.REDACTED
    assert _GENERIC not in str(out)


# --------------------------------------------------------------------------- #
# Item 4 — encoded credential material                                         #
# --------------------------------------------------------------------------- #
def test_long_base64_blob_caught_in_free_text():
    blob = "A" * 20 + "+" + "B" * 20 + "/" + "C" * 5  # 47 chars, standard b64
    out = R.scrub_text(f"payload={blob}")
    assert blob not in out and R.REDACTED in out


def test_long_hex_credential_caught_in_free_text():
    hex_secret = "deadbeefcafef00d" * 4  # 64 hex chars, >= 40 generic floor
    out = R.scrub_text(f"key={hex_secret}")
    assert hex_secret not in out


def test_short_hex_credential_under_entropy_floor_is_documented_limit():
    # HONEST LIMIT: a 32-char hex value with no vendor prefix, under a NON-secret
    # non-structural key / in free text, is below the 40-char generic floor and
    # is NOT scrubbed. Lowering the floor would false-positive on ids/digests.
    short_hex = "deadbeef" * 4  # 32 chars
    assert short_hex in R.scrub_text(f"value={short_hex}")


# --------------------------------------------------------------------------- #
# Item 5 — split secrets are not reassembled (documented residual)             #
# --------------------------------------------------------------------------- #
def test_split_secret_fragments_handled_per_value_not_reassembled():
    half1, half2 = _GENERIC[:20], _GENERIC[20:]  # each below the generic floor
    assert R.scrub_text(half1) == half1
    assert R.scrub_text(half2) == half2
    out = R.redact_journal_payload({"part_one": half1, "part_two": half2})
    assert out["part_one"] == half1 and out["part_two"] == half2
    assert _GENERIC not in str(out)  # never concatenated + matched as one


# --------------------------------------------------------------------------- #
# Item 6 — Authorization Bearer / Basic                                        #
# --------------------------------------------------------------------------- #
def test_basic_auth_scrubbed_in_free_text():
    out = R.scrub_text(f"auth failed: Basic {_BASIC_B64}")
    assert _BASIC_B64 not in out and R.REDACTED in out


def test_bearer_auth_scrubbed_in_free_text():
    out = R.scrub_text(f"auth failed: Bearer {_GENERIC}")
    assert _GENERIC not in out


def test_authorization_value_redacted_in_mapping():
    for fn in (R.redact_mapping, R.redact_journal_payload):
        out = fn({"Authorization": "Basic " + _BASIC_B64})
        assert out["Authorization"] == R.REDACTED


def test_authorization_dropped_from_headers_basic_and_bearer():
    out = R.redact_headers({
        "Authorization": "Basic " + _BASIC_B64,
        "Content-Type": "application/json",
    })
    assert "authorization" not in out
    assert out["content-type"] == "application/json"
    assert _BASIC_B64 not in str(out)


# --------------------------------------------------------------------------- #
# Item 7 — Cookie / Set-Cookie                                                 #
# --------------------------------------------------------------------------- #
def test_set_cookie_dropped_from_headers():
    out = R.redact_headers({"Set-Cookie": "sid=" + _GENERIC + "; HttpOnly"})
    assert "set-cookie" not in out
    assert _GENERIC not in str(out)


def test_cookie_key_redacted_in_mapping():
    for fn in (R.redact_mapping, R.redact_journal_payload):
        out = fn({"cookie": "sid=" + _GENERIC})
        assert out["cookie"] == R.REDACTED


# --------------------------------------------------------------------------- #
# Item 8 — URLs carrying credentials                                           #
# --------------------------------------------------------------------------- #
def test_redact_url_structured_drops_userinfo_and_query():
    out = R.redact_url(
        "https://alice:s3cr3tpw@api.example.com:8443/v1/runs/abc?token=leakme"
    )
    assert out["host"] == "api.example.com"
    assert out["had_userinfo"] is True and out["had_query"] is True
    blob = str(out)
    assert "s3cr3tpw" not in blob and "leakme" not in blob


def test_url_userinfo_scrubbed_in_free_text_host_stays_legible():
    out = R.scrub_text("GET https://alice:s3cr3tpw@api.example.com/v1 -> 500")
    assert "s3cr3tpw" not in out
    assert "api.example.com" in out  # host remains legible


def test_url_sensitive_query_params_scrubbed_in_free_text():
    out = R.scrub_text(
        "redirect https://h/cb?token=abc123&api_key=xyz789&sig=deadbeef&user=bob"
    )
    assert "abc123" not in out
    assert "xyz789" not in out
    assert "deadbeef" not in out
    assert "user=bob" in out   # non-sensitive param preserved
    assert "cb" in out         # path stays legible


# --------------------------------------------------------------------------- #
# Item 9 — exception messages                                                  #
# --------------------------------------------------------------------------- #
def test_exception_str_scrubbed_via_redact_error():
    exc = RuntimeError(f"upstream Bearer {_GENERIC} rejected")
    assert _GENERIC not in R.redact_error(str(exc))


def test_exception_object_scrubbed_via_redact_mapping_and_result():
    exc = RuntimeError(f"cred {_AWS_KEY} rejected")
    assert _AWS_KEY not in str(R.redact_mapping({"error": exc}))
    assert _AWS_KEY not in str(R.redact_result(exc))


# --------------------------------------------------------------------------- #
# Item 10 — tool arguments and outputs                                        #
# --------------------------------------------------------------------------- #
def test_tool_args_nested_secret_redacted():
    out = R.redact_tool_args(
        "http_get",
        {"url": "https://h/x", "headers": {"Authorization": "Bearer " + _GENERIC}},
    )
    assert out["args"]["headers"]["Authorization"] == R.REDACTED
    assert _GENERIC not in str(out)


def test_tool_result_structured_secret_dropped_from_summary():
    out = R.redact_result({"stdout": "ok", "env": {"API_KEY": "sk-" + _GENERIC}})
    assert _GENERIC not in out["summary"]
    assert len(out["digest"]) == 64
