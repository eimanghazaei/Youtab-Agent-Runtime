"""CXR-01: canonical HMAC v2 contract parity — the cross-repo conformance oracle.

The canonical-v2 signature string binds EXACTLY these 11 fields, in this order
(``runtime_command_auth.canonical_string_v2``):

    1. protocol version   (``youtab.runtime-sig.v2``)
    2. method
    3. path
    4. tenant
    5. workspace          (SERVER-DERIVED; ``-`` when unscoped)
    6. user
    7. timestamp
    8. nonce
    9. body SHA-256 (hex)
   10. correlation id
   11. key id

This module is the shared source of truth the AI-OS connector must reproduce
byte-for-byte. It asserts:

* a committed GOLDEN VECTOR: fixed inputs -> a fixed canonical STRING (exact
  bytes) -> a fixed HMAC signature (so any drift on either repo is caught);
* a PER-FIELD negative: mutating EACH of the 11 signed fields individually, one
  at a time, makes ``verify_command`` reject the request (the signature no longer
  matches / the protocol is refused) — proving every field is genuinely bound.

Pure unit tests (no server), fast, credential-free.
"""
from __future__ import annotations

import hashlib
import time

import pytest

from youtab_agent_cli import runtime_command_auth as rca

# ── committed golden vector (TEST-ONLY values; cross-repo conformance oracle) ──
GOLDEN_SECRET = "test-secret-do-not-use-in-prod-000000000000000"  # len 46
GOLDEN_PROTO = rca.CANONICAL_V2
GOLDEN_METHOD = "POST"
GOLDEN_PATH = "/api/runtime/v1/runs"
GOLDEN_TENANT = "tenant-alpha"
GOLDEN_WORKSPACE = "ws-default"
GOLDEN_USER = "user-eiman"
GOLDEN_TS = "1700000000"
GOLDEN_NONCE = "n-0123456789abcdef0123456789abcdef"
GOLDEN_BODY = b'{"agent":"default","task":"hello"}'
GOLDEN_CORR = "cid-0123456789abcdef"
GOLDEN_KEYID = "svc-hmac-2026a"

# The EXACT canonical string (11 fields joined by "\n"). If either repo changes
# the field set, order, or separators, this literal breaks — by design.
GOLDEN_BODY_HASH = hashlib.sha256(GOLDEN_BODY).hexdigest()
GOLDEN_CANONICAL = "\n".join([
    GOLDEN_PROTO,
    GOLDEN_METHOD,
    GOLDEN_PATH,
    GOLDEN_TENANT,
    GOLDEN_WORKSPACE,
    GOLDEN_USER,
    GOLDEN_TS,
    GOLDEN_NONCE,
    GOLDEN_BODY_HASH,
    GOLDEN_CORR,
    GOLDEN_KEYID,
])
GOLDEN_HMAC = "d2f948992594ac90d88cf16b2feacbfb9eee20629d4a9c3affc4af08c6d573c0"


def _canonical():
    return rca.canonical_string_v2(
        method=GOLDEN_METHOD, path=GOLDEN_PATH, tenant=GOLDEN_TENANT,
        workspace=GOLDEN_WORKSPACE, user=GOLDEN_USER, timestamp=GOLDEN_TS,
        nonce=GOLDEN_NONCE, body=GOLDEN_BODY, correlation=GOLDEN_CORR,
        key_id=GOLDEN_KEYID,
    )


# ── golden vector: fixed inputs -> fixed canonical string + signature ─────────


def test_canonical_v2_exact_string_bytes():
    """The builder emits the EXACT 11-field canonical string, byte-for-byte."""
    canonical = _canonical()
    assert canonical == GOLDEN_CANONICAL
    # Structural: exactly 11 fields, protocol first, key id last.
    lines = canonical.split("\n")
    assert len(lines) == 11
    assert lines[0] == rca.CANONICAL_V2
    assert lines[4] == GOLDEN_WORKSPACE   # server-derived workspace is field 5
    assert lines[8] == GOLDEN_BODY_HASH   # body SHA-256 is field 9
    assert lines[10] == GOLDEN_KEYID      # key id is field 11


def test_canonical_v2_signature_matches_golden():
    assert rca.compute_signature(GOLDEN_SECRET, _canonical()) == GOLDEN_HMAC


def test_verify_command_accepts_golden_v2():
    # A non-raising return IS the acceptance proof: the presented signature had to
    # equal the golden HMAC, which only matches if the verify-side canonical v2
    # string (incl. workspace + key_id) is byte-identical to the signer's.
    store = rca._MemoryNonceStore()
    rca.verify_command(
        method=GOLDEN_METHOD, path=GOLDEN_PATH, tenant=GOLDEN_TENANT,
        workspace=GOLDEN_WORKSPACE, user=GOLDEN_USER, body=GOLDEN_BODY,
        headers={
            rca.SIGNATURE_HEADER: GOLDEN_HMAC,
            rca.TIMESTAMP_HEADER: GOLDEN_TS,
            rca.NONCE_HEADER: GOLDEN_NONCE,
            rca.CORRELATION_HEADER: GOLDEN_CORR,
            rca.PROTO_HEADER: rca.CANONICAL_V2,
            rca.KEYID_HEADER: GOLDEN_KEYID,
        },
        secret=GOLDEN_SECRET, store=store, now=int(GOLDEN_TS),
    )


