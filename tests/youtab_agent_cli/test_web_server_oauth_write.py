import os

import pytest

from youtab_agent_cli.web_server import _save_anthropic_oauth_creds


class _DummyPool:
    def entries(self):
        return []

    def remove_entry(self, _id):
        return None

    def add_entry(self, _entry):
        return None


@pytest.fixture
def oauth_file(monkeypatch, tmp_path):
    target = tmp_path / '.anthropic_oauth.json'
    monkeypatch.setattr('agent.anthropic_adapter._get_youtab_oauth_file', lambda: target)
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda _provider: _DummyPool())
    return target


def _filesystem_enforces_mode_bits(tmp_path) -> bool:
    """Does this filesystem actually store POSIX permission bits?

    A capability probe rather than a platform name: the question is what the
    filesystem does, and the answer is a property of the mount, not of
    ``sys.platform``. NTFS has no mode bits for ``chmod`` to set -- Python's
    ``os.chmod`` there only toggles the read-only attribute -- so ``st_mode``
    reads back 0o666 whatever was requested.
    """
    probe = tmp_path / '.mode-probe'
    probe.write_text('', encoding='utf-8')
    os.chmod(probe, 0o600)
    supported = (probe.stat().st_mode & 0o777) == 0o600
    probe.unlink()
    return supported


def test_dashboard_oauth_write_uses_owner_only_permissions(oauth_file, tmp_path, monkeypatch):
    """The OAuth token file must be created owner-only, never world-readable.

    Two assertions, because two different things are being checked and only
    one of them is about our code.

    The mode our writer *applies* is our code, it is identical on every
    platform, and it is asserted on every platform. This previously had no
    coverage outside POSIX at all: the test asserted only the resulting bits,
    so on Windows it failed with ``assert 438 == 384`` -- 0o666 against 0o600 --
    which is a true statement about NTFS and says nothing about whether the
    product still asks for 0o600.

    Whether those bits actually land is the filesystem's contract, so it is
    asserted wherever the filesystem implements it. On the Linux CI runner
    that is the full end-to-end check exactly as before; nothing is weakened
    there.
    """
    import utils

    applied = {}
    real = utils.atomic_json_write

    def spy(path, data, **kwargs):
        applied['mode'] = kwargs.get('mode')
        return real(path, data, **kwargs)

    monkeypatch.setattr(utils, 'atomic_json_write', spy)

    old_umask = os.umask(0o022)
    try:
        _save_anthropic_oauth_creds('access-token', 'refresh-token', 123456)
    finally:
        os.umask(old_umask)

    assert oauth_file.exists()
    assert applied.get('mode') == 0o600, (
        'the OAuth token file must be created 0o600; a umask-dependent default '
        'leaves it world-readable'
    )

    if _filesystem_enforces_mode_bits(tmp_path):
        assert oauth_file.stat().st_mode & 0o777 == 0o600


def test_dashboard_oauth_write_uses_atomic_json_write_with_owner_only_mode(oauth_file, monkeypatch):
    """The OAuth token file must be written 0o600 from creation via
    ``atomic_json_write(mode=0o600)``, so it is never briefly world-readable
    (the old ``os.replace`` + post-hoc ``chmod`` TOCTOU)."""
    import utils

    calls = {}
    real = utils.atomic_json_write

    def spy(path, data, **kwargs):
        calls['mode'] = kwargs.get('mode')
        return real(path, data, **kwargs)

    monkeypatch.setattr(utils, 'atomic_json_write', spy)

    _save_anthropic_oauth_creds('access-token', 'refresh-token', 123456)

    assert calls.get('mode') == 0o600, \
        'OAuth creds must be written 0o600 atomically (no chmod-after-replace window)'
    assert oauth_file.exists()
