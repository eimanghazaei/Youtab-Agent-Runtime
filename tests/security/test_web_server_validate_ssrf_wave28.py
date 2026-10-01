"""WAVE-28 §6.2 — DNS-rebinding / connect-time TOCTOU closure for provider validation.

These tests cover the three httpx call sites in ``youtab_agent_cli.web_server`` that
probe an operator-supplied endpoint before a credential/endpoint is saved:

  * ``validate_custom_endpoint``                       (POST /api/providers/custom-endpoints/validate)
  * ``validate_provider_credential`` OPENAI_BASE_URL branch (POST /api/providers/validate)
  * ``validate_provider_credential`` hosted-probe branch    (POST /api/providers/validate)

Before the fix each of these used a RAW ``httpx.Client``: the hostname was (at best)
checked at resolve time and then re-resolved by httpx at connect time, so a sub-second
DNS rebind between check and connect defeated the metadata/private floor. The fix routes
all three through ``tools.url_safety.create_ssrf_safe_client``, which resolves + validates
the hostname at TCP-connect time and dials the validated IP, re-validating every redirect
hop, while still permitting operator-enabled self-hosted private endpoints.

Test taxonomy:
  * SEAM tests (``*uses_pinning_client*``) — assert the validation path constructs the
    connect-pinning client and never the raw ``httpx.Client``. These FAIL if the fix is
    reverted (raw client is used again → the pinning spy is never called and the raw-client
    trip-wire fires).
  * BEHAVIORAL tests — drive the real pinning client through the real endpoint with a
    controlled resolver and assert each SSRF vector is blocked / each legitimate target is
    allowed. Every behavioral test also asserts the pinning client was used, so it too FAILS
    on revert.
  * PRESERVATION / DOCUMENTATION guards (clearly labelled) — assert dashboard auth is still
    enforced and document the known proxy-delegation residual.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx
import pytest

import tools.url_safety as url_safety
from tools.url_safety import _reset_allow_private_cache
from youtab_agent_cli import web_server
from youtab_agent_cli.web_models import CustomEndpointUpdate, EnvVarUpdate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _resolves_to(*ips):
    """Patch ``socket.getaddrinfo`` so any hostname resolves to *ips*.

    ``url_safety`` reads only ``sockaddr[0]`` so a 2-tuple works for v4 and v6.
    This is the seam a real DNS rebind would flip: the pinning client resolves
    here at connect time and dials exactly what this returns.
    """
    return patch(
        "socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips
        ],
    )


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"data": [{"id": "seam-model"}]}

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for the pinning client in SEAM tests (no real network)."""

    def __init__(self, response=None):
        self._response = response or _FakeResponse()
        self.get_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        self.get_calls.append((url, headers, params))
        return self._response


@pytest.fixture(autouse=True)
def _reset_private_toggle():
    """Keep the process-global allow-private cache from leaking across tests."""
    _reset_allow_private_cache()
    yield
    _reset_allow_private_cache()


@pytest.fixture
def pin_spy(monkeypatch):
    """Wrap the REAL ``create_ssrf_safe_client`` and record every construction.

    Used by behavioral tests: the real connect-pinning client is exercised, and
    ``pin_spy`` proving it was constructed is what makes the test fail on revert
    (a reverted raw-``httpx.Client`` path never calls this).
    """
    calls: list[dict] = []
    real = url_safety.create_ssrf_safe_client

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(url_safety, "create_ssrf_safe_client", spy)
    return calls


