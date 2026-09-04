"""HONEST egress-boundary coverage enumeration (WAVE-26 Agent 3).

RECON R3 established that the runtime has **no single outbound chokepoint**:
egress happens over httpx, requests, aiohttp, urllib.request, websockets and a
set of vendor SDKs, and across two planes (in-process host egress and sandboxed
code egress). WAVE-26 does not — and cannot — close all of them.

This test encodes that reality as an executable contract so the incomplete
coverage is *visible* and cannot silently rot into a false universal-coverage
claim:

  * ``INSIDE_BOUNDARY``   — adapters that CAN be routed through the audited
    factory today (``youtab_runtime.egress_guard_http``). This is the shared
    boundary API itself; it is real and importable.
  * ``HOOK_PROPOSED``     — reachable in-process adapters for which WAVE-26
    Agent 3 delivers a hook *proposal* (unified diff in the report) but which
    are NOT yet wired in this frozen substrate.
  * ``OUTSIDE_BOUNDARY``  — adapters the in-process audit boundary cannot wrap
    in this wave (other client libs, webhooks, websockets, and out-of-process
    SDK / sandbox egress).

The gate-block condition is explicit: :data:`UNIVERSAL_EGRESS_COVERAGE` is
``False``. Anyone who wants to claim universal egress visibility must first make
``OUTSIDE_BOUNDARY`` empty *and* flip that flag — which this test forces them to
do in the open.
"""

from __future__ import annotations

# The honest headline: WAVE-26 does NOT achieve universal production egress
# closure. Flipping this to True without emptying OUTSIDE_BOUNDARY fails the
# test below by construction.
UNIVERSAL_EGRESS_COVERAGE = False


# Each entry: adapter id -> (mechanism, source anchor from RECON R3, status)
# status in {"inside", "hook_proposed", "outside"}.
ADAPTERS: dict[str, tuple[str, str, str]] = {
    # -- the boundary itself -------------------------------------------------
    "audited_httpx_factory": (
        "httpx",
        "youtab_runtime/egress_guard_http.py",
        "inside",
    ),
    # -- reachable in-process adapters WAVE-26 proposes to route via factory --
    "aux_provider_httpx_keepalive": (
        "httpx",
        "agent/process_bootstrap.py:185 build_keepalive_http_client",
        "hook_proposed",
    ),
    "mcp_http_sse_client": (
        "httpx",
        "tools/mcp_tool.py:2638/2924/236",
        "hook_proposed",
    ),
    # -- in-process httpx adapters NOT yet audited (SSRF-guarded at most) -----
    "url_safety_ssrf_client_raw": (
        "httpx",
        "tools/url_safety.py:825/841 create_ssrf_safe_client (guarded, not audited)",
        "outside",
    ),
    "web_search_providers": (
        "httpx/requests/sdk",
        "tavily:59 / brave:80 / searxng:83 / xai:257",
        "outside",
    ),
    "browser_providers": (
        "httpx/sdk",
        "browser_use:209 / browserbase:143 / firecrawl:89",
        "outside",
    ),
    "send_message_webhooks": (
        "httpx/requests",
        "tools/send_message_tool.py:1576/1672/1685/2017 (no URL validation)",
        "outside",
    ),
    # -- other client libraries (url_safety covers httpx only) ---------------
    "requests_library_adapters": (
        "requests",
        "RECON R3 — requests mechanism, no SSRF/audit wrap",
        "outside",
    ),
    "aiohttp_adapters": (
        "aiohttp",
        "RECON R3 — aiohttp mechanism, no SSRF/audit wrap",
        "outside",
    ),
    "urllib_request_paths": (
        "urllib.request",
        "discord_tool:96 / osv_check:154 / tirith_security:285 / "
        "anthropic_adapter:1114 / bitwarden:280",
        "outside",
    ),
    "websocket_clients": (
        "websockets",
        "browser_cdp / tts_streaming / relay / ws_transport",
        "outside",
    ),
    # -- out-of-process SDK egress (in-process boundary cannot wrap) ---------
    "vendor_sdk_egress": (
        "sdk",
        "daytona:62 / modal:100 / managed_modal:240 / vercel_sandbox:23 / "
        "firecrawl / tavily",
        "outside",
    ),
    # -- plane B: sandboxed code egress (only iron-proxy/Docker today) -------
    "sandboxed_code_egress": (
        "subprocess/arbitrary",
        "code_execution_tool.py:1426 / terminal_tool.py / skill scripts",
        "outside",
    ),
    "remote_env_egress": (
        "ssh/singularity/file_sync",
        "RECON R3 remote envs — out-of-process boundary",
        "outside",
    ),
}

