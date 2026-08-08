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

import re
from pathlib import Path

import pytest

from youtab_runtime.origin_protection import (
    AGENT_ORIGIN_CONF_PATH,
    CLOUDFLARE_RANGES,
    is_cloudflare_ip,
    render_agent_origin_conf,
    render_nginx_allowlist,
    render_nginx_server_block,
)

#: The origin this deployment targets. A request arriving *from* here is a
#: direct connection to the box, not something the edge forwarded.
ORIGIN_ADDRESS = "46.224.151.39"

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Rendered with the parameters the committed artifact uses, taken from the
#: module rather than spelled out again here. A test that renders its own
#: parameter set proves that the generator is self-consistent and nothing at
#: all about the file the host is given.
BLOCK = render_agent_origin_conf()

#: The floor this origin states. Written out rather than assembled from parts
#: so that a change to it is a visible change to this line.
TLS_FLOOR = "ssl_protocols TLSv1.2 TLSv1.3;"

#: 15 IPv4 ranges and 7 IPv6 ranges. Pinned as a number because "the list
#: still matches the constant" is satisfied by a constant someone shortened;
#: this fails until a reviewer changes the expected count deliberately.
EXPECTED_ALLOW_COUNT = 22


def _directives(config: str) -> str:
    """``config`` with nginx comments stripped.

    Assertions about what the origin permits have to read directives, not
    prose. This block's own comment explains the exposure by quoting nginx's
    compiled default, ``ssl_protocols TLSv1 TLSv1.1 TLSv1.2;`` -- so a test
    that searched the raw text for ``TLSv1.1`` would fail on the sentence
    explaining why TLS 1.1 is not served, and the obvious way to "fix" that
    failure is to delete the explanation.
    """
    return "\n".join(line.split("#", 1)[0] for line in config.splitlines())


def _values(directive: str, config: str) -> list[str]:
    """Every value ``directive`` is set to, in order of appearance."""
    return [
        match.strip()
        for match in re.findall(
            rf"^\s*{re.escape(directive)}\s+([^;]*);", _directives(config), re.M
        )
    ]


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


# --------------------------------------------------------------------------
# The TLS floor.
#
# `ssl_protocols` was absent from this block entirely. Absent is not neutral:
# nginx then applies its compiled default, which on 1.18 -- the deployment
# target -- is `TLSv1 TLSv1.1 TLSv1.2`. So the origin served two protocols
# withdrawn by RFC 8996, on the hostname that can start and stop the gateway.
#
# These tests read the artifact. What they cannot show is that an nginx
# actually refuses a TLS 1.0 ClientHello, because no nginx runs here --
# `scripts/youtab/nginx_tls_gate.sh` does that against a real 1.18 server, and
# refuses to report a pass unless it has first proved legacy TLS was
# negotiable in that environment.
# --------------------------------------------------------------------------


def test_the_tls_floor_is_stated_exactly_once_and_says_what_it_should():
    """One directive, and it names 1.2 and 1.3 and nothing else.

    `== [...]` rather than a substring check on purpose: two `ssl_protocols`
    directives in one block is not additive, the last one wins, so a second
    line reinstating TLS 1.0 would leave the first one intact and readable
    and entirely without effect.
    """
    assert _values("ssl_protocols", BLOCK) == ["TLSv1.2 TLSv1.3"]


@pytest.mark.parametrize("legacy", ["SSLv2", "SSLv3", "TLSv1", "TLSv1.1"])
def test_no_withdrawn_protocol_is_served(legacy):
    """The exposure, stated as the assertion it is.

    Tokenised rather than matched as a substring: `TLSv1` is a prefix of
    `TLSv1.2`, so `"TLSv1" not in line` would fail against a correct config
    and tempt exactly the wrong fix.
    """
    enabled = _values("ssl_protocols", BLOCK)
    assert enabled, (
        "no ssl_protocols directive -- nginx 1.18 then falls back to its "
        "compiled default, which serves TLS 1.0 and TLS 1.1"
    )
    for directive in enabled:
        assert legacy not in directive.split()


def test_the_floor_cannot_be_lost_to_inheritance():
    """It is in the server block, not left to whatever `http {}` happens to say.

    An http-level floor applies only to server blocks that do not set their
    own, so it is one unrelated `include` away from being overridden -- and
    the weakened block still starts, still serves, and still passes
    `nginx -t`. This artifact is copied to the host on its own; the floor has
    to travel with it.
    """
    assert TLS_FLOOR in _directives(BLOCK)
    # Before the first location, i.e. server-wide rather than scoped to one
    # route while the rest of the block inherits.
    directives = _directives(BLOCK)
    assert directives.index("ssl_protocols") < directives.index("location /")


