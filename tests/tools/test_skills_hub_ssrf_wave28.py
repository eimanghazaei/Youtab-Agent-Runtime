"""WAVE-28 §6.3 — SSRF regression tests for the skills-hub and pet/catalog
egress paths that previously used a plain ``httpx`` client with
``follow_redirects=True``.

Every fetch whose destination (or any redirect hop) is influenced by
config/manifest/remote-catalog content must now go through
``tools.url_safety.create_ssrf_safe_client`` so that:

  * the initial connect is validated by resolved IP at TCP-connect time, and
  * EVERY redirect hop is re-resolved and re-validated — a trusted host that
    3xx-redirects to 169.254.169.254 / loopback / RFC-1918 is refused at the
    hop, before any socket is opened to the internal address.

These tests are hermetic: ``socket.getaddrinfo`` is patched to control
resolution and the httpcore ``SyncBackend.connect_tcp`` is patched to a
``MockStream`` so no real network traffic occurs. Each test FAILS if the fix is
reverted to a plain ``httpx`` client (a plain client neither raises
``SSRFConnectionBlocked`` nor stops before dialing the internal address).
"""

import socket
from unittest.mock import MagicMock

import pytest
from httpcore._backends.mock import MockStream
from httpcore._backends.sync import SyncBackend

from agent.pet import store as pet_store
from tools.skills_hub import BrowseShSource, _ssrf_safe_http_get_following
from tools.url_safety import SSRFConnectionBlocked, _reset_allow_private_cache


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _deny_private_by_default(monkeypatch):
    """Force the default (production) posture: private/loopback URLs are unsafe.

    Otherwise a developer machine with ``YOUTAB_AGENT_ALLOW_PRIVATE_URLS`` set
    would silently neuter these tests.
    """
    for var in (
        "YOUTAB_AGENT_ALLOW_PRIVATE_URLS",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    _reset_allow_private_cache()
    yield
    _reset_allow_private_cache()


def _getaddrinfo_map(mapping, default):
    """Return a fake ``socket.getaddrinfo`` resolving hosts per *mapping*.

    A literal-IP host resolves to itself so the connect-time guard still sees
    the real (blocked) address; everything else falls back to *default*.
    """
    def _fake(host, port, *args, **kwargs):
        ip = mapping.get(host, host if _looks_like_ip(host) else default)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]
    return _fake


def _looks_like_ip(host):
    parts = str(host).split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def _recording_connect(connects, *, stream_factory=None):
    """Patched ``SyncBackend.connect_tcp`` that records every real dial.

    When *stream_factory* is given, the (allowed) connection returns a
    ``MockStream`` so a first hop can serve a canned HTTP response; otherwise it
    hard-fails, because in the blocking tests no connect should ever happen.
    """
    def _connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        connects.append((host, port))
        if stream_factory is None:
            raise AssertionError(f"unexpected connect to {host}:{port}")
        return stream_factory()
    return _connect


_REDIRECT_TO_METADATA = (
    b"HTTP/1.1 302 Found\r\n"
    b"Location: http://169.254.169.254/latest/meta-data/\r\n"
    b"Content-Length: 0\r\n"
    b"Connection: close\r\n"
    b"\r\n"
)


# ---------------------------------------------------------------------------
# Helper-level: destination itself resolves to a blocked address
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_ip", ["169.254.169.254", "127.0.0.1", "10.11.12.13"])
def test_following_helper_blocks_metadata_loopback_and_private(monkeypatch, bad_ip):
    """A catalog/manifest URL that resolves to metadata/loopback/private is
    refused at connect time, before any socket opens."""
    connects = []
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _getaddrinfo_map({"catalog.attacker.example": bad_ip}, bad_ip),
    )
    monkeypatch.setattr(SyncBackend, "connect_tcp", _recording_connect(connects))

    with pytest.raises(SSRFConnectionBlocked):
        _ssrf_safe_http_get_following(
            "https://catalog.attacker.example/skill.md", timeout=5
        )
    assert connects == []


# ---------------------------------------------------------------------------
# browse.sh: skillMdUrl comes straight from remote detail JSON (line ~3004)
# ---------------------------------------------------------------------------

def _browse_item():
    return {
        "slug": "evil.com/x",
        "name": "x",
        "title": "X",
        "description": "desc",
        "tags": [],
    }


def test_browse_sh_skill_md_url_pointing_at_metadata_is_blocked(monkeypatch):
    """``skillMdUrl`` from the per-skill detail JSON is attacker-influenced; a
    value resolving to 169.254.169.254 must be blocked, not fetched."""
    src = BrowseShSource()
    connects = []
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _getaddrinfo_map({}, "169.254.169.254"),
    )
    monkeypatch.setattr(SyncBackend, "connect_tcp", _recording_connect(connects))
    monkeypatch.setattr(BrowseShSource, "_fetch_catalog", lambda self: [_browse_item()])
    monkeypatch.setattr(
        BrowseShSource, "_resolve_skill_md_url",
        lambda self, slug, item: "https://cdn.attacker.example/SKILL.md",
    )

    with pytest.raises(SSRFConnectionBlocked):
        src.fetch("browse-sh/evil.com/x")
    assert connects == []


