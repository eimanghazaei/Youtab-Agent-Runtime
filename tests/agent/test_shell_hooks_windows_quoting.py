"""Windows quoting / tokenization contract for shell-hook commands (WAVE-24).

Background
----------
``shlex.split(cmd)`` defaults to POSIX mode where backslash is the *escape*
character.  On Windows that mangles native paths: ``C:\\Program Files\\t.exe``
collapses to ``C:Program Filest.exe`` and the surviving space then splits the
token.  ``agent.shell_hooks._tokenize_command`` fixes this by driving ``shlex``
with Windows lexical rules (no escape character) when ``os.name == "nt"``,
while leaving the POSIX path byte-for-byte identical to ``shlex.split``.

Cross-platform test strategy (no blanket skips)
-----------------------------------------------
The tokenizer branches on ``os.name`` — a real production read.  Both branches
are *pure* string logic once selected (``shlex`` is pure Python), so we exercise
each branch deterministically on ANY host by pinning ``os.name`` with
``monkeypatch`` (restored automatically).  This is NOT a
``skipif(sys.platform == "win32")`` over the module — every assertion runs on
every platform.

Real subprocess execution uses ``sys.executable`` (present on every host) with
``shell=False``, so the injection/e2e tests need no POSIX shell (no ``bash``)
and no platform gate.  ``subprocess`` caches its ``_mswindows`` flag at import,
so pinning ``os.name`` only steers OUR tokenizer, not the OS exec path — which
lets a single test prove the Windows tokenizer yields a *runnable* argv for a
spaced path on Linux/macOS CI too.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from agent import shell_hooks


# ── helpers ────────────────────────────────────────────────────────────────


@pytest.fixture
def as_windows(monkeypatch):
    """Pin the tokenizer onto its Windows branch (restored after the test)."""
    monkeypatch.setattr(os, "name", "nt")


@pytest.fixture
def as_posix(monkeypatch):
    """Pin the tokenizer onto its POSIX branch (restored after the test)."""
    monkeypatch.setattr(os, "name", "posix")


def _host_quote(s: str) -> str:
    """Quote a single command token for the CURRENT host's tokenizer branch.

    Real-execution tests run under the host's actual ``os.name`` (not pinned),
    so they must quote the way that branch expects: POSIX-style on POSIX,
    double-quote wrapping on native Windows.
    """
    if os.name == "nt":
        return '"' + s + '"'
    return shlex.quote(s)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


_ARGV_ECHO = "import sys, json; sys.stdout.write(json.dumps(sys.argv[1:]))"


# ── POSIX branch is unchanged ──────────────────────────────────────────────


class TestPosixBranchUnchanged:
    """The POSIX contract must not shift by a single byte."""

    @pytest.mark.parametrize(
        "command",
        [
            "/usr/bin/env bash /home/u/hook.sh",
            "python3 /path/to/hook.py --flag",
            "/path/with\\ space/hook.sh",       # POSIX backslash-escaped space
            "'quoted path/hook.sh' arg",
            '"double quoted/h.sh" a b',
            "~/hook.sh",
            "cmd 'a b' \"c d\" e",
            "prog a#b",                          # '#' is not a comment
        ],
    )
    def test_identical_to_shlex_split(self, command, as_posix):
        assert shell_hooks._tokenize_command(command) == shlex.split(command)


# ── Windows branch keeps backslashes + honours quotes ──────────────────────


class TestWindowsBranch:
    def test_bare_backslash_path_not_mangled(self, as_windows):
        assert shell_hooks._tokenize_command(r"C:\tools\hook.exe --x") == [
            r"C:\tools\hook.exe",
            "--x",
        ]

    def test_quoted_path_with_spaces_and_args(self, as_windows):
        got = shell_hooks._tokenize_command(
            '"C:\\Program Files\\tool.exe" --flag "arg with space"'
        )
        assert got == ["C:\\Program Files\\tool.exe", "--flag", "arg with space"]

    def test_unicode_quoted_path(self, as_windows):
        got = shell_hooks._tokenize_command('"C:\\Users\\u\\برنامه ها\\hook.exe" -x')
        assert got == ["C:\\Users\\u\\برنامه ها\\hook.exe", "-x"]

    def test_interpreter_prefix_with_backslash_script(self, as_windows):
        assert shell_hooks._tokenize_command("python C:\\hooks\\h.py") == [
            "python",
            "C:\\hooks\\h.py",
        ]

    def test_quotes_and_spaces_in_args_stay_separated(self, as_windows):
        assert shell_hooks._tokenize_command('tool "a\\b c" d') == [
            "tool",
            "a\\b c",
            "d",
        ]

    def test_posix_shlex_would_mangle_same_input(self, as_windows):
        """Documents the exact defect: the old POSIX split destroys the path
        that the Windows branch now preserves."""
        native = r"C:\Program Files\tool.exe"
        assert shlex.split(native) != [native]          # old behaviour: mangled
        # Quoted, the Windows tokenizer preserves it intact:
        assert shell_hooks._tokenize_command(f'"{native}"') == [native]


# ── Documented limitation: a bare UNQUOTED spaced path is ambiguous ────────


class TestBareUnquotedSpacedPathContract:
    def test_bare_unquoted_spaced_path_splits_and_must_be_quoted(self, as_windows):
        """No string tokenizer (POSIX or Windows) can disambiguate a bare
        unquoted path containing spaces from a path + argument — identical to
        the POSIX contract.  The supported form is to QUOTE the path, proven
        by :meth:`TestWindowsBranch.test_quoted_path_with_spaces_and_args`."""
        assert shell_hooks._tokenize_command("C:\\Program Files\\t.exe --flag") == [
            "C:\\Program",
            "Files\\t.exe",
            "--flag",
        ]


# ── Malformed config fails closed with a stable, typed error ───────────────


class TestMalformedFailsClosed:
    @pytest.mark.parametrize("pin", ["nt", "posix"])
    def test_unbalanced_quote_raises_typed_error(self, pin, monkeypatch):
        monkeypatch.setattr(os, "name", pin)
        with pytest.raises(shell_hooks.HookCommandError) as ei:
            shell_hooks._tokenize_command('"C:\\unterminated')
        # Typed *and* backwards-compatible with existing `except ValueError`.
        assert isinstance(ei.value, ValueError)

    def test_spawn_on_malformed_command_fails_closed(self, as_windows):
        """A mis-tokenized command must NOT execute: _spawn returns an error
        diagnostic with no returncode (nothing ran) rather than raising or
        silently passing."""
        spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command='"C:\\oops')
        result = shell_hooks._spawn(spec, "{}")
        assert result["error"] is not None
        assert result["returncode"] is None

    def test_malformed_callback_returns_none_no_block(self, as_windows):
        """The live callback surfaces neither a block nor a crash on malformed
        config — the hook simply does not fire."""
        spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command='"C:\\oops')
        cb = shell_hooks._make_callback(spec)
        assert cb(tool_name="terminal", args={}) is None


# ── Command-injection regression: shell metacharacters stay literal ────────


class TestNoShellInjection:
    def test_metacharacters_not_interpreted_by_a_shell(self, tmp_path):
        """A hook arg full of shell metacharacters must reach the child as a
        single literal argument, and no injected command may run."""
        echo = _write(tmp_path, "argv_echo.py", _ARGV_ECHO)
        payload = "a & echo PWNED | b ; c $(whoami) `id` %USERNAME%"
        command = " ".join(
            [_host_quote(sys.executable), _host_quote(str(echo)), _host_quote(payload)]
        )
        spec = shell_hooks.ShellHookSpec(event="on_session_start", command=command)
        result = shell_hooks._spawn(spec, "{}")

        received = json.loads(result["stdout"])
        # (a) the payload arrived as exactly ONE literal arg …
        assert received == [payload]
        # (b) … and nothing else ran: stdout is exactly the child's own argv
        #     JSON.  A shell would have appended `echo PWNED` output here.
        assert result["stdout"] == json.dumps([payload])

    def test_spawn_always_uses_shell_false_and_list_argv(self, tmp_path, monkeypatch):
        echo = _write(tmp_path, "argv_echo.py", _ARGV_ECHO)
        captured = {}
        real_run = shell_hooks.subprocess.run

        def spy(*args, **kwargs):
            captured["shell"] = kwargs.get("shell")
            captured["argv"] = args[0] if args else kwargs.get("args")
            return real_run(*args, **kwargs)

        monkeypatch.setattr(shell_hooks.subprocess, "run", spy)
        command = " ".join([_host_quote(sys.executable), _host_quote(str(echo))])
        shell_hooks._spawn(
            shell_hooks.ShellHookSpec(event="on_session_start", command=command), "{}"
        )
        assert captured["shell"] is False
        assert isinstance(captured["argv"], list)


# ── Invocation forms: .exe / .cmd / .bat / script forms ────────────────────


class TestInvocationForms:
    @pytest.mark.parametrize("ext", ["exe", "cmd", "bat", "sh", "bash", "py", "js"])
    def test_each_form_tokenizes_to_single_program_token(self, ext, as_windows):
        """Every documented invocation form resolves to a single program token
        plus its args — the correct structured argv for ``shell=False``."""
        got = shell_hooks._tokenize_command(f'"C:\\hooks\\hook.{ext}" --flag')
        assert got == [f"C:\\hooks\\hook.{ext}", "--flag"]

    def test_cmd_bat_supported_only_via_explicit_interpreter(self, as_windows):
        """``.cmd``/``.bat`` are not PE executables; the safe, portable form is
        an explicit interpreter argv (``cmd /c <path>``) — a *structured* argv,
        never ``shell=True`` string interpolation."""
        assert shell_hooks._tokenize_command('cmd /c "C:\\hooks\\deploy.cmd"') == [
            "cmd",
            "/c",
            "C:\\hooks\\deploy.cmd",
        ]

    def test_real_exe_form_executes(self, tmp_path):
        """The ``.exe`` form runs directly with ``shell=False`` (sys.executable
        is a native executable on every host)."""
        echo = _write(tmp_path, "argv_echo.py", _ARGV_ECHO)
        command = " ".join(
            [_host_quote(sys.executable), _host_quote(str(echo)), "hello"]
        )
        result = shell_hooks._spawn(
            shell_hooks.ShellHookSpec(event="on_session_start", command=command), "{}"
        )
        assert json.loads(result["stdout"]) == ["hello"]

    def test_real_script_form_block_end_to_end(self, tmp_path):
        """Interpreter + script form flows through _make_callback and its
        block directive is translated to the canonical Youtab shape."""
        blocker = _write(
            tmp_path,
            "blocker.py",
            "import sys, json; sys.stdin.read();"
            " sys.stdout.write(json.dumps({'decision': 'block',"
            " 'reason': 'no terminal'}))",
        )
        command = " ".join([_host_quote(sys.executable), _host_quote(str(blocker))])
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call", command=command, matcher="terminal"
        )
        cb = shell_hooks._make_callback(spec)
        assert cb(tool_name="terminal", args={"command": "rm -rf /"}) == {
            "action": "block",
            "message": "no terminal",
        }

    def test_windows_tokenizer_yields_runnable_argv_for_spaced_path(
        self, tmp_path, monkeypatch
    ):
        """Force the Windows tokenizer, put the script under a directory with a
        space, quote it, and confirm the resulting argv actually runs.  Proves
        the fix end-to-end for a spaced path on any host (subprocess runs
        natively; only OUR tokenizer is pinned to the Windows branch)."""
        spaced = tmp_path / "a b c"
        spaced.mkdir()
        blocker = _write(
            spaced,
            "hook.py",
            "import sys, json; sys.stdin.read();"
            " sys.stdout.write(json.dumps({'action': 'block', 'message': 'spaced'}))",
        )
        monkeypatch.setattr(os, "name", "nt")
        command = f'"{sys.executable}" "{blocker}"'
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call", command=command, matcher="terminal"
        )
        cb = shell_hooks._make_callback(spec)
        assert cb(tool_name="terminal", args={}) == {
            "action": "block",
            "message": "spaced",
        }


# ── Doctor / drift helpers use the same platform-aware tokenizer ───────────


class TestDoctorHelpersUseWindowsTokenizer:
    def test_command_script_path_resolves_backslash_form(self, as_windows):
        assert (
            shell_hooks._command_script_path("python C:\\hooks\\hook.py")
            == "C:\\hooks\\hook.py"
        )

    def test_command_script_path_malformed_returns_input(self, as_windows):
        # Fail-closed: unparseable command is returned verbatim, not crashed.
        assert shell_hooks._command_script_path('"C:\\oops') == '"C:\\oops'
