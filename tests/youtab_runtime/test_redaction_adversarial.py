"""WAVE-27 adversarial redaction tests (contract 6 hardening).

State-based proofs that the hardened :mod:`youtab_runtime.redaction` policy
catches encoded/prefixed provider secrets (below the old 40-char generic floor),
redacts secrets nested at depth, preserves numeric usage counts and structural
audit evidence at the journal chokepoint, and never crashes on malformed input.

Also documents — honestly, with an asserting test rather than an xfail — the one
residual: redaction is per-value and does NOT reassemble a secret split across
two sibling fields.
"""

from __future__ import annotations

from youtab_runtime import redaction as R


# --------------------------------------------------------------------------- #
# Encoded / prefixed inline secrets — scrub_text                              #
# --------------------------------------------------------------------------- #
# Each entry: (label, secret). Every secret must be removed by scrub_text even
# though several are BELOW the old generic 40-char floor (AWS=20, Google=39),
# use standard-base64 (+ /) the url-safe generic class misses, or are structured
# tokens (JWT / github_pat_ / xoxb-).
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"                      # AKIA + 16 = 20 chars
_GOOGLE_KEY = "AIza" + "Sy8bQ1w3rT5" + "K" * 24  # AIza + 35 = 39 chars total
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
_GITHUB_PAT = "github_pat_11ABCDE0000abcdefghij1234567890"
_SLACK = "xoxb-123456789012-abcdefghijklmnop"
# Standard-base64 blob: contains + and /, 47 chars -> only the base64 branch
# ([A-Za-z0-9+/]{40,}) catches it; the url-safe generic class excludes + and /.
_B64 = "A" * 20 + "+" + "B" * 20 + "/" + "C" * 5
# Generic url-safe high-entropy run (>= 40 chars, no + or /).
_GENERIC = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP1234"


def _assert_scrubbed(secret: str) -> None:
    out = R.scrub_text(f"downstream error, leaked={secret} end")
    assert R.REDACTED in out, out
    assert secret not in out, (secret, out)


def test_aws_access_key_id_scrubbed_below_generic_floor():
    assert len(_AWS_KEY) == 20  # below the old 40-char generic floor
    _assert_scrubbed(_AWS_KEY)


def test_google_api_key_scrubbed_below_generic_floor():
    assert len(_GOOGLE_KEY) == 39  # 4 + 35, below the old 40-char floor
    _assert_scrubbed(_GOOGLE_KEY)


def test_jwt_scrubbed_even_without_bearer_prefix():
    _assert_scrubbed(_JWT)


def test_github_fine_grained_pat_scrubbed():
    _assert_scrubbed(_GITHUB_PAT)


def test_slack_bot_token_scrubbed():
    _assert_scrubbed(_SLACK)


def test_standard_base64_blob_with_plus_and_slash_scrubbed():
    assert "+" in _B64 and "/" in _B64 and len(_B64) >= 40
    _assert_scrubbed(_B64)


def test_generic_high_entropy_token_scrubbed():
    assert len(_GENERIC) >= 40
    _assert_scrubbed(_GENERIC)


# --------------------------------------------------------------------------- #
# Nested / recursive redaction                                                 #
# --------------------------------------------------------------------------- #
def test_secret_nested_at_depth_is_dropped_by_redact_mapping():
    payload = {
        "outer": {
            "list": [
                {"harmless": "ok"},
                {"nested": {"api_key": "sk-" + _GENERIC}},
            ]
        }
    }
    out = R.redact_mapping(payload)
    assert out["outer"]["list"][1]["nested"]["api_key"] == R.REDACTED
    assert _GENERIC not in str(out)


def test_secret_nested_at_depth_is_dropped_by_journal_payload():
    payload = {"a": {"b": {"c": [{"authorization": "Bearer " + _GENERIC}]}}}
    out = R.redact_journal_payload(payload)
    assert out["a"]["b"]["c"][0]["authorization"] == R.REDACTED
    assert _GENERIC not in str(out)


# --------------------------------------------------------------------------- #
# Mixed-case secret keys                                                       #
# --------------------------------------------------------------------------- #
def test_mixed_case_secret_keys_all_redacted():
    payload = {
        "Authorization": "Bearer aaaaaaaaaaaaaaaaaaaa",
        "AUTHORIZATION": "Bearer bbbbbbbbbbbbbbbbbbbb",
        "authorization": "Bearer cccccccccccccccccccc",
    }
    for fn in (R.redact_mapping, R.redact_journal_payload):
        out = fn(payload)
        assert out["Authorization"] == R.REDACTED
        assert out["AUTHORIZATION"] == R.REDACTED
        assert out["authorization"] == R.REDACTED


