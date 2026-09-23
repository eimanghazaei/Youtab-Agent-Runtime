"""Standalone unit tests for the sidecar_main loopback-only enforcement.

Pure logic only — importing sidecar_main does NOT import youtab_agent_cli (that
import is guarded under __main__), so this runs fast with any Python and never
touches the heavy CLI. Run: python test_sidecar_main.py  (exit 0 = pass).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sidecar_main as sm  # noqa: E402


def _check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"  ok: {name}")


def run():
    # loopback accepted
    _check("127.0.0.1 loopback", sm._host_is_loopback("127.0.0.1"))
    _check("127.0.0.53 loopback (127/8)", sm._host_is_loopback("127.0.0.53"))
    _check("::1 loopback", sm._host_is_loopback("::1"))
    _check("[::1] bracketed loopback", sm._host_is_loopback("[::1]"))
    _check("localhost loopback", sm._host_is_loopback("localhost"))
    _check("case-insensitive LocalHost", sm._host_is_loopback("LocalHost"))

    # non-loopback rejected
    _check("0.0.0.0 rejected", not sm._host_is_loopback("0.0.0.0"))
    _check(":: rejected", not sm._host_is_loopback("::"))
    _check("LAN 192.168.x rejected", not sm._host_is_loopback("192.168.1.10"))
    _check("public IP rejected", not sm._host_is_loopback("8.8.8.8"))
    _check("routable IPv6 rejected", not sm._host_is_loopback("2001:4860:4860::8888"))
    _check("arbitrary hostname rejected", not sm._host_is_loopback("evil.example.com"))
    _check("empty rejected", not sm._host_is_loopback(""))

    # argv parsing
    _check("--host space form", sm._requested_host(["serve", "--host", "0.0.0.0", "--port", "0"]) == "0.0.0.0")
    _check("--host= form", sm._requested_host(["serve", "--host=127.0.0.1"]) == "127.0.0.1")
    _check("no --host -> None", sm._requested_host(["serve", "--port", "0"]) is None)

    # enforcement: refuse non-loopback (SystemExit with the config code), allow loopback
    try:
        sm._enforce_loopback_only(["serve", "--host", "0.0.0.0", "--port", "0"])
        raise AssertionError("0.0.0.0 should have raised SystemExit")
    except SystemExit as e:
        _check("0.0.0.0 refused with EX_CONFIG", e.code == sm.NON_LOOPBACK_REFUSED_EXIT)

    try:
        sm._enforce_loopback_only(["serve", "--host", "::", "--port", "0"])
        raise AssertionError(":: should have raised SystemExit")
    except SystemExit as e:
        _check(":: refused", e.code == sm.NON_LOOPBACK_REFUSED_EXIT)

    # loopback / no-host must NOT raise
    sm._enforce_loopback_only(["serve", "--host", "127.0.0.1", "--port", "0"])
    _check("127.0.0.1 not refused", True)
    sm._enforce_loopback_only(["serve", "--port", "0"])
    _check("no explicit host not refused", True)


if __name__ == "__main__":
    run()
    print("ALL_SIDECAR_MAIN_TESTS_PASSED")
