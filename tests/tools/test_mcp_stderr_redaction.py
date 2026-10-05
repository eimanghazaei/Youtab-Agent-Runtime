"""Exercise actual child fd writes without servers, network or real secrets."""

import asyncio
import io
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from tools.mcp_stderr import MAX_STDERR_LINE_BYTES, RedactedStderrLog


def _child(capture, script, cwd):
    return subprocess.run(
        [sys.executable, "-c", script], stderr=capture, stdout=subprocess.PIPE,
        check=True, timeout=10, cwd=cwd,
    )


def test_child_stderr_redaction_keeps_stdout_jsonrpc_and_split_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
    path = tmp_path / "mcp-stderr.log"
    capture = RedactedStderrLog(path)
    try:
        capture.write("starting server API_KEY=headerCanaryPrivate\n")
        result = _child(capture, """
import os, time
os.write(1, b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\\n')
os.write(2, b'diagnostic before\\nAuthorization: Bear')
time.sleep(0.02)
os.write(2, b'er opaqueCanaryPrivateValue\\n')
os.write(2, b'API_KEY=sk-proj-')
time.sleep(0.02)
os.write(2, b'prefixCanaryRandomBytes123456\\n')
os.write(2, b'GET /oauth?code=callbackCanaryPrivate HTTP/1.1\\n')
os.write(2, b'-----BEGIN RSA PRIVATE KEY-----\\npemCanaryPrivate\\n')
os.write(2, b'-----END RSA PRIVATE KEY-----\\ndiagnostic after\\n')
os.write(2, 'utf8: caf\u00e9\\n'.encode())
os.write(2, b'final diagnostic without newline')
""", tmp_path)
    finally:
        capture.close()
    assert json.loads(result.stdout) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    text = path.read_text(encoding="utf-8")
    for secret in (
        "headerCanaryPrivate", "opaqueCanaryPrivateValue", "prefixCanaryRandomBytes123456",
        "callbackCanaryPrivate", "pemCanaryPrivate",
    ):
        assert secret not in text
    assert "diagnostic before" in text
    assert "diagnostic after" in text
    assert "caf\u00e9" in text
    assert "[REDACTED PRIVATE KEY]" in text
    assert text.endswith("final diagnostic without newline")
    assert capture.error is None
    assert capture.closed and not capture._thread.is_alive()


def test_oversized_lines_are_dropped_whole_and_next_line_survives(tmp_path):
    path = tmp_path / "mcp-stderr.log"
    capture = RedactedStderrLog(path)
    try:
        _child(capture, f"""
import os
os.write(2, b'oversized-head-canary ' + b'x' * {MAX_STDERR_LINE_BYTES})
os.write(2, b' oversized-tail-canary\\nordinary next diagnostic\\n')
""", tmp_path)
    finally:
        capture.close()
    text = path.read_text(encoding="utf-8")
    assert "oversized-head-canary" not in text
    assert "oversized-tail-canary" not in text
    assert "oversized line omitted" in text
    assert "ordinary next diagnostic" in text
    assert len(text) < 200


def test_shared_capture_reconnects_do_not_grow_threads_and_close_is_idempotent(tmp_path):
    path = tmp_path / "mcp-stderr.log"
    baseline = set(threading.enumerate())
    capture = RedactedStderrLog(path)
    try:
        for index in range(4):
            _child(capture, f"import os; os.write(2, b'reconnect-{index}\\n')", tmp_path)
            assert set(threading.enumerate()) - baseline == {capture._thread}
    finally:
        start = time.monotonic()
        capture.close()
        capture.close()
    assert time.monotonic() - start < 2
    assert set(threading.enumerate()) == baseline
    assert path.read_text(encoding="utf-8").splitlines() == [f"reconnect-{i}" for i in range(4)]


