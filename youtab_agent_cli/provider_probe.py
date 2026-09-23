"""Bounded, credential-bearing probes for operator-configured model endpoints."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class InvalidProviderProbeURL(ValueError):
    """The requested model endpoint is unsafe for a credential-bearing probe."""


def _probe_destination(base_url: str) -> tuple[httpx.URL, str, str]:
    if any(ord(char) < 32 or ord(char) == 127 for char in base_url) or "\\" in base_url:
        raise InvalidProviderProbeURL("Endpoint URL contains control characters")
    try:
        parsed = urlsplit(base_url.strip().rstrip("/"))
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise InvalidProviderProbeURL("Invalid endpoint URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or (port is not None and port == 0)
        or parsed.username is not None
        or parsed.password is not None
        or "?" in base_url
        or "#" in base_url
    ):
        raise InvalidProviderProbeURL(
            "Endpoint must be an HTTP(S) URL without credentials or query"
        )
    try:
        hostname = hostname.encode("idna").decode("ascii")
        addresses = {
            ipaddress.ip_address(row[4][0])
            for row in socket.getaddrinfo(
                hostname,
                port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, UnicodeError, ValueError) as exc:
        raise InvalidProviderProbeURL("Endpoint host could not be resolved") from exc
    if not addresses or any(
        address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or (address.is_reserved and not address.is_loopback)
        for address in addresses
    ):
        raise InvalidProviderProbeURL("Endpoint resolves to a prohibited address")
    # Plain HTTP is useful for local model servers. Never send an API key to a
    # publicly routed endpoint over cleartext. DNS is pinned below, so a later
    # resolver change cannot turn a checked private address into another host.
    if parsed.scheme == "http" and any(
        not (address.is_private or address.is_loopback) for address in addresses
    ):
        raise InvalidProviderProbeURL("Public model endpoints must use HTTPS")

    address = str(sorted(addresses, key=str)[0])
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        host_header += f":{port}"
    # Preserve the original Host header and TLS SNI/certificate name while the
    # connection uses the checked address. The original hostname is never
    # resolved again by the HTTP client.
    pinned = httpx.URL(
        scheme=parsed.scheme,
        host=address,
        port=port,
        path=parsed.path.rstrip("/") + "/models",
    )
    return pinned, host_header, hostname


def probe_provider_models(
    base_url: str, api_key: str = "", *, timeout: float = 8.0
) -> httpx.Response:
    """GET /models once, with no redirect, proxy, or post-check hostname lookup."""
    url, host_header, hostname = _probe_destination(base_url)
    headers = {"Accept": "application/json", "Host": host_header}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    with httpx.Client(
        timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False
    ) as client:
        return client.get(url, headers=headers, extensions={"sni_hostname": hostname})