INSIDE_BOUNDARY = frozenset(k for k, v in ADAPTERS.items() if v[2] == "inside")
HOOK_PROPOSED = frozenset(k for k, v in ADAPTERS.items() if v[2] == "hook_proposed")
OUTSIDE_BOUNDARY = frozenset(k for k, v in ADAPTERS.items() if v[2] == "outside")

#: The explicit, documented list of adapters NOT covered by the audit boundary
#: in WAVE-26. This is the visible gap the integrator reports on. Silently
#: "covering" one of these (removing it from OUTSIDE_BOUNDARY) forces a matching
#: edit here — the coverage gap can never be hidden.
NOT_YET_COVERED = OUTSIDE_BOUNDARY | HOOK_PROPOSED


def test_boundary_api_is_real_and_importable():
    # The inside-boundary claim must be backed by real, importable code.
    from youtab_runtime.egress_guard_http import (  # noqa: F401
        audited_async_client,
        audited_client,
    )
    from youtab_runtime.egress_audit import authorize  # noqa: F401


def test_coverage_is_not_universal():
    # The gate-block condition, executable.
    assert UNIVERSAL_EGRESS_COVERAGE is False
    # There is genuinely uncovered egress; the boundary is honest about it.
    assert len(OUTSIDE_BOUNDARY) > 0
    assert len(NOT_YET_COVERED) > 0


def test_universal_claim_would_require_empty_gap():
    # If someone ever flips the flag, this proves it is only legitimate once the
    # outside set is truly empty. Prevents a false green.
    if UNIVERSAL_EGRESS_COVERAGE:
        assert not OUTSIDE_BOUNDARY, (
            "cannot claim universal egress coverage while adapters remain "
            f"outside the boundary: {sorted(OUTSIDE_BOUNDARY)}"
        )


def test_partitions_are_disjoint_and_complete():
    assert INSIDE_BOUNDARY.isdisjoint(HOOK_PROPOSED)
    assert INSIDE_BOUNDARY.isdisjoint(OUTSIDE_BOUNDARY)
    assert HOOK_PROPOSED.isdisjoint(OUTSIDE_BOUNDARY)
    assert INSIDE_BOUNDARY | HOOK_PROPOSED | OUTSIDE_BOUNDARY == set(ADAPTERS)


def test_all_five_mechanisms_are_enumerated():
    # RECON R3: httpx, requests, aiohttp, urllib.request, websockets. Every one
    # must appear so the enumeration cannot quietly forget a mechanism.
    mechanisms = " ".join(v[0] for v in ADAPTERS.values())
    for mech in ("httpx", "requests", "aiohttp", "urllib.request", "websockets"):
        assert mech in mechanisms, f"mechanism not enumerated: {mech}"


def test_proposed_hooks_are_documented_not_claimed_covered():
    # The two reachable adapters we propose to wire must be tracked as proposals,
    # never mislabelled as already inside the boundary.
    assert "aux_provider_httpx_keepalive" in HOOK_PROPOSED
    assert "mcp_http_sse_client" in HOOK_PROPOSED
    assert "aux_provider_httpx_keepalive" not in INSIDE_BOUNDARY
    assert "mcp_http_sse_client" not in INSIDE_BOUNDARY


def test_out_of_process_egress_is_acknowledged_uncoverable_this_wave():
    # SDK + sandbox planes cannot be wrapped by an in-process boundary; they must
    # be explicitly outside, not optimistically assumed covered.
    for adapter in ("vendor_sdk_egress", "sandboxed_code_egress", "remote_env_egress"):
        assert adapter in OUTSIDE_BOUNDARY