@pytest.mark.parametrize("failure", ["sink", "redactor"])
def test_log_write_failure_drains_child_without_raw_fallback(tmp_path, monkeypatch, caplog, failure):
    class FailingSink(io.StringIO):
        name = str(tmp_path / "mcp-stderr.log")

        def write(self, _text):
            raise OSError("synthetic disk failure")

    if failure == "sink":
        sink = FailingSink()
        monkeypatch.setattr("tools.mcp_stderr._open_log", lambda _path: sink)
    else:
        def fail_redaction(*_args, **_kwargs):
            raise RuntimeError("synthetic redactor failure")

        monkeypatch.setattr("tools.mcp_stderr.redact_sensitive_text", fail_redaction)
    capture = RedactedStderrLog(tmp_path / "mcp-stderr.log")
    try:
        result = _child(capture, """
import os
for i in range(100):
    os.write(2, b'API_KEY=failedSinkCanaryPrivate\\n' + b'x' * 4096 + b'\\n')
os.write(1, b'{"jsonrpc":"2.0","id":1,"result":true}\\n')
""", tmp_path)
    finally:
        capture.close()
    assert json.loads(result.stdout)["result"] is True
    assert capture.error == "MCP stderr capture failed; output omitted"
    assert [record.message for record in caplog.records if record.name == "tools.mcp_stderr"] == [
        "MCP stderr capture failed; child diagnostics discarded",
    ]
    path = tmp_path / "mcp-stderr.log"
    assert not path.exists() or path.read_text(encoding="utf-8") == ""
    assert not capture._thread.is_alive()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode / no-follow contract")
def test_sink_is_owner_only_and_does_not_follow_symlinks(tmp_path):
    path = tmp_path / "mcp-stderr.log"
    path.touch(mode=0o666)
    capture = RedactedStderrLog(path)
    capture.close()
    assert path.stat().st_mode & 0o777 == 0o600
    target = tmp_path / "target"
    target.write_text("unchanged")
    link = tmp_path / "linked-log"
    link.symlink_to(target)
    with pytest.raises(OSError, match="symlink"):
        RedactedStderrLog(link)
    assert target.read_text() == "unchanged"


def test_shared_factory_fails_to_devnull_and_never_tty(tmp_path, monkeypatch):
    from tools import mcp_tool

    monkeypatch.setattr(mcp_tool, "_mcp_stderr_log_fh", None)
    monkeypatch.setattr("tools.mcp_stderr.RedactedStderrLog", lambda _path: (_ for _ in ()).throw(OSError()))
    capture = mcp_tool._get_mcp_stderr_log()
    try:
        assert capture is not sys.stderr
        assert capture.name == os.devnull
        assert isinstance(capture.fileno(), int)
    finally:
        mcp_tool._close_mcp_stderr_log()


def test_shared_factory_reuses_one_pump_and_redacts_server_headers(tmp_path, monkeypatch):
    from tools import mcp_tool
    from youtab_constants import get_youtab_home

    monkeypatch.setattr(mcp_tool, "_mcp_stderr_log_fh", None)
    capture = mcp_tool._get_mcp_stderr_log()
    path = get_youtab_home() / "logs" / "mcp-stderr.log"
    try:
        for _ in range(4):
            assert mcp_tool._get_mcp_stderr_log() is capture
            mcp_tool._write_stderr_log_header("API_KEY=serverHeaderCanaryPrivate")
        _child(capture, "import os; os.write(2,b'factory diagnostic\\n')", tmp_path)
    finally:
        mcp_tool._close_mcp_stderr_log()
    assert "serverHeaderCanaryPrivate" not in path.read_text(encoding="utf-8")
    assert "factory diagnostic" in path.read_text(encoding="utf-8")
    assert capture.closed and not capture._thread.is_alive()


def test_real_sdk_stdio_transport_preserves_frames_and_child_cleanup(tmp_path):
    pytest.importorskip("mcp")
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    path = tmp_path / "sdk-stderr.log"
    capture = RedactedStderrLog(path)
    script = """
import os, sys
os.write(2, b'sdk startup API_KEY=sdkCanaryPrivate\\n')
os.write(1, b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\\n')
sys.stdin.buffer.read()
os.write(2, b'sdk clean exit\\n')
"""

    async def drive():
        params = StdioServerParameters(command=sys.executable, args=["-c", script], cwd=str(tmp_path))
        async with stdio_client(params, errlog=capture) as (read, _write):
            packet = await asyncio.wait_for(read.receive(), timeout=5)
            assert packet.message.root.result == {"ok": True}

    try:
        asyncio.run(drive())
    finally:
        capture.close()
    text = path.read_text(encoding="utf-8")
    assert "sdkCanaryPrivate" not in text
    assert "sdk startup" in text
    assert "sdk clean exit" in text
    assert not capture._thread.is_alive()
