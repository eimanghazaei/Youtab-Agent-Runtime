"""The dashboard answers to one declared public name, and to nothing else.

The process binds to `127.0.0.1` and is reached as `https://agent.youtab.io`
through a reverse proxy. Those two facts are in tension: the Host guard exists
to stop a browser being tricked into treating an attacker hostname as the
dashboard's own origin (DNS rebinding, GHSA-ppp5-vxwm-4cf7), and it did that by
requiring the Host header to name the bound interface. Behind a proxy that
rejects every real request — the deployment returned
`Invalid Host header` for `agent.youtab.io` — while still rejecting attackers.

The fix is one narrow seam: a single URL an operator wrote down. Not a pattern,
not a list, not a header the request can influence. These tests exist to keep
it narrow, because the tempting repairs — accepting `X-Forwarded-Host`,
trusting any Host when a proxy is present, rewriting Host to loopback — each
either reopen the rebinding hole or break OAuth callback construction, which
needs the real external hostname.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

GATEWAY = Path(__file__).resolve().parents[2]
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))

PUBLIC_URL = "https://agent.youtab.io"
PUBLIC_HOST = "agent.youtab.io"
BOUND = "127.0.0.1"


@pytest.fixture
def configured(monkeypatch):
    """The deployment's actual configuration: loopback bind, declared public URL."""
    monkeypatch.setenv("YOUTAB_AGENT_DASHBOARD_PUBLIC_URL", PUBLIC_URL)
    yield


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_DASHBOARD_PUBLIC_URL", raising=False)
    yield


def _accepted(host: str) -> bool:
    from youtab_agent_cli.web_server import _is_accepted_host

    return _is_accepted_host(host, BOUND)


# --- the public host is accepted -------------------------------------------


def test_the_declared_public_host_is_accepted(configured):
    """The defect, stated as a test: this returned 400 in production."""
    assert _accepted(PUBLIC_HOST)


def test_the_declared_public_host_is_accepted_with_a_port(configured):
    assert _accepted(f"{PUBLIC_HOST}:443")


def test_loopback_still_works_for_direct_local_access(configured):
    """Adding a public name must not remove the way an operator reaches it."""
    for host in ("127.0.0.1", "127.0.0.1:8081", "localhost", "[::1]"):
        assert _accepted(host), host


# --- everything else is still refused ---------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "agent.youtab.io.evil.example",  # suffix spoof
        "evil.example/agent.youtab.io",  # path, not host
        "notagent.youtab.io",
        "youtab.io",  # the parent name is not the declared one
        "",
        "  ",
    ],
)
def test_an_undeclared_host_is_refused(configured, host):
    assert not _accepted(host), f"{host!r} was accepted"


def test_nothing_is_accepted_when_no_public_url_is_configured(unconfigured):
    """The seam closes completely when the operator has not opened it.

    Without this, a deployment that forgot to set the variable could still be
    addressed by whatever name happened to reach it.
    """
    assert not _accepted(PUBLIC_HOST)
    assert _accepted("127.0.0.1"), "loopback must still work unconfigured"


def test_a_malformed_public_url_opens_nothing(monkeypatch):
    """A typo must fail closed, not accept everything."""
    for bad in ("not a url", "://x", "ftp://agent.youtab.io", ""):
        monkeypatch.setenv("YOUTAB_AGENT_DASHBOARD_PUBLIC_URL", bad)
        assert not _accepted(PUBLIC_HOST), f"{bad!r} opened the guard"
        assert not _accepted("evil.example"), f"{bad!r} opened the guard"


# --- WebSocket Host/Origin ---------------------------------------------------


class _FakeWS:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def _ws_reason(headers: dict[str, str]):
    from youtab_agent_cli import web_server

    class _State:
        bound_host = BOUND

    original = web_server.app.state
    try:
        web_server.app.state.bound_host = BOUND
    except Exception:  # pragma: no cover - defensive
        pass
    return web_server._ws_host_origin_reason(_FakeWS(headers))