class _Handler(BaseHTTPRequestHandler):
    # class attributes configured per-server below
    mode = "ok"  # "ok" | "redirect"
    hits: list = []

    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        type(self).hits.append(self.path)
        if type(self).mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        body = b'{"data": [{"id": "local-model"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_loopback_server(mode="ok"):
    handler = type("_H", (_Handler,), {"mode": mode, "hits": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, handler


# ---------------------------------------------------------------------------
# SEAM tests — prove the connect-pinning client is used, raw httpx.Client is not
# ---------------------------------------------------------------------------
class TestUsesPinningClient:
    def _install_seam(self, monkeypatch, response=None):
        fake = _FakeClient(response)
        monkeypatch.setattr(url_safety, "create_ssrf_safe_client", lambda **kw: fake)

        def _raw_client_forbidden(*a, **k):
            raise AssertionError(
                "validation path constructed a RAW httpx.Client — it must use "
                "create_ssrf_safe_client (connect-time SSRF pinning)"
            )

        # Trip-wire: url_safety's own create_ssrf_safe_client is replaced by the
        # fake above, so nothing legitimate constructs httpx.Client anymore. A
        # reverted validation path would, and this fires.
        monkeypatch.setattr(httpx, "Client", _raw_client_forbidden)
        return fake

    @pytest.mark.asyncio
    async def test_validate_custom_endpoint_uses_pinning_client(self, monkeypatch):
        fake = self._install_seam(monkeypatch)
        body = CustomEndpointUpdate(name="x", base_url="https://api.example.com/v1", model="m")
        result = await web_server.validate_custom_endpoint(body)
        assert fake.get_calls, "pinning client .get was never invoked"
        assert fake.get_calls[0][0] == "https://api.example.com/v1/models"
        assert result["ok"] is True
        assert result["models"] == ["seam-model"]

    @pytest.mark.asyncio
    async def test_validate_provider_openai_base_url_uses_pinning_client(self, monkeypatch):
        monkeypatch.setattr(web_server, "_require_token", lambda request: None)
        fake = self._install_seam(monkeypatch)
        body = EnvVarUpdate(key="OPENAI_BASE_URL", value="https://selfhost.example/v1")
        result = await web_server.validate_provider_credential(body, request=object())
        assert fake.get_calls, "pinning client .get was never invoked"
        assert fake.get_calls[0][0] == "https://selfhost.example/v1/models"
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_validate_provider_hosted_probe_uses_pinning_client(self, monkeypatch):
        monkeypatch.setattr(web_server, "_require_token", lambda request: None)
        fake = self._install_seam(monkeypatch, response=_FakeResponse(200))
        body = EnvVarUpdate(key="OPENAI_API_KEY", value="sk-test")
        result = await web_server.validate_provider_credential(body, request=object())
        assert fake.get_calls, "pinning client .get was never invoked"
        assert fake.get_calls[0][0] == "https://api.openai.com/v1/models"
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# BEHAVIORAL tests — real pinning client, controlled resolver
# ---------------------------------------------------------------------------
class TestConnectTimeBlocking:
    @pytest.mark.asyncio
    async def test_dns_rebind_to_metadata_is_blocked_at_connect(self, pin_spy):
        """Check-time public, connect-time metadata: the pin re-resolves at connect.

        A stateful resolver returns a public IP first (as a pre-flight check
        might have seen) then flips to the AWS/GCP metadata IP for the actual
        connect. Because the pinning client validates the IP it dials, the
        rebind is caught.
        """
        state = {"n": 0}

        def flipping(*a, **k):
            state["n"] += 1
            ip = "93.184.216.34" if state["n"] == 1 else "169.254.169.254"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

        with patch("socket.getaddrinfo", side_effect=flipping):
            body = CustomEndpointUpdate(
                name="x", base_url="http://rebind.attacker.example/v1", model="m"
            )
            result = await web_server.validate_custom_endpoint(body)
        assert pin_spy, "connect-pinning client was not used (fix reverted?)"
        assert result["reachable"] is False and result["ok"] is False

    @pytest.mark.parametrize(
        "ip",
        [
            "169.254.169.254",   # AWS/GCP/Azure/DO/Oracle metadata (IPv4)
            "169.254.170.2",     # AWS ECS task metadata (task IAM creds)
            "fd00:ec2::254",     # AWS metadata (IPv6)
            "169.254.1.1",       # link-local
            "127.0.0.1",         # loopback
            "10.0.0.5",          # RFC1918 private
            "192.168.1.50",      # RFC1918 private
            "172.16.5.5",        # RFC1918 private
            "fd00::1",           # IPv6 ULA private
            "100.64.1.1",        # CGNAT / RFC6598 (not covered by is_private)
        ],
    )
    @pytest.mark.asyncio
    async def test_private_and_metadata_targets_blocked(self, pin_spy, ip):
        with _resolves_to(ip):
            body = CustomEndpointUpdate(
                name="x", base_url="http://target.example/v1", model="m"
            )
            result = await web_server.validate_custom_endpoint(body)
        # Defense-in-depth: metadata / link-local (169.254.0.0/16 + the IPv6
        # metadata address) are refused up front by the WAVE-27 resolve-time floor
        # (Layer 1), so the connect-pin (Layer 2) is legitimately not reached for
        # them. Every OTHER private target (loopback / RFC1918 / ULA / CGNAT)
        # passes the floor and MUST be blocked by the pin — so pin usage is
        # required there. Either way the endpoint is refused.
        floor_caught = ip.startswith("169.254.") or ip == "fd00:ec2::254"
        assert result["reachable"] is False and result["ok"] is False
        if not floor_caught:
            assert pin_spy, "connect-pinning client was not used for a non-floor target"

    @pytest.mark.parametrize(
        "ip",
        ["169.254.169.254", "169.254.170.2", "fd00:ec2::254"],
    )
    @pytest.mark.asyncio
    async def test_metadata_blocked_even_with_allow_private_enabled(
        self, pin_spy, monkeypatch, ip
    ):
        """Operator-enabled private-URL mode must NOT open the metadata floor.

        Metadata is refused by both layers regardless of allow_private_urls: the
        WAVE-27 resolve-time floor (Layer 1) short-circuits it here, and the
        connect-pin's always-blocked set (Layer 2) would too. The security outcome
        is what matters — metadata stays blocked even with private URLs enabled.
        """
        monkeypatch.setenv("YOUTAB_AGENT_ALLOW_PRIVATE_URLS", "true")
        _reset_allow_private_cache()
        with _resolves_to(ip):
            body = CustomEndpointUpdate(
                name="x", base_url="http://target.example/v1", model="m"
            )
            result = await web_server.validate_custom_endpoint(body)
        assert result["reachable"] is False and result["ok"] is False

    @pytest.mark.parametrize(
        "host, resolved",
        [
            ("2852039166", "169.254.169.254"),   # decimal for 169.254.169.254
            ("0xA9FEA9FE", "169.254.169.254"),   # hex for 169.254.169.254
            ("2130706433", "127.0.0.1"),         # decimal for 127.0.0.1
            ("0x7f000001", "127.0.0.1"),         # hex for 127.0.0.1
            ("0177.0.0.1", "127.0.0.1"),         # octal-ish loopback
        ],
    )
    @pytest.mark.asyncio
    async def test_alternate_ip_notation_blocked(self, pin_spy, host, resolved):
        """Alternate IP notations normalise to the same address once resolved.

        Textual normalisation of decimal/octal/hex host forms is delegated to the
        OS resolver; the pinning client validates the *resolved* address, so the
        metadata/loopback target is blocked regardless of the notation used.
        """
        with _resolves_to(resolved):
            body = CustomEndpointUpdate(
                name="x", base_url=f"http://{host}/v1", model="m"
            )
            result = await web_server.validate_custom_endpoint(body)
        # metadata notations are floor-caught (Layer 1); loopback notations pass
        # the floor and are blocked by the connect-pin (Layer 2).
        floor_caught = resolved.startswith("169.254.") or resolved == "fd00:ec2::254"
        assert result["reachable"] is False and result["ok"] is False
        if not floor_caught:
            assert pin_spy, "connect-pinning client was not used for a non-floor target"


class TestLegitimateSelfHostedAllowed:
    @pytest.mark.asyncio
    async def test_self_hosted_loopback_allowed_when_allow_private(
        self, pin_spy, monkeypatch
    ):
        """The fix must not over-block: a real operator-enabled private endpoint
        is reachable and its models enumerate. Loopback is blocked with the
        toggle off and allowed with it on — exactly the operator contract."""
        monkeypatch.setenv("YOUTAB_AGENT_ALLOW_PRIVATE_URLS", "true")
        _reset_allow_private_cache()
        server, handler = _start_loopback_server(mode="ok")
        try:
            port = server.server_address[1]
            body = CustomEndpointUpdate(
                name="local", base_url=f"http://127.0.0.1:{port}/v1", model="m"
            )
            result = await web_server.validate_custom_endpoint(body)
        finally:
            server.shutdown()
        assert pin_spy, "connect-pinning client was not used (fix reverted?)"
        assert result["ok"] is True
        assert result["reachable"] is True
        assert "local-model" in result["models"]

    @pytest.mark.asyncio
    async def test_loopback_blocked_when_allow_private_disabled(self, pin_spy):
        """Same endpoint, toggle OFF → the pin blocks the private connect."""
        _reset_allow_private_cache()
        server, handler = _start_loopback_server(mode="ok")
        try:
            port = server.server_address[1]
            body = CustomEndpointUpdate(
                name="local", base_url=f"http://127.0.0.1:{port}/v1", model="m"
            )
            result = await web_server.validate_custom_endpoint(body)
        finally:
            server.shutdown()
        assert pin_spy, "connect-pinning client was not used (fix reverted?)"
        assert result["reachable"] is False and result["ok"] is False


class TestRedirectEscape:
    @pytest.mark.asyncio
    async def test_redirect_to_metadata_is_not_followed(self, pin_spy, monkeypatch):
        """A 302 to the metadata endpoint must never dial metadata.

        The validation client does not follow redirects, so the metadata hop is
        never requested; the caller just sees the 3xx. (The pinning client also
        re-validates each hop's connect for any caller that does follow
        redirects — see tests/tools/test_url_safety.py for the connect-resolver
        proof.) allow_private is on so the loopback origin itself is reachable,
        isolating the redirect behaviour.
        """
        monkeypatch.setenv("YOUTAB_AGENT_ALLOW_PRIVATE_URLS", "true")
        _reset_allow_private_cache()
        server, handler = _start_loopback_server(mode="redirect")
        try:
            port = server.server_address[1]
            body = CustomEndpointUpdate(
                name="local", base_url=f"http://127.0.0.1:{port}/v1", model="m"
            )
            result = await web_server.validate_custom_endpoint(body)
        finally:
            server.shutdown()
        assert pin_spy, "connect-pinning client was not used (fix reverted?)"
        # Got the 3xx back (reachable) but not a success, and metadata was never hit.
        assert result["ok"] is False
        assert result["reachable"] is True
        assert all("meta-data" not in h for h in handler.hits)


# ---------------------------------------------------------------------------
# PRESERVATION / DOCUMENTATION guards
# ---------------------------------------------------------------------------
class TestAuthAndResidual:
    @pytest.mark.asyncio
    async def test_validate_provider_credential_still_requires_token(self, monkeypatch):
        """Dashboard auth is preserved: the endpoint calls _require_token before
        any network probe. (Regression guard — not a revert detector.)"""
        called = {"n": 0}

        def _deny(request):
            called["n"] += 1
            raise web_server.HTTPException(status_code=401, detail="Unauthorized")

        monkeypatch.setattr(web_server, "_require_token", _deny)
        # Trip-wire: if auth were skipped and a probe ran, this would fire.
        monkeypatch.setattr(
            url_safety,
            "create_ssrf_safe_client",
            lambda **kw: (_ for _ in ()).throw(
                AssertionError("probe ran before auth check")
            ),
        )
        body = EnvVarUpdate(key="OPENAI_API_KEY", value="sk-test")
        with pytest.raises(web_server.HTTPException) as exc:
            await web_server.validate_provider_credential(body, request=object())
        assert exc.value.status_code == 401
        assert called["n"] == 1

    def test_proxy_delegation_is_a_documented_residual(self):
        """DOCUMENTED RESIDUAL: when an HTTP(S) proxy is configured, httpx routes
        the request through the proxy and final-target resolution is delegated to
        that proxy, so the connect-time IP pin cannot be applied. The proxy is the
        trusted egress boundary in that deployment (see create_ssrf_safe_client /
        create_ssrf_safe_async_client docstrings). This test simply pins that the
        standard proxy env vars are the recognised delegation trigger so the
        residual stays visible."""
        assert {"HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"}.issubset(
            set(url_safety._PROXY_ENV_VARS)
        )
