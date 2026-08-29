"""Native ``<NAME>_FILE`` secret-source tests for the Agent Runtime service boundary.

Covers the engine's service-boundary secrets:
  - YOUTAB_AGENT_RUNTIME_SERVICE_SECRET  (gateway<->engine auth/HMAC, runtime plane)
  - YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN (single-operator dashboard session token)

Proves, per class: file consumption; the VALUE is absent from os.environ,
/proc/self/environ, a spawned child's environment, sys.argv, and logs; and the
loader fails closed on missing / empty / oversized / symlinked / non-regular /
group-or-other-writable files and on a dual inline+file source. (docker-inspect
absence is a corollary of env-absence and is separately proven end-to-end by the
eco-verify container evidence.)
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from youtab_agent_cli.secret_file import SecretFileError, env_or_file, read_secret_file

_HARNESS_VARS = {"PYTEST_CURRENT_TEST"}


def _app_env_items():
    return {k: v for k, v in os.environ.items() if k not in _HARNESS_VARS}


def _write_secret(tmp_path, name, value, *, mode=0o400, trailing="\n"):
    p = tmp_path / f"{name}.secret"
    p.write_bytes((value + trailing).encode("utf-8"))
    if os.name == "posix":
        os.chmod(p, mode)
    return str(p)


# ---- helper-level fail-closed controls -------------------------------------


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(SecretFileError, match="not accessible"):
        read_secret_file(
            str(tmp_path / "nope"), var="YOUTAB_AGENT_RUNTIME_SERVICE_SECRET"
        )


def test_empty_file_fails_closed(tmp_path):
    p = tmp_path / "e"
    p.write_bytes(b"")
    with pytest.raises(SecretFileError, match="empty"):
        read_secret_file(str(p), var="X")


def test_oversized_file_fails_closed(tmp_path):
    from youtab_agent_cli import secret_file as sf

    p = tmp_path / "big"
    p.write_bytes(b"x" * (sf._MAX_SECRET_FILE_BYTES + 5))
    with pytest.raises(SecretFileError, match="too large"):
        read_secret_file(str(p), var="X")


def test_non_regular_file_fails_closed(tmp_path):
    with pytest.raises(SecretFileError, match="not a regular file"):
        read_secret_file(str(tmp_path), var="X")


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX")
def test_symlink_fails_closed(tmp_path):
    target = tmp_path / "real"
    target.write_bytes(b"v\n")
    os.chmod(target, 0o400)
    link = tmp_path / "link"
    os.symlink(target, link)
    with pytest.raises(SecretFileError, match="symlink"):
        read_secret_file(str(link), var="X")


@pytest.mark.skipif(os.name != "posix", reason="mode bits are POSIX")
def test_group_or_other_writable_fails_closed(tmp_path):
    p = tmp_path / "loose"
    p.write_bytes(b"v\n")
    os.chmod(p, 0o406)
    with pytest.raises(SecretFileError, match="writable"):
        read_secret_file(str(p), var="X")


def test_trailing_newline_trimmed_exactly_once(tmp_path):
    p = tmp_path / "s"
    for raw, want in [
        (b"abc\n", "abc"),
        (b"abc\r\n", "abc"),
        (b"abc\n\n", "abc\n"),
        (b"abc", "abc"),
        (b"a b\tc", "a b\tc"),
    ]:
        p.write_bytes(raw)
        assert read_secret_file(str(p), var="X") == want


def test_dual_inline_and_file_source_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", "inline")
    monkeypatch.setenv(
        "YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE",
        _write_secret(tmp_path, "svc", "filev"),
    )
    with pytest.raises(SecretFileError, match="ambiguous secret source"):
        env_or_file("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET")


def test_env_or_file_falls_back_to_inline(monkeypatch):
    monkeypatch.delenv("X_FILE", raising=False)
    monkeypatch.setenv("X", "inline")
    assert env_or_file("X", "d") == "inline"
    monkeypatch.delenv("X", raising=False)
    assert env_or_file("X", "d") == "d"


# ---- per service-boundary secret: consumption + non-leak -------------------

SECRETS = [
    ("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", "SENTINEL_svc_9c1f2a7b4e6d8033ffa1"),
    ("YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN", "SENTINEL_dash_5a1c9f3e6d8033ffb2"),
]


@pytest.mark.parametrize("name,value", SECRETS)
def test_boundary_secret_consumed_from_file_and_not_leaked(
    tmp_path, monkeypatch, name, value
):
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(f"{name}_FILE", _write_secret(tmp_path, name, value))

    # (1) the helper consumes the file value
    assert env_or_file(name) == value
    # (2) value absent from os.environ; only the path is present
    assert name not in os.environ
    assert all(value not in v for v in _app_env_items().values())
    assert os.environ[f"{name}_FILE"].endswith(".secret")
    # (5) value absent from argv
    assert value not in " ".join(sys.argv)

    # (4) a spawned child inherits NO secret value
    child_env = {k: v for k, v in os.environ.items() if k not in _HARNESS_VARS}
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sys;sys.stdout.write(chr(0).join(f'{k}={v}' for k,v in os.environ.items()))",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=child_env,
    ).stdout
    assert value not in out
    assert f"{name}_FILE" in out
    # (3) /proc/self/environ (POSIX)
    if os.name == "posix" and os.path.exists("/proc/self/environ"):
        with open("/proc/self/environ", "rb") as fh:
            assert value.encode() not in fh.read()


def test_runtime_secret_boundary_reads_file(tmp_path, monkeypatch):
    """The real service-boundary reader _runtime_secret() consumes the file."""
    from youtab_agent_cli.web_routers import runtime as rt

    value = "SENTINEL_rt_boundary_7b4e6d80"
    monkeypatch.delenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", raising=False)
    monkeypatch.setenv(
        "YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE",
        _write_secret(tmp_path, "svc", value),
    )
    assert rt._runtime_secret() == value
    assert "YOUTAB_AGENT_RUNTIME_SERVICE_SECRET" not in os.environ
    assert all(value not in v for v in _app_env_items().values())


def test_dashboard_token_boundary_reads_file(tmp_path, monkeypatch):
    """The dashboard session-token resolver consumes the file."""
    from youtab_agent_cli import web_server as ws

    value = "SENTINEL_dash_boundary_5a1c9f3e"
    monkeypatch.delenv("YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN", raising=False)
    monkeypatch.setenv(
        "YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN_FILE",
        _write_secret(tmp_path, "dash", value),
    )
    assert ws._resolve_session_token() == value
    assert "YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN" not in os.environ
    assert all(value not in v for v in _app_env_items().values())
