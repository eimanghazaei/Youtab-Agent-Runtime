"""Credential-bearing model probes must use only the checked destination."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import youtab_agent_cli.provider_probe as probe
from youtab_agent_cli.provider_probe import (
    InvalidProviderProbeURL,
    probe_provider_models,
)


def _resolve(monkeypatch, *addresses):
    calls = []

    def _getaddrinfo(host, port, *_args, **kwargs):
        calls.append((host, port, kwargs))
        if host == "127.0.0.1":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port),
            )
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    return calls


def test_https_dns_pinned_with_original_host_and_no_redirect_or_proxy(monkeypatch):
    resolutions = _resolve(monkeypatch, "203.0.113.10")
    observed = {}

    class Client:
        def __init__(self, **kwargs):
            observed["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, url, *, headers, extensions):
            observed.update(url=str(url), headers=headers, extensions=extensions)
            return object()

    monkeypatch.setattr("httpx.Client", Client)
    probe_provider_models("https://models.example.test:8443/v1", "sk-example")

    assert len(resolutions) == 1
    assert resolutions[0][0:2] == ("models.example.test", 8443)
    assert observed["url"] == "https://203.0.113.10:8443/v1/models"
    assert observed["headers"]["Host"] == "models.example.test:8443"
    assert observed["headers"]["Authorization"] == "Bearer sk-example"
    assert observed["extensions"] == {"sni_hostname": "models.example.test"}
    assert observed["client"]["follow_redirects"] is False
    assert observed["client"]["trust_env"] is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:pass@models.example.test/v1",
        "https://models.example.test/v1?next=localhost",
        "https://models.example.test/v1#fragment",
        "https://models.example.test:bad/v1",
        "https://models.example.test/v1\r\nHost:evil.test",
    ],
)
def test_malformed_probe_url_rejected_before_network(monkeypatch, url):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *_args, **_kwargs: pytest.fail("DNS called")
    )
    with pytest.raises(InvalidProviderProbeURL):
        probe_provider_models(url, "sk-secret")


@pytest.mark.parametrize(
    "address", ["169.254.169.254", "0.0.0.0", "224.0.0.1", "fe80::1"]
)
def test_metadata_and_non_destination_addresses_rejected(monkeypatch, address):
    _resolve(monkeypatch, address)
    with pytest.raises(InvalidProviderProbeURL):
        probe_provider_models("https://models.example.test/v1", "sk-secret")


def test_mixed_safe_and_link_local_dns_answers_are_rejected(monkeypatch):
    _resolve(monkeypatch, "10.0.0.3", "169.254.169.254")
    with pytest.raises(InvalidProviderProbeURL):
        probe_provider_models("http://internal.example.test/v1", "sk-secret")


def test_public_http_rejected_but_private_http_allowed(monkeypatch):
    _resolve(monkeypatch, "8.8.8.8")
    with pytest.raises(InvalidProviderProbeURL, match="HTTPS"):
        probe_provider_models("http://models.example.test/v1", "sk-secret")
    _resolve(monkeypatch, "10.0.0.4")

    pinned, host, _sni = probe._probe_destination("http://internal.example.test/v1")
    assert str(pinned) == "http://10.0.0.4/v1/models"
    assert host == "internal.example.test"


def test_real_local_probe_does_not_follow_redirect_or_reresolve(monkeypatch):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((
                self.path,
                self.headers.get("Host"),
                self.headers.get("Authorization"),
            ))
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        resolutions = _resolve(monkeypatch, "127.0.0.1")
        response = probe_provider_models(
            f"http://internal.example.test:{server.server_port}/v1", "sk-secret"
        )
        assert response.status_code == 302
        assert observed == [
            (
                "/v1/models",
                f"internal.example.test:{server.server_port}",
                "Bearer sk-secret",
            )
        ]
        assert [call[0] for call in resolutions].count("internal.example.test") == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