def test_browse_sh_detail_redirect_to_metadata_is_blocked_at_hop(monkeypatch):
    """browse.sh (trusted first hop) 302-redirecting the detail request to a
    metadata IP is refused at the redirected hop — the internal address is
    never dialed."""
    src = BrowseShSource()
    connects = []
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _getaddrinfo_map({"browse.sh": "93.184.216.34"}, "93.184.216.34"),
    )
    monkeypatch.setattr(
        SyncBackend, "connect_tcp",
        _recording_connect(connects, stream_factory=lambda: MockStream([_REDIRECT_TO_METADATA])),
    )
    monkeypatch.setattr(BrowseShSource, "_fetch_catalog", lambda self: [_browse_item()])

    with pytest.raises(SSRFConnectionBlocked):
        src.fetch("browse-sh/evil.com/x")

    # Only the trusted first hop reached the real backend; the metadata hop was
    # blocked before connect.
    assert connects == [("93.184.216.34", 443)]


def test_browse_sh_fetch_public_url_still_works(monkeypatch):
    """A normal public SKILL.md fetch continues to work through the pinning
    client (proves the fix does not break the happy path AND that the code
    routes through create_ssrf_safe_client)."""
    src = BrowseShSource()

    class _Resp:
        status_code = 200
        text = "# hello skill"

    class _Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None, params=None):
            return _Resp()

    monkeypatch.setattr(BrowseShSource, "_fetch_catalog", lambda self: [_browse_item()])
    monkeypatch.setattr(
        BrowseShSource, "_resolve_skill_md_url",
        lambda self, slug, item: "https://cdn.example.com/SKILL.md",
    )
    monkeypatch.setattr(
        "tools.url_safety.create_ssrf_safe_client",
        lambda **kwargs: _Client(),
    )

    bundle = src.fetch("browse-sh/evil.com/x")
    assert bundle is not None
    assert bundle.files["SKILL.md"] == "# hello skill"


# ---------------------------------------------------------------------------
# agent/pet/store.py: host-pinned to petdex.dev, but must not chase a redirect
# off that trusted host to an internal address.
# ---------------------------------------------------------------------------

def test_pet_download_json_redirect_off_petdex_is_blocked(monkeypatch):
    connects = []
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _getaddrinfo_map({"assets.petdex.dev": "93.184.216.34"}, "93.184.216.34"),
    )
    monkeypatch.setattr(
        SyncBackend, "connect_tcp",
        _recording_connect(connects, stream_factory=lambda: MockStream([_REDIRECT_TO_METADATA])),
    )

    with pytest.raises(SSRFConnectionBlocked):
        pet_store._download_json("https://assets.petdex.dev/pet.json", timeout=5)
    assert connects == [("93.184.216.34", 443)]


def test_pet_download_stream_redirect_off_petdex_is_blocked(monkeypatch, tmp_path):
    connects = []
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _getaddrinfo_map({"assets.petdex.dev": "93.184.216.34"}, "93.184.216.34"),
    )
    monkeypatch.setattr(
        SyncBackend, "connect_tcp",
        _recording_connect(connects, stream_factory=lambda: MockStream([_REDIRECT_TO_METADATA])),
    )

    dest = tmp_path / "spritesheet.png"
    # _download wraps failures in PetStoreError; the SSRF block is the cause.
    with pytest.raises(pet_store.PetStoreError) as excinfo:
        pet_store._download(
            "https://assets.petdex.dev/sprite.png", dest, timeout=5
        )
    assert "download failed" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, SSRFConnectionBlocked)
    assert connects == [("93.184.216.34", 443)]
    assert not dest.exists()


def test_pet_download_json_direct_metadata_host_is_blocked(monkeypatch):
    """Defense in depth: even the first hop is IP-validated, so a manifest whose
    asset host resolves straight to the metadata address is refused."""
    connects = []
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _getaddrinfo_map({"assets.petdex.dev": "169.254.169.254"}, "169.254.169.254"),
    )
    monkeypatch.setattr(SyncBackend, "connect_tcp", _recording_connect(connects))

    with pytest.raises(SSRFConnectionBlocked):
        pet_store._download_json("https://assets.petdex.dev/pet.json", timeout=5)
    assert connects == []


# ---------------------------------------------------------------------------
# youtab_agent_cli/skills_hub.py: the GitHub publish flow carries a write-scoped
# token and must be IP-validated at connect time (and must never chase a
# redirect off api.github.com — follow_redirects stays False for the token).
# ---------------------------------------------------------------------------

def test_cli_github_publish_is_ip_validated_at_connect(monkeypatch, tmp_path):
    from youtab_agent_cli.skills_hub import _github_publish

    connects = []
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _getaddrinfo_map({"api.github.com": "169.254.169.254"}, "169.254.169.254"),
    )
    monkeypatch.setattr(SyncBackend, "connect_tcp", _recording_connect(connects))

    auth = MagicMock()
    auth.get_headers.return_value = {"Authorization": "token secret"}
    (tmp_path / "SKILL.md").write_text("x", encoding="utf-8")

    ok, msg = _github_publish(tmp_path, "demo", "owner/repo", auth)

    # The fork POST is refused before any socket opens to the metadata address.
    assert ok is False
    assert connects == []