@pytest.mark.parametrize(
    "server_name,upstream",
    [
        ("agent.youtab.io", "127.0.0.1:8081"),
        ("another.youtab.io", "127.0.0.1:9099"),
    ],
)
def test_every_generated_block_carries_the_floor_not_just_this_one(
    server_name, upstream
):
    """Template drift: the floor belongs to the generator, not to one call.

    Without this, the floor could be reintroduced as a value someone passes
    in, and the next hostname rendered from this generator would ship without
    it while every assertion above still passed.
    """
    block = render_nginx_server_block(
        server_name=server_name,
        upstream=upstream,
        certificate="/etc/ssl/example.crt",
        certificate_key="/etc/ssl/example.key",
        origin_pull_ca="/etc/ssl/example-ca.pem",
    )
    assert _values("ssl_protocols", block) == ["TLSv1.2 TLSv1.3"]


# --------------------------------------------------------------------------
# The controls that were already here and must survive this change.
# --------------------------------------------------------------------------


def test_authenticated_origin_pulls_stay_mandatory():
    """`on`, not `optional`, and not `optional_no_ca`.

    Both weaker settings let a handshake complete with no client certificate
    at all and leave the difference in `$ssl_client_verify`, which nothing in
    this block reads. The block would look unchanged and verify nothing.
    """
    assert _values("ssl_verify_client", BLOCK) == ["on"]
    assert _values("ssl_client_certificate", BLOCK) == [
        "/etc/ssl/cloudflare/origin-pull-ca.pem"
    ]


def test_the_allowlist_is_exactly_the_published_ranges_in_order():
    allowed = _values("allow", BLOCK)
    assert allowed == list(CLOUDFLARE_RANGES)
    assert len(allowed) == EXPECTED_ALLOW_COUNT
    assert _values("deny", BLOCK) == ["all"]


def test_the_deny_follows_the_last_allow_with_nothing_between():
    """Order is the whole control.

    `deny all;` placed before the allows, or another `allow` after it, turns
    the same set of lines into an open door -- nginx takes the first matching
    rule. Every line is present either way, so a membership check cannot see
    the difference.
    """
    lines = [line.strip() for line in _directives(BLOCK).splitlines() if line.strip()]
    last_allow = max(i for i, line in enumerate(lines) if line.startswith("allow "))
    assert lines[last_allow + 1] == "deny all;"
    assert not any(line.startswith("allow ") for line in lines[last_allow + 1 :])


def test_http2_is_spelled_the_way_the_deployment_target_understands():
    """`listen ... http2`, not `http2 on;`.

    `http2 on;` is nginx >= 1.25.1. On the 1.18 that runs this origin it is an
    unknown directive, so nginx refuses to start -- and the last time that
    happened the block was corrected by hand on the host, which is the
    divergence the generator exists to prevent.
    """
    directives = _directives(BLOCK)
    assert "listen 443 ssl http2;" in directives
    assert "listen [::]:443 ssl http2;" in directives
    assert not re.search(r"^\s*http2\s+(on|off)\s*;", directives, re.M)


def test_websocket_and_health_routing_are_untouched():
    """This change is the TLS floor and nothing else.

    The dashboard terminal and streaming replies are WebSockets; dropping the
    upgrade headers or the long read timeout would break them in a way that
    looks like an intermittent network fault rather than a config change.
    """
    directives = _directives(BLOCK)
    for directive in (
        "proxy_http_version 1.1;",
        "proxy_set_header Upgrade    $http_upgrade;",
        'proxy_set_header Connection "upgrade";',
        "proxy_read_timeout 3600s;",
        "proxy_pass http://127.0.0.1:8081;",
    ):
        assert directive in directives

    assert "location = /api/health {" in directives
    assert "proxy_pass http://127.0.0.1:8081/api/health;" in directives
    health = directives.split("location = /api/health")[1]
    assert "allow " not in health
    assert "deny " not in health


# --------------------------------------------------------------------------
# The committed artifact.
# --------------------------------------------------------------------------


def test_the_committed_artifact_is_byte_identical_to_the_generator():
    """The file on disk is generator output, not something shaped like it.

    Bytes, not parsed directives. A block that differs only in whitespace is
    still not the block that was reviewed, and the header at the top of that
    file tells every future reader the generator is authoritative -- which is
    only true if something checks.

    This is also what makes the nginx gate meaningful: it mounts this exact
    file into the container, so a handshake proved against it is a handshake
    proved against what the host is given.
    """
    committed = (REPO_ROOT / AGENT_ORIGIN_CONF_PATH).read_bytes()
    assert committed == render_agent_origin_conf().encode("utf-8"), (
        f"{AGENT_ORIGIN_CONF_PATH} has drifted from its generator. "
        "Regenerate with: python scripts/youtab/render_origin_nginx.py"
    )


def test_the_committed_artifact_has_unix_line_endings():
    """A CRLF checkout would make the byte comparison mean different things.

    `core.autocrlf=true` on a Windows workstation rewrites this file on
    checkout, so without the `.gitattributes` pin the identity test above
    would pass in CI and fail locally against the same commit. The file also
    goes to a Linux host and into a Linux container.
    """
    assert b"\r" not in (REPO_ROOT / AGENT_ORIGIN_CONF_PATH).read_bytes()