# --------------------------------------------------------------------------- #
# Journal chokepoint: numeric preserved, structural evidence preserved         #
# --------------------------------------------------------------------------- #
def test_numeric_under_secret_shaped_key_is_preserved():
    # "input_tokens" contains the substring "token" -> secret-shaped key, but a
    # numeric value is a usage COUNT and must survive; the string secret is gone.
    payload = {"input_tokens": 512, "api_key": "sk-" + _GENERIC}
    out = R.redact_journal_payload(payload)
    assert out["input_tokens"] == 512
    assert isinstance(out["input_tokens"], int)
    assert out["api_key"] == R.REDACTED
    assert _GENERIC not in str(out)


def test_structural_digest_value_kept_verbatim_as_evidence():
    # A 64-hex digest is shape-identical to a high-entropy secret, but under the
    # structural key "digest" it is audit evidence and must survive verbatim.
    digest_hex = "a" * 64
    out = R.redact_journal_payload({"digest": digest_hex})
    assert out["digest"] == digest_hex


def test_structural_suffix_keys_kept_verbatim():
    long_id = "run-" + "z" * 60  # would trip the generic scrubber as free text
    out = R.redact_journal_payload({"target_scope_digest": long_id,
                                    "provider_idempotency_key": long_id})
    assert out["target_scope_digest"] == long_id
    assert out["provider_idempotency_key"] == long_id


# --------------------------------------------------------------------------- #
# redact_result — secret VALUE under secret KEY dropped from summary           #
# --------------------------------------------------------------------------- #
def test_redact_result_drops_short_keyed_secret_from_summary():
    out = R.redact_result({"password": "hunter2", "data": "ok"})
    assert "hunter2" not in out["summary"]
    assert len(out["digest"]) == 64


# --------------------------------------------------------------------------- #
# URL + header redaction                                                       #
# --------------------------------------------------------------------------- #
def test_redact_url_drops_query_and_userinfo():
    out = R.redact_url(
        "https://alice:s3cr3tpw@api.example.com:8443/v1/runs/abc?token=leakme"
    )
    assert out["scheme"] == "https"
    assert out["host"] == "api.example.com"
    assert out["had_query"] is True
    assert out["had_userinfo"] is True
    blob = str(out)
    assert "leakme" not in blob
    assert "s3cr3tpw" not in blob


def test_redact_headers_drops_authorization_and_cookie():
    out = R.redact_headers({
        "Authorization": "Bearer " + _GENERIC,
        "Cookie": "session=" + _GENERIC,
        "Content-Type": "application/json",
    })
    assert "authorization" not in out
    assert "cookie" not in out
    assert out["content-type"] == "application/json"
    assert _GENERIC not in str(out)


# --------------------------------------------------------------------------- #
# Malformed / partial input is bounded and never crashes                       #
# --------------------------------------------------------------------------- #
def test_malformed_and_oversized_input_is_bounded_and_safe():
    # A truncated/garbage secret-shaped fragment must not raise and stays bounded.
    junk = "sk-" + ("!" * 5)  # too short / non-matching -> passes through as-is
    assert isinstance(R.scrub_text(junk), str)

    huge = "x" * 10000
    scrubbed = R.scrub_text(huge)
    assert len(scrubbed) <= R._MAX_SUMMARY_CHARS + len("…[truncated]")

    # Mixed / partial structures must not crash any of the entrypoints.
    weird = {"token": None, "n": 3, "b": b"\x00\x01", "lst": [None, {"secret": ""}]}
    for fn in (R.redact_mapping, R.redact_journal_payload, R.redact_result):
        assert fn(weird) is not None


# --------------------------------------------------------------------------- #
# RESIDUAL (honest): a secret split across two fields is NOT reassembled        #
# --------------------------------------------------------------------------- #
def test_secret_split_across_two_fields_is_not_reassembled():
    """Redaction is per-value: a secret split over two sibling fields is judged
    half-by-half. Each half is below every pattern's threshold, so neither is
    redacted and — critically — the two halves are never concatenated and matched
    as one. This asserts the CURRENT honest behavior (documented residual), not a
    desired one: cross-field reassembly is out of scope for a per-value scrubber.
    """
    whole = _GENERIC  # 45 chars: would be redacted if seen as one value
    half1, half2 = whole[:20], whole[20:]  # 20 + 25, each below the 40 floor
    # Sanity: each half on its own is not caught by the inline scrubber.
    assert R.scrub_text(half1) == half1
    assert R.scrub_text(half2) == half2

    payload = {"part_one": half1, "part_two": half2}  # non-secret keys
    out = R.redact_journal_payload(payload)
    # Each half survives verbatim; the whole secret is never reconstructed.
    assert out["part_one"] == half1
    assert out["part_two"] == half2
    # The scrubber never joined them into the redactable whole.
    assert whole not in str(out)
