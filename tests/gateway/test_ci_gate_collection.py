"""WAVE-29 §7: guard that critical Gateway security tests stay in required CI.

The WeCom encrypted-reply defect shipped because NO required CI job collected
anything under ``tests/gateway`` — the coverage gap, not the bug, was the real
failure. Closing the gap by adding ``tests/gateway/test_wecom_callback.py`` to
``run_all_gates.sh`` is only durable if the wiring cannot silently regress. This
module is that ratchet:

* it fails if the critical WeCom crypto / XML-hardening test cases disappear
  from the test module (rename, deletion, accidental removal), and
* it fails if the required gate script stops invoking those files.

It is itself listed in the required gate, so a green PR cannot quietly drop the
guard either.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_SCRIPT = _REPO_ROOT / "scripts" / "youtab" / "run_all_gates.sh"

# The (class, method) pairs that must remain collected. These are the cases that
# prove the encrypted-reply fix and the inbound-XML hardening; losing any of
# them would reopen a shipped-defect blind spot.
_REQUIRED_CASES = {
    "TestWecomCrypto": ["test_roundtrip_encrypt_decrypt"],
    "TestWecomEncryptedReplyRoundtrip": [
        "test_encrypt_produces_wellformed_envelope_with_all_fields",
        "test_reply_roundtrips_to_original_plaintext",
        "test_xml_special_chars_are_escaped_and_survive_roundtrip",
    ],
    "TestWecomCryptoFailurePaths": [
        "test_invalid_signature_rejected",
        "test_tampered_ciphertext_rejected_even_with_valid_signature",
        "test_wrong_aes_key_rejected",
    ],
    "TestWecomInboundXmlHardening": [
        "test_billion_laughs_rejected",
        "test_external_entity_xxe_rejected",
        "test_external_parameter_entity_rejected",
    ],
    "TestWecomNoSecretLeakage": [
        "test_decrypt_error_does_not_leak_key_or_ciphertext",
    ],
}

# Files that must remain wired into the required gate.
_REQUIRED_IN_GATE = (
    "tests/gateway/test_wecom_callback.py",
    "tests/gateway/test_ci_gate_collection.py",
    "tests/gateway/test_discord_voice_crypto.py",
)


def test_critical_wecom_cases_are_collectable():
    mod = importlib.import_module("tests.gateway.test_wecom_callback")
    missing: list[str] = []
    for cls_name, methods in _REQUIRED_CASES.items():
        cls = getattr(mod, cls_name, None)
        if cls is None:
            missing.append(cls_name)
            continue
        for m in methods:
            if not hasattr(cls, m):
                missing.append(f"{cls_name}.{m}")
    assert not missing, f"critical WeCom security tests missing: {missing}"


@pytest.mark.skipif(not _GATE_SCRIPT.exists(), reason="gate script not present")
def test_wecom_tests_are_wired_into_required_gate():
    text = _GATE_SCRIPT.read_text(encoding="utf-8")
    for path in _REQUIRED_IN_GATE:
        assert path in text, (
            f"{path} is not referenced in {_GATE_SCRIPT.name}; a critical "
            "Gateway security test would not run in required CI"
        )
