"""Profile-scoped bridge credential reads and out-of-process delivery."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.platforms.whatsapp.adapter import WhatsAppAdapter, _read_bridge_capability, _standalone_send


def _credential(session, token="t" * 43):
    session.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(session / "bridge-capability", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(token)
    return token


def test_missing_and_malformed_credential_fail_closed(tmp_path):
    assert _read_bridge_capability(tmp_path) == ""
    _credential(tmp_path, "invalid")
    with pytest.raises(RuntimeError, match="Invalid WhatsApp bridge capability file"):
        _read_bridge_capability(tmp_path)


def test_private_credential_remains_profile_scoped(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    first = _credential(one)
    second = _credential(two, "u" * 43)
    assert _read_bridge_capability(one) == first
    assert _read_bridge_capability(two) == second


@pytest.mark.parametrize("managed", [False, True])
def test_adopted_bridge_refreshes_its_epoch_without_overriding_managed_startup(tmp_path, managed):
    first = _credential(tmp_path)
    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter._session_path = tmp_path
    adapter._bridge_capability = first
    adapter._bridge_process = MagicMock() if managed else None
    (tmp_path / "bridge-capability").write_text("u" * 43)
    expected = first if managed else "u" * 43
    assert adapter._bridge_headers() == {"Authorization": f"Bearer {expected}"}


def test_hard_link_to_external_file_is_refused_without_modification(tmp_path):
    external = tmp_path / "external"
    external.write_text("outside sentinel")
    os.link(external, tmp_path / "bridge-capability")
    with pytest.raises(RuntimeError, match="private owned regular file"):
        _read_bridge_capability(tmp_path)
    assert external.read_text() == "outside sentinel"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks require a POSIX filesystem")
def test_symlink_and_public_read_permissions_are_refused(tmp_path):
    external = tmp_path / "external"
    external.write_text("t" * 43)
    os.chmod(external, 0o600)
    credential = tmp_path / "bridge-capability"
    credential.symlink_to(external)
    with pytest.raises(RuntimeError, match="private owned regular file"):
        _read_bridge_capability(tmp_path)
    credential.unlink()
    _credential(tmp_path)
    os.chmod(credential, 0o644)
    with pytest.raises(RuntimeError, match="private owned regular file"):
        _read_bridge_capability(tmp_path)


def test_out_of_process_sender_refuses_missing_credential_before_http_or_attachment(tmp_path):
    config = SimpleNamespace(extra={"session_path": str(tmp_path), "bridge_port": 3000})
    with patch("aiohttp.ClientSession") as session, patch("os.path.exists") as attachment:
        result = asyncio.run(_standalone_send(config, "synthetic-chat", "text", media_files=[("/synthetic/file", False)]))
    assert "authorization unavailable" in result["error"]
    session.assert_not_called()
    attachment.assert_not_called()


def test_out_of_process_sender_uses_existing_session_capability_without_rotation(tmp_path):
    capability = _credential(tmp_path)
    config = SimpleNamespace(extra={"session_path": str(tmp_path), "bridge_port": 3000})
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={"messageId": "synthetic-message"})
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = request
    with patch("aiohttp.ClientSession", return_value=session), patch("secrets.token_urlsafe") as random:
        result = asyncio.run(_standalone_send(config, "synthetic-chat", "text"))
    assert result["success"]
    assert session.post.call_args.kwargs["headers"] == {"Authorization": f"Bearer {capability}"}
    assert _read_bridge_capability(tmp_path) == capability
    random.assert_not_called()
