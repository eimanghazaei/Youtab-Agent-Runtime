"""HONEST egress-boundary coverage enumeration (WAVE-26 ledger, WAVE-27 enforced).

WAVE-26 established that the runtime has no single outbound chokepoint and left
``UNIVERSAL_EGRESS_COVERAGE = False`` as an executable admission that coverage
was incomplete.

WAVE-27 turns the incomplete sweep into an *enforced, regression-proof* boundary
for the part that CAN be controlled in-process:

  * The audited httpx factory (:mod:`youtab_runtime.egress_guard_http`) and the
    non-httpx audited adapters (:mod:`youtab_runtime.egress_adapters`) are the
    approved construction path; the shared SSRF factory
    ``tools.url_safety.create_ssrf_safe_*`` routes through the boundary
    (observe mode) inside a run.
  * A CI static gate (``tools/egress_policy_lint.py``, run by the required
    ``python-security`` job) BANS raw outbound-client construction outside those
    adapters. Every pre-existing raw site is enumerated with a documented reason
    in ``security/egress_allowlist.json``; a new un-allowlisted site, or a stale
    entry, fails the gate.

What remains genuinely outside an in-process boundary — vendor SDK internal
transports and subprocess/sandbox/remote-env egress — cannot be wrapped this way
and is documented as PENDING_OWNER_ACTION. So the LITERAL "every byte of egress
is audited" claim (:data:`UNIVERSAL_EGRESS_COVERAGE`) stays ``False``, while the
repository-controlled in-process boundary is proven complete-and-enforced below.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALLOWLIST_PATH = REPO_ROOT / "security" / "egress_allowlist.json"
EXCEPTIONS_DOC = REPO_ROOT / "docs" / "security" / "EGRESS_EXCEPTIONS.md"

# Literal all-egress coverage. Still False: out-of-process egress (vendor SDK
# internal transports, subprocess/sandbox/remote-env) cannot be wrapped by an
# in-process boundary. Flipping this to True is illegitimate while those exist.
UNIVERSAL_EGRESS_COVERAGE = False

_VALID_CATEGORIES = frozenset(
    {
        "fixed_destination_infra",
        "untrusted_destination_guarded",
        "sdk_internal",
        "dev_tooling",
    }
)


def test_boundary_api_is_real_and_importable():
    from youtab_runtime.egress_guard_http import (  # noqa: F401
        audited_async_client,
        audited_async_client_ambient,
        audited_client,
        audited_client_ambient,
    )
    from youtab_runtime.egress_audit import (  # noqa: F401
        authorize,
        record_observed,
    )
    from youtab_runtime.egress_context import (  # noqa: F401
        current_context,
        egress_run_context,
    )


def test_non_httpx_adapters_are_real_and_importable():
    from youtab_runtime.egress_adapters import (  # noqa: F401
        audited_aiohttp_request,
        audited_requests_request,
        audited_urlopen,
        audited_websocket_connect,
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Platform-INDEPENDENT egress coverage proof (a full-repo AST scan of the "
        "same tracked files on every OS). It is enforced authoritatively by the "
        "`egress-policy` step of the required python-security CI gate on ubuntu "
        "(scripts/youtab/run_all_gates.sh) and re-run by this test there. Skipped "
        "ONLY on Windows to avoid a ~13s CPU/IO-heavy redundant scan occupying an "
        "xdist worker in the windows-tools job under -j3 --file-retries 0, which "
        "adds resource pressure to co-scheduled timeout-sensitive tests. No "
        "Windows-specific behaviour is hidden — the scan result is identical "
        "across platforms."
    ),
)
def test_ci_gate_has_no_new_or_stale_raw_sites():
    """The executable coverage proof: every production raw outbound-client site
    is either inside the audited adapters or justified in the allowlist, and no
    allowlist entry is stale. This is exactly what the required CI gate enforces."""
    from collections import Counter

    from tools.egress_policy_lint import _allow_counts, _load_allowlist, scan

    findings = scan()
    found = Counter(f.site() for f in findings)
    allowed = _allow_counts(_load_allowlist())
    new = found - allowed
    stale = allowed - found
    assert not new, f"un-allowlisted / mutated raw egress sites: {list(new)[:20]}"
    assert not stale, f"stale allowlist entries (prune/re-review): {list(stale)[:20]}"


def test_allowlist_entries_are_individually_documented():
    """No broad/silent exceptions: every entry has a concrete reason, a valid
    category, an owner_action, AND a per-call-site fingerprint (WAVE-28 §6.5) so
    an approved (file, symbol) cannot be silently mutated into an unsafe site."""
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    entries = data["allow"]
    assert entries, "allowlist must enumerate the known raw sites"
    for e in entries:
        assert e.get("file") and e.get("symbol"), e
        assert e.get("category") in _VALID_CATEGORIES, e
        assert isinstance(e.get("reason"), str) and len(e["reason"]) >= 12, e
        assert isinstance(e.get("owner_action"), str) and e["owner_action"], e
        # WAVE-28 §6.5: every entry is fingerprinted (16-hex sha256 prefix).
        fp = e.get("fingerprint")
        assert isinstance(fp, str) and len(fp) == 16 and all(
            c in "0123456789abcdef" for c in fp
        ), f"entry missing/!invalid fingerprint: {e}"
        # Guard against a resurrected placeholder baseline.
        assert "PENDING: pre-existing raw client" not in e["reason"], e


def test_universal_all_egress_coverage_is_false_out_of_process_remains():
    assert UNIVERSAL_EGRESS_COVERAGE is False
    # Out-of-process egress is enumerated (as documented exceptions) and cannot
    # be wrapped in-process — the honest reason the literal flag stays False.
    doc = EXCEPTIONS_DOC.read_text(encoding="utf-8").lower()
    for token in ("sdk", "subprocess", "pending_owner_action"):
        assert token in doc, f"exceptions doc must address: {token}"


def test_exceptions_doc_states_precise_scope():
    doc = EXCEPTIONS_DOC.read_text(encoding="utf-8").lower()
    assert "in-process" in doc
    assert "egress_policy_lint" in doc or "egress-policy" in doc


# ===========================================================================
# WAVE-28 §6.5 — the fingerprinted gate rejects mutation of an approved site.
# Each test baselines a fixture module, proves the gate is GREEN, then mutates
# and proves the gate FAILS. This is the executable proof that an approved
# `file::symbol` cannot be turned into a new/unsafe site while CI still passes.
# ===========================================================================

import ast as _ast  # noqa: E402


def _fp(src: str) -> str:
    """Fingerprint of the single call expression in ``src``."""
    from tools.egress_policy_lint import _call_fingerprint

    node = _ast.parse(src).body[0].value
    assert isinstance(node, _ast.Call)
    return _call_fingerprint(node)


def test_fingerprint_is_stable_and_argument_sensitive():
    # Same call, moved to a different line → same fingerprint (no churn: the
    # fingerprint strips line/col attributes).
    assert _fp('httpx.get("https://api.github.com/x")') == _fp(
        '\n\nhttpx.get("https://api.github.com/x")'
    )
    base = _fp('httpx.get("https://api.github.com/x")')
    # A different destination literal → different fingerprint.
    assert _fp('httpx.get("http://169.254.169.254/latest")') != base
    # A changed redirect policy → different fingerprint.
    assert _fp('httpx.get("https://api.github.com/x", follow_redirects=True)') != base
    # A widened/added keyword → different fingerprint.
    assert _fp('httpx.get("https://api.github.com/x", verify=False)') != base


def _gate(monkeypatch, tmp_path, source, allow=None):
    """Point the gate at a single fixture module + a temp allowlist."""
    import tools.egress_policy_lint as lint

    mod = tmp_path / "modx.py"
    mod.write_text(source, encoding="utf-8")
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "ALLOWLIST_PATH", tmp_path / "allow.json")
    monkeypatch.setattr(lint, "_iter_py_files", lambda: [mod])
    if allow is not None:
        (tmp_path / "allow.json").write_text(
            json.dumps({"allow": allow}), encoding="utf-8"
        )
    return lint, mod


_APPROVED = 'import httpx\n\n\ndef f():\n    return httpx.get("https://api.github.com/x")\n'


def test_baseline_then_green(monkeypatch, tmp_path):
    lint, _ = _gate(monkeypatch, tmp_path, _APPROVED)
    assert lint.update_baseline() == 0
    assert lint.check() == 0  # freshly baselined → clean


def test_mut_new_module_level_verb_fails(monkeypatch, tmp_path):
    lint, mod = _gate(monkeypatch, tmp_path, _APPROVED)
    lint.update_baseline()
    # Add a SECOND httpx verb in the same already-approved file+symbol.
    mod.write_text(
        _APPROVED + '\n\ndef g():\n    return httpx.get("https://example.com/y")\n',
        encoding="utf-8",
    )
    assert lint.check() == 1  # new fingerprint is not allowlisted


def test_mut_untrusted_url_in_approved_symbol_fails(monkeypatch, tmp_path):
    lint, mod = _gate(monkeypatch, tmp_path, _APPROVED)
    lint.update_baseline()
    # Same file+symbol, destination swapped to a metadata address.
    mod.write_text(
        _APPROVED.replace(
            "https://api.github.com/x", "http://169.254.169.254/latest/meta"
        ),
        encoding="utf-8",
    )
    assert lint.check() == 1  # changed destination → fingerprint mismatch


def test_mut_changed_redirect_policy_fails(monkeypatch, tmp_path):
    lint, mod = _gate(monkeypatch, tmp_path, _APPROVED)
    lint.update_baseline()
    mod.write_text(
        _APPROVED.replace(
            'httpx.get("https://api.github.com/x")',
            'httpx.get("https://api.github.com/x", follow_redirects=True)',
        ),
        encoding="utf-8",
    )
    assert lint.check() == 1


def test_mut_raw_client_behind_alias_or_local_import_fails(monkeypatch, tmp_path):
    # No allowlist at all: a raw client hidden behind an import-alias AND a
    # function-local import must still be detected as a new site.
    src = (
        "def f():\n"
        "    import httpx as _h\n"
        '    return _h.get("https://example.com/z")\n'
    )
    lint, _ = _gate(monkeypatch, tmp_path, src, allow=[])
    assert lint.check() == 1
    # from-import alias form too.
    src2 = "from httpx import get as _g\n\n\ndef f():\n    return _g('https://example.com/z')\n"
    lint2, _ = _gate(monkeypatch, tmp_path, src2, allow=[])
    assert lint2.check() == 1


def test_mut_stale_fingerprint_fails(monkeypatch, tmp_path):
    # Allowlist an entry whose fingerprint does NOT match the actual site.
    entry = [{
        "file": "modx.py", "symbol": "httpx.get", "fingerprint": "deadbeefdeadbeef",
        "reason": "intentionally wrong fingerprint for the test", "category": "dev_tooling",
        "owner_action": "NONE",
    }]
    lint, _ = _gate(monkeypatch, tmp_path, _APPROVED, allow=entry)
    # The real site is 'new' (its true fingerprint isn't allowed) AND the bogus
    # entry is 'stale' — both fail the gate.
    assert lint.check() == 1


def test_mut_broad_entry_without_fingerprint_is_rejected(monkeypatch, tmp_path):
    # A "broadened" entry that omits the fingerprint (the old coarse file::symbol
    # form) is unenforceable and must be rejected, not silently honored.
    entry = [{
        "file": "modx.py", "symbol": "httpx.get",
        "reason": "coarse entry without a fingerprint", "category": "dev_tooling",
        "owner_action": "NONE",
    }]
    lint, _ = _gate(monkeypatch, tmp_path, _APPROVED, allow=entry)
    assert lint.check() == 1  # real site is new; broad entry has no fingerprint
