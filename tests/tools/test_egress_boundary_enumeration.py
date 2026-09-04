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
    from tools.egress_policy_lint import _load_allowlist, scan

    findings = scan()
    allow = _load_allowlist()
    new = [f for f in findings if f.key() not in allow]
    live = {f.key() for f in findings}
    stale = [k for k in allow if k not in live]
    assert not new, f"un-allowlisted raw egress sites: {[f.key() for f in new][:20]}"
    assert not stale, f"stale allowlist entries (prune them): {stale[:20]}"


def test_allowlist_entries_are_individually_documented():
    """No broad/silent exceptions: every entry has a concrete reason, a valid
    category, and an owner_action."""
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    entries = data["allow"]
    assert entries, "allowlist must enumerate the known raw sites"
    for e in entries:
        assert e.get("file") and e.get("symbol"), e
        assert e.get("category") in _VALID_CATEGORIES, e
        assert isinstance(e.get("reason"), str) and len(e["reason"]) >= 12, e
        assert isinstance(e.get("owner_action"), str) and e["owner_action"], e
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