def test_websocket_accepts_the_declared_host_and_https_origin(configured):
    assert _ws_reason({"host": PUBLIC_HOST, "origin": PUBLIC_URL}) is None


def test_websocket_refuses_a_malicious_origin(configured):
    reason = _ws_reason({"host": PUBLIC_HOST, "origin": "https://evil.example"})
    assert reason and reason.startswith("origin_mismatch"), reason


def test_websocket_refuses_a_malicious_host(configured):
    reason = _ws_reason({"host": "evil.example", "origin": PUBLIC_URL})
    assert reason and reason.startswith("host_mismatch"), reason


def test_websocket_refuses_the_public_host_on_the_wrong_scheme(configured):
    """`_is_accepted_host` compares hostnames only.

    Without an explicit scheme check, `http://agent.youtab.io` — the origin a
    stripped or spoofed page presents — would pass on a deployment that is only
    ever reachable over HTTPS.
    """
    reason = _ws_reason({"host": PUBLIC_HOST, "origin": f"http://{PUBLIC_HOST}"})
    assert reason and reason.startswith("origin_mismatch"), reason


# --- OAuth callback ----------------------------------------------------------


def test_oauth_callback_resolves_to_the_public_url(configured):
    """The reason Host must not simply be rewritten to loopback.

    The callback has to be the externally reachable URL; rewriting Host at the
    proxy would make this resolve to `http://127.0.0.1:8081/auth/callback`,
    which the identity provider would refuse and which no browser could reach.
    """
    from youtab_agent_cli.dashboard_auth.prefix import resolve_public_url

    assert resolve_public_url() == PUBLIC_URL
    assert f"{resolve_public_url()}/auth/callback" == "https://agent.youtab.io/auth/callback"


# --- the generated nginx block ----------------------------------------------


def test_the_generator_emits_nginx_1_18_compatible_http2():
    """`http2 on;` is nginx >= 1.25.1; the deployment target runs 1.18.

    Emitting it made `nginx -t` fail with `unknown directive` and forced a
    hand-edit on the host — a divergence between the committed artifact and
    what production actually runs, which is what the generator exists to
    prevent.
    """
    from youtab_runtime.origin_protection import render_nginx_server_block

    block = render_nginx_server_block(
        server_name=PUBLIC_HOST,
        upstream="127.0.0.1:8081",
        certificate="/c.crt",
        certificate_key="/c.key",
        origin_pull_ca="/ca.pem",
    )
    assert "listen 443 ssl http2;" in block
    assert "\n    http2 on;" not in block


def test_the_generator_sets_every_forwarded_header_it_must_not_inherit():
    from youtab_runtime.origin_protection import render_nginx_server_block

    block = render_nginx_server_block(
        server_name=PUBLIC_HOST,
        upstream="127.0.0.1:8081",
        certificate="/c.crt",
        certificate_key="/c.key",
        origin_pull_ca="/ca.pem",
    )
    for header in ("Host", "X-Forwarded-Host", "X-Real-IP", "X-Forwarded-For", "X-Forwarded-Proto"):
        assert f"proxy_set_header {header}" in block.replace("  ", " ") or any(
            line.strip().startswith(f"proxy_set_header {header}") for line in block.splitlines()
        ), header
    # Scheme is asserted literally, not reflected from the request.
    assert "X-Forwarded-Proto https;" in block


def test_the_committed_block_matches_the_generator():
    """The artifact in the repo must be what the generator produces.

    It drifted once already, by hand, on the host.
    """
    from youtab_runtime.origin_protection import render_nginx_server_block

    committed = (GATEWAY / "infrastructure" / "nginx" / "agent.youtab.io.conf").read_text(encoding="utf-8")
    generated = render_nginx_server_block(
        server_name=PUBLIC_HOST,
        upstream="127.0.0.1:8081",
        certificate="/etc/ssl/cloudflare/agent.youtab.io.crt",
        certificate_key="/etc/ssl/cloudflare/agent.youtab.io.key",
        origin_pull_ca="/etc/ssl/cloudflare/origin-pull-ca.pem",
    )
    assert committed.strip() == generated.strip()
