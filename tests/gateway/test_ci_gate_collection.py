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
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "youtab-ci.yml"

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


@pytest.mark.skipif(not _CI_WORKFLOW.exists(), reason="CI workflow not present")
def test_ci_installs_fixed_pynacl_for_the_voice_crypto_gate():
    """Tie the runtime PyNaCl>=1.6.2 assertion to the CI install.

    test_discord_voice_crypto.py uses importorskip('nacl.secret') and asserts
    the running PyNaCl is >=1.6.2 — but that only fires if PyNaCl is actually
    installed in the required job. `pip install -e` does NOT honor the uv
    override-dependencies pin, so the job installs it explicitly. If that install
    line is removed, the voice crypto suite would silently SKIP with a green
    build, losing the belt-and-suspenders check that a vulnerable PyNaCl is not
    the one exercised. This guard fails closed on that regression.
    """
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "pynacl==1.6.2" in text.lower(), (
        "the python-security job must install pynacl==1.6.2 so "
        "test_discord_voice_crypto.py runs (and asserts the fixed version) "
        "rather than silently skipping"
    )
