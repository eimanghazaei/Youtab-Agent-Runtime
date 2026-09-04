"""WAVE-27 SSRF regression tests for the four untrusted-destination fixes.

Each test proves a previously-raw fetch of an attacker/user-influenced URL now
refuses a cloud-metadata / unsafe target, while a legitimate destination is still
allowed.
"""

from __future__ import annotations

import asyncio

import pytest

_METADATA = "http://169.254.169.254/latest/meta-data/"


# 1) plugins/image_gen/openai:_load_image_bytes — agent/user-supplied image URL.
def test_image_gen_load_image_bytes_blocks_metadata():
    from plugins.image_gen.openai import _load_image_bytes

    # The SSRF-pinning client refuses to connect to the metadata address, so the
    # fetch raises rather than returning bytes.
    with pytest.raises(Exception):
        _load_image_bytes(_METADATA)


# 2) gateway/relay/media.py:RelayMediaClient.download — inbound-message media URL.
def test_relay_media_download_blocks_unsafe(monkeypatch):
    from gateway.relay import media as relay_media

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not open an unsafe media URL")

    monkeypatch.setattr(relay_media.urllib.request, "urlopen", _boom)
    client = relay_media.RelayMediaClient("https://relay.example", "gw", "secret")
    out = asyncio.run(client.download(_METADATA))
    assert out is None
    assert called["n"] == 0


def test_relay_media_download_allows_public(monkeypatch):
    """A public (safe) non-relay URL is still fetched — the guard did not over-block."""
    from gateway.relay import media as relay_media

    import urllib.error

    # is_safe_url passes for a public URL; assert the fetch is then attempted.
    monkeypatch.setattr("tools.url_safety.is_safe_url", lambda u: True)
    reached = {"n": 0}

    def _fake_urlopen(req, timeout=None):
        reached["n"] += 1
        # URLError is the download()'s expected best-effort failure path.
        raise urllib.error.URLError("stop after the guard passed")

    monkeypatch.setattr(relay_media.urllib.request, "urlopen", _fake_urlopen)
    client = relay_media.RelayMediaClient("https://relay.example", "gw", "secret")
    # A public, non-/relay/media/ URL: not needs_auth, passes is_safe_url.
    out = asyncio.run(client.download("https://cdn.example.com/pic.png"))
    assert out is None  # the fake urlopen failed, download degrades to None
    assert reached["n"] == 1  # but the fetch WAS attempted (not blocked)


# 3+4) web_server metadata floor: the exact security contract the fix relies on —
# a cloud-metadata endpoint is refused, a private/self-hosted provider is allowed.
def test_metadata_floor_blocks_metadata_allows_selfhosted():
    from tools.url_safety import is_always_blocked_url

    assert is_always_blocked_url("http://169.254.169.254/models") is True
    assert is_always_blocked_url("http://metadata.google.internal/models") is True
    # A legitimate self-hosted / private provider endpoint is NOT in the floor, so
    # operator validation of a local provider keeps working.
    assert is_always_blocked_url("http://192.168.1.50:11434/models") is False
    assert is_always_blocked_url("http://127.0.0.1:1234/models") is False


def test_web_server_validate_custom_endpoint_refuses_metadata():
    """The dashboard custom-endpoint validator refuses a metadata URL up front."""
    try:
        from youtab_agent_cli import web_server
        from youtab_agent_cli.web_server import validate_custom_endpoint
    except Exception:  # pragma: no cover - heavy optional import
        pytest.skip("web_server import unavailable in this environment")

    Model = getattr(web_server, "CustomEndpointUpdate", None)
    if Model is None:
        pytest.skip("CustomEndpointUpdate model not present")
    body = Model(name="probe", model="m", base_url="http://169.254.169.254")
    result = asyncio.run(validate_custom_endpoint(body))
    assert result["ok"] is False
    assert "metadata" in result["message"].lower()
