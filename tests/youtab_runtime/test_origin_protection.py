"""A direct request to the origin address is rejected, even with the right Host.

This is the test the deployment plan turns on. Putting `agent.youtab.io` behind
Cloudflare Access protects the hostname; it does nothing for the machine, and
this zone already publishes a sibling A record pointing straight at the origin.
So the address is public knowledge, and "connect to it and send the right Host
header" is the whole bypass. These tests pin that the origin refuses that
request.

They run before any of it is installed and need no live host, because the rule
is data and a function rather than a hand-written config file. What they cannot
prove is that the generated block was actually deployed — that check belongs to
the post-install verification, and it is the only part of this that waits on
external access.
"""

from __future__ import annotations

import pytest

from youtab_runtime.origin_protection import (
    CLOUDFLARE_RANGES,
    is_cloudflare_ip,
    render_nginx_allowlist,
    render_nginx_server_block,
)

#: The origin this deployment targets. A request arriving *from* here is a
#: direct connection to the box, not something the edge forwarded.
ORIGIN_ADDRESS = "46.224.151.39"

BLOCK = render_nginx_server_block(
    server_name="agent.youtab.io",
    upstream="127.0.0.1:8081",
    certificate="/etc/ssl/cloudflare/agent.youtab.io.crt",
    certificate_key="/etc/ssl/cloudflare/agent.youtab.io.key",
    origin_pull_ca="/etc/ssl/cloudflare/origin-pull-ca.pem",
)


def test_a_direct_request_to_the_origin_address_is_not_from_cloudflare():
    """The bypass, stated as the assertion it is.

    Someone resolves or already knows the origin address, opens a connection to
    it and sends `Host: agent.youtab.io`. The Host header is attacker-chosen and
    proves nothing; the source address is what the origin judges, and this one
    is not Cloudflare.
    """
    assert not is_cloudflare_ip(ORIGIN_ADDRESS)


@pytest.mark.parametrize(
    "address",
    [
        "203.0.113.7",      # arbitrary public host
        "10.0.0.5",         # inside a private network
        "127.0.0.1",        # the origin talking to itself
        "2001:db8::1",      # arbitrary public IPv6
    ],
)
def test_non_cloudflare_sources_are_refused(address):
    assert not is_cloudflare_ip(address)


@pytest.mark.parametrize(
    "address",
    [
        "104.16.0.1",       # 104.16.0.0/13
        "172.64.0.1",       # 172.64.0.0/13
        "162.158.1.1",      # 162.158.0.0/15
        "131.0.72.1",       # 131.0.72.0/22
        "2606:4700::1",     # 2606:4700::/32
    ],
)
def test_cloudflare_edge_addresses_are_admitted(address):
    """Positive control.

    Without it, a function that refused everything would satisfy every
    assertion above and read as correct origin protection while taking the
    site off the internet.
    """
    assert is_cloudflare_ip(address)


def test_a_malformed_source_is_refused_rather_than_erroring():
    """Fail closed: an address that cannot be placed is not admitted."""
    for junk in ["", "not-an-ip", "999.999.999.999", "46.224.151.39; allow all"]:
        assert not is_cloudflare_ip(junk)


def test_the_allowlist_ends_in_a_deny():
    """`allow` lines without a closing `deny` are exceptions to an open door."""
    rendered = render_nginx_allowlist()
    assert rendered.strip().endswith("deny all;")
    for cidr in CLOUDFLARE_RANGES:
        assert f"allow {cidr};" in rendered


def test_the_generated_block_carries_every_control():
    assert "ssl_verify_client on;" in BLOCK, "Authenticated Origin Pulls not enforced"
    assert "ssl_client_certificate" in BLOCK
    assert "deny all;" in BLOCK
    assert "proxy_pass http://127.0.0.1:8081;" in BLOCK, "upstream must be loopback"


def test_the_health_endpoint_is_not_exempt_from_the_allowlist():
    """A health check outside the deny is an unauthenticated status window.

    nginx applies `allow`/`deny` from the enclosing server block to each
    location that does not override them, so the assertion is that the health
    location introduces no `allow` of its own.
    """
    health = BLOCK.split("location = /api/health")[1]
    assert "allow " not in health
    assert "satisfy any" not in BLOCK


def test_the_allowlist_and_the_check_come_from_one_list():
    """Config and rule cannot disagree about what is allowed.

    A hand-maintained nginx allowlist drifts from whatever the code believes,
    and it drifts silently in the permissive direction.
    """
    for cidr in CLOUDFLARE_RANGES:
        assert f"allow {cidr};" in BLOCK
        network_first_host = cidr.split("/")[0]
        # The rendered range and the programmatic check agree on membership.
        assert is_cloudflare_ip(network_first_host)