# ── per-field negative: mutate EACH of the 11 signed fields individually ──────
#
# Each case presents the GOLDEN signature (computed over the unmutated fields)
# but flips ONE field on the verify side. A correctly-bound field makes the
# verify-side canonical differ, so the HMAC compare fails (bad_signature) — or,
# for the protocol version, the request is refused outright. Any field that did
# NOT change the digest would (wrongly) still verify, so a passing case here
# proves that field is genuinely part of the signed contract.
#
# ``now`` is pinned to the signed timestamp with the default 300s window so a
# +1s timestamp mutation stays INSIDE the freshness window and is therefore
# caught by the SIGNATURE (bad_signature), isolating "field is signed" from the
# separate "expired" freshness check.

_BASE_KW = dict(
    method=GOLDEN_METHOD, path=GOLDEN_PATH, tenant=GOLDEN_TENANT,
    workspace=GOLDEN_WORKSPACE, user=GOLDEN_USER, body=GOLDEN_BODY,
    secret=GOLDEN_SECRET, now=int(GOLDEN_TS),
)


def _base_headers():
    return {
        rca.SIGNATURE_HEADER: GOLDEN_HMAC,
        rca.TIMESTAMP_HEADER: GOLDEN_TS,
        rca.NONCE_HEADER: GOLDEN_NONCE,
        rca.CORRELATION_HEADER: GOLDEN_CORR,
        rca.PROTO_HEADER: rca.CANONICAL_V2,
        rca.KEYID_HEADER: GOLDEN_KEYID,
    }


# (field-name, kw-override, header-override, accepted-rejection-codes)
_FIELD_MUTATIONS = [
    ("protocol",   {}, {rca.PROTO_HEADER: rca.CANONICAL_V1}, {"bad_signature"}),
    ("method",     {"method": "GET"}, {}, {"bad_signature"}),
    ("path",       {"path": "/api/runtime/v1/runs/EVIL/cancel"}, {}, {"bad_signature"}),
    ("tenant",     {"tenant": "tenant-ATTACKER"}, {}, {"bad_signature"}),
    ("workspace",  {"workspace": "ws-ATTACKER"}, {}, {"bad_signature"}),
    ("user",       {"user": "user-ATTACKER"}, {}, {"bad_signature"}),
    ("timestamp",  {"now": int(GOLDEN_TS) + 1},
                   {rca.TIMESTAMP_HEADER: str(int(GOLDEN_TS) + 1)}, {"bad_signature"}),
    ("nonce",      {}, {rca.NONCE_HEADER: "n-ffffffffffffffffffffffffffffffff"}, {"bad_signature"}),
    ("body",       {"body": b'{"agent":"default","task":"TAMPERED"}'}, {}, {"bad_signature"}),
    ("correlation", {}, {rca.CORRELATION_HEADER: "cid-ffffffffffffffff"}, {"bad_signature"}),
    ("key_id",     {}, {rca.KEYID_HEADER: "svc-OTHER-key"}, {"bad_signature"}),
]


@pytest.mark.parametrize(
    "field,kw_over,hdr_over,codes",
    _FIELD_MUTATIONS,
    ids=[m[0] for m in _FIELD_MUTATIONS],
)
def test_per_field_mutation_is_rejected(field, kw_over, hdr_over, codes):
    store = rca._MemoryNonceStore()
    kw = {**_BASE_KW, **kw_over}
    headers = _base_headers()
    headers.update(hdr_over)
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(headers=headers, store=store, **kw)
    assert ei.value.code in codes, (field, ei.value.code)
    # A rejected (unauthenticated) command must NEVER burn a nonce.
    assert store.seen(headers[rca.NONCE_HEADER]) is False


def test_unmutated_control_still_verifies():
    """Control: with NO field mutated, the same harness ACCEPTS — proving the
    per-field failures above are caused by the mutation, not the harness."""
    store = rca._MemoryNonceStore()
    rca.verify_command(headers=_base_headers(), store=store, **_BASE_KW)


def test_empty_body_golden_vector_still_binds_all_fields():
    """A second committed vector (empty body, a cancel path) keeps the contract
    honest for the no-body mutating commands (cancel/retry)."""
    empty_hash = hashlib.sha256(b"").hexdigest()
    canonical = rca.canonical_string_v2(
        method="POST", path="/api/runtime/v1/runs/run-123/cancel",
        tenant="tenant-alpha", workspace="ws-default", user="user-eiman",
        timestamp="1700000000", nonce="n-ffffffffffffffffffffffffffffffff",
        body=b"", correlation="cid-ffffffffffffffff", key_id=GOLDEN_KEYID,
    )
    assert canonical.split("\n")[8] == empty_hash
    assert (
        rca.compute_signature(GOLDEN_SECRET, canonical)
        == "ffa3e9a2c7616bccdbbea361850f204dbae602173ef189cc267d87d6562d2234"
    )


def test_unsupported_proto_fails_closed_not_downgrade():
    """An unknown protocol version fails closed (400) rather than silently
    downgrading to a weaker canonical."""
    store = rca._MemoryNonceStore()
    headers = _base_headers()
    headers[rca.PROTO_HEADER] = "youtab.runtime-sig.v999"
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(headers=headers, store=store, **_BASE_KW)
    assert ei.value.code == "unsupported_proto"
    assert ei.value.http_status == 400


def test_v2_missing_keyid_fails_closed():
    store = rca._MemoryNonceStore()
    headers = _base_headers()
    del headers[rca.KEYID_HEADER]
    with pytest.raises(rca.CommandAuthError) as ei:
        rca.verify_command(headers=headers, store=store, **_BASE_KW)
    assert ei.value.code == "missing_signature"
